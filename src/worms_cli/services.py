"""The platform's services, in one registry: where each runs, its systemd units, its log, and
whether `worms service restart` may restart it. `worms service`, `worms log <name>`, `worms service
restart|stop|start <name>` and `worms show <vm>` read it (docs/03-manage-py.md).

    worms service restart processor           its units, on the orchestrator
    worms service stop orchestrator           every service of the orchestrator: api, processor, topology
    worms service stop agent                  the round agent, once its job under way has ended
    worms service start agent orchestrator    ... started again

The round agent is restarted or stopped only when no host job is RUNNING (the database says so;
one silent for minutes has no agent any more): systemd stops a service within its stop timeout
and then kills it, which would end a devbox rebuild halfway. A restart waits for that
(`--timeout`), and refuses once it has waited that long; a stop first pauses the agent (it takes no
other job), then waits as long as it takes (Ctrl-C stops waiting; `worms jobs resume` undoes the
pause).
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from worms_cli import output
from worms_cli.build import BuildError


@dataclass(frozen=True)
class Service:
    where: str  # "host" or the VM it runs on
    about: str
    units: tuple[str, ...] = ()  # its systemd units: its journal, and what a restart restarts
    files: str = ""  # its log as a file glob, instead of the journal
    restartable: bool = True
    why_not: str = ""  # what to do instead, when it is not restartable
    active: tuple[str, ...] | None = None  # the units that are active when it is up (default: units)

    @property
    def up_units(self) -> tuple[str, ...]:
        """What `worms status` checks is active: a oneshot that a timer runs is up when its timer is."""
        return self.units if self.active is None else self.active

    @property
    def log(self):
        """What `worms log <name>` shows: a file glob, or the units' journal."""
        return self.files or list(self.units)


# In restart order: the agent first, the orchestrator's before the website and the sandboxes' APIs.
SERVICES: dict[str, Service] = {
    "agent": Service("host", "the round agent: each host job started, done or failed", ("worms-round-agent",)),
    "firewall": Service("host", "the host firewall, its watchdog and the traffic mirror",
                        ("worms-host-firewall", "worms-firewall-watchdog", "worms-mirror"), restartable=False,
                        why_not="the host's firewall: `worms apply host-network` applies it again",
                        active=("worms-host-firewall", "worms-firewall-watchdog.timer", "worms-mirror")),
    "processor": Service("orchestrator", "captures and worm logs counted; dev uploads run", ("orchestrator-process-attempts",)),
    "api": Service("orchestrator", "the orchestrator's API: simulations, devbox key pushes", ("orchestrator",)),
    "topology": Service("orchestrator", "the Proxmox topology for the dashboard, every 2 minutes",
                        ("orchestrator-collect-topology.timer", "orchestrator-collect-topology.service"),
                        active=("orchestrator-collect-topology.timer",)),
    "dashboard": Service("dashboard", "the website: errors and tracebacks", ("dashboard",)),
    "scoring": Service("scoring", "flag submissions (the sandbox API)", ("sandbox-api",)),
    "logging": Service("logging", "the worms' log records (the sandbox API)", ("sandbox-api",)),
    "database": Service("database", "PostgreSQL", ("postgresql",), files="/var/log/postgresql/postgresql-*-main.log",
                        restartable=False, why_not="every service holds connections to it: by hand, outside a round "
                        "(`worms run database --root -- systemctl restart postgresql`)"),
    "monitor": Service("monitor", "Zeek and its shipper (connections into the database)", ("zeek", "zeek-shipper")),
    "audit": Service("syslog", "every platform VM's audit trail (auditd), one file per sender address",
                     files="/var/log/remote/*/audit.log", restartable=False, why_not="a log, not a service"),
}  # fmt: skip


# `worms service stop|start <group>`: several services at once.
GROUPS: dict[str, tuple[str, ...]] = {"orchestrator": ("api", "processor", "topology")}
# A unit of an earlier deployment, stopped with its group if it is still there (`worms apply` removes it).
RETIRED = {"orchestrator": ("orchestrator-conduct-rounds",)}
SILENT_AFTER = 300  # s: a running host job without a heartbeat this long has no agent any more (it beats every 30 s)


def expand(names) -> list[str]:
    """Services and groups, as the services, once each, in SERVICES' order; KeyError for an unknown name."""
    wanted = set()
    for name in names:
        if name in GROUPS:
            wanted.update(GROUPS[name])
        elif name in SERVICES:
            wanted.add(name)
        else:
            raise KeyError(name)
    return [name for name in SERVICES if name in wanted]


def on(vm: str) -> list[str]:
    """The services that run on a VM (`worms show <vm>`)."""
    return [name for name, service in SERVICES.items() if service.where == vm]


def in_order(names) -> list[str]:
    """`names` once each, in restart order; KeyError for an unknown one, ValueError for one that
    is not restarted from here (and what to do instead)."""
    for name in names:
        if name not in SERVICES:
            raise KeyError(name)
        if not SERVICES[name].restartable:
            raise ValueError(f"{name} is not restarted by `worms service restart`: {SERVICES[name].why_not}")
    return [name for name in SERVICES if name in set(names)]


def this_process() -> dict:
    """Who runs a host job outside the round agent (worms_cli/rounds.py LocalJobs): kept in the job's
    result while it runs, so that one this process leaves behind is known for what it is at once."""
    return {"host": socket.gethostname(), "pid": os.getpid(), "start": _started(os.getpid()),
            "by": shlex.join(["worms", *sys.argv[1:]])[:200]}  # fmt: skip


def _started(pid: int) -> str | None:
    """A process's start (clock ticks after boot, /proc/<pid>/stat field 22): a pid used again is
    another process. None: no such process (or no /proc)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def left_behind(job, agent_stopped: bool = False) -> str:
    """Why a host job marked running runs nowhere any more, or "" while it may still run: the
    `worms round start` that ran it has ended (its process, on this host, is gone), the round agent
    is stopped and the job is not a session's, or its heartbeat stopped (SILENT_AFTER)."""
    from django.utils import timezone

    owner = job.result.get("owner") if isinstance(job.result, dict) else None
    if isinstance(owner, dict) and owner.get("host") == socket.gethostname():
        try:
            pid = int(owner.get("pid"))
        except (TypeError, ValueError):
            pid = 0
        started = _started(pid) if pid else None
        if started is None or (owner.get("start") and started != owner["start"]):
            return f"the `{owner.get('by') or 'worms'}` that ran it (pid {pid}) has ended"
        return ""  # its process runs on this host, and beats
    if owner is None and agent_stopped:
        return "the round agent is stopped, and no `worms round start` runs it"
    beat = job.heartbeat_at or job.started_at or job.requested_at
    if beat and (timezone.now() - beat).total_seconds() > SILENT_AFTER:
        return f"no heartbeat since {beat:%Y-%m-%d %H:%M:%S} UTC"
    return ""


def running_jobs() -> list:
    """The host jobs the round agent is running now (the database's word). A database that
    does not answer is a BuildError: without its word the agent is not restarted."""
    from core.models.rounds import HostJob
    from django.db import DatabaseError

    try:
        from django.db.models.functions import Now
        from datetime import timedelta

        # One whose heartbeat stopped (its agent was killed) runs nowhere: `worms jobs cancel` closes it.
        return list(HostJob.objects.filter(state=HostJob.State.RUNNING, heartbeat_at__gte=Now() - timedelta(seconds=SILENT_AFTER))
                    .order_by("id"))  # fmt: skip
    except DatabaseError as exc:
        raise BuildError(f"the database does not say whether the round agent runs a host job ({str(exc).strip()[:120]}): "
                         "not restarted. `worms status` says what is down") from None  # fmt: skip


def wait_until_idle(timeout: int, poll: float = 5.0, jobs=running_jobs, clock=time.monotonic, sleep=time.sleep) -> None:
    """Return once no host job runs; a BuildError after `timeout` seconds of waiting."""
    deadline = clock() + timeout
    said = None
    while True:
        busy = jobs()
        if not busy:
            return
        names = ", ".join(f"#{job.id} {job.kind}" for job in busy)
        if clock() >= deadline:
            raise BuildError(
                f"the round agent is still running {names} after {timeout} s: not restarted (systemd would kill the job "
                "at its stop timeout). Try again once it is done (`worms jobs`, `worms log latest -f`), or wait "
                "longer: --timeout SECONDS"
            )
        if names != said:
            print(output.color(f"waiting for the round agent to finish {names} (up to {timeout} s; Ctrl-C stops waiting)", "dim"),
                  flush=True)  # fmt: skip
            said = names
        sleep(poll)


class Restarter:
    def __init__(self, config, catalogue=None):
        from worms_cli.vms import Catalogue

        self.config = config
        self.catalogue = catalogue or Catalogue(config)
        self.runner = self.catalogue.runner

    def restart(self, name: str) -> None:
        """Restart one service's units where it runs, then check that they are active."""
        service = SERVICES[name]
        units = " ".join(service.units)
        if service.where == "host":
            print(output.color(f"restarting {name} on the host: {units}", "dim"), flush=True)
            self.runner.host_sh(f"systemctl restart {units}")
            if self.runner.host_sh(f"systemctl is-active --quiet {units}", check=False, quiet=True) != 0:
                raise BuildError(f"{name} is not active after its restart: `worms log {name}`")
            return
        vm = self.catalogue.resolve(service.where)
        if vm.get("status") != "running":
            raise BuildError(f"{vm['name']} is {vm.get('status')}: {name} runs there (`worms vm start {vm['name']}`)")
        print(output.color(f"restarting {name} on {vm['name']}: {units}", "dim"), flush=True)
        argv, env = self.catalogue.ssh_argv(vm, f"sudo -n systemctl restart {units} && sudo -n systemctl is-active --quiet {units}",
                                            interactive=False)  # fmt: skip
        proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise BuildError(f"{name} on {vm['name']}: restart failed or not active after it (exit {proc.returncode}: "
                             f"{proc.stderr.strip()[-200:]}): `worms log {name}`")  # fmt: skip


class Units:
    """`worms service stop|start <name>...` and what `worms round start` asks: a service's (or a
    group's) units stopped, started, or which of them are active, where they run."""

    def __init__(self, config, catalogue=None):
        from worms_cli.vms import Catalogue

        self.config = config
        self.catalogue = catalogue or Catalogue(config)
        self.runner = self.catalogue.runner

    def _units(self, names, retired=True, up=False) -> dict[str, list[str]]:
        """{where: [units]} of these services and groups (up: the ones that are active when it runs;
        a timer's, not its oneshot's)."""
        out: dict[str, list[str]] = {}
        for name in expand(names):
            out.setdefault(SERVICES[name].where, []).extend(SERVICES[name].up_units if up else SERVICES[name].units)
        if retired:
            for name in names:
                for unit in RETIRED.get(name, ()):
                    out.setdefault(SERVICES[GROUPS[name][0]].where, []).append(unit)
        return out

    def _sh(self, where: str, command: str, check=True) -> tuple[int, str]:
        if where == "host":
            code, out, err = self.runner.session.host_run(["sh", "-c", command])
        else:
            vm = self.catalogue.resolve(where)
            if vm.get("status") != "running":
                if check:
                    raise BuildError(f"{vm['name']} is {vm.get('status')}")
                return 0, ""
            argv, env = self.catalogue.ssh_argv(vm, f"sudo -n sh -c {_quote(command)}", interactive=False)
            proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        if check and code != 0:
            raise BuildError(f"on {where}: {command}: exit {code}: {(err or out).strip()[-300:]}")
        return code, out

    def vm_states(self) -> tuple[dict, dict]:
        """`worms status`: the units of every service that runs on a VM of its own (the
        orchestrator's, the website's, the sandbox APIs', the database's, ...), one SSH command a VM,
        the VMs at once. ({VM: {unit: state}, or why it has none: its VM not running}, {VM: why
        it could not be asked}). A VM this system does not have is left out."""
        from concurrent.futures import ThreadPoolExecutor

        wanted: dict[str, list[str]] = {}
        for service in SERVICES.values():
            if service.where != "host" and service.up_units:  # syslog's audit trail is files alone
                units = wanted.setdefault(service.where, [])
                units += [unit for unit in service.up_units if unit not in units]
        self.catalogue.vms()  # listed once, before the threads ask

        def one(where, units):
            try:
                vm = self.catalogue.resolve(where)
            except BuildError:
                return where, None, None  # not on this system
            if vm.get("status") != "running":
                return where, f"its VM is {vm.get('status')}", None
            try:
                code, out = self._sh(where, "systemctl is-active " + " ".join(units) + " || true", check=False)
            except Exception as exc:  # noqa: BLE001  (no address, no answer: not checked, said)
                return where, None, str(exc).strip().splitlines()[0][:80] if str(exc).strip() else type(exc).__name__
            states = out.split()
            if len(states) != len(units):
                return where, None, f"no answer (exit {code})"
            return where, dict(zip(units, states)), None

        states, unchecked = {}, {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for where, found, why in pool.map(lambda item: one(*item), wanted.items()):
                if found is not None:
                    states[where] = found
                elif why is not None:
                    unchecked[where] = why
        return states, unchecked

    def active(self, name: str) -> list[str]:
        """The units of a service or group that are active (a VM that is not running: none)."""
        found = []
        for where, units in self._units([name]).items():
            _code, out = self._sh(where, "systemctl is-active " + " ".join(units) + " || true", check=False)
            found += [unit for unit, state in zip(units, out.split()) if state in ("active", "activating", "reloading")]
        return found

    def stopped(self, name: str) -> bool:
        """Every unit of a service or group says it is stopped (no answer is not stopped)."""
        for where, units in self._units([name], retired=False).items():
            code, out = self._sh(where, "systemctl is-active " + " ".join(units) + " || true", check=False)
            states = out.split()
            if code != 0 or len(states) != len(units) or any(state not in ("inactive", "failed") for state in states):
                return False
        return True

    def stop(self, names) -> None:
        if "agent" in names:
            self._stop_agent()
        for where, units in self._units([n for n in names if n != "agent"]).items():
            up = [unit for unit in units if unit in self._active_on(where, units)]
            if not up:
                print(output.status_cell("stopped", f"{where}: {', '.join(units)}: stopped already"))
                continue
            self._sh(where, "systemctl stop " + " ".join(up))
            still = self._active_on(where, up)
            if still:
                raise BuildError(f"on {where}: still active after the stop: {', '.join(still)}")
            print(output.status_cell("stopped", f"{where}: {', '.join(up)}: stopped"))

    def start(self, names) -> None:
        for where, up in self._units(names, retired=False, up=True).items():
            self._sh(where, "systemctl start " + " ".join(up))
            down = [unit for unit in up if unit not in self._active_on(where, up)]
            if down:
                raise BuildError(f"on {where}: not active after the start: {', '.join(down)} (`worms log {names[0]}`)")
            print(output.status_cell("running", f"{where}: {', '.join(up)}: started"))

    def _active_on(self, where, units) -> list[str]:
        _code, out = self._sh(where, "systemctl is-active " + " ".join(units) + " || true", check=False)
        return [unit for unit, state in zip(units, out.split()) if state in ("active", "activating", "reloading")]

    def _stop_agent(self) -> None:
        """Paused first (it takes no other job), its job under way waited for, then stopped; the pause
        undone (a start of the service takes jobs again)."""
        from worms_cli.jobs import PAUSED

        unit = "worms-round-agent"
        if not self._active_on("host", [unit]):
            print(output.status_cell("stopped", f"host: {unit}: stopped already"))
            return
        self._sh("host", f"install -d -m 755 /var/lib/worms && printf '%s\\n' 'paused by worms service stop agent' > {PAUSED}")
        try:
            said = None
            while True:
                busy = running_jobs()
                if not busy:
                    break
                names = ", ".join(f"#{job.id} {job.kind}" for job in busy)
                if names != said:
                    print(output.color(f"the round agent is paused; waiting for it to finish {names} (Ctrl-C stops "
                                       "waiting: `worms jobs resume` undoes the pause)", "dim"), flush=True)  # fmt: skip
                    said = names
                time.sleep(5)
            self._sh("host", f"systemctl stop {unit}")
        finally:
            if not self._active_on("host", [unit]):
                self._sh("host", f"rm -f {PAUSED}", check=False)
        print(output.status_cell("stopped", f"host: {unit}: stopped"))


def _quote(text: str) -> str:
    import shlex

    return shlex.quote(text)
