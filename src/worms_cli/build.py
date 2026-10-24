"""Run the build pipeline's commands: OpenTofu, Ansible and the generator
scripts on this machine, and the steps of the node's own part (the sandbox and
devbox stacks, their playbooks) in the node's workspace over SSH. Which is
which, and each tool's environment, comes from worms_cli/session.py. The
pipeline itself (every former Makefile recipe) lives in worms_cli/pipeline.py.
"""

from __future__ import annotations

import shlex
import subprocess
import sys

from worms_cli import output
from worms_cli.session import NODE_PLAYBOOKS, NODE_RUN, NODE_STACKS, SessionError, for_config


class BuildError(RuntimeError):
    pass


class Runner:
    def __init__(self, config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.repo_root = config.repo_root
        self.session = for_config(config)
        self._node_ready = False

    def _run(self, argv, label: str, where: str, env, check: bool, quiet: bool) -> int:
        if not quiet:
            label = label if len(label) < 100 else label[:97] + "..."
            print(output.color(f"$ {label}  [{where}]", "dim"))
        if self.dry_run:
            print(output.color("  (dry run: not executed)", "yellow"))
            return 0
        try:  # argv and env may be callables: computed only when run
            proc = subprocess.run(argv() if callable(argv) else argv, env=env() if callable(env) else env)
        except SessionError as exc:
            raise BuildError(str(exc)) from exc
        if check and proc.returncode != 0:
            raise BuildError(f"command failed (exit {proc.returncode}): {label}")
        return proc.returncode

    def sh(self, command: str, cwd: str = ".", check: bool = True, quiet: bool = False, tofu: bool = False) -> int:
        """Run `command` (a shell string) in <repo>/<cwd> on this machine."""
        target = f"{self.repo_root}/{cwd}" if cwd not in (".", "") else str(self.repo_root)
        argv = ["sh", "-c", f"cd {shlex.quote(target)} && {command}"]
        env = self.session.tofu_env if tofu else self.session.env
        return self._run(argv, f"({cwd}) {command}", "locally", env, check, quiet)

    def node_sh(self, command: str, check: bool = True, quiet: bool = False) -> int:
        """Run a host command (qm, ...) on the Proxmox node."""
        if not self.session.laptop:
            return self.sh(command, check=check, quiet=quiet)
        return self._run(lambda: self._checked(self.session.ssh_argv(command)), command,
                         f"on {self.config.ssh}", self.session.env, check, quiet)  # fmt: skip

    def _checked(self, argv):
        self.session.check_node()
        return argv

    def _node_part(self) -> bool:
        """Whether the sandbox/devbox steps belong in the node's workspace: from
        a controller's checkout (it has the vault) yes; in the workspace itself
        (the round agent: no vault there) they run right here."""
        return self.session.has_vault()

    def _node_workspace(self, cwd: str, command: str, check: bool = True) -> int:
        """Run in the round agent's workspace on the node (over SSH from a
        laptop), after bringing it up to date with this checkout once per run:
        one state for the sandbox and devbox stacks, the agent's."""
        if not self._node_ready and not self.dry_run:
            # The agent's workspace: code, CA certificate, platform/firewall
            # outputs (ansible role round_agent, tasks/workspace.yml).
            self.ansible("SetupRoundAgent.yml", "--tags", "agent-sync")
            self._node_ready = True
        if not self.session.laptop:
            return self._run(["sh", "-c", f"{NODE_RUN} {shlex.quote(cwd)} {shlex.quote(command)}"],
                             f"({cwd}) {command}", "in the agent's workspace", self.session.env, check, False)  # fmt: skip
        argv = lambda: self._checked(self.session.node_argv(cwd, command))  # noqa: E731
        return self._run(argv, f"({cwd}) {command}", f"on {self.config.ssh}", self.session.env, check, False)

    def _check_inventory(self) -> None:
        """From a laptop, never with an inventory written on the node: it makes
        the node `ansible_connection: local`, which would run the node's roles
        on this machine."""
        inventory = self.repo_root / "ansible" / "inventory.generated.yml"
        if not inventory.exists():
            raise BuildError("no ansible/inventory.generated.yml: run `worms build inventory` first")
        if "ansible_connection: local" in inventory.read_text():
            raise BuildError("ansible/inventory.generated.yml was written on the node: run `worms build inventory` here")

    # The tools the pipeline drives.

    def tofu(self, stack: str, *args: str, check: bool = True) -> int:
        command = "tofu " + " ".join(args)
        if stack in NODE_STACKS and self._node_part():
            if args and args[0] == "init":
                # Initialised on the node whenever the workspace is synced
                # (tasks/workspace.yml), which needs the inventory and the CA:
                # not there yet when a first `worms build` starts with init.
                print(output.color(f"$ (opentofu/stacks/{stack}) {command}  [done on the node by agent-sync]", "dim"))
                return 0
            return self._node_workspace(f"opentofu/stacks/{stack}", command, check=check)
        return self.sh(command, cwd=f"opentofu/stacks/{stack}", check=check, tofu=True)

    def ansible(self, playbook: str, *args: str, vault: bool = True) -> int:
        extra = (" " + " ".join(args)) if args else ""
        if playbook in NODE_PLAYBOOKS and self._node_part():
            # The node's own inventory (its stacks) and no vault: these plays
            # need neither the controller's secrets nor its VMs.
            return self._node_workspace(
                "ansible", f"python3 ../scripts/generate_inventory.py --quiet && ansible-playbook {playbook}{extra}"
            )
        base = "ansible-playbook"
        if vault and self.session.has_vault():
            base += " -e @group_vars/vault.yml"
        if self.session.laptop and not self.dry_run:
            self._check_inventory()
            # The node's internet flag (group_vars/all.yml can only test the
            # controller's own filesystem).
            base += f" -e internet_enabled={str(self._internet_allowed()).lower()}"
        return self.sh(f"{base} {playbook}{extra}", cwd="ansible")

    def _internet_allowed(self) -> bool:
        try:
            return self.session.internet_allowed()
        except SessionError as exc:
            raise BuildError(str(exc)) from exc

    def secrets(self) -> None:
        """Generate the missing generated secrets; report the entered ones
        that are missing (`worms secrets set <name>`)."""
        print(output.color(f"$ (vault) generate missing secrets  [{self.session.vault.file}]", "dim"))
        if self.dry_run:
            print(output.color("  (dry run: not executed)", "yellow"))
            return
        try:
            added = self.session.vault.ensure()
            rows = self.session.vault.status()
        except Exception as exc:  # noqa: BLE001  (VaultError, ssh-keygen/ansible-vault failures)
            raise BuildError(f"vault: {exc}") from exc
        print(f"secrets: {len(added)} generated" if added else "secrets: nothing to generate")
        for name, _what, state in rows:
            if state.startswith("MISSING"):
                output.warn(f"{name}: {state} -- `worms secrets set {name}`")

    def python(self, script: str, *args: str) -> int:
        extra = (" " + " ".join(shlex.quote(a) for a in args)) if args else ""
        return self.sh(f"python3 {script}{extra}")

    def flag_exists(self, path: str) -> bool:
        """Whether a file exists on the node (for the internet flag check)."""
        if self.dry_run:
            return False  # nothing was actually run; don't block the dry run
        if not self.session.laptop:
            return self.sh(f"test -e {shlex.quote(path)}", check=False, quiet=True) == 0
        return self.node_sh(f"test -e {shlex.quote(path)}", check=False, quiet=True) == 0


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        output.error("refusing a destructive action without a TTY; pass --yes to confirm")
        return False
    answer = input(output.color(f"{prompt} [y/N] ", "yellow")).strip().lower()
    return answer == "y"
