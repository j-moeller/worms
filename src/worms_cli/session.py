"""Where the CLI runs and how it reaches the platform.

Two modes, chosen by WORMS_SSH (~/.worms/config):

  laptop  WORMS_SSH=root@<node>. OpenTofu and Ansible run on this machine with
          the secrets of this checkout's vault (worms_cli/secrets.py); Ansible
          reaches the VMs through the node (ProxyJump, written into the
          generated inventory). The node keeps only the round agent's
          workspace (/opt/worms: the sandbox and devbox stacks and their
          state); steps for those run there over SSH (worms-node-run).
  node    WORMS_SSH empty: everything runs on this machine, the Proxmox node
          (the round agent itself, or an operator logged in on the node).

Every SSH connection the CLI starts (and OpenTofu's provider, and Ansible)
uses a private ssh-agent holding the WORMS key from the vault; it lives as long
as the CLI process. The key never touches the disk.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from worms_cli.secrets import SSH_KEY, SSH_PUBLIC_KEY, Vault, VaultError

NODE_WORKSPACE = "/opt/worms"
NODE_RUN = "/usr/local/sbin/worms-node-run"  # installed by ansible role round_agent
NODE_STACKS = frozenset({"sandbox", "devbox"})
NODE_PLAYBOOKS = frozenset({"SetupSandbox.yml", "SetupSSHGateway.yml", "SetupDevbox.yml"})
INTERNET_FLAG = "/run/worms/internet-allowed"


class SessionError(RuntimeError):
    pass


def for_config(config) -> "Session":
    """The one Session of this CLI process (one ssh-agent, one set of tunnels)."""
    session = getattr(config, "_worms_session", None)
    if session is None:
        session = Session(config)
        config._worms_session = session
    return session


def group_var(repo_root: Path, name: str, default: str) -> str:
    """A plain scalar from ansible/group_vars/all.yml (names, not secrets)."""
    text = (Path(repo_root) / "ansible" / "group_vars" / "all.yml").read_text()
    match = re.search(rf'^{re.escape(name)}:\s*"?([^"\n#]+?)"?\s*$', text, re.M)
    return match.group(1) if match else default


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:  # already gone
        pass


def ssh_host(target: str) -> str:
    """`root@host` / `host` / `root@host:port` -> host."""
    host = target.rsplit("@", 1)[-1]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class Session:
    def __init__(self, config):
        self.config = config
        self.laptop = bool(config.ssh)
        self.vault = Vault(config.repo_root)
        self._values = None
        self._agent = None  # {"SSH_AUTH_SOCK": ..., "SSH_AGENT_PID": ...}
        self._tmp = None
        self._node_ok = False

    # -- secrets ---------------------------------------------------------------

    def values(self) -> dict:
        if self._values is None:
            try:
                self.vault.ensure()  # generated secrets appear on first use
                self._values = self.vault.load()
            except (VaultError, OSError, subprocess.CalledProcessError) as exc:
                raise SessionError(f"vault: {exc}") from exc
        return self._values

    def has_vault(self) -> bool:
        # Laptop mode always works from the vault (it is created on first use);
        # on the node only if the operator keeps one there (the agent has none).
        return self.laptop or self.vault.exists()

    # -- ssh-agent with the WORMS key -------------------------------------------

    def agent_env(self) -> dict:
        if not self.has_vault():
            return {}
        if self._agent is None:
            if not shutil.which("ssh-agent"):
                raise SessionError("ssh-agent is not installed (OpenSSH client)")
            out = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True, check=True).stdout
            agent = dict(re.findall(r"(SSH_AUTH_SOCK|SSH_AGENT_PID)=([^;]+);", out))
            atexit.register(_stop, int(agent["SSH_AGENT_PID"]))
            add = subprocess.run(["ssh-add", "-q", "-"], input=self.values()[SSH_KEY], text=True,
                                 capture_output=True, env={**os.environ, **agent})  # fmt: skip
            if add.returncode != 0:
                raise SessionError(f"ssh-add of the WORMS key failed: {add.stderr.strip()}")
            self._agent = agent
        return dict(self._agent)

    def _tmpdir(self) -> Path:
        if self._tmp is None:
            self._tmp = Path(tempfile.mkdtemp(prefix="worms-"))
            atexit.register(shutil.rmtree, self._tmp, True)
        return self._tmp

    def public_key_file(self) -> Path:
        path = self._tmpdir() / "worms.pub"
        if not path.exists():
            path.write_text(self.values()[SSH_PUBLIC_KEY].strip() + "\n")
        return path

    # -- environments for the tools ---------------------------------------------

    def env(self) -> dict:
        """For ssh, ansible and the generator scripts."""
        env = dict(os.environ)
        if self.laptop:
            env["WORMS_SSH"] = self.config.ssh
        env.update(self.agent_env())
        return env

    def proxmox_api_url(self) -> str:
        if self.config.get("WORMS_PROXMOX_URL"):
            return self.config.get("WORMS_PROXMOX_URL")
        host = ssh_host(self.config.ssh) if self.laptop else "127.0.0.1"
        return f"https://{host}:8006/"

    def tofu_env(self) -> dict:
        """OpenTofu's inputs from the vault, in the process environment only.
        On the node without a vault (the round agent) its environment file
        already has them."""
        env = self.env()
        if not self.has_vault():
            return env
        token = self.values().get("vault_tofu_proxmox_api_token", "")
        if not token:
            raise SessionError("the Proxmox provisioning token is not in the vault: run `worms secrets set proxmox-token`")
        env["TF_VAR_proxmox_api_token"] = token
        env["TF_VAR_proxmox_api_url"] = self.proxmox_api_url()
        env["TF_VAR_ssh_public_key"] = str(self.public_key_file())
        return env

    # -- the node ----------------------------------------------------------------

    def ssh_opts(self) -> list[str]:
        """Exactly the WORMS key (from the agent; not ~/.ssh's defaults), as
        OpenTofu's provider uses it, so a check that passes here holds there."""
        opts = ["-o", "BatchMode=yes"]
        if self.has_vault():
            opts += ["-o", "IdentitiesOnly=yes", "-o", f"IdentityFile={self.public_key_file()}"]
        return opts

    def ssh_argv(self, command: str) -> list[str]:
        return ["ssh", *self.ssh_opts(), self.config.ssh, command]

    def check_node(self) -> None:
        """Laptop mode: the node must accept the WORMS key (`worms node trust`)."""
        if not self.laptop or self._node_ok:
            return
        proc = subprocess.run(self.ssh_argv("true"), env=self.env(), capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise SessionError(
                f"cannot log in to {self.config.ssh} with the WORMS key ({proc.stderr.strip()[:200]}). "
                "Authorize it once with `worms node trust`."
            )
        self._node_ok = True

    def node_argv(self, cwd: str, command: str) -> list[str]:
        """Run `command` in the node's workspace (<NODE_WORKSPACE>/<cwd>), with
        the round agent's environment and key (worms-node-run)."""
        return self.ssh_argv(f"{NODE_RUN} {shlex.quote(cwd)} {shlex.quote(command)}")

    def tunnel(self, host: str, port: int) -> int:
        """Laptop mode: forward a local port through the node to host:port
        (for the database and the Proxmox API, which accept the node only).
        Closed when the CLI exits. Returns the local port."""
        self.check_node()
        local = free_port()
        proc = subprocess.Popen(
            ["ssh", "-N", *self.ssh_opts(), "-o", "ExitOnForwardFailure=yes",
             "-L", f"127.0.0.1:{local}:{host}:{port}", self.config.ssh],
            env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        atexit.register(proc.terminate)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise SessionError(f"SSH tunnel to {host}:{port} failed: {proc.stderr.read().strip()[:200]}")
            try:
                socket.create_connection(("127.0.0.1", local), timeout=1).close()
                return local
            except OSError:
                time.sleep(0.2)
        raise SessionError(f"SSH tunnel to {host}:{port}: no connection after 20 s")

    def infrastructure(self) -> dict:
        """The platform stack's `infrastructure` output (addresses)."""
        stack = Path(self.config.repo_root) / "opentofu" / "stacks" / "platform"
        proc = subprocess.run(["tofu", f"-chdir={stack}", "output", "-json", "infrastructure"],
                              capture_output=True, text=True)  # fmt: skip
        if proc.returncode != 0:
            raise SessionError(f"no platform state here ({proc.stderr.strip()[:120]}): `worms build platform` first")
        return json.loads(proc.stdout)

    def pve_ca(self) -> str:
        """Path of the CA of the Proxmox API's own certificate (PVE's)."""
        if not self.laptop:
            return "/etc/pve/pve-root-ca.pem"
        path = self._tmpdir() / "pve-root-ca.pem"
        if not path.exists():
            self.check_node()
            proc = subprocess.run(self.ssh_argv("cat /etc/pve/pve-root-ca.pem"), env=self.env(),
                                  capture_output=True, text=True, timeout=30)  # fmt: skip
            if proc.returncode != 0:
                raise SessionError(f"cannot read the node's PVE CA: {proc.stderr.strip()[:120]}")
            path.write_text(proc.stdout)
        return str(path)

    def internet_allowed(self) -> bool:
        if not self.laptop:
            return os.path.exists(INTERNET_FLAG)
        self.check_node()
        proc = subprocess.run(self.ssh_argv(f"test -e {INTERNET_FLAG}"), env=self.env(), timeout=30)
        return proc.returncode == 0
