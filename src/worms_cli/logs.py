"""`worms log`: every log of the platform from one place (docs/03-manage-py.md).

    worms log                  the sources, and the newest host jobs
    worms log 1782             a host job's log, by its number (`worms jobs`)
    worms log conductor        a service's log, on whichever VM it runs (services.SERVICES)
    worms log orchestrator     ... and any platform VM by its name: its whole journal
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
            raise BuildError(f"no host job #{number}: `worms jobs` lists them")
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


def run_log(nth: int, lines: int, follow: bool, vm: str | None = None) -> int:
    """`worms log run [N] [<vm>]`: the N-th newest run log of this machine (worms_cli/runlog.py), or
    one VM's own Ansible log of that run (ansible/callback_plugins/worms.py)."""
    from worms_cli import runlog

    try:
        path = runlog.nth(nth)
    except LookupError as exc:
        raise BuildError(f"no such run log: {exc}") from None
    per_vm = runlog.vm_logs(path)
    found = sorted(per_vm.glob("*.log")) if per_vm.is_dir() else []
    if vm:
        wanted = per_vm / f"{vm.removesuffix('.worms.internal')}.log"
        if wanted not in found:
            raise BuildError(f"run #{nth} has no Ansible log of {vm!r}" + (f": {', '.join(f.stem for f in found)}" if found else ""))
        path = wanted
        print(output.color(f"run #{nth}, {vm}: {path}", "dim"), flush=True)
    else:
        print(output.color(f"run log #{nth}: {path}", "dim"), flush=True)
        if found:
            print(output.color(f"each VM's own Ansible log (`worms log run {nth} <vm>`): {', '.join(f.stem for f in found)}", "dim"),
                  flush=True)  # fmt: skip
    argv = ["tail", "-n", str(int(lines)), *(["-F"] if follow else []), str(path)]
    os.execvp(argv[0], argv)
    return 0  # not reached


def overview() -> None:
    from worms_cli import queries, runlog

    newest = runlog.logs()[:5]
    if newest:
        print(output.heading("run logs here: worms log run [N]"))
        print(output.table([[str(n), path.name] for n, path in enumerate(newest, 1)], headers=["N", "file"]))
        print(output.color(f"in {runlog.directory()}: every run of a command that changes something", "dim"))
        print()
    print(output.heading("logs: worms log <name>"))
    print(output.table([[name, s.where, s.about] for name, s in SERVICES.items()], headers=["name", "on", "what"]))
    print(output.color("Any platform VM by its name (`worms log orchestrator`, `worms log apt-cache`, `worms log node`; a name above comes first): "
                       "its whole journal. -f follows, -n lines, --since a time.", "dim"))  # fmt: skip
    print()
    print(output.heading("host jobs: worms log <#>, worms log latest [-f]"))
    print(output.table(queries.job_rows(10), headers=["#", "Job", "State", "Team", "Requested", "Finished", "Error"]))


def team(config, name: str, lines: int = 20, since: str = "2 days ago") -> int:
    """`worms log <team>`: everything about one team, from where it is: its host jobs (and the newest
    one's log), the attempt processor's lines about its dev uploads, and what scoring and logging
    refused from its devbox gateway's address (they log no accepted request: nginx access_log off)."""
    import subprocess

    from core.models.devbox import Devbox
    from core.models.rounds import HostJob
    from core.models.worms import Upload
    from django.db.models import Q

    from worms_cli import queries
    from worms_cli.vms import Catalogue

    found = queries.team(name)
    box = Devbox.objects.filter(team=found).first()
    address = box.services_source_ip if box else ""
    catalogue = Catalogue(config)
    print(output.heading(f"{found.name}: its devbox's address towards the services {address or '(none)'}"))

    jobs = list(HostJob.objects.select_related("run__team").filter(Q(params__team_uuid=str(found.uuid)) | Q(run__team=found))
                .order_by("-id")[:10])  # fmt: skip
    print(output.heading("host jobs, newest first (`worms show job <n>`, `worms log <n> [-f]`)"))
    if jobs:
        print(output.table([[str(j.id), j.kind, j.state, f"{j.requested_at:%m-%d %H:%M}", f"{j.finished_at:%H:%M}" if j.finished_at else "",
                             (j.error or "").replace("\n", " ")[:90]] for j in jobs],
                           headers=["#", "job", "state", "requested", "finished", "error"]))  # fmt: skip
    else:
        print(output.color("none", "dim"))

    def journal(vm_name: str, unit: str, pattern: str, fixed: bool) -> str:
        vm = next((vm for vm in catalogue.vms() if vm["name"] == vm_name), None)
        if vm is None or vm.get("status") != "running":
            return f"({vm_name} is not running)"
        grep = f"grep {'-F ' if fixed else '-E '}{shlex.quote(pattern)}"
        command = f"sudo journalctl --no-pager -u {shlex.quote(unit)} --since {shlex.quote(since)} | {grep} | tail -n {int(lines)}"
        try:
            argv, env = catalogue.ssh_argv(vm, command, interactive=False)
            proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        except Exception as exc:  # noqa: BLE001  (one source out of reach: the others still)
            return f"(not read: {exc})"
        return proc.stdout.rstrip() or "(nothing)"

    uploads = list(Upload.objects.filter(team=found, devbox=True).order_by("-id").values_list("id", flat=True)[:20])
    print()
    print(output.heading(f"the attempt processor about its dev uploads ({since}; `worms trace {name}`)"))
    print(journal("orchestrator", "orchestrator-process-attempts", rf"upload ({'|'.join(map(str, uploads))})\b", False)
          if uploads else output.color("no dev upload", "dim"))  # fmt: skip
    for service in ("scoring", "logging"):
        print()
        print(output.heading(f"{service}: what it refused from {address or 'its gateway'} ({since})"))
        print(journal(service, "sandbox-api", address, True) if address else output.color("no devbox address", "dim"))
    return 0
