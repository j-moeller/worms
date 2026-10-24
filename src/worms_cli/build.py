"""Run the build the way the Makefile does, locally on the Proxmox host or
over SSH from your laptop (WORMS_SSH). This keeps the Makefile as the single
implementation of *how* things are built; the CLI just drives it with a
clearer, discoverable interface and a component registry.

Every component maps to make target(s). `worms build` with no component runs
the full `make` (idempotent, end to end). `worms build <component>` runs just
that part. Destructive targets (reset, rebuild, emergency stop) are separate
subcommands that confirm first.
"""

from __future__ import annotations

import shlex
import subprocess
import sys

from worms_cli import output

# component -> (make targets, one-line help). Order matters where listed.
COMPONENTS: dict[str, tuple[list[str], str]] = {
    "all": (["all"], "full idempotent end-to-end setup (= bare `make`)"),
    "secrets": (["secrets"], "generate management secrets into the vault"),
    "platform": (["platform"], "bridges, images, cloud-init, pools, hookscript"),
    "firewall": (["firewall"], "Proxmox cluster/node firewall + security groups"),
    "host-network": (["host-network"], "host nftables/sysctls (SetupHostNetwork.yml)"),
    "infrastructure": (["infrastructure"], "create the base infrastructure VMs"),
    "inventory": (["inventory"], "regenerate the Ansible inventory from OpenTofu"),
    "provision": (["provision"], "Ansible configuration of all VMs"),
    "private-internet": (["internet-deny", "inventory"], "note: use provision to (re)configure PI"),
    "devbox": (["devbox-apply", "devbox-configure"], "build + configure the team devboxes and SSH gateway"),
}

# Disruptive make targets, reached through dedicated subcommands (each confirms
# first). emergency-stop only FREEZES (stops) the VMs; it never destroys.
DESTRUCTIVE = {
    "emergency-stop": (["stop-all"], "freeze: hard-stop ALL VMs (does NOT destroy them)"),
    "destroy": (["destroy"], "destroy the VM stacks (keeps firewall/platform)"),
    "destroy-all": (["destroy_all"], "destroy everything (VMs, firewall, platform)"),
}


class BuildError(RuntimeError):
    pass


class Runner:
    def __init__(self, config, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run

    def _wrap(self, argv: list[str]) -> list[str]:
        """Run locally, or over SSH when WORMS_SSH is set (make runs on the host)."""
        inner = " ".join(shlex.quote(a) for a in argv)
        remote = f"cd {shlex.quote(str(self.config.repo_root))} && {inner}"
        if self.config.ssh:
            return ["ssh", self.config.ssh, remote]
        return ["sh", "-c", remote]

    def make(self, *targets: str, extra: list[str] | None = None) -> int:
        argv = ["make", *targets, *(extra or [])]
        cmd = self._wrap(argv)
        location = f"on {self.config.ssh}" if self.config.ssh else "locally"
        print(output.color(f"$ make {' '.join(targets)}  ({location})", "dim"))
        if self.dry_run:
            print(output.color("  (dry run: not executed)", "yellow"))
            return 0
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            raise BuildError(f"`make {' '.join(targets)}` exited {proc.returncode}")
        return proc.returncode

    def component(self, name: str | None, extra: list[str] | None = None) -> None:
        if name is None:
            name = "all"
        if name not in COMPONENTS:
            known = ", ".join(COMPONENTS)
            raise BuildError(f"unknown component {name!r}. Known: {known}")
        targets, _ = COMPONENTS[name]
        for target in targets:
            self.make(target, extra=extra)


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        output.error("refusing a destructive action without a TTY; pass --yes to confirm")
        return False
    answer = input(output.color(f"{prompt} [y/N] ", "yellow")).strip().lower()
    return answer == "y"
