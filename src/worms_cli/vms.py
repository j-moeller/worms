"""The VMs `worms status`, `worms login` and `worms show` know: every VM on the node by its name,
with where it belongs and how to reach it.

Proxmox says which VMs exist (name, vmid, pool, state). The management address and login user
come from the Ansible inventories: this checkout's (the platform's VMs and the private internet)
and the round agent's workspace on the node (the devboxes, the SSH gateway, a round's sandbox).
A login uses the WORMS key in its own agent, through the node from a laptop, and never forwards
the agent: devbox and sandbox VMs are assumed to be rooted by a team.
"""

from __future__ import annotations

import os
import shlex

from worms_cli import output
from worms_cli.build import BuildError, Runner

DOMAIN = ".worms.internal"
UNTRUSTED_POOLS = ("sandbox", "devbox")
KINDS = {
    "persistent": "infrastructure (persistent)",
    "ephemeral": "service (snapshot `clean`)",
    "private_internet": "private internet (snapshot `clean`)",
    "devbox": "devbox (a team's)",
    "sandbox": "sandbox (a round's)",
}


def aliases(name: str) -> list[str]:
    """The names `worms login` accepts for a VM besides its own."""
    if name == "devbox-ssh-gateway":
        return ["ssh-gateway"]
    if name.startswith("devbox-") and name.endswith("-gw"):
        return [f"ssh-gateway-{name[len('devbox-'):-len('-gw')]}"]
    return []


class Catalogue:
    def __init__(self, config):
        self.config = config
        self.runner = Runner(config)
        self._hosts = None

    def vms(self) -> list[dict]:
        return sorted(self.runner.vms(), key=lambda vm: (vm.get("pool") or "", vm["name"]))

    def _inventory(self, text: str) -> dict:
        import yaml

        hosts = {}

        def walk(group, node):
            for name, host in ((node or {}).get("hosts") or {}).items():
                host = dict(host or {})
                host["group"] = group
                hosts[name.removesuffix(DOMAIN)] = host
            for child, sub in ((node or {}).get("children") or {}).items():
                walk(child, sub)

        walk("all", (yaml.safe_load(text) or {}).get("all"))
        return hosts

    def hosts(self) -> dict:
        """name -> inventory entry (ansible_host, ansible_user, group, opentofu_stack)."""
        if self._hosts is None:
            hosts = {}
            local = self.config.repo_root / "ansible" / "inventory.generated.yml"
            if local.exists():
                hosts.update(self._inventory(local.read_text()))
            code, out, _ = self.runner.session.node_run(
                ["sh", "-c", 'cat "$(sed -n "s/^REPO_ROOT=//p" /etc/worms/round-agent.env)/ansible/inventory.generated.yml"'])
            if code == 0:
                for name, host in self._inventory(out).items():
                    hosts.setdefault(name, host)
            hosts.pop("proxmox.local", None)
            self._hosts = hosts
        return self._hosts

    def names(self) -> list[tuple[str, str]]:
        """(login name, what it is) for every VM there is now."""
        out = [("node", "the Proxmox node (root)")]
        for vm in self.vms():
            kind = KINDS.get(vm.get("pool") or "", vm.get("pool") or "")
            state = vm.get("status", "?")
            for name in [vm["name"], *aliases(vm["name"])]:
                out.append((name, f"{kind}, {state}" + ("" if name == vm["name"] else f" (= {vm['name']})")))
        return out

    def resolve(self, name: str) -> dict:
        """The VM a login name means."""
        vms = {vm["name"]: vm for vm in self.vms()}
        for vm_name in vms:
            if name == vm_name or name in aliases(vm_name):
                return vms[vm_name]
        raise BuildError(f"no VM {name!r}: `worms login --help` lists them")

    def ssh_argv(self, vm: dict, command=None, interactive=True) -> tuple[list[str], dict]:
        host = self.hosts().get(vm["name"])
        if host is None or not host.get("ansible_host"):
            raise BuildError(f"{vm['name']} has no management address in the inventories: `worms step inventory`")
        argv = ["ssh", "-o", "IdentityFile=none", "-o", "IdentityAgent=SSH_AUTH_SOCK", "-o", "ForwardAgent=no",
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                "-o", "ConnectTimeout=10"]  # fmt: skip
        if self.runner.session.laptop:
            argv += ["-o", f"ProxyJump={self.config.ssh}"]
        if not interactive:
            argv += ["-o", "BatchMode=yes"]
        argv += (["-t"] if interactive else []) + [f"{host.get('ansible_user', 'debian')}@{host['ansible_host']}"]
        if command:
            argv.append(command)
        return argv, self.runner.session.env()

    def login(self, name: str) -> int:
        if name == "node":
            if not self.runner.session.laptop:
                output.error("this is the node")
                return 1
            os.execvpe("ssh", ["ssh", "-t", self.config.ssh], os.environ)
        vm = self.resolve(name)
        if vm.get("status") != "running":
            raise BuildError(f"{vm['name']} is {vm.get('status')}: start it first (`qm start {vm['vmid']}` on the node)")
        if vm.get("pool") in UNTRUSTED_POOLS:
            output.warn(f"{vm['name']} is a team VM, assumed rooted by the team: type nothing secret there")
        argv, env = self.ssh_argv(vm)
        print(output.color(f"$ {shlex.join(argv)}   (sudo -i for root)", "dim"), flush=True)
        os.execvpe("ssh", argv, env)
        return 0  # not reached

    def reachable(self, vm: dict) -> str:
        try:
            argv, env = self.ssh_argv(vm, "true", interactive=False)
        except BuildError as exc:
            return str(exc)
        import subprocess

        try:
            proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return "no answer in 20 s"
        return "yes" if proc.returncode == 0 else (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1][:120]

    def details(self, name: str) -> list[list[str]]:
        """`worms show <vm>`: rows of (what, value)."""
        from worms_cli import components

        vm = self.resolve(name)
        node, vmid = vm["node"], vm["vmid"]
        config = self.runner._node_json("pvesh", "get", f"/nodes/{node}/qemu/{vmid}/config", "--output-format", "json")
        snapshots = [s for s in self.runner._node_json("pvesh", "get", f"/nodes/{node}/qemu/{vmid}/snapshot",
                                                         "--output-format", "json") if s.get("name") != "current"]  # fmt: skip
        host = self.hosts().get(vm["name"], {})
        component = components.COMPONENTS.get(vm["name"])
        uptime = int(vm.get("uptime") or 0)
        rows = [
            ["name", vm["name"] + (f" (also: {', '.join(aliases(vm['name']))})" if aliases(vm["name"]) else "")],
            ["vmid", f"{vmid} on {node}"],
            ["kind", KINDS.get(vm.get("pool") or "", vm.get("pool") or "(no pool)")],
            ["state", output.status_cell("running" if vm.get("status") == "running" else "stopped",
                                         f"{vm.get('status')}" + (f", up {uptime // 3600} h {uptime % 3600 // 60} min" if uptime else ""))],  # fmt: skip
            ["resources", f"{config.get('cores', '?')} core(s) ({config.get('cpu', 'default')}), {config.get('memory', '?')} MB"
                          + (f", balloon {config['balloon']} MB" if config.get("balloon") else "")],  # fmt: skip
            ["disks", "; ".join(f"{k}: {v.split(',')[0]} {_size(v)}" for k, v in sorted(config.items())
                                if k.rstrip("0123456789") in ("scsi", "virtio", "sata", "ide") and "media=cdrom" not in v)],  # fmt: skip
            ["networks", "; ".join(f"{k}: {_bridge(v)}" for k, v in sorted(config.items()) if k.startswith("net"))],
            ["management", f"{host.get('ansible_host', '?')} as {host.get('ansible_user', '?')}"
                           + (f" (inventory group {host['group']})" if host.get("group") else "")],  # fmt: skip
            ["OpenTofu", host.get("opentofu_stack", "?") + (f", {component.module}" if component and component.module else "")],
            ["snapshots", ", ".join(f"{s['name']} ({s.get('description', '').strip()})" for s in snapshots) or "none"],
            ["login", f"worms login {vm['name']}"],
        ]
        if component:
            rows.append(["build", f"worms build {component.name} [--rebuild]; needs {', '.join(component.deps) or 'nothing'}"])
        if vm.get("status") == "running":
            rows.append(["reachable", self.reachable(vm)])
        return rows


def _bridge(net: str) -> str:
    parts = dict(p.split("=", 1) for p in net.split(",") if "=" in p)
    extra = [f"rate {float(parts['rate']):g} MB/s"] if "rate" in parts else []
    return " ".join([parts.get("bridge", "?"), *extra, "firewall" if parts.get("firewall") == "1" else "no firewall"])


def _size(disk: str) -> str:
    parts = dict(p.split("=", 1) for p in disk.split(",") if "=" in p)
    return parts.get("size", "")
