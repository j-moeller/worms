"""`worms upgrade <bundle|rev> [--resume] [--plan]`: one command that moves a system to a commit, in
the one order that works, and picks up where it stopped (docs/03-manage-py.md, "From dev to
production"). It only composes commands that exist:

    1 preflight     no sandbox, no host job, no unfinished build, the window shut, the checkout
                    clean, the bundle sound, the target a fast-forward of this checkout
    2 backup        `worms db backup`, and a tar of the git-ignored state (the vault, its key,
                    the PKI, every OpenTofu state, the host's too) in ~/.worms/backups/
    3 code          `git pull --ff-only <bundle> main`, or `git merge --ff-only <rev>`: never a
                    merge; then the rest runs with the new code (this command again, --resume)
    4 orchestrator  `worms apply orchestrator`: the migrations first, the CLI's models are new
    5 apply         `worms apply`: what changed, with its own rules (packages, the window)
    6 round-agent   `worms apply round-agent gameplans`: the host's workspace
    7 verify        `worms status`: `system: on <target>`, and no error in its attention block

Each step done is recorded in /var/lib/worms/upgrade.json ({target, started, steps_done,
finished}, rewritten whole); `--resume` goes on after the last. Every `worms apply` asks its own
questions in this terminal (`--yes` answers them). The end says the way back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from worms_cli import output
from worms_cli.build import BuildError

PROGRESS = "/var/lib/worms/upgrade.json"
STEPS = [
    ("preflight", "nothing in the way"),
    ("backup", "the database dumped, the git-ignored state kept"),
    ("code", "the checkout at the target (a fast-forward)"),
    ("orchestrator", "`worms apply orchestrator`: the migrations first"),
    ("apply", "`worms apply`: what changed"),
    ("round-agent", "`worms apply round-agent gameplans`: the host's workspace"),
    ("verify", "`worms status`: on the target, no error"),
]
STATE_FILES = ("ansible/group_vars/vault.yml", "ansible/.vault_pass", "ansible/pki")
HOST_STATES = ("/opt/worms/opentofu/stacks/sandbox/terraform.tfstate", "/opt/worms/opentofu/stacks/devbox/terraform.tfstate")


class Ops:
    """What the upgrade does to the world, one method each (the tests' fakes replace them)."""

    def __init__(self, config):
        self.config = config
        self.repo = Path(config.repo_root)

    def git(self, *args) -> tuple[int, str]:
        done = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        return done.returncode, (done.stdout + done.stderr).strip()

    def worms(self, *args) -> int:
        """This checkout's CLI (the new code, after the code step), in this terminal."""
        return subprocess.run([sys.executable, str(self.repo / "src" / "manage.py"), *args]).returncode

    def worms_json(self, *args) -> dict:
        done = subprocess.run([sys.executable, str(self.repo / "src" / "manage.py"), *args, "--json"], capture_output=True, text=True)
        try:
            return json.loads(done.stdout)
        except ValueError:
            return {"exit": done.returncode, "sections": []}

    def blockers(self) -> list[str]:
        """What stands in the way, each with what to do (empty: nothing)."""
        from worms_cli import power
        from worms_cli.build import Runner

        out = []
        runner = Runner(self.config)
        sandbox = [vm["name"] for vm in runner.vms() if vm.get("pool") == "sandbox"]
        if sandbox:
            out.append(f"a round's sandbox exists ({len(sandbox)} VMs): finish the round, then `worms round teardown`")
        if runner.flag_exists(power.INTERNET_FLAG):
            out.append("the internet window is open: `worms internet off`")
        if runner.host_read("/var/lib/worms/sandbox-build.json") is not None:
            out.append("an unfinished sandbox build is recorded: `worms round teardown`")
        try:
            from worms_cli import django_setup

            django_setup.setup(self.config)
            from core.models.rounds import HostJob

            jobs = list(HostJob.objects.filter(state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING]).values_list("id", "kind"))
            if jobs:
                out.append("host jobs queued or running (" + ", ".join(f"#{n} {kind}" for n, kind in jobs[:5])
                           + "): let them end, or `worms jobs cancel <n>`")  # fmt: skip
        except Exception as exc:  # noqa: BLE001  (a database that does not answer is in the way too)
            out.append(f"the database does not say which host jobs run ({str(exc).strip()[:100]})")
        return out

    def backup(self) -> list[str]:
        """The database's dump and the git-ignored state's tar: their paths."""
        from worms_cli import database

        dump, _size = database.backup(self.config)
        folder = Path.home() / ".worms" / "backups"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        tar = folder / f"{datetime.now():%Y%m%d-%H%M%S}-state.tar.gz"
        with os.fdopen(os.open(tar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh, \
                tarfile.open(fileobj=fh, mode="w:gz") as archive:  # fmt: skip
            for name in STATE_FILES:
                if (self.repo / name).exists():
                    archive.add(self.repo / name, arcname=f"checkout/{name}")
            for state in sorted((self.repo / "opentofu" / "stacks").glob("**/terraform.tfstate")):
                archive.add(state, arcname=f"checkout/{state.relative_to(self.repo)}")
            for state in HOST_STATES:
                if Path(state).exists():
                    archive.add(state, arcname=f"host{state}")
        return [str(dump), str(tar)]

    def read(self) -> dict:
        try:
            record = json.loads(Path(PROGRESS).read_text())
        except (OSError, ValueError):
            return {}
        return record if isinstance(record, dict) else {}

    def write(self, record: dict) -> None:
        path = Path(PROGRESS)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".new").write_text(json.dumps(record, indent=1) + "\n")
        os.replace(path.with_suffix(".new"), path)

    def continue_with_new_code(self, args: list[str]) -> None:
        """The steps after the code: this command again, run by the code it brought."""
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable, str(self.repo / "src" / "manage.py"), "upgrade", *args])


def _target(ops, spec: str) -> tuple[str, str]:
    """(the target commit, short, and how to get there: "bundle" or "rev"), or a BuildError."""
    if Path(spec).expanduser().is_file():
        bundle = str(Path(spec).expanduser())
        code, said = ops.git("bundle", "verify", bundle)
        if code != 0:
            raise BuildError(f"the bundle does not verify: {said[-300:]}")
        code, heads = ops.git("bundle", "list-heads", bundle, "refs/heads/main")
        if code != 0 or not heads.split():
            raise BuildError(f"the bundle has no main: {heads[-200:]}")
        return heads.split()[0][:7], "bundle"
    code, commit = ops.git("rev-parse", "--verify", "--short=7", f"{spec}^{{commit}}")
    if code != 0:
        raise BuildError(f"{spec!r} is neither a bundle nor a commit here")
    return commit.strip(), "rev"


def _head(ops) -> str:
    return ops.git("rev-parse", "--short=7", "HEAD")[1].strip()


def verify(ops, target: str) -> list[str]:
    """What `worms status` (the new code's) says differs: [] when on the target, no error."""
    document = ops.worms_json("status")
    problems = []
    for section in document.get("sections", []):
        if section.get("heading") == "version":
            lines = section.get("lines") or []
            if not (lines and lines[0].endswith(f"system: on {target}")):
                problems += [line.strip() for line in lines]
        if section.get("heading") == "attention":
            for table in section.get("tables", []):
                problems += [f"{row[0]}: {row[1]}" for row in table if len(row) > 1 and row[1].startswith("✗")]
    return problems


def run(config, spec: str | None, resume=False, plan=False, yes=False, ops=None) -> int:
    ops = ops or Ops(config)
    record = ops.read()
    if resume:
        if not record or record.get("finished"):
            output.error(f"no upgrade to resume ({PROGRESS})")
            return 1
        target, how, bundle = record["target"], record["how"], record.get("bundle", "")
    else:
        if not spec:
            output.error("to which commit? `worms upgrade <bundle|rev>`")
            return 2
        target, how = _target(ops, spec)
        bundle = str(Path(spec).expanduser()) if how == "bundle" else ""
        if record.get("target") == target and record.get("finished") and _head(ops) == target:
            print(output.status_cell("ok", f"already on {target}"))
            return 0
        record = {"target": target, "how": how, "bundle": bundle, "spec": spec, "from": _head(ops),
                  "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "steps_done": [], "kept": []}  # fmt: skip
    done = list(record.get("steps_done", []))
    print(output.heading(f"upgrade to {target} (from {record.get('from', '?')})"))
    for name, what in STEPS:
        print(output.status_cell("ok" if name in done else "todo", f"{name}: {what}"))
    if plan:
        code, log = ops.git("log", "--oneline", f"HEAD..{target}") if how == "rev" else (0, "")
        print(log or "(the commits come with the bundle)")
        blockers = ops.blockers()
        for line in blockers:
            print(output.status_cell("error", line))
        print(output.color("nothing changed (--plan); `worms apply --plan` says what each component gets once the code is in", "dim"))
        return 1 if blockers else 0
    if not resume and not yes and not ask_go(target):
        return 1
    for name, _what in STEPS:
        if name in done:
            continue
        print(output.heading(f"upgrade: {name}"))
        code = _step(ops, name, record, target, how, bundle, yes)
        if code is not None:
            print(output.color(f"stopped at {name}: mend it, then `worms upgrade --resume`", "red"))
            return code
        done.append(name)
        record["steps_done"] = done
        ops.write(record)
        if name == "code":
            ops.continue_with_new_code(["--resume", *(["--yes"] if yes else [])])
            return 0  # not reached: the new code goes on
    record["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ops.write(record)
    print(output.status_cell("ok", f"system: on {target}"))
    print(output.color(f"the way back: `git checkout {record.get('from', '?')}` and `worms apply`; the database: `worms db "
                       f"restore {(record.get('kept') or ['?'])[0]}`; the state: {(record.get('kept') or ['', '?'])[-1]}", "dim"))  # fmt: skip
    return 0


def ask_go(target: str) -> bool:
    from worms_cli.rounds import ask

    return ask(f"Upgrade this system to {target}? Each `worms apply` asks its own questions.", False, False)


def _step(ops, name, record, target, how, bundle, yes) -> int | None:
    """One step; None when done, else the exit code to stop with."""
    flag = ["--yes"] if yes else []
    if name == "preflight":
        problems = ops.blockers()
        if ops.git("diff", "--quiet", "HEAD")[0] != 0:
            problems.append("this checkout has uncommitted changes: commit or stash them")
        if _head(ops) != target and how == "rev" and ops.git("merge-base", "--is-ancestor", "HEAD", target)[0] != 0:
            problems.append(f"{target} is not ahead of this checkout: no fast-forward (an upgrade never merges)")
        for line in problems:
            print(output.status_cell("error", line))
        return 1 if problems else None
    if name == "backup":
        record["kept"] = ops.backup()
        print(output.status_cell("ok", "kept: " + ", ".join(record["kept"])))
        return None
    if name == "code":
        if _head(ops) == target:
            print(output.status_cell("ok", f"the checkout is at {target} already"))
            return None
        code, said = ops.git("pull", "--ff-only", bundle, "main") if how == "bundle" else ops.git("merge", "--ff-only", target)
        print(said[-600:])
        if code != 0 or _head(ops) != target:
            output.error(f"the checkout did not get to {target} as a fast-forward")
            return 1
        return None
    if name == "orchestrator":
        return ops.worms("apply", "orchestrator", *flag) or None
    if name == "apply":
        return ops.worms("apply", *flag) or None
    if name == "round-agent":
        return ops.worms("apply", "round-agent", "gameplans", *flag) or None
    problems = verify(ops, target)
    for line in problems:
        print(output.status_cell("error", line))
    return 1 if problems else None
