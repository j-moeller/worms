"""Run shell commands for the build pipeline, locally on the Proxmox host or
over SSH from your laptop (WORMS_SSH). The pipeline itself (every former
Makefile recipe) lives in worms_cli/pipeline.py; this module is just the
primitive that runs one command in a repo subdirectory and the confirm helper.
"""

from __future__ import annotations

import shlex
import subprocess
import sys

from worms_cli import output


class BuildError(RuntimeError):
    pass


class Runner:
    def __init__(self, config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.repo_root = config.repo_root

    def sh(self, command: str, cwd: str = ".", check: bool = True, quiet: bool = False) -> int:
        """Run `command` (a shell string) in <repo>/<cwd>, locally or over SSH.

        The command runs through /bin/sh on the target; `cwd` is relative to
        the repo root (which must exist on the node when running over SSH).
        """
        target_dir = f"{self.repo_root}/{cwd}" if cwd not in (".", "") else str(self.repo_root)
        full = f"cd {shlex.quote(target_dir)} && {command}"
        if self.config.ssh:
            argv = ["ssh", self.config.ssh, full]
            where = f"on {self.config.ssh}"
        else:
            argv = ["sh", "-c", full]
            where = "locally"
        if not quiet:
            label = command if len(command) < 100 else command[:97] + "..."
            print(output.color(f"$ ({cwd}) {label}  [{where}]", "dim"))
        if self.dry_run:
            print(output.color("  (dry run: not executed)", "yellow"))
            return 0
        proc = subprocess.run(argv)
        if check and proc.returncode != 0:
            raise BuildError(f"command failed (exit {proc.returncode}): {command}")
        return proc.returncode

    # Convenience wrappers for the three tools the pipeline drives.

    def tofu(self, stack: str, *args: str, check: bool = True) -> int:
        return self.sh("tofu " + " ".join(args), cwd=f"opentofu/stacks/{stack}", check=check)

    def ansible(self, playbook: str, *args: str, vault: bool = True) -> int:
        base = "ansible-playbook"
        if vault:
            base += " -e @group_vars/vault.yml"
        extra = (" " + " ".join(args)) if args else ""
        return self.sh(f"{base} {playbook}{extra}", cwd="ansible")

    def python(self, script: str, *args: str) -> int:
        extra = (" " + " ".join(shlex.quote(a) for a in args)) if args else ""
        return self.sh(f"python3 {script}{extra}")

    def flag_exists(self, path: str) -> bool:
        """Whether a file exists on the target (for the internet flag check)."""
        if self.dry_run:
            return False  # nothing was actually run; don't block the dry run
        rc = self.sh(f"test -e {shlex.quote(path)}", check=False, quiet=True)
        return rc == 0


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        output.error("refusing a destructive action without a TTY; pass --yes to confirm")
        return False
    answer = input(output.color(f"{prompt} [y/N] ", "yellow")).strip().lower()
    return answer == "y"
