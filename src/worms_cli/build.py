"""Run the build pipeline's commands: OpenTofu, Ansible and the generator
scripts on this machine, and the steps of the host's own part (the sandbox and
devbox stacks, their playbooks) in the host's workspace over SSH. Which is
which, and each tool's environment, comes from worms_cli/session.py. The
pipeline itself (every former Makefile recipe) lives in worms_cli/pipeline.py.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from worms_cli import output
from worms_cli.session import HOST_PLAYBOOKS, HOST_RUN, HOST_STACKS, SessionError, check_known_hosts, for_config


class BuildError(RuntimeError):
    pass


class Runner:
    def __init__(self, config):
        self.config = config
        self.repo_root = config.repo_root
        self.session = for_config(config)
        self._host_ready = False

    def pending_migrations(self) -> list[str]:
        """The checkout's Django migrations the deployed database has not applied (read
        with the CLI's database access, django_setup)."""
        from worms_cli import django_setup

        try:
            django_setup.setup(self.config)
        except django_setup.NotConfigured as exc:
            raise BuildError(f"cannot check the database's migrations: {exc}") from None
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        return [f"{m.app_label}.{m.name}" for m, _ in executor.migration_plan(executor.loader.graph.leaf_nodes())]

    def _run(self, argv, label: str, where: str, env, check: bool, quiet: bool) -> int:
        if not quiet:
            label = label if len(label) < 100 else label[:97] + "..."
            print(output.color(f"$ {label}  [{where}]", "dim"))
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

    def host_sh(self, command: str, check: bool = True, quiet: bool = False) -> int:
        """Run a host command (qm, ...) on the Proxmox host."""
        if not self.session.laptop:
            return self.sh(command, check=check, quiet=quiet)
        return self._run(lambda: self._checked(self.session.ssh_argv(command)), command,
                         f"on {self.config.ssh}", self.session.env, check, quiet)  # fmt: skip

    def host_parallel(self, chains: list[list[str]]) -> list[int]:
        """Host commands on the host, a chain per VM: each chain's commands one after the other (the
        first failure ends it), all chains at once. Their exit codes, in order. One VM's shutdown
        does not wait for another's: N VMs take the time of the slowest, not of all."""
        chains = [chain for chain in chains if chain]
        if not chains:
            return []
        if len(chains) == 1:
            return [self.host_sh(" && ".join(chains[0]), check=False)]
        lines = ["set -u"]
        for i, chain in enumerate(chains):
            lines.append(f"( {' && '.join(chain)} ) >&2 & p{i}=$!")
        lines += [f'wait "$p{i}"; echo "worms-rc {i} $?"' for i in range(len(chains))]
        shown = "; ".join(" && ".join(chain) for chain in chains)
        print(output.color(f"$ (at once) {shown if len(shown) < 100 else shown[:97] + '...'}  [on the host]", "dim"), flush=True)
        try:
            code, out, err = self.session.host_run(["sh", "-c", "\n".join(lines)])
        except SessionError as exc:
            raise BuildError(str(exc)) from exc
        codes = {int(i): int(rc) for i, rc in (line.split()[1:3] for line in out.splitlines() if line.startswith("worms-rc "))}
        if err.strip():
            print(err.rstrip(), file=sys.stderr)
        return [codes.get(i, code or 1) for i in range(len(chains))]

    def _checked(self, argv):
        self.session.check_host()
        return argv

    def _host_part(self) -> bool:
        """Whether the sandbox/devbox steps belong in the host's workspace: from
        a controller's checkout (it has the vault) yes; in the workspace itself
        (the round agent: no vault there) they run right here."""
        return self.session.has_vault()

    def _host_workspace(self, cwd: str, command: str, check: bool = True) -> int:
        """Run in the round agent's workspace on the host (over SSH from a
        laptop), after bringing it up to date with this checkout once per run:
        one state for the sandbox and devbox stacks, the agent's."""
        if not self._host_ready:
            # The agent's workspace: code, CA certificate, platform/firewall
            # outputs (ansible role round_agent, tasks/workspace.yml).
            self.ansible("SetupRoundAgent.yml", "--tags", "agent-sync")
            self._host_ready = True
        if not self.session.laptop:
            return self._run(["sh", "-c", f"{HOST_RUN} {shlex.quote(cwd)} {shlex.quote(command)}"],
                             f"({cwd}) {command}", "in the agent's workspace", self.session.env, check, False)  # fmt: skip
        argv = lambda: self._checked(self.session.host_argv(cwd, command))  # noqa: E731
        return self._run(argv, f"({cwd}) {command}", f"on {self.config.ssh}", self.session.env, check, False)

    def _check_inventory(self) -> None:
        """From a laptop, never with an inventory written on the host: it makes
        the host `ansible_connection: local`, which would run the host's roles
        on this machine."""
        inventory = self.repo_root / "ansible" / "inventory.generated.yml"
        if not inventory.exists():
            raise BuildError("no ansible/inventory.generated.yml: run `worms step inventory` first")
        if "ansible_connection: local" in inventory.read_text():
            raise BuildError("ansible/inventory.generated.yml was written on the host: run `worms step inventory` here")

    # The tools the pipeline drives.

    def tofu(self, stack: str, *args: str, check: bool = True) -> int:
        command = "tofu " + " ".join(args)
        if args and args[0] in ("apply", "destroy") and not (stack in HOST_STACKS and self._host_part()):
            try:  # OpenTofu runs here, and its provider logs in to the host over SSH
                check_known_hosts()
            except SessionError as exc:
                raise BuildError(str(exc)) from exc
        if stack in HOST_STACKS and self._host_part():
            if args and args[0] == "init":
                # Initialised on the host whenever the workspace is synced
                # (tasks/workspace.yml), which needs the inventory and the CA:
                # not there yet when a first `worms apply --all` starts with init.
                print(output.color(f"$ (opentofu/stacks/{stack}) {command}  [done on the host by agent-sync]", "dim"))
                return 0
            return self._host_workspace(f"opentofu/stacks/{stack}", command, check=check)
        return self.sh(command, cwd=f"opentofu/stacks/{stack}", check=check, tofu=True)

    def ansible(self, playbook: str, *args: str, vault: bool = True) -> int:
        extra = (" " + " ".join(args)) if args else ""
        if playbook in HOST_PLAYBOOKS and self._host_part():
            # The host's own inventory (its stacks) and no vault: these plays
            # need neither the controller's secrets nor its VMs.
            return self._host_workspace(
                "ansible", f"python3 ../scripts/generate_inventory.py --quiet && ansible-playbook {playbook}{extra}"
            )
        base = "ansible-playbook"
        if vault and self.session.has_vault():
            base += " -e @group_vars/vault.yml"
        if self.session.laptop:
            self._check_inventory()
            # The host's internet flag (group_vars/all.yml can only test the
            # controller's own filesystem).
            base += f" -e internet_enabled={str(self._internet_allowed()).lower()}"
        return self.sh(f"{base} {playbook}{extra}", cwd="ansible")

    def _internet_allowed(self) -> bool:
        try:
            return self.session.internet_allowed()
        except SessionError as exc:
            raise BuildError(str(exc)) from exc

    def secrets(self) -> None:
        """Generate the missing generated secrets (never a new WORMS key while VMs exist
        that authorize the old one), and stop on a missing token the platform needs:
        `worms secret issue <name>`."""
        from worms_cli.secrets import EXTERNAL, vms_deployed

        vault = self.session.vault
        print(output.color(f"$ (vault) generate missing secrets  [{vault.file}]", "dim"))
        try:
            added = vault.ensure(new_ssh_key=not vms_deployed(self.repo_root))
            vault.check(vault.load())
            required, optional = vault.missing_tokens()
        except Exception as exc:  # noqa: BLE001  (VaultError, ssh-keygen/ansible-vault failures)
            raise BuildError(f"vault: {exc}") from exc
        print(f"secrets: generated {', '.join(added)}" if added else "secrets: nothing to generate")
        for name in optional:
            output.warn(f"{name}: {EXTERNAL[name][2]} -- `worms secret issue {name}`")
        if required:
            raise BuildError("; ".join(f"{name}: {EXTERNAL[name][2]}" for name in required)
                             + " -- `worms secret issue " + "` and `worms secret issue ".join(required) + "`")  # fmt: skip

    def python(self, script: str, *args: str) -> int:
        extra = (" " + " ".join(shlex.quote(a) for a in args)) if args else ""
        return self.sh(f"python3 {script}{extra}")

    def flag_exists(self, path: str) -> bool:
        """Whether a file exists on the host (for the internet flag check). A failed
        check is an error, never an answer: a lost connection says nothing about the flag."""
        if not self.session.laptop:
            return os.path.exists(path)
        code = self.host_sh(f"test -e {shlex.quote(path)}", check=False, quiet=True)
        if code not in (0, 1):
            raise BuildError(f"could not check {path} on {self.config.ssh} (exit {code})")
        return code == 0

    def host_read(self, path: str) -> str | None:
        """A file on the host, or None when it is not there."""
        if not self.session.laptop:
            try:
                return Path(path).read_text()
            except FileNotFoundError:
                return None
        try:
            code, out, err = self.session.host_run(["cat", path])
        except SessionError as exc:
            raise BuildError(str(exc)) from exc
        if code == 0:
            return out
        if "No such file" in err:
            return None
        raise BuildError(f"could not read {path} on {self.config.ssh}: {err.strip()[:200]}")

    def vm_run(self, name: str, argv: list[str], input: str | None = None) -> None:
        """A command on a platform VM, by name, over its management SSH (as `worms run` reaches
        it): raises when it fails."""
        from worms_cli.vms import Catalogue

        catalogue = Catalogue(self.config)
        ssh, env = catalogue.ssh_argv(catalogue.resolve(name), shlex.join(argv), interactive=False)
        done = subprocess.run(ssh, input=input, env=env, capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise BuildError(f"on {name}: {shlex.join(argv)}: exit {done.returncode}: "
                             f"{(done.stderr or done.stdout).strip()[-200:]}")  # fmt: skip

    def _host_json(self, *argv: str):
        """A read-only command on the host whose output is JSON."""
        try:
            code, out, err = self.session.host_run(list(argv))
        except SessionError as exc:
            raise BuildError(str(exc)) from exc
        if code != 0:
            raise BuildError(f"{' '.join(argv)} failed: {err.strip()[:200]}")
        return json.loads(out)

    def vms(self) -> list[dict]:
        """Every VM on the host, with its vmid, name, pool, status and node."""
        return self._host_json("pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json")

    def snapshot(self, vm: dict, name: str) -> dict | None:
        """A VM's snapshot of that name (snaptime, description), if it has one."""
        path = f"/nodes/{vm['node']}/qemu/{vm['vmid']}/snapshot"
        return next((s for s in self._host_json("pvesh", "get", path, "--output-format", "json") if s.get("name") == name), None)

    def commit(self) -> str:
        """The checkout's commit, to record what a snapshot was taken from and what built a VM
        (worms_cli/history.py): "-dirty" when tracked files differ from it. In the round agent's
        workspace, which has no .git, the commit its sync came from (.worms-commit); "unknown"
        when neither says."""
        if getattr(self, "_commit", None) is None:
            git = ["git", "-C", str(self.repo_root)]
            commit = subprocess.run([*git, "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True).stdout.strip()
            if commit:
                if subprocess.run([*git, "diff", "--quiet", "HEAD"], capture_output=True).returncode == 1:
                    commit += "-dirty"
            else:
                try:
                    commit = (self.repo_root / ".worms-commit").read_text().strip() or "unknown"
                except OSError:
                    commit = "unknown"
            self._commit = commit
        return self._commit

    def record_history(self, lines) -> None:
        """Append to the host's VM history (worms_cli/history.py): here on the host, over SSH
        from a laptop. Raises on failure; the caller decides that it is no reason to stop."""
        from worms_cli import history

        if not lines:
            return
        if not self.session.laptop:
            history.append(lines)
        elif self.host_sh(history.append_command(lines), check=False, quiet=True) != 0:
            raise BuildError(f"could not append to {history.LEDGER} on {self.config.ssh}")


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        output.error("refusing a destructive action without a TTY; pass --yes to confirm")
        return False
    answer = input(output.color(f"{prompt} [y/N] ", "yellow")).strip().lower()
    return answer == "y"
