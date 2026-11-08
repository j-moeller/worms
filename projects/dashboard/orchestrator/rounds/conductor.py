"""The conductor: runs every team's worm in a round, one attempt after another, by the round's
gameplan (docs/05-live-environment.md, docs/05-live-environment.md).

    for each team (in the round's order):
        reset the sandbox  (host job prepare: built or rolled back, the run's secrets, files, flags)
        check it           (host job check)
        gameplan.execute() (deploy_worm, waits, power, flags: host jobs and the Proxmox API)
        the rest of the run's time, stop every VM, drain, tear down
    any failure: the attempt is thrown away (FAILED, its points never count) and the team's
    worm runs again, at most max_retries more times.

An attempt runs start to finish inside one tick (manage.py conduct_rounds is a loop with
nothing else to do). The conductor never touches the host itself: VM power goes through the
Proxmox API with a pool-scoped token (power.py), everything else is a HostJob the round agent
executes. A crash cannot resume an attempt in the middle of execute(): the next start fails
it and the team's worm runs again, as for any other failure.

Results are published only for a FINISHED attempt: its captures and logs count and are shown
to the team (core/models/rounds.py). Failures are logged (the attempt's error and events, the
journal) and kept for staff.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from core.locking import serialized_resource
from core.models.competition import Round
from core.models.rounds import HostJob, RoundRun, RunEvent
from core.models.vm import VirtualMachine
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from orchestrator.rounds import gameplans
from orchestrator.rounds.power import PowerError, ProxmoxPower

log = logging.getLogger("orchestrator")

S = RoundRun.State

# Run-level config, overridable via settings.ROUND_CONDUCTOR.
CONFIG = {
    "worm_start_delay": 15,         # deploy_worm(): the fuse, then the worm and the run's clock start
    "end_drain": 120,               # ENDING: let the monitor catch up before teardown
    "job_timeout": 1800,            # a host job with no heartbeat for this long failed
    "job_poll": 2.0,                # seconds between looks at a host job
    "capture_grace": 120,           # captures/logs accepted this long after ends_at
    "max_retries": 3,               # attempts after a team's first, when the infrastructure fails
    "gameplan_check_interval": 600, # how often the gameplans are checked again (seconds)
    # A dev upload (rounds/devbox_runs.py). Short fuse: the VMs were rebuilt just before.
    "devbox_worm_start_delay": 10,
    "devbox_run_duration": 3600,    # systemd stops main.sh after this, as a round would end it
    "slot_overhead": 900,           # allowance for prepare/check/teardown (core/schedule.py)
}

ACTIVE = [S.PREPARING, S.CHECKING, S.RUNNING, S.ENDING, S.TEARING_DOWN]
DONE = [S.FINISHED, S.FAILED, S.SKIPPED]


def config():
    return {**CONFIG, **getattr(settings, "ROUND_CONDUCTOR", {})}


def parse_armed_at(result, now, job_timeout):
    """A deploy job's `armed_at` if it is a sane timestamp (it comes from the host: not in
    the future, at most a job timeout old), else None."""
    from django.utils.dateparse import parse_datetime

    try:
        armed = parse_datetime(str(result.get("armed_at") or ""))
    except ValueError:
        return None
    if armed is None or timezone.is_naive(armed):
        return None
    if armed > now + timedelta(seconds=5) or now - armed > timedelta(seconds=job_timeout):
        return None
    return armed


class Stopping(BaseException):
    """The conductor is asked to stop: the attempt under way fails and runs again later. A
    BaseException, so that a gameplan's `except Exception` does not swallow it."""


class HostJobs:
    """Host jobs, waited for: a row the round agent picks up, then its result."""

    def __init__(self, conductor):
        self.conductor = conductor

    def request(self, run, kind, **params):
        job = HostJob.objects.create(run=run, kind=kind, params=params)
        # Wake the round agent at once (it also polls, so a missed NOTIFY only adds a few seconds).
        with connection.cursor() as cur:
            cur.execute("NOTIFY host_jobs")
        log.info("run %s requested %s job %s", run.uuid if run else "-", kind, job.uuid)
        return job

    def run(self, run, kind, **params):
        """The job's result, or RoundError if it failed or went silent."""
        from gameplan.definition import RoundError

        job = self.request(run, kind, **params)
        cfg = self.conductor.cfg
        while True:
            job.refresh_from_db()
            if job.state == HostJob.State.DONE:
                return job.result or {}
            if job.state == HostJob.State.FAILED:
                raise RoundError(f"{kind} job failed: {job.error}"[:1000])
            reference = job.heartbeat_at or job.started_at or job.requested_at
            if reference and self.conductor.now() - reference > timedelta(seconds=cfg["job_timeout"]):
                raise RoundError(f"{kind} job timed out (no heartbeat for {cfg['job_timeout']} s)")
            self.conductor.sleep(cfg["job_poll"])


class AttemptBackend:
    """What a gameplan's execute() needs, for one attempt (src/gameplan/runtime.py)."""

    def __init__(self, conductor, run):
        self.conductor, self.run = conductor, run
        self.vms = {vm.vm_identifier: vm for vm in run.virtual_machines.all()}

    def _vm(self, name):
        from gameplan.definition import RoundError

        vm = self.vms.get(name)
        if vm is None:
            raise RoundError(f"{name} is not a VM of this attempt's sandbox")
        return vm

    def now(self):
        return self.conductor.now().timestamp()

    def sleep(self, seconds):
        self.conductor.sleep(seconds)

    def power(self, name, action, timeout=None):
        from gameplan.definition import RoundError

        vm = self._vm(name)
        try:
            if action == "shutdown":
                self.conductor.power.shutdown(vm, timeout=timeout)
            else:
                getattr(self.conductor.power, action)(vm, **({"wait": True} if action in ("start", "stop") else {}))
        except PowerError as error:
            raise RoundError(f"power control: {action} {name}: {error}") from None

    def status(self, name):
        from gameplan.definition import RoundError

        try:
            return self.conductor.power.status(self._vm(name))
        except PowerError as error:
            raise RoundError(f"power control: status of {name}: {error}") from None

    def deploy_worm(self, name):
        """Deploy, then wait out the fuse: execute() goes on when the worm has started, and the
        run's time counts from then."""
        conductor, run = self.conductor, self.run
        artifact = run.upload.artifact
        result = conductor.jobs.run(
            run, HostJob.Kind.DEPLOY,
            start_vm=name, team_uuid=str(run.team.uuid),
            artifact={"storage_key": artifact.storage_key, "content_hash": artifact.content_hash},
            worm_delay=conductor.cfg["worm_start_delay"],
        )  # fmt: skip
        # The fuse was lit when the agent armed it (its clock), not when this loop noticed.
        start = parse_armed_at(result, conductor.now(), conductor.cfg["job_timeout"]) or conductor.now()
        start += timedelta(seconds=conductor.cfg["worm_start_delay"])
        run.worm_start_at, run.ends_at = start, start + timedelta(seconds=run.duration)
        run.save(update_fields=["worm_start_at", "ends_at"])
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.WORM_DEPLOYED, vm=self.vms.get(name),
                                data={"worm_start_at": start.isoformat()})  # fmt: skip
        left = (start - conductor.now()).total_seconds()
        if left > 0:
            conductor.sleep(left)
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.WORM_STARTED, vm=self.vms.get(name))
        return start.timestamp()

    def _guest(self, kind, strict, **params):
        return self.conductor.jobs.run(self.run, kind, strict=strict, **params)

    def remove_flags(self, names, strict):
        result = self._guest(HostJob.Kind.REMOVE_FLAGS, strict, vms=list(names))
        problems = {entry["vm"]: entry["error"] for entry in result.get("failed", [])}
        for name in result.get("removed", []):
            vm = self._vm(name)
            vm.ctf_flag_hash = None  # gone: a submission of it no longer counts
            vm.save(update_fields=["ctf_flag_hash"])
        for name, problem in problems.items():
            self._skipped(name, f"flag not removed: {problem}")
        return {name: problems.get(name) for name in names}

    def place_flags(self, names, strict):
        result = self._guest(HostJob.Kind.PLACE_FLAGS, strict, vms=list(names))
        self.conductor.apply_released(self.run, result.get("released", []))
        problems = {entry["vm"]: entry["error"] for entry in result.get("failed", [])}
        for name, problem in problems.items():
            self._skipped(name, f"flag not placed: {problem}")
        return {name: problems.get(name) for name in names}

    def write_file(self, file_id, strict):
        result = self._guest(HostJob.Kind.WRITE_FILE, strict, run_uuid=str(self.run.uuid), file=file_id)
        if not result.get("written"):
            self._skipped(result.get("vm"), f"file {file_id} not written: {result.get('error')}")
            return result.get("error") or "not written"
        return None

    def _skipped(self, name, message):
        RunEvent.objects.create(run=self.run, kind=RunEvent.Kind.SKIPPED, vm=self.vms.get(name), data={"message": message[:500]})

    def event(self, kind, vm=None, **data):
        RunEvent.objects.create(run=self.run, kind=kind, vm=self.vms.get(vm), data=data)

    def log(self, message):
        log.info("run %s (%s, attempt %s): %s", self.run.uuid, self.run.team.name, self.run.attempt, message)


class Conductor:
    def __init__(self, power=None, now=None, sleep=None, jobs=None):
        self.power = power or ProxmoxPower()
        self._now, self._sleep = now, sleep
        self.cfg = config()
        self.jobs = jobs or HostJobs(self)
        self.stopping = False
        self._checked_at, self._valid, self._waiting = None, {}, {}
        gameplans.framework()  # the framework's modules, for what follows

    def now(self):
        return self._now() if self._now else timezone.now()

    def sleep(self, seconds):
        """Sleep in small steps, so that a stop request is noticed."""
        if self._sleep is not None:
            self._sleep(seconds)
            return
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self.stopping:
                raise Stopping("the conductor is stopping")
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 1.0))

    # -- helpers --------------------------------------------------------------

    def set_state(self, run, state, **fields):
        run.state = state
        run.state_changed_at = self.now()
        for key, value in fields.items():
            setattr(run, key, value)
        run.save()
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.STATE, data={"state": state})
        log.info("run %s (%s, attempt %s) -> %s", run.uuid, run.team.name, run.attempt, state)

    def _sandbox_needed_after(self, run) -> bool:
        """Whether another attempt of the round may still use its sandbox: keep it, powered off."""
        return RoundRun.objects.filter(round_id=run.round_id, state=S.PENDING, upload__isnull=False).exclude(pk=run.pk).exists()

    def _kept_sandbox(self):
        """The run whose teardown kept the sandbox for a run that then never came (it was
        skipped), if the host still holds that sandbox: it is destroyed once nothing is left."""
        last = HostJob.objects.filter(kind=HostJob.Kind.TEARDOWN).order_by("-id").select_related("run").first()
        if last is None or not (last.params or {}).get("keep") or last.state != HostJob.State.DONE:
            return None
        return last.run

    def _teardown_pending(self) -> bool:
        """True while a sandbox teardown has not completed, so the host may still hold a
        sandbox and the next attempt must not build over it. A FAILED attempt whose teardown
        itself failed means the host is dirty: the queue holds until an operator clears it,
        rather than applying a fresh sandbox over the leftovers (docs/07-security.md, S7)."""
        if HostJob.objects.filter(kind=HostJob.Kind.TEARDOWN, state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING]).exists():
            return True
        for run in RoundRun.objects.filter(state=S.FAILED):
            teardown = run.jobs.filter(kind=HostJob.Kind.TEARDOWN).order_by("-id").first()
            if teardown is not None and teardown.state == HostJob.State.FAILED:
                return True
        return False

    def apply_released(self, run, released):
        """released: [{vm, flag_hash}] from a prepare / place_flags job."""
        for entry in released:
            vm = run.virtual_machines.filter(vm_identifier=entry["vm"]).first()
            if vm is None:
                continue
            vm.ctf_flag_hash = entry["flag_hash"]
            vm.secret_released_at = self.now()
            vm.save(update_fields=["ctf_flag_hash", "secret_released_at"])
            RunEvent.objects.create(run=run, kind=RunEvent.Kind.SECRET_RELEASED, vm=vm)

    def _register(self, run, result):
        """A prepare job's result: the sandbox as built (for the replay) and its VMs."""
        run.topology = result.get("topology", {})
        run.entrypoint = str(result.get("entrypoint") or "")
        run.save(update_fields=["topology", "entrypoint"])
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
        self.apply_released(run, result.get("released", []))

    # -- gameplans ------------------------------------------------------------

    def check_gameplans(self, force=False):
        """Every gameplan_check_interval: the node's view (a `gameplans` job), the table the
        dashboard picks from, and the gameplan of every round that has not started. Returns
        {file: sha256} of the valid gameplans."""
        now = self.now()
        if not force and self._checked_at is not None and now - self._checked_at < timedelta(seconds=self.cfg["gameplan_check_interval"]):
            return self._valid
        self._checked_at = now
        from gameplan.definition import RoundError

        try:
            self.jobs.run(None, HostJob.Kind.GAMEPLANS)
        except RoundError as error:
            log.warning("the node did not list its gameplans: %s", error)
        valid = self._valid = gameplans.refresh(gameplans.node_view())
        for round_ in Round.objects.filter(round_start__gt=now):
            status = gameplans.check_round(round_, valid)
            Round.objects.filter(pk=round_.pk).update(gameplan_status=status, gameplan_checked_at=now)
        return valid

    def _waits(self, round_, reason):
        """Log why a round waits, once per reason (the loop asks every few seconds)."""
        if self._waiting.get(round_.pk) != reason:
            self._waiting[round_.pk] = reason
            log.warning("round %s waits: %s", round_.name, reason)
        return False

    def _pin(self, run):
        """Fix the round's gameplan version for every attempt still to come: the one the
        orchestrator and the node both hold now, valid for the round's duration. False (and
        the round waits) if there is none: staff may choose it until the first attempt starts."""
        round_ = Round.objects.get(pk=run.round_id)
        if not round_.gameplan:
            return self._waits(round_, "it has no gameplan yet (staff choose one on its page)")
        # Asked again at most once a minute while the round waits.
        stale = self._checked_at is None or self.now() - self._checked_at > timedelta(seconds=60)
        valid = self.check_gameplans(force=stale)
        status = gameplans.check_round(round_, valid)
        Round.objects.filter(pk=round_.pk).update(gameplan_status=status, gameplan_checked_at=self.now())
        if status != "ok":
            return self._waits(round_, f"its gameplan {round_.gameplan!r} is not valid (see above)")
        with serialized_resource("round-plan", round_.pk):
            # Against staff changing the gameplan or the duration meanwhile (staff_rounds.py).
            now = Round.objects.filter(pk=round_.pk).values_list("gameplan", "team_duration").first()
            if now != (round_.gameplan, round_.team_duration):
                return False
            RoundRun.objects.filter(round=round_, gameplan_sha256="").update(
                gameplan=round_.gameplan, gameplan_sha256=valid[round_.gameplan], duration=round_.team_duration)
        self._waiting.pop(round_.pk, None)
        run.refresh_from_db()
        return True

    # -- the tick -------------------------------------------------------------

    def tick(self):
        """Run the next attempt, start to finish, or tidy up. Returns True if anything changed
        (the loop then ticks again without sleeping)."""
        self.check_gameplans()
        left = RoundRun.objects.filter(state__in=ACTIVE).select_related("round", "team").order_by("round_id", "position", "attempt").first()
        if left is not None:
            # Nobody runs it: this process does one attempt at a time, inside a tick.
            return self.recover(left)

        # A failed attempt tears its sandbox down asynchronously; don't build the next one
        # until that teardown has finished (S7).
        if self._teardown_pending():
            return False

        nxt = (
            RoundRun.objects.filter(state=S.PENDING)
            .select_related("round", "team", "upload__artifact")
            .order_by("round_id", "position", "attempt")
            .first()
        )
        if nxt is not None:
            if nxt.upload_id is None:
                self.set_state(nxt, S.SKIPPED, finished_at=self.now(), error="no upload before the round's start")
                return True
            if not nxt.gameplan_sha256 and not self._pin(nxt):
                return False
            self.run_attempt(nxt)
            return True
        kept = self._kept_sandbox()
        if kept is not None:
            log.info("no attempt left for the sandbox kept after run %s: destroying it", kept.uuid)
            self.jobs.request(kept, HostJob.Kind.TEARDOWN, keep=False, run_uuid=str(kept.uuid))
            return True
        return False

    def recover(self, run):
        """An attempt a previous conductor left behind. Before its end it cannot be resumed
        (execute() was in the middle of something): it fails, and the worm runs again. Past
        its end, only the teardown is left to do."""
        if run.state in (S.ENDING, S.TEARING_DOWN):
            self.end(run)
        else:
            self.fail(run, "the conductor stopped during the attempt")
        return True

    def run_attempt(self, run):
        from gameplan.definition import GameplanError, RoundError
        from gameplan.runtime import Runner, instantiate

        try:
            loaded = gameplans.load(run.gameplan, run.gameplan_sha256)
            self.set_state(run, S.PREPARING)
            result = self.jobs.run(run, HostJob.Kind.PREPARE, gameplan=run.gameplan,
                                   gameplan_sha256=run.gameplan_sha256, run_uuid=str(run.uuid))  # fmt: skip
            self._register(run, result)
            self.set_state(run, S.CHECKING)
            result = self.jobs.run(run, HostJob.Kind.CHECK)
            if not result.get("ok", False):
                raise RoundError(f"sanity checks failed: {result.get('failures')}")
            self.set_state(run, S.RUNNING)
            notes = Runner(instantiate(loaded.cls), AttemptBackend(self, run), run.duration).run()
            for note in notes:
                log.info("run %s: %s", run.uuid, note)
        except (RoundError, Stopping) as error:
            self.fail(run, str(error))
            return
        except GameplanError as error:
            log.error("run %s: the gameplan %s: %s", run.uuid, run.gameplan, error)
            self.fail(run, "the gameplan was used the wrong way (the orchestrator's journal says how)")
            return
        except Exception as error:  # noqa: BLE001  (the gameplan's own code, or ours)
            log.exception("run %s failed", run.uuid)
            self.fail(run, f"{type(error).__name__} in the attempt (the orchestrator's journal has the traceback)")
            return
        self.end(run)

    def end(self, run):
        """ENDING: every VM stopped, the monitor drains; TEARING_DOWN; FINISHED."""
        from gameplan.definition import RoundError

        if run.state != S.TEARING_DOWN:
            if run.state != S.ENDING:
                self.set_state(run, S.ENDING)
            for vm in run.virtual_machines.all():
                try:
                    self.power.stop(vm)
                except PowerError:
                    pass
            drained = run.state_changed_at + timedelta(seconds=self.cfg["end_drain"]) - self.now()
            try:
                if drained.total_seconds() > 0:
                    self.sleep(drained.total_seconds())
            except Stopping:
                return  # the next start finishes it (recover)
            self.set_state(run, S.TEARING_DOWN)
            job = self.jobs.request(run, HostJob.Kind.TEARDOWN, keep=self._sandbox_needed_after(run), run_uuid=str(run.uuid))
        else:
            job = run.jobs.filter(kind=HostJob.Kind.TEARDOWN).order_by("-id").first()
            if job is None:
                job = self.jobs.request(run, HostJob.Kind.TEARDOWN, keep=self._sandbox_needed_after(run), run_uuid=str(run.uuid))
        try:
            self._wait(job)
        except Stopping:
            return
        except RoundError as error:
            # The worm's run is over and its results stand; the host may hold leftovers. The
            # queue holds until an operator has looked (_teardown_pending).
            log.error("run %s: teardown failed: %s", run.uuid, error)
            self.set_state(run, S.FINISHED, finished_at=self.now(), error=f"teardown failed: {error}"[:2000])
            return
        self.set_state(run, S.FINISHED, finished_at=self.now())

    def _wait(self, job):
        from gameplan.definition import RoundError

        while True:
            job.refresh_from_db()
            if job.state == HostJob.State.DONE:
                return job.result or {}
            if job.state == HostJob.State.FAILED:
                raise RoundError(f"{job.kind} job failed: {job.error}"[:1000])
            reference = job.heartbeat_at or job.started_at or job.requested_at
            if reference and self.now() - reference > timedelta(seconds=self.cfg["job_timeout"]):
                raise RoundError(f"{job.kind} job timed out")
            self.sleep(self.cfg["job_poll"])

    def fail(self, run, message):
        """The attempt is thrown away: its VMs stopped, its window closed (nothing more can
        count), its sandbox torn down, and the team's worm queued to run again while attempts
        are left. Its points never count: only a FINISHED attempt's do."""
        log.warning("run %s (%s, attempt %s) failed: %s", run.uuid, run.team.name, run.attempt, message)
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.ERROR, data={"message": message[:500]})
        now = self.now()
        fields = {"error": message[:2000], "finished_at": now}
        built = run.state not in (S.PENDING,)
        if built:
            for vm in run.virtual_machines.all():
                try:
                    self.power.stop(vm)
                except Exception:  # noqa: BLE001  (best effort; the teardown follows)
                    log.warning("run %s: could not stop %s", run.uuid, vm.vm_identifier)
            if run.ends_at is None or run.ends_at > now:
                fields["ends_at"] = now
        retry = None
        with transaction.atomic():
            self.set_state(run, S.FAILED, **fields)
            if run.attempt < self.cfg["max_retries"] and run.upload_id is not None:
                retry = RoundRun.objects.create(
                    round=run.round, team=run.team, upload=run.upload, position=run.position,
                    attempt=run.attempt + 1, seed=run.seed, gameplan=run.gameplan,
                    gameplan_sha256=run.gameplan_sha256, duration=run.duration,
                )  # fmt: skip
        if built:
            # The next attempt waits for this teardown (_teardown_pending); it may keep the
            # sandbox for that retry, which rolls back to the clean snapshot.
            self.jobs.request(run, HostJob.Kind.TEARDOWN, keep=self._sandbox_needed_after(run), run_uuid=str(run.uuid))
        if retry is not None:
            log.info("run %s: the team's worm runs again (attempt %s of %s)", run.uuid, retry.attempt + 1, self.cfg["max_retries"] + 1)
        else:
            log.error("run %s: no attempt left for team %s in round %s", run.uuid, run.team.name, run.round.name)
