"""Memory-saving changes (docs/01-components.md): ballooning on every VM,
right-sized ceilings, a light rsyslog log server, and the per-round sandbox RAM
ceiling. Static checks over the OpenTofu/Ansible sources plus the generator."""

from __future__ import annotations

import sys

import pytest
import yaml

from conftest import REPO

OTF = REPO / "opentofu"
ANSIBLE = REPO / "ansible"

# Every place a VM's memory is defined (module or stack); each must balloon.
MEMORY_SITES = [
    "modules/infrastructure_vm/main.tf",
    "modules/private_internet_vm/main.tf",
    "modules/network_monitor_vm/main.tf",
    "modules/devbox_vm/main.tf",
    "stacks/sandbox/main.tf",      # sandbox-gw and the per-VM block
    "stacks/devbox/main.tf",       # the shared SSH gateway
]


@pytest.mark.parametrize("path", MEMORY_SITES)
def test_every_vm_balloons(path):
    """`memory { dedicated = … }` without `floating` is a fixed allocation the
    host can never reclaim. Every VM definition sets a balloon floor."""
    text = (OTF / path).read_text()
    assert "floating" in text, path
    # No bare one-line fixed allocation slipped back in.
    assert "memory { dedicated =" not in text, f"{path}: fixed (no ballooning)"


def test_a_sandbox_vm_keeps_a_devbox_vms_memory_floor():
    """512 MB, as on a devbox VM, or half the ceiling on a larger VM. Never above the ceiling,
    which Proxmox refuses: a round may set 256 MB per VM (core/schedule.py)."""
    text = (OTF / "stacks/sandbox/main.tf").read_text()
    assert "floating = min(each.value.ram, max(512, floor(each.value.ram / 2)))" in text


def test_the_log_server_is_a_small_rsyslog_vm():
    # Wazuh (a JVM indexer) was replaced by plain rsyslog: one small VM, always on.
    persistent = (OTF / "stacks/infrastructure/persistent/main.tf").read_text()
    syslog = persistent[persistent.index("    syslog = {"):]
    assert "ram               = 512" in syslog.split("}")[0]
    assert "for_each = local.persistent" in persistent
    audit = (ANSIBLE / "SetupAudit.yml").read_text()
    assert "syslog_server" in audit and "when:" not in audit


def test_infra_ceilings_were_trimmed():
    persistent = (OTF / "stacks/infrastructure/persistent/main.tf").read_text()
    # dashboard and orchestrator dropped from the 2048 default to 1024.
    assert persistent.count("ram               = 1024") == 2
    ephemeral = (OTF / "stacks/infrastructure/ephemeral/main.tf").read_text()
    assert "ram                         = 512" in ephemeral   # dns
    assert "ram      = 512" in ephemeral                      # scoring/logging module call


# --- a sandbox VM's RAM ceiling: the gameplan's (src/gameplan) --------------------------


def test_a_gameplans_ram_ceiling_reaches_the_sandbox_stack():
    sys.path.insert(0, str(REPO / "src"))
    from gameplan import BasicVM, Gameplan, GameplanError, build
    from gameplan.definition import LIMITS
    from gameplan.runtime import instantiate

    assert LIMITS["memory"] == (256, 8192)

    def plan(memory):
        def init(self):
            self.a = BasicVM(memory=memory)
            self.register_network(self.a)
            self.set_worm_entrypoint(self.a)

        return instantiate(type("Plan", (Gameplan,), {"__init__": init}))

    assert "ram                      = 640" in build.hcl(plan(640).spec(), "p.py", "0" * 64)
    for bad in (255, 8193):
        with pytest.raises(GameplanError, match="memory"):
            plan(bad).spec()
