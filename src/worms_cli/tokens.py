"""The Proxmox API tokens: which exist, the least each may do, and how they are
issued and checked.

This is the one definition of them. Their values live in the vault
(worms_cli/secrets.py), which is the only place they are read from and written
to: `worms secrets issue <name>` creates or rotates a token and stores it,
`worms secrets check` asks Proxmox whether each stored value works and has
exactly the rights defined here. No value is ever printed.

Every token is privilege-separated and has its role on the user and on the
token: its rights are the intersection of both, so neither alone widens it.

Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Token:
    user: str
    token_id: str
    role: str
    privs: tuple[str, ...]
    path: str
    purpose: str
    full_form: bool = False  # the vault holds user!id=secret, not only the secret

    @property
    def full_id(self) -> str:
        return f"{self.user}!{self.token_id}"


TOKENS = {
    # OpenTofu on the controller, and the round agent on the node for the
    # sandbox and devbox stacks. It builds VMs, bridges, pools and firewall
    # rules, so it needs these on the whole tree; it cannot grant rights, manage
    # users, reach a console or power off the node.
    "proxmox-token": Token(
        user="worms-provision@pve",
        token_id="worms",
        role="WormsProvision",
        privs=(
            "Datastore.Allocate",  # deleting stored files: snippets and images
            "Datastore.AllocateSpace",
            "Datastore.AllocateTemplate",
            "Datastore.Audit",
            "Pool.Allocate",
            "Pool.Audit",
            "SDN.Use",
            "Sys.Audit",
            "Sys.Modify",
            "VM.Allocate",
            "VM.Audit",
            "VM.Config.CDROM",
            "VM.Config.CPU",
            "VM.Config.Cloudinit",
            "VM.Config.Disk",
            "VM.Config.HWType",
            "VM.Config.Memory",
            "VM.Config.Network",
            "VM.Config.Options",
            "VM.PowerMgmt",
        ),
        path="/",
        purpose="OpenTofu (controller) and the round agent (node)",
        full_form=True,
    ),
    # The orchestrator VM, for the game plan: start and stop sandbox VMs, and
    # nothing outside the sandbox pool.
    "orchestrator-token": Token(
        user="orchestrator@pam",
        token_id="orchestrator-prov",
        role="WormsSandboxPower",
        privs=("VM.Audit", "VM.PowerMgmt"),
        path="/pool/sandbox",
        purpose="the orchestrator VM (sandbox VM power)",
    ),
    # The orchestrator's topology collector and `worms status`: read-only, and
    # only VMs, pools and the node's network and firewall.
    "topology-token": Token(
        user="topology@pve",
        token_id="readonly",
        role="WormsTopology",
        privs=("Pool.Audit", "SDN.Audit", "Sys.Audit", "VM.Audit"),  # SDN.Audit: else bridges are silently hidden
        path="/",
        purpose="the orchestrator VM (topology view) and `worms status`",
    ),
}


class TokenError(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


# (argv) -> (returncode, stdout, stderr), on the Proxmox node.
Run = Callable[[list], tuple]


def _must(run: Run, *argv: str) -> str:
    rc, out, err = run(list(argv))
    if rc != 0:
        # The command line and pveum's error, never its output: that may hold a secret.
        raise TokenError(f"{' '.join(argv)}: {err.strip()[:200]}")
    return out


def _tolerate_existing(run: Run, *argv: str) -> None:
    rc, _, err = run(list(argv))
    if rc != 0 and "already exists" not in err:
        raise TokenError(f"{' '.join(argv)}: {err.strip()[:200]}")


def issue(spec: Token, run: Run) -> str:
    """Bring role, user and grants to the definition, create (or rotate) the
    token and remove every other grant of this identity. Returns what the vault
    stores."""
    privs = ",".join(spec.privs)
    _tolerate_existing(run, "pveum", "role", "add", spec.role, "--privs", privs)
    _must(run, "pveum", "role", "modify", spec.role, "--privs", privs)
    _tolerate_existing(run, "pveum", "user", "add", spec.user, "--comment", f"WORMS: {spec.purpose}")

    listed = json.loads(_must(run, "pveum", "user", "token", "list", spec.user, "--output-format", "json") or "[]")
    if any(entry.get("tokenid") == spec.token_id for entry in listed):
        _must(run, "pveum", "user", "token", "remove", spec.user, spec.token_id)
    created = _must(run, "pveum", "user", "token", "add", spec.user, spec.token_id, "--privsep", "1",
                    "--comment", f"WORMS: {spec.purpose}", "--output-format", "json")  # fmt: skip
    value = json.loads(created).get("value")
    if not value:
        raise TokenError(f"pveum did not return a secret for {spec.full_id}")

    _must(run, "pveum", "acl", "modify", spec.path, "--roles", spec.role, "--users", spec.user)
    _must(run, "pveum", "acl", "modify", spec.path, "--roles", spec.role, "--tokens", spec.full_id)
    for entry in json.loads(_must(run, "pveum", "acl", "list", "--output-format", "json") or "[]"):
        if entry.get("ugid") not in (spec.user, spec.full_id):
            continue
        if entry.get("path") == spec.path and entry.get("roleid") == spec.role:
            continue
        kind = "--tokens" if entry.get("type") == "token" else "--users"
        _must(run, "pveum", "acl", "delete", entry["path"], "--roles", entry["roleid"], kind, entry["ugid"])
    return f"{spec.full_id}={value}" if spec.full_form else value


def check(spec: Token, stored: str, permissions: Callable[[str, str], dict]) -> str:
    """Does the stored value authenticate, with exactly the defined rights?
    `permissions(secret, path)` is GET /access/permissions?path=<path> as that
    token, raising ApiError. Returns a state starting with one of: ok, not set,
    invalid, missing, broader, error."""
    if not stored:
        return "not set"
    secret = stored.rsplit("=", 1)[-1]
    try:
        actual = set(permissions(secret, spec.path).get(spec.path, {}))
        outside = {} if spec.path == "/" else permissions(secret, "/").get("/", {})
    except ApiError as exc:
        return "invalid: Proxmox refuses this value" if exc.status == 401 else f"error: {exc}"
    wanted = set(spec.privs)
    if actual - wanted or outside:
        extra = sorted((actual - wanted) | set(outside))
        return f"broader than {spec.role}: {', '.join(extra[:4])}{' ...' if len(extra) > 4 else ''}"
    if wanted - actual:
        return f"missing from {spec.role}: {', '.join(sorted(wanted - actual)[:4])}"
    return f"ok: {spec.role} on {spec.path}"
