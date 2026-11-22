"""What the host itself knows about its VMs, for the round's jobs (handlers: prepare, check,
preflight, observe). After the worm every sandbox VM is assumed rooted, so nothing here asks a
VM anything: QEMU's processes in /proc, the host's memory, and the host's own records (its
kernel's and systemd's log, qmeventd's, Proxmox's task list). A guest can write to none of them.

The OOM priority: every QEMU starts with oom_score_adj 0, so when the host runs out of memory
the kernel kills the biggest VM, the database or cve-search. Each running sandbox and devbox
VM's QEMU gets 1000 instead: the kernel then kills one of those (that attempt fails and runs
again). QEMU is started afresh with every power-on, so it is set again after every start
(prepare, check, and each observe the conductor asks for after a start).

Why a VM is not running, from what the records say about its QEMU's last run: from systemd's
"Started <vmid>.scope" to its "<vmid>.scope: Deactivated" (qmeventd's "Starting cleanup for
<vmid>" if systemd's lines are missing; it trails the exit by 2 to 22 s, and after a rollback
comes after the new start):

    oom     the kernel's OOM killer killed its QEMU
    crash   its QEMU crashed: a segfault or trap the kernel logged, of one of its threads, or a
            core dump systemd-coredump logged
    host    a Proxmox task stopped, shut down, suspended or rolled it back while it ran (type, user)
    guest   none of these: the guest powered itself off (the worm's doing, if it ran)
    unknown the records could not be read, or say nothing of it stopping

A QEMU whose guest halted for good (QEMU's run state shutdown, internal-error, guest-panicked)
counts as stopped: it runs no code and only a reset leaves that state. internal-error is KVM
failing on the guest's code, which a rooted guest can bring about: the guest's doing, as a
power-off is.

What the records cannot tell: a QEMU that aborts (an assertion: SIGABRT) or is killed with
SIGKILL by hand leaves no line of the kernel's, and QEMU's own stderr reaches no log.
systemd-coredump logs the first if the host has it (worms-dev has not, 2026-11-22: core_pattern
"core"); nothing logs the second. Without it, both read as guest.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PROC = "/proc"
SYS = "/sys"
QEMU_PIDS = "/var/run/qemu-server"  # <vmid>.pid, root only
TASKS = "/var/log/pve/tasks"  # Proxmox's task list: active, index, index.1
OOM_SCORE_ADJ = 1000
RECORD = "qemu-processes.json"  # in the agent's state directory: each VM's QEMU and its threads
_RECORD_LOCK = threading.Lock()  # the agent's two workers (agent.py) both write it
JOURNAL_LINES = 50_000  # at most, of the journal since `since` (about 2000 lines a day: systemd's are the VMs' own)
# A QEMU's threads: the process (kvm), its vCPUs, and those named after its pid (vhost-<pid>, ...).
QEMU_THREAD = re.compile(r"^(kvm|CPU \d+/KVM|call_rcu|vhost-\d+|iou-wrk-\d+)$")
STOPPING = {"qmstop", "qmshutdown", "qmreboot", "qmrollback", "qmdestroy", "qmsuspend", "vzdump", "qmigrate"}
PAUSING = {"qmpause", "qmsuspend", "vzdump"}
# QEMU's run states of a guest halted for good (QEMU still up): a reset is the only way out.
HALTED = {"shutdown", "internal-error", "guest-panicked"}
SLACK = 60  # seconds before qmeventd's line of a QEMU's exit in which its OOM kill or crash is looked for
TASK_LAG = 2  # seconds a QEMU's exit may be logged after the task that ended it has ended
CLEANUP_LAG = 25  # qmeventd's cleanup line comes this long after the exit at most (2 to 22 s seen)

UPID = re.compile(r"^UPID:(?P<node>[^:]+):[0-9A-Fa-f]+:[0-9A-Fa-f]+:(?P<start>[0-9A-Fa-f]+):(?P<type>[^:]+):(?P<id>[^:]*):"
                  r"(?P<user>[^:]+):\s*(?P<rest>.*)$")  # fmt: skip
JOURNAL = re.compile(r"^(?P<at>\d+(?:\.\d+)?) \S+ (?P<ident>[^\s:\[]+)(?:\[\d+\])?: (?P<message>.*)$")
CLEANUP = re.compile(r"^Starting cleanup for (\d+)$")
SCOPE_START = re.compile(r"^Started (\d+)\.scope\b")
SCOPE_END = re.compile(r"^(\d+)\.scope: (?:Deactivated successfully|Failed with result )")
# Proxmox logs each task as it starts and ends (pvedaemon: the API's; qm: the command line's),
# so a task the index has rotated away is still known.
TASK_LINE = re.compile(r"^<[^>]+> (?P<what>starting|end) task (?P<upid>UPID:\S+:)(?: (?P<status>.*))?$")
TASK_LOGGERS = ("pvedaemon", "qm")
QEMU_STARTED = re.compile(r"^VM (\d+) started with PID (\d+)\b")  # qm, pvedaemon: each QEMU's pid as it starts
COREDUMP_LOGGER = "systemd-coredump"
COREDUMP = re.compile(r"^Process (?P<pid>\d+) \((?P<comm>.{1,32}?)\) of user \d+ (?:dumped core|terminated abnormally)")
OOM_SCOPE = re.compile(r"\boom-kill:.*\btask_memcg=/qemu\.slice/(\d+)\.scope\b")
OOM_PID = re.compile(r"^(?:Out of memory|Memory cgroup out of memory): Killed process (\d+) ")
SCOPE_OOM = re.compile(r"^(\d+)\.scope: (?:A process of this unit has been killed by the OOM killer|Failed with result 'oom-kill')")
# A QEMU that died without exiting (a signal: SIGKILL, SIGSEGV, SIGABRT; not the OOM killer's, which
# the kernel names): the kernel takes its taps down while it unregisters them. A QEMU that exits,
# after the guest's own power-off as after a shutdown of the host's, closes them first: "tap<id>i0:
# left allmulticast mode" (worms-dev, 2026-11-22: kill -KILL, an ACPI power-off, qm shutdown).
TORN = re.compile(r"^tap(\d+)i\d+ \(unregistering\): left allmulticast mode")
CRASH = re.compile(r"^(?:traps: )?(?P<comm>.{1,32}?)\[(?P<tid>\d+)\]:? (?:segfault at|general protection|trap )")


def _when(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# -- QEMU processes and their OOM priority ----------------------------------------------------


def qemu_pid(vmid):
    """The pid of a running VM's QEMU, from its pid file; None unless that process's command
    line names this VM (-id <vmid>): a stale pid file may name another process by now."""
    try:
        pid = int(Path(QEMU_PIDS, f"{int(vmid)}.pid").read_text().strip())
        argv = Path(PROC, str(pid), "cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    wanted = str(int(vmid)).encode()
    return pid if any(argv[i] == b"-id" and argv[i + 1] == wanted for i in range(len(argv) - 1)) else None


def _threads(pid):
    try:
        return sorted(int(entry.name) for entry in Path(PROC, str(pid), "task").iterdir() if entry.name.isdigit())
    except OSError:
        return []


def qemu_rss_mb(vmid):
    """What a running VM's QEMU holds of the host's memory now (VmRSS, MB); None without one."""
    pid = qemu_pid(vmid)
    return None if pid is None else _number(Path(PROC, str(pid), "status"), "VmRSS:", unit=1024)


def read_record(state_dir):
    try:
        record = json.loads((Path(state_dir) / RECORD).read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def lower_oom_priority(vmids, state_dir, note=lambda text: None, up=None):
    """oom_score_adj 1000 for the QEMU of each of these VMs that has one: its pid file decides,
    not Proxmox's status, which can be seconds old. Returns the vmids whose QEMU has it now. Each
    one's pid and threads are kept (RECORD), so a later crash of one of its threads can be told
    apart from another VM's. Best effort: a VM whose QEMU cannot be found or set is a line in
    the job log (if it is one of `up`, those Proxmox says run; all if None), never a failed job."""
    with _RECORD_LOCK:
        return _lower_oom_priority(vmids, state_dir, note, None if up is None else {int(vmid) for vmid in up})


def _lower_oom_priority(vmids, state_dir, note, up):
    record, adjusted = read_record(state_dir), []
    wanted = {str(int(vmid)) for vmid in vmids}
    for vmid in sorted(wanted, key=int):
        pid = qemu_pid(vmid)
        if pid is None:
            if up is None or int(vmid) in up:
                note(f"OOM priority: no QEMU of VM {vmid} found ({QEMU_PIDS}/{vmid}.pid)")
            continue
        path = Path(PROC, str(pid), "oom_score_adj")
        try:
            if path.read_text().strip() != str(OOM_SCORE_ADJ):
                path.write_text(f"{OOM_SCORE_ADJ}\n")
        except OSError as error:
            note(f"OOM priority of VM {vmid} (pid {pid}) not set: {error}")
            continue
        adjusted.append(int(vmid))
        seen = record.get(vmid) if isinstance(record.get(vmid), dict) and record[vmid].get("pid") == pid else {"tids": []}
        tids = sorted(set(seen.get("tids", [])) | set(_threads(pid)))[-1024:]
        record[vmid] = {"pid": pid, "tids": tids, "at": time.time()}
    # A day's worth: enough to tell whose thread crashed during any run.
    record = {vmid: entry for vmid, entry in record.items() if isinstance(entry, dict) and entry.get("at", 0) > time.time() - 86400}
    path = Path(state_dir) / RECORD
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump(record, fh)
        os.replace(tmp, path)
    except OSError as error:
        note(f"the QEMU record ({path}) not written: {error}")
    return adjusted


# -- memory ------------------------------------------------------------------------------------


def _number(path, field=None, unit=None):
    """The number in a file (its first word), or on the line of `field`: its last word, or with
    `unit` (/proc/<pid>/status: "VmRSS: 520948 kB") the one after the field, divided by unit."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    if field is None:
        return int(text.split()[0]) if text.split() and text.split()[0].isdigit() else None
    for line in text.splitlines():
        parts = line.split()
        if unit is not None and len(parts) > 1 and parts[0] == field and parts[1].isdigit():
            return int(parts[1]) // unit
        if unit is None and parts and parts[0] == field and parts[-1].isdigit():
            return int(parts[-1])
    return None


def memory():
    """The host's memory now (MB, None where the kernel does not say). MemAvailable leaves out
    the ZFS ARC (arc_mb), which gives memory back under pressure down to its floor
    (arc_reclaimable_mb). PSI: the share of the last 10 s
    in which some (or every) task waited for memory."""
    info = {}
    try:
        for line in Path(PROC, "meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if value.split() and value.split()[0].isdigit():
                info[key] = int(value.split()[0]) // 1024  # kB
    except OSError:
        pass
    psi = {}
    try:
        for line in Path(PROC, "pressure", "memory").read_text().splitlines():
            kind, *fields = line.split()
            values = dict(field.split("=", 1) for field in fields if "=" in field)
            psi[kind] = float(values["avg10"]) if "avg10" in values else None
    except (OSError, ValueError):
        pass
    sharing = _number(Path(SYS, "kernel", "mm", "ksm", "pages_sharing"))
    arc = _number(Path(PROC, "spl", "kstat", "zfs", "arcstats"), "size")
    arc_min = _number(Path(PROC, "spl", "kstat", "zfs", "arcstats"), "c_min")
    return {
        "available_mb": info.get("MemAvailable"),
        "total_mb": info.get("MemTotal"),
        "psi_some_avg10": psi.get("some"),
        "psi_full_avg10": psi.get("full"),
        "ksm_sharing_mb": None if sharing is None else sharing * os.sysconf("SC_PAGE_SIZE") // 2**20,
        "arc_mb": None if arc is None else arc // 2**20,
        # What the ARC gives back under pressure: down to its floor (c_min), not below.
        "arc_reclaimable_mb": None if arc is None else max(0, arc - (arc_min or 0)) // 2**20,
    }


# -- VM states and why one is not running -------------------------------------------------------


def state(entry):
    """running, paused or stopped, as Proxmox reports a VM (/nodes/<node>/qemu, full): for a
    running QEMU its own run state decides. A guest halted for good (HALTED) is stopped:
    `shutdown`, the guest powered off and QEMU is about to exit (-no-shutdown keeps it until
    qmeventd ends it); internal-error and guest-panicked, QEMU stays until it is stopped (the
    orchestrator's power.py reads them alike). S3 sleep (suspended) still runs."""
    if entry.get("status") != "running":
        return "stopped"
    qmp = entry.get("qmpstatus")
    if qmp in (None, "running", "suspended"):
        return "running"
    return "stopped" if qmp in HALTED else "paused"


def _task(upid, end=None):
    """{type, vmid, user, start, end} of a VM's task by its UPID; None for any other task."""
    match = UPID.match(upid)
    if not match or not match["id"].isdigit():
        return None
    return {"type": match["type"], "vmid": int(match["id"]), "user": match["user"], "start": int(match["start"], 16), "end": end,
            "upid": upid.split()[0]}  # fmt: skip


def tasks():
    """Proxmox's tasks of VMs from its task list, oldest first: [{type, vmid, user, start, end
    (None: still running), upid}]. The list keeps the last thousand or so; the journal has the
    older ones (journal_events)."""
    out = []
    for name in ("index.1", "index", "active"):
        path = Path(TASKS, name)
        if not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            match = UPID.match(line)
            if not match:
                continue
            rest = match["rest"].split()
            if name == "active":  # "<saved 0|1> <end> <status>" once it has ended
                end = rest[1] if len(rest) > 1 else None
            else:  # "<end> <status>"
                end = rest[0] if rest else None
            try:
                end = int(end, 16) if end else None
            except ValueError:
                end = None  # unreadable: as if it were still running (the error side)
            task = _task(line, end)
            if task is not None:
                out.append(task)
    return sorted(out, key=lambda task: task["start"])


def journal_events(text):
    """From journalctl's short-unix lines (kernel, systemd, qmeventd, Proxmox's task loggers,
    systemd-coredump): [(kind, key, time)]. kind: start, stop, cleanup (key: vmid; a QEMU's
    scope began or ended, qmeventd saw it end), torn (vmid: its QEMU died without exiting),
    qemu ((vmid, pid): a QEMU started), oom (vmid),
    oom-pid (pid), crash ((comm, tid)), core ((comm, pid)), task (its dict)."""
    events = []
    for line in text.splitlines():
        match = JOURNAL.match(line)
        if not match:
            continue
        at, ident, message = float(match["at"]), match["ident"], match["message"]
        if ident == "qmeventd":
            found = CLEANUP.match(message)
            if found:
                events.append(("cleanup", int(found.group(1)), at))
        elif ident in TASK_LOGGERS:
            found = TASK_LINE.match(message)
            task = _task(found["upid"], at if found["what"] == "end" else None) if found else None
            if task is not None:
                events.append(("task", task, at))
            elif QEMU_STARTED.match(message):
                found = QEMU_STARTED.match(message)
                events.append(("qemu", (int(found.group(1)), int(found.group(2))), at))
        elif ident == COREDUMP_LOGGER:
            found = COREDUMP.match(message)
            if found:
                events.append(("core", (found["comm"], int(found["pid"])), at))
        elif ident == "kernel":
            found = OOM_SCOPE.search(message)
            if found:
                events.append(("oom", int(found.group(1)), at))
            elif OOM_PID.match(message):
                events.append(("oom-pid", int(OOM_PID.match(message).group(1)), at))
            elif CRASH.match(message):
                found = CRASH.match(message)
                events.append(("crash", (found["comm"], int(found["tid"])), at))
            elif TORN.match(message):
                events.append(("torn", int(TORN.match(message).group(1)), at))
        elif ident == "systemd":
            for kind, pattern in (("oom", SCOPE_OOM), ("start", SCOPE_START), ("stop", SCOPE_END)):
                found = pattern.match(message)
                if found:
                    events.append((kind, int(found.group(1)), at))
                    break
    return events


class Records:
    """The host's records since `since` (epoch seconds), read once: `journal` (journalctl's
    output) and `task_list` (tasks()), or `error` when they could not be read."""

    def __init__(self, since, journal="", task_list=(), qemu=None, error=None, now=None):
        self.since, self.error = since, error
        self.now = time.time() if now is None else now
        self.events = journal_events(journal)
        # The task list and the journal's task lines, one entry per task (its UPID); an end
        # either of them knows.
        merged = {task["upid"]: dict(task) for task in task_list}
        for kind, task, _ in self.events:
            if kind == "task":
                known = merged.setdefault(task["upid"], dict(task))
                known["end"] = known["end"] if known["end"] is not None else task["end"]
        self.tasks = sorted((t for t in merged.values() if t["end"] is None or t["end"] >= since or t["start"] >= since),
                            key=lambda task: task["start"])  # fmt: skip
        # read_record(): {vmid: {pid, tids}}
        self.qemu = {vmid: entry for vmid, entry in (qemu or {}).items() if isinstance(entry, dict)}
        # Each QEMU's pid: {pid: vmid}, from the record and from Proxmox's line as each started.
        self.pids = {entry["pid"]: int(vmid) for vmid, entry in self.qemu.items() if entry.get("pid")}
        self.pids.update({key[1]: key[0] for kind, key, _ in self.events if kind == "qemu"})

    def _owner(self, comm, tid):
        """The VM whose QEMU a crashed thread was, by the threads recorded and the pids known;
        None if unknown."""
        for vmid, entry in self.qemu.items():
            pid = entry.get("pid")
            if tid == pid or tid in entry.get("tids", []) or comm.endswith(f"-{pid}"):
                return int(vmid)
        named = re.search(r"-(\d+)$", comm)  # vhost-<pid>, iou-wrk-<pid>
        return self.pids.get(tid) or (self.pids.get(int(named.group(1))) if named else None)

    def cause(self, vmid, status, qmpstatus=None):
        """(cause, detail) for a VM that is not running (status stopped or paused)."""
        if self.error:
            return "unknown", f"the host's records could not be read: {self.error}"[:300]
        mine = [t for t in self.tasks if t["vmid"] == vmid]
        if status == "paused":
            last = next((t for t in reversed(mine) if t["type"] in PAUSING | {"qmresume", "qmstart"}), None)
            if last is not None and last["type"] in PAUSING:
                return "host", f"{last['type']} by {last['user']} at {_when(last['start'])}"
            if qmpstatus == "io-error":
                return "host", "QEMU paused it on a disk I/O error (the sandbox storage)"
            return "unknown", f"paused ({qmpstatus}) and no host task paused it"
        if qmpstatus in ("internal-error", "guest-panicked"):
            what = "KVM could not run its code" if qmpstatus == "internal-error" else "its kernel panicked"
            return "guest", f"{what} (QEMU {qmpstatus}): the guest's doing. Its QEMU holds it until it is stopped"

        # Its QEMU's last run: what happened between its start and its exit counts. systemd's scope
        # lines date both to the moment. qmeventd's trails the exit by 2 to 22 s (worms-dev,
        # 2026-11-22), and after a rollback comes after the new start: only without systemd's.
        def times(kind):
            return [at for k, key, at in self.events if k == kind and key == vmid]

        last_start, lag = max(times("start"), default=None), 0  # lag: how long before `stop` it may have stopped
        if qmpstatus == "shutdown":
            stop, lag = self.now, CLEANUP_LAG  # powered off moments ago, QEMU on its way out
        else:
            stop = max(times("stop"), default=None)
            if stop is None or (last_start is not None and stop < last_start):
                stop, lag = max((at for at in times("cleanup") if last_start is None or at > last_start), default=None), CLEANUP_LAG
        until = stop if stop is not None else self.now
        started = max([at for at in times("start") if at <= until]
                      or [t["start"] for t in mine if t["type"] == "qmstart" and t["start"] <= until] or [self.since])  # fmt: skip
        low, high = (max(stop - SLACK, started), stop + 5) if stop is not None else (started, self.now)
        pids = {pid for pid, owner in self.pids.items() if owner == vmid}
        for kind, key, at in self.events:
            if low <= at <= high and ((kind == "oom" and key == vmid) or (kind == "oom-pid" and key in pids)):
                return "oom", f"the host's OOM killer killed its QEMU at {_when(at)}"
        unattributed = None
        for kind, key, at in self.events:
            if kind in ("crash", "core") and low <= at <= high:
                owner = self._owner(*key) if kind == "crash" else self.pids.get(key[1])
                if owner == vmid:
                    return "crash", f"its QEMU {'crashed' if kind == 'crash' else 'dumped core'} at {_when(at)} ({key[0]})"
                if owner is None and QEMU_THREAD.match(key[0]):
                    unattributed = at
        if unattributed is not None:
            return "crash", f"a QEMU crashed at {_when(unattributed)}, and the host's records do not say whose"
        # A task of the host's counts only if the VM stopped while it ran. One that ended before (the
        # rollback or start that began this run, a stop of an earlier run) is no cause: a guest that
        # powers off later must not read as the host's doing.
        for task in reversed(mine):
            if task["type"] not in STOPPING:
                continue
            if stop is None:
                ran = task["end"] is None  # under way: the VM stops for it
            else:
                ran = task["start"] - 1 <= stop and stop - lag <= (task["end"] if task["end"] is not None else self.now) + TASK_LAG
            if ran:
                return "host", f"{task['type']} by {task['user']} at {_when(task['start'])}"
        for kind, key, at in self.events:
            if kind == "torn" and key == vmid and low <= at <= high:
                return "crash", (f"its QEMU ended without a clean exit at {_when(at)}: killed by a signal or crashed (the kernel "
                                 "took its network devices down; a guest's own power-off lets QEMU close them)")  # fmt: skip
        if stop is None:
            return "unknown", f"the host has no record of it stopping since {_when(self.since)}"
        return "guest", f"powered off from inside at {_when(stop)}: no host task, OOM kill or crash then"
