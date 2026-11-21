"""What changed between what runs on the platform and this checkout, per component: `worms apply`
alone (and `--plan`); `worms apply <component>... --plan`'s view of what each VM runs and of
OpenTofu's own plan.

    what runs       per component, the commit it was last brought up to: the node's record
                    (/var/lib/worms/deployed.json, written after every successful apply or
                    rebuild of it), else for a VM the VM history (its last configuration),
                    else unknown. A commit with "-dirty" had uncommitted changes nobody can see now.
    what changed    `git diff` from that commit to this checkout's working tree (what a deploy takes).
    what it touches the files each component is made from, read from the repository itself (the
                    reading the tests check against every host: tests/ansible_static.py):
                      a VM: the configure playbooks whose plays reach its inventory group, the roles
                        they apply (with the roles those include, and their dependencies), the
                        group_vars of its groups, the inventory, and what a role copies from the
                        repository (django_app: projects/dashboard/, the gameplans);
                      host-network, round-agent: their playbook and roles; the round agent's
                        workspace is a copy of the repository (ansible/, opentofu/, scripts/, src/,
                        gameplans/: role round_agent, tasks/workspace.yml);
                      gameplans: gameplans/, src/gameplan/;
                      the OpenTofu stacks: OpenTofu's own plan, never a guess from files.
    packages        a component's package names (apt, pip) read at both commits: different means it
                    needs the internet window (--packages). The caches' own lists: a refill.

Not deployed: the CLI (src/manage.py, src/worms_cli/: it runs from the checkout), docs/, tests/. A
file no rule claims is said, not guessed at.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from worms_cli import output

DEPLOYED = "/var/lib/worms/deployed.json"
NOT_DEPLOYED = ("src/manage.py", "src/worms_cli/", "docs/", "tests/", "README.md", "Makefile", ".gitignore", "pytest.ini",
                "helpers/", "bundle.git", "scripts/apt-cache-offline-index.py")  # fmt: skip
# Run here, on the controller, by every apply and configure (the CA, the inventory, Ansible's settings).
CONTROLLER = ("ansible/CreateCA.yml", "ansible/roles/pki/ca_create/", "scripts/generate_inventory.py", "ansible/ansible.cfg",
              "ansible/callback_plugins/")  # fmt: skip
AGENT_SOURCES = ("scripts/round_agent/", "src/gameplan/")  # the round agent's own code, besides its role
WORKSPACE = ("ansible/", "opentofu/", "scripts/", "src/", "gameplans/")  # the round agent's copy (minus its excludes)
WORKSPACE_EXCLUDED = ("ansible/group_vars/vault.yml", "projects/", "docs/")
ROLE_SOURCES = {"django_app": ("projects/dashboard/", "gameplans/", "src/gameplan/")}
SEEDS = {"apt-cache": ("ansible/roles/private_internet/apt_cache/defaults/main.yml", "apt_cache_seed_packages"),
         "devpi": ("ansible/roles/private_internet/devpi/defaults/main.yml", "devpi_seed_packages")}  # fmt: skip


# -- the repository at a commit ------------------------------------------------------------------


class Tree:
    """The repository as it is here (rev None: the working tree, which a deploy takes) or at a commit."""

    def __init__(self, repo, rev: str | None = None):
        self.repo, self.rev = Path(repo), rev
        self._cache: dict[str, str | None] = {}

    def read(self, path: str) -> str | None:
        if path not in self._cache:
            if self.rev is None:
                file = self.repo / path
                self._cache[path] = file.read_text(errors="replace") if file.is_file() else None
            else:
                proc = subprocess.run(["git", "-C", str(self.repo), "show", f"{self.rev}:{path}"], capture_output=True, text=True)
                self._cache[path] = proc.stdout if proc.returncode == 0 else None
        return self._cache[path]

    def yaml(self, path: str):
        text = self.read(path)
        try:
            return yaml.safe_load(text) if text else None
        except yaml.YAMLError:
            return None


def _role_dir(repo, name: str) -> str | None:
    found = [p for p in (Path(repo) / "ansible" / "roles").glob(f"*/{name}") if p.is_dir()]
    return str(found[0].relative_to(repo)) if found else None


def _groups_of(tree: Tree, group: str) -> set:
    children = ((tree.yaml("ansible/inventory.yml") or {}).get("all") or {}).get("children") or {}
    found = {group, "all"}
    while True:
        more = {g for g, v in children.items() if isinstance(v, dict) and set(v.get("children") or {}) & found} - found
        if not more:
            return found
        found |= more


def _targets(pattern: str, groups: set) -> bool:
    terms = re.split(r"[:,]", pattern)
    include = [t for t in terms if t and not t.startswith(("!", "&"))]
    exclude = [t[1:] for t in terms if t.startswith("!")]
    return any(t in groups for t in include) and not any(t in groups for t in exclude)


def _included_roles(tasks) -> set:
    names = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for block in ("block", "rescue", "always"):
            names |= _included_roles(task.get(block))
        for module in ("ansible.builtin.include_role", "ansible.builtin.import_role", "include_role", "import_role"):
            if isinstance(task.get(module), dict) and "name" in task[module]:
                names.add(task[module]["name"])
    return names


def _role_closure(tree: Tree, roles: set) -> dict[str, str]:
    """{role: its directory} of these roles, the roles their tasks include and their dependencies."""
    out: dict[str, str] = {}
    todo = list(roles)
    while todo:
        name = todo.pop()
        if name in out:
            continue
        directory = _role_dir(tree.repo, name)
        if directory is None:
            continue
        out[name] = directory
        meta = tree.yaml(f"{directory}/meta/main.yml") or {}
        todo += [d if isinstance(d, str) else d.get("role", "") for d in (meta.get("dependencies") or [])]
        for file in (Path(tree.repo) / directory / "tasks").glob("*.yml"):
            todo += list(_included_roles(tree.yaml(str(file.relative_to(tree.repo)))))
    return out


@dataclass
class Reach:
    playbooks: set = field(default_factory=set)
    roles: dict = field(default_factory=dict)  # role -> directory
    groups: set = field(default_factory=set)


def reach(tree: Tree, group: str, playbooks: list[str]) -> Reach:
    """What of the repository makes the hosts of `group`: the plays of `playbooks` that reach it."""
    found = Reach(groups=_groups_of(tree, group))
    roles: set = set()
    for playbook in playbooks:
        for play in tree.yaml(f"ansible/{playbook}") or []:
            if not isinstance(play, dict) or "import_playbook" in play or not _targets(str(play.get("hosts", "")), found.groups):
                continue
            found.playbooks.add(playbook)
            roles |= {r if isinstance(r, str) else r.get("role", "") for r in play.get("roles") or []}
            for section in ("pre_tasks", "tasks", "post_tasks"):
                roles |= _included_roles(play.get(section))
    found.roles = _role_closure(tree, roles)
    return found


def _packages(tree: Tree, found: Reach) -> set:
    """The package names (apt, pip) the roles and plays reaching a component install, at `tree`."""
    variables: dict = {}
    for group in sorted(found.groups):
        variables.update(tree.yaml(f"ansible/group_vars/{group}.yml") or {})
    names: set = set()

    def values(value, local) -> set:
        if isinstance(value, list):
            return {str(v) for v in value}
        if isinstance(value, str) and "{{" in value:
            out: set = set()
            for ref in re.findall(r"\b([a-z_][a-z0-9_]*)\b", value):
                found_value = {**local, **variables}.get(ref)
                if isinstance(found_value, list):
                    out |= {str(v) for v in found_value}
            return out
        return {value} if isinstance(value, str) else set()

    def scan(tasks, local):
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            for block in ("block", "rescue", "always"):
                scan(task.get(block), local)
            for module in ("ansible.builtin.apt", "apt", "ansible.builtin.package", "package", "ansible.builtin.pip", "pip"):
                if isinstance(task.get(module), dict) and "name" in task[module]:
                    names.update(values(task[module]["name"], local))

    def lists_of(variables_here: dict) -> None:
        # A role may install a list it computes (devbox: devbox_packages_held from devbox_packages):
        # every list whose name says "packages" counts (the caches' seed lists too: a refill).
        for key, value in variables_here.items():
            if "packages" in key and isinstance(value, list):
                names.update(f"{key}:{v}" if "seed" in key else str(v) for v in value)

    lists_of(variables)
    for directory in found.roles.values():
        local: dict = {}
        for sub in ("defaults", "vars"):
            local.update(tree.yaml(f"{directory}/{sub}/main.yml") or {})
        lists_of(local)
        for file in sorted((Path(tree.repo) / directory / "tasks").glob("*.yml")):
            scan(tree.yaml(str(file.relative_to(tree.repo))), local)
    return names


# -- what runs, what changed -----------------------------------------------------------------------


def deployed(session) -> dict[str, tuple[str, str]]:
    """{component: (the commit it was last brought up to, when)}, from the node's record."""
    code, out, _err = session.node_run(["cat", DEPLOYED])
    try:
        return {k: (v["commit"], v.get("time", "")) for k, v in (json.loads(out) if code == 0 and out.strip() else {}).items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def record_deployed(session, names, commit: str) -> None:
    """After a successful apply or rebuild: these components run `commit` now."""
    code, out, _err = session.node_run(["cat", DEPLOYED])
    try:
        current = json.loads(out) if code == 0 and out.strip() else {}
    except ValueError:
        current = {}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name in names:
        current[name] = {"commit": commit, "time": stamp}
    text = json.dumps(current, indent=1, sort_keys=True)
    import shlex

    session.node_run(["sh", "-c", f"install -d -m 755 /var/lib/worms && printf '%s\\n' {shlex.quote(text)} > {DEPLOYED}.new "
                                  f"&& mv {DEPLOYED}.new {DEPLOYED}"])  # fmt: skip


def vm_commits(session) -> dict[str, str]:
    """{VM name: the commit of its last configuration}, from the VM history (worms_cli/history.py)."""
    from worms_cli import history

    code, out, _err = session.node_run(["cat", history.LEDGER])
    last: dict[str, str] = {}
    for event in history.parse(out) if code == 0 else []:
        if event["event"] in ("configured", "packages", *history.CREATED):
            last[event["name"]] = event["commit"]
    return last


def changed_files(repo, base: str, since: str = "") -> list[str] | None:
    """The files that differ between `base` and the working tree (None: `base` is not in this
    repository's history). An untracked file has no history: it counts when it was modified after
    `since` (the record's time, UTC), or always when that is not known."""
    repo = str(repo)
    if subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{base}^{{commit}}"], capture_output=True).returncode != 0:
        return None
    tracked = subprocess.run(["git", "-C", repo, "diff", "--name-only", base], capture_output=True, text=True).stdout.split()
    untracked = subprocess.run(["git", "-C", repo, "ls-files", "--others", "--exclude-standard"], capture_output=True,
                               text=True).stdout.split()  # fmt: skip
    if since:
        try:
            limit = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            untracked = [f for f in untracked if (Path(repo) / f).stat().st_mtime > limit]
        except (ValueError, OSError):
            pass
    return sorted(set(tracked) | set(untracked))


def _oldest(repo, commits) -> str:
    """Of these commits, the one furthest behind HEAD (the devboxes: the team reset longest ago)."""
    def behind(commit: str) -> int:
        proc = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", f"{commit.removesuffix('-dirty')}..HEAD"],
                              capture_output=True, text=True)  # fmt: skip
        return int(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip().isdigit() else 10**9
    return max(commits, key=behind) if commits else ""


@dataclass
class Change:
    component: str
    base: str  # the commit it runs, "" unknown
    why: list = field(default_factory=list)  # files (or reasons) that touch it
    packages: list = field(default_factory=list)  # package names added or removed
    note: str = ""


def _claims(path: str, prefixes) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in prefixes)


def analyse(config, session) -> tuple[list[Change], dict, list[str]]:
    """(the components that changed, with why; {component: base}; files no rule claims)."""
    from worms_cli import components
    from worms_cli.pipeline import CONFIGURE_PLAYBOOKS

    repo = config.repo_root
    here = Tree(repo)
    record = deployed(session)
    vms = vm_commits(session)
    bases: dict[str, str] = {}
    changes: list[Change] = []
    every_prefix: list = []  # what any component is made from: a changed file outside them all is said
    all_changed: set = set()
    for name, component in components.COMPONENTS.items():
        if component.kind == "stack":
            continue  # OpenTofu's own plan says (plan_stacks)
        base, since = record.get(name, ("", ""))
        if not base and component.kind == "vm":
            base = vms.get(name, "")
        if not base and component.kind == "devbox":
            base = _oldest(repo, {c for vm, c in vms.items() if re.fullmatch(r"devbox-t\d+-(a|b|c|gw)", vm)})
        bases[name] = base
        if component.kind == "devbox":
            playbooks, group = ["SetupDevbox.yml", "SetupSSHGateway.yml"], "devbox"
        elif component.kind == "node":
            playbooks, group = [component.playbook], "proxmox_host"
        elif component.kind == "gameplans":
            playbooks, group = [], ""
        else:
            playbooks, group = [p for p in CONFIGURE_PLAYBOOKS if p != "SetupRoundAgent.yml"], component.group
        found = reach(here, group, playbooks) if group else Reach()
        prefixes = [f"ansible/{p}" for p in found.playbooks] + [f"{d}/" for d in found.roles.values()]
        prefixes += [f"ansible/group_vars/{g}.yml" for g in found.groups] + (["ansible/inventory.yml"] if group else [])
        for role in found.roles:
            prefixes += list(ROLE_SOURCES.get(role, ()))
        if name == "gameplans":
            prefixes += ["gameplans/", "src/gameplan/"]
        if name == "round-agent":
            prefixes += list(AGENT_SOURCES)
        every_prefix += prefixes
        if name == "round-agent":
            prefixes += list(WORKSPACE)  # its workspace: a copy of these, synced whenever it is configured
        devbox_note = "the devboxes take it when they are reset (`worms vm reset devboxes`): never by itself, it is the teams' work"
        clean_base = base.removesuffix("-dirty")
        files = changed_files(repo, clean_base, since) if clean_base else None
        if files is None:
            reason = "runs an unknown commit" if not base else f"runs {base}, not in this checkout's history"
            changes.append(Change(name, base, [reason], note=devbox_note if component.kind == "devbox" else ""))
            continue
        all_changed |= set(files)
        relevant = [f for f in files if _claims(f, prefixes) and not (name == "round-agent" and _claims(f, WORKSPACE_EXCLUDED))]
        why = relevant + ([f"deployed from {base}: uncommitted changes then, unknown now"] if base.endswith("-dirty") else [])
        if not why:
            continue
        change = Change(name, base, why)
        if component.kind in ("vm", "node") and group:
            then = Tree(repo, clean_base)
            before, after = _packages(then, reach(then, group, playbooks)), _packages(here, found)
            change.packages = sorted(f"+{n}" for n in after - before) + sorted(f"-{n}" for n in before - after)
        if name in SEEDS:
            path, key = SEEDS[name]
            then, now = (Tree(repo, clean_base).yaml(path) or {}).get(key) or [], (here.yaml(path) or {}).get(key) or []
            if then != now:
                change.note = f"its seed list changed ({len(set(now) - set(then))} added, {len(set(then) - set(now))} removed): a refill"
        if component.kind == "devbox":
            change.note = devbox_note
        changes.append(change)
    unclaimed = sorted(f for f in all_changed if not _claims(f, every_prefix) and not _claims(f, NOT_DEPLOYED)
                       and not _claims(f, CONTROLLER) and not f.startswith("opentofu/"))  # fmt: skip
    return changes, bases, unclaimed


# -- OpenTofu: its own plan, saved, and applied as shown ----------------------------------------------


@dataclass
class StackPlan:
    stack: str
    file: str = ""  # the saved plan
    updates: list = field(default_factory=list)  # in place
    creates: list = field(default_factory=list)
    replaces: list = field(default_factory=list)
    deletes: list = field(default_factory=list)
    error: str = ""

    @property
    def safe(self) -> bool:
        """Applied as part of a change: only in-place updates and new resources that are no VM."""
        return not self.error and not self.replaces and not self.deletes and not any(".proxmox_virtual_environment_vm." in a or a.startswith("proxmox_virtual_environment_vm.") for a in self.creates)

    @property
    def empty(self) -> bool:
        return not (self.updates or self.creates or self.replaces or self.deletes)


def plan_stack(pipe, stack: str, out_dir) -> StackPlan:
    """`tofu plan -out`, then what it would do (`tofu show -json`), resource by resource."""
    found = StackPlan(stack)
    cwd = f"{pipe.runner.repo_root}/opentofu/stacks/{stack}"
    file = str(Path(out_dir) / f"{stack.replace('/', '-')}.tfplan")
    try:
        env = pipe.runner.session.tofu_env()
    except Exception as exc:  # noqa: BLE001
        found.error = str(exc)
        return found
    proc = subprocess.run(["tofu", "plan", "-input=false", "-no-color", "-lock=false", f"-out={file}"], cwd=cwd, env=env,
                          capture_output=True, text=True)  # fmt: skip
    if proc.returncode != 0:
        found.error = ((proc.stderr or proc.stdout).strip().splitlines() or ["?"])[-1][:300]
        return found
    shown = subprocess.run(["tofu", "show", "-json", file], cwd=cwd, env=env, capture_output=True, text=True)
    try:
        resources = json.loads(shown.stdout).get("resource_changes") or []
    except ValueError:
        found.error = "tofu show gave no plan"
        return found
    found.file = file
    for change in resources:
        actions, address = change.get("change", {}).get("actions") or [], change.get("address", "?")
        if actions == ["update"]:
            found.updates.append(address)
        elif actions == ["create"]:
            found.creates.append(address)
        elif "delete" in actions and "create" in actions:
            found.replaces.append(address)
        elif actions == ["delete"]:
            found.deletes.append(address)
    return found


def stacks_to_plan() -> list[str]:
    """The stacks the CLI applies (the devbox and sandbox stacks are the round agent's)."""
    from worms_cli import components
    from worms_cli.session import NODE_STACKS

    seen = []
    for component in components.COMPONENTS.values():
        if component.stack and component.stack not in NODE_STACKS and component.stack not in seen:
            seen.append(component.stack)
    return seen


# -- `worms apply <component>... --plan`: what each VM runs, OpenTofu's own plan ------------------


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
