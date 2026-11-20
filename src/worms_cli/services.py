"""The platform's services, in one registry: where each runs, its systemd units, its log, and
whether `worms restart` may restart it. `worms logs <name>`, `worms restart <name>` and `worms show
<vm>` read it (docs/03-manage-py.md).

    worms restart conductor           its units, on the orchestrator
    worms restart agent conductor     always the agent first: it waits until no host job runs

The round agent is restarted only when no host job is RUNNING (the database says so): systemd
stops a service within its stop timeout and then kills it, which would end a sandbox build
halfway. It waits for that (`--timeout`), and refuses once it has waited that long. A job claimed
in the second between that check and the restart finishes on SIGTERM (agent.py, main) as long as
systemd lets it.

The conductor checks the gameplans when it starts, against the node: after the agent, then.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from worms_cli import output
from worms_cli.build import BuildError


@dataclass(frozen=True)
class Service:
    where: str  # "node" or the VM it runs on
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
        """What `worms logs <name>` shows: a file glob, or the units' journal."""
        return self.files or list(self.units)


# In restart order: the agent before the conductor (it checks the gameplans against the node when
# it starts), the orchestrator's before the website and the sandboxes' APIs.
SERVICES: dict[str, Service] = {
    "agent": Service("node", "the round agent: each host job started, done or failed", ("worms-round-agent",)),
    "firewall": Service("node", "the host firewall, its watchdog and the traffic mirror",
                        ("worms-host-firewall", "worms-firewall-watchdog", "worms-mirror"), restartable=False,
                        why_not="the node's firewall: `worms apply host-network` applies it again",
                        active=("worms-host-firewall", "worms-firewall-watchdog.timer", "worms-mirror")),
    "processor": Service("orchestrator", "captures and worm logs counted; dev uploads run", ("orchestrator-process-attempts",)),
    "api": Service("orchestrator", "the orchestrator's API: simulations, devbox key pushes", ("orchestrator",)),
    "conductor": Service("orchestrator", "rounds and their attempts; the gameplan checks (why one is invalid)",
                         ("orchestrator-conduct-rounds",)),
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
            raise ValueError(f"{name} is not restarted by `worms restart`: {SERVICES[name].why_not}")
    return [name for name in SERVICES if name in set(names)]


def running_jobs() -> list:
    """The host jobs the round agent is running now (the database's word). A database that
    does not answer is a BuildError: without its word the agent is not restarted."""
    from core.models.rounds import HostJob
    from django.db import DatabaseError

    try:
        return list(HostJob.objects.filter(state=HostJob.State.RUNNING).order_by("id"))
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
                "at its stop timeout). Try again once it is done (`worms show jobs`, `worms logs latest -f`), or wait "
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
        if service.where == "node":
            print(output.color(f"restarting {name} on the node: {units}", "dim"), flush=True)
            self.runner.node_sh(f"systemctl restart {units}")
            if self.runner.node_sh(f"systemctl is-active --quiet {units}", check=False, quiet=True) != 0:
                raise BuildError(f"{name} is not active after its restart: `worms logs {name}`")
            return
        vm = self.catalogue.resolve(service.where)
        if vm.get("status") != "running":
            raise BuildError(f"{vm['name']} is {vm.get('status')}: {name} runs there (`worms start {vm['name']}`)")
        print(output.color(f"restarting {name} on {vm['name']}: {units}", "dim"), flush=True)
        argv, env = self.catalogue.ssh_argv(vm, f"sudo -n systemctl restart {units} && sudo -n systemctl is-active --quiet {units}",
                                            interactive=False)  # fmt: skip
        proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise BuildError(f"{name} on {vm['name']}: restart failed or not active after it (exit {proc.returncode}: "
                             f"{proc.stderr.strip()[-200:]}): `worms logs {name}`")  # fmt: skip
