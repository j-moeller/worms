"""Running a gameplan's execute(): the rules every run follows, whoever carries it out.

The Runner holds them; a Backend does the work. The conductor's backend
(orchestrator/rounds/attempt.py) powers VMs through the Proxmox API and asks the round agent
for everything inside a VM; the dry run's (DryRun, below) only keeps a clock and a record,
so a gameplan can be checked before any round uses it (`validate`).

The rules:
  - the run's clock starts at deploy_worm(); it ends round_duration seconds later, whether
    execute() has returned or not (RunOver ends execute() where it is);
  - before the worm is deployed every step must work: a failure is a RoundError and the
    attempt is thrown away;
  - after it, what happens inside a VM may fail because of the worm (it is root there): a flag
    or a file that cannot be written is logged and skipped. Power and the Proxmox API are not
    the worm's to break: their failures stay RoundErrors.
"""

from __future__ import annotations

import json

from gameplan.definition import BasicVM, Flag, Gameplan, GameplanError, RoundError

POWER_ACTIONS = ("start", "stop", "shutdown", "suspend", "resume")


class RunOver(BaseException):
    """The run's time is up: ends execute() wherever it is. A BaseException, so that a
    gameplan's `except Exception` does not keep it running."""


class Backend:
    """What a run needs done. Times are seconds on the backend's own clock."""

    def now(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError

    def power(self, vm: str, action: str, timeout: int | None = None) -> None:
        """Do it and see it done (the VM's state afterwards), or raise RoundError."""
        raise NotImplementedError

    def status(self, vm: str) -> str:
        """running, paused or stopped."""
        raise NotImplementedError

    def deploy_worm(self, vm: str) -> float:
        """Put the team's worm on `vm` and arm it; returns when it starts (this clock)."""
        raise NotImplementedError

    def remove_flags(self, vms: list[str], strict: bool) -> dict:
        """{vm: None if removed, else why not}. strict: a failure raises RoundError instead."""
        raise NotImplementedError

    def place_flags(self, vms: list[str], strict: bool) -> dict:
        raise NotImplementedError

    def write_file(self, file_id: int, strict: bool) -> str | None:
        raise NotImplementedError

    def event(self, kind: str, vm: str | None = None, **data) -> None:
        """Something the run's timeline shows (vm_stopped, flag_removed, ...)."""

    def log(self, message: str) -> None:
        """A line for the operator's journal."""


def instantiate(cls):
    """A gameplan, described: its __init__ has run and can describe nothing more."""
    if not isinstance(cls, type) or not issubclass(cls, Gameplan) or cls is Gameplan:
        raise GameplanError("not a Gameplan subclass")
    gameplan = cls()
    gameplan._plan.phase = "sealed"
    return gameplan


class Runner:
    """One run of `gameplan.execute()` against a backend."""

    def __init__(self, gameplan, backend: Backend, duration: int | None = None):
        self.gameplan, self.backend = gameplan, backend
        self.plan = gameplan._plan
        self.duration = int(duration if duration is not None else gameplan.round_duration)
        self.started = None  # the worm's start, backend clock
        self.flags = {vm.name: True for vm in self.plan.vms}  # every VM starts with its flag
        self.files = {handle.id: handle for handle in self.plan.files}
        self.notes: list[str] = []
        self.cut = False  # the run's time was up before execute() returned
        # The first RoundError: the attempt fails even if execute() caught it and went on.
        self.failure = None

    # -- the run -----------------------------------------------------------------

    def run(self):
        """execute(), then the rest of the run's time. Returns the notes (what was skipped)."""
        self.plan.phase, self.plan.runner = "run", self
        self.gameplan.round_duration = self.duration
        try:
            self.gameplan.execute()
        except RunOver:
            self.cut = True
            self.note("the run's time was up before execute() returned")
        finally:
            self.plan.runner = None
            self.plan.phase = "sealed"
        if self.failure is not None:
            raise self.failure
        if self.started is None:
            raise GameplanError("execute() returned without self.deploy_worm()")
        left = self.started + self.duration - self.backend.now()
        if left > 0:
            self.backend.sleep(left)
        return self.notes

    def note(self, message):
        self.notes.append(message)
        self.backend.log(message)

    def _guard(self):
        """Every step: none after a RoundError (execute() may have caught it), none once the
        run is over."""
        if self.failure is not None:
            raise self.failure
        if self.started is not None and self.backend.now() >= self.started + self.duration:
            raise RunOver()

    def _do(self, action, *args, **kwargs):
        """A backend step; a RoundError is kept, so that the attempt fails whatever execute()
        does with it."""
        try:
            return action(*args, **kwargs)
        except RoundError as error:
            self.failure = self.failure or error
            raise

    @property
    def strict(self):
        return self.started is None

    def elapsed(self):
        return 0.0 if self.started is None else max(0.0, self.backend.now() - self.started)

    def remaining(self):
        return float(self.duration) if self.started is None else max(0.0, self.started + self.duration - self.backend.now())

    def log(self, message):
        self.backend.log(message)

    # -- steps -------------------------------------------------------------------------

    def deploy_worm(self):
        if self.started is not None:
            raise GameplanError("deploy_worm(): the worm is deployed once")
        self._guard()
        self.started = self._do(self.backend.deploy_worm, self.plan.entrypoint.name)

    def wait(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise GameplanError("wait(): a number of seconds, at least 0")
        self._guard()
        if self.started is not None:
            left = self.started + self.duration - self.backend.now()
            if seconds >= left:
                self.backend.sleep(max(0.0, left))
                raise RunOver()
        self.backend.sleep(seconds)

    def power(self, vm, action, timeout=None):
        if action not in POWER_ACTIONS:
            raise GameplanError(f"unknown power action {action!r}")
        if action == "shutdown" and (isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600):
            raise GameplanError("shutdown(timeout): whole seconds in [1, 600]")
        self._guard()
        self._do(self.backend.power, vm.name, action, timeout=timeout)
        self.backend.event({"start": "vm_started", "stop": "vm_stopped", "shutdown": "vm_stopped",
                            "suspend": "vm_suspended", "resume": "vm_resumed"}[action], vm.name, how=action)  # fmt: skip

    def status(self, vm):
        self._guard()
        return self._do(self.backend.status, vm.name)

    def remove_flag(self, vm):
        if not self.flags[vm.name]:
            raise GameplanError(f"remove_flag({vm.name}): its flag is removed already")
        self._guard()
        problem = self._do(self.backend.remove_flags, [vm.name], strict=self.strict).get(vm.name)
        self.flags[vm.name] = False
        if problem:
            self.note(f"could not remove the flag of {vm.name}: {problem}")
        else:
            self.backend.event("flag_removed", vm.name)
        return Flag(vm)

    def add_flag(self, vm):
        if self.flags[vm.name]:
            raise GameplanError(f"add_flag({vm.name}): it has a flag (remove_flag first)")
        self._guard()
        problem = self._do(self.backend.place_flags, [vm.name], strict=self.strict).get(vm.name)
        self.flags[vm.name] = True
        if problem:
            self.note(f"could not place a flag on {vm.name}: {problem}")

    def write_file(self, handle):
        if self.files.get(handle.id) is not handle:
            raise GameplanError(f"write_file: {handle!r} is not a file of this gameplan")
        self._guard()
        problem = self._do(self.backend.write_file, handle.id, strict=self.strict)
        if problem:
            self.note(f"could not write {handle.path} on {handle.vm.name}: {problem}")


class FreezeState:
    """Suspend VMs (qm suspend), change what you like, resume them:

        with FreezeState(self.vms) as freeze:
            freeze.unfreeze(self.vm0)    # this one runs again now
            self.vm0.shutdown()

    On leaving, every VM still suspended is resumed, and each must be running again, or the
    attempt fails (RoundError). VMs stopped inside the block stay stopped. VMs that were not
    running when the block began are left alone."""

    def __init__(self, vms):
        self.vms = [vms] if isinstance(vms, BasicVM) else list(vms)
        if not self.vms or not all(isinstance(vm, BasicVM) for vm in self.vms):
            raise GameplanError("FreezeState: give VMs of the gameplan")
        self.frozen: list = []

    def __enter__(self):
        self.frozen = [vm for vm in self.vms if vm.status() == "running"]
        for vm in self.frozen:
            vm.suspend()
        return self

    def unfreeze(self, vm):
        if vm not in self.frozen:
            raise GameplanError(f"unfreeze({vm!r}): it is not frozen here")
        self.frozen.remove(vm)
        vm.resume()

    def __exit__(self, exc_type, exc, tb):
        frozen, self.frozen = self.frozen, []
        failures = []
        for vm in frozen:
            try:
                if vm.status() == "paused":
                    vm.resume()
            except (RoundError, GameplanError) as error:
                failures.append(f"{vm.name}: {error}")
        if exc_type is not None:
            return False  # the block's own error wins (the VMs were resumed as far as possible)
        for vm in frozen:
            state = vm.status()
            if state not in ("running", "stopped"):
                failures.append(f"{vm.name} is {state} after the freeze")
        if failures:
            raise RoundError("FreezeState: could not resume every VM: " + "; ".join(failures))
        return False


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


class DryRun(Backend):
    """A backend that does nothing but keep a clock and the VMs' power, and refuses what the
    real one would refuse (resuming a running VM, ...). It never fails on its own."""

    def __init__(self, vms):
        self.clock = 0.0
        self.state = {name: "running" for name in vms}
        self.steps: list = []

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += max(0.0, float(seconds))

    def power(self, vm, action, timeout=None):
        current = self.state[vm]
        allowed = {"start": ("stopped",), "stop": ("running", "paused"), "shutdown": ("running",),
                   "suspend": ("running",), "resume": ("paused",)}[action]  # fmt: skip
        if current not in allowed:
            raise GameplanError(f"{vm} is {current}: it cannot {action}")
        self.state[vm] = {"start": "running", "stop": "stopped", "shutdown": "stopped",
                          "suspend": "paused", "resume": "running"}[action]  # fmt: skip
        self.steps.append((self.clock, action, vm))

    def status(self, vm):
        return self.state[vm]

    def deploy_worm(self, vm):
        self.steps.append((self.clock, "deploy_worm", vm))
        return self.clock

    def remove_flags(self, vms, strict):
        self.steps += [(self.clock, "remove_flag", vm) for vm in vms]
        return {vm: None for vm in vms}

    def place_flags(self, vms, strict):
        self.steps += [(self.clock, "add_flag", vm) for vm in vms]
        return {vm: None for vm in vms}

    def write_file(self, file_id, strict):
        self.steps.append((self.clock, "write_file", file_id))
        return None


def validate(cls, duration=None, packages=None):
    """Every problem of a gameplan class, for this duration per team (None: its own
    round_duration): [] if there is none. Builds the spec twice (it must not change: the
    orchestrator and the node each build it), then dry-runs execute(). packages: the names
    the private apt cache holds, if known (the node knows them; gameplan/build.py)."""
    try:
        first = instantiate(cls)
        spec = first.spec()
        if json.dumps(instantiate(cls).spec(), sort_keys=True) != json.dumps(spec, sort_keys=True):
            return ["__init__ described a different sandbox the second time: it must not depend on chance or the clock"]
    except GameplanError as error:
        return str(error).splitlines()
    except Exception as error:  # noqa: BLE001  (the gameplan's own code)
        return [f"__init__ raised {type(error).__name__}: {error}"]
    problems = []
    if packages is not None:
        for vm in spec["vms"]:
            missing = sorted(set(vm["install"]) - set(packages))
            if missing:
                problems.append(f"{vm['name']}: not in the private apt cache: {', '.join(missing)} "
                                "(apt_cache_seed_packages, then fill the cache)")  # fmt: skip
    if duration is not None:
        low, high = 60, 7 * 86400
        if isinstance(duration, bool) or not isinstance(duration, int) or not low <= duration <= high:
            return problems + [f"the duration per team is whole seconds in [{low}, {high}]"]
    backend = DryRun([vm["name"] for vm in spec["vms"]])
    runner = Runner(first, backend, duration)
    try:
        runner.run()
        if runner.cut:
            problems.append(f"execute() is still going when the run ends ({runner.duration} s after deploy_worm()): "
                            "everything after that point would never happen")
    except GameplanError as error:
        problems += [f"execute(): {line}" for line in str(error).splitlines()]
    except RoundError as error:
        problems.append(f"execute(): {error}")
    except Exception as error:  # noqa: BLE001  (the gameplan's own code)
        problems.append(f"execute() raised {type(error).__name__}: {error}")
    return problems
