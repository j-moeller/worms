"""worms-gui's data: one store that the collectors (collect.py) and the runner (actions.py) fill and
the UI (ui.py) reads. The UI's API, which stays as it is:

    store = Store()
    snap = store.snapshot()     a deep copy, taken under the lock: consistent, and the UI's own (but
                                the jobs' and the log's dicts, which nobody changes: read them only)
    store.version               goes up with every change of the data: no new snapshot needed while it
                                stands (but for the ages: a source's mere answer does not move it)
    sys.stderr = LogStream(store)   while curses draws: what a library prints goes to the Log tab

Times are epoch seconds (float, this machine's clock), sizes bytes, rates bytes per second, shares
0..100 (a VM's CPU as htop counts it: 100 = one core busy; the host's: 100 = every core). None is
"not known" (the source has not answered, or cannot say), never 0. Sets are Python sets.

    snap["now"]           when the snapshot was taken
    snap["host"]          the Proxmox host
        name, public_name, system ("development" | "production" | "": worms_environments by the
        host's name, worms_cli.session), cpu_pct, cpus, load [1, 5, 15], mem_total, mem_available,
        psi_some_avg10, psi_full_avg10 (memory pressure, %), ksm_sharing_bytes, arc_bytes, uptime,
        internet ("allowed" | "denied" | "unknown"), agent_paused ("" or the pause's text),
        held_devboxes [name], window_stopped [name] (power.py's records), devboxes_held (the held
        record is there: the agent takes no devbox job), units {unit: is-active} (the host units of
        worms_cli.services, and proxmox-firewall), watchdog {result, status, last_run_at (this
        machine's clock), last_run_age_s (aged at each snapshot), running}
        (worms-firewall-watchdog.service: its last run, every 15 s), cluster_firewall
        (bool | None: cluster.fw's `enable`),
        vms_with_guest_rules {vmid} (the host's nftables has rules for its tap devices),
        vm_firewall_enabled {vmid: bool} (/etc/pve/firewall/<vmid>.fw), clean_snapshots {vmid},
        lock ("" or who holds the host's lock: a `worms` changing the platform, worms_cli/lock.py)
    snap["vms"]           {vmid: vm}, every VM on the host
        vmid, name, node, pool, kind (worms_cli.vms.KINDS' label), team (its label: `alpha (t2)`,
        `(every team)` for the shared SSH gateway, "" for a platform VM), team_slug, team_handle
        (what `worms vm reset <team>` takes), role (a | b | c | gw | ""), group (platform | team |
        sandbox: the tab it is on, state.group), status (running | paused
        | stopped), qmpstatus, uptime, cpu_pct, maxcpu, mem, maxmem, net_in_bps, net_out_bps,
        disk_read_bps, disk_write_bps (None until two readings of the counters), psi_cpu, psi_io,
        psi_mem (its pressure as the host sees it, some avg10 %: a team VM's too), tags, lock
        (Proxmox's: "backup", "snapshot", ... or ""), task (the Proxmox task on it now, else its last one:
        {type, status, user, start, end, active} | None), trusted (a platform VM: in a platform pool
        (persistent, ephemeral, private_internet) and not a gateway by its name; a VM in no pool yet,
        as a sandbox VM is while it is built, is not: only trusted VMs have a watcher),
        firewall {vm: bool | None (its firewall option), rules: bool | None (the host's guest rules
        for it; None for a stopped VM), nics: [with firewall=1, all, on the mirror bridge: not in the
        two, fine while worms-mirror is active] | None, summary: "on" | "OFF" | "?"}, clean (has its
        `clean` snapshot: bool, None outside the service pools), created,
        updated (cells as `worms status`: `11-19 abc1234`, "—"), updated_at (ISO time or ""),
        held, window_stopped (bool: in power.py's records), services [the services that run on
        it], watch (a trusted VM's watcher: {load1, load5, load15, cpus, mem_used_pct, cpu_pct (its
        own work: user, nice, system, irq, softirq), steal_pct (time the host did not give it; 100 =
        one core), root_used_pct, failed_units, top [[cpu_pct, command]] (its 3 busiest processes in
        the last 3 s), units {unit: is-active} (its services', every 30 s), at, age_s, stale (no line
        for 10 s), error ("" or why its SSH ended: what it gave before stays)} | None)
    snap["jobs"]          the newest host jobs (200), newest first
        id, uuid, kind, state (queued | running | done | failed), team, run (its round run's id
        or None), requested_at, started_at, heartbeat_at, finished_at, error, reasons (why a
        queued one waits: worms_cli.jobs.queue_reasons; [] for the others)
    snap["rounds"]        the current competition's rounds, by their start
        id, name, start, end, gameplan, gameplan_status, team_duration (s), on_hold, devboxes_off,
        devbox_hold, runs {state: count}, current (now between its start and its end)
    snap["run"]           the attempt under way: {id, round, round_id, team, state, attempt,
                          worm_start_at, ends_at, state_changed_at}, or None
    snap["services"]      worms_cli.services.SERVICES, in their order
        name, where, about, restartable, why_not, units {unit: is-active | None}, state ("active"
        | "partial" | "down" | "unknown" | "n/a": a log, no unit): host units from the host, a VM's
        from its watcher ("inactive" when the VM is stopped; None when the host or the watcher is
        stale: not known, never their old state)
    snap["components"]    worms_cli.components.COMPONENTS, in build order
        name, about, kind, deps, exists (bool | None), deployed_commit, deployed_at (the host's
        record of the last apply, worms_cli/changes.py; "" when none)
    snap["teams"]         {slug: {name, label, handle}}: the team map (the database's; {} until it answers)
    snap["checks"]        the devbox checks (checks.py: `worms check <team> --json`, run in the background)
        teams {slug: the team's entry of the JSON's `checks.teams` (worms_cli/validate.py), and at (when
        it ended), took (s), command}, ssh_gateway (the newest one's, and its at), errors {slug ("*":
        every team's): {at, started, text}} (a check that gave no answer for it), running ({slugs ([] =
        every team), started} | None), queue [[slugs]]
    snap["sandbox_check"] the round's sandbox as its own `check` host job saw it (the database): the run
                          that built it (its newest `prepare`), {run, round, round_id, team, attempt,
                          prepare {id, state, finished_at}, check {id, state, started_at, finished_at,
                          ok, error, vms {vm name: [[probe, ok, detail]]}, host [[check, ok, detail]]}
                          | None}, or None
    snap["tasks"]         the GUI's background commands, newest first (Task.view)
        id, title, command, argv, targets, locks (it takes the host's lock), state (waiting | running |
        paused | done | failed | cancelled), started, ended, rc, lines [str] (its output's last lines)
    snap["log"]           the GUI's messages, newest last: [{at, level, text}]; level is info, ok,
                          warn or error
    snap["status"]        the newest message for the status line ({at, level, text}) or None
    snap["sources"]       {name: {interval, updated_at, age_s, error, stale}}: proxmox, host, db,
                          watchers; stale = no answer for 3 times its interval (or none yet): show
                          what it gave dim, never as current

Writers (threads): `store.register(source, interval)` once; `store.update(source, **fields)` sets
those fields and says the source answered (a field in MERGED is merged key by key instead: several
sources fill `host`, one watcher per VM fills `watch`); `store.error(source, text)` says it did not
(what it gave stays, and goes stale); `store.discard(field, key)` drops one key of a merged field;
`store.set(field, value)` a field no source answers for (the devbox checks: they run on demand).
The runner keeps its tasks with add_task, task_line and task_state; anyone may `say` (status line
and Log tab) or `log` (Log tab only).
"""

from __future__ import annotations

import copy
import threading
import time
from collections import deque
from dataclasses import dataclass, field

STALE_FACTOR = 3
LOG_SIZE = 500
TASK_LINES = 400
MERGED = ("host", "watch")
WATCH_STALE = 10.0  # s without a line from a watcher (it prints one every 3 s)
FINISHED_TASKS = 100  # the ended tasks kept (the newest)
SHARED = ("jobs", "log")  # in a snapshot as new lists of the same (never changed) dicts

HOST = {
    "name": "", "public_name": "", "system": "", "cpu_pct": None, "cpus": None, "load": None, "mem_total": None,
    "mem_available": None, "psi_some_avg10": None, "psi_full_avg10": None, "ksm_sharing_bytes": None, "arc_bytes": None,
    "uptime": None, "internet": "unknown", "agent_paused": "", "held_devboxes": [], "window_stopped": [], "units": {},
    "watchdog": {"result": "", "status": None, "last_run_at": None, "last_run_age_s": None, "running": False},
    "cluster_firewall": None,
    "vms_with_guest_rules": set(), "vm_firewall_enabled": {}, "clean_snapshots": set(), "lock": "",
}  # fmt: skip


def gateway(name: str) -> bool:
    """A gateway by its name: a team's NAT gateway (devbox-t2-gw), the sandbox's (sandbox-gw), the
    shared SSH gateway teams log in through. Assumed rooted, as the team VMs are."""
    from worms_cli import teams

    return name == teams.SHARED_GATEWAY or name.endswith("-gw") or "gateway" in name


PLATFORM_POOLS = ("persistent", "ephemeral", "private_internet")
CHECKS = {"teams": {}, "ssh_gateway": None, "errors": {}, "running": None, "queue": []}


def group(name: str, pool: str) -> str:
    """Which VMs tab a VM is on: "platform" (the platform's own: infrastructure, services, the private
    internet, the shared SSH gateway), "team" (a team's devbox VMs A, B, C and its NAT gateway) or
    "sandbox" (the round's sandbox VMs and sandbox-gw; a VM in no platform pool, as a sandbox VM is
    while it is built)."""
    from worms_cli import teams

    if name == teams.SHARED_GATEWAY:
        return "platform"
    if teams.of_vm(name) or pool == "devbox":
        return "team"
    return "platform" if (pool or "") in PLATFORM_POOLS and not name.startswith("sandbox") else "sandbox"


def trusted(name: str, pool: str) -> bool:
    """A platform VM: one we may run read-only commands on (a watcher). Only a VM of a platform pool
    (persistent, ephemeral, private_internet) that is no gateway by its name: a list of what is
    trusted, not of what is not. A sandbox VM is created and started before it joins its pool
    (opentofu/stacks/sandbox), and its name is the gameplan's: in no pool, a VM is not trusted."""
    from worms_cli.vms import KINDS, UNTRUSTED_POOLS

    platform = (pool or "") in KINDS and pool not in UNTRUSTED_POOLS
    return platform and not gateway(name) and not name.startswith(("devbox-", "sandbox"))


@dataclass
class Task:
    """A background command of the GUI (actions.Runner): its output's last lines, how it ended."""

    id: int
    title: str
    argv: list
    command: str
    targets: list = field(default_factory=list)
    locks: bool = False  # it takes the host's lock (manage.py's LOCKED): paused, it keeps it
    state: str = "waiting"  # waiting (for the host's lock) | running | paused (SIGSTOP) | done | failed | cancelled
    started: float | None = None
    ended: float | None = None
    rc: int | None = None
    pid: int | None = None
    lines: deque = field(default_factory=lambda: deque(maxlen=TASK_LINES))

    def view(self) -> dict:
        return {"id": self.id, "title": self.title, "command": self.command, "argv": list(self.argv), "targets": list(self.targets),
                "locks": self.locks, "state": self.state, "started": self.started, "ended": self.ended, "rc": self.rc,
                "lines": list(self.lines)}  # fmt: skip


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self._data: dict = {}
        self._sources: dict[str, dict] = {}
        self._tasks: dict[int, Task] = {}
        self._next_task = 1
        self._log: deque = deque(maxlen=LOG_SIZE)
        self._status = None
        self._version = 0
        self._composed = (-1, None)

    # -- writers -----------------------------------------------------------------------------------

    def register(self, source: str, interval: float) -> None:
        with self._lock:
            self._sources.setdefault(source, {"interval": float(interval), "updated_at": None, "error": ""})
            self._sources[source]["interval"] = float(interval)

    def update(self, source: str, **fields) -> None:
        with self._lock:
            for name, value in fields.items():
                if name in MERGED and isinstance(value, dict):
                    self._data[name] = {**self._data.get(name, {}), **value}
                else:
                    self._data[name] = value
            state = self._sources.setdefault(source, {"interval": 5.0, "updated_at": None, "error": ""})
            if state["error"]:
                self._add_log("ok", f"{source}: answers again")
            # Only what changed the composed data moves the version (the sources' times are read at each snapshot):
            # a source that only answered (the watchers' round) costs the screen no new composition.
            if fields or state["error"] or state["updated_at"] is None:
                self._version += 1
            state.update(updated_at=time.time(), error="")

    def error(self, source: str, text: str) -> None:
        """The source did not answer: said once in the log (again only when the error changes)."""
        text = " ".join(str(text).split())[:300] or "failed"
        with self._lock:
            state = self._sources.setdefault(source, {"interval": 5.0, "updated_at": None, "error": ""})
            if state["error"] != text:
                self._add_log("error", f"{source}: {text}")
                self._version += 1
            state["error"] = text

    def set(self, name: str, value) -> None:
        """A field no source answers for (the devbox checks): set as given, no source's time moved."""
        with self._lock:
            self._data[name] = value
            self._version += 1

    def discard(self, name: str, key) -> None:
        with self._lock:
            if key in self._data.get(name, {}):
                self._data[name] = {k: v for k, v in self._data[name].items() if k != key}
                self._version += 1

    def get(self, name: str, default=None):
        """A copy of one field as its writer gave it (the watchers read the Proxmox collector's VMs)."""
        with self._lock:
            return _copy(self._data.get(name, default))

    def log(self, text: str, level: str = "info") -> None:
        with self._lock:
            self._add_log(level, text)
            self._version += 1

    def say(self, text: str, level: str = "info") -> None:
        """The status line, and the Log tab."""
        with self._lock:
            self._add_log(level, text)
            self._status = {"at": time.time(), "level": level, "text": text}
            self._version += 1

    def _add_log(self, level, text):
        self._log.append({"at": time.time(), "level": level, "text": str(text)})

    # -- tasks (actions.Runner) ------------------------------------------------------------------------

    def add_task(self, title: str, argv: list, command: str, targets=(), locks: bool = False) -> Task:
        with self._lock:
            task = Task(self._next_task, title, list(argv), command, list(targets), locks)
            self._next_task += 1
            self._tasks[task.id] = task
            ended = sorted(t.id for t in self._tasks.values() if t.ended is not None)
            for old in ended[:-FINISHED_TASKS]:  # the oldest ended ones go: the GUI may run for weeks
                del self._tasks[old]
            self._version += 1
            return task

    def task_line(self, task_id: int, line: str) -> None:
        with self._lock:
            self._tasks[task_id].lines.append(line)
            self._version += 1

    def task_state(self, task_id: int, state: str, **values) -> bool:
        """Its state, and started, ended, rc or pid when given. A task that ended stays as it ended: a
        pause or a resume that crossed its end is not written (False)."""
        with self._lock:
            task = self._tasks[task_id]
            if task.ended is not None and state in ("waiting", "running", "paused"):
                return False
            task.state = state
            for name, value in values.items():
                setattr(task, name, value)
            self._version += 1
            return True

    def task(self, task_id: int) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.view() if task else None

    def busy(self) -> list[int]:
        """The tasks waiting, running or paused."""
        with self._lock:
            return [task.id for task in self._tasks.values() if task.state in ("waiting", "running", "paused")]

    # -- the reader ----------------------------------------------------------------------------------------

    @property
    def version(self) -> int:
        """Goes up with every change of the data (not when a source only answered: the snapshot reads the
        sources' times anew): a UI may take a new snapshot only when it did, and now and then for the ages."""
        return self._version

    def snapshot(self) -> dict:
        with self._lock:
            version, composed = self._composed
            if version != self._version:
                composed = compose(self._data, {name: dict(s) for name, s in self._sources.items()})
                composed["tasks"] = [task.view() for task in sorted(self._tasks.values(), key=lambda t: -t.id)]
                composed["log"] = list(self._log)
                composed["status"] = dict(self._status) if self._status else None
                self._composed = (self._version, composed)
            # A deep copy, but for the jobs and the log: their dicts are made anew by their writer and never changed
            # after (read them only), and the 200 jobs were half the cost of a copy.
            snap = {key: list(value) if key in SHARED else _copy(value) for key, value in composed.items()}
            # The sources as they are now, not as at the composition: a source's answer moves no version.
            snap["sources"] = {name: {"interval": s["interval"], "updated_at": s["updated_at"], "error": s["error"]}
                               for name, s in self._sources.items()}  # fmt: skip
        now = time.time()
        snap["now"] = now
        for state in snap["sources"].values():
            state["age_s"] = now - state["updated_at"] if state["updated_at"] else None
            state["stale"] = state["age_s"] is None or state["age_s"] > STALE_FACTOR * state["interval"]
        for vm in snap["vms"].values():
            watch = vm.get("watch")
            if watch is not None:
                watch["age_s"] = now - watch["at"] if watch.get("at") else None
                watch["stale"] = watch["age_s"] is None or watch["age_s"] > WATCH_STALE
        watchdog = snap["host"].get("watchdog") or {}
        at = watchdog.get("last_run_at")
        watchdog["last_run_age_s"] = now - at if at else None  # aged here: a host probe that hangs does not freeze it
        host = snap["sources"].get("host") or {}
        # After the ages: a unit is known only from a source that answers now.
        snap["services"] = _services(snap["host"], snap["vms"], bool(host.get("updated_at")), bool(host.get("stale")))
        return snap


def _copy(value):
    """A deep copy of the store's plain data (dicts, lists, sets, tuples of numbers and strings): what
    copy.deepcopy does, five times as fast (no memo: the data has no cycles)."""
    kind = type(value)
    if kind is dict:
        return {key: _copy(item) for key, item in value.items()}
    if kind is list:
        return [_copy(item) for item in value]
    if kind is set:
        return set(value)
    if kind is tuple:
        return tuple(_copy(item) for item in value)
    return value if kind in (str, int, float, bool, type(None)) else copy.deepcopy(value)


class LogStream:
    """A file for sys.stderr (or stdout) while curses draws: what a library prints there (Django's
    settings banner, a warning) goes to the Log tab, not across the screen."""

    def __init__(self, store: Store, level: str = "warn"):
        self.store, self.level, self.pending = store, level, ""

    def write(self, text: str) -> int:
        self.pending += text
        *lines, self.pending = self.pending.split("\n")
        for line in lines:
            if line.strip():
                self.store.log(line.rstrip(), self.level)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


# -- composing what the sources gave ------------------------------------------------------------------------


def compose(data: dict, sources: dict) -> dict:
    """The snapshot's fields from the sources' raw fields (collect.py writes them):

    pve_vms {vmid: vm}, pve_tasks {vmid: task}   the Proxmox collector
    host {...}, vm_history, vm_ctimes, vm_nics, deployed, components_built   the host collector
    jobs, rounds, run, teams {slug: {name, label, handle}}, sandbox_check   the database collector
    watch {vmid: {...}}   the watchers
    checks   the devbox checks (checks.py), by store.set
    """
    host = {**_copy(HOST), **data.get("host", {})}
    host_known = bool(sources.get("host", {}).get("updated_at"))
    vms = _vms(data, host, host_known)
    return {
        "host": host,
        "vms": vms,
        "jobs": data.get("jobs", []),
        "rounds": data.get("rounds", []),
        "run": data.get("run"),
        "teams": data.get("teams", {}),
        "checks": {**_copy(CHECKS), **(data.get("checks") or {})},
        "sandbox_check": data.get("sandbox_check"),
        "services": [],  # by snapshot(), once the watchers' and the host's ages are known
        "components": _components(data, vms, host),
        "sources": {name: {"interval": s["interval"], "updated_at": s["updated_at"], "error": s["error"]}
                    for name, s in sources.items()},  # fmt: skip
    }


def _vms(data: dict, host: dict, host_known: bool) -> dict:
    from worms_cli import history, services, teams
    from worms_cli.pipeline import SERVICE_POOLS
    from worms_cli.vms import KINDS

    known_teams = data.get("teams", {})
    ledger = data.get("vm_history", {})
    ctimes = data.get("vm_ctimes", {})
    nics = data.get("vm_nics", {})
    tasks = data.get("pve_tasks", {})
    watch = data.get("watch", {})
    held, window = set(host.get("held_devboxes") or []), set(host.get("window_stopped") or [])
    fw_known = host_known and "vm_firewall_enabled" in data.get("host", {})
    out = {}
    for vmid, raw in (data.get("pve_vms") or {}).items():
        name, pool = raw.get("name", ""), raw.get("pool") or ""
        running = raw.get("status") in ("running", "paused")
        found = teams.of_vm(name)
        team = known_teams.get(found[0]) if found else None
        if found and team is None:
            team = {"name": "", "label": found[0], "handle": found[0]}
        vm_fw = bool(host["vm_firewall_enabled"].get(vmid, False)) if fw_known else None
        rules = (vmid in host["vms_with_guest_rules"]) if fw_known and running else None
        is_trusted = trusted(name, pool)
        nic = list(nics[vmid]) if vmid in nics else None
        if nic and len(nic) > 2 and nic[2] and not is_trusted:
            nic = [nic[0], nic[1] + nic[2], 0]  # only a platform VM (the monitor) may have one there: another's counts as unfirewalled
        # A NIC on the mirror bridge (the monitor's capture NIC) has no firewall by design: fine while worms-mirror
        # keeps it receive-only.
        mirror = host["units"].get("worms-mirror") if nic and len(nic) > 2 and nic[2] else "active"
        if vm_fw is None or (running and rules is None) or mirror is None:
            summary = "?"
        else:
            summary = "on" if vm_fw and rules is not False and (not nic or nic[0] == nic[1]) and mirror == "active" else "OFF"
        known = ledger.get(vmid)
        if known and known.get("name") != name:
            known = None  # another VM had that vmid
        created = (known or {}).get("created") or ctimes.get(vmid)
        updated = (known or {}).get("updated")
        out[vmid] = {
            **raw,
            "vmid": vmid, "name": name, "pool": pool, "kind": KINDS.get(pool, pool or "—"),
            "team": "(every team)" if name == teams.SHARED_GATEWAY else (team["label"] if team else ""),
            "team_slug": found[0] if found else "", "team_handle": team["handle"] if team else "", "role": found[1] if found else "",
            "group": group(name, pool), "trusted": is_trusted,
            "firewall": {"vm": vm_fw, "rules": rules, "nics": list(nic) if nic else None, "summary": summary},
            "clean": (vmid in host["clean_snapshots"]) if host_known and pool in SERVICE_POOLS else None,
            "created": history.cell(created), "updated": history.cell(updated), "updated_at": str((updated or {}).get("time", "")),
            "held": name in held, "window_stopped": name in window,
            "services": services.on(name),
            "task": tasks.get(vmid),
            "watch": _copy(watch.get(vmid)) if is_trusted else None,
        }  # fmt: skip
    return out


def _services(host: dict, vms: dict, host_known: bool, host_stale: bool = False) -> list[dict]:
    """The services with their units' states: a host unit from the host, a VM's from its watcher; from a
    source that went quiet (stale) not known (None), never as it was."""
    from worms_cli import services

    by_name = {vm["name"]: vm for vm in vms.values()}
    out = []
    for name, service in services.SERVICES.items():
        if service.where == "host":
            known = host_known and not host_stale
            units = {unit: host["units"].get(unit) if known else None for unit in service.up_units}
        else:
            vm = by_name.get(service.where)
            watched = (vm or {}).get("watch") or {}
            seen = (watched.get("units") or {}) if not watched.get("stale") else {}
            units = {unit: seen.get(unit) for unit in service.up_units}
            if vm is not None and vm.get("status") == "stopped":
                units = {unit: "inactive" for unit in service.up_units}  # its VM is off: said by the host
        states = [state for state in units.values() if state is not None]
        if not units:
            state = "n/a"  # a log, not a service (audit)
        elif len(states) < len(units):
            state = "unknown" if not states else ("partial" if "active" in states else "down")
        else:
            state = "active" if all(s == "active" for s in states) else ("partial" if "active" in states else "down")
        out.append({"name": name, "where": service.where, "about": service.about, "restartable": service.restartable,
                    "why_not": service.why_not, "units": units, "state": state})  # fmt: skip
    return out


def _components(data: dict, vms: dict, host: dict) -> list[dict]:
    from worms_cli import components

    deployed = data.get("deployed", {})
    stacks = data.get("components_built", {})
    names = {vm["name"] for vm in vms.values()}
    have_vms = bool(data.get("pve_vms"))
    out = []
    for name, component in components.COMPONENTS.items():
        if component.kind == "vm":
            exists = (name in names) if have_vms else None
        elif component.kind == "host":
            state = host["units"].get(component.host_service)
            exists = None if state is None else state == "active"
        elif component.kind == "stack":
            exists = stacks.get(name)
        else:
            exists = True  # devbox, gameplans: always there to apply
        commit, at = deployed.get(name, ("", ""))
        out.append({"name": name, "about": component.help, "kind": component.kind, "deps": list(component.deps), "exists": exists,
                    "deployed_commit": commit, "deployed_at": at})  # fmt: skip
    return out
