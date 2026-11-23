"""worms-gui's collectors: daemon threads that fill the store (state.py), each at its own pace. Nothing
raises into the UI: a failure goes to `store.error(source, ...)`, and the thread tries again later,
waiting longer each time (up to MAX_BACKOFF). Everything here only reads.

    proxmox   (--interval, 2 s)  worms_cli.proxmox.Infra's API with the topology token, as `worms
              status`, but /cluster/resources itself: every VM with its pool, its use and its
              counters (their rates) in one call (Infra.vms() makes one per pool). Every
              STATUS_EVERY rounds the node's VM list in full (one call, not status/current per VM:
              QEMU's own state, which tells paused from running, and each VM's pressure) and the
              node's recent tasks; the active tasks every round
    host      (2.5 x, 5 s)   one shell script on the host (here, or through the session's SSH from
              a laptop, as the CLI's host commands go): memory, pressure, load, CPU, KSM, ARC, the
              internet flag, the agent's pause, the devbox records, the host units, the watchdog,
              the guest firewall rules, every VM's firewall option and NICs, the `clean` snapshots,
              the host's lock; every SLOW s also the VM history and the deployed record
    db        (1.5 x, 3 s)   the host jobs, the rounds, the run under way (Django, as the CLI reads
              them); the team map every TEAMS_EVERY s; with the jobs, the sandbox's newest `check`
              job (its per-VM records)
    watchers  (2 s)   one long-lived SSH per running trusted VM (state.trusted: never a team's, a
              sandbox's or a gateway): one python3 there (WATCH) reads /proc itself and prints a
              line every 3 s; it starts no process but systemctl, every 30 s (auditd logs every
              execve on the platform's VMs). Restarted with backoff when it ends. One worms-gui per
              machine runs them (SHARE's lock); another shows what that one's watchers see

    collectors = Collectors(config, store, interval=2.0, watchers=True)
    collectors.start()      the threads
    collectors.refresh()    every collector now, not at its next turn (the UI's `r`)
    collectors.stop()       on quit: the threads end, the watchers' SSH processes are killed

The CLI's Session (worms_cli/session.py) starts its ssh-agent, reads the vault and opens its tunnels
once, lazily: SETUP lets one thread at a time do that, and nothing that waits on the network is
done under it. What runs on the host has a timeout (HOST_TIMEOUT): a hung /etc/pve or a dead SSH
connection is an error, said and tried again, never a collector stuck for good.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import socket
import threading
import time
from collections import deque
from pathlib import Path

from worms_cli.gui.state import trusted

SETUP = threading.Lock()
MAX_BACKOFF = 60.0
STATUS_EVERY = 5  # Proxmox rounds
SLOW = 60.0  # s between two readings of the VM history and the deployed record
TEAMS_EVERY = 30.0
JOBS = 200
JOBS_AGAIN = 30.0  # s: the jobs are read again at least this often, else only when their signature changed
REASONS_FOR = 20  # queued jobs whose reasons are asked (each costs a few queries)
WATCH_RESTART = (5, 10, 20, 40, 80, 120)  # s before a watcher's SSH is started again, after each failure in a row
STAGGER = 0.6  # s between the starts of new watchers
CATALOGUE_FOR = 600.0  # s a Catalogue (the management addresses) is used before it is read again
CATALOGUE_AGAIN = 60.0  # s at least between two readings for a VM the addresses lack (a new one)
HOST_TIMEOUT = 30.0  # s for a command on the host (the probe, the inventory)
CHILDREN: set = set()  # the watchers' SSH processes (each in a session of its own): killed at exit, stop() or not


@atexit.register
def _kill_children() -> None:
    for proc in list(CHILDREN):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass


def host_command(config, script: str, timeout: float = HOST_TIMEOUT) -> tuple[int, str, str]:
    """`sh -c script` on the Proxmox host, as Session.host_run, with a timeout: here on the host, or
    over the session's SSH from a laptop (ServerAlive: a dead connection is noticed). Killed with its
    children (a session of its own) when it takes longer: a TimeoutError."""
    from worms_cli.session import for_config

    session = for_config(config)
    if session.laptop:
        with SETUP:  # its ssh-agent and the first login (each with its own timeout): by one thread at a time
            session.check_host()
            env = session.env()
        argv = ["ssh", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2", *session.ssh_opts(), config.ssh,
                shlex.join(["sh", "-c", script])]  # fmt: skip
    else:
        argv, env = ["sh", "-c", script], dict(os.environ)
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)  # fmt: skip
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a child stuck in the kernel (a hung /etc/pve) keeps the pipes: left as it is
            pass
        raise TimeoutError(f"no answer from the host in {timeout:.0f} s") from None
    return proc.returncode, out, err


def _said(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0][:240]}" if text else type(exc).__name__


class Collector(threading.Thread):
    source = ""

    def __init__(self, config, store, interval: float):
        super().__init__(name=f"worms-gui-{self.source}", daemon=True)
        self.config, self.store, self.interval = config, store, float(interval)
        self.stopping = threading.Event()
        self.woken = threading.Event()
        store.register(self.source, self.interval)

    def run(self):
        failures = 0
        while not self.stopping.is_set():
            started = time.monotonic()
            try:
                self.collect()
                failures = 0
            except Exception as exc:  # noqa: BLE001  (said in the store, tried again)
                failures += 1
                self.store.error(self.source, _said(exc))
            delay = min(MAX_BACKOFF, self.interval * 2 ** min(failures, 6)) if failures else self.interval
            self.woken.wait(max(0.2, delay - (time.monotonic() - started)))
            self.woken.clear()

    def collect(self):
        raise NotImplementedError

    def poke(self):
        self.woken.set()

    def stop(self):
        self.stopping.set()
        self.woken.set()


# -- Proxmox -----------------------------------------------------------------------------------------------

PAUSED = ("paused", "suspended", "prelaunch")  # QEMU's states of a VM that runs and does not
COUNTERS = ("netin", "netout", "diskread", "diskwrite")
RATES = ("net_in_bps", "net_out_bps", "disk_read_bps", "disk_write_bps")
IDLE = 30.0  # s without a change of a VM's counters: its rates are 0 (pvestatd changes them every ~10 s)


class Proxmox(Collector):
    source = "proxmox"

    def __init__(self, config, store, interval: float):
        super().__init__(config, store, interval)
        self.infra = None
        self.failures = 0
        self.seen: dict[int, tuple] = {}  # vmid -> (time, counters) of their last change
        self.rates: dict[int, list] = {}
        self.full: dict[int, dict] = {}  # vmid -> the full list's qmpstatus and pressure
        self.last_task: dict[int, dict] = {}
        self.deep_at = 0.0  # when the full list was last read
        self.refused: set[str] = set()  # the extras that failed (said once)

    def collect(self):
        if self.infra is None:
            from worms_cli.proxmox import Infra

            with SETUP:
                infra = Infra(self.config)
            if not infra.configured:
                raise RuntimeError(infra.error or "no Proxmox API configured (worms secret issue topology-token)")
            self.infra = infra
        try:
            self._collect(self.infra.api())
            self.failures = 0
        except Exception:
            self.failures += 1
            if self.failures >= 3 and self._tunnel_gone():
                self.infra, self.failures = None, 0  # a new API next time (from a laptop: a new tunnel, the old one is dead)
            raise

    def _tunnel_gone(self) -> bool:
        """Whether a new Infra may help: on the host always (no tunnel); from a laptop only when the
        tunnel's port no longer answers. An API that refuses (pveproxy down, the token) is asked again
        through the same tunnel: a new Infra would open one more each time."""
        if not self.config.ssh:
            return True
        port = _port_of((self.infra._cfg or {}).get("url", "")) if self.infra is not None else None
        return port is None or not _port_open(port)

    def _collect(self, api):
        now = time.time()
        resources = [r for r in api.cluster.resources.get(type="vm") if r.get("type") == "qemu" and not r.get("template")]
        nodes = sorted({r["node"] for r in resources if r.get("node")}) or [self.infra.node]
        deep = time.monotonic() - self.deep_at >= STATUS_EVERY * self.interval
        if deep:
            for node in nodes:
                for vm in self._extra("the full VM list", lambda: api.nodes(node).qemu.get(full=1)):
                    self.full[int(vm["vmid"])] = {"qmpstatus": vm.get("qmpstatus") or "", "psi_cpu": vm.get("pressurecpusome"),
                                                  "psi_io": vm.get("pressureiosome"),
                                                  "psi_mem": vm.get("pressurememorysome")}  # fmt: skip
                for task in self._extra("the recent tasks", lambda: api.nodes(node).tasks.get(limit=100)):
                    if str(task.get("id", "")).isdigit():
                        vmid = int(task["id"])
                        if vmid not in self.last_task or task.get("starttime", 0) >= self.last_task[vmid]["start"]:
                            self.last_task[vmid] = _task(task, active=False)
        active = {}
        for node in nodes:
            for task in self._extra("the active tasks", lambda: api.nodes(node).tasks.get(source="active")):
                if str(task.get("id", "")).isdigit():
                    active[int(task["id"])] = _task(task, active=True)
        vms = {}
        for r in resources:
            vmid = int(r["vmid"])
            status = r.get("status") or "unknown"
            full = self.full.get(vmid, {}) if status == "running" else {}
            if status == "running" and full.get("qmpstatus") in PAUSED:
                status = "paused"
            vms[vmid] = {
                "vmid": vmid, "name": r.get("name") or f"vm{vmid}", "node": r.get("node", ""), "pool": r.get("pool") or "",
                "status": status, "qmpstatus": full.get("qmpstatus", ""), "uptime": int(r.get("uptime") or 0),
                "cpu_pct": float(r.get("cpu") or 0) * int(r.get("maxcpu") or 0) * 100 if status != "stopped" else 0.0,
                "maxcpu": int(r.get("maxcpu") or 0), "mem": int(r.get("mem") or 0), "maxmem": int(r.get("maxmem") or 0),
                **dict(zip(RATES, self._rates(vmid, now, [int(r.get(c) or 0) for c in COUNTERS]))),
                "lock": r.get("lock") or "", "tags": r.get("tags") or "",
                "psi_cpu": full.get("psi_cpu"), "psi_io": full.get("psi_io"), "psi_mem": full.get("psi_mem"),
            }  # fmt: skip
        for gone in (set(self.seen) | set(self.full) | set(self.last_task)) - set(vms):
            for cache in (self.seen, self.rates, self.full, self.last_task):
                cache.pop(gone, None)
        tasks = {vmid: active.get(vmid) or self.last_task.get(vmid) for vmid in vms if vmid in active or vmid in self.last_task}
        self.store.update(self.source, pve_vms=vms, pve_tasks=tasks)
        if deep:
            self.deep_at = time.monotonic()

    def _extra(self, what: str, ask) -> list:
        """What /cluster/resources lacks (QEMU's state, the tasks): [] when the token may not ask it, said
        once; the VM list stands without it."""
        try:
            answer = ask()
        except Exception as exc:  # noqa: BLE001
            if what not in self.refused:
                self.store.log(f"proxmox: {what}: {_said(exc)} (shown without it)", "warn")
                self.refused.add(what)
            return []
        self.refused.discard(what)
        return answer or []

    def _rates(self, vmid: int, now: float, counters: list) -> list:
        """Bytes per second between the last two changes of the counters (Proxmox updates them every
        ~10 s: between two of our readings they mostly stand still); None until there are two."""
        seen = self.seen.get(vmid)
        if seen is None or any(new < old for new, old in zip(counters, seen[1])):  # new, or restarted: counters from 0
            self.seen[vmid] = (now, counters)
            self.rates[vmid] = [None] * len(counters)
        elif counters != seen[1]:
            span = max(now - seen[0], 0.5)
            self.rates[vmid] = [(new - old) / span for new, old in zip(counters, seen[1])]
            self.seen[vmid] = (now, counters)
        elif now - seen[0] > IDLE and self.rates.get(vmid, [None])[0] is not None:
            self.rates[vmid] = [0.0] * len(counters)
        return self.rates.get(vmid, [None] * len(counters))


def _port_of(url: str):
    from urllib.parse import urlsplit

    try:
        return urlsplit(url if "://" in url else f"https://{url}").port
    except ValueError:
        return None


def _port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        return True
    except OSError:
        return False


def _task(task: dict, active: bool) -> dict:
    return {"type": task.get("type", ""), "status": "running" if active else str(task.get("status", ""))[:120],
            "user": task.get("user", ""), "start": float(task.get("starttime") or 0), "end": float(task.get("endtime") or 0) or None,
            "active": active}  # fmt: skip


# -- the host ------------------------------------------------------------------------------------------------

PROXMOX_FIREWALL = ("proxmox-firewall", "worms-host-firewall", "worms-firewall-watchdog.timer", "worms-mirror")
WATCHDOG = "worms-firewall-watchdog.service"
GUEST_TABLE = "bridge proxmox-firewall-guests"

HOST_SCRIPT = r"""
echo @@now; date +%s
echo @@meminfo; cat /proc/meminfo
echo @@psi; cat /proc/pressure/memory 2>/dev/null
echo @@loadavg; cat /proc/loadavg
echo @@stat; grep '^cpu' /proc/stat
echo @@uptime; cat /proc/uptime
echo @@ksm; cat /sys/kernel/mm/ksm/pages_sharing 2>/dev/null && getconf PAGESIZE
echo @@arc; awk '$1 == "size" {{print $3}}' /proc/spl/kstat/zfs/arcstats 2>/dev/null
echo @@internet; [ -e {flag} ] && echo allowed || echo denied
echo @@paused; [ -e {paused} ] && {{ t=$(head -c 300 {paused} | tr '\n' ' '); echo "${{t:-paused}}"; }}
echo @@held; [ -e {held} ] && {{ echo exists; cat {held}; }}
echo @@window; cat {window} 2>/dev/null
echo @@units; systemctl is-active {units} 2>/dev/null
echo @@watchdog; systemctl show {watchdog} -p Result -p ExecMainStatus -p ExecMainExitTimestamp -p ActiveState \
  --timestamp=unix 2>/dev/null
echo @@guests; nft list table {guests} 2>/dev/null | grep -o 'tap[0-9]*i[0-9]*' | sort -u
echo @@vmfw; for f in /etc/pve/firewall/*.fw; do
  [ -e "$f" ] && printf '%s %s\n' "${{f##*/}}" "$(sed -n 's/^enable: *//p' "$f" | head -n 1)"
done
echo @@clean; grep -lx '\[clean\]' /etc/pve/qemu-server/*.conf 2>/dev/null
echo @@nics; awk 'FNR == 1 {{s = 0}} /^\[/ {{s = 1}} !s && /^net[0-9]+:/ {{print FILENAME, $0}}' \
  /etc/pve/qemu-server/*.conf 2>/dev/null
echo @@lock; inode=$(stat -c %i {lock} 2>/dev/null) && grep -q ":$inode " /proc/locks \
  && {{ cat {lock}.info 2>/dev/null || echo held; }}
"""
HOST_SLOW = r"""
echo @@ledger; tail -n 20000 {ledger} 2>/dev/null
echo @@meta; grep -H '^meta:' /etc/pve/qemu-server/*.conf 2>/dev/null
echo @@deployed; cat {deployed} 2>/dev/null
"""


def host_units() -> list[str]:
    """The host units the header and the Services tab show: worms_cli.services' host services, and the
    Proxmox firewall and the host firewall's own."""
    from worms_cli import services

    units = [unit for service in services.SERVICES.values() if service.where == "host" for unit in service.up_units]
    return list(dict.fromkeys([*units, *PROXMOX_FIREWALL]))


class Host(Collector):
    source = "host"

    def __init__(self, config, store, interval: float = 5.0):
        super().__init__(config, store, interval)
        from worms_cli import changes, history, jobs, lock, power
        from worms_cli.session import INTERNET_FLAG

        self.units = host_units()
        q = shlex.quote
        values = {"flag": q(INTERNET_FLAG), "paused": q(jobs.PAUSED), "held": q(power.HELD_DEVBOXES),
                  "window": q(power.WINDOW_STOPPED), "units": " ".join(map(q, self.units)), "watchdog": q(WATCHDOG),
                  "guests": GUEST_TABLE, "lock": q(lock.LOCK), "ledger": q(history.LEDGER),
                  "deployed": q(changes.DEPLOYED)}  # fmt: skip
        self.script = HOST_SCRIPT.format(**values)
        self.slow = HOST_SLOW.format(**values)
        self.cpu = None  # the last /proc/stat cpu line's counters
        self.system = None
        self.watchdog_ended = 0  # the host's clock
        self.slow_at = None  # when the slow part was last read (None: not yet)
        self.mirror = None  # the traffic mirror's bridge (vmbr3): the monitor's capture NIC on it has no firewall

    def _mirror_bridge(self):
        """The mirror bridge, as OpenTofu named it in the generated inventory (all.vars.bridges.mirror); None
        without one: a NIC there then counts as any other (OFF without firewall=1)."""
        import yaml

        try:
            text = (self.config.repo_root / "ansible" / "inventory.generated.yml").read_text()
            bridge = (((yaml.safe_load(text) or {}).get("all") or {}).get("vars") or {}).get("bridges", {}).get("mirror")
        except (OSError, yaml.YAMLError, AttributeError):
            return None
        return str(bridge) if bridge else None

    def collect(self):
        from worms_cli.session import for_config

        session = for_config(self.config)
        slow = self.slow_at is None or time.monotonic() - self.slow_at >= SLOW
        if slow:
            from worms_cli import components

            built = {name: components.stack_built(self.config.repo_root, c.stack) for name, c in components.COMPONENTS.items()
                     if c.kind == "stack"}  # fmt: skip
            self.mirror = self._mirror_bridge()
        if not self.config.ssh and not os.path.isdir("/etc/pve"):
            raise RuntimeError("not on the Proxmox host: set WORMS_SSH=root@<host>")
        code, out, err = host_command(self.config, self.script + (self.slow if slow else "") + "echo @@end\n")
        sections = _sections(out)
        if "end" not in sections:
            raise RuntimeError(err.strip()[:200] or f"the host's probe ended early (exit {code})")
        if self.system is None and slow:
            try:
                self.system = session.environment()  # after the probe: from a laptop its agent and login are there
            except Exception as exc:  # noqa: BLE001  (said; the rest is read all the same)
                self.store.log(f"host: which system this is: {_said(exc)}", "warn")
        host, extra = self._parse(sections)
        if self.system:
            host.update(name=self.system.get("host", ""), system=self.system.get("name", ""),
                        public_name=self.system.get("public_name", ""))
        if slow:
            extra["components_built"] = built
            self.slow_at = time.monotonic()
        self.store.update(self.source, host=host, **extra)

    def _parse(self, s: dict) -> tuple[dict, dict]:
        host: dict = {}
        mem = {k.rstrip(":"): int(v) * 1024 for k, v, *_ in (line.split() + ["", ""] for line in s.get("meminfo", [])) if v.isdigit()}
        host["mem_total"], host["mem_available"] = mem.get("MemTotal"), mem.get("MemAvailable")
        for line in s.get("psi", []):
            kind, _, rest = line.partition(" ")
            values = dict(part.split("=", 1) for part in rest.split() if "=" in part)
            host[f"psi_{kind}_avg10"] = _float(values.get("avg10"))
        load = (s.get("loadavg") or [""])[0].split()
        host["load"] = [_float(x) for x in load[:3]] if len(load) >= 3 else None
        cpus = [line.split() for line in s.get("stat", []) if line.startswith("cpu")]
        host["cpus"] = sum(1 for fields in cpus if fields[0] != "cpu") or None
        total = next(([int(x) for x in fields[1:9]] for fields in cpus if fields[0] == "cpu"), None)
        if total and self.cpu:
            host["cpu_pct"], _steal = _cpu_shares(self.cpu, total, 1)
        self.cpu = total or self.cpu
        host["uptime"] = _float((s.get("uptime") or [""])[0].split()[0] if s.get("uptime") else None)
        ksm = s.get("ksm", [])
        host["ksm_sharing_bytes"] = int(ksm[0]) * int(ksm[1]) if len(ksm) >= 2 and ksm[0].isdigit() and ksm[1].isdigit() else None
        arc = s.get("arc", [])
        host["arc_bytes"] = int(arc[0]) if arc and arc[0].isdigit() else None
        host["internet"] = (s.get("internet") or ["unknown"])[0]
        host["agent_paused"] = " ".join(s.get("paused", [])).strip()
        held = s.get("held", [])
        host["devboxes_held"] = bool(held)  # the record is there: the agent takes no devbox job (jobs.queue_reasons)
        host["held_devboxes"] = _record("\n".join(held[1:]))
        host["window_stopped"] = _record("\n".join(s.get("window", [])))
        states = s.get("units", [])
        host["units"] = {unit: states[i] if i < len(states) else "unknown" for i, unit in enumerate(self.units)}
        props = dict(line.split("=", 1) for line in s.get("watchdog", []) if "=" in line)
        now = _float((s.get("now") or [""])[0]) or time.time()
        ended = props.get("ExecMainExitTimestamp", "").lstrip("@")
        if ended.isdigit() and int(ended):
            self.watchdog_ended = int(ended)  # while it runs (a few seconds of every 15) it has no exit time: the last one
        running = props.get("ActiveState") in ("activating", "active", "reloading")
        status = props.get("ExecMainStatus", "")
        # In this machine's clock (a laptop's may differ from the host's): the snapshot ages it from there.
        at = time.time() - (now - self.watchdog_ended) if self.watchdog_ended else None
        host["watchdog"] = {"result": props.get("Result", ""), "status": int(status) if status.isdigit() else None,
                            "last_run_at": at, "running": running}
        host["vms_with_guest_rules"] = {int(m) for m in re.findall(r"tap(\d+)i\d+", " ".join(s.get("guests", [])))}
        enabled, cluster = {}, None
        for line in s.get("vmfw", []):
            name, _, value = line.partition(" ")
            if name == "cluster.fw":
                cluster = value.strip() == "1"
            elif name.removesuffix(".fw").isdigit():
                enabled[int(name.removesuffix(".fw"))] = value.strip() == "1"
        host["vm_firewall_enabled"], host["cluster_firewall"] = enabled, cluster
        host["clean_snapshots"] = {int(m) for m in re.findall(r"/(\d+)\.conf", " ".join(s.get("clean", [])))}
        host["lock"] = " ".join(s.get("lock", [])).strip()
        nics: dict[int, list] = {}  # vmid -> [NICs with firewall=1, NICs, NICs on the mirror bridge (not in the two)]
        for line in s.get("nics", []):
            match = re.match(r"\S*/(\d+)\.conf net\d+: (.*)", line)
            if match:
                counted = nics.setdefault(int(match.group(1)), [0, 0, 0])
                options = match.group(2).split(",")
                if self.mirror and f"bridge={self.mirror}" in options:
                    counted[2] += 1  # firewall=0 by design (modules/network_monitor_vm): worms-mirror makes it receive-only
                    continue
                counted[0] += "firewall=1" in options
                counted[1] += 1
        extra = {"vm_nics": nics}
        if "ledger" in s:
            from worms_cli import history

            extra["vm_history"] = history.summary(history.parse("\n".join(s["ledger"])))
            ctimes = {}
            for line in s.get("meta", []):  # /etc/pve/qemu-server/<vmid>.conf:meta: ...,ctime=<epoch>
                path, _, meta = line.partition(":meta:")
                vmid = path.rsplit("/", 1)[-1].removesuffix(".conf")
                if vmid.isdigit() and history.ctime(meta):
                    ctimes[int(vmid)] = {"time": history.ctime(meta), "commit": "?"}
            extra["vm_ctimes"] = ctimes
            try:
                record = json.loads("\n".join(s.get("deployed", [])) or "{}")
                extra["deployed"] = {name: (str(v.get("commit", "")), str(v.get("time", ""))) for name, v in record.items()
                                     if isinstance(v, dict)}  # fmt: skip
            except (ValueError, AttributeError):
                extra["deployed"] = {}
        return host, extra


def _cpu_shares(before: list, after: list, cpus: int) -> tuple:
    """(busy, steal) between two readings of /proc/stat's cpu line (user nice system idle iowait irq
    softirq steal), as shares of `cpus` x 100. Busy is the work done: user, nice, system, irq and
    softirq. Steal is not: it is time the hypervisor gave another (htop shows it apart), and in an
    overloaded host's VM it is most of what is not idle."""
    delta = [new - old for new, old in zip(after[:8], before[:8])]
    total = sum(delta)
    if total <= 0 or len(delta) < 8:
        return None, None
    busy = delta[0] + delta[1] + delta[2] + delta[5] + delta[6]
    return 100.0 * cpus * busy / total, 100.0 * cpus * delta[7] / total


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("@@") and line[2:].isidentifier():
            current = sections.setdefault(line[2:], [])
        elif current is not None:
            current.append(line)
    return sections


def _record(text: str) -> list[str]:
    """The VM names in one of power.py's records ({"vms": [{vmid, name}]})."""
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return ["(unreadable record)"]
    vms = data.get("vms", []) if isinstance(data, dict) else data
    return [str(vm.get("name")) for vm in vms if isinstance(vm, dict) and vm.get("name")] if isinstance(vms, list) else []


def _float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


# -- the database ----------------------------------------------------------------------------------------------


class Database(Collector):
    source = "db"

    def __init__(self, config, store, interval: float = 3.0):
        super().__init__(config, store, interval)
        self.ready = False
        self.teams_at = 0.0
        self.uuids: dict[str, str] = {}  # team uuid -> its name, for the jobs without a run
        self.failures = 0
        self.jobs_seen = (None, 0.0)  # (the jobs' signature, when they were read)

    def collect(self):
        from worms_cli import django_setup

        # setup() and a connection of our own each round, not connected(): that one keeps a failure for
        # the rest of the run (a CLI run is seconds long), and the GUI must see a database come back.
        # Django's settings banner goes to sys.stderr: app.py has made that the Log tab before this runs.
        if not self.ready:
            with SETUP:
                django_setup.setup(self.config)  # NotConfigured: said, and tried again
            self.ready = True
        from django.db import connection

        try:
            connection.ensure_connection()
            fields = {}
            if time.monotonic() - self.teams_at > TEAMS_EVERY:
                fields["teams"] = self._teams()
                self.teams_at = time.monotonic()
            jobs = self._jobs()
            if jobs is not None:  # read again: a check job may have ended
                fields["jobs"] = jobs
                fields["sandbox_check"] = self._sandbox_check()
            fields.update(self._rounds())
            self.failures = 0
        except Exception:
            connection.close()  # a database that went away: connect again next time
            self.failures += 1
            if self.failures >= 3:
                self._new_tunnel(connection)
            raise
        self.store.update(self.source, **fields)

    def _new_tunnel(self, connection) -> None:
        """From a laptop the database is reached through an SSH tunnel opened once, by setup(): when its
        port no longer answers, a new one, and Django is pointed at it."""
        port = str(connection.settings_dict.get("PORT") or "")
        if not self.config.ssh or not os.environ.get("WORMS_DB_HOSTADDR") or not port.isdigit() or _port_open(int(port)):
            return
        from worms_cli.session import for_config

        with SETUP:
            new = str(for_config(self.config).tunnel(os.environ["WORMS_DB_HOST"], 5432))
        from django.conf import settings

        settings.DATABASES["default"]["PORT"] = connection.settings_dict["PORT"] = os.environ["WORMS_DB_PORT"] = new
        self.failures = 0
        self.store.log(f"db: its SSH tunnel had ended: a new one (port {new})", "warn")

    def _teams(self) -> dict:
        from core.models.team import Team as TeamRow
        from worms_cli import django_setup, teams

        self.uuids = {str(uuid): name for uuid, name in TeamRow.objects.values_list("uuid", "name")}
        # connected() keeps a failure for the rest of a CLI run; the GUI runs for hours, and the
        # connection was just made.
        django_setup._UNREACHABLE = None
        names = [vm["name"] for vm in (self.store.get("pve_vms") or {}).values()]
        return {slug: {"name": t.name, "label": t.label, "handle": t.handle} for slug, t in teams.team_map(self.config, names).items()}

    def _jobs(self) -> list[dict] | None:
        """The newest JOBS host jobs, or None when nothing about them changed: one row (the newest id and
        the latest of each time) is asked every round, the 200 jobs only when it moved, the agent's state
        that the queue reasons depend on did, or JOBS_AGAIN s went by."""
        from core.models.rounds import HostJob
        from django.db.models import Max
        from worms_cli import jobs

        host = self.store.get("host") or {}
        agent = (host.get("units") or {}).get(jobs.AGENT_UNIT)
        state = {"agent": agent or "unknown", "held": bool(host.get("devboxes_held")), "paused": host.get("agent_paused", "")}
        times = HostJob.objects.aggregate(Max("id"), Max("requested_at"), Max("started_at"), Max("heartbeat_at"), Max("finished_at"))
        signature = (tuple(sorted(times.items())), tuple(sorted(state.items())))
        if signature == self.jobs_seen[0] and time.monotonic() - self.jobs_seen[1] < JOBS_AGAIN:
            return None
        self.jobs_seen = (signature, time.monotonic())
        out, asked = [], 0
        for job in HostJob.objects.select_related("run__team").order_by("-id")[:JOBS]:
            if job.run_id and job.run and job.run.team_id:
                team = job.run.team.name
            else:
                team = self.uuids.get(str((job.params or {}).get("team_uuid", "")), "")
            reasons = []
            if job.state == HostJob.State.QUEUED and asked < REASONS_FOR:
                reasons, asked = jobs.queue_reasons(job, state), asked + 1
            out.append({"id": job.id, "uuid": str(job.uuid), "kind": job.kind, "state": job.state, "team": team, "run": job.run_id,
                        "requested_at": _epoch(job.requested_at), "started_at": _epoch(job.started_at),
                        "heartbeat_at": _epoch(job.heartbeat_at), "finished_at": _epoch(job.finished_at),
                        "error": (job.error or "").strip(), "reasons": reasons})  # fmt: skip
        return out

    @staticmethod
    def _sandbox_check() -> dict | None:
        """The round's sandbox as its own `check` job saw it: the run that built it (the newest `prepare`
        that started: it built the sandbox or rolled it back, with that run's flags) and that run's newest
        `check` that started, its records by VM ("vm:<name>:<probe>"). Two queries; nothing is run on a
        sandbox VM from here. The details are the round agent's records, which carry what each VM answered
        (the UI shows them escaped)."""
        from core.models.rounds import HostJob

        # Started: one cancelled while it waited (`worms jobs cancel`) is failed, but never built anything.
        prepare = (HostJob.objects.filter(kind=HostJob.Kind.PREPARE, run__isnull=False, started_at__isnull=False)
                   .select_related("run__round", "run__team").order_by("-id").first())  # fmt: skip
        if prepare is None:
            return None
        run = prepare.run
        out = {"run": run.id, "round": run.round.name, "round_id": run.round_id, "team": run.team.name, "attempt": run.attempt,
               "prepare": {"id": prepare.id, "state": prepare.state, "finished_at": _epoch(prepare.finished_at)}, "check": None}
        check = HostJob.objects.filter(kind=HostJob.Kind.CHECK, run=run, started_at__isnull=False).order_by("-id").first()
        if check is None:
            return out
        result = check.result if isinstance(check.result, dict) else {}
        vms: dict[str, list] = {}
        host = []
        for row in result.get("checks") or []:
            if not isinstance(row, dict):
                continue
            name, ok, detail = str(row.get("check", ""))[:120], bool(row.get("ok")), str(row.get("detail") or "")[:300]
            parts = name.split(":", 2)
            if len(parts) == 3 and parts[0] == "vm":
                vms.setdefault(parts[1], []).append([parts[2], ok, detail])
            else:
                host.append([name, ok, detail])
        out["check"] = {"id": check.id, "state": check.state, "started_at": _epoch(check.started_at),
                        "finished_at": _epoch(check.finished_at), "ok": result.get("ok") if isinstance(result.get("ok"), bool) else None,
                        "error": (check.error or "").strip()[:300], "vms": vms, "host": host}  # fmt: skip
        return out

    def _rounds(self) -> dict:
        from core.models.competition import ApplicationSetting, Round
        from core.models.rounds import RoundRun
        from django.db.models import Count

        now = time.time()
        competition = ApplicationSetting.get_current_competition()
        rounds = []
        if competition is not None:
            counts: dict[int, dict] = {}
            for row in RoundRun.objects.filter(round__competition=competition).values("round_id", "state").annotate(n=Count("id")):
                counts.setdefault(row["round_id"], {})[row["state"]] = row["n"]
            for r in Round.objects.filter(competition=competition).order_by("round_start", "id"):
                start, end = _epoch(r.round_start), _epoch(r.round_end)
                rounds.append({"id": r.id, "name": r.name, "start": start, "end": end, "gameplan": r.gameplan,
                               "gameplan_status": r.gameplan_status, "team_duration": r.team_duration, "on_hold": r.on_hold,
                               "devboxes_off": r.devboxes_off, "devbox_hold": r.devbox_hold, "runs": counts.get(r.id, {}),
                               "published": r.results_published_at is not None,
                               "current": bool(start and start <= now and (end is None or now < end))})  # fmt: skip
        done = (RoundRun.State.FINISHED, RoundRun.State.FAILED, RoundRun.State.SKIPPED, RoundRun.State.PENDING)
        active = RoundRun.objects.exclude(state__in=done).select_related("round", "team").order_by("-id").first()
        run = None
        if active is not None:
            run = {"id": active.id, "round": active.round.name, "round_id": active.round_id, "team": active.team.name,
                   "state": active.state, "attempt": active.attempt, "worm_start_at": _epoch(active.worm_start_at),
                   "ends_at": _epoch(active.ends_at), "state_changed_at": _epoch(active.state_changed_at)}  # fmt: skip
        return {"rounds": rounds, "run": run}


def _epoch(value):
    return value.timestamp() if value is not None else None


# -- the watchers ---------------------------------------------------------------------------------------------

# One python3 on the VM (Ansible needs it there): a JSON line every 3 s. It reads /proc and statvfs itself and
# starts no process but `systemctl list-units` every 30 s: auditd logs every execve on the platform's VMs
# (ansible/roles/audit/auditd), and a shell loop of head, awk, df and sleep filled that log with its own trace.
# Its own process is left out of the busiest ones. Read-only; it ends at the first line it cannot write.
WATCH = r"""
import json, os, subprocess, sys, time
units = [u if "." in u else u + ".service" for u in sys.argv[1:]]
me = os.getpid()
def cpu():
    with open("/proc/stat") as f:
        return [int(x) for x in f.readline().split()[1:9]]
def procs():
    out = {}
    for pid in os.listdir("/proc"):
        if pid.isdigit() and int(pid) != me:
            try:
                with open("/proc/%s/stat" % pid, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            head, _, rest = data.rpartition(b")")
            fields = rest.split()
            if len(fields) > 12:
                out[pid] = (int(fields[11]) + int(fields[12]), head.partition(b"(")[2].decode("utf-8", "replace"))
    return out
def systemd():
    try:
        rows = [line.split() for line in subprocess.run(["systemctl", "list-units", "--all", "--no-legend", "--plain", "--full"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20).stdout.splitlines()]
    except (OSError, subprocess.SubprocessError):
        return None, {}
    active = {row[0]: row[2] for row in rows if len(row) >= 3}
    return sum(1 for state in active.values() if state == "failed"), {u: active.get(u, "inactive") for u in units}
with open("/proc/stat") as f:
    n = sum(1 for line in f if line[:3] == "cpu" and line[3:4].isdigit())
before, seen, asked, failed, states = cpu(), procs(), 0.0, None, {}
while True:
    time.sleep(3)
    now, ps = cpu(), procs()
    d = [a - b for a, b in zip(now, before)]
    total = sum(d) or 1
    top = sorted(((t - seen[p][0]) * 100.0 * n / total, c) for p, (t, c) in ps.items() if p in seen and t > seen[p][0])[-3:]
    before, seen = now, ps
    if time.time() - asked >= 30:
        failed, states, asked = *systemd(), time.time()
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, value = line.partition(":")
            mem[key] = int(value.split()[0]) if value.split() else 0
    with open("/proc/loadavg") as f:
        load = [float(x) for x in f.read().split()[:3]]
    fs = os.statvfs("/")
    used = (fs.f_blocks - fs.f_bfree) * fs.f_frsize
    root = 100.0 * used / (used + fs.f_bavail * fs.f_frsize) if fs.f_blocks else None
    line = {"load": load, "mem": [mem.get("MemTotal", 0), mem.get("MemAvailable", 0)], "cpus": n,
            "cpu": 100.0 * n * (d[0] + d[1] + d[2] + d[5] + d[6]) / total, "steal": 100.0 * n * d[7] / total,
            "root": root, "failed": failed, "units": states, "top": [[round(p, 1), c] for p, c in reversed(top)]}
    try:
        sys.stdout.write(json.dumps(line) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        sys.exit(0)
"""

# Two worms-gui on one machine share one set of watchers: the first to hold SHARE's lock runs them and writes what
# they see to SHARE's file every round; another reads it, and runs its own once the first has quit.
SHARE_DIRS = ("/run/worms", "~/.worms")


def _share_dir() -> Path:
    for candidate in SHARE_DIRS:
        path = Path(candidate).expanduser()
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(path, os.W_OK):
            return path
    raise OSError(f"none of {', '.join(SHARE_DIRS)} can be written")


class Watchers(Collector):
    """Which VMs have a watcher: every running trusted one, and no other; and whether this worms-gui
    runs them or another one on this machine does."""

    source = "watchers"

    def __init__(self, config, store, interval: float = 2.0):
        super().__init__(config, store, interval)
        self.catalogue = None
        self.catalogue_at = 0.0
        self.catalogue_asked = 0.0  # when the addresses were last read for a VM they lacked
        self.running: dict[int, Watcher] = {}
        self.owner = None  # the share's lock file, open while this GUI runs the watchers
        self.other = ""  # who runs them, when another worms-gui does (said once)
        self.shared: set = set()  # the vmids taken from the other one's file

    def collect(self):
        if self.owner is None and not self._own():
            self._read_share()
            self.store.update(self.source)
            return
        vms = self.store.get("pve_vms") or {}
        wanted = {vmid: vm for vmid, vm in vms.items() if vm.get("status") == "running" and trusted(vm["name"], vm.get("pool"))}
        for vmid, watcher in list(self.running.items()):
            if vmid not in wanted or wanted[vmid]["name"] != watcher.vm["name"] or not watcher.is_alive():
                watcher.stop()
                del self.running[vmid]
                self.store.discard("watch", vmid)
        for vmid in set((self.store.get("watch") or {})) - set(wanted) - set(self.running):
            self.store.discard("watch", vmid)  # what another GUI's watchers said, or a VM that stopped
        if wanted:
            self._catalogue(wanted)
        new = 0
        for vmid, vm in wanted.items():
            if vmid not in self.running and not self.stopping.is_set():
                # Not all at once: from a laptop each goes through the host's sshd (ProxyJump), whose
                # MaxStartups resets the handshakes beyond 10.
                self.running[vmid] = Watcher(lambda: self.catalogue, self.store, vm, delay=STAGGER * new)
                self.running[vmid].start()
                new += 1
        self._write_share()
        self.store.update(self.source)

    def _catalogue(self, wanted: dict) -> None:
        """How to reach the VMs (worms_cli.vms.Catalogue): read again every CATALOGUE_FOR s, and sooner
        (once a minute at most) when a VM to watch is missing from it: a new or rebuilt one."""
        now = time.monotonic()
        stale = self.catalogue is None or now - self.catalogue_at > CATALOGUE_FOR
        if not stale and now - self.catalogue_asked > CATALOGUE_AGAIN:
            known = self.catalogue._hosts or {}
            stale = any(vm["name"] not in known for vm in wanted.values())  # its watcher retries with the new one
            self.catalogue_asked = now if stale else self.catalogue_asked
        if not stale:
            return
        from worms_cli.vms import Catalogue

        with SETUP:
            catalogue = Catalogue(self.config)
            catalogue.runner.session.env()  # its ssh-agent, started here: not by several watchers at once
        catalogue._hosts = _hosts(self.config, catalogue)  # outside SETUP: it asks the host, with a timeout
        self.catalogue, self.catalogue_at = catalogue, now

    def _own(self) -> bool:
        """Whether this GUI holds the share's lock now (it then runs the watchers)."""
        try:
            path = _share_dir() / "gui-watchers.lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            if self.other != str(exc):
                self.store.log(f"watchers: no lock to share them ({exc}): this worms-gui runs its own", "warn")
                self.other = str(exc)
            self.owner = -1
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                who = os.pread(fd, 200, 0).decode(errors="replace").strip()
            finally:
                os.close(fd)
            if self.other != who:
                self.store.log(f"watchers: another worms-gui runs them on this machine ({who or 'pid ?'}): this one shows what "
                               "they see, and runs its own once that one has quit")  # fmt: skip
                self.other = who
            return False
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()}, since {time.strftime('%H:%M:%S UTC', time.gmtime())}\n".encode(), 0)
        self.owner = fd
        if self.other:
            self.store.log("watchers: the other worms-gui has quit: this one runs them now", "ok")
        self.other = ""
        return True

    def _write_share(self) -> None:
        if self.owner is None or self.owner < 0:
            return
        watch = self.store.get("watch") or {}
        try:
            path = _share_dir() / "gui-watch.json"
            tmp = path.with_name(f".{path.name}.{os.getpid()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump({str(vmid): record for vmid, record in watch.items()}, out)
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError) as exc:
            self.store.log(f"watchers: the shared file: {_said(exc)}", "warn")

    def _read_share(self) -> None:
        """The watch records another worms-gui's watchers wrote: stale by their own time when it stops."""
        try:
            data = json.loads((_share_dir() / "gui-watch.json").read_text())
            records = {int(vmid): record for vmid, record in data.items() if isinstance(record, dict)}
        except (OSError, ValueError, AttributeError):
            records = {}
        for vmid in self.shared - set(records):
            self.store.discard("watch", vmid)
        self.shared = set(records)
        if records:
            self.store.update(self.source, watch=records)

    def stop(self):
        super().stop()
        for watcher in list(self.running.values()):
            watcher.stop()
        if self.owner is not None and self.owner >= 0:
            try:
                os.close(self.owner)  # the lock goes with it: another worms-gui takes the watchers over
            except OSError:
                pass


def _hosts(config, catalogue) -> dict:
    """Catalogue.hosts(), with a timeout: this checkout's inventory, then the round agent's on the host."""
    hosts = {}
    local = config.repo_root / "ansible" / "inventory.generated.yml"
    if local.exists():
        hosts.update(catalogue._inventory(local.read_text()))
    agent = '"$(sed -n "s/^REPO_ROOT=//p" /etc/worms/round-agent.env)"'  # the round agent's checkout on the host
    code, out, _err = host_command(config, f"cat {agent}/ansible/inventory.generated.yml")
    if code == 0:
        for name, host in catalogue._inventory(out).items():
            hosts.setdefault(name, host)
    hosts.pop("proxmox.local", None)
    return hosts


class Watcher(threading.Thread):
    """One VM's SSH and its loop; what it prints goes to the store (`watch`, under its vmid)."""

    def __init__(self, catalogue, store, vm: dict, delay: float = 0.0):
        """`catalogue`: a callable, the Watchers' current worms_cli.vms.Catalogue (how to reach a VM)."""
        from worms_cli import services

        super().__init__(name=f"worms-gui-watch-{vm['vmid']}", daemon=True)
        self.catalogue, self.store, self.vm, self.delay = catalogue, store, dict(vm), delay
        self.vmid = int(vm["vmid"])
        self.units = [unit for name in services.on(vm["name"]) for unit in services.SERVICES[name].up_units]
        self.units = list(dict.fromkeys(self.units))
        self.stopping = threading.Event()
        self.proc = None
        self.last: dict = {}
        self.said = ""

    def run(self):
        failures = 0
        self.stopping.wait(self.delay)
        while not self.stopping.is_set():
            started = time.monotonic()
            error = self._once()
            if self.stopping.is_set():
                break
            failures = 0 if time.monotonic() - started > 60 else failures + 1
            delay = WATCH_RESTART[min(failures, len(WATCH_RESTART) - 1)]
            if error != self.said:  # once in the log, not at every try
                self.store.log(f"watcher {self.vm['name']}: {error}", "warn")
                self.said = error
            self._put({**self.last, "error": f"{error}; again in {delay} s"})
            self.stopping.wait(delay)

    def _put(self, record: dict) -> None:
        if not self.stopping.is_set():  # a stopped one writes nothing more: its record went with it
            self.store.update("watchers", watch={self.vmid: record})

    def _once(self) -> str:
        """One SSH session, until it ends: why it did."""
        command = shlex.join(["python3", "-c", WATCH, *self.units])
        try:
            argv, env = self.catalogue().ssh_argv(self.vm, command, interactive=False)
        except Exception as exc:  # noqa: BLE001  (no management address: said, tried again)
            return _said(exc)
        argv[1:1] = ["-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]  # a VM gone quiet: noticed in 30 s
        try:
            self.proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         start_new_session=True)  # fmt: skip
        except OSError as exc:
            return _said(exc)
        CHILDREN.add(self.proc)
        if self.stopping.is_set():  # stopped while it started
            self.stop()
        errors: deque = deque(maxlen=3)
        reader = threading.Thread(target=lambda: errors.extend(line.decode(errors="replace").strip() for line in self.proc.stderr),
                                  daemon=True)  # fmt: skip
        reader.start()
        for raw in self.proc.stdout:
            data = self._parse(raw.decode(errors="replace"))
            if data is not None:
                self.last = data
                self._put(data)
        code = self.proc.wait()
        CHILDREN.discard(self.proc)
        reader.join(timeout=2)
        said = "; ".join(line for line in errors if line)[:200]
        return f"its SSH ended (exit {code})" + (f": {said}" if said else "")

    def _parse(self, line: str) -> dict | None:
        """A line of WATCH, checked field by field (the VM is a platform VM: still, what it says is data)."""
        try:
            data = json.loads(line)
            load = [float(x) for x in data["load"][:3]]
            total, available = (int(x) for x in data["mem"][:2])
            cpus = int(data["cpus"])
            failed = None if data.get("failed") is None else int(data["failed"])
            units = data.get("units") or {}
            top = [[float(pct), "".join(ch for ch in str(command) if ch.isprintable())[:40]]
                   for pct, command in data.get("top") or []][:3]  # fmt: skip
            numbers = {key: None if data.get(key) is None else float(data[key]) for key in ("cpu", "steal", "root")}
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return None
        if len(load) < 3:
            return None
        states = {}
        for unit in self.units:
            state = units.get(unit if "." in unit else unit + ".service") if isinstance(units, dict) else None
            states[unit] = str(state)[:20] if state is not None else None
        return {"load1": load[0], "load5": load[1], "load15": load[2], "cpus": cpus,
                "mem_used_pct": 100.0 * (total - available) / total if total else None, "cpu_pct": numbers["cpu"],
                "steal_pct": numbers["steal"], "root_used_pct": numbers["root"], "failed_units": failed, "top": top,
                "units": states, "at": time.time(), "error": ""}  # fmt: skip

    def stop(self):
        self.stopping.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass


# -- all of them ----------------------------------------------------------------------------------------------


class Collectors:
    def __init__(self, config, store, interval: float = 2.0, watchers: bool = True):
        """`interval`: the Proxmox collector's (--interval); the host's and the database's follow it,
        never faster than 5 s and 3 s."""
        self.threads: list[Collector] = [Proxmox(config, store, interval), Host(config, store, max(5.0, 2.5 * interval)),
                                         Database(config, store, max(3.0, 1.5 * interval))]  # fmt: skip
        if watchers:
            self.threads.append(Watchers(config, store))

    def start(self) -> None:
        for thread in self.threads:
            thread.start()

    def refresh(self) -> None:
        for thread in self.threads:
            thread.poke()

    def stop(self, timeout: float = 3.0) -> None:
        for thread in self.threads:
            thread.stop()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
