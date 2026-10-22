"""OpenTofu: formatting, validation of every stack, and consistency of the
addressing across the layers that each enforce a part of it (Proxmox
firewall, host nftables, pg_hba).

`tofu validate` needs the bpg/proxmox provider. Without registry access, point
TF_CLI_CONFIG_FILE at a provider_installation with a filesystem_mirror.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
import yaml

from conftest import FIXTURES, REPO, require

OPENTOFU = REPO / "opentofu"
STACKS = ["platform", "firewall", "infrastructure/persistent", "infrastructure/ephemeral", "private_internet", "sandbox"]


@pytest.fixture(scope="session")
def tofu_copy(tmp_path_factory):
    """The OpenTofu tree without state or .terraform (never touches yours)."""
    require("tofu")
    dest = tmp_path_factory.mktemp("opentofu") / "opentofu"
    shutil.copytree(
        OPENTOFU,
        dest,
        symlinks=True,  # shared-variables.tf links stay relative, inside the copy
        ignore=shutil.ignore_patterns(".terraform*", "terraform.tfstate*", "images", "iso"),
    )
    return dest


def test_fixture_matches_shared_variables(tofu_copy, tmp_path):
    """tests/fixtures/platform-outputs.json is the platform `infrastructure`
    output computed from opentofu/shared/shared-variables.tf."""
    stack = tmp_path / "stack"
    stack.mkdir()
    shutil.copy(tofu_copy / "shared" / "shared-variables.tf", stack)
    outputs = (OPENTOFU / "stacks" / "platform" / "outputs.tf").read_text()
    (stack / "outputs.tf").write_text(re.search(r'output "infrastructure" \{.*?\n\}\n', outputs, re.S).group(0))
    run = lambda *a: subprocess.run(["tofu", *a], cwd=stack, check=True, capture_output=True, text=True)  # noqa: E731
    run("init", "-input=false")
    run("apply", "-auto-approve", "-input=false", "-var", "proxmox_api_url=x", "-var", "proxmox_api_token=x")
    fresh = json.loads(run("output", "-json").stdout)["infrastructure"]["value"]
    committed = json.loads((FIXTURES / "platform-outputs.json").read_text())["infrastructure"]["value"]
    assert fresh == committed, "regenerate tests/fixtures/platform-outputs.json (see docs/testing.md)"


def test_format(tofu_copy):
    files = [p for p in tofu_copy.rglob("*.tf") if not p.name.startswith("generated_vms")]
    proc = subprocess.run(["tofu", "fmt", "-check", "-list=true", *map(str, files)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("stack", STACKS)
def test_validate(tofu_copy, stack):
    path = tofu_copy / "stacks" / stack
    init = subprocess.run(["tofu", "init", "-backend=false", "-input=false", "-no-color"], cwd=path, capture_output=True, text=True)
    if init.returncode != 0:
        pytest.skip("cannot install the providers (registry/mirror): " + init.stderr.strip().splitlines()[-1])
    proc = subprocess.run(["tofu", "validate", "-no-color"], cwd=path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- consistency across layers (no tofu needed) ------------------------------


def test_database_clients_are_allowed_everywhere(infrastructure):
    """A VM with a database role must be allowed by the Proxmox `database`
    security group, by pg_hba (its /32) and, if routed, by the host."""
    firewall = (OPENTOFU / "stacks" / "firewall" / "main.tf").read_text()
    sg = re.search(r'security_group" "database".*?for_each = \[(.*?)\]', firewall, re.S).group(1)
    allowed = set(re.findall(r'"(\w+)"', sg))
    users = yaml.safe_load((REPO / "ansible" / "group_vars" / "all.yml").read_text())["postgresql_users"]
    nft = (REPO / "ansible" / "roles" / "host" / "host_network" / "templates" / "worms.nft.j2").read_text()
    for user in users:
        vm = re.search(r"infrastructure_ips\.(\w+)", user["cidr"]).group(1)
        assert vm in allowed, f"{vm} ({user['name']}) missing in the database security group"
        assert vm in infrastructure["infrastructure_ips"]
        if vm in ("scoring", "logging", "monitor"):  # routed ingest -> core
            assert f"infrastructure_ips.{vm}" in nft, f"{vm} not routed to the database by the host"


def test_monitor_capture_nic_is_receive_only_and_unfiltered():
    module = (OPENTOFU / "modules" / "network_monitor_vm" / "main.tf").read_text()
    devices = re.findall(r"network_device \{(.*?)\n  \}", module, re.S)
    assert len(devices) == 3
    assert "var.capture_bridge" in devices[2] and "firewall = false" in devices[2]
    assert all("firewall = true" in d for d in devices[:2])
    # Only two ip_config blocks: the capture NIC gets no address.
    assert module.count("ip_config {") == 2


def test_monitor_ips_are_in_their_zones(infrastructure):
    import ipaddress

    nets = {k: ipaddress.ip_network(v) for k, v in infrastructure["networks"].items()}
    assert ipaddress.ip_address(infrastructure["infrastructure_ips"]["monitor"]) in nets["ingest"]
    assert ipaddress.ip_address(infrastructure["management_ips"]["monitor"]) in nets["mgmt_exposed"]
    assert infrastructure["bridges"]["mirror"] not in (infrastructure["bridges"]["sandbox_glob"],)
