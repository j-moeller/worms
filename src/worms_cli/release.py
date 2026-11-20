"""Changing the platform safely: one change at a time, a plan that tells the truth, a way back.

    the lock       every `worms` that changes the platform (apply, configure, rebuild, reset, start,
                   stop, restart, open/close window, refill, destroy, sync keys, step) holds
                   /run/worms/worms.lock on the node while it runs; a second one is refused, saying
                   who holds it. Never `emergency-stop`, `pause`, `resume`, `cancel`: those must work
                   while a build runs. On 2026-11-18 a second `worms` closed the internet window under
                   a running build, and its apt hung for 1 h 40 min.
    worms plan     besides the steps (components.Plan.touches): what each VM runs now (the VM
                   history's commit, worms_cli/history.py) against this checkout, the migrations the
                   database lacks, and OpenTofu's own plan for the stacks and VMs concerned: on
                   production, before anything changes, what it would really do there.
    worms restore db <dump>
                   the database back to a dump (`worms backup db`): the dashboard, the orchestrator's
                   services and the round agent stopped meanwhile (they hold connections), then
                   started again. Everything written since the dump is lost.

A release, then: `worms plan` (here on dev, then on prod: the two should match), `worms backup db`,
`worms apply`/`configure`, `worms check`. The way back: the previous commit checked out, `worms
configure` again, and if a migration has to go back, `worms restore db` of the dump taken first.
"""

from __future__ import annotations

import fcntl
import getpass
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone

from worms_cli import output
from worms_cli.build import BuildError

LOCK = "/run/worms/worms.lock"


class Lock:
    """The node's lock for changing the platform, held for this process's life (release() ends it).
    On the node: flock(2) on LOCK. From a laptop: `flock` on the node over SSH, held while its
    stdin is open."""

    def __init__(self, session, what: str):
        self.session, self.what = session, what
        self.fd = None
        self.proc = None

    def info(self) -> str:
        return f"pid {os.getpid()} · {getpass.getuser()} · worms {self.what} · since {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC"

    def holder(self) -> str:
        code, out, _err = self.session.node_run(["cat", f"{LOCK}.info"])
        return out.strip() if code == 0 and out.strip() else "another worms"

    def acquire(self) -> "Lock":
        if self.session.laptop:
            command = (f"mkdir -p /run/worms && exec flock -n {LOCK} sh -c "
                       + shlex.quote(f"printf '%s\\n' {shlex.quote(self.info())} > {LOCK}.info; echo locked; cat >/dev/null"))
            self.proc = subprocess.Popen(self.session.ssh_argv(command), env=self.session.env(), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # fmt: skip
            if self.proc.stdout.readline().strip() != b"locked":
                self.proc.wait()
                self.proc = None
                raise BuildError(self._refused())
            return self
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        self.fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise BuildError(self._refused()) from None
        with open(f"{LOCK}.info", "w") as out:
            out.write(self.info() + "\n")
        return self

    def _refused(self) -> str:
        return (f"another worms is changing the platform: {self.holder()}. Wait for it (`worms logs run -f` where it "
                "runs), or stop it there; `worms emergency-stop`, `pause`, `resume` and `cancel` work meanwhile")  # fmt: skip

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001  (the lock goes with the SSH connection anyway)
                self.proc.kill()
            self.proc = None


# -- worms plan: what it would really do --------------------------------------------------------


def deployed_commits(pipe, names) -> list[list[str]]:
    """Per VM of the plan: the commit its last configuration came from (the VM history), this
    checkout's, and how far apart they are in git."""
    from worms_cli import history

    here = pipe.runner.commit()
    code, out, _err = pipe.runner.session.node_run(["cat", history.LEDGER])
    events = history.parse(out) if code == 0 else []
    last = {}
    for event in events:
        if event["event"] in ("configured", "packages", *history.CREATED):
            last[event["name"]] = event["commit"]
    rows = []
    for name in names:
        commit = last.get(name, "")
        if not commit:
            rows.append([name, "unknown (no history)", here, ""])
            continue
        base = commit.removesuffix("-dirty")
        behind = subprocess.run(["git", "-C", str(pipe.runner.repo_root), "rev-list", "--count", f"{base}..HEAD"],
                                capture_output=True, text=True)  # fmt: skip
        gap = f"{behind.stdout.strip()} commit(s) behind" if behind.returncode == 0 else "not in this checkout's history"
        rows.append([name, commit, here, "the same" if commit == here else gap])
    return rows


def tofu_plans(pipe, plan) -> None:
    """OpenTofu's own plan, per stack the plan concerns (a VM component: -target its module),
    summed up: what it would add, change, destroy; nothing is applied."""
    from worms_cli import components

    targets: dict[str, list[str]] = {}
    for name, _action in plan.steps:
        component = components.COMPONENTS[name]
        if component.kind == "stack":
            targets.setdefault(component.stack, [])
        elif component.kind == "vm":
            targets.setdefault(component.stack, []).append(f"-target={component.module}")
    from worms_cli.session import NODE_STACKS

    for stack, args in targets.items():
        if stack in NODE_STACKS:
            print(output.color(f"OpenTofu: {stack} is the round agent's (planned and applied on the node, per team)", "dim"))
            continue
        label = stack + (f" ({len(args)} VM(s))" if len(args) > 2 else f" ({', '.join(a.split('=', 1)[1] for a in args)})" if args else "")
        print(output.color(f"OpenTofu plan of {label} ...", "dim"), flush=True)
        command = shlex.join(["tofu", "plan", "-input=false", "-no-color", "-lock=false", *args])
        try:
            env = pipe.runner.session.tofu_env()
        except Exception as exc:  # noqa: BLE001  (no token: the plan of the steps stands)
            print(output.status_cell("warn", f"{stack}: not planned ({exc})"))
            continue
        proc = subprocess.run(["sh", "-c", command], cwd=f"{pipe.runner.repo_root}/opentofu/stacks/{stack}", env=env,
                              capture_output=True, text=True)  # fmt: skip
        lines = proc.stdout.splitlines()
        changes = [line.strip() for line in lines if line.strip().startswith("# ") and (" will be " in line or " must be " in line)]
        summary = next((line for line in lines if line.startswith(("Plan:", "No changes."))), "")
        if proc.returncode != 0:
            last = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["?"]
            print(output.status_cell("error", f"{stack}: tofu plan failed: {last[0][:200]}"))
            continue
        print(output.status_cell("ok" if summary.startswith("No changes") else "warn", f"{stack}: {summary or 'no plan line'}"))
        for change in changes[:25]:
            print(output.color(f"    {change}", "dim"))
        if len(changes) > 25:
            print(output.color(f"    ... and {len(changes) - 25} more", "dim"))


# -- worms restore db ------------------------------------------------------------------------------


def restore_db(config, path: str) -> int:
    """The database back to the dump at `path`: who holds connections stopped meanwhile, then started."""
    from worms_cli.services import SERVICES
    from worms_cli.session import SessionError, group_var
    from worms_cli.vms import Catalogue

    with open(path, "rb") as dump:
        if dump.read(5) != b"PGDMP":
            raise BuildError(f"{path} is no pg_dump archive (`worms backup db` makes one)")
    catalogue = Catalogue(config)
    session = catalogue.runner.session
    if any(vm.get("pool") == "sandbox" for vm in catalogue.vms()):
        raise BuildError("refusing during a round: sandbox VMs exist")
    try:
        name = group_var(config.repo_root, "postgres_db")
    except SessionError as exc:
        raise BuildError(str(exc)) from None
    holders = ["agent", "processor", "conductor", "api", "dashboard"]

    def units(action: str, names) -> None:
        for service_name in names:
            service = SERVICES[service_name]
            command = f"systemctl {action} {' '.join(service.units)}"
            if service.where == "node":
                code, _out, err = session.node_run(["sh", "-c", command])
            else:
                vm = catalogue.resolve(service.where)
                argv, env = catalogue.ssh_argv(vm, f"sudo -n {command}", interactive=False)
                proc = subprocess.run(argv, env=env, capture_output=True, text=True)
                code, err = proc.returncode, proc.stderr
            print(output.status_cell("ok" if code == 0 else "error", f"{service_name}: {action}" + (f" ({err.strip()[:120]})" if code else "")),
                  flush=True)  # fmt: skip

    vm = catalogue.resolve("database")
    units("stop", holders)
    try:
        argv, env = catalogue.ssh_argv(vm, f"cd / && sudo -n -u postgres pg_restore --clean --if-exists --single-transaction "
                                           f"-d {shlex.quote(name)}", interactive=False)  # fmt: skip
        with open(path, "rb") as dump:
            proc = subprocess.run(argv, env=env, stdin=dump, capture_output=True)
        if proc.returncode != 0:
            raise BuildError(f"pg_restore failed (nothing changed: one transaction): {proc.stderr.decode(errors='replace').strip()[-400:]}")
        print(output.status_cell("ok", f"{name}: restored from {path}"))
    finally:
        units("start", list(reversed(holders)))
    print(output.color("The grants are the dump's; this checkout's: `worms configure database`.", "dim"))
    return 0



# -- worms release / worms rollback: a record of each release, and the way back ---------------------

RELEASES = "~/.worms/releases"  # one JSON file per release, newest last (0600, directory 0700)


def _releases_dir():
    from pathlib import Path

    directory = Path(RELEASES).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def start_record(pipe, components: list[str], vms: list[str], dump: str) -> dict:
    """Before a release changes anything: what each VM runs now, this checkout, the dump."""
    import json

    previous = {row[0]: row[1] for row in deployed_commits(pipe, vms)}
    counts: dict[str, int] = {}
    for commit in previous.values():
        if commit and not commit.startswith("unknown"):
            counts[commit] = counts.get(commit, 0) + 1
    record = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "by": getpass.getuser(),
        "components": components,
        "from": max(counts, key=counts.get) if counts else "",  # what most of them ran
        "from_per_vm": previous,
        "to": pipe.runner.commit(),
        "dump": dump,
        "configure": None,
        "check": None,
    }
    path = _releases_dir() / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(record, out, indent=2)
    record["_path"] = str(path)
    return record


def finish_record(record: dict, configure: int, check: int | None) -> None:
    import json

    record["configure"], record["check"] = configure, check
    path = record.pop("_path")
    with open(path, "w") as out:
        json.dump(record, out, indent=2)
    record["_path"] = path


def newest_record() -> dict:
    import json

    files = sorted(_releases_dir().glob("*.json"))
    if not files:
        raise BuildError("no release recorded here (`worms release` records each one in ~/.worms/releases/)")
    record = json.loads(files[-1].read_text())
    record["_path"] = str(files[-1])
    return record


def describe(record: dict) -> list[list[str]]:
    return [
        ["release", f"{record['time']} by {record.get('by', '?')} ({record['_path']})"],
        ["components", ", ".join(record["components"]) or "every one that exists"],
        ["from", record["from"] or "unknown"],
        ["to", record["to"]],
        ["database dump", record["dump"]],
        ["configure", "—" if record["configure"] is None else "ok" if record["configure"] == 0 else f"failed (exit {record['configure']})"],
        ["check", "—" if record["check"] is None else "ok" if record["check"] == 0 else f"problems (exit {record['check']})"],
    ]


def rollback_code(config, record: dict) -> None:
    """The checkout back to the commit most VMs ran before the release, then that commit's own CLI
    configures the release's components (this process is replaced: its code and that commit's
    never mix). Refused when the checkout has uncommitted changes, or the commit was dirty."""
    repo = str(config.repo_root)
    target = record["from"]
    if not target or target.endswith("-dirty"):
        raise BuildError(f"the release came from {target or 'an unknown commit'}: no commit to go back to "
                         "(a -dirty one had uncommitted changes); check one out yourself and `worms configure`")  # fmt: skip
    dirty = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True)
    if dirty.stdout.strip():
        raise BuildError(f"this checkout has uncommitted changes ({len(dirty.stdout.splitlines())} file(s)): commit or "
                         "stash them first, nothing was changed")  # fmt: skip
    if subprocess.run(["git", "-C", repo, "checkout", "--detach", target], capture_output=True).returncode != 0:
        raise BuildError(f"git cannot check out {target} here")
    print(output.status_cell("warn", f"the checkout is at {target} now (detached; `git -C {repo} checkout main` to come back)"),
          flush=True)  # fmt: skip
    argv = [sys.executable, os.path.join(repo, "src", "manage.py"), "configure", *record["components"]]
    print(output.color(f"$ {' '.join(argv[1:])}  [that commit's own CLI]", "dim"), flush=True)
    os.execv(sys.executable, argv)
