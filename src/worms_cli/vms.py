"""The VMs `worms status`, `worms login`, `worms show`, `worms log` and `worms rsync` know: every
VM on the node by its name, with where it belongs, which team's it is, and how to reach it.

A VM is named by its own name, an alias (below), or `<team>/<role>` (`alpha/gw`, `t2/a`: the team
map, worms_cli/teams.py).

Proxmox says which VMs exist (name, vmid, pool, state). The management address and login user
come from the Ansible inventories: this checkout's (the platform's VMs and the private internet)
and the round agent's workspace on the node (the devboxes, the SSH gateway, a round's sandbox).
A login uses the WORMS key in its own agent, through the node from a laptop, and never forwards
the agent: devbox and sandbox VMs are assumed to be rooted by a team.
"""

from __future__ import annotations

import os
import shlex

from worms_cli import output, services, teams
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
    """The names `worms login` accepts for a VM besides its own and `<team>/<role>`. The old
    `ssh-gateway-t<n>` is the team's NAT gateway devbox-t<n>-gw, not the shared SSH gateway:
    still accepted, but `<team>/gw` is what the help shows (shown_aliases)."""
    if name == teams.SHARED_GATEWAY:
        return ["ssh-gateway"]
    if name.startswith("devbox-") and name.endswith("-gw"):
        return [f"ssh-gateway-{name[len('devbox-'):-len('-gw')]}"]
    return []


def shown_aliases(name: str) -> list[str]:
    return [alias for alias in aliases(name) if not alias.startswith("ssh-gateway-")]


class Catalogue:
    def __init__(self, config):
        self.config = config
        self.runner = Runner(config)
        self._hosts = None
        self._vms = None
        self._teams = None

    def vms(self) -> list[dict]:
        if self._vms is None:
            self._vms = sorted(self.runner.vms(), key=lambda vm: (vm.get("pool") or "", vm["name"]))
        return self._vms

    def teams(self) -> dict:
        """slug -> teams.Team: the database's, or the devbox VMs' (worms_cli/teams.py)."""
        if self._teams is None:
            names = [vm["name"] for vm in self.vms()]
            try:
                hosts = self.hosts()
            except Exception:  # noqa: BLE001  (the node's inventory is a fallback's fallback)
                hosts = {}
            self._teams = teams.team_map(self.config, names, hosts)
        return self._teams

    def team_of(self, name: str):
        """The teams.Team whose devbox VM `name` is, or None."""
        found = teams.of_vm(name)
        if found is None:
            return None
        return self.teams().get(found[0]) or teams.Team(found[0])

    def team_label(self, name: str) -> str:
        """The team column of a VM: `alpha (t2)`, `(every team)` for the shared SSH gateway."""
        if name == teams.SHARED_GATEWAY:
            return "(every team)"
        team = self.team_of(name)
        return team.label if team else ""

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

    def names(self) -> list[tuple[str, str, str]]:
        """(login name, team, what it is) for every VM there is now; a team's VM also by
        `<team>/<role>`, which is what to type."""
        out = [("node", "", "the Proxmox node (root)")]
        for vm in self.vms():
            kind = KINDS.get(vm.get("pool") or "", vm.get("pool") or "")
            state = vm.get("status", "?")
            team, found = self.team_of(vm["name"]), teams.of_vm(vm["name"])
            what = f"{kind}, {state}"
            if team and found:
                what = f"{team.handle}/{found[1]}: {teams.ROLE_HELP[found[1]]}, {state}"
            out.append((vm["name"], self.team_label(vm["name"]), what))
            for alias in shown_aliases(vm["name"]):
                out.append((alias, self.team_label(vm["name"]), f"= {vm['name']}"))
        return out

    def vm_name(self, name: str) -> str | None:
        """The VM's own name that a VM name, an alias or `<team>/<role>` means, or None."""
        vms = {vm["name"] for vm in self.vms()}
        for vm_name in sorted(vms):
            if name == vm_name or name in aliases(vm_name):
                return vm_name
        if "/" not in name:
            return None
        try:
            meant = teams.target(self.teams(), name)
        except ValueError as exc:
            raise BuildError(str(exc)) from None
        if meant is not None and meant not in vms:
            raise BuildError(f"{name!r} is {meant}, which is not there (`worms show {name.rpartition('/')[0]}`)")
        return meant

    def knows(self, name: str) -> bool:
        """Whether `name` is a VM there is now (or the node), by any of its names."""
        if name == "node":
            return True
        try:
            return self.vm_name(name) is not None
        except BuildError:
            return False

    def find_team(self, text: str):
        """The teams.Team `text` names (its slug or its name), or None."""
        try:
            return teams.find(self.teams(), text)
        except ValueError as exc:
            raise BuildError(str(exc)) from None

    def team_rows(self, team, key_cells: dict) -> list[list[str]]:
        """`worms show <team>`: its VMs a, b, c and gw, and the shared SSH gateway's lines for it:
        VM, vmid, management address, state, the team's keys there (`key_cells`, keys.team_cells)."""
        vms = {vm["name"]: vm for vm in self.vms()}
        try:
            hosts = self.hosts()
        except Exception:  # noqa: BLE001  (the addresses are a detail; the node may be out of reach)
            hosts = {}
        rows = []
        for role in teams.ROLES:
            name = teams.vm_name(team.slug, role)
            vm = vms.get(name)
            if vm is None:
                rows.append([f"{team.handle}/{role}", name, "", "", output.status_cell("n/a", "— not there"), ""])
                continue
            state = vm.get("status", "?")
            keys = key_cells.get(role, "") if role != "gw" else output.color("(no team key: its NAT gateway)", "dim")
            rows.append([f"{team.handle}/{role}", name, str(vm["vmid"]), hosts.get(name, {}).get("ansible_host", "?"),
                         output.status_cell("running" if state == "running" else "stopped", state), keys])  # fmt: skip
        gateway = vms.get(teams.SHARED_GATEWAY)
        rows.append(["ssh-gateway", f"{teams.SHARED_GATEWAY} (shared)", str(gateway["vmid"]) if gateway else "",
                     hosts.get(teams.SHARED_GATEWAY, {}).get("ansible_host", "") if gateway else "",
                     output.status_cell("running" if gateway and gateway.get("status") == "running" else "stopped",
                                        gateway.get("status", "?") if gateway else "— not there"),
                     key_cells.get("gateway", "")])  # fmt: skip
        return rows

    def resolve(self, name: str) -> dict:
        """The VM a login name means: its name, an alias, or `<team>/<role>`."""
        vm_name = self.vm_name(name)
        if vm_name is None:
            raise BuildError(f"no VM {name!r}: `worms login --help` lists them; a team's is <team>/a|b|c|gw")
        return next(vm for vm in self.vms() if vm["name"] == vm_name)

    def destination(self, vm: dict) -> str:
        """login user@management address."""
        host = self.hosts().get(vm["name"])
        if host is None or not host.get("ansible_host"):
            raise BuildError(f"{vm['name']} has no management address in the inventories: `worms step inventory`")
        return f"{host.get('ansible_user', 'debian')}@{host['ansible_host']}"

    def ssh_options(self, interactive=True) -> list[str]:
        """ssh's options for a VM: the WORMS key in its own agent, through the node from a laptop."""
        options = ["-o", "IdentityFile=none", "-o", "IdentityAgent=SSH_AUTH_SOCK", "-o", "ForwardAgent=no",
                   "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                   "-o", "ConnectTimeout=10"]  # fmt: skip
        if self.runner.session.laptop:
            options += ["-o", f"ProxyJump={self.config.ssh}"]
        if not interactive:
            options += ["-o", "BatchMode=yes"]
        return options

    def ssh_argv(self, vm: dict, command=None, interactive=True) -> tuple[list[str], dict]:
        argv = ["ssh", *self.ssh_options(interactive), *(["-t"] if interactive else []), self.destination(vm)]
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
        team, found = self.team_of(vm["name"]), teams.of_vm(vm["name"])
        also = shown_aliases(vm["name"]) + ([f"{team.handle}/{found[1]}"] if team and found else [])
        rows = [
            ["name", vm["name"] + (f" (also: {', '.join(also)})" if also else "")],
            ["vmid", f"{vmid} on {node}"],
            ["kind", KINDS.get(vm.get("pool") or "", vm.get("pool") or "(no pool)")],
            *([["team", f"{team.label}: {teams.ROLE_HELP[found[1]]} (`worms show {team.handle}`: its four VMs)"]]
              if team and found else []),  # fmt: skip
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
        running = services.on(vm["name"])
        if running:
            rows.append(["services", ", ".join(f"{name} ({' '.join(services.SERVICES[name].units) or 'a log'})" for name in running)
                         + f": `worms log {running[0]}`"
                         + (f", `worms restart {running[0]}`" if services.SERVICES[running[0]].restartable else "")])  # fmt: skip
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
