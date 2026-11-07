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
    # The inputs without defaults (`worms` sets them); the node as in the fixture.
    inputs = {"proxmox_api_url": "x", "proxmox_api_token": "x", "proxmox_instance": "worms", "node_ssh_address": "x",
              "node_ssh_port": "22", "ssh_public_key": "x", "llm_access_ip": "192.0.2.34"}  # fmt: skip
    run("apply", "-auto-approve", "-input=false", *(arg for k, v in inputs.items() for arg in ("-var", f"{k}={v}")))
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


def test_the_llm_endpoint_is_the_only_external_destination_of_untrusted_vms():
    """Every sandbox and devbox VM may send to one address on TCP 443, and no
    gateway has that group. The address is tracked, so a rebuild keeps it, and
    not in a *.auto.tfvars: `worms` refuses those to protect the vault's token."""
    firewall_dir = OPENTOFU / "stacks" / "firewall"
    settings = yaml.safe_load((REPO / "ansible" / "group_vars" / "all.yml").read_text())
    assert settings["llm_access_ip"] == "141.23.117.34"
    assert [p for p in OPENTOFU.rglob("*.auto.tfvars") if ".terraform" not in p.parts] == []

    group = _security_group((firewall_dir / "main.tf").read_text(), "llm_access")
    assert group.count("rule {") == 1 and 'type    = "out"' in group and 'action  = "ACCEPT"' in group
    assert 'proto   = "tcp"' in group and 'dport   = "443"' in group and "dest    = var.llm_access_ip" in group

    sandbox = (OPENTOFU / "stacks" / "sandbox" / "main.tf").read_text()
    rules = re.search(r'firewall_rules" "(?P<name>vm)".*?\n}\n', sandbox, re.S).group(0)
    assert "local.sg.llm_access" in rules
    gateway = re.search(r'firewall_rules" "gw".*?\n}\n', sandbox, re.S).group(0)
    assert "llm_access" not in gateway

    devbox = (OPENTOFU / "stacks" / "devbox" / "main.tf").read_text()
    assert devbox.count("local.sg.llm_access") == 1, "only the devbox VMs get the group, not their gateways"
    assert re.search(r"devbox\s+= local\.sg\.devbox\n.*?llm_access\s+= local\.sg\.llm_access", devbox, re.S)


def test_a_devbox_reset_keeps_the_operators_memory_floor():
    """Set by hand on the running VMs. A reset rebuilds them from the stack, so it has to be there."""
    devbox = (OPENTOFU / "stacks" / "devbox" / "main.tf").read_text()
    vms = devbox.split('module "devbox_vm" {', 1)[1].split('module "devbox_gw"', 1)[0]
    assert re.search(r"ram_min\s+= 512", vms)
    gateways = devbox.split('module "devbox_gw" {', 1)[1]
    assert "ram_min" not in gateways.split("\n}\n", 1)[0]


def test_no_team_vm_is_offered_hardware_virtualisation():
    """Sandbox and devbox VMs are assumed hostile and rooted. `host` and `max` pass VMX on to them,
    which gives root /dev/kvm and a way into the node's nested-KVM code. Measured on 2026-11-05: with
    `host`, /dev/kvm opened; with x86-64-v3, kvm_intel refuses to load (docs/security-assessment.md)."""
    agent = (REPO / "scripts" / "round_agent" / "handlers.py").read_text()
    expected = re.search(r'^TEAM_VM_CPU = "(.+)"$', agent, re.M).group(1)
    assert expected not in {"host", "max"}

    devbox = (OPENTOFU / "stacks" / "devbox" / "main.tf").read_text()
    sandbox = (OPENTOFU / "stacks" / "sandbox" / "main.tf").read_text()
    vms = devbox.split('module "devbox_vm" {', 1)[1].split('module "devbox_gw"', 1)[0]
    assert re.findall(r'^\s*cpu_type\s*=\s*"(.+)"', vms, re.M) == [expected], "a devbox VM"
    _, sandbox_vms = sandbox.split('resource "proxmox_virtual_environment_vm" "vm" {', 1)
    assert re.findall(r'^\s*type\s*=\s*"(.+)"', sandbox_vms.split("cpu {", 1)[1].split("}", 1)[0], re.M) == [expected], "a sandbox VM"

    # The gateways run no team code, and nothing else in the two stacks may ask for it either.
    module = (OPENTOFU / "modules" / "devbox_vm" / "variables.tf").read_text()
    assert 'contains(["host", "max"], var.cpu_type)' in module, "the module refuses them"
    for name, text in {"devbox": devbox, "sandbox": sandbox}.items():
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        assert not re.search(r'(cpu_type|\btype)\s*=\s*"(host|max)"', code), name
        # `nested-virt` is Proxmox's own shorthand for either, and any token with VM.Config.CPU may set it.
        assert "+vmx" not in code and "+svm" not in code and "nested-virt" not in code, name


def test_every_layer_on_the_way_to_the_llm_endpoint_allows_only_it():
    """The only way out of the sandbox and the devboxes. Each layer is independent, so each
    is checked: one address, TCP 443, and on the gateways only the management NIC leads out."""
    stacks = OPENTOFU / "stacks"
    firewall = (stacks / "firewall" / "main.tf").read_text()
    for name, network in (("sandbox_gw", "sandbox_vms"), ("devbox_gw", "devbox_vms")):
        group = _security_group(firewall, name)
        assert group.count("var.llm_access_ip") == 1, name
        rule = next(r for r in group.split("rule {")[1:] if "var.llm_access_ip" in r)
        for line in ('type    = "in"', 'proto   = "tcp"', 'dport   = "443"', f"source  = local.networks.{network}"):
            assert line in rule, (name, line)
    egress = _security_group(firewall, "llm_egress")
    assert egress.count("rule {") == 1
    for line in ('type    = "out"', 'proto   = "tcp"', 'dport   = "443"', "dest    = var.llm_access_ip"):
        assert line in egress, line
    # Nothing else in the firewall stack names the address.
    assert firewall.count("var.llm_access_ip") == 4

    sandbox = (stacks / "sandbox" / "main.tf").read_text()
    assert re.search(r"security_group = local\.sg\.llm_egress\n\s+iface\s+= local\.gw_mgmt_iface", sandbox)
    assert sandbox.count("local.sg.llm_egress") == 1
    devbox = (stacks / "devbox" / "main.tf").read_text()
    assert devbox.count("local.sg.llm_egress") == 1
    assert re.search(r"devbox_gw\s+= local\.sg\.devbox_gw\n.*?llm_egress\s+= local\.sg\.llm_egress", devbox, re.S)
    module = (OPENTOFU / "modules" / "devbox_vm" / "main.tf").read_text()
    assert re.search(r'for_each = var\.is_gateway \? \[1\] : \[\]\n\s+content \{\n\s+security_group = var\.security_groups\["llm_egress"\]'
                     r'\n\s+iface\s+= "net\$\{local\.mgmt_idx\}"', module)  # fmt: skip

    for template in (stacks / "sandbox/templates/sandbox-gw.tftpl", stacks / "devbox/templates/devbox-gw.tftpl"):
        text = template.read_text()
        assert "define LLM = ${llm_access_ip}" in text
        lines = [line.strip() for line in text.splitlines() if "$LLM" in line]
        assert len(lines) == 2 and lines[0].endswith("ip daddr $LLM tcp dport 443 accept"), template
        assert lines[1].endswith("ip daddr $LLM tcp dport 443 masquerade"), template
        # A route to this one address, and still no default route.
        assert "ip route replace ${llm_access_ip}/32 via ${mgmt_host_ip}" in text
        assert "default via" not in text and "route add default" not in text


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
