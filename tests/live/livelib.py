"""Helpers of the live journey (tests/live/test_devbox_journey.py): the running platform's
addresses, the `worms` CLI, the database view, the browser, SSH as a team member, and the
report. Importing this file touches nothing.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LOGIN_SPACING_MS = 7000  # nginx allows 10 sign-ins a minute per address (group_vars/dashboard.yml)


class NotBaseState(Exception):
    """The platform is not in the state the journey starts from."""


def check_wipe_allowed(given: str | None, node: str) -> bool:
    """`--live-wipe-database=<node>`: the operator must name the node whose database gets
    wiped, so a command copied from another machine does nothing."""
    if not given:
        return False
    if given != node:
        raise NotBaseState(f"--live-wipe-database names {given!r}, but this node is {node!r}: nothing wiped")
    return True


def parse_key_values(text: str) -> dict[str, str]:
    """The key=value lines of on_vm.sh and vm_state.sh."""
    return dict(line.split("=", 1) for line in text.splitlines() if re.match(r"^[a-z_]+=", line))


def split_ssh_command(command: str) -> tuple[str, str, str]:
    """`ssh -A -J team@host:port root@target` as the dashboard prints it: (gateway user, public
    host:port, target address)."""
    match = re.fullmatch(r"ssh -A -J (\w+)@(\S+) root@(\S+)", command or "")
    if not match:
        raise ValueError(f"not the expected SSH command: {command!r}")
    return match.group(1), match.group(2), match.group(3)


def wait_for(what: str, probe, timeout: float, interval: float = 20.0):
    """Call probe() until it returns something true. Returns that, or raises at the timeout."""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"gave up after {timeout:.0f} s waiting for {what}")
        time.sleep(interval)


@dataclass
class Member:
    """One test user: their account, and the SSH key they register."""

    first: str
    last: str
    team: str
    otp: str = ""
    password: str = field(default_factory=lambda: secrets.token_urlsafe(15))
    config: str = ""
    public_key: str = ""

    @property
    def email(self) -> str:
        return f"{self.first.lower()}.{self.team}@example.org"

    @property
    def short(self) -> str:
        return self.first.lower()


class Platform:
    """The deployed platform, as seen from the Proxmox node."""

    def __init__(self, out: Path):
        self.out = out
        self.node = socket.gethostname()
        names = yaml.safe_load((REPO / "ansible/group_vars/all.yml").read_text())["dashboard_public_names"]
        if self.node not in names:
            raise NotBaseState(f"this node ({self.node}) is not in dashboard_public_names: the journey runs on a deployed Proxmox node")
        self.public_name = names[self.node]
        addressing = json.loads((REPO / "tests/fixtures/platform-outputs.json").read_text())["infrastructure"]["value"]
        self.dashboard_ip = addressing["infrastructure_ips"]["dashboard"]
        self.gateway_ip = addressing["ssh_gateway_ips"]["devbox_ssh"]
        self.apt_cache_ip = addressing["private_internet_ips"]["apt_cache"]["network"]
        networks = addressing["networks"]
        self.devbox_vm_network = networks["devbox_vms"]
        self.devbox_networks = [networks["devbox_vms"], networks["devbox_ssh"], networks["mgmt_devbox"], addressing["devbox_services_block"]]
        self.node_bin = shutil.which("node") or self._nvm_node()
        self.started = datetime.now()
        self._tmp = tempfile.TemporaryDirectory(prefix="worms-live-")
        self.tmp = Path(self._tmp.name)
        os.chmod(self.tmp, 0o700)
        self.agents: dict[str, tuple[int, str]] = {}

    @staticmethod
    def _nvm_node() -> str | None:
        found = sorted((Path.home() / ".nvm/versions/node").glob("*/bin/node"))
        return str(found[-1]) if found else None

    def close(self):
        for pid, _sock in self.agents.values():
            subprocess.run(["kill", str(pid)], capture_output=True)
        self._tmp.cleanup()

    # -- the administrator's console ----------------------------------------------------

    def worms(self, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
        """Output may hold a one-time password, so callers decide what to keep of it."""
        return subprocess.run([sys.executable, str(REPO / "src/manage.py"), *args], cwd=REPO, capture_output=True, text=True, timeout=timeout)

    def database(self) -> dict:
        proc = subprocess.run(
            [sys.executable, str(HERE / "db_view.py"), json.dumps(self.devbox_networks)],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        if proc.returncode:
            raise RuntimeError(f"database view failed: {proc.stderr[-400:]}")
        return json.loads(proc.stdout[proc.stdout.index("{"):])

    def vms(self) -> list[dict]:
        out = subprocess.run(["qm", "list"], capture_output=True, text=True, timeout=120, check=True).stdout
        return [{"id": int(line.split()[0]), "name": line.split()[1], "status": line.split()[2]} for line in out.splitlines()[1:] if line.strip()]

    def devbox_vms(self) -> list[dict]:
        return [vm for vm in self.vms() if vm["name"].startswith("devbox-")]

    def orphan_volumes(self) -> list[str]:
        """Volumes named after a VM that does not exist. Proxmox hands out that ID again, and
        creating the VM then fails with "dataset already exists"."""
        names = subprocess.run(["zfs", "list", "-H", "-o", "name"], capture_output=True, text=True, timeout=120).stdout.split()
        vm_ids = {vm["id"] for vm in self.vms()}
        return [name for name in names if (m := re.search(r"/vm-(\d+)-", name)) and int(m.group(1)) not in vm_ids]

    def watchdog_stops(self) -> int:
        since = self.started.strftime("%Y-%m-%d %H:%M:%S")
        out = subprocess.run(
            ["journalctl", "-u", "worms-firewall-watchdog", "--since", since, "--no-pager"],
            capture_output=True, text=True, timeout=120,
        ).stdout  # fmt: skip
        return out.count("stopping sandbox/devbox")

    def vms_without_firewall(self) -> list[str]:
        """Devbox VMs whose firewall file does not turn the firewall on (a replaced VM could lose it)."""
        bad = []
        for vm in self.devbox_vms():
            path = Path(f"/etc/pve/firewall/{vm['id']}.fw")
            if not path.exists() or "enable: 1" not in path.read_text():
                bad.append(vm["name"])
        return bad

    def mirror_filters_on_devbox_ports(self) -> int:
        """Devbox traffic is not mirrored to the monitor, by design (docs/requirements.md)."""
        filters = 0
        for port in Path("/sys/class/net").iterdir():
            if not re.match(r"(tap|fwpr)\d+", port.name) or not (port / "master").exists():
                continue
            if re.fullmatch(r"vmbr(7|9\d+)", (port / "master").resolve().name):
                out = subprocess.run(["tc", "filter", "show", "dev", port.name, "ingress"], capture_output=True, text=True).stdout
                filters += out.count("mirred")
        return filters

    # -- the browser --------------------------------------------------------------------

    def browser(self, action: str, **fields) -> dict:
        if not self.node_bin:
            raise RuntimeError("Node.js with Playwright is needed (docs/testing.md)")
        request = {
            "action": action, "public_name": self.public_name, "dashboard_ip": self.dashboard_ip,
            "out": str(self.out), "login_spacing_ms": LOGIN_SPACING_MS, **fields,
        }  # fmt: skip
        env = {**os.environ, "NODE_PATH": str(Path.home() / "node_modules")}
        proc = subprocess.run([self.node_bin, str(HERE / "dashboard.js")], input=json.dumps(request), capture_output=True, text=True, timeout=900, env=env)
        try:
            answer = json.loads(proc.stdout)
        except json.JSONDecodeError:
            answer = {"error": f"no answer: {proc.stderr[-300:]}"}
        if proc.returncode or "error" in answer:
            raise RuntimeError(f"browser step {action}: {answer.get('error')}")
        return answer

    # -- SSH, as a team member ----------------------------------------------------------

    def make_key(self, member: Member):
        key = self.tmp / f"key-{member.short}"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{member.short}@laptop", "-f", str(key)], check=True, stdin=subprocess.DEVNULL)
        member.public_key = (self.tmp / f"key-{member.short}.pub").read_text().strip()
        member.config = str(self.tmp / f"ssh-{member.short}.cfg")
        # A config file, not -o and -i: only the file reaches the ssh that -J starts.
        Path(member.config).write_text(
            f"Host *\n  IdentityFile {key}\n  IdentitiesOnly yes\n  IdentityAgent none\n  UserKnownHostsFile /dev/null\n"
            "  StrictHostKeyChecking no\n  LogLevel ERROR\n  BatchMode yes\n  ConnectTimeout 40\n"
        )

    def _agent(self, member: Member) -> str:
        """An agent holding the member's key, as forwarded to pivot from A to B and C."""
        if member.short not in self.agents:
            sock = str(self.tmp / f"agent-{member.short}")
            out = subprocess.run(["ssh-agent", "-a", sock, "-s"], capture_output=True, text=True, check=True).stdout
            pid = int(re.search(r"SSH_AGENT_PID=(\d+)", out).group(1))
            subprocess.run(["ssh-add", "-q", str(self.tmp / f"key-{member.short}")], env={**os.environ, "SSH_AUTH_SOCK": sock}, check=True, capture_output=True)
            self.agents[member.short] = (pid, sock)
        return self.agents[member.short][1]

    def ssh(self, member: Member, command: str, target: str | None = None, *, jump=True, extra=(), stdin: str | None = None, timeout=120) -> subprocess.CompletedProcess:
        """As the member, from the node. Through the gateway to `target`, or to the gateway itself."""
        gateway = f"team@{self.gateway_ip}"
        args = ["ssh", "-F", member.config, *extra]
        if jump and target:
            args += ["-J", gateway, target]
        else:
            args += [gateway]
        if command:
            args.append(command)
        try:
            return subprocess.run(args, input=stdin if stdin is not None else "", capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(args, 124, exc.stdout or "", f"timed out after {timeout} s")

    def on_vm(self, member: Member, chain: list[str], script: str, args: list[str], timeout=900) -> subprocess.CompletedProcess:
        """Run a script from this directory on the last VM of `chain`: the entry VM, then through
        it as a team pivots, with the member's forwarded agent."""
        hop = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=30"
        command = "sh -s -- " + " ".join(args)
        for address in reversed(chain[1:]):
            command = "ssh " + hop + " -A root@" + address + " " + _quote(command)
        agent = self._agent(member)
        return self.ssh(
            member, command, f"root@{chain[0]}", extra=["-o", f"IdentityAgent={agent}", "-A"],
            stdin=(HERE / script).read_text(), timeout=timeout,
        )  # fmt: skip


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


class Report:
    """The protocol of a run, in the same form as docs/e2e-run-*/protocol.md: written after
    every row, so a run that dies still leaves everything so far."""

    def __init__(self, out: Path):
        self.path = out / "report.md"
        self.failed = 0
        self.passed = 0
        self.path.write_text(f"# Live journey, {datetime.now():%Y-%m-%d %H:%M}\n")

    def _write(self, text: str):
        with self.path.open("a") as handle:
            handle.write(text)

    def stage(self, title: str, prior: str = ""):
        self._write(f"\n## {title}\n\n")
        if prior:
            self._write(f"**Prior state:** {prior}\n\n")
        self._write("| Time | Action | Expected | Actual | Result |\n|---|---|---|---|---|\n")

    def row(self, action: str, expected: str, actual, ok: bool):
        self.passed += ok
        self.failed += not ok
        actual = str(actual).replace("|", "\\|").replace("\n", " ")[:300]
        self._write(f"| {datetime.now():%H:%M:%S} | {action} | {expected} | {actual} | {'PASS' if ok else '**FAIL**'} |\n")

    def note(self, text: str):
        self._write(f"\n{text}\n\n| Time | Action | Expected | Actual | Result |\n|---|---|---|---|---|\n")

    def finish(self):
        self._write(f"\n## Result\n\n{self.passed} checks passed, {self.failed} failed.\n")
