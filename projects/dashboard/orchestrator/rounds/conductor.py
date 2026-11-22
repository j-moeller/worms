"""The conductor: runs every team's worm in a round, one attempt after another, by the round's
gameplan (docs/05-live-environment.md).

One round at a time. The round under way (its gameplan pinned, attempts left) goes on until
every team's run is over; then the next round that can begin, earliest start first. A round
whose gameplan is missing or not valid waits alone: the rounds after it go ahead, and it is
looked at again every `gameplan_poll` seconds until staff choose one (_next, _pin). Its runs
were planned at its start, with the uploads from before it, however late the gameplan comes.

    for each team (in the round's order):
        hold the devboxes  (host job hold_devboxes, a round with devboxes_off: before every attempt)
        preflight          (host job preflight: the host ready, nothing changed; else no attempt
                            starts, none is used up, and it is tried again: _ready)
        reset the sandbox  (host job prepare: built or rolled back, the run's secrets, files, flags)
        check it           (host job check: the host, and probes inside every VM)
        gameplan.execute() (deploy_worm, waits, power, flags: host jobs and the Proxmox API), the
                           VMs watched meanwhile (AttemptBackend)
        the rest of the run's time, stop every VM, drain, tear down
    any failure: the attempt is thrown away (FAILED, its points never count) and the team's
    worm runs again, at most max_retries more times.
    a fault that trying again does not mend puts the round on hold (`worms round resume`):
    preflight_attempts failed preflights in a row, two attempts in a row that failed before
    their worm was deployed, or two teams' attempts in a row that failed on our side or in their
    deploy (_breaker).

Errors on our side fail the attempt; nothing a team's worm does can. After the worm is deployed
every sandbox VM is assumed rooted: what the watch trusts is the host's (Proxmox's VM state, the
host's own records of why a VM stopped), never anything a VM says. Nor can a team's own jobs
(a devbox reset) fail another team's attempt, or shift its schedule (waited, _observed).

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
from django.db.models import Case, Q, Value, When
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
    "max_retries": 3,               # attempts after a team's first, when the infrastructure fails
    "gameplan_check_interval": 600, # how often the gameplans are checked again (seconds)
    "gameplan_poll": 60,            # a round waiting for its gameplan is looked at again after this
    "preflight_retry": 60,          # a failed preflight (or devbox hold): tried again after this
    "preflight_attempts": 5,        # ... this many failed in a row: the round is put on hold
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
# Round.devbox_hold of a round that holds the devboxes shut down (devboxes_off when it began).
HOLDING = ("wanted", "held")

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
    """A host job not done yet, looked at again (HostJobs.run, AttemptBackend._observed): RoundError
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
    every later step of the attempt raises it again (AttemptBackend._go_on)."""


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

    def run(self, run, kind, within=None, **params):
        """The job's result, or RoundError if it failed or went silent. within: seconds the agent
        may take over it once it could be on it (waited): an observe job, which it answers in
        seconds, while the attempt it explains waits for it."""
        counted = self.conductor.now()
        job = self.request(run, kind, **params)
        while True:
            job.refresh_from_db()
            if job.state == HostJob.State.DONE:
                return job.result or {}
            counted = waited(self.conductor, job, counted, within)
            self.conductor.sleep(self.conductor.cfg["job_poll"])


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

    def _broken(self, message):
        self.failed = AttemptFailed(message)
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
            raise self._broken(f"the host did not say how the sandbox's VMs are: {error}") from None

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
        if running and Round.objects.filter(pk=self.run.round_id, devbox_hold__in=HOLDING).exists():
            log.error("run %s: devbox VMs run while round %s holds them shut down: %s; shutting them down again",
                      self.run.uuid, self.run.round.name, ", ".join(running)[:500])  # fmt: skip
            # Not waited for: the attempt goes on, and the agent takes it before the next of its jobs.
            asked = HostJob.objects.filter(kind=HostJob.Kind.HOLD_DEVBOXES, state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING])
            if not asked.exists():
                self.conductor.jobs.request(None, HostJob.Kind.HOLD_DEVBOXES, round_uuid=str(self.run.round.uuid))

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
    def __init__(self, power=None, now=None, sleep=None, jobs=None):
        self.power = power or ProxmoxPower()
        self._now, self._sleep = now, sleep
        self.cfg = config()
        self.jobs = jobs or HostJobs(self)
        self.stopping = False
        self._checked_at, self._valid, self._waiting = None, {}, {}
        self._held_said: set = set()  # the rounds on hold it has said so of (_say_held)
        self._next_look = {}  # round pk -> when a waiting round is looked at again
        self._release_after = None  # after a failed release of the devboxes: when to try again
        # round pk -> (failed tries in a row, when to try again) before an attempt (_ready). This
        # process's count: a restart, or the round put on hold (_say_held), starts it again.
        self._gate = {}
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
        sandbox and the next attempt must not build over it. An attempt (failed or finished)
        whose last teardown itself failed means the host is dirty: the queue holds until an
        operator clears it, rather than applying a fresh sandbox over the leftovers
        (docs/07-security.md, S7)."""
        teardowns = HostJob.objects.filter(kind=HostJob.Kind.TEARDOWN)
        if teardowns.filter(state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING]).exists():
            return True
        for job in teardowns.filter(state=HostJob.State.FAILED, run__state__in=[S.FAILED, S.FINISHED]).only("id", "run_id"):
            if not teardowns.filter(run_id=job.run_id, id__gt=job.id).exclude(state=HostJob.State.FAILED).exists():
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
        """Every gameplan_check_interval: the host's view (a `gameplans` job), the table the
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
            log.warning("the host did not list its gameplans: %s", error)
        valid = self._valid = gameplans.refresh(gameplans.host_view())
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

    def _pin(self, round_):
        """Whether the round can begin: its gameplan is chosen, and the orchestrator and the
        host both hold it and find it valid at the round's duration per team. Then its version
        is fixed for every attempt still to come. Otherwise the round waits, alone, and is
        looked at again after gameplan_poll seconds: staff may choose its gameplan until its
        first attempt starts. Each look is recorded (gameplan_checked_at) for the staff pages."""
        now = self.now()
        due = self._next_look.get(round_.pk)
        if due is not None and now < due:
            return False
        self._next_look[round_.pk] = now + timedelta(seconds=self.cfg["gameplan_poll"])
        round_ = Round.objects.get(pk=round_.pk)
        if not round_.gameplan:
            Round.objects.filter(pk=round_.pk).update(gameplan_status="", gameplan_checked_at=now)
            return self._waits(round_, "it has no gameplan yet (staff choose one on its page); the rounds after it go ahead")
        # The host is asked again at most once a minute while the round waits.
        stale = self._checked_at is None or now - self._checked_at > timedelta(seconds=60)
        valid = self.check_gameplans(force=stale)
        status = gameplans.check_round(round_, valid)
        Round.objects.filter(pk=round_.pk).update(gameplan_status=status, gameplan_checked_at=self.now())
        if status != "ok":
            return self._waits(round_, f"its gameplan {round_.gameplan!r} is not valid (see above); the rounds after it go ahead")
        with serialized_resource("round-plan", round_.pk):
            # Against staff changing the gameplan or the duration meanwhile (staff_rounds.py).
            current = Round.objects.filter(pk=round_.pk).values_list("gameplan", "team_duration").first()
            if current != (round_.gameplan, round_.team_duration):
                self._next_look.pop(round_.pk, None)  # changed under us: look again at once
                return False
            # The version is recorded already (check_gameplans: gameplans.refresh records each
            # version it sees); the run names it by hash and links it (core/models/rounds.py).
            RoundRun.objects.filter(round=round_, gameplan_sha256="").update(
                gameplan=round_.gameplan, gameplan_sha256=valid[round_.gameplan],
                gameplan_version_id=valid[round_.gameplan], duration=round_.team_duration)  # fmt: skip
            # Staff's devboxes_off, recorded once: from here on only the record counts (it can no
            # longer change on the dashboard either, the round has started).
            Round.objects.filter(pk=round_.pk, devbox_hold="").update(
                devbox_hold=Case(When(devboxes_off=True, then=Value("wanted")), default=Value("no")))  # fmt: skip
        self._waiting.pop(round_.pk, None)
        self._next_look.pop(round_.pk, None)
        log.info("round %s begins: gameplan %s, version %s", round_.name, round_.gameplan, valid[round_.gameplan][:12])
        return True

    def _next(self):
        """The attempt to run now, or None.

        The round under way (a pinned attempt still to come) goes on, so that one round runs
        at a time. Otherwise the rounds with attempts to come, earliest start first: one whose
        teams all lack an upload needs no gameplan (each run is skipped); any other begins once
        _pin lets it, and until then the next round is tried."""
        # A round on hold (`worms round pause`) starts no attempt, under way or not.
        self._say_held()
        pending = RoundRun.objects.filter(state=S.PENDING, round__on_hold=False).select_related("round", "team", "upload__artifact")
        under_way = pending.exclude(gameplan_sha256="").order_by("round__round_start", "round_id", "position", "attempt").first()
        if under_way is not None:
            return under_way
        for round_ in Round.objects.filter(runs__state=S.PENDING, on_hold=False).distinct().order_by("round_start", "id"):
            runs = pending.filter(round=round_).order_by("position", "attempt")
            if runs.filter(upload__isnull=False).exists() and not self._pin(round_):
                continue
            return runs.first()
        return None

    def _say_held(self):
        """Once per round put on hold: that it is."""
        held = set(Round.objects.filter(on_hold=True, runs__state=S.PENDING).distinct().values_list("pk", "name"))
        for pk, name in held - self._held_said:
            log.warning("round %s is on hold (`worms round resume %s`): no attempt of it starts", name, pk)
        for pk, _name in held:
            self._gate.pop(pk, None)  # resumed, its tries before an attempt start again from none
        self._held_said = held

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
        if self._release_devboxes():
            return True

        nxt = self._next()
        if nxt is not None:
            if nxt.upload_id is None:
                self.set_state(nxt, S.SKIPPED, finished_at=self.now(), error="no upload before the round's start")
                return True
            after = self._gate.get(nxt.round_id, (0, None))[1]
            if after is not None and self.now() < after:
                return False  # its last try before an attempt failed: not again yet
            if self._ready(nxt):
                self.run_attempt(nxt)
            return True
        # Nothing to run now (no attempt left, or every round left waits for its gameplan).
        kept = self._kept_sandbox()
        if kept is not None:
            log.info("no attempt left for the sandbox kept after run %s: destroying it", kept.uuid)
            self.jobs.request(kept, HostJob.Kind.TEARDOWN, keep=False, run_uuid=str(kept.uuid))
            return True
        return False

    # -- before an attempt; the devboxes of a round with devboxes_off ----------------------
    # The host holds them for every such round at once (round_agent/handlers.py, hold_devboxes):
    # one record of the VMs it shut down, while which it takes no devbox job.

    HOLDING = HOLDING

    def _ready(self, run):
        """Before each attempt (after _pin): the devboxes held, if the round holds them, then the
        host's preflight. True if the attempt may start. Otherwise it does not, and no attempt is
        used up: logged as an error and tried again after preflight_retry seconds; after
        preflight_attempts failures in a row the round is put on hold (`worms round resume`)."""
        round_ = run.round
        problem = self._hold_devboxes(round_) or self._preflight(run)
        if problem is None:
            self._gate.pop(round_.pk, None)
            return True
        tries = self._gate.get(round_.pk, (0, None))[0] + 1
        limit = self.cfg["preflight_attempts"]
        if tries >= limit:
            self._gate.pop(round_.pk, None)
            Round.objects.filter(pk=round_.pk).update(on_hold=True)
            log.error("round %s is put on hold: %s tries in a row before an attempt failed, the last: %s "
                      "(`worms round resume %s` once that is mended)", round_.name, tries, problem, round_.pk)  # fmt: skip
        else:
            self._gate[round_.pk] = (tries, self.now() + timedelta(seconds=self.cfg["preflight_retry"]))
            log.error("round %s: no attempt starts (try %s of %s, the next in %s s): %s",
                      round_.name, tries, limit, self.cfg["preflight_retry"], problem)  # fmt: skip
        return False

    def _hold_devboxes(self, round_):
        """Before every attempt of a round that holds the devboxes (it recorded devboxes_off when
        it began): every devbox VM and gateway shut down, until _release_devboxes. The job is
        idempotent: it shuts down any that runs again. None if they are held (or the round does
        not hold them), else why not: the attempt does not start, the memory it counts on is not
        there (fail closed)."""
        from gameplan.definition import RoundError

        if not Round.objects.filter(pk=round_.pk, devbox_hold__in=HOLDING).exists():
            return None
        try:
            result = self.jobs.run(None, HostJob.Kind.HOLD_DEVBOXES, round_uuid=str(round_.uuid))
        except RoundError as error:
            return f"the devboxes were not all shut down: {error}"
        log.info("round %s: the devboxes are shut down while its attempts run (%s VMs shut down now, %s held)",
                 round_.name, len(result.get("stopped") or []), len(result.get("held") or []))  # fmt: skip
        Round.objects.filter(pk=round_.pk, devbox_hold="wanted").update(devbox_hold="held")
        return None

    def _preflight(self, run):
        """The host's checks before an attempt (a preflight job; it changes nothing): None if they
        pass, else every one that failed."""
        from gameplan.definition import RoundError

        name = run.round.name
        holds = Round.objects.filter(pk=run.round_id, devbox_hold__in=HOLDING).exists()
        try:
            result = self.jobs.run(run, HostJob.Kind.PREFLIGHT, gameplan=run.gameplan,
                                   gameplan_sha256=run.gameplan_sha256, hold_devboxes=holds)  # fmt: skip
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
        if result.get("ok") is True and not failed:
            return None
        detail = {str(check.get("check")): str(check.get("detail") or "")[:200] for check in checks}
        named = [f"{check} ({detail[check]})" if detail.get(check) else check for check in failed]
        return "preflight checks failed: " + ("; ".join(named) or "not ok, and no check named")

    def _release_devboxes(self):
        """Once no round that holds the devboxes has a run to come or under way: the round agent
        starts again the ones it shut down. True if it did. A failure (the internet window open,
        ...) is logged and tried again after gameplan_poll seconds; the next round goes ahead
        meanwhile, and if it holds them too, they stay off until it is over."""
        from gameplan.definition import RoundError

        holding = Round.objects.filter(devbox_hold__in=self.HOLDING)
        if not holding.exists():
            return False
        # A round on hold waits for the operator, its devboxes needn't: only an attempt running holds them.
        busy = RoundRun.objects.filter(Q(state__in=ACTIVE) | Q(state=S.PENDING, round__on_hold=False)).values("round_id")
        if holding.filter(pk__in=busy).exists():
            return False
        now = self.now()
        if self._release_after is not None and now < self._release_after:
            return False
        names = ", ".join(holding.order_by("round_start", "id").values_list("name", flat=True))
        try:
            result = self.jobs.run(None, HostJob.Kind.RELEASE_DEVBOXES, round_uuid=str(holding.order_by("-id").first().uuid))
        except RoundError as error:
            self._release_after = now + timedelta(seconds=self.cfg["gameplan_poll"])
            log.error("round %s: the devboxes were not started again (tried again in %s s): %s", names, self.cfg["gameplan_poll"], error)
            return False
        self._release_after = None
        holding.exclude(pk__in=busy).update(devbox_hold="released")
        log.info("round %s is over: the devboxes are started again (%s VMs)", names, len(result.get("started") or []))
        return True

    def recover(self, run):
        """An attempt a previous conductor left behind. Before its end it cannot be resumed
        (execute() was in the middle of something): it fails, and the worm runs again. Past
        its end, only the teardown is left to do."""
        if run.state in (S.ENDING, S.TEARING_DOWN):
            self.end(run)
        else:
            self.fail(run, "the conductor stopped during the attempt", counts=False)
        return True

    def run_attempt(self, run):
        from gameplan.definition import GameplanError, RoundError
        from gameplan.runtime import Runner, instantiate

        try:
            loaded = gameplans.load(run.gameplan, run.gameplan_sha256)
            self.set_state(run, S.PREPARING)
            prepared = self.jobs.run(run, HostJob.Kind.PREPARE, gameplan=run.gameplan,
                                     gameplan_sha256=run.gameplan_sha256, run_uuid=str(run.uuid))  # fmt: skip
            self._register(run, prepared)
            self.set_state(run, S.CHECKING)
            # With the run, the round agent probes inside every VM too: they are ours until the worm.
            result = self.jobs.run(run, HostJob.Kind.CHECK, run_uuid=str(run.uuid))
            if not result.get("ok", False):
                raise RoundError(f"sanity checks failed: {result.get('failures')}")
            self.set_state(run, S.RUNNING)
            backend = AttemptBackend(self, run, gateway=prepared.get("gateway"))
            backend.begin()
            notes = Runner(instantiate(loaded.cls), backend, run.duration).run()
            if backend.failed is not None:
                raise backend.failed  # the gameplan caught it and went on
            for note in notes:
                log.info("run %s: %s", run.uuid, note)
        except (RoundError, Stopping, AttemptFailed) as error:
            self.fail(run, str(error), counts=not isinstance(error, Stopping), ours=isinstance(error, AttemptFailed))
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

    def fail(self, run, message, counts=True, ours=False):
        """The attempt is thrown away: its VMs stopped, its window closed (nothing more can
        count), its sandbox torn down, and the team's worm queued to run again while attempts
        are left. Its points never count: only a FINISHED attempt's do. counts: the attempt's
        own failure, not the conductor stopping. ours: a fault of ours whatever the team's worm
        did (the watch's or power control's: AttemptFailed). Both for _breaker, which the error
        event tells."""
        log.warning("run %s (%s, attempt %s) failed: %s", run.uuid, run.team.name, run.attempt, message)
        # Before the deploy job the team's upload had no part in it: the fault is ours. From the
        # deploy on it may be the upload's (a zip that does not unpack, a worm that breaks the
        # SSH a flag step needs), which must not put the round on hold by itself.
        deploy = run.jobs.filter(kind=HostJob.Kind.DEPLOY).order_by("-id").values_list("state", flat=True).first()
        before_worm = deploy is None
        ours = counts and (ours or before_worm)
        # The deploy job not done: the worm never ran. What failed is a step of ours (the artifact,
        # the scoring and logging servers, the VM; the agent does not say which) or the upload.
        deploy_failed = counts and not before_worm and deploy != HostJob.State.DONE
        RunEvent.objects.create(run=run, kind=RunEvent.Kind.ERROR,
                                data={"message": message[:500], "before_worm": before_worm, "ours": ours,
                                      "deploy_failed": deploy_failed, "conductor_stopped": not counts})  # fmt: skip
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
                    gameplan_sha256=run.gameplan_sha256, gameplan_version_id=run.gameplan_version_id, duration=run.duration,
                )  # fmt: skip
        if built:
            # The next attempt waits for this teardown (_teardown_pending); it may keep the
            # sandbox for that retry, which rolls back to the clean snapshot.
            self.jobs.request(run, HostJob.Kind.TEARDOWN, keep=self._sandbox_needed_after(run), run_uuid=str(run.uuid))
        if retry is not None:
            log.info("run %s: the team's worm runs again (attempt %s of %s)", run.uuid, retry.attempt + 1, self.cfg["max_retries"] + 1)
        else:
            log.error("run %s: no attempt left for team %s in round %s", run.uuid, run.team.name, run.round.name)
        if ours or deploy_failed:
            self._breaker(run, before_worm)

    def _breaker(self, run, before_worm):
        """A fault on our side that trying again does not mend puts the round on hold, rather than
        every team's attempts used up on it; `worms round resume` once it is mended. An attempt
        that failed on our side or in its deploy (fail) puts it on hold
        - right after another attempt of the round that failed before its worm was deployed, if
          it did too: the host, the sandbox the gameplan builds (the upload had no part yet);
        - if the last attempt of another team to end failed on our side or in its deploy too: a
          fault from the deploy on (a step of the deploy, power control, the watch, the host's
          memory), two teams' attempts in a row, which no team can bring about alone (a team's
          own retries never count twice; a deploy fails by the upload only if it does not unpack,
          which two teams' in a row are unlikely to, and a hold costs no attempt).
        An attempt that failed because the conductor stopped is passed over: it neither counts
        nor parts two that do."""
        ended = RoundRun.objects.filter(round_id=run.round_id, state__in=[S.FAILED, S.FINISHED]).exclude(pk=run.pk)

        def last(runs):
            """The last of these attempts to end, passing over the conductor's stops, and its error
            event's data ({} if it finished)."""
            for other in runs.order_by("-finished_at", "-id"):
                if other.state != S.FAILED:
                    return other, {}
                data = other.events.filter(kind=RunEvent.Kind.ERROR).order_by("-id").values_list("data", flat=True).first() or {}
                if not data.get("conductor_stopped"):
                    return other, data
            return None, {}

        previous, error = last(ended)
        if before_worm and error.get("before_worm"):
            why = "two attempts in a row failed before their worm was deployed"
        else:
            previous, error = last(ended.exclude(team_id=run.team_id))
            # An event from before `ours` and `deploy_failed`: before_worm was all.
            if not (error.get("ours", error.get("before_worm")) or error.get("deploy_failed")):
                return
            why = "two teams' attempts in a row failed on our side or in their deploy"
        Round.objects.filter(pk=run.round_id).update(on_hold=True)
        log.error("round %s is put on hold: %s (%s, attempt %s: %s; %s, attempt %s: %s), a fault on our side "
                  "(`worms round resume %s` once it is mended)", run.round.name, why, previous.team.name, previous.attempt,
                  previous.error[:300], run.team.name, run.attempt, run.error[:300], run.round_id)  # fmt: skip
