"""Site.yml's role stamps: is a role on this VM unchanged since it last ran here to its end?

A role's stamp is a hash of what it is made from: every file of its directory, the sources
roles/sources.yml names for it (the dashboard's code, the CA), the values of the variables its files
name (group_vars, the inventory, the vault: as they are for this VM), the VM's groups and name.
After a role ran on a VM to its end, its handlers too, tasks/role.yml writes its stamp there
(/var/lib/worms/stamps/<role>). The next run reads them all at once (Site.yml: "The roles'
stamps here"); this plugin, on the controller only (no connection), compares: equal, and the role
is skipped there. An unchanged VM so costs a gate per role, not every task of it.

Never skipped:
  - with packages (no --skip-tags packages: the internet window): what a role installs is fetched;
  - with -e worms_full=true (`worms apply --full`): every role runs, and its stamp is written again;
  - in a run of a part (--tags, or any --skip-tags but packages): no stamp is read or written.

What a stamp cannot see: the VM itself drifting (a hand edit, a deleted file); the facts (they
describe the VM); the Proxmox CA django_app fetches from the host. `--full` checks everything.
A role that names `hostvars` or `groups` also hashes the inventory files. A service VM rolled back
to its snapshot has the stamps of its snapshot: they still say what that state was made from.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re

import yaml
from ansible.plugins.action import ActionBase

VERSION = "1"  # a change here invalidates every stamp
STAMPS = "/var/lib/worms/stamps"
IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
CONNECTION = {"ansible_host", "ansible_user", "ansible_port", "ansible_python_interpreter"}
# Variables whose value is the run's, not the configuration's.
VOLATILE = {"hostvars", "groups", "play_hosts", "omit", "playbook_dir", "role_path", "inventory_dir", "inventory_file",
            "environment", "vars", "role_names", "role_name", "worms_gate", "worms_stamps", "worms_cloud_init",
            "worms_full", "worms_role", "worms_part"}  # fmt: skip
SKIPPED_DIRS = {"__pycache__", ".git", "node_modules", ".venv"}


def _undated(value):
    """A YAML file's content without its *_generated_at keys: the generated inventory says when it
    was written (inventory_generated_at), which changes on every regeneration and nothing else."""
    if isinstance(value, dict):
        return {k: _undated(v) for k, v in value.items() if not str(k).endswith("_generated_at")}
    if isinstance(value, list):
        return [_undated(v) for v in value]
    return value


def _digest(path: str) -> str:
    """The content of a file, or of a directory's files (their paths and contents), as a hash; a YAML
    file by what it says, without the time it was generated."""
    h = hashlib.sha256()
    if os.path.isfile(path):
        if path.endswith((".yml", ".yaml")):
            try:
                with open(path, encoding="utf-8") as handle:
                    h.update(json.dumps(_undated(yaml.safe_load(handle)), sort_keys=True, default=str).encode())
                return h.hexdigest()
            except (OSError, yaml.YAMLError):
                pass  # hashed as bytes
        with open(path, "rb") as handle:
            h.update(handle.read())
        return h.hexdigest()
    if not os.path.isdir(path):
        return "missing"
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(root, name)
            h.update(os.path.relpath(full, path).encode() + b"\0")
            with open(full, "rb") as handle:
                h.update(handle.read())
            h.update(b"\0")
    return h.hexdigest()


def _names(role_dir: str) -> set[str]:
    """Every identifier in the role's text files: a superset of the variables it uses."""
    names: set[str] = set()
    for root, dirs, files in os.walk(role_dir):
        dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS and d != "files"]
        for name in files:
            if name.endswith((".yml", ".yaml", ".j2", ".cfg", ".conf", ".sh", ".py")):
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as handle:
                    names.update(IDENT.findall(handle.read()))
    return names


def _result(value) -> bool:
    """A registered task result (its times change with every run): not configuration."""
    return isinstance(value, dict) and ("changed" in value or "failed" in value) and ("rc" in value or "msg" in value
                                                                                      or "invocation" in value)


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _requires_connection = False

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        task_vars = task_vars or {}
        role = self._task.args["role"]
        part = self._task.args.get("part") or "main"
        key = role if part == "main" else f"{role}.{part}"
        run_tags = list(task_vars.get("ansible_run_tags") or ["all"])
        skip_tags = list(task_vars.get("ansible_skip_tags") or [])
        whole = run_tags == ["all"] and set(skip_tags) <= {"packages"}
        packages = "packages" not in skip_tags
        full = str(task_vars.get("worms_full", "false")).lower() in ("1", "true", "yes")
        if not whole:
            return {**result, "changed": False, "skip": False, "record": False, "key": key, "stamp": "",
                    "why": "a part of the configuration (--tags or --skip-tags): no stamp"}
        stamp = self._stamp(role, task_vars)
        mode = "packages" if packages else "offline"
        recorded = {}
        for line in ((task_vars.get("worms_stamps") or {}).get("stdout") or "").splitlines():
            name, _, value = line.partition(" ")
            recorded[name] = value.split(" ", 1)[0]
        same = recorded.get(key) == stamp
        skip = same and not packages and not full
        why = ("unchanged since its last run here: skipped" if skip else
               "with packages: it runs" if same and packages else "-e worms_full=true: it runs" if same else
               "no stamp here yet: it runs" if key not in recorded else "changed since its last run here: it runs")  # fmt: skip
        return {**result, "changed": False, "skip": skip, "record": True, "key": key, "stamp": f"{stamp} {mode}", "why": why}

    def _stamp(self, role: str, task_vars: dict) -> str:
        playbook_dir = task_vars.get("playbook_dir") or os.getcwd()
        repo = os.path.dirname(os.path.abspath(playbook_dir))
        found = sorted(glob.glob(os.path.join(playbook_dir, "roles", "*", role)))
        role_dir = found[0] if found else ""
        h = hashlib.sha256(f"{VERSION}\0{role}\0".encode())
        h.update(_digest(role_dir).encode() if role_dir else b"no role dir")
        try:
            with open(os.path.join(playbook_dir, "roles", "sources.yml"), encoding="utf-8") as handle:
                sources = (yaml.safe_load(handle) or {}).get(role) or []
        except OSError:
            sources = []
        names = _names(role_dir) if role_dir else set()
        if names & {"hostvars", "groups"}:
            sources = [*sources, "ansible/inventory.yml", "ansible/inventory.generated.yml"]
        for source in sources:
            h.update(f"{source}\0{_digest(os.path.join(repo, source))}\0".encode())
        values = {}
        for name in sorted(names):
            if name not in task_vars or name in VOLATILE or (name.startswith("ansible_") and name not in CONNECTION):
                continue
            value = task_vars[name]
            if _result(value):
                continue
            try:
                value = self._templar.template(value)
            except Exception:  # noqa: BLE001  (a value that does not render here is hashed as written)
                pass
            values[name] = value
        values["group_names"] = sorted(task_vars.get("group_names") or [])
        values["inventory_hostname"] = task_vars.get("inventory_hostname")
        h.update(json.dumps(values, sort_keys=True, default=str).encode())
        return h.hexdigest()[:32]
