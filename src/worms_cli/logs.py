"""`worms log`: every log of the platform from one place (docs/03-manage-py.md).

    worms log                 the sources, and the newest host jobs
    worms log 1782            a host job's log, by its number (`worms status jobs`)
    worms log conductor       a service's log, on whichever VM it runs (services.SERVICES)
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
from worms_cli.services import SERVICES  # where each service runs, its units, its log

JOB_LOGS = "/var/log/worms/jobs"  # scripts/round_agent/executor.py, JobLog: <job uuid>.log, kept 30 days
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

    def latest(self, lines: int, follow: bool) -> int:
        """`worms log latest`: the newest host job's log, without looking up its number (the agent
        points latest.log at each job it begins: -f moves on to the next one)."""
        print(output.color(f"the newest host job · {JOB_LOGS}/latest.log", "dim"), flush=True)
        return self._on_node(f"tail -n {int(lines)}{' -F' if follow else ''} {JOB_LOGS}/latest.log")

    def source(self, name: str, lines: int, follow: bool, since: str | None) -> int:
        service = SERVICES[name]
        if service.where == "node":
            return self._on_node(_command(service.log, lines, follow, since, root=True))
        return self._on_vm(service.where, _command(service.log, lines, follow, since, root=False))

    def vm(self, name: str, lines: int, follow: bool, since: str | None) -> int:
        if name == "node":
            return self._on_node(_command(None, lines, follow, since, root=True))
        return self._on_vm(name, _command(None, lines, follow, since, root=False))


def run_log(nth: int, lines: int, follow: bool) -> int:
    """`worms log build [N]`: the N-th newest run log of this machine (worms_cli/runlog.py)."""
    from worms_cli import runlog

    try:
        path = runlog.nth(nth)
    except LookupError as exc:
        raise BuildError(f"no such run log: {exc}") from None
    print(output.color(f"run log #{nth}: {path}", "dim"), flush=True)
    argv = ["tail", "-n", str(int(lines)), *(["-F"] if follow else []), str(path)]
    os.execvp(argv[0], argv)
    return 0  # not reached


def overview() -> None:
    from worms_cli import queries, runlog

    newest = runlog.logs()[:5]
    if newest:
        print(output.heading("run logs here: worms log build [N]"))
        print(output.table([[str(n), path.name] for n, path in enumerate(newest, 1)], headers=["N", "file"]))
        print(output.color(f"in {runlog.directory()}: every build, deploy, reset, internet, keys, step, restart, power run", "dim"))
        print()
    print(output.heading("logs: worms log <name>"))
    print(output.table([[name, s.where, s.about] for name, s in SERVICES.items()], headers=["name", "on", "what"]))
    print(output.color("Any platform VM by its name (`worms log orchestrator`, `worms log apt-cache`, `worms log node`; a name above comes first): "
                       "its whole journal. -f follows, -n lines, --since a time.", "dim"))  # fmt: skip
    print()
    print(output.heading("host jobs: worms log <#>, worms log latest [-f]"))
    print(output.table(queries.job_rows(10), headers=["#", "Job", "State", "Team", "Requested", "Finished", "Error"]))
