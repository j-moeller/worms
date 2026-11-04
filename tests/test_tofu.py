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

from conftest import FIXTURES, REPO

OPENTOFU = REPO / "opentofu"
STACKS = ["platform", "firewall", "infrastructure/persistent", "infrastructure/ephemeral", "private_internet", "sandbox", "devbox"]


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


def _security_group(firewall: str, name: str) -> str:
    pattern = rf'security_group" "{name}" \{{.*?\n\}}'
    return re.search(pattern, firewall, re.S).group(0)


def test_devbox_ssh_gateway_accepts_ssh_only_from_the_host():
    """The shared SSH gateway shares vmbr7 with every team's devbox VMs. Its
    inbound SSH is pinned to the host's fabric address (teams arrive SNATed to
    it), so a devbox VM cannot open a connection to the gateway even if its own
    output policy were bypassed (docs/security-assessment.md, devbox isolation).
    """
    firewall = (OPENTOFU / "stacks" / "firewall" / "main.tf").read_text()
    group = _security_group(firewall, "devbox_ssh_gateway")
    in_rule = re.search(r'type    = "in".*?\n  \}', group, re.S).group(0)
    assert "source  = local.host_ips.devbox_ssh" in in_rule
    nft = (OPENTOFU.parent / "ansible" / "roles" / "host" / "host_network" / "templates" / "worms.nft.j2").read_text()
    assert "ct status dnat masquerade" in nft  # the host SNATs team ingress to itself


def test_untrusted_egress_is_scoped_to_the_service_hosts():
    """Sandbox and devbox VMs and their gateways may reach only the service
    hosts on the services bridge (scoring, logging, DNS, private internet),
    never another gateway's SNAT address or an unused services address."""
    firewall = (OPENTOFU / "stacks" / "firewall" / "main.tf").read_text()
    for name in ("sandbox", "sandbox_gw", "devbox", "devbox_gw"):
        group = _security_group(firewall, name)
        assert 'join(",", local.services_hosts)' in group, f"{name}: services egress not scoped to the hosts"
        assert "dest    = local.networks.services" not in group, f"{name}: still targets the whole services subnet"


def test_database_clients_are_allowed_everywhere(infrastructure):
    """A VM with a database role must be allowed by the Proxmox `database`
    security group, by pg_hba (its /32) and, if routed, by the host."""
    firewall = (OPENTOFU / "stacks" / "firewall" / "main.tf").read_text()
    sg = re.search(r'security_group" "database".*?for_each = \[(.*?)\]', firewall, re.S).group(1)
    allowed = set(re.findall(r'"(\w+)"', sg))
    users = yaml.safe_load((REPO / "ansible" / "group_vars" / "all.yml").read_text())["postgresql_users"]
    nft = (REPO / "ansible" / "roles" / "host" / "host_network" / "templates" / "worms.nft.j2").read_text()
    for user in users:
        if "proxmox.host" in user["cidr"]:  # the round agent, on the host
            assert "source  = local.proxmox_host_ip" in firewall, f"{user['name']}: host not allowed"
            continue
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


def _block(text: str, header: str) -> str:
    start = text.index(header)
    end = text.find("\n}\n", start)
    return text[start:end]


@pytest.mark.parametrize(
    "path, membership",
    [
        ("opentofu/modules/devbox_vm/main.tf", 'resource "proxmox_pool_membership" "vm"'),
        ("opentofu/stacks/sandbox/main.tf", 'resource "proxmox_pool_membership" "gw"'),
        ("opentofu/stacks/sandbox/main.tf", 'resource "proxmox_pool_membership" "pool_membership"'),
    ],
)
def test_an_untrusted_vm_joins_its_pool_only_after_its_firewall(path, membership):
    """The firewall watchdog hard-stops every running VM of these pools when one
    of them has no rules. Joining first made a build stop its own VMs (live, on
    the first devbox build)."""
    block = _block((REPO / path).read_text(), membership)
    assert "depends_on" in block, membership
    for resource in ("firewall_options", "firewall_rules"):
        assert resource in block, f"{membership} must wait for {resource}"


@pytest.mark.parametrize(
    "path, resource, vm",
    [
        ("opentofu/modules/devbox_vm/main.tf", 'proxmox_virtual_environment_firewall_options" "vm"', "vm.id"),
        ("opentofu/modules/devbox_vm/main.tf", 'proxmox_pool_membership" "vm"', "vm.id"),
        ("opentofu/modules/infrastructure_vm/main.tf", 'proxmox_virtual_environment_firewall_options" "infrastructure"', "infrastructure.id"),
        ("opentofu/modules/private_internet_vm/main.tf", 'proxmox_virtual_environment_firewall_options" "service_vm"', "service_vm.id"),
        ("opentofu/modules/network_monitor_vm/main.tf", 'proxmox_virtual_environment_firewall_options" "monitor"', "monitor.id"),
        ("opentofu/stacks/devbox/main.tf", 'proxmox_virtual_environment_firewall_options" "ssh_gateway"', "ssh_gateway.id"),
        ("opentofu/stacks/sandbox/main.tf", 'proxmox_virtual_environment_firewall_options" "gw"', "gw.id"),
        ("opentofu/stacks/sandbox/main.tf", 'proxmox_virtual_environment_firewall_options" "vm"', "vm[each.key].id"),
        ("opentofu/stacks/sandbox/main.tf", 'proxmox_pool_membership" "gw"', "gw.id"),
        ("opentofu/stacks/sandbox/main.tf", 'proxmox_pool_membership" "pool_membership"', "vm[each.key].id"),
    ],
)
def test_a_replaced_vm_gets_its_firewall_back(path, resource, vm):
    """Proxmox deletes a VM's firewall settings with the VM. A replacement that got
    the same ID kept its firewall off, with no drift for OpenTofu to see: live, the
    trusted SSH gateway ran that way for two hours, and a devbox reset made the
    watchdog stop every team's VMs. Only `.id` changes on a replacement alone: a
    whole-resource reference would also fire on every in-place update."""
    text = (REPO / path).read_text()
    start = text.index(f'resource "{resource}')
    block = text[start : text.index("\n}\n", start)]
    assert f"replace_triggered_by = [proxmox_virtual_environment_vm.{vm}]" in block


def test_the_ssh_gateway_lets_only_the_node_administer_it():
    """Ansible configures the gateway as debian. `AllowUsers team` alone locked
    it out before the team keys could be installed (live, on the first devbox
    build); debian may log in from the node's management address, and only there."""
    template = (REPO / "opentofu" / "stacks" / "devbox" / "templates" / "ssh-gateway.tftpl").read_text()
    line = next(l.strip() for l in template.splitlines() if l.strip().startswith("AllowUsers"))
    assert line == "AllowUsers ${gateway_user} debian@${mgmt_host_ip}"
    stack = (REPO / "opentofu" / "stacks" / "devbox" / "main.tf").read_text()
    block = stack[stack.index('"${path.module}/templates/ssh-gateway.tftpl"'):]
    assert "mgmt_host_ip    = local.host_ips.mgmt_exposed" in block[: block.index("file_name")]
