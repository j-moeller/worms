"""Memory-saving changes (docs/resources.md): ballooning on every VM,
right-sized ceilings, Wazuh off by default, and the per-round sandbox RAM
ceiling. Static checks over the OpenTofu/Ansible sources plus the generator."""

from __future__ import annotations

import subprocess
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


def test_sandbox_vm_ram_floor_is_half_the_ceiling():
    text = (OTF / "stacks/sandbox/main.tf").read_text()
    assert "floating = max(256, floor(each.value.ram / 2))" in text


def test_wazuh_is_disabled_by_default():
    # OpenTofu: the wazuh VM is gated and off by default.
    persistent = (OTF / "stacks/infrastructure/persistent/main.tf").read_text()
    assert 'variable "enable_wazuh"' in persistent and "default     = false" in persistent
    assert 'name != "wazuh" || var.enable_wazuh' in persistent
    # Ansible: the flag is off, and SetupAudit's roles are gated on it.
    allvars = yaml.safe_load((ANSIBLE / "group_vars" / "all.yml").read_text())
    assert allvars["wazuh_enabled"] is False
    audit = (ANSIBLE / "SetupAudit.yml").read_text()
    assert audit.count("when: wazuh_enabled | default(false) | bool") == 2


def test_infra_ceilings_were_trimmed():
    persistent = (OTF / "stacks/infrastructure/persistent/main.tf").read_text()
    # dashboard and orchestrator dropped from the 2048 default to 1024.
    assert persistent.count("ram               = 1024") == 2
    ephemeral = (OTF / "stacks/infrastructure/ephemeral/main.tf").read_text()
    assert "ram                         = 512" in ephemeral   # dns
    assert "ram      = 512" in ephemeral                      # scoring/logging module call


# --- the per-round sandbox RAM ceiling -------------------------------------------


def test_the_sandbox_ram_bounds_match_the_agent():
    sys.path.insert(0, str(REPO / "projects" / "dashboard"))
    sys.path.insert(0, str(REPO / "scripts"))
    from core import schedule  # noqa: E402

    import importlib.util

    spec = importlib.util.spec_from_file_location("agent_validation", REPO / "scripts" / "round_agent" / "validation.py")
    validation = importlib.util.module_from_spec(spec)
    sys.modules["agent_validation"] = validation
    spec.loader.exec_module(validation)

    default, low, high = schedule.SANDBOX_BOUNDS["vm_ram"]
    assert (default, low, high) == (512, 256, 8192)
    assert validation.sandbox_params({})["vm_ram"] == default
    validation.sandbox_params({"vm_ram": low})
    validation.sandbox_params({"vm_ram": high})
    for bad in (low - 1, high + 1):
        with pytest.raises(validation.Invalid):
            validation.sandbox_params({"vm_ram": bad})


def test_the_generator_honours_the_ram_ceiling(tmp_path):
    out = subprocess.run(
        [sys.executable, "-I", str(REPO / "scripts" / "generate_sandbox.py"),
         "--seed", "1", "--networks", "3", "--vms", "5", "--max-interfaces", "2", "--sites", "1",
         "--vm-ram", "640", "--network-file", str(tmp_path / "g.json")],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    rams = [line for line in out.splitlines() if "ram" in line and "=" in line]
    assert rams and all("640" in line for line in rams), rams
