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
in group_vars/<vm group>.yml. Re-run after every `tofu apply` (`make inventory`).

Standard library only, so it runs with the system Python on the control node.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import subprocess
import sys
from pathlib import Path


def tofu_output(stack_dir: Path, tofu_bin: str) -> dict:
    """Return the parsed `tofu output -json` map for a stack, or {} if the stack
    has no state yet (not applied)."""
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
VM_STACKS = [
    ("infrastructure/persistent", None),
    ("infrastructure/ephemeral", None),
    ("private_internet", None),
    ("sandbox", lambda key: key if key == "sandbox_gw" else "sandbox"),
]

DOMAIN = "worms.internal"
ANSIBLE_USER = "debian"


def build_inventory(infrastructure: dict, stacks: dict, sandbox: dict) -> dict:
    """Assemble the generated-inventory structure from the stack outputs.

    `stacks` maps a VM stack name to its `ansible_hosts` output ({} if the
    stack is not applied)."""
    proxmox = dict(infrastructure.get("proxmox", {}))
    # Proxmox API port is numeric in HCL but arrives as int; keep it an int.
    if "api_port" in proxmox:
        proxmox["api_port"] = int(proxmox["api_port"])

    group_vars = {
        "infrastructure_ips": infrastructure.get("infrastructure_ips", {}),
        "management_ips": infrastructure.get("management_ips", {}),
        "services_ips": infrastructure.get("services_ips", {}),
        "host_ips": infrastructure.get("host_ips", {}),
        "private_internet_ips": infrastructure.get("private_internet_ips", {}),
        "services_ports": infrastructure.get("services_ports", {}),
        "networks": infrastructure.get("networks", {}),
        "bridges": infrastructure.get("bridges", {}),
        # Devbox (docs/devbox.md): the trusted SSH gateway and the services
        # block reserved for the per-team devbox gateways. Defaulted so an older
        # platform output (without them) still produces a usable inventory.
        "ssh_gateway_ips": infrastructure.get("ssh_gateway_ips", {}),
        "ssh_gateway_public_port": infrastructure.get("ssh_gateway_public_port", 2222),
        "devbox_services_block": infrastructure.get("devbox_services_block", ""),
        "proxmox": proxmox,
    }

    sandbox_addresses = {
        name: host.get("addresses", [])
        for name, host in sandbox.get("hosts", {}).items()
    }

    children: dict = {}
    for stack, group_of in VM_STACKS:
        for key, vm in sorted(stacks.get(stack, {}).items()):
            group = group_of(key) if group_of else key
            hostname = vm["name"] if group == "sandbox" else f"{vm['name']}.{DOMAIN}"
            host = {
                "ansible_host": vm["management_ip"],
                "ansible_user": ANSIBLE_USER,
                "proxmox_vm_id": int(vm["vm_id"]),
                "opentofu_stack": stack,
            }
            if key in sandbox_addresses:
                host["sandbox_addresses"] = sandbox_addresses[key]
            # Optional per-VM facts a stack exports for Ansible (e.g. the
            # monitor's capture_mac). Never overrides the keys above.
            for var, value in sorted((vm.get("host_vars") or {}).items()):
                host.setdefault(var, value)
            children.setdefault(group, {"hosts": {}})["hosts"][hostname] = host

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
    "# and re-run `make inventory`.\n"
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
        print(f"Generated    : {gv['inventory_generated_at']}  (make inventory to refresh)")
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
            sys.exit(f"error: {show_path} not found; run `make inventory` first.")
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
            "       Apply it first: cd opentofu/stacks/platform && make apply"
        )

    stacks, status = {}, {}
    for stack, _ in VM_STACKS:
        stack_dir = root / "opentofu" / "stacks" / stack
        outputs = tofu_output(stack_dir, args.tofu_bin)
        if not outputs:
            status[stack] = "NOT APPLIED (no state or no outputs)"
        elif "ansible_hosts" not in outputs:
            # Applied before the output existed: its VMs would silently vanish.
            status[stack] = "NO ansible_hosts OUTPUT (run tofu apply in the stack)"
        stacks[stack] = (outputs.get("ansible_hosts") or {}).get("value", {})
        status.setdefault(stack, f"applied, {len(stacks[stack])} VM(s)")
        if not status[stack].startswith("applied"):
            print(f"warning: stack {stack}: {status[stack]}", file=sys.stderr)
    sandbox = load_outputs(sandbox_dir, "sandbox", args.tofu_bin, args.sandbox_json)
    if args.require_sandbox and not sandbox.get("hosts"):
        sys.exit("error: --require-sandbox set but the sandbox stack has no hosts.")

    inventory = build_inventory(infrastructure, stacks, sandbox)
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
