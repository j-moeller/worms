#!/usr/bin/env python3
"""Generate the Ansible inventory from OpenTofu outputs (the ground truth).

OpenTofu owns all addressing:
  * static infrastructure IPs / subnets / Proxmox host come from the
    `infrastructure` output of the platform stack (which derives them from the
    single source of truth in opentofu/shared/shared-variables.tf);
  * dynamic sandbox VM addresses come from the `sandbox` output of the sandbox
    stack (opentofu/stacks/sandbox/generated_vms*.tf).

This script reads `tofu output -json` from those stacks and writes
`ansible/inventory.generated.yml`:

    all:
      vars:        # ground-truth addressing, referenced by inventory.yml
        infrastructure_ips: {...}
        private_internet_ips: {...}
        networks: {...}
        proxmox: {...}
      children:
        sandbox:   # dynamic hosts, enumerated from OpenTofu
          hosts:
            sandbox-vm01: {ansible_host: 10.3.0.111, ...}

Ansible is then run with both inventories, e.g.

    ansible-playbook -i inventory.yml -i inventory.generated.yml <playbook>

so the static, human-maintained groups/roles in inventory.yml keep working while
every IP is sourced from OpenTofu. Re-run this whenever the platform or sandbox
stack changes (`make inventory`).

Standard library only, so it runs with the system Python on the control node.
"""

from __future__ import annotations

import argparse
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


def load_outputs(stack_dir: Path, name: str, tofu_bin: str, override: Path | None) -> dict:
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


def build_inventory(infrastructure: dict, sandbox: dict) -> dict:
    """Assemble the generated-inventory structure from the stack outputs."""
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
        "networks": infrastructure.get("networks", {}),
        "bridges": infrastructure.get("bridges", {}),
        "proxmox": proxmox,
    }

    sandbox_hosts = {}
    for name, host in sorted(sandbox.get("hosts", {}).items()):
        sandbox_hosts[name] = {
            "ansible_host": host["maintenance_ip"],
            "ansible_user": "debian",
            "sandbox_addresses": host.get("addresses", []),
        }

    return {
        "all": {
            "vars": group_vars,
            "children": {
                "sandbox": {"hosts": sandbox_hosts},
            },
        }
    }


# --- minimal, deterministic YAML emitter (stdlib only) ----------------------

def _needs_quote(value: str) -> bool:
    if value == "":
        return True
    specials = set(':{}[],&*#?|-<>=!%@`"\'')
    return value[0] in specials or any(c in value for c in ":#") or value.strip() != value


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
    """Print a human-readable table of the configuration being set up
    (VM names + IP addresses), sourced from the OpenTofu ground truth."""
    allv = inventory.get("all", {})
    gv = allv.get("vars", {})
    infra = gv.get("infrastructure_ips", {}) or {}
    mgmt = gv.get("management_ips", {}) or {}
    pi = gv.get("private_internet_ips", {}) or {}
    proxmox = gv.get("proxmox", {}) or {}
    sandbox = (
        allv.get("children", {}).get("sandbox", {}).get("hosts", {}) or {}
    )

    line = "=" * 64
    print(line)
    print("Configuration (ground truth from OpenTofu)")
    print(line)

    if proxmox:
        print(
            f"Proxmox host : {proxmox.get('host', '?')}:"
            f"{proxmox.get('api_port', '?')}  (node {proxmox.get('node', '?')})"
        )
        print("-" * 64)

    print("Infrastructure VMs           data IP           management IP")
    for name in sorted(set(infra) | set(mgmt)):
        print(f"  {name:<26} {infra.get(name, '-'):<17} {mgmt.get(name, '-')}")

    if pi:
        print("-" * 64)
        print("Private-internet services    private IP        maintenance IP")
        for name in sorted(pi):
            svc = pi[name]
            print(
                f"  {name:<26} {svc.get('network', '?'):<16} "
                f"{svc.get('maintenance', '?')}"
            )

    print("-" * 64)
    if sandbox:
        print(f"Sandbox VMs ({len(sandbox)})            maintenance IP    addresses")
        for name in sorted(sandbox):
            host = sandbox[name]
            addrs = ", ".join(host.get("sandbox_addresses", []))
            print(f"  {name:<26} {host.get('ansible_host', '?'):<16} {addrs}")
    else:
        print("Sandbox VMs                  (none deployed)")
    print(line)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repo_root)
    parser.add_argument("--tofu-bin", default="tofu")
    parser.add_argument("--quiet", action="store_true",
                        help="Do not print the configuration summary.")
    parser.add_argument("--show", nargs="?", const="", default=None, metavar="FILE",
                        help="Print the config summary from an existing generated "
                             "inventory (default: the output path) and exit; does not "
                             "run tofu or rewrite anything.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Inventory file to write (default: <repo>/ansible/inventory.generated.yml).",
    )
    parser.add_argument("--platform-json", type=Path, default=None,
                        help="Pre-dumped `tofu output -json` for the platform stack (testing).")
    parser.add_argument("--sandbox-json", type=Path, default=None,
                        help="Pre-dumped `tofu output -json` for the sandbox stack (testing).")
    parser.add_argument("--require-sandbox", action="store_true",
                        help="Fail if the sandbox stack has no state (default: allow empty).")
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

    infrastructure = load_outputs(platform_dir, "infrastructure", args.tofu_bin, args.platform_json)
    if not infrastructure.get("infrastructure_ips"):
        sys.exit(
            "error: platform stack has no 'infrastructure' output.\n"
            "       Apply it first: cd opentofu/stacks/platform && make apply"
        )

    sandbox = load_outputs(sandbox_dir, "sandbox", args.tofu_bin, args.sandbox_json)
    if args.require_sandbox and not sandbox.get("hosts"):
        sys.exit("error: --require-sandbox set but the sandbox stack has no hosts.")

    inventory = build_inventory(infrastructure, sandbox)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(HEADER + to_yaml(inventory))

    n_infra = len(infrastructure.get("infrastructure_ips", {}))
    n_pi = len(infrastructure.get("private_internet_ips", {}))
    n_sandbox = len(sandbox.get("hosts", {}))
    print(
        f"wrote {output_path} "
        f"({n_infra} infrastructure IPs, {n_pi} private-internet services, "
        f"{n_sandbox} sandbox VMs)"
    )

    if not args.quiet:
        summarize(inventory)


if __name__ == "__main__":
    main()
