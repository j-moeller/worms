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

SSH: one key, the WORMS key (vault). It is loaded into a private ssh-agent that
lives as long as the CLI process and never touches the disk, and every SSH login
of the build uses that agent and nothing else: the CLI's to the node, OpenTofu's
Proxmox provider (snippet uploads), Ansible to the node and the VMs, rsync. Your
own keys, a forwarded agent and ~/.ssh/id_* are never offered (IdentityFile=none,
IdentityAgent=SSH_AUTH_SOCK, SSH_AUTH_SOCK = the WORMS agent). Root on the node
has to accept the key: `worms node trust`, once, with your own login, says so
when it does not (check_node).
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
from subprocess import DEVNULL

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


def group_var(repo_root: Path, name: str) -> str:
    """A plain scalar from ansible/group_vars/all.yml (names, not secrets); an error if it is not there."""
    path = Path(repo_root) / "ansible" / "group_vars" / "all.yml"
    match = re.search(rf'^{re.escape(name)}:\s*"?([^"\n#]+?)"?\s*$', path.read_text(), re.M)
    if not match:
        raise SessionError(f"{name} is not set in {path}")
    return match.group(1)


def environments(repo_root: Path) -> dict:
    """The switch between the systems (ansible/group_vars/all.yml, worms_environments):
    node name -> {name, public_name, operator_ssh_port, ssh_gateway_public_port}."""
    try:
        import yaml  # ships with Ansible, which `worms` needs anyway
    except ImportError as exc:
        raise SessionError("PyYAML is missing (it comes with Ansible): install Ansible") from exc
    path = Path(repo_root) / "ansible" / "group_vars" / "all.yml"
    try:
        table = (yaml.safe_load(path.read_text()) or {}).get("worms_environments")
    except (OSError, yaml.YAMLError) as exc:
        raise SessionError(f"cannot read the systems from {path}: {exc}") from exc
    keys = {"name", "public_name", "operator_ssh_port", "ssh_gateway_public_port"}
    if not isinstance(table, dict) or not table or any(not isinstance(e, dict) or not keys <= set(e) for e in table.values()):
        raise SessionError(f"worms_environments in {path} is missing or incomplete (each system: {', '.join(sorted(keys))})")
    return table


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


# Set in the environment, OpenTofu's Proxmox provider would log in to the node with
# these instead of the WORMS key in the agent (bpg/proxmox).
PROVIDER_SSH_OVERRIDES = ("PROXMOX_VE_SSH_PRIVATE_KEY", "PROXMOX_VE_SSH_AUTH_SOCK", "PROXMOX_VE_SSH_PASSWORD",
                          "PROXMOX_VE_SSH_USERNAME")  # fmt: skip


class Session:
    def __init__(self, config):
        self.config = config
        self.laptop = bool(config.ssh)
        self.vault = Vault(config.repo_root)
        # The round agent's workspace is never a controller, whatever files are in it.
        self.workspace = Path(config.repo_root).resolve() == Path(NODE_WORKSPACE)
        self._values = None
        self._agent = None  # the WORMS key's agent: {"SSH_AUTH_SOCK": ..., "SSH_AGENT_PID": ...}
        self._tmp = None
        self._node_ok = False
        self._node_name = None
        self._environment = None

    # -- secrets ---------------------------------------------------------------

    def values(self) -> dict:
        """The vault, complete (Vault.check): a missing secret is an error that says how to
        fix it, never generated on the side (`worms build secrets` does that)."""
        if self._values is None:
            try:
                values = self.vault.load()
                self.vault.check(values)
            except (VaultError, OSError, subprocess.CalledProcessError) as exc:
                raise SessionError(f"vault: {exc}") from exc
            self._values = values
        return self._values

    def has_vault(self) -> bool:
        """Whether this is a controller: laptop mode always works from the vault (values()
        says how to make one); on the node only a checkout with one. The round agent's
        workspace never: a vault there (once made by mistake, 2026-11-06) is ignored, or
        every job would try to sync the workspace from itself."""
        if self.workspace:
            if self.laptop:
                raise SessionError(f"WORMS_SSH is set in the round agent's workspace ({NODE_WORKSPACE}), which is the node: "
                                   "empty it (/etc/worms/round-agent.env does)")  # fmt: skip
            return False
        return self.laptop or self.vault.exists()

    # -- ssh-agents ----------------------------------------------------------------

    def worms_agent(self) -> dict:
        """A private ssh-agent holding the WORMS key (started once)."""
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
        """For ssh, OpenTofu, Ansible and the generator scripts: SSH_AUTH_SOCK (and
        WORMS_SSH_AUTH_SOCK, which the generated inventory names) is the WORMS key's
        agent, whatever agent you have: the build logs in with that key only. Without
        a vault (the round agent in its workspace) worms-node-run has set it already."""
        env = dict(os.environ)
        if self.laptop:
            env["WORMS_SSH"] = self.config.ssh
        if self.has_vault():
            worms = self.worms_agent()
            env.update(worms)
            env["WORMS_SSH_AUTH_SOCK"] = worms["SSH_AUTH_SOCK"]
        return env

    def proxmox_api_url(self) -> str:
        if self.config.get("WORMS_PROXMOX_URL"):
            return self.config.get("WORMS_PROXMOX_URL")
        host = ssh_host(self.config.ssh) if self.laptop else "127.0.0.1"
        return f"https://{host}:8006/"

    def stray_tofu_credentials(self) -> list[Path]:
        root = Path(self.config.repo_root) / "opentofu"
        return sorted(p for p in root.rglob("*.auto.tfvars") if ".terraform" not in p.parts)

    def tofu_env(self) -> dict:
        """OpenTofu's inputs from the vault, in the process environment only.
        On the node without a vault (the round agent) its environment file
        already has them."""
        if not self.has_vault():
            return self.env()
        stray = self.stray_tofu_credentials()
        if stray:
            # OpenTofu prefers *.auto.tfvars to TF_VAR_*: the vault would silently lose.
            raise SessionError(f"{stray[0]} overrides the vault's token for OpenTofu: remove it ({len(stray)} file(s))")
        env = self.env()
        overrides = [name for name in PROVIDER_SSH_OVERRIDES if env.get(name)]
        if overrides:
            raise SessionError(
                f"{', '.join(overrides)} is set in your environment: OpenTofu would log in to the node with it "
                "instead of the WORMS key. Unset it"
            )
        # The provider logs in to root@<node> with the agent's key; say so here
        # rather than let it fail with "unable to authenticate".
        self.check_node()
        token = self.values().get("vault_tofu_proxmox_api_token", "")
        if not token:
            raise SessionError("the Proxmox provisioning token is not in the vault: `worms secrets issue proxmox-token`")
        env["TF_VAR_proxmox_api_token"] = token
        env["TF_VAR_proxmox_api_url"] = self.proxmox_api_url()
        env["TF_VAR_ssh_public_key"] = str(self.public_key_file())
        env["TF_VAR_proxmox_instance"] = self.node_name()
        # The provider's SSH login to the node: on the node itself over loopback, from a
        # laptop under the system's public name; the port is the system's (the switch).
        env["TF_VAR_node_ssh_address"] = self.environment()["public_name"] if self.laptop else "127.0.0.1"
        env["TF_VAR_node_ssh_port"] = str(self.environment()["operator_ssh_port"])
        # Not a secret, but the stacks' default (no access) would hide that it is missing.
        env["TF_VAR_llm_access_ip"] = group_var(self.config.repo_root, "llm_access_ip")
        return env

    def node_name(self) -> str:
        """The Proxmox node's name, from the node itself, for OpenTofu (proxmox_instance):
        never a default, which would build on whichever node it names. A name set in the
        environment (TF_VAR_proxmox_instance) must be that one."""
        if self._node_name is None:
            code, out, err = self.node_run(["hostname", "-s"])
            name = out.strip()
            if code != 0 or not name:
                raise SessionError(f"cannot read the node's name (`hostname -s`): {err.strip()[:200] or 'empty'}")
            wanted = os.environ.get("TF_VAR_proxmox_instance")
            if wanted and wanted != name:
                raise SessionError(f"TF_VAR_proxmox_instance={wanted} is set, but the node is {name}: unset it")
            known = environments(self.config.repo_root)
            if name not in known:
                raise SessionError(
                    f"the node is called {name}, which is none of the systems in ansible/group_vars/all.yml "
                    f"(worms_environments: {', '.join(f'{node} = {e['name']}' for node, e in known.items())})"
                )
            self._node_name = name
        return self._node_name

    def environment(self) -> dict:
        """Which system this is (the switch, by the node's name): production or development,
        with its public name and ports. Django's DEBUG has nothing to do with it."""
        if self._environment is None:
            self._environment = {"node": self.node_name(), **environments(self.config.repo_root)[self.node_name()]}
        return self._environment

    # -- the node ----------------------------------------------------------------

    def ssh_opts(self) -> list[str]:
        """To the node: the WORMS key in the agent and nothing else (no key file, not an
        IdentityAgent from ~/.ssh/config), never a prompt, and no long wait: every
        command starts by asking the node about its internet access."""
        return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "IdentityFile=none",
                "-o", "IdentityAgent=SSH_AUTH_SOCK"]  # fmt: skip

    def ssh_argv(self, command: str) -> list[str]:
        return ["ssh", *self.ssh_opts(), self.config.ssh, command]

    def check_node(self) -> None:
        """Root on the node accepts the WORMS key, as OpenTofu's provider logs in with it:
        from a laptop, a login with that key alone; on the node, the key in root's
        authorized_keys. Without a vault (the round agent) there is nothing to check here."""
        if self._node_ok or not self.has_vault():
            return
        if not self.laptop:
            # As the provider will: root@127.0.0.1 on this system's sshd port, the WORMS key alone.
            port = str(self.environment()["operator_ssh_port"])
            argv = ["ssh", *self.ssh_opts(), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "LogLevel=ERROR", "-p", port, "root@127.0.0.1", "true"]  # fmt: skip
            proc = subprocess.run(argv, env=self.env(), stdin=DEVNULL, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                raise SessionError(
                    f"cannot log in to root@127.0.0.1 port {port} with the WORMS key: {proc.stderr.strip()[:200].rstrip('.')}. "
                    "OpenTofu, Ansible and the round agent log in with it. Let it in once: `worms node trust`; "
                    f"if it is in and this still fails, sshd does not listen on {port} "
                    "(ansible/group_vars/all.yml, worms_environments: operator_ssh_port)"
                )
            self._node_ok = True
            return
        argv = ["ssh", *self.ssh_opts(), self.config.ssh, "true"]
        # No stdin: ssh would read it, and `worms secrets set ssh-key < key` needs it.
        proc = subprocess.run(argv, env=self.env(), stdin=DEVNULL, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise SessionError(
                f"cannot log in to {self.config.ssh} with the WORMS key: {proc.stderr.strip()[:200].rstrip('.')}. "
                "The build logs in with that key only. Let it in once, with your own login: `worms node trust`."
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

    def node_run(self, argv: list) -> tuple:
        """A command on the Proxmox node: (returncode, stdout, stderr), all captured. Run
        right here on the node it logs in nowhere, so it needs no key (`worms secrets
        issue` runs before the vault is complete)."""
        env = dict(os.environ)
        if self.laptop:
            self.check_node()
            argv, env = self.ssh_argv(shlex.join(argv)), self.env()
        proc = subprocess.run(argv, env=env, stdin=DEVNULL, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def token_permissions(self):
        """(secret-free factory) permissions(token, secret, path): GET
        /access/permissions as that token, TLS verified against PVE's CA."""
        import ssl
        import urllib.error
        import urllib.parse
        import urllib.request

        from worms_cli.tokens import ApiError

        port = self.tunnel("127.0.0.1", 8006) if self.laptop else 8006
        context = ssl.create_default_context(cafile=self.pve_ca())

        def permissions(spec, secret: str, path: str) -> dict:
            query = urllib.parse.urlencode({"path": path})
            request = urllib.request.Request(
                f"https://127.0.0.1:{port}/api2/json/access/permissions?{query}",
                headers={"Authorization": f"PVEAPIToken={spec.full_id}={secret}"},
            )
            try:
                with urllib.request.urlopen(request, context=context, timeout=15) as response:  # noqa: S310
                    return json.load(response).get("data") or {}
            except urllib.error.HTTPError as exc:
                raise ApiError(exc.code) from None

        return permissions

    def internet_allowed(self) -> bool:
        if not self.laptop:
            return os.path.exists(INTERNET_FLAG)
        self.check_node()
        proc = subprocess.run(self.ssh_argv(f"test -e {INTERNET_FLAG}"), env=self.env(), stdin=DEVNULL, capture_output=True, text=True, timeout=30)
        if proc.returncode not in (0, 1):  # 255: ssh failed, which says nothing about the flag
            raise SessionError(f"could not ask {self.config.ssh}: {proc.stderr.strip()[:120]}")
        return proc.returncode == 0
