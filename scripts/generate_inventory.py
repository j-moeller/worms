#!/usr/bin/env python3
"""Generate the Ansible inventory from OpenTofu outputs (the ground truth).

OpenTofu owns all addressing AND decides which VMs exist:
  * addressing (subnets, data-plane IPs, Proxmox host) comes from the
    `infrastructure` output of the platform stack (derived from the single
    source of truth in opentofu/shared/shared-variables.tf);
  * hosts come from the `ansible_hosts` output of every VM stack, i.e. from the
    VMs in that stack's STATE. A VM that is commented out (and applied away) or
    a stack that is not applied simply has no host, so Ansible never contacts it.

This script reads `tofu output -json` from those stacks and writes
`ansible/inventory.generated.yml`:

    all:
      vars:          # addressing, referenced by inventory.yml / group_vars
        infrastructure_ips: {...}
        ...
      children:
        database:    # one group per OpenTofu VM key
          hosts:
            database.worms.internal:
              ansible_host: 10.3.0.16      # management IP
              ansible_user: debian
              proxmox_vm_id: 101           # `qm status 101`, `qm terminal`, ...
              opentofu_stack: infrastructure/persistent
        sandbox:     # all generated sandbox VMs
          hosts: {...}

inventory.yml only declares groups (no hosts): the role groups (tls_hosts,
django_hosts, ...) have these VM groups as children, and per-VM settings live
in group_vars/<vm group>.yml. Re-run after every `tofu apply` (`worms step inventory`).

How Ansible connects is written here too: with the WORMS key in the agent, and no
other key (IdentityFile=none). On the Proxmox node (WORMS_SSH unset) the node is
`ansible_connection: local`. On a laptop (WORMS_SSH = root@<node>, as the `worms`
CLI sets it) the node is reached over SSH, every VM through it (ProxyJump), and
the node's own stacks (sandbox, devbox: state in the round agent's workspace
there) are read over SSH. Written by `worms` (WORMS_SSH_AUTH_SOCK set), the
connections name its agent; in the round agent's workspace, the agent
worms-node-run started (SSH_AUTH_SOCK). The ProxyJump hop reads your
~/.ssh/config: it must not name another IdentityAgent for the node.

Every value the inventory needs must be there: a missing platform output, or a
VM stack whose last apply did not finish (outputs, but no `ansible_hosts`), stops
it with an error rather than leave hosts out.

Standard library only, so it runs with the system Python on the control node.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import subprocess
import sys
from pathlib import Path


# Stacks whose state lives on the node (the round agent's workspace).
NODE_STACKS = {"sandbox", "devbox"}
NODE_RUN = "/usr/local/sbin/worms-node-run"
# The one key: the agent's, never a key file (~/.ssh/id_*).
ONE_KEY = ["-o", "IdentityFile=none", "-o", "IdentityAgent=SSH_AUTH_SOCK"]
# How to finish (or remove) a stack whose last apply stopped half way.
UNFINISHED = {
    "devbox": "the round agent applies it: `worms build devbox` (follow it: `tail -F /var/log/worms/jobs/latest.log` "
              "on the node). Or remove what was built, on the node: "
              "`worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'`",
    "sandbox": "the round agent builds it again for the next round. Or remove it, on the node: "
               "`worms-node-run opentofu/stacks/sandbox 'tofu destroy -auto-approve -parallelism=1'`",
}
# What the platform stack's `infrastructure` output must have.
PLATFORM_KEYS = ("infrastructure_ips", "management_ips", "services_ips", "host_ips", "private_internet_ips",
                 "services_ports", "networks", "bridges", "ssh_gateway_ips", "devbox_services_block",
                 "proxmox")  # fmt: skip


def node_output(target: str, stack: str) -> dict:
    """`tofu output -json` of a node-owned stack, over SSH ({} if unavailable)."""
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", *ONE_KEY, target, f"{NODE_RUN} opentofu/stacks/{stack} 'tofu output -json'"],
        capture_output=True, text=True,
    )  # fmt: skip
    if proc.returncode == 127 and NODE_RUN in proc.stderr:
        # A fresh node: the round agent (and its wrapper) comes later in the build.
        print(f"note: stack {stack} on {target}: no round agent yet, so not applied", file=sys.stderr)
        return {}
    if proc.returncode != 0:
        # Anything else is a failure (SSH, tofu, the state): an inventory without
        # these VMs would let the playbooks after it run against the wrong hosts.
        sys.exit(f"error: 'tofu output' of stack {stack} on {target} failed (exit {proc.returncode}):\n"
                 f"{proc.stderr.strip()[:500]}")  # fmt: skip
    return json.loads(proc.stdout or "{}")


def tofu_output(stack_dir: Path, tofu_bin: str) -> dict:
    """Return the parsed `tofu output -json` map for a stack, or {} if the stack
    has no state yet (not applied)."""
    controller = os.environ.get("WORMS_SSH", "")
    if controller and stack_dir.name in NODE_STACKS:
        return node_output(controller, stack_dir.name)
    if not (stack_dir / "terraform.tfstate").exists():
        # Not applied yet; nothing to read. Caller decides whether that is fatal.
        return {}
    try:
        proc = subprocess.run(
            [tofu_bin, f"-chdir={stack_dir}", "output", "-json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        sys.exit(f"error: '{tofu_bin}' not found on PATH (set --tofu-bin).")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"error: 'tofu output' failed in {stack_dir}:\n{exc.stderr}")
    return json.loads(proc.stdout or "{}")


def load_outputs(
    stack_dir: Path, name: str, tofu_bin: str, override: Path | None
) -> dict:
    """Return the `.value` of output `name` for a stack.

    `override` lets callers feed a pre-dumped `tofu output -json` file (testing /
    offline); otherwise `tofu` is invoked in `stack_dir`.
    """
    if override is not None:
        outputs = json.loads(override.read_text())
    else:
        outputs = tofu_output(stack_dir, tofu_bin)
    entry = outputs.get(name)
    return entry.get("value", {}) if entry else {}


# VM stacks whose `ansible_hosts` output becomes inventory hosts, with the
# group each VM key lands in (default: the key itself).
def _devbox_group(key):
    if key == "ssh_gateway":
        return "ssh_gateway"
    if key.endswith("-gw"):
        return "devbox_gw"
    return "devbox"


VM_STACKS = [
    ("infrastructure/persistent", None),
    ("infrastructure/ephemeral", None),
    ("private_internet", None),
    ("sandbox", lambda key: key if key == "sandbox_gw" else "sandbox"),
    ("devbox", _devbox_group),
]

# Groups whose hosts are addressed by bare name (untrusted; no DNS name), like
# the sandbox. Everything else gets the <name>.worms.internal form.
_BARE_NAME_GROUPS = {"sandbox", "devbox", "devbox_gw"}

DOMAIN = "worms.internal"
ANSIBLE_USER = "debian"


def build_inventory(infrastructure: dict, stacks: dict, sandbox: dict, controller: str = "",
                    worms_agent: bool = False) -> dict:  # fmt: skip
    """Assemble the generated-inventory structure from the stack outputs.

    `stacks` maps a VM stack name to its `ansible_hosts` output ({} if the
    stack is not applied). `controller` is the node's SSH target when Ansible
    runs elsewhere (a laptop), empty on the node itself. `worms_agent`: the VM
    connections use the agent in WORMS_SSH_AUTH_SOCK (expanded by ssh)."""
    missing = [key for key in PLATFORM_KEYS if key not in infrastructure]
    proxmox = dict(infrastructure.get("proxmox") or {})
    missing += [f"proxmox.{key}" for key in ("node", "host", "api_port") if not proxmox.get(key)]
    if missing:
        raise ValueError(f"the platform stack's `infrastructure` output lacks {', '.join(missing)}: "
                         "apply it again (`worms build platform`)")  # fmt: skip
    # Proxmox API port is numeric in HCL but arrives as int; keep it an int.
    proxmox["api_port"] = int(proxmox["api_port"])

    # Addressing (and, for devboxes, the trusted SSH gateway and the services block
    # reserved for the per-team devbox gateways: docs/04-dev-environment.md).
    group_vars = {key: infrastructure[key] for key in PLATFORM_KEYS}
    group_vars["proxmox"] = proxmox

    sandbox_addresses = {
        name: host.get("addresses", [])
        for name, host in sandbox.get("hosts", {}).items()
    }

    agent = ["-o", "IdentityFile=none", "-o", "IdentityAgent=" + ("${WORMS_SSH_AUTH_SOCK}" if worms_agent else "SSH_AUTH_SOCK")]
    children: dict = {}
    for stack, group_of in VM_STACKS:
        for key, vm in sorted(stacks.get(stack, {}).items()):
            group = group_of(key) if group_of else key
            hostname = vm["name"] if group in _BARE_NAME_GROUPS else f"{vm['name']}.{DOMAIN}"
            host = {
                "ansible_host": vm["management_ip"],
                "ansible_user": ANSIBLE_USER,
                "proxmox_vm_id": int(vm["vm_id"]),
                "opentofu_stack": stack,
            }
            host["ansible_ssh_common_args"] = " ".join(([f"-o ProxyJump={controller}"] if controller else []) + agent)
            if key in sandbox_addresses:
                host["sandbox_addresses"] = sandbox_addresses[key]
            # Optional per-VM facts a stack exports for Ansible (e.g. the
            # monitor's capture_mac). Never overrides the keys above.
            for var, value in sorted((vm.get("host_vars") or {}).items()):
                host.setdefault(var, value)
            children.setdefault(group, {"hosts": {}})["hosts"][hostname] = host

    # The node itself: local on the node; from a laptop over SSH as root, with
    # its host key checked (known_hosts), unlike the frequently rebuilt VMs.
    if controller:
        user, _, host = controller.rpartition("@")
        node = {"ansible_host": host, "ansible_user": user or "root", "ansible_host_key_checking": True,
                "ansible_ssh_common_args": " ".join(agent)}  # fmt: skip
    else:
        node = {"ansible_host": "127.0.0.1", "ansible_connection": "local"}
    children["proxmox_host"] = {"hosts": {"proxmox.local": node}}

    return {"all": {"vars": group_vars, "children": children}}


# --- minimal, deterministic YAML emitter (stdlib only) ----------------------


def _needs_quote(value: str) -> bool:
    if value == "":
        return True
    specials = set(":{}[],&*#?|-<>=!%@`\"'")
    return (
        value[0] in specials or any(c in value for c in ":#") or value.strip() != value
    )


def _emit_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _needs_quote(text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _emit(node, indent: int, out: list) -> None:
    pad = "  " * indent
    if isinstance(node, dict):
        if not node:
            out[-1] += " {}"
            return
        for key, val in node.items():
            if isinstance(val, dict) and val:
                out.append(f"{pad}{key}:")
                _emit(val, indent + 1, out)
            elif isinstance(val, list) and val:
                out.append(f"{pad}{key}:")
                _emit(val, indent + 1, out)
            elif isinstance(val, dict):
                out.append(f"{pad}{key}: {{}}")
            elif isinstance(val, list):
                out.append(f"{pad}{key}: []")
            else:
                out.append(f"{pad}{key}: {_emit_scalar(val)}")
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)) and item:
                out.append(f"{pad}-")
                _emit(item, indent + 1, out)
            else:
                out.append(f"{pad}- {_emit_scalar(item)}")


def to_yaml(data: dict) -> str:
    out: list[str] = []
    _emit(data, 0, out)
    return "\n".join(out) + "\n"


HEADER = (
    "# ============================================================\n"
    "# GENERATED by scripts/generate_inventory.py from OpenTofu.\n"
    "# DO NOT EDIT BY HAND. Change addresses in OpenTofu\n"
    "# (opentofu/shared/shared-variables.tf or the sandbox stack)\n"
    "# and re-run `worms step inventory`.\n"
    "# Use with: ansible-playbook -i inventory.yml -i inventory.generated.yml ...\n"
    "# ============================================================\n"
)


def summarize(inventory: dict) -> None:
    """Print the hosts Ansible will contact (sourced from the OpenTofu state)
    and the configured VMs that do not exist there (Ansible skips them)."""
    allv = inventory.get("all", {})
    gv = allv.get("vars", {})
    proxmox = gv.get("proxmox", {}) or {}
    groups = allv.get("children", {}) or {}

    line = "=" * 78
    print(line)
    print("Hosts Ansible will contact (from the OpenTofu state)")
    print(line)
    if proxmox:
        print(
            f"Proxmox host : {proxmox.get('host', '?')}:"
            f"{proxmox.get('api_port', '?')}  (node {proxmox.get('node', '?')})"
        )
        print("-" * 78)

    if gv.get("inventory_generated_at"):
        print(f"Generated    : {gv['inventory_generated_at']}  (worms step inventory to refresh)")
    for stack, state in (gv.get("opentofu_stacks") or {}).items():
        print(f"Stack        : {stack:<26} {state}")
    print("-" * 78)

    print(f"  {'group':<16} {'host':<30} {'management IP':<15} {'vmid':<6} stack")
    for group in sorted(groups):
        for name, host in sorted((groups[group].get("hosts") or {}).items()):
            print(
                f"  {group:<16} {name:<30} {host.get('ansible_host', '?'):<15} "
                f"{host.get('proxmox_vm_id', '?')!s:<6} {host.get('opentofu_stack', '?')}"
            )

    # Addressed in OpenTofu but no VM in any state: not deployed / commented out.
    expected = set(gv.get("management_ips", {}) or {}) | set(
        gv.get("private_internet_ips", {}) or {}
    )
    missing = sorted(expected - set(groups))
    if missing:
        print("-" * 78)
        print("Not in the OpenTofu state (Ansible skips them): " + ", ".join(missing))
    print(line)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repo_root)
    parser.add_argument("--tofu-bin", default="tofu")
    parser.add_argument(
        "--quiet", action="store_true", help="Do not print the configuration summary."
    )
    parser.add_argument(
        "--show",
        nargs="?",
        const="",
        default=None,
        metavar="FILE",
        help="Print the config summary from an existing generated "
        "inventory (default: the output path) and exit; does not "
        "run tofu or rewrite anything.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Inventory file to write (default: <repo>/ansible/inventory.generated.yml).",
    )
    parser.add_argument(
        "--platform-json",
        type=Path,
        default=None,
        help="Pre-dumped `tofu output -json` for the platform stack (testing).",
    )
    parser.add_argument(
        "--sandbox-json",
        type=Path,
        default=None,
        help="Pre-dumped `tofu output -json` for the sandbox stack (testing).",
    )
    parser.add_argument(
        "--require-sandbox",
        action="store_true",
        help="Fail if the sandbox stack has no state (default: allow empty).",
    )
    args = parser.parse_args()

    root = args.repo_root.resolve()
    platform_dir = root / "opentofu" / "stacks" / "platform"
    sandbox_dir = root / "opentofu" / "stacks" / "sandbox"
    output_path = args.output or (root / "ansible" / "inventory.generated.yml")

    # --show: just print the summary from an existing generated inventory.
    if args.show is not None:
        show_path = Path(args.show) if args.show else output_path
        if not show_path.exists():
            sys.exit(f"error: {show_path} not found; run `worms step inventory` first.")
        try:
            import yaml  # available on any Ansible control node
        except ImportError:
            sys.exit("error: PyYAML is required for --show (it ships with Ansible).")
        summarize(yaml.safe_load(show_path.read_text()))
        return

    infrastructure = load_outputs(
        platform_dir, "infrastructure", args.tofu_bin, args.platform_json
    )
    if not infrastructure.get("infrastructure_ips"):
        sys.exit(
            "error: platform stack has no 'infrastructure' output.\n"
            "       Apply it first: worms build platform"
        )

    stacks, status = {}, {}
    for stack, _ in VM_STACKS:
        stack_dir = root / "opentofu" / "stacks" / stack
        outputs = tofu_output(stack_dir, args.tofu_bin)
        if not outputs:
            status[stack] = "NOT APPLIED (no state or no outputs)"
            print(f"note: stack {stack}: {status[stack]}", file=sys.stderr)
        elif "ansible_hosts" not in outputs:
            # Its last apply stopped before the VMs (or predates the output): an
            # inventory without them would have the playbooks after it skip them.
            fix = UNFINISHED.get(stack, "`worms build infrastructure` applies it again and shows the error")
            sys.exit(f"error: stack {stack} has outputs ({', '.join(sorted(outputs))}) but no ansible_hosts: "
                     f"its last apply did not finish (its VMs are missing). To go on, {fix}")  # fmt: skip
        stacks[stack] = (outputs.get("ansible_hosts") or {}).get("value", {})
        status.setdefault(stack, f"applied, {len(stacks[stack])} VM(s)")
    sandbox = load_outputs(sandbox_dir, "sandbox", args.tofu_bin, args.sandbox_json)
    if args.require_sandbox and not sandbox.get("hosts"):
        sys.exit("error: --require-sandbox set but the sandbox stack has no hosts.")

    try:
        inventory = build_inventory(infrastructure, stacks, sandbox, os.environ.get("WORMS_SSH", ""),
                                    bool(os.environ.get("WORMS_SSH_AUTH_SOCK")))  # fmt: skip
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    inventory["all"]["vars"]["opentofu_stacks"] = status
    inventory["all"]["vars"]["inventory_generated_at"] = datetime.now().isoformat(timespec="seconds")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(HEADER + to_yaml(inventory))

    n_hosts = sum(
        len(g.get("hosts", {})) for g in inventory["all"]["children"].values()
    )
    print(f"wrote {output_path} ({n_hosts} hosts from the OpenTofu state)")

    if not args.quiet:
        summarize(inventory)


if __name__ == "__main__":
    main()
