"""A static reading of the Ansible side, without running Ansible: which plays
of the configure sequence (src/worms_cli/pipeline.py, CONFIGURE_PLAYBOOKS: Site.yml and the round agent's)
reach a VM, which roles they apply and which packages those roles install
(apt/package tasks, following includes and role dependencies; a package list
given as "{{ var }}" is looked up in the role defaults/vars).
"""

from __future__ import annotations

import re

import yaml

from conftest import REPO

ANSIBLE = REPO / "ansible"


def configure_playbooks() -> list:
    pipeline = (REPO / "src" / "worms_cli" / "pipeline.py").read_text()
    block = re.search(r"CONFIGURE_PLAYBOOKS = \[(.*?)\]", pipeline, re.S).group(1)
    names = {name: value for name, value in re.findall(r'^(\w+) = "([\w.]+\.yml)"', pipeline, re.M)}
    return [names.get(word.strip(), word.strip().strip('"')) for word in block.split(",") if word.strip()]


def groups_of(vm: str) -> set:
    """The VM group and every group that contains it (inventory.yml)."""
    children = yaml.safe_load((ANSIBLE / "inventory.yml").read_text())["all"]["children"]
    found = {vm, "all"}
    while True:
        more = {g for g, v in children.items() if v and set((v.get("children") or {})) & found} - found
        if not more:
            return found
        found |= more


def targets(pattern: str, groups: set) -> bool:
    terms = re.split(r"[:,]", pattern)
    include = [t for t in terms if t and not t.startswith(("!", "&"))]
    exclude = [t[1:] for t in terms if t.startswith("!")]
    return any(t in groups for t in include) and not any(t in groups for t in exclude)


def role_dir(name: str):
    found = [p for p in (ANSIBLE / "roles").glob(f"*/{name}") if p.is_dir()]
    assert found, f"role {name} not found"
    return found[0]


def role_vars(role) -> dict:
    out = {}
    for sub in ("defaults", "vars"):
        f = role / sub / "main.yml"
        if f.exists():
            out.update(yaml.safe_load(f.read_text()) or {})
    return out


def apt_names(tasks, variables, role=None) -> set:
    """Package names of the apt/package tasks (following includes)."""
    names = set()
    for task in tasks or []:
        for block in ("block", "rescue", "always"):
            names |= apt_names(task.get(block), variables, role)
        for module in ("ansible.builtin.apt", "apt", "ansible.builtin.package", "package"):
            if module in task and isinstance(task[module], dict) and "name" in task[module]:
                value = task[module]["name"]
                ref = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", value) if isinstance(value, str) else None
                if ref:
                    value = variables.get(ref.group(1), [])
                names |= set([value] if isinstance(value, str) else value)
        for module in ("ansible.builtin.include_tasks", "ansible.builtin.import_tasks", "include_tasks", "import_tasks"):
            if module in task and role is not None:
                target = task[module] if isinstance(task[module], str) else task[module]["file"]
                names |= apt_names(yaml.safe_load((role / "tasks" / target).read_text()), variables, role)
        for module in ("ansible.builtin.include_role", "ansible.builtin.import_role", "include_role", "import_role"):
            if module in task:
                names |= role_packages(task[module]["name"])
    return names


def role_packages(name: str) -> set:
    role = role_dir(name)
    variables = role_vars(role)
    names = set()
    meta = role / "meta" / "main.yml"
    if meta.exists():
        for dep in (yaml.safe_load(meta.read_text()) or {}).get("dependencies") or []:
            names |= role_packages(dep if isinstance(dep, str) else dep["role"])
    tasks = role / "tasks" / "main.yml"
    if tasks.exists():
        names |= apt_names(yaml.safe_load(tasks.read_text()), variables, role)
    return names


def ansible_packages(vm: str) -> set:
    groups = groups_of(vm)
    names = set()
    for _playbook, play in plays(groups):
        for role in play_roles(play, groups):
            names |= role_packages(role)
        for section in ("pre_tasks", "tasks", "post_tasks"):
            names |= apt_names(play.get(section), play.get("vars") or {})
    return names


def plays(groups: set):
    """(playbook, play) of the configure sequence that target these groups."""
    for playbook in configure_playbooks():
        for play in yaml.safe_load((ANSIBLE / playbook).read_text()) or []:
            if "import_playbook" not in play and targets(str(play.get("hosts", "")), groups):
                yield playbook, play


GROUP_CONDITION = re.compile(r"""['"]([\w-]+)['"]\s+(not\s+in|in)\s+group_names""")


def for_groups(when, groups) -> bool:
    """Whether a task's `when` lets hosts of these groups through, as far as it names groups
    (`'dns_hosts' in group_names`, Site.yml); any other condition counts as true."""
    for condition in when if isinstance(when, list) else [when]:
        for group, how in GROUP_CONDITION.findall(str(condition or "")):
            if (group in groups) != (how == "in"):
                return False
    return True


def play_roles(play, groups=None) -> list:
    """The play's roles, and those its tasks include (for hosts of `groups`: their conditions on
    group_names), each once."""
    roles = [role if isinstance(role, str) else role["role"] for role in play.get("roles") or []]
    for section in ("pre_tasks", "tasks", "post_tasks"):
        for task in play.get(section) or []:
            include = task.get("ansible.builtin.include_role") or task.get("include_role")
            name = include["name"] if include else (task.get("vars") or {}).get("worms_role")  # Site.yml: tasks/role.yml
            if name and (groups is None or for_groups(task.get("when"), groups)):
                roles.append(name)
    return list(dict.fromkeys(roles))
