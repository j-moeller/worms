"""worms-gui's start and stop: its options, the store, the collectors and the runner wired to the screen.

    worms-gui                      the live platform (the host this runs on, or WORMS_SSH's)
    worms-gui --dry-run            every action is shown (status line, Log tab), none runs; the devbox
                                   checks (`worms check --json`) still run: they change nothing
    worms-gui --no-auto-check      a devbox check only on c or C, never by itself when the cursor rests
    worms-gui --interval 5         the base refresh (the Proxmox API), in seconds
    worms-gui --no-watchers        no SSH watchers on the platform VMs: the host's view only
    worms-gui --tab teams          start on a tab: platform, teams, jobs, rounds, services, components,
                                   tasks, log (vms: platform)

A hidden --demo[=production] draws made-up records (some 40 VMs of every kind, jobs, rounds, tasks,
devbox checks and a round's check job) with actions.py's own menus, whose commands run nothing (they
change the made-up records; a devbox check makes its results up): the screen and the menus without a
platform, to try them and to check them.
"""

from __future__ import annotations

import argparse
import copy
import locale
import os
import random
import shlex
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path

from worms_cli.gui import actions, checks, ui
from worms_cli.gui.state import CHECKS, group, trusted

MANAGE = str(Path(__file__).resolve().parents[2] / "manage.py")  # this checkout's CLI: the GUI's own


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="worms-gui", description="An htop-like, interactive dashboard of the WORMS platform: "
                                "every VM, job, round, service and component, live; the CLI's commands on the marked "
                                "ones. `?` in it lists the keys.")  # fmt: skip
    p.add_argument("--dry-run", action="store_true", help="every action is shown, not run; the devbox checks (`worms check "
                   "--json`, read-only: they change nothing) still run, by themselves too (--no-auto-check: only on c, C)")  # fmt: skip
    p.add_argument("--no-auto-check", action="store_true", help="the Teams tab checks a devbox only on c or C, not by itself "
                   "when the cursor rests on a team's VM")  # fmt: skip
    p.add_argument("--interval", type=float, default=2.0, metavar="SECONDS", help="the base refresh (the Proxmox API), default 2")
    p.add_argument("--no-watchers", action="store_true", help="no SSH watchers on the platform VMs (the host's view only)")
    p.add_argument("--tab", choices=(*ui.TAB_NAMES, *ui.TAB_ALIASES), default="platform",
                   help="the tab to start on (default platform; vms: platform)")
    p.add_argument("--demo", nargs="?", const="development", choices=("development", "production"), help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.interval < 0.5:
        parser().error("--interval: half a second at least")
    problem = ui.terminal_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    ascii_only = _locale()
    backend = Demo(args.demo, args) if args.demo else Live(args)
    stderr = sys.stderr
    # The terminal closed (SIGHUP) or a kill (SIGTERM): an exit through the finally below and atexit, as `q`
    # is; by default Python dies at once, and the session's ssh-agent with the WORMS key in it stays behind.
    for signum in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, _ended_by)
    try:
        # Before the collectors start: a library's print (Django's settings banner, a warning) goes to the Log
        # tab, and no thread swaps sys.stderr under another.
        sys.stderr = backend.stderr()
        backend.begin()
        return ui.run(backend, tab=args.tab, ascii_only=ascii_only)
    finally:
        sys.stderr = stderr
        for signum in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(signum, signal.SIG_IGN)  # a second one does not cut the stop short
        backend.stop()


def _ended_by(signum, _frame):
    """An exit as `q` is; while a command runs on the terminal, once it has ended (actions.HOLD): raised
    there, the exception would SIGKILL it."""
    if actions.HOLD.active:
        actions.HOLD.signum = signum
        return
    raise SystemExit(128 + signum)


def _locale() -> bool:
    """The terminal's locale for curses (ASCII glyphs when it is not UTF-8: True). Its children get a
    UTF-8 one all the same: the vault's ansible-vault refuses to run under another (`ERROR: Ansible
    requires the locale encoding to be UTF-8`), and with it the Proxmox and database sources. This
    process keeps the terminal's: the environment is read by setlocale, which is not called again."""
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass  # a locale not installed: C's
    codeset = (locale.nl_langinfo(locale.CODESET) or "").lower().replace("-", "").replace("_", "")
    if codeset != "utf8":
        os.environ["LC_ALL"] = "C.UTF-8"
        return True
    return False


# -- the live platform ---------------------------------------------------------------------------------


class Live:
    """The store (state.py), its collectors (collect.py), the menus and the runner (actions.py)."""

    def __init__(self, args):
        from worms_cli.config import Config
        from worms_cli.gui import actions, checks, collect, state

        self.dry_run, self.watchers, self.auto_checks = args.dry_run, not args.no_watchers, not args.no_auto_check
        self.state, self.actions = state, actions
        self.store = state.Store()
        # The runner first: its commands get the environment as it is now (main's locale in it), not the
        # database settings that the database collector's Django setup puts into this process's.
        self.runner = actions.Runner(self.store, dry_run=args.dry_run, env=dict(os.environ))
        # The devbox checks: the CLI's, read-only; an every-team run's timeout grows with the teams.
        self.checks = checks.Checks(self.store, checks.CliCheck(self.runner.env, lambda: len(self.store.get("teams") or {})))
        self.collectors = collect.Collectors(Config.load(), self.store, interval=args.interval, watchers=self.watchers)

    def begin(self):
        said = self.store.say if self.dry_run and self.auto_checks else self.store.log
        said("worms-gui started" + (" with --dry-run: no action runs; the panes' `worms show`, the watchers and the devbox "
                                    "checks still read (a check runs as root on a team's VMs when the cursor rests on one: "
                                    "--no-auto-check for none)" if self.dry_run else "")
             + ("" if self.watchers else "; --no-watchers") + ("" if self.auto_checks else "; --no-auto-check"),
             "warn" if self.dry_run and self.auto_checks else "info")  # fmt: skip
        self.checks.start()
        self.collectors.start()

    def stderr(self):
        return self.state.LogStream(self.store)

    def snapshot(self):
        return self.store.snapshot()

    @property
    def version(self):
        """Goes up with every change of the store: the screen takes a new snapshot only then."""
        return self.store.version

    def menu(self, tab, items, snap):
        return self.actions.menu(tab, items, snap)

    def start(self, action):
        return self.runner.start(action)

    def foreground(self, actions) -> int:
        return self.runner.foreground(actions)

    def check(self, slugs, auto=False) -> str:
        """A devbox check of these teams ([] = every team), queued: `worms check --json` changes nothing, so it
        runs with --dry-run too. auto: the cursor's, not asked for (checks.Checks.want)."""
        return self.checks.want(slugs, auto=auto)

    def task_signal(self, task_id, what):
        """cancel (Ctrl-C, then SIGTERM), pause (SIGSTOP), resume (SIGCONT): the runner's."""
        if getattr(self.runner, what)(task_id):
            return ""
        state = (self.store.task(task_id) or {}).get("state")
        return {"waiting": "it waits for the host's lock: no process yet", "paused": "paused already: P resumes it",
                "running": "it runs: not paused"}.get(state, "it does not run")  # fmt: skip

    def read_argv(self, words):
        return self.actions.worms(*words)

    def read_env(self):
        return self.runner.env

    def refresh(self):
        self.collectors.refresh()

    def say(self, text, level="info"):
        self.store.say(text, level)

    def stop(self):
        self.runner.shutdown()  # what runs or is paused: Ctrl-C (a paused one continued first)
        self.checks.stop()  # a `worms check` under way: Ctrl-C, then SIGTERM, SIGKILL
        self.collectors.stop()


# -- the demo: made-up records, commands that run nothing -----------------------------------------------


PLATFORM_VMS = (  # name, vmid, pool, cores, MB: as `worms vm` lists worms-dev's
    ("dashboard", 110, "persistent", 2, 1024), ("database", 107, "persistent", 2, 2048),
    ("devbox-ssh-gateway", 115, "persistent", 1, 512), ("orchestrator", 109, "persistent", 2, 1024),
    ("syslog", 108, "persistent", 1, 512), ("logging", 112, "ephemeral", 1, 512), ("monitor", 113, "ephemeral", 2, 2048),
    ("scoring", 111, "ephemeral", 1, 512), ("apt-cache", 106, "private_internet", 1, 1024),
    ("cve-search", 103, "private_internet", 2, 4096), ("devpi", 102, "private_internet", 1, 2048),
    ("dns", 114, "private_internet", 1, 512), ("docker-registry", 101, "private_internet", 1, 1024),
    ("manpages", 100, "private_internet", 1, 1024), ("ntp", 104, "private_internet", 1, 512),
    ("wikipedia", 105, "private_internet", 1, 1024),
)  # fmt: skip
TEAMS = (("t1", "charlie"), ("t2", "alpha"), ("t3", "bravo"), ("t4", "delta"))
SANDBOX_VMS = ("sandbox-gw", "sb-web-01", "sb-db-01", "sb-ws-01", "sb-ws-02", "sb-dc-01", "sb-files-01", "sb-mail-01")
TOP = {"dashboard": "gunicorn: worker", "database": "postgres: checkpointer", "orchestrator": "python conduct_rounds",
       "syslog": "rsyslogd", "logging": "uvicorn sandbox_api", "monitor": "zeek", "scoring": "uvicorn sandbox_api",
       "apt-cache": "apt-cacher-ng", "cve-search": "mongod", "devpi": "devpi-server", "dns": "named",
       "docker-registry": "registry serve", "manpages": "nginx", "ntp": "chronyd", "wikipedia": "kiwix-serve"}  # fmt: skip
JOB_KINDS = ("prepare", "check", "deploy", "remove_flags", "place_flags", "read_file", "teardown", "devbox_reconfigure",
             "devbox_deploy", "hold_devboxes", "release_devboxes", "gameplans", "devbox_provision")  # fmt: skip


class DemoStore:
    """A store in state.py's snapshot shape, filled with made-up records that move a little each tick."""

    def __init__(self, system: str, interval: float):
        self.lock = threading.RLock()
        self.interval = interval
        self.started = time.time()
        self.log = deque(maxlen=500)
        self.status = None
        self.tasks: dict[int, dict] = {}
        self.next_task = 1
        self.random = random.Random(7)
        self.version = 0  # goes up with every change, as Store.version
        self.sources = {"proxmox": interval, "host": 5.0, "db": 3.0, "watchers": 2.0}
        self.updated = {name: time.time() for name in self.sources}
        self.world = self._world(system)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._tick_forever, name="worms-gui demo", daemon=True)

    # -- the made-up platform ---------------------------------------------------------------------------

    def _world(self, system):
        from worms_cli import components, services

        now = time.time()
        rnd = self.random
        vms = {}
        held = [f"devbox-t4-{role}" for role in ("a", "b", "c", "gw")]
        for name, vmid, pool, cores, mb in PLATFORM_VMS:
            vms[vmid] = self._vm(name, vmid, pool, cores, mb, "stopped" if name == "wikipedia" else "running", now)
        vmid = 116
        for slug, team in TEAMS:
            for role in ("a", "b", "c", "gw"):
                name = f"devbox-{slug}-{role}"
                vm = self._vm(name, vmid, "devbox", 1, 512, "stopped" if name in held else "running", now, team=(slug, team, role))
                vm["held"] = name in held
                vms[vmid] = vm
                vmid += 1
        for i, name in enumerate(SANDBOX_VMS):
            status = "paused" if name == "sb-files-01" else "stopped" if name == "sb-mail-01" else "running"
            vms[140 + i] = self._vm(name, 140 + i, "sandbox", 2 if name == "sb-dc-01" else 1, 1024, status, now)
        vms[121]["firewall"].update(rules=False, summary="OFF")  # devbox-t2-b: the host lost its guest rules
        vms[103]["clean"] = False  # cve-search: no `clean` snapshot
        vms[113]["watch"].update(error="ssh: connect to host 10.10.0.13 port 22: Connection timed out", at=now - 40)
        vms[111]["watch"].update(failed_units=1, units={"sandbox-api": "failed"})
        vms[146]["task"] = {"type": "qmsuspend", "status": "OK", "user": "root@pam", "start": now - 300, "end": now - 299,
                            "active": False}
        vms[142]["task"] = {"type": "qmrollback", "status": "", "user": "worms@pve", "start": now - 4, "end": None, "active": True}
        host_units = {unit: "active" for s in services.SERVICES.values() if s.where == "host" for unit in s.up_units}
        host_units["proxmox-firewall"] = "active"
        host = {
            "name": "worms" if system == "production" else "worms-dev", "public_name": "demo.invalid", "system": system,
            "cpu_pct": 23.0, "cpus": 32, "load": [3.12, 2.87, 2.65], "mem_total": 128 << 30, "mem_available": 61 << 30,
            "psi_some_avg10": 0.4, "psi_full_avg10": 0.0, "ksm_sharing_bytes": int(3.2 * (1 << 30)), "arc_bytes": 16 << 30,
            "uptime": 18 * 86400 + 10 * 3600, "internet": "denied", "agent_paused": "", "held_devboxes": held,
            "window_stopped": [], "units": host_units, "watchdog": {"result": "success", "status": 0, "last_run_at": now - 3},
            "cluster_firewall": True, "vms_with_guest_rules": {v for v, vm in vms.items() if vm["firewall"]["rules"]},
            "vm_firewall_enabled": {v: True for v in vms}, "clean_snapshots": {v for v, vm in vms.items() if vm["clean"]}, "lock": "",
        }  # fmt: skip
        jobs = []
        teams = [f"{team} ({slug})" for slug, team in TEAMS]
        for i, number in enumerate(range(836, 800, -1)):
            kind = JOB_KINDS[(number * 7) % len(JOB_KINDS)]
            state = "queued" if i < 4 else "running" if i == 4 else "failed" if number in (812, 803) else "done"
            requested = now - 60 * (i + 1) * 3 - rnd.randint(0, 50)
            started = None if state == "queued" else requested + rnd.randint(1, 20)
            finished = None if state in ("queued", "running") else started + rnd.randint(4, 400)
            reasons = []
            if state == "queued":
                ahead = 836 - 4  # the running one
                reasons = [f"a job runs ahead: #{ahead} {JOB_KINDS[(ahead * 7) % len(JOB_KINDS)]} ({teams[ahead % 4]})"]
                reasons += [f"jobs queued ahead: {3 - i}"] if i < 3 else []
            error = {812: "ansible-playbook SetupDevbox.yml: exit 2 (UNREACHABLE: devbox-t3-gw)",
                     803: "sandbox-gw: no SSH after 300 s"}.get(number, "")  # fmt: skip
            jobs.append({"id": number, "uuid": f"6f1c{number:04d}-demo", "kind": kind, "state": state, "team": teams[number % 4],
                         "run": 30 + number % 4 if kind in ("prepare", "check", "deploy", "teardown") else None,
                         "requested_at": requested, "started_at": started, "heartbeat_at": now - 2 if state == "running" else None,
                         "finished_at": finished, "error": error, "reasons": reasons})  # fmt: skip
        rounds = [
            {"id": 1, "name": "Warm-up", "start": now - 3 * 86400, "end": now - 3 * 86400 + 4 * 3600, "gameplan": "warmup",
             "gameplan_status": "valid", "team_duration": 1200, "on_hold": False, "devboxes_off": False, "devbox_hold": False,
             "runs": {"finished": 4}, "current": False},
            {"id": 2, "name": "Round 1", "start": now - 2 * 3600, "end": now + 4 * 3600, "gameplan": "lateral-movement",
             "gameplan_status": "valid", "team_duration": 1800, "on_hold": False, "devboxes_off": True, "devbox_hold": True,
             "runs": {"finished": 1, "failed": 1, "running": 1, "pending": 1}, "current": True},
            {"id": 3, "name": "Round 2", "start": now + 86400, "end": now + 86400 + 6 * 3600, "gameplan": "ransom",
             "gameplan_status": "invalid", "team_duration": 1800, "on_hold": False, "devboxes_off": False, "devbox_hold": False,
             "runs": {}, "current": False},
            {"id": 4, "name": "Final", "start": now + 3 * 86400, "end": now + 3 * 86400 + 8 * 3600, "gameplan": "final",
             "gameplan_status": "unchecked", "team_duration": 2400, "on_hold": False, "devboxes_off": False, "devbox_hold": False,
             "runs": {}, "current": False},
        ]  # fmt: skip
        run = {"id": 33, "round": "Round 1", "round_id": 2, "team": "alpha (t2)", "state": "running", "attempt": 2,
               "worm_start_at": now - 180, "ends_at": now + 720, "state_changed_at": now - 180}  # fmt: skip
        comps = [{"name": c.name, "about": c.help, "kind": c.kind, "deps": list(c.deps), "exists": True,
                  "deployed_commit": "b24c6b7" if c.kind != "devbox" else "",
                  "deployed_at": "2026-11-21T18:02:11" if c.kind != "devbox" else ""}
                 for c in components.COMPONENTS.values()]  # fmt: skip
        self._log("info", "worms-gui started: the demo (made-up records; nothing runs)")
        self._log("error", "watchers: monitor: ssh: connect to host 10.10.0.13 port 22: Connection timed out")
        self._log("warn", "scoring: sandbox-api failed (its watcher)")
        known = {slug: {"name": team, "label": f"{team} ({slug})", "handle": team} for slug, team in TEAMS}
        world = {"host": host, "vms": vms, "jobs": jobs, "rounds": rounds, "run": run, "components": comps, "teams": known,
                 "checks": copy.deepcopy(CHECKS), "sandbox_check": demo_sandbox_check(vms, now)}  # fmt: skip
        self._add_task("vm restart dns", ["vm", "restart", "dns"], ["dns"], "done", ["stopping dns (114)", "starting dns (114)",
                       "dns: running"], 0, now - 900, locks=True)  # fmt: skip
        self._add_task("service restart scoring", ["service", "restart", "scoring"], ["scoring"], "failed",
                       ["restarting scoring on scoring: sandbox-api",
                        "error: scoring on scoring: restart failed or not active after it (exit 3): `worms log scoring`"], 1,
                       now - 600,
                       locks=True)  # fmt: skip
        return world

    def _vm(self, name, vmid, pool, cores, mb, status, now, team=None):
        from worms_cli import services
        from worms_cli.vms import KINDS

        rnd = self.random
        running = status in ("running", "paused")
        is_trusted = trusted(name, pool)
        slug, label, role = (team[0], f"{team[1]} ({team[0]})", team[2]) if team else ("", "", "")
        units = {unit: "active" for s in services.SERVICES.values() if s.where == name for unit in s.up_units} if is_trusted else {}
        up = status == "running"
        vm = {
            "vmid": vmid, "name": name, "node": "worms-dev", "pool": pool, "kind": KINDS.get(pool, pool),
            "team": "(every team)" if name == "devbox-ssh-gateway" else label, "team_slug": slug,
            "team_handle": team[1] if team else "",
            "role": role, "group": group(name, pool), "status": status, "qmpstatus": status,
            "uptime": rnd.randint(3600, 18 * 86400) if running else 0,
            "cpu_pct": rnd.uniform(0.5, 30) if up else 0.0, "maxcpu": cores,
            "mem": int(mb * rnd.uniform(0.3, 0.98)) << 20 if running else 0, "maxmem": mb << 20,
            "net_in_bps": rnd.uniform(0, 4e4) if up else 0, "net_out_bps": rnd.uniform(0, 2e4) if up else 0,
            "disk_read_bps": rnd.uniform(0, 1e5) if up else 0, "disk_write_bps": rnd.uniform(0, 3e5) if up else 0,
            "lock": "", "task": None, "trusted": is_trusted,
            "psi_cpu": rnd.uniform(0, 30) if up else None, "psi_io": rnd.uniform(0, 8) if up else None,
            "psi_mem": 0.0 if up else None,
            "firewall": {"vm": True, "rules": True if running else None, "nics": [1, 1], "summary": "on"},
            "clean": True if pool in ("ephemeral", "private_internet") else None,
            "created": "11-03 ?" if pool != "devbox" else "11-19 ?",
            "updated": "11-21 b24c6b7" if pool != "devbox" else "11-20 f78d853",
            "updated_at": "2026-11-21T18:02:11", "held": False, "window_stopped": False, "services": services.on(name),
            "watch": None,
        }  # fmt: skip
        if is_trusted and running:
            vm["watch"] = {"load1": rnd.uniform(0, 1.5), "load5": rnd.uniform(0, 1), "load15": rnd.uniform(0, 1),
                           "mem_used_pct": rnd.uniform(20, 80), "cpu_pct": vm["cpu_pct"] / cores, "root_used_pct": rnd.uniform(15, 70),
                           "steal_pct": rnd.uniform(0, 4), "failed_units": 0,
                           "top": [[rnd.uniform(2, 30), TOP.get(name, "python3")], [rnd.uniform(0, 2), "systemd-journald"],
                                                      [rnd.uniform(0, 1), "sshd: debian"]],
                           "units": units, "at": now, "error": ""}  # fmt: skip
        return vm

    # -- time goes by --------------------------------------------------------------------------------------

    def _tick_forever(self):
        while not self.stopped.wait(self.interval):
            self.tick()

    def tick(self):
        now = time.time()
        rnd = self.random
        with self.lock:
            host = self.world["host"]
            host["cpu_pct"] = max(2.0, min(99.0, host["cpu_pct"] + rnd.uniform(-6, 6)))
            host["load"] = [max(0.1, host["load"][0] + rnd.uniform(-0.3, 0.3)), host["load"][1], host["load"][2]]
            host["mem_available"] = max(8 << 30, min(100 << 30, host["mem_available"] + rnd.randint(-1 << 29, 1 << 29)))
            host["psi_some_avg10"] = max(0.0, host["psi_some_avg10"] + rnd.uniform(-0.3, 0.3))
            if now - host["watchdog"]["last_run_at"] >= 15:
                host["watchdog"]["last_run_at"] = now  # it runs every 15 s
            host["uptime"] += self.interval
            for vm in self.world["vms"].values():
                if vm["status"] == "running":
                    vm["uptime"] += self.interval
                    vm["cpu_pct"] = max(0.1, min(100.0 * vm["maxcpu"], vm["cpu_pct"] + rnd.uniform(-5, 5)))
                    for key in ("net_in_bps", "net_out_bps", "disk_read_bps", "disk_write_bps"):
                        vm[key] = max(0.0, vm[key] * rnd.uniform(0.6, 1.5) + rnd.uniform(0, 500))
                watch = vm.get("watch")
                if watch and not watch.get("error"):
                    watch["at"] = now
                    watch["load1"] = max(0.0, watch["load1"] + rnd.uniform(-0.2, 0.2))
                    watch["top"][0][0] = max(0.5, min(99.0, watch["top"][0][0] + rnd.uniform(-5, 5)))
            for job in self.world["jobs"]:
                if job["state"] == "running":
                    job["heartbeat_at"] = now - rnd.uniform(0, 3)
            run = self.world["run"]
            if run and run["ends_at"] < now:
                run.update(attempt=run["attempt"] + 1, worm_start_at=now, ends_at=now + 900)
            for name in self.sources:
                # the database misses its answers for 15 s each minute: its records go dim, the header says it
                if name == "db" and 40 <= (now - self.started) % 60 < 55:
                    continue
                self.updated[name] = now
            self.version += 1

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()

    # -- the store's API, as state.py's ------------------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            snap = copy.deepcopy(self.world)
            snap["tasks"] = [copy.deepcopy(t) for t in sorted(self.tasks.values(), key=lambda t: -t["id"])]
            snap["log"] = list(self.log)
            snap["status"] = dict(self.status) if self.status else None
            sources = {name: {"interval": interval, "updated_at": self.updated[name], "error": ""}
                       for name, interval in self.sources.items()}  # fmt: skip
        snap["now"] = now
        for state in sources.values():
            state["age_s"] = now - state["updated_at"]
            state["stale"] = state["age_s"] > 3 * state["interval"]
        snap["sources"] = sources
        for vm in snap["vms"].values():
            if vm.get("watch"):
                vm["watch"]["age_s"] = now - vm["watch"]["at"]
                vm["watch"]["stale"] = vm["watch"]["age_s"] > 10
        snap["host"]["watchdog"]["last_run_age_s"] = now - snap["host"]["watchdog"]["last_run_at"]
        snap["services"] = self._services(snap)
        return snap

    def _services(self, snap):
        from worms_cli import services

        by_name = {vm["name"]: vm for vm in snap["vms"].values()}
        out = []
        for name, service in services.SERVICES.items():
            if service.where == "host":
                units = {unit: snap["host"]["units"].get(unit) for unit in service.up_units}
            else:
                vm = by_name.get(service.where) or {}
                seen = (vm.get("watch") or {}).get("units") or {}
                units = {unit: seen.get(unit) for unit in service.up_units}
                if vm.get("status") == "stopped":
                    units = {unit: "inactive" for unit in service.up_units}
            states = [s for s in units.values() if s is not None]
            if not units or len(states) < len(units):
                state = "unknown" if not states else "partial" if "active" in states else "down"
            else:
                state = "active" if all(s == "active" for s in states) else "partial" if "active" in states else "down"
            out.append({"name": name, "where": service.where, "about": service.about, "restartable": service.restartable,
                        "why_not": service.why_not, "units": units, "state": state})  # fmt: skip
        return out

    def _log(self, level, text):
        self.log.append({"at": time.time(), "level": level, "text": text})

    def set(self, name, value):
        """As Store.set: the devbox checks (checks.Checks) write theirs here."""
        with self.lock:
            self.world[name] = copy.deepcopy(value)
            self.version += 1

    def say(self, text, level="info"):
        with self.lock:
            self._log(level, text)
            self.status = {"at": time.time(), "level": level, "text": text}
            self.version += 1

    def note(self, text, level="info"):
        """As Store.log (its `log` here is the lines): the Log tab only."""
        with self.lock:
            self._log(level, text)
            self.version += 1

    def _add_task(self, title, words, targets, state="waiting", lines=(), rc=None, started=None, locks=False) -> dict:
        with self.lock:
            task = {"id": self.next_task, "title": title, "command": "worms " + shlex.join(map(str, words)),
                    "argv": [sys.executable, MANAGE, *words], "targets": list(targets), "locks": locks, "state": state,
                    "started": started, "ended": (started + 20) if rc is not None else None, "rc": rc, "lines": list(lines),
                    "paused": False, "cancel": False}  # fmt: skip
            self.tasks[task["id"]] = task
            self.next_task += 1
            self.version += 1
            return task


STAND_IN = r"""
import sys, time
w = sys.argv[1:]
follow = "-f" in w
print("(demo) nothing runs: this stands in for `worms " + " ".join(w) + "`", flush=True)
try:
    for i in range(1, 10**6 if follow else 6):
        print(f"{time.strftime('%H:%M:%S')}  made-up line {i}" + ("   (Ctrl-C ends it)" if follow and i == 1 else ""), flush=True)
        time.sleep(0.4)
except KeyboardInterrupt:
    print("^C")
    sys.exit(130)
"""


# -- the demo's checks: made up, in the shape of `worms check --json`'s `checks` (worms_cli/validate.py) -------------

DEMO_KEYS = {"t1": ["micha-pc 0a1b2c3d4e5f6071"], "t2": ["laptop 1f2e3d4c5b6a7980", "jonas-laptop 2b3c4d5e6f708192"],
             "t3": ["fabian 3c4d5e6f708192a3", "anna 4d5e6f708192a3b4"], "t4": ["delta-ci 5e6f708192a3b4c5"]}  # fmt: skip
DEMO_CHECK_SECONDS = 4.0  # how long a made-up check "runs"


def demo_check(world: dict, slugs: list) -> dict:
    """What `worms check <slugs> --json` gives as `checks`, made up from the demo's VMs: most of it fine, a few
    faults to see (alpha's B resolves nothing, bravo's C lacks a key, charlie's C has no flag, delta is held)."""
    from worms_cli.validate import CHECKS as ALL, GW_CHECKS, VM_CHECKS

    by_name = {vm["name"]: vm for vm in world["vms"].values()}
    out, stopped = [], []
    for slug in slugs or [slug for slug, _ in TEAMS]:
        team = (world.get("teams") or {}).get(slug) or {"name": "", "label": slug}
        names = DEMO_KEYS.get(slug, [])
        vms = {role: by_name.get(f"devbox-{slug}-{role}") for role in ("a", "b", "c", "gw")}
        cells: dict = {check: {} for check in ALL}
        for role, vm in vms.items():
            mine = (GW_CHECKS if role == "gw" else (*VM_CHECKS, "login")) + ("firewall",)
            for check in ALL:
                if vm is None or check not in mine:
                    cell = {"state": "n/a", "text": "—"}
                elif check == "firewall":  # the CLI's: the VM's options and groups, not its NICs or the host's rules
                    off = (vm.get("firewall") or {}).get("vm") is False
                    cell = ({"state": "error", "text": "fail", "reason": "the firewall is off (`worms vm rebuild <team>`)"} if off
                            else {"state": "ok", "text": "✓"})
                elif vm["status"] != "running":
                    cell = {"state": "stopped", "text": "off"}
                    stopped += [vm["name"]] if check == "login" else []
                else:
                    cell = {"state": "ok", "text": "✓", "from_vm": check != "login"}
                cells[check][role] = {"reason": "", "from_vm": False, **cell}
            login = cells["login"][role]
            if login["state"] == "ok":
                login.update(text=f"✓ {len(names)}/{len(names)}", keys=len(names), reached=True, taken=list(names), missing=[])
                if role == "a":
                    login.update(gateway_taken=list(names), gateway_missing=[], forwarded=True)

        def fault(check, role, state, text, reason, from_vm=True):
            if cells[check][role]["state"] == "ok":
                cells[check][role].update(state=state, text=text, reason=reason, from_vm=from_vm)

        notes, activity = [], []
        if slug == "t2":
            fault("DNS", "b", "error", "fail", "no answer for apt-cache pypi.worms.internal (the private DNS, via the gateway; "
                  "/etc/resolv.conf: nameserver 10.20.2.1)")  # fmt: skip
            fault("apt", "b", "error", "fail", "E: Failed to fetch http://apt-cache.worms.internal:3142/deb.debian.org/debian/dists/"
                  "trixie/InRelease  Temporary failure resolving 'apt-cache.worms.internal'")  # fmt: skip
            fault("PyPI", "b", "error", "fail", "index http://pypi.worms.internal/root/pypi/+simple/: ERROR: Could not find a "
                  "version that satisfies the requirement requests (from versions: none)")  # fmt: skip
            activity.append({"kind": "upload", "id": 17, "state": "ok", "text": "newest dev upload 17 (11-21 17:40): runs: ran "
                             "2026-11-21 17:46:02 to 2026-11-21 18:46:02 (`worms trace alpha`)"})  # fmt: skip
        if slug == "t3" and cells["login"]["c"]["state"] == "ok":
            cells["login"]["c"].update(state="error", text=f"1/{len(names)}", taken=names[:1], missing=names[1:],
                                       reason=f"takes 1 of the team's {len(names)} key(s) as root (`worms key sync`)")
            activity.append({"kind": "job", "id": 833, "state": "queued", "text": "job #833 devbox_reconfigure queued for 9 min: a "
                             "job runs ahead: #832 deploy (alpha (t2))"})  # fmt: skip
        if slug == "t1":
            fault("flag", "c", "error", "fail", "no /etc/worms/flag")
            fault("scoring", "a", "warn", "warn", "429: reached, but the team is over its rate limit right now")
            activity.append({"kind": "job", "id": 812, "state": "failed", "text": "job #812 devbox_rebuild failed 14:02: "
                             "ansible-playbook SetupDevbox.yml: exit 2 (UNREACHABLE: devbox-t1-gw) (`worms log 812`)"})  # fmt: skip
        gw = vms["gw"]
        if gw is not None and gw["status"] != "running":
            notes.append({"state": "error", "text": f"its NAT gateway {gw['name']} is {gw['status']}: its VMs reach no service"})
        out.append({"slug": slug, "name": team.get("name", ""), "label": team.get("label", slug),
                    "keys": {"registered": len(names), "invalid": 0, "names": list(names)},
                    "vms": {role: ({"name": vm["name"], "vmid": vm["vmid"], "status": vm["status"]} if vm else None)
                            for role, vm in vms.items()},
                    "checks": cells, "notes": notes, "activity": activity})  # fmt: skip
    gateway = {"name": "devbox-ssh-gateway", "status": "running",
               "lines": [{"state": "ok", "text": "OpenSSH's per-source penalties spare the teams' address 10.7.0.1"}]}
    problems = sum(1 for team in out for roles in team["checks"].values() for cell in roles.values() if cell["state"] == "error")
    problems += sum(1 for team in out for note in team["notes"] if note["state"] == "error")
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "ssh_gateway": gateway, "teams": out,
            "stopped": stopped, "problems": problems}  # fmt: skip


def demo_sandbox_check(vms: dict, now: float) -> dict:
    """The round's newest `check` job of the run that built the sandbox, as the database collector gives it: made up."""
    records = {}
    for vm in vms.values():
        if vm["pool"] != "sandbox":
            continue
        name, up = vm["name"], vm["status"] != "stopped"
        rows = [["running", up, f"status: {'running' if up else 'stopped'}"], ["no-kvm", True, "its CPU must be x86-64-v2-AES"]]
        if not up:
            rows.append(["probe", False, "no answer (the job log on the host says why)"])
        elif not name.endswith("-gw"):
            rows += [["probe", True, ""], ["flag", True, ""], ["dns", True, ""], ["scoring", True, ""], ["logging", True, ""],
                     ["apt", True, ""], ["pypi", name != "sb-db-01", "" if name != "sb-db-01" else "ERROR: Could not find a "
                      "version that satisfies the requirement requests (from versions: none)"],
                     ["time", True, "synchronised"], ["isolated", True, "none of proxmox, orchestrator, internet answered"],
                     ["firewall", True, "6 connections as its rules say"]]  # fmt: skip
        records[name] = rows
    return {"run": 33, "round": "Round 1", "round_id": 2, "team": "alpha (t2)", "attempt": 2,
            "prepare": {"id": 829, "state": "done", "finished_at": now - 420},
            "check": {"id": 830, "state": "done", "started_at": now - 400, "finished_at": now - 330, "ok": False, "error": "",
                      "vms": records, "host": [["host:worms-round-agent", True, "active"], ["sandbox:vms-registered", True, "8 VM(s)"]]}}  # fmt: skip


class DemoCheck:
    """--demo's runner for checks.Checks: made-up results after DEMO_CHECK_SECONDS; nothing runs."""

    def __init__(self, store):
        self.store, self.stopped = store, threading.Event()

    def command(self, slugs) -> str:
        return "worms check " + " ".join([*slugs, "--json"]) + "   (demo: made up)"

    def __call__(self, slugs):
        if self.stopped.wait(DEMO_CHECK_SECONDS):
            return None, "stopped"
        with self.store.lock:
            world = {"vms": copy.deepcopy(self.store.world["vms"]), "teams": copy.deepcopy(self.store.world["teams"])}
        return demo_check(world, slugs), ""

    def stop(self):
        self.stopped.set()


class Demo:
    """--demo's backend: DemoStore's records and actions.menu's own menus; their commands change those
    records and run nothing (a shell stands in for a login, made-up lines for the rest; a devbox check
    makes its results up)."""

    def __init__(self, system, args):
        self.dry_run, self.watchers, self.auto_checks = args.dry_run, not args.no_watchers, not args.no_auto_check
        self.store = DemoStore(system, args.interval)
        self.checks = checks.Checks(self.store, DemoCheck(self.store))

    def begin(self):
        self.store.start()
        self.checks.start()
        now = time.time()
        with self.store.lock:
            world = {"vms": copy.deepcopy(self.store.world["vms"]), "teams": copy.deepcopy(self.store.world["teams"])}
        self.checks.remember(demo_check(world, ["t1"]), now - 70, 48.0)  # charlie: a minute ago
        self.checks.remember(demo_check(world, ["t3"]), now - 540, 52.0)  # bravo: 9 min ago, older than 5 min

    def check(self, slugs, auto=False):
        return self.checks.want(slugs, auto=auto)

    def stderr(self):
        return sys.stderr

    def stop(self):
        self.checks.stop()
        self.store.stop()

    def snapshot(self):
        return self.store.snapshot()

    @property
    def version(self):
        return self.store.version

    def say(self, text, level="info"):
        self.store.say(text, level)

    def refresh(self):
        self.store.tick()

    def read_argv(self, words):
        script = ("import sys, time; time.sleep(0.6); w = sys.argv[1:]; print('(demo) worms ' + ' '.join(w)); "
                  "[print(f'{i:>3}  made-up line {i} of what `worms {\" \".join(w)}` shows') for i in range(1, 25)]")  # fmt: skip
        return [sys.executable, "-c", script, *words]

    # -- the menus: actions.menu's own, on the made-up records --------------------------------------------

    def menu(self, tab, items, snap):
        return actions.menu(tab, items, snap)

    # -- running it: nothing runs, the records change ---------------------------------------------------

    def start(self, action):
        if self.dry_run:
            self.store.say(f"dry run, not run: $ {action.command}", "warn")
            return None
        words = [str(w) for w in action.argv[2:]]
        task = self.store._add_task(action.title, words, action.targets, locks=actions.locked(action.argv))
        threading.Thread(target=self._play, args=(task, words), daemon=True).start()
        self.store.say(f"started #{task['id']}: $ {action.command}")
        return task["id"]

    def _play(self, task, words):
        store = self.store
        with store.lock:
            if task["cancel"]:
                task.update(state="cancelled", rc=None, ended=time.time())
                store.version += 1
                return
            task.update(state="running", started=time.time())
            task["lines"].append(f"(demo) $ worms {shlex.join(words)}")
            store.version += 1
        steps = [f"{words[0]} {words[1] if len(words) > 1 else ''}: {target} ..." for target in task["targets"] or ["(the platform)"]]
        steps += [f"{target}: done" for target in task["targets"] or ["(the platform)"]]
        fail = words[:2] == ["round", "start"]
        for line in steps:
            for _ in range(10):
                time.sleep(0.15)
                while task["paused"] and not task["cancel"]:
                    time.sleep(0.1)
                if task["cancel"]:
                    break
            if task["cancel"]:
                break
            with store.lock:
                task["lines"].append(line)
                store.version += 1
        with store.lock:
            store.version += 1
            if task["cancel"]:
                task.update(state="cancelled", rc=-2, ended=time.time())
                task["lines"].append("^C (cancelled)")
            elif fail:
                task.update(state="failed", rc=1, ended=time.time())
                task["lines"].append("error: the round starts at its start time; `worms round start` asks the conductor "
                                     "(demo: it fails)")
            else:
                task.update(state="done", rc=0, ended=time.time())
                self._effect(words)
        state = task["state"]
        store.say(f"#{task['id']} {'done' if state == 'done' else state.upper()}: {task['command']}",
                  "ok" if state == "done" else "warn" if state == "cancelled" else "error")  # fmt: skip

    def _effect(self, words):
        """What the command would have changed, in the made-up records."""
        world = self.store.world
        by_name = {vm["name"]: vm for vm in world["vms"].values()}
        if words[:1] == ["vm"] and words[1] in ("start", "stop", "restart"):
            for name in words[2:]:
                vm = by_name.get(name)
                if vm:
                    running = words[1] != "stop"
                    vm.update(status="running" if running else "stopped", qmpstatus="running" if running else "stopped", held=False,
                              uptime=0, cpu_pct=1.0 if running else 0.0, mem=(vm["maxmem"] // 3) if running else 0)  # fmt: skip
                    for key in ("net_in_bps", "net_out_bps", "disk_read_bps", "disk_write_bps"):
                        vm[key] = 0.0
                    if running and vm.get("parked_watch"):
                        vm["watch"] = vm.pop("parked_watch")  # a watcher again, as collect.py starts one
                    elif not running and vm.get("watch"):
                        vm["parked_watch"] = vm.pop("watch")
                    vm["firewall"]["rules"] = True if running else None
        elif words[:2] == ["vm", "kill"]:
            for vm in world["vms"].values():
                vm.update(status="stopped", qmpstatus="stopped", uptime=0)
                if vm.get("watch"):
                    vm["parked_watch"] = vm.pop("watch")
        elif words[:2] == ["jobs", "cancel"]:
            for job in world["jobs"]:
                if str(job["id"]) in words[2:]:
                    job.update(state="failed", error="cancelled by the operator (worms jobs cancel)", finished_at=time.time(),
                               reasons=[])
        elif words[:2] == ["jobs", "pause"]:
            reason = next((w.partition("=")[2] for w in words if w.startswith("--reason=")), "")
            world["host"]["agent_paused"] = "paused by the demo" + (f": {reason}" if reason else "")
        elif words[:2] == ["jobs", "resume"]:
            world["host"]["agent_paused"] = ""
        elif words[:1] == ["round"] and words[1] in ("pause", "resume"):
            for r in world["rounds"]:
                if str(r["id"]) == words[2]:
                    r["on_hold"] = words[1] == "pause"
        self.store.version += 1

    def foreground(self, chosen) -> int:
        """As actions.Runner.foreground (its on_terminal: the same key, the same way back after a login, the
        same hold of SIGHUP and SIGTERM): stand-ins on the terminal, one after the other."""
        if self.dry_run:
            for action in chosen:
                self.store.say(f"dry run, not run: $ {action.command}", "warn")
            return 0

        def log(text, level="info"):
            with self.store.lock:
                self.store._log(level, f"{text} (demo)")

        return actions.on_terminal(chosen, lambda action: self._stand_in([str(w) for w in action.argv[2:]]), log)

    def _stand_in(self, words) -> int:
        """What stands in for a command on the terminal: a shell for a login, made-up lines for the rest."""
        if words[:1] == ["login"]:
            print(f"(demo) a shell stands in for the VM {words[1]}: `exit` (or Ctrl-D) comes back to worms-gui", flush=True)
            code = actions._attached(["sh", "-i"], {**os.environ, "PS1": f"(demo) debian@{words[1]}$ "})
        else:
            code = actions._attached([sys.executable, "-c", STAND_IN, *words], dict(os.environ))
        if code == 0 and words[:1] == ["internet"]:
            self.store.world["host"]["internet"] = "allowed" if words[1] == "on" else "denied"
            self.store.version += 1
        if code == 0 and words[:2] == ["vm", "kill"]:
            with self.store.lock:
                self._effect(words)
        return code

    def task_signal(self, task_id, what):
        task = self.store.tasks.get(task_id)
        with self.store.lock:
            if task is None or task["state"] not in ("waiting", "running", "paused") or task.get("ended"):
                return "it does not run"
            if what == "pause" and task["state"] == "paused":
                return "paused already: P resumes it"
            if what == "resume" and task["state"] != "paused":
                return "it runs: not paused"
            self.store.version += 1
            if what == "cancel":
                task["cancel"] = True
            elif what == "pause":
                task.update(paused=True, state="paused")
            elif what == "resume":
                task.update(paused=False, state="running")
        return ""
