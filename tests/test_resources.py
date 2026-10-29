"""Memory-saving changes (docs/resources.md): ballooning on every VM,
right-sized ceilings, a light rsyslog log server, and the per-round sandbox RAM
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
