"""A team's attempt in a round, start to finish, by the round's gameplan (docs/05-live-environment.md).
Only `worms round start` runs one (src/worms_cli/rounds.py): on the host, in the operator's
terminal, one team after another. Nothing here starts, stops, retries or waits by itself.

    with the devboxes and the orchestrator stopped:
    build()                (build_sandbox: OpenTofu's apply and the configuration, once per round;
    snapshot()              then every VM's clean snapshot, before any team's code)
    for i, team in enumerate(teams):
        preflight          (the host ready, nothing changed; else the round stops here; not for
                           the first attempt after the snapshot: the session's ran before the build)
        if i > 0:
            load_snapshot()  (rollback: every VM back to the snapshot; precisely: every attempt
                           but the first after the snapshot was taken, a later session's first too)
        prepare()          (the run's secrets, files, flags)
        check()            (the host, and probes inside every VM)
        gameplan()         (gameplan.execute(): deploy_worm, waits, power, flags), the VMs watched
                           meanwhile (AttemptBackend); the rest of the run's time, stop every VM,
                           drain, the attempt's end (teardown keep: its secrets gone, its VMs off)
    delete_snapshot()      (destroy_sandbox, once the round is complete: the session asks)
    teardown()
    any failure: the attempt is thrown away (FAILED, its points never count), written to the
    round's log, and the session stops the round there (the next team's attempt would most likely
    meet the same error); the sandbox and its snapshot stay. `worms round start <r> --resume`
    runs a new attempt of that team, from the snapshot, never from a new build, then the rest. A build that fails is kept (asked), and the
    next session continues it after OpenTofu's apply.

Errors on our side fail the attempt; nothing a team's worm does can. After the worm is deployed
every sandbox VM is assumed rooted: what the watch trusts is the host's (Proxmox's VM state, the
host's own records of why a VM stopped), never anything a VM says.

Results are published only for a FINISHED attempt: its captures and logs count and are shown
to the team (core/models/rounds.py). Failures are logged (the attempt's error and events, the
round's log) and kept for staff.
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import timedelta

from core.locking import serialized_resource
from core.models.competition import Round
from core.models.rounds import HostJob, RoundRun, RunEvent
from core.models.vm import VirtualMachine
from django.conf import settings
from django.db import transaction
from django.db.models.functions import Now
from django.utils import timezone

from orchestrator.rounds import gameplans
from orchestrator.rounds.power import PowerError, ProxmoxPower, VMStopped

log = logging.getLogger("orchestrator")

S = RoundRun.State

# Run-level config, overridable via settings.ROUND_CONDUCTOR.
CONFIG = {
    "worm_start_delay": 15,         # deploy_worm(): the fuse, then the worm and the run's clock start
    "end_drain": 120,               # ENDING: let the monitor catch up before teardown
    "job_timeout": 1800,            # a host job with no heartbeat for this long failed
    "job_poll": 2.0,                # seconds between looks at a host job
    "capture_grace": 120,           # captures/logs accepted this long after ends_at
    "watch_interval": 10,           # while execute() waits: every VM's state from the Proxmox API this often
    "observe_interval": 60,         # ... and an observe job for every VM this often (and after a start)
    "observe_timeout": 120,         # an observe job not answered within this: the attempt fails
    "memory_warn_mb": 2048,         # the host's available memory below this: a warning in the journal
    # A dev upload (rounds/devbox_runs.py). Short fuse: the VMs were rebuilt just before.
    "devbox_worm_start_delay": 10,
    "devbox_run_duration": 3600,    # systemd stops main.sh after this, as a round would end it
    "slot_overhead": 900,           # allowance for prepare/check/teardown (core/schedule.py)
}

ACTIVE = [S.PREPARING, S.CHECKING, S.RUNNING, S.ENDING, S.TEARING_DOWN]
DONE = [S.FINISHED, S.FAILED, S.SKIPPED]

# What a power action leaves a VM in.
AFTER = {"start": "running", "stop": "stopped", "shutdown": "stopped", "suspend": "paused", "resume": "running"}
# An observe job's cause of a VM not running (scripts/round_agent), for the attempt's error.
CAUSES = {
    "oom": "killed by the host's OOM killer",
    "crash": "its QEMU crashed",
    "host": "stopped by a task on the host",
    "guest": "powered off from inside",
    "unknown": "the host's records do not say why",
}


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


def waited(conductor, job, since, within=None):
    """A host job not done yet, looked at again (AttemptBackend._observed): RoundError
    if it failed, went silent (no heartbeat for job_timeout seconds), or, given `within` (seconds,
    the conductor's clock), is not done that long after the round agent could be on it; else the
    moment `within` counts from, `since` or later.

    The agent does one job at a time: while it is on another (a team's devbox reset, the devboxes
    held again: minutes), this one waits its turn and that time does not count, so that nothing a
    team asks for fails another team's attempt (job_timeout bounds the wait). The other job counts
    only while its heartbeat is fresh: a dead agent's leftover does not."""
    from gameplan.definition import RoundError

    if job.state == HostJob.State.FAILED:
        raise RoundError(f"{job.kind} job failed: {job.error}"[:1000])
    now, timeout = conductor.now(), conductor.cfg["job_timeout"]
    reference = job.heartbeat_at or job.started_at or job.requested_at
    if reference and now - reference > timedelta(seconds=timeout):
        raise RoundError(f"{job.kind} job timed out (no heartbeat for {timeout} s)")
    if within is None:
        return since
    if job.state == HostJob.State.QUEUED:
        fresh = Now() - timedelta(seconds=within)  # the database's clock, which the heartbeats are on
        if HostJob.objects.filter(state=HostJob.State.RUNNING, heartbeat_at__gte=fresh).exclude(pk=job.pk).exists():
            return now
    if (now - since).total_seconds() > within:
        raise RoundError(f"{job.kind} job not done {within} s after the agent could take it ({job.state})")
    return since


class Stopping(BaseException):
    """The conductor is asked to stop: the attempt under way fails and runs again later. A
    BaseException, so that a gameplan's `except Exception` does not swallow it."""


class AttemptFailed(BaseException):
    """The watch found the attempt broken on our side (a VM down that the worm did not power
    off, the gateway down, the host not answering): it fails, and the team's worm runs again. A
    BaseException, so that a gameplan's `except Exception` does not swallow it; once raised,
    every later step of the attempt raises it again (AttemptBackend._go_on). `failure`: the
    failed host step's account (LocalJobs), when one was the cause."""

    def __init__(self, message, failure=None):
        super().__init__(message)
        self.failure = failure


def checks_failed(result):
    """A check job's failed checks, said: (headline, detail). Grouped by what failed (`vm:<name>:
    <what>`: the same probe on every VM is one entry, with how many VMs and the first one's why);
    the detail has every distinct why with the VMs it was said of. 2026-11-24: "sanity checks
    failed: ['vm:sandbox-a:time', ...]" named fifteen VMs and no reason."""
    failed = {str(name) for name in result.get("failures") or []}  # the checks that decide (handlers.GATES)
    rows = [row for row in result.get("checks") or [] if isinstance(row, dict) and not row.get("ok")
            and (not failed or str(row.get("check")) in failed)]  # fmt: skip
    rows = rows or [{"check": name, "detail": ""} for name in sorted(failed)]
    groups = {}
    for row in rows:
        name, why = str(row.get("check")), str(row.get("detail") or "").strip()
        vm, _, what = name.removeprefix("vm:").partition(":") if name.startswith("vm:") else ("", "", name)
        groups.setdefault(what or name, []).append((vm, why))
    parts, lines = [], []
    for what, found in groups.items():
        vms = [vm for vm, _ in found if vm]
        first_vm, first_why = found[0]
        part = what + (f" on {len(vms)} VM(s)" if vms else "")
        if first_why:
            part += f" ({f'{first_vm}: ' if first_vm else ''}{first_why[:200]})"
        parts.append(part)
        whys = {}
        for vm, why in found:
            whys.setdefault(why, []).append(vm)
        for why, names in whys.items():
            named = [name for name in names if name]
            lines.append(f"{what}: {why or '(no reason given)'}" + (f"\n    on {', '.join(named)}" if named else ""))
    return "check failed: " + "; ".join(parts or ["not ok, and no check named"]), "\n".join(lines)


def failure_of(error, phase):
    """What the attempt's row keeps of why it failed (RoundRun.failure, the dashboard's staff
    pages): the failed host step's account when it was one (its kind, number, log on the host,
    the excerpt of its output that says why, or for a step that may quote the gameplan only
    where it failed: scripts/round_agent/failure.py), else the error and where it was raised
    (the last frames, a gameplan's own first: file, line and function, never the code there)."""
    found = getattr(error, "failure", None)
    if isinstance(found, dict) and found:
        return {**found, "phase": phase}
    frames = traceback.extract_tb(error.__traceback__) if getattr(error, "__traceback__", None) else []
    lines = []
    mine = [frame for frame in frames if "/gameplans/" in frame.filename]
    if mine:
        frame = mine[-1]
        lines.append(f"in the gameplan at {frame.filename}:{frame.lineno}, in {frame.name}")
    for frame in frames[-3:]:
        lines.append(f"at {frame.filename}:{frame.lineno}, in {frame.name}")
    headline = (f"{type(error).__name__}: {error}".splitlines() or [type(error).__name__])[0][:400]
    return {"phase": phase, "headline": headline, "detail": "\n".join([str(error)[:1500], *lines])}


class AttemptBackend:
    """What a gameplan's execute() needs, for one attempt (src/gameplan/runtime.py), and the
    watch over it.

    Every sandbox VM and the gateway have a state they should be in: running after the check,
    then what each power action of the gameplan left. Each wait is cut into watch_interval steps;
    after each, Proxmox says what every VM is (the pool-scoped token: QEMU's state, which a worm
    that is root in the VM cannot fake; a QEMU its guest halted is stopped, power.py). A VM not
    as it should be is explained by the host (an observe job, waited for, from its own records):
    powered off or halted from inside once the worm is armed is the worm's doing, and it stays
    off; anything else (an OOM kill, a crash, a task on the host, no record, a VM running or
    paused that should not be, the gateway not running) fails the attempt (AttemptFailed) and
    the team's worm runs again.

    Before execute() (begin), every observe_interval and after each start of a VM, an observe job
    for every VM: the round agent gives their QEMU processes the OOM killer's first pick (a
    sandbox VM goes, and its attempt runs again, never the database), the host's memory goes to
    the journal, and a devbox found running while the round holds them is shut down again. Once
    the gameplan runs these are not waited for (_observed): an agent busy with another job must
    not shift the gameplan's schedule, which is the same for every team. One that fails, or is
    late (waited), fails the attempt."""

    def __init__(self, conductor, run, gateway=None):
        from gameplan.definition import RoundError

        self.conductor, self.run = conductor, run
        self.vms = {vm.vm_identifier: vm for vm in run.virtual_machines.all()}
        self.vmids = {name: vm.proxmox_vm_id for name, vm in self.vms.items()}
        self.gateway = None  # its name; it is watched as the VMs are, and must run throughout
        if isinstance(gateway, dict) and gateway.get("name"):
            name = str(gateway["name"])
            try:
                vmid = int(gateway.get("vmid"))
            except (TypeError, ValueError):
                raise RoundError(f"prepare named the gateway {name!r} without a VMID") from None
            if name in self.vms:
                raise RoundError(f"prepare named the gateway {name!r}, a VM of the gameplan")
            self.gateway, self.vmids[name] = name, vmid
        else:
            log.warning("run %s: prepare named no gateway: the sandbox's gateway is not watched", run.uuid)
        now = conductor.now()
        self.expected = {name: "running" for name in self.vmids}  # as the check found them
        self.since = {name: now for name in self.vmids}  # since when each is as it should be
        self.powered_off = set()  # the VMs the worm powered off (the host said so)
        # From the deploy job on, the worm is on its VM, armed: its fuse runs on the VM's clock,
        # which need not match this one to the second, so a VM powered off from inside may be its
        # doing from then on, never only once this clock says the fuse is out.
        self.armed = False
        # Suspended, and no look has seen them since: a poweroff of the guest's that was on its
        # way as the suspend came may still land (QEMU ends it right after the stop).
        self.unconfirmed = set()
        self.observing = []  # [(job, counted from)]: observe jobs asked for, not waited for (_ask)
        self.asked_at = now  # when the last observe job for every VM was asked for
        self.failed = None  # the AttemptFailed raised, if any
        # (vm, how): the Runner's next record of this power action is not made: it was not done
        # (the worm made it impossible), or it is recorded already (event).
        self._recorded = None

    def _vm(self, name):
        from gameplan.definition import RoundError

        vm = self.vms.get(name)
        if vm is None:
            raise RoundError(f"{name} is not a VM of this attempt's sandbox")
        return vm

    def _go_on(self):
        """No step once the attempt has failed: a gameplan may have caught AttemptFailed."""
        if self.failed is not None:
            raise self.failed

    def _broken(self, message, failure=None):
        self.failed = AttemptFailed(message, failure)
        return self.failed

    def now(self):
        return self.conductor.now().timestamp()

    def sleep(self, seconds):
        """A wait of the gameplan (and the fuse, and the rest of the run), watched."""
        self._go_on()
        conductor = self.conductor
        deadline = conductor.now() + timedelta(seconds=max(0.0, float(seconds)))
        while True:
            left = (deadline - conductor.now()).total_seconds()
            if left <= 0:
                return
            conductor.sleep(min(float(conductor.cfg["watch_interval"]), left))
            self._look()
            self._observed()
            if conductor.on_watch is not None:
                conductor.on_watch()  # the CLI counts the flag submissions and log records so far

    def begin(self):
        """Before execute(): the watch's first look (power control answers, every VM is as the
        check found it) and an observe job for every VM, waited for, so that a fault of ours in
        the watch itself fails the attempt before the worm is deployed."""
        self._look()
        self.asked_at = self.conductor.now()
        self._took(self._observe(self.vmids.values()))

    # -- the watch ----------------------------------------------------------------------

    def _state_of(self, name):
        power = self.conductor.power
        try:
            if name == self.gateway:
                return power.status_of(self.vmids[name], name)
            return power.status(self.vms[name])
        except PowerError as error:
            raise self._broken(f"power control: the state of {name}: {error}") from None

    def _look(self, names=None):
        """What Proxmox says each VM is (every VM, or these), against what it should be. Returns
        {name: state}."""
        states = {name: self._state_of(name) for name in (names or list(self.expected))}
        off = {name: state for name, state in states.items() if state != self.expected[name]}
        if off:
            self._explain(off)
        self.unconfirmed -= set(states)  # seen paused (a poweroff on its way would have landed), or explained
        return states

    def _explain(self, off):
        """{name: state} of VMs not as they should be: the host says why, from its records since
        each was last put in that state (an earlier power action of ours is no cause)."""
        groups = {}
        for name in off:
            groups.setdefault(self.since[name], []).append(name)
        for since, names in sorted(groups.items()):
            result = self._observe([self.vmids[name] for name in names], since)
            entries = {}
            for entry in result.get("vms") or []:
                try:
                    entries[int(entry["vmid"])] = entry
                except (KeyError, TypeError, ValueError):
                    continue
            for name in names:
                entry = entries.get(self.vmids[name]) or {}
                cause = str(entry.get("cause") or "unknown")
                # Only a VM let run powers itself off: a paused one runs no code of the worm's, but
                # for a poweroff on its way as the suspend came, which the first look after the
                # suspend finds (FreezeState looks at once).
                let_run = self.expected[name] == "running" or (self.expected[name] == "paused" and name in self.unconfirmed)
                if name != self.gateway and self.armed and off[name] == "stopped" and let_run and cause == "guest":
                    self._powered_off_by_the_worm(name, entry)
                    continue
                raise self._broken(self._deviation(name, off[name], cause, entry))

    def _deviation(self, name, state, cause, entry):
        what = f"the sandbox's gateway {name}" if name == self.gateway else name
        text = f"{what} is {state}, it should be {self.expected[name]}"
        if state != "running":
            detail = str(entry.get("detail") or "")[:200]
            text += f": {CAUSES.get(cause, cause)} (cause {cause}{f'; {detail}' if detail else ''})"
            if cause == "guest" and not self.armed:
                text += ", before the worm was deployed"
        seen = entry.get("status")
        if seen and seen != state:
            text += f"; the host saw it {seen}"
        return text

    def _powered_off_by_the_worm(self, name, entry):
        self.expected[name], self.since[name] = "stopped", self.conductor.now()
        self.powered_off.add(name)
        RunEvent.objects.create(run=self.run, kind=RunEvent.Kind.VM_STOPPED, vm=self.vms[name], data={"how": "guest"})
        detail = str(entry.get("detail") or "")[:200] or "powered off from inside, the host has no other cause"
        self.log(f"{name}: {detail}: the worm's doing, it stays off")

    def _observe(self, vmids, since=None):
        from gameplan.definition import RoundError

        params = {"vmids": sorted(set(vmids))}
        if since is not None:
            params["since"] = since.isoformat()
        try:
            return self.conductor.jobs.run(self.run, HostJob.Kind.OBSERVE, within=self.conductor.cfg["observe_timeout"], **params)
        except RoundError as error:
            raise self._broken(f"the host did not say how the sandbox's VMs are: {error}", getattr(error, "failure", None)) from None

    def _ask(self):
        """An observe job for every VM, not waited for (_observed). One still queued covers a new
        ask: the agent looks when it takes it."""
        if not any(job.state == HostJob.State.QUEUED for job, _ in self.observing):
            self.asked_at = self.conductor.now()
            job = self.conductor.jobs.request(self.run, HostJob.Kind.OBSERVE, vmids=sorted(set(self.vmids.values())))
            self.observing.append((job, self.asked_at))

    def _observed(self):
        """The observe jobs asked for: each one's result once it is done; AttemptFailed if one
        failed or is late (waited). A new one every observe_interval."""
        from gameplan.definition import RoundError

        cfg, waiting = self.conductor.cfg, []
        for job, counted in self.observing:
            job.refresh_from_db()
            if job.state == HostJob.State.DONE:
                self._took(job.result or {})
                continue
            try:
                waiting.append((job, waited(self.conductor, job, counted, cfg["observe_timeout"])))
            except RoundError as error:
                raise self._broken(f"the host did not say how the sandbox's VMs are: {error}") from None
        self.observing = waiting
        if not waiting and (self.conductor.now() - self.asked_at).total_seconds() >= cfg["observe_interval"]:
            self._ask()

    def _took(self, result):
        """An observe job's result for every VM: the host's memory to the journal, the devboxes."""
        cfg = self.conductor.cfg
        memory = result.get("memory") or {}
        available = memory.get("available_mb")
        line = (f"the host's memory: {available} MB available of {memory.get('total_mb')} MB (pressure "
                f"{memory.get('psi_some_avg10')} some, {memory.get('psi_full_avg10')} full; KSM shares "
                f"{memory.get('ksm_sharing_mb')} MB, the ZFS ARC holds {memory.get('arc_mb')} MB)")  # fmt: skip
        if isinstance(available, (int, float)) and not isinstance(available, bool) and available < cfg["memory_warn_mb"]:
            log.warning("run %s: %s: below %s MB", self.run.uuid, line, cfg["memory_warn_mb"])
        else:
            self.log(line)
        running = [str(name) for name in result.get("devboxes_running") or []]
        if running:
            # Nothing here starts or stops them: the operator stopped them before the round.
            log.error("run %s: devbox VMs are running during the round, which counted on them being stopped: %s "
                      "(`worms vm stop devboxes`)", self.run.uuid, ", ".join(running)[:500])  # fmt: skip

    # -- the gameplan's steps --------------------------------------------------------------

    def power(self, name, action, timeout=None):
        """Do it and see it done. The VM is looked at first (as it should be, or explained). An
        action that meets the VM stopped (VMStopped): before the worm is deployed that fails the
        attempt; after, the host says why (_explain). Powered off from inside, a suspend or
        resume is skipped and a start stands (it ran, and is off again); anything else fails the
        attempt. Any other PowerError is ours, and fails it too."""
        from gameplan.definition import GameplanError

        self._go_on()
        vm = self._vm(name)
        self._look([name])
        if action in ("suspend", "resume") and self.expected[name] == "stopped":
            if name in self.powered_off:
                return self._not_done(name, action)
            raise GameplanError(f"{name} is stopped: it cannot {action}")  # as the dry run says
        power = self.conductor.power
        try:
            if action == "shutdown":
                power.shutdown(vm, timeout=timeout)
            else:
                getattr(power, action)(vm, **({"wait": True} if action in ("start", "stop") else {}))
        except VMStopped as error:
            if not self.armed:
                raise self._broken(f"power control: {action} {name}: {error}, before the worm was deployed") from None
            if error.ran:
                self.expected[name] = "running"  # a start or resume let it run: its guest may have stopped it since
            if action == "start":
                self.event(RunEvent.Kind.VM_STARTED, name, how="start")  # first: it ran, then it was off
                self._recorded = (name, "start")
            self._explain({name: "stopped"})  # the worm's doing, or AttemptFailed
            if action != "start":
                self._not_done(name, action)
            return
        except PowerError as error:
            raise self._broken(f"power control: {action} {name}: {error}") from None
        self.expected[name], self.since[name] = AFTER[action], self.conductor.now()
        self.powered_off.discard(name)
        if action == "suspend":
            self.unconfirmed.add(name)
        else:
            self.unconfirmed.discard(name)
        if action == "start":
            self._ask()  # its new QEMU process: the OOM priority

    def _not_done(self, name, action):
        self._skipped(name, f"{action} not done: the worm powered {name} off")
        self.log(f"{action} {name} not done: the worm powered it off")
        self._recorded = (name, action)

    def status(self, name):
        self._go_on()
        self._vm(name)
        return self._look([name])[name]

    def deploy_worm(self, name):
        """Deploy, then wait out the fuse: execute() goes on when the worm has started, and the
        run's time counts from then."""
        self._go_on()
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
        self.armed = True
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.WORM_DEPLOYED, vm=self.vms.get(name),
                                data={"worm_start_at": start.isoformat()})  # fmt: skip
        left = (start - conductor.now()).total_seconds()
        if left > 0:
            self.sleep(left)  # the fuse, watched too
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.WORM_STARTED, vm=self.vms.get(name))
        return start.timestamp()

    def _guest(self, kind, strict, **params):
        self._go_on()
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

    def read_file(self, entry, strict):
        """By its number in the gameplan's `reads`: the path stays on the host and here."""
        result = self._guest(HostJob.Kind.READ_FILE, strict, run_uuid=str(self.run.uuid), file=entry["id"])
        if result.get("error"):
            self._skipped(entry["vm"], f"file {entry['id']} not read: {result['error']}")
        return result

    def _skipped(self, name, message):
        RunEvent.objects.create(run=self.run, kind=RunEvent.Kind.SKIPPED, vm=self.vms.get(name), data={"message": message[:500]})

    def event(self, kind, vm=None, **data):
        if self._recorded is not None and self._recorded == (vm, data.get("how")):
            self._recorded = None  # the Runner's record of a power action not done, or recorded already
            return
        RunEvent.objects.create(run=self.run, kind=kind, vm=self.vms.get(vm), data=data)

    def log(self, message):
        log.info("run %s (%s, attempt %s): %s", self.run.uuid, self.run.team.name, self.run.attempt, message)


class Conductor:
    """One attempt of a team, start to finish, when `worms round start` asks for it
    (src/worms_cli/rounds.py): nothing here runs by itself. `jobs` runs the host's steps (the CLI
    runs them in its own process on the host, worms_cli/rounds.py LocalJobs), `power` switches the
    sandbox's VMs (power.py), `on_watch` is called after every look of the watch (the CLI counts
    the flag submissions and log records there)."""

    def __init__(self, jobs, power=None, now=None, sleep=None, on_watch=None):
        self.power = power or ProxmoxPower()
        self._now, self._sleep = now, sleep
        self.cfg = config()
        self.jobs = jobs
        self.on_watch = on_watch
        self.stopping = False  # Ctrl-C: the attempt under way fails at its next wait
        # The operator's yes to continuing an unfinished sandbox build (worms_cli/rounds.py): the
        # next build does (scripts/round_agent/handlers.py, build), once.
        self.resume_build = False
        # The sandbox as its snapshot was just taken (build_sandbox): the next attempt begins on
        # it without a rollback; every other one rolls it back first.
        self.fresh = False
        # The team whose preflight passed last, with no attempt since: its attempt needs no other
        # (the session's preflight before the build covers the first).
        self.checked = None
        # The preflight's failures the operator accepted this session (parts of the private
        # internet that are off): later preflights do not ask again.
        self.accepted = set()
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
                raise Stopping("stopped by the operator (Ctrl-C)")
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

    # -- the gameplan ---------------------------------------------------------

    def check_gameplans(self):
        """The host's gameplans checked now (a `gameplans` job), the table the dashboard picks
        from brought up to date, and the gameplan of every round still to come checked against
        its duration. Returns {file: sha256} of the valid gameplans."""
        from gameplan.definition import RoundError

        now = self.now()
        try:
            self.jobs.run(None, HostJob.Kind.GAMEPLANS)
        except RoundError as error:
            log.warning("the host did not list its gameplans: %s", error)
        valid = gameplans.refresh(gameplans.host_view())
        for round_ in Round.objects.filter(runs__isnull=True).distinct():
            status = gameplans.check_round(round_, valid)
            Round.objects.filter(pk=round_.pk).update(gameplan_status=status, gameplan_checked_at=now)
        return valid

    def pin(self, round_):
        """The round's gameplan checked (chosen, valid at its duration per team) and its version
        fixed on every attempt still to come. None once it is, else why not."""
        round_ = Round.objects.get(pk=round_.pk)
        if not round_.gameplan:
            Round.objects.filter(pk=round_.pk).update(gameplan_status="", gameplan_checked_at=self.now())
            return "it has no gameplan: staff choose one on its page (Staff -> Rounds)"
        valid = self.check_gameplans()
        status = gameplans.check_round(round_, valid)
        Round.objects.filter(pk=round_.pk).update(gameplan_status=status, gameplan_checked_at=self.now())
        if status != "ok":
            return f"its gameplan {round_.gameplan!r} is not valid at {round_.team_duration} s per team (`worms show gameplans`)"
        with serialized_resource("round-plan", round_.pk):
            pinned = RoundRun.objects.filter(round=round_).exclude(gameplan_sha256="").values_list("gameplan_sha256", flat=True).first()
            if pinned and pinned != valid[round_.gameplan]:
                return (f"its attempts so far ran version {pinned[:12]} of the gameplan, the host now has "
                        f"{valid[round_.gameplan][:12]}: every team must run the same version")  # fmt: skip
            RoundRun.objects.filter(round=round_, gameplan_sha256="").update(
                gameplan=round_.gameplan, gameplan_sha256=valid[round_.gameplan],
                gameplan_version_id=valid[round_.gameplan], duration=round_.team_duration)  # fmt: skip
        return None

    # -- before an attempt ----------------------------------------------------

    def preflight(self, run, ask=None):
        """The host's checks before an attempt (a preflight job; it changes nothing): None if they
        pass, else every one that failed. A part of the private internet that is off (the host's
        `optional`: not DNS, apt-cache or devpi) does not fail it once the operator accepted it:
        `ask([(check, detail), ...])` is asked once those are the only failures, and its yes holds
        for the session's later preflights (`accepted`). Without `ask` they fail it."""
        from gameplan.definition import RoundError

        name = run.round.name
        try:
            result = self.jobs.run(run, HostJob.Kind.PREFLIGHT, gameplan=run.gameplan,
                                   gameplan_sha256=run.gameplan_sha256, hold_devboxes=True)  # fmt: skip
        except RoundError as error:
            return f"the preflight failed: {error}"
        for warning in result.get("warnings") or []:
            log.warning("round %s: preflight: %s", name, str(warning)[:300])
        memory = result.get("memory") or {}
        if memory:
            log.info("round %s: preflight: the host has %s MB available of %s MB; the gameplan's VMs need %s MB and %s MB "
                     "are kept back", name, memory.get("available_mb"), memory.get("total_mb"), memory.get("needed_mb"),
                     memory.get("reserve_mb"))  # fmt: skip
        checks = [check for check in result.get("checks") or [] if isinstance(check, dict)]
        failed = [str(check) for check in result.get("failures") or []]
        failed += [str(check.get("check")) for check in checks if not check.get("ok") and str(check.get("check")) not in failed]
        detail = {str(check.get("check")): str(check.get("detail") or "")[:200] for check in checks}
        optional = {str(check) for check in result.get("optional") or []}
        unasked = [check for check in failed if check in optional and check not in self.accepted]
        if unasked and ask is not None and all(check in optional for check in failed) and ask([(check, detail.get(check, "")) for check in unasked]):
            self.accepted.update(unasked)
        waived = [check for check in failed if check in self.accepted]
        for check in waived:
            log.warning("round %s: preflight: %s (%s): accepted by the operator", name, check, detail.get(check, ""))
        failed = [check for check in failed if check not in self.accepted]
        if not failed and (result.get("ok") is True or waived):
            self.checked = run.team_id
            return None
        named = [f"{check} ({detail[check]})" if detail.get(check) else check for check in failed]
        return "preflight checks failed: " + ("; ".join(named) or "not ok, and no check named")

    # -- an attempt -----------------------------------------------------------

    def attempt(self, run):
        """A team's pending attempt: SKIPPED without an upload, else run start to finish
        (run_attempt). Returns the run as it ended."""
        if run.upload_id is None:
            self.set_state(run, S.SKIPPED, finished_at=self.now(), error="no upload before the round's start")
        else:
            self.run_attempt(run)
        run.refresh_from_db()
        return run

    def again(self, run):
        """Another attempt of a team whose last one failed: a new pending run (attempt + 1)."""
        if run.state != S.FAILED:
            raise ValueError(f"{run.team.name}'s attempt {run.attempt} is {run.state}: only a failed one is run again")
        return RoundRun.objects.create(
            round=run.round, team=run.team, upload=run.upload, position=run.position,
            attempt=run.attempt + 1, seed=run.seed, gameplan=run.gameplan,
            gameplan_sha256=run.gameplan_sha256, gameplan_version_id=run.gameplan_version_id, duration=run.duration,
        )  # fmt: skip

    def abandon(self, run):
        """An attempt an earlier `worms round start` left under way (it was killed): it cannot be
        resumed. Past its end only the teardown is left; before it, it fails."""
        if run.state in (S.ENDING, S.TEARING_DOWN):
            self.end(run)
        else:
            self.fail(run, "left under way by an earlier `worms round start` (it was stopped)", counts=False)

    def build_sandbox(self, run):
        """build(), then snapshot(): the round's sandbox from the gameplan `run` (an attempt still
        to come) has pinned, once, before the round's first attempt; the attempts then start
        from its snapshot. The host keeps a sandbox of this gameplan that has its snapshot (a
        later session of the round: nothing is built, each attempt rolls it back), takes the
        snapshot of a build configured already, and continues a build left unfinished when the
        operator said so (resume_build): never from the start once OpenTofu has built it.
        Returns the build step's result; RoundError when a step failed (no attempt is used up)."""
        resume, self.resume_build = self.resume_build, False
        params = {"gameplan": run.gameplan, "gameplan_sha256": run.gameplan_sha256, "round": run.round_id}
        built = self.jobs.run(None, HostJob.Kind.BUILD, **params, **({"resume_build": True} if resume else {}))
        if built.get("snapshot_due"):
            self.jobs.run(None, HostJob.Kind.SNAPSHOT, round=run.round_id)
            self.fresh = True
        return built

    def destroy_sandbox(self, round_id=None):
        """delete_snapshot(), then teardown(): the round's sandbox destroyed, its snapshot first
        (one that will not go is destroyed with its VM)."""
        from gameplan.definition import RoundError

        self.fresh = False
        params = {"round": round_id} if round_id is not None else {}
        try:
            self.jobs.run(None, HostJob.Kind.DELETE_SNAPSHOT, **params)
        except RoundError as error:
            log.warning("the sandbox's snapshots were not deleted (%s): destroying its VMs deletes them", error)
        return self.jobs.run(None, HostJob.Kind.TEARDOWN, keep=False, **params)

    def run_attempt(self, run):
        from gameplan.definition import GameplanError, RoundError
        from gameplan.runtime import Runner, instantiate

        try:
            loaded = gameplans.load(run.gameplan, run.gameplan_sha256)
            self.set_state(run, S.PREPARING)
            fresh, self.fresh = self.fresh, False  # whatever this attempt does, the next one rolls back
            self.checked = None  # and has a preflight of its own
            if not fresh:
                # load_snapshot(): nothing of the team before is left.
                self.jobs.run(run, HostJob.Kind.ROLLBACK, gameplan=run.gameplan, gameplan_sha256=run.gameplan_sha256)
            prepared = self.jobs.run(run, HostJob.Kind.PREPARE, gameplan=run.gameplan, gameplan_sha256=run.gameplan_sha256,
                                     run_uuid=str(run.uuid))  # fmt: skip
            self._register(run, prepared)
            self.set_state(run, S.CHECKING)
            # With the run, the host probes inside every VM too: they are ours until the worm.
            result = self.jobs.run(run, HostJob.Kind.CHECK, run_uuid=str(run.uuid))
            if not result.get("ok", False):  # its warnings: in the check's own log (handlers.check)
                headline, detail = checks_failed(result)
                error = RoundError(headline[:1000])
                last = getattr(self.jobs, "last", None) or {}  # the check's own job and log (worms_cli/rounds.py LocalJobs)
                error.failure = {"step": "check", "job": last.get("job"), "log": last.get("log", ""),
                                 "headline": headline[:400], "detail": detail[:3500]}  # fmt: skip
                raise error
            self.set_state(run, S.RUNNING)
            backend = AttemptBackend(self, run, gateway=prepared.get("gateway"))
            backend.begin()
            notes = Runner(instantiate(loaded.cls), backend, run.duration).run()
            if backend.failed is not None:
                raise backend.failed  # the gameplan caught it and went on
            for note in notes:
                log.info("run %s: %s", run.uuid, note)
        except (RoundError, Stopping, AttemptFailed) as error:
            self.fail(run, str(error), counts=not isinstance(error, Stopping), ours=isinstance(error, AttemptFailed),
                      failure=failure_of(error, run.state))  # fmt: skip
            return
        except GameplanError as error:
            log.error("run %s: the gameplan %s: %s", run.uuid, run.gameplan, error)
            self.fail(run, f"the gameplan was used the wrong way: {error}"[:2000], failure=failure_of(error, run.state))
            return
        except Exception as error:  # noqa: BLE001  (the gameplan's own code, or ours)
            log.exception("run %s failed", run.uuid)
            self.fail(run, f"{type(error).__name__} in the attempt: {error} (the round's log has the traceback)"[:2000],
                      failure=failure_of(error, run.state))  # fmt: skip
            return
        self.end(run)

    def end(self, run):
        """ENDING: every VM stopped, the monitor drains; TEARING_DOWN (the sandbox kept, powered
        off, for the next attempt); FINISHED."""
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
                pass  # the drain cut short: the rest of the teardown still runs
            self.set_state(run, S.TEARING_DOWN)
        try:
            self.jobs.run(run, HostJob.Kind.TEARDOWN, keep=True, run_uuid=str(run.uuid))
        except RoundError as error:
            # The worm's run is over and its results stand; the host may hold leftovers.
            log.error("run %s: teardown failed: %s", run.uuid, error)
            self.set_state(run, S.FINISHED, finished_at=self.now(), error=f"teardown failed: {error}"[:2000],
                           failure=self._where(run, failure_of(error, S.TEARING_DOWN)))  # fmt: skip
            return
        self.set_state(run, S.FINISHED, finished_at=self.now())

    def _where(self, run, failure):
        """A failure's account with when, and where the session keeps its logs (the jobs'
        `context`: worms_cli/rounds.py LocalJobs), for the dashboard to say where to look."""
        context = getattr(self.jobs, "context", None)
        where = context(run) if callable(context) else {}
        return {**(where or {}), **(failure or {}), "at": self.now().isoformat()}

    def fail(self, run, message, counts=True, ours=False, failure=None):
        """The attempt is thrown away: its VMs stopped, its window closed (nothing more can
        count), its sandbox kept, powered off, for the next attempt. Its points never count: only
        a FINISHED attempt's do. counts: the attempt's own failure, not the operator stopping it.
        ours: a fault of ours whatever the team's worm did (the watch's or power control's).
        failure: why, for the attempt's row (failure_of)."""
        from gameplan.definition import RoundError

        log.warning("run %s (%s, attempt %s) failed: %s", run.uuid, run.team.name, run.attempt, message)
        deploy = run.jobs.filter(kind=HostJob.Kind.DEPLOY).order_by("-id").values_list("state", flat=True).first()
        before_worm = deploy is None
        ours = counts and (ours or before_worm)
        deploy_failed = counts and not before_worm and deploy != HostJob.State.DONE
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.ERROR,
                                data={"message": message[:500], "before_worm": before_worm, "ours": ours,
                                      "deploy_failed": deploy_failed, "conductor_stopped": not counts})  # fmt: skip
        now = self.now()
        headline = (message.splitlines() or [""])[0][:400]
        failure = self._where(run, failure or {"phase": run.state, "headline": headline})
        fields = {"error": message[:2000], "finished_at": now, "failure": failure}
        built = run.state not in (S.PENDING,)
        if built:
            for vm in run.virtual_machines.all():
                try:
                    self.power.stop(vm)
                except Exception:  # noqa: BLE001  (best effort; the teardown follows)
                    log.warning("run %s: could not stop %s", run.uuid, vm.vm_identifier)
            if run.ends_at is None or run.ends_at > now:
                fields["ends_at"] = now
        self.set_state(run, S.FAILED, **fields)
        if built:
            try:
                self.jobs.run(run, HostJob.Kind.TEARDOWN, keep=True, run_uuid=str(run.uuid))
            except RoundError as error:
                log.error("run %s: teardown failed: %s", run.uuid, error)
