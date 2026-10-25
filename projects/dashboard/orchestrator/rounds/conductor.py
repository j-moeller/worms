"""The conductor: drives each team's run through its states, requests the
privileged work from the Proxmox host (host_jobs) and runs the game plan.

It never touches the host directly. The only privileged thing it does itself
is sandbox VM power (orchestrator/rounds/power.py, a pool-scoped token); every
host-level operation (build the sandbox, checks, deploy the worm, release
secrets, teardown) is a HostJob the round agent executes. The conductor is a
single loop (manage.py conduct_rounds) that advances one step per tick, so a
crash just resumes from the database state.

State machine of a RoundRun (see docs/rounds.md):

    PENDING ─ prepare ─▶ PREPARING ─▶ CHECKING ─▶ DEPLOYING ─▶ ARMED
                                                                 │ worm_start_at
                                                                 ▼
    FINISHED ◀─ TEARING_DOWN ◀─ ENDING ◀──────────────────── RUNNING
                                 (ends_at)              (game plan steps)

A failed host job or an exception moves the run to FAILED and tears the
sandbox down. One run is active at a time (one sandbox fits on the host).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from core.models.rounds import GamePlanStep, HostJob, RoundRun, RunEvent
from core.models.vm import VirtualMachine
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from orchestrator.rounds.power import ProxmoxPower, PowerError

log = logging.getLogger("orchestrator")

# Run-level config, overridable via settings.ROUND_CONDUCTOR.
CONFIG = {
    "worm_start_delay": 300,        # ARMED -> RUNNING: the fuse before the worm starts
    "round_duration": 3600,         # worm start -> ENDING
    "end_drain": 120,               # ENDING: let the monitor catch up before teardown
    "job_timeout": 1800,            # a host job with no heartbeat for this long failed
    "capture_grace": 120,           # captures/logs accepted this long after ends_at
}


def config():
    return {**CONFIG, **getattr(settings, "ROUND_CONDUCTOR", {})}


class Conductor:
    def __init__(self, power=None, now=None):
        self.power = power or ProxmoxPower()
        self._now = now
        self.cfg = config()

    def now(self):
        return self._now() if self._now else timezone.now()

    # -- helpers --------------------------------------------------------------

    def set_state(self, run, state, **fields):
        run.state = state
        run.state_changed_at = self.now()
        for key, value in fields.items():
            setattr(run, key, value)
        run.save()
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.STATE, data={"state": state})
        log.info("run %s -> %s", run.uuid, state)

    def fail(self, run, message):
        log.warning("run %s failed: %s", run.uuid, message)
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.ERROR, data={"message": message[:500]})
        # Tear the sandbox down unless prepare never built one.
        if run.state in (RoundRun.State.PENDING, RoundRun.State.PREPARING):
            self.set_state(run, RoundRun.State.FAILED, error=message[:2000], finished_at=self.now())
        else:
            self.request_job(run, HostJob.Kind.TEARDOWN)
            self.set_state(run, RoundRun.State.FAILED, error=message[:2000])

    def request_job(self, run, kind, **params):
        job = HostJob.objects.create(run=run, kind=kind, params=params)
        # Wake the round agent at once (it also polls, so a missed NOTIFY only
        # adds a few seconds).
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute("NOTIFY host_jobs")
        log.info("run %s requested %s job %s", run.uuid, kind, job.uuid)
        return job

    def active_job(self, run, kind):
        return run.jobs.filter(kind=kind).order_by("-id").first()

    def timing(self, run):
        """The run's fuse and duration: its round's own (sandbox_params
        "timing", set on the dashboard; core/schedule.py), else the defaults."""
        own = (run.sandbox_params or {}).get("timing") or {}

        def pick(key, default):
            value = own.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default

        return {
            "worm_start_delay": pick("worm_start_delay", self.cfg["worm_start_delay"]),
            "run_duration": pick("run_duration", self.cfg["round_duration"]),
        }

    def _teardown_pending(self) -> bool:
        """True while a sandbox teardown has not completed, so the host may
        still hold a sandbox and the next run must not build over it. A teardown
        that is queued/running means wait; a FAILED run whose teardown itself
        failed means the host is dirty and the queue holds until an operator
        clears it, rather than applying a fresh sandbox over the leftovers
        (docs/security-assessment.md, S7)."""
        if HostJob.objects.filter(
            kind=HostJob.Kind.TEARDOWN, state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING]
        ).exists():
            return True
        for run in RoundRun.objects.filter(state=RoundRun.State.FAILED):
            teardown = self.active_job(run, HostJob.Kind.TEARDOWN)
            if teardown is not None and teardown.state == HostJob.State.FAILED:
                return True
        return False

    # -- the tick -------------------------------------------------------------

    def tick(self):
        """Advance the one active run, or start the next pending one. Returns
        True if anything changed (the loop then ticks again without sleeping).

        Processing of the sandbox APIs' attempts into captures/logs is NOT done
        here: a separate continuous process owns it (manage.py
        process_attempts), so there is exactly one writer of captures."""
        active = RoundRun.objects.exclude(
            state__in=[RoundRun.State.FINISHED, RoundRun.State.FAILED, RoundRun.State.SKIPPED, RoundRun.State.PENDING]
        ).order_by("round_id", "position").first()
        if active is not None:
            return self.advance(active)

        # A failed run tears its sandbox down asynchronously; don't build the
        # next run's sandbox until that teardown has finished (S7).
        if self._teardown_pending():
            return False

        nxt = RoundRun.objects.filter(state=RoundRun.State.PENDING).order_by("round_id", "position").first()
        if nxt is not None:
            if nxt.upload_id is None:
                self.set_state(nxt, RoundRun.State.SKIPPED, finished_at=self.now())
                return True
            self.set_state(nxt, RoundRun.State.PREPARING)
            self.request_job(nxt, HostJob.Kind.PREPARE, seed=nxt.seed, sandbox_params=nxt.sandbox_params)
            return True
        return False

    def advance(self, run):
        handler = getattr(self, f"_in_{run.state}", None)
        if handler is None:
            return False
        try:
            return handler(run)
        except PowerError as exc:
            self.fail(run, f"power control: {exc}")
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("run %s", run.uuid)
            self.fail(run, f"{type(exc).__name__}: {exc}")
            return True

    # Each _in_<state> returns True if it changed something.

    def _finished_job(self, run, kind):
        """The job's result if it is done, None while it runs; fails the run
        if the job failed or timed out."""
        job = self.active_job(run, kind)
        if job is None:
            self.fail(run, f"no {kind} job")
            return None
        if job.state == HostJob.State.DONE:
            return job.result or {}
        if job.state == HostJob.State.FAILED:
            self.fail(run, f"{kind} job failed: {job.error}"[:1000])
            return None
        reference = job.heartbeat_at or job.started_at or job.requested_at
        if reference and self.now() - reference > timedelta(seconds=self.cfg["job_timeout"]):
            self.fail(run, f"{kind} job timed out")
        return None

    def _in_preparing(self, run):
        result = self._finished_job(run, HostJob.Kind.PREPARE)
        if result is None:
            return run.state == RoundRun.State.FAILED
        self._register_topology(run, result)
        self.set_state(run, RoundRun.State.CHECKING)
        self.request_job(run, HostJob.Kind.CHECK)
        return True

    def _in_checking(self, run):
        result = self._finished_job(run, HostJob.Kind.CHECK)
        if result is None:
            return run.state == RoundRun.State.FAILED
        if not result.get("ok", False):
            self.fail(run, f"sanity checks failed: {result.get('failures')}")
            return True
        self.set_state(run, RoundRun.State.DEPLOYING)
        self._build_game_plan(run)
        artifact = run.upload.artifact
        self.request_job(
            run, HostJob.Kind.DEPLOY,
            start_vm=run.sandbox_params.get("start_vm"),
            team_uuid=str(run.team.uuid),
            artifact={"storage_key": artifact.storage_key, "content_hash": artifact.content_hash},
            initial_secrets=[s.vm.vm_identifier for s in self._initial_secret_steps(run)],
            worm_delay=self.timing(run)["worm_start_delay"],
        )  # fmt: skip
        return True

    def _in_deploying(self, run):
        result = self._finished_job(run, HostJob.Kind.DEPLOY)
        if result is None:
            return run.state == RoundRun.State.FAILED
        self._apply_released(run, result.get("released", []))
        timing = self.timing(run)
        start = self.now() + timedelta(seconds=timing["worm_start_delay"])
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.WORM_DEPLOYED, data={"worm_start_at": start.isoformat()})
        for step in self._initial_secret_steps(run):
            step.state = GamePlanStep.State.DONE
            step.executed_at = self.now()
            step.save(update_fields=["state", "executed_at"])
        self.set_state(
            run, RoundRun.State.ARMED,
            worm_start_at=start, ends_at=start + timedelta(seconds=timing["run_duration"]),
        )  # fmt: skip
        return True

    def _in_armed(self, run):
        if self.now() >= run.worm_start_at:
            RunEvent.objects.create(run=run, kind=RunEvent.Kind.WORM_STARTED)
            self.set_state(run, RoundRun.State.RUNNING)
            return True
        return False

    def _in_running(self, run):
        changed = self._run_due_steps(run)
        if self.now() >= run.ends_at:
            self.set_state(run, RoundRun.State.ENDING)
            return True
        return changed

    def _in_ending(self, run):
        # Stop every VM still running, so nothing keeps talking while the
        # monitor drains, then wait out the drain before teardown.
        if self.active_job(run, HostJob.Kind.TEARDOWN) is None:
            for vm in run.virtual_machines.all():
                try:
                    self.power.stop(vm)
                except PowerError:
                    pass
            if self.now() - run.state_changed_at < timedelta(seconds=self.cfg["end_drain"]):
                return False
            self.set_state(run, RoundRun.State.TEARING_DOWN)
            self.request_job(run, HostJob.Kind.TEARDOWN)
            return True
        return False

    def _in_tearing_down(self, run):
        result = self._finished_job(run, HostJob.Kind.TEARDOWN)
        if result is None:
            return run.state == RoundRun.State.FAILED
        self.set_state(run, RoundRun.State.FINISHED, finished_at=self.now())
        return True

    # FAILED still has to finish its teardown job before the next run starts.
    def _in_failed(self, run):
        job = self.active_job(run, HostJob.Kind.TEARDOWN)
        if job is None:
            run.finished_at = run.finished_at or self.now()
            run.save(update_fields=["finished_at"])
            return False
        if job.state in (HostJob.State.DONE, HostJob.State.FAILED):
            if not run.finished_at:
                run.finished_at = self.now()
                run.save(update_fields=["finished_at"])
            return False
        return False

    # -- game plan ------------------------------------------------------------

    def _build_game_plan(self, run):
        from orchestrator.rounds import gameplan

        if run.steps.exists():
            return
        vms = {vm.vm_identifier: vm for vm in run.virtual_machines.all()}
        plan = gameplan.generate(
            list(vms), run.seed, self.timing(run)["run_duration"], run.sandbox_params.get("game_plan")
        )
        run.sandbox_params = {**run.sandbox_params, "start_vm": plan.start_vm}
        run.save(update_fields=["sandbox_params"])
        with transaction.atomic():
            for step in plan.steps:
                GamePlanStep.objects.create(
                    run=run, position=step.position, action=step.action,
                    vm=vms[step.vm], at_seconds=step.at_seconds, after=step.after,
                )  # fmt: skip

    def _initial_secret_steps(self, run):
        return list(run.steps.filter(action=GamePlanStep.Action.RELEASE_SECRET, at_seconds__isnull=True))

    def _run_due_steps(self, run):
        elapsed = (self.now() - run.worm_start_at).total_seconds()
        done = set(run.steps.filter(state=GamePlanStep.State.DONE).values_list("position", flat=True))
        changed = False
        # REQUESTED steps (a release_secret waiting for its host job) are
        # revisited every tick so the result is applied once the job finishes;
        # PENDING steps become due when their time and preconditions are met.
        for step in run.steps.filter(state=GamePlanStep.State.REQUESTED).select_related("host_job", "vm"):
            self._run_step(run, step)
            changed = True
        for step in run.steps.filter(state=GamePlanStep.State.PENDING, at_seconds__isnull=False).order_by("position"):
            if step.at_seconds > elapsed or not set(step.after).issubset(done):
                continue
            self._run_step(run, step)
            changed = True
        return changed

    def _run_step(self, run, step):
        if step.action == GamePlanStep.Action.STOP_VM:
            self.power.stop(step.vm)
            RunEvent.objects.create(run=run, kind=RunEvent.Kind.VM_STOPPED, vm=step.vm)
            self._finish_step(step)
        elif step.action == GamePlanStep.Action.START_VM:
            self.power.start(step.vm)
            RunEvent.objects.create(run=run, kind=RunEvent.Kind.VM_STARTED, vm=step.vm)
            self._finish_step(step)
        elif step.action == GamePlanStep.Action.RELEASE_SECRET:
            # A secret needs the host: request a job once, finish when it is done.
            if step.host_job is None:
                step.host_job = self.request_job(run, HostJob.Kind.RELEASE_SECRETS, vms=[step.vm.vm_identifier])
                step.state = GamePlanStep.State.REQUESTED
                step.save(update_fields=["host_job", "state"])
            elif step.host_job.state == HostJob.State.DONE:
                self._apply_released(run, step.host_job.result.get("released", []))
                self._finish_step(step)
            elif step.host_job.state == HostJob.State.FAILED:
                step.state = GamePlanStep.State.FAILED
                step.detail = step.host_job.error[:500]
                step.save(update_fields=["state", "detail"])

    def _finish_step(self, step):
        step.state = GamePlanStep.State.DONE
        step.executed_at = self.now()
        step.save(update_fields=["state", "executed_at"])

    def _apply_released(self, run, released):
        """released: [{vm, flag_hash}] from a deploy / release_secrets job."""
        for entry in released:
            vm = run.virtual_machines.filter(vm_identifier=entry["vm"]).first()
            if vm is None:
                continue
            vm.ctf_flag_hash = entry["flag_hash"]
            vm.secret_released_at = self.now()
            vm.save(update_fields=["ctf_flag_hash", "secret_released_at"])
            RunEvent.objects.create(run=run, kind=RunEvent.Kind.SECRET_RELEASED, vm=vm)

    def _register_topology(self, run, result):
        """result from a prepare job: the sandbox as built (full topology and
        the generated OpenTofu definition, saved for the round's replay)."""
        run.topology = result.get("topology", {})
        run.sandbox_definition = result.get("sandbox_definition", "")
        run.save(update_fields=["topology", "sandbox_definition"])
        with transaction.atomic():
            for entry in result.get("vms", []):
                VirtualMachine.objects.update_or_create(
                    run=run, vm_identifier=entry["name"],
                    defaults={
                        "round": run.round,
                        "vm_maintenance_ip": entry["maintenance_ip"],
                        "proxmox_vm_id": entry["vmid"],
                        "addresses": entry.get("addresses", []),
                        "networks": entry.get("networks", []),
                    },
                )  # fmt: skip
