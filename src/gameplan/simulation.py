"""Simulating a round: a gameplan's attempt played against a pretend clock, for staff to see how
it plays out before a round uses it (the dashboard's Simulations page, docs/05-live-environment.md).

    simulate(cls, duration)  the attempt, as a record of what happened when (plain JSON)

The attempt runs as the conductor runs it (orchestrator/rounds/conductor.py): prepare (every VM
gets its flag, the files written at reset are written), check, execute() with the Runner's rules
(gameplan/runtime.py), the rest of the run's time, every VM stopped, the drain, the teardown.
Nothing is built or contacted, and nothing waits: wait(600) moves the clock by 600 s and is
recorded. Every other step takes the nominal time in TIMING that its host job or Proxmox call
takes in a real round.

There is no worm: nothing is captured, and a file changes only when execute() writes it. A VM
does what execute() tells it; what a real one would refuse (resuming a running VM, ...) is
refused here too. The record holds the sandbox's shape and the files' places (`outline`), every
log line and every problem: it is for the orchestrator, which turns it into what the dashboard may
see (orchestrator/rounds/simulations.py).

On the orchestrator it runs in a child process (`main`, limited in CPU time, memory and output): a
gameplan is code, and a loop in it must not hold the web worker that asked.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

from gameplan.definition import GameplanError, RoundError
from gameplan.runtime import Backend, Runner, instantiate

FORMAT = 1
# Seconds a step takes in a real round, nominally: host jobs (SSH into a VM from the node) and
# Proxmox API calls. The conductor's own times (the fuse, the drain) are its settings.
TIMING = {
    "prepare": 60,  # a rollback to the round's snapshot, secrets, files, flags (the first build takes minutes)
    "check": 10,
    "deploy": 10,  # the worm's upload put on the entry VM, its fuse lit
    "worm_start_delay": 15,  # the fuse (conductor: worm_start_delay)
    "start": 5,
    "stop": 2,
    "shutdown": 10,  # at most the shutdown's timeout
    "suspend": 1,
    "resume": 1,
    "flags": 5,  # remove_flag / add_flag
    "write_file": 5,
    "read_file": 3,
    "end_drain": 120,  # conductor: end_drain
    "teardown": 30,
}
MAX_EVENTS = 5000
POWER_EVENTS = {"start": "vm_started", "stop": "vm_stopped", "shutdown": "vm_stopped",
                "suspend": "vm_suspended", "resume": "vm_resumed"}  # fmt: skip
ALLOWED = {"start": ("stopped",), "stop": ("running", "paused"), "shutdown": ("running",),
           "suspend": ("running",), "resume": ("paused",)}  # fmt: skip
AFTER = {"start": "running", "stop": "stopped", "shutdown": "stopped", "suspend": "paused", "resume": "running"}


class Simulated(Backend):
    """A backend with a pretend clock: each step moves it by its nominal time and is recorded."""

    def __init__(self, spec, timing):
        self.timing = timing
        self.clock = 0.0
        self.state = {vm["name"]: "running" for vm in spec["vms"]}
        self.events: list[dict] = []
        self.truncated = False
        self.log_lines: list[str] = []
        self.files = {f["id"]: f for f in spec["files"]}
        # What read_file names: a file written with write_file (its number), or a watched path.
        first = {}
        for f in spec["files"]:
            first.setdefault((f["vm"], f["path"]), f["id"])
        self.reads = {}
        watched = 0
        for entry in spec["reads"]:
            key = (entry["vm"], entry["path"])
            if key in first:
                self.reads[entry["id"]] = {"file": first[key]}
            else:
                self.reads[entry["id"]] = {"watched": watched}
                watched += 1
        # (VM, path) -> how often it was written: a file is there once written, and changes with each write.
        self.versions = {(f["vm"], f["path"]): 0 for f in spec["files"]}
        self.waiting = None  # the seconds execute() asked to wait, while it waits
        self.runner = None
        self.returned_at = None

    def record(self, kind, vm=None, t=None, d=0.0, force=False, **data):
        """An event; past MAX_EVENTS only the run's own (its states, its end: `force`)."""
        if len(self.events) >= MAX_EVENTS and not force:
            self.truncated = True
            return
        self.events.append({"t": round(self.clock if t is None else t, 3), "d": round(d, 3), "kind": kind, "vm": vm, "data": data})

    def advance(self, seconds):
        self.clock += max(0.0, float(seconds))

    def now(self):
        return self.clock

    def sleep(self, seconds):
        start = self.clock
        if self.waiting is not None:
            self.advance(seconds)
            self.record("wait", t=start, d=self.clock - start, seconds=self.waiting, cut=self.clock - start < self.waiting)
        else:
            # The rest of the run's time, once execute() has returned.
            if self.returned_at is None:
                self.returned_at = start
                self.record("execute_returned")
            self.advance(seconds)

    def power(self, vm, action, timeout=None):
        current = self.state[vm]
        if current not in ALLOWED[action]:
            raise GameplanError(f"{vm} is {current}: it cannot {action}")
        start = self.clock
        self.advance(min(self.timing["shutdown"], timeout or self.timing["shutdown"]) if action == "shutdown" else self.timing[action])
        self.state[vm] = AFTER[action]
        self.record(POWER_EVENTS[action], vm, t=start, d=self.clock - start, how=action)

    def status(self, vm):
        return self.state[vm]

    def deploy_worm(self, vm):
        start = self.clock
        self.advance(self.timing["deploy"])
        self.record("worm_deployed", vm, t=start, d=self.clock - start)
        self.advance(self.timing["worm_start_delay"])
        self.record("worm_started", vm)
        return self.clock

    def _flags(self, kind, vms):
        start = self.clock
        self.advance(self.timing["flags"])
        for vm in vms:
            self.record(kind, vm, t=start, d=self.clock - start)
        return {vm: None for vm in vms}

    def remove_flags(self, vms, strict):
        return self._flags("flag_removed", vms)

    def place_flags(self, vms, strict):
        # A new flag: the event a real run records when its hash comes back.
        return self._flags("secret_released", vms)

    def write_file(self, file_id, strict, at_reset=False):
        entry = self.files[file_id]
        start = self.clock
        if not at_reset:
            self.advance(self.timing["write_file"])
        self.versions[(entry["vm"], entry["path"])] += 1
        self.record("file_written", entry["vm"], t=start, d=self.clock - start, file=file_id, at_reset=at_reset)
        return None

    def read_file(self, entry, strict):
        start = self.clock
        self.advance(self.timing["read_file"])
        version = self.versions.get((entry["vm"], entry["path"]), 0)
        self.record("file_read", entry["vm"], t=start, d=self.clock - start, exists=version > 0, **self.reads[entry["id"]])
        if not version:
            return {"exists": False, "size": None, "sha256": None}
        # The same until the gameplan writes the file again (no worm changes anything here).
        return {"exists": True, "size": 0, "sha256": hashlib.sha256(f"{entry['vm']}:{entry['path']}:{version}".encode()).hexdigest()}

    def log(self, message):
        """Notes of the Runner (the gameplan's own lines come through Recorder.log)."""


class Recorder(Runner):
    """The Runner, telling the backend what execute() asked for (a wait's seconds, a log line)."""

    def wait(self, seconds):
        self.backend.waiting = seconds
        try:
            super().wait(seconds)
        finally:
            self.backend.waiting = None

    def log(self, message):
        self.backend.log_lines.append(str(message))
        self.backend.record("log", line=len(self.backend.log_lines) - 1)


def outline(spec):
    """The parts of a spec a record keeps: the sandbox's shape and where the files go. Not what is
    built or written (packages, commands, users, contents)."""
    return {
        "round_duration": spec["round_duration"],
        "entrypoint": spec["entrypoint"],
        "networks": [{"name": net["name"], "cidr": net["cidr"]} for net in spec["networks"]],
        "vms": [
            {"name": vm["name"], "nics": [{"network": nic["network"], "address": nic["address"]} for nic in vm["nics"]],
             "firewall": vm["firewall"]}
            for vm in spec["vms"]
        ],  # fmt: skip
        "files": [{"id": f["id"], "vm": f["vm"], "path": f["path"], "at_reset": f["at_reset"]} for f in spec["files"]],
        "reads": spec["reads"],
    }


def _problem(error):
    if isinstance(error, (GameplanError, RoundError)):
        return str(error).splitlines() or [type(error).__name__]
    return [f"{type(error).__name__}: {error}"]


def simulate(cls, duration=None, timing=None) -> dict:
    """The attempt of `cls` (a Gameplan subclass) for `duration` seconds per team (None: its own
    round_duration):

        {format, class, duration, outcome, problems, log, spec, events, truncated, worm_start}

    outcome: ok; cut (execute() was still going when the run ended); failed (the attempt fails:
    problems says why); invalid (the spec could not be made: __init__ failed). Each event is
    {t, d, kind, vm, data}, t and d in seconds from the start of prepare."""
    t = {**TIMING, **(timing or {})}
    out = {"format": FORMAT, "class": getattr(cls, "__name__", "?"), "duration": duration, "outcome": "invalid",
           "problems": [], "log": [], "spec": None, "events": [], "truncated": False, "worm_start": None}  # fmt: skip
    try:
        gameplan = instantiate(cls)
        spec = outline(gameplan.spec())
    except Exception as error:  # noqa: BLE001  (the gameplan's own code)
        out["problems"] = _problem(error) if isinstance(error, GameplanError) else [f"__init__ raised {type(error).__name__}: {error}"]
        return out
    out["spec"] = spec
    backend = Simulated(spec, t)
    state = lambda name: backend.record("state", force=True, state=name)  # noqa: E731

    state("preparing")
    for vm in spec["vms"]:
        backend.record("secret_released", vm["name"])  # every VM starts with its flag
    for f in spec["files"]:
        if f["at_reset"]:
            backend.write_file(f["id"], True, at_reset=True)
    backend.advance(t["prepare"])
    state("checking")
    backend.advance(t["check"])
    state("running")
    runner = Recorder(gameplan, backend, duration)
    out["duration"] = runner.duration
    try:
        low, high = 60, 7 * 86400
        if not low <= runner.duration <= high:
            raise GameplanError(f"the duration per team is whole seconds in [{low}, {high}]")
        runner.run()
        out["outcome"] = "cut" if runner.cut else "ok"
        if not runner.cut and backend.returned_at is None:
            backend.record("execute_returned", force=True)  # at the run's very end
    except Exception as error:  # noqa: BLE001  (the gameplan's own code, as the conductor catches it)
        out["outcome"], out["problems"] = "failed", _problem(error)
        backend.record("error", force=True)
        state("failed")
    else:
        state("ending")
        for vm, current in backend.state.items():
            if current != "stopped":
                backend.state[vm] = "stopped"
                backend.record("vm_stopped", vm, force=True, how="end")
        backend.advance(t["end_drain"])
        state("tearing_down")
        backend.advance(t["teardown"])
        state("finished")
    out.update(events=backend.events, truncated=backend.truncated, log=backend.log_lines,
               worm_start=None if runner.started is None else round(runner.started, 3))  # fmt: skip
    return out


def main(argv=None) -> int:
    """`<directory> <file> <sha256> <duration> <timing JSON>`: load the gameplan (exactly that
    version) and print simulate()'s record as JSON. Run by the orchestrator in a child process;
    it limits itself first: CPU time, memory, what it writes to files. Whatever the gameplan
    itself prints goes to stderr: stdout carries the record only."""
    import resource

    directory, filename, sha256, duration, timing = argv if argv is not None else sys.argv[1:]
    resource.setrlimit(resource.RLIMIT_CPU, (10, 12))
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 << 20, 16 << 20))
    record_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    from gameplan import loader

    try:
        loaded = loader.load(directory, filename, sha256 or None)
    except Exception as error:  # noqa: BLE001  (a file that does not load is a problem like any other)
        record = {"format": FORMAT, "outcome": "invalid", "problems": [f"{type(error).__name__}: {error}"], "log": [],
                  "spec": None, "events": [], "truncated": False, "worm_start": None, "duration": None}  # fmt: skip
    else:
        record = simulate(loaded.cls, int(duration) if duration else None, json.loads(timing or "{}"))
    with os.fdopen(record_fd, "w") as out:
        out.write(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
