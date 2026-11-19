"""`worms log`: every log of the platform from one place (docs/03-manage-py.md).

    worms log                 the sources, and the newest host jobs
    worms log 1782            a host job's log, by its number (`worms status jobs`)
    worms log conductor       a service's log, on whichever VM it runs (SOURCES)
    worms log orchestrator    ... and any platform VM by its name: its whole journal
    -f follow · -n lines · --since (journals: "1 hour ago", "2026-11-19 18:00")

It reads them where they are, as `worms login` gets there: the node (here, or over SSH from a
laptop) and the platform's VMs over their management SSH, the WORMS key never forwarded. Not the
team VMs (devboxes, sandboxes): they are assumed rooted, their journals are the teams'.
"""

from __future__ import annotations

import os
import shlex

from worms_cli import output
from worms_cli.build import BuildError

JOB_LOGS = "/var/log/worms/jobs"  # scripts/round_agent/executor.py, JobLog: <job uuid>.log, kept 30 days

# name -> (where, what it holds, journal units | a file glob)
SOURCES = {
    "agent": ("node", "the round agent: each host job started, done or failed", ["worms-round-agent"]),
    "firewall": ("node", "the host firewall, its watchdog and the traffic mirror", ["worms-host-firewall", "worms-firewall-watchdog", "worms-mirror"]),
    "conductor": ("orchestrator", "rounds and their attempts; the gameplan checks (why one is invalid)", ["orchestrator-conduct-rounds"]),
    "processor": ("orchestrator", "captures and worm logs counted; dev uploads run", ["orchestrator-process-attempts"]),
    "api": ("orchestrator", "the orchestrator's API: simulations, devbox key pushes", ["orchestrator"]),
    "dashboard": ("dashboard", "the website: errors and tracebacks", ["dashboard"]),
    "scoring": ("scoring", "flag submissions (the sandbox API)", ["sandbox-api"]),
    "logging": ("logging", "the worms' log records (the sandbox API)", ["sandbox-api"]),
    "database": ("database", "PostgreSQL", "/var/log/postgresql/postgresql-*-main.log"),
    "monitor": ("monitor", "Zeek and its shipper (connections into the database)", ["zeek", "zeek-shipper"]),
    "audit": ("syslog", "every platform VM's audit trail (auditd), one file per sender address", "/var/log/remote/*/audit.log"),
}  # fmt: skip
UNTRUSTED_POOLS = ("sandbox", "devbox")


def _command(source, lines: int, follow: bool, since: str | None, root: bool) -> str:
    """The shell command that shows `source` (units, a file glob, or None: the whole journal)."""
    sudo = "" if root else "sudo "
    if isinstance(source, str):
        tail = f"tail -n {int(lines)}{' -F' if follow else ''} {source}"  # the glob is ours, expanded as root
        return f"{sudo}sh -c {shlex.quote(tail)}"
    argv = ["journalctl", "--no-pager", "-n", str(int(lines)), *(["-f"] if follow else []), *(["--since", since] if since else [])]
    for unit in source or []:
        argv += ["-u", unit]
    return sudo + shlex.join(argv)


class Logs:
    def __init__(self, config):
        from worms_cli.vms import Catalogue

        self.config = config
        self.catalogue = Catalogue(config)
        self.session = self.catalogue.runner.session

    def _on_node(self, command: str) -> int:
        """Run `command` on the node, its output straight to this terminal (here: no SSH)."""
        if self.session.laptop:
            argv, env = self.session.ssh_argv(command), self.session.env()
        else:
            argv, env = ["sh", "-c", command], dict(os.environ)
        os.execvpe(argv[0], argv, env)
        return 0  # not reached

    def _on_vm(self, name: str, command: str) -> int:
        vm = self.catalogue.resolve(name)
        if vm.get("pool") in UNTRUSTED_POOLS:
            raise BuildError(f"{vm['name']} is a team VM: its journal is the team's (`worms login {name}` to look anyway)")
        if vm.get("status") != "running":
            raise BuildError(f"{vm['name']} is {vm.get('status')}: start it first (`qm start {vm['vmid']}` on the node)")
        argv, env = self.catalogue.ssh_argv(vm, command, interactive=False)
        os.execvpe(argv[0], argv, env)
        return 0  # not reached

    def job(self, number: int, lines: int, follow: bool) -> int:
        from core.models.rounds import HostJob

        job = HostJob.objects.filter(pk=number).select_related("run__team").first()
        if job is None:
            raise BuildError(f"no host job #{number}: `worms status jobs` lists them")
        who = job.run.team.name if job.run else (job.params or {}).get("team_uuid", "")[:8]
        print(output.color(f"#{job.pk} {job.kind} {job.state} {who} · {JOB_LOGS}/{job.uuid}.log", "dim"), flush=True)
        if job.state == HostJob.State.QUEUED and not follow:
            print("Queued: the round agent has not begun it, so there is no log yet (-f waits for it).")
            return 0
        command = f"tail -n {int(lines)}{' -F' if follow else ''} {shlex.quote(f'{JOB_LOGS}/{job.uuid}.log')}"
        return self._on_node(command)

    def source(self, name: str, lines: int, follow: bool, since: str | None) -> int:
        where, _, what = SOURCES[name]
        if where == "node":
            return self._on_node(_command(what, lines, follow, since, root=True))
        return self._on_vm(where, _command(what, lines, follow, since, root=False))

    def vm(self, name: str, lines: int, follow: bool, since: str | None) -> int:
        if name == "node":
            return self._on_node(_command(None, lines, follow, since, root=True))
        return self._on_vm(name, _command(None, lines, follow, since, root=False))


def overview() -> None:
    from worms_cli import queries

    print(output.heading("logs: worms log <name>"))
    print(output.table([[name, where, what] for name, (where, what, _) in SOURCES.items()], headers=["name", "on", "what"]))
    print(output.color("Any platform VM by its name (`worms log orchestrator`, `worms log apt-cache`, `worms log node`; a name above comes first): "
                       "its whole journal. -f follows, -n lines, --since a time.", "dim"))  # fmt: skip
    print()
    print(output.heading("host jobs: worms log <#>"))
    print(output.table(queries.job_rows(10), headers=["#", "Job", "State", "Team", "Requested", "Finished", "Error"]))
