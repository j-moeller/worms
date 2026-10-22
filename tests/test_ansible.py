"""Static checks of the Ansible side: every playbook parses, every role
template renders with the generated inventory, and the rendered files are
valid for the tool that will read them (nft, bash/shellcheck, systemd).
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import REPO, require

ANSIBLE = REPO / "ansible"
PLAYBOOKS = sorted(p.name for p in ANSIBLE.glob("*.yml") if p.name != "inventory.yml")


@pytest.fixture(scope="session")
def ansible_env(tmp_path_factory, generated_inventory):
    """ansible.cfg of the repository, without the vault key (not in git)."""
    require("ansible-playbook")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(ANSIBLE / "ansible.cfg")
    roles = ":".join(str(ANSIBLE / p) for p in cfg["defaults"]["roles_path"].split(":"))
    cfg["defaults"]["roles_path"] = roles
    cfg["defaults"]["inventory"] = f"{ANSIBLE / 'inventory.yml'},{generated_inventory}"
    cfg["defaults"].pop("vault_password_file", None)
    path = tmp_path_factory.mktemp("ansible") / "ansible.cfg"
    with open(path, "w") as f:
        cfg.write(f)
    return dict(os.environ, ANSIBLE_CONFIG=str(path), ANSIBLE_NOCOLOR="1")


@pytest.mark.parametrize("playbook", PLAYBOOKS)
def test_playbook_syntax(ansible_env, playbook):
    """Needs the collections the playbooks use (the `ansible` package has
    them; plain ansible-core does not)."""
    proc = subprocess.run(
        ["ansible-playbook", "--syntax-check", "-e", "target=localhost", playbook],
        cwd=ANSIBLE,
        env=ansible_env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_every_vm_group_is_in_the_inventory():
    """A VM key in OpenTofu whose group is missing from inventory.yml would
    get no role groups (and e.g. no audit logging)."""
    groups = yaml.safe_load((ANSIBLE / "inventory.yml").read_text())["all"]["children"]
    for vm in ("database", "dashboard", "orchestrator", "scoring", "logging", "monitor", "dns", "wazuh"):
        assert vm in groups
    audit = groups["audit_hosts"]["children"]
    assert "monitor" in audit and "monitor" in groups["tls_hosts"]["children"]
    assert "monitor" in groups["network_monitor_hosts"]["children"]


def test_monitor_role_in_make_provision():
    makefile = (REPO / "Makefile").read_text()
    assert "SetupNetworkMonitor.yml" in makefile
    assert makefile.index("SetupDbAndDashboard.yml") < makefile.index("SetupNetworkMonitor.yml")


# --- rendered templates ------------------------------------------------------

MONITOR_VARS = {
    "monitor_capture_mac": "bc:24:11:aa:bb:cc",
    "network_monitor_capture_iface": "ens20",
    "postgres_db": "mydb",
    "postgres_host": "10.11.0.16",
    "vault_dbuser_network_monitor_password": "secret",
    "ansible_distribution_major_version": "13",
}
MONITOR = "ansible/roles/applications/network_monitor"


def test_host_firewall_ruleset(render, tmp_path):
    require("nft")
    path = tmp_path / "worms.nft"
    path.write_text(render("ansible/roles/host/host_network", "worms.nft.j2"))
    proc = subprocess.run(["nft", "-c", "-f", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        errors = [line for line in proc.stderr.splitlines() if "Error:" in line]
        if errors and all("Operation not supported" in e or "Permission denied" in e for e in errors):
            pytest.skip("this kernel cannot check the bridge family (nft -c): " + errors[0])
    assert proc.returncode == 0, proc.stderr
    text = path.read_text()
    # The mirror bridge is receive-only.
    assert 'meta ibrname "vmbr3" counter drop' in text


@pytest.mark.parametrize(
    "role, template, extra",
    [
        ("ansible/roles/host/host_network", "worms-mirror.sh.j2", {}),
        ("ansible/roles/host/host_network", "worms-firewall-watchdog.sh.j2", {}),
        (MONITOR, "worms-capture-iface.sh.j2", MONITOR_VARS),
    ],
)
def test_shell_scripts(render, tmp_path, role, template, extra):
    script = tmp_path / "script.sh"
    script.write_text(render(role, template, **extra))
    subprocess.run(["bash", "-n", str(script)], check=True)
    if shutil.which("shellcheck"):
        proc = subprocess.run(["shellcheck", "-S", "warning", "-s", "bash", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout


@pytest.mark.parametrize(
    "template",
    ["zeek.service.j2", "zeek-shipper.service.j2", "worms-capture-iface.service.j2",
     "zeek-archive-cleanup.service.j2", "zeek-archive-cleanup.timer.j2"],
)  # fmt: skip
def test_monitor_units(render, tmp_path, template):
    unit = tmp_path / template.removesuffix(".j2")
    unit.write_text(render(MONITOR, template, **MONITOR_VARS))
    if shutil.which("systemd-analyze"):
        proc = subprocess.run(["systemd-analyze", "verify", "--man=no", str(unit)], capture_output=True, text=True)
        # Missing binaries (zeek is not installed here) are expected.
        problems = [line for line in proc.stderr.splitlines() if "not found" not in line.lower() and "is not executable" not in line]
        assert not [p for p in problems if "Unknown" in p or "Invalid" in p], proc.stderr


def test_zeek_unit_is_contained(render):
    unit = render(MONITOR, "zeek.service.j2", **MONITOR_VARS)
    assert "User=zeek" in unit and "IPAddressDeny=any" in unit
    assert "CapabilityBoundingSet=CAP_NET_RAW CAP_NET_ADMIN" in unit
    assert "-i ens20" in unit


def test_shipper_unit_reaches_only_the_database(render):
    unit = render(MONITOR, "zeek-shipper.service.j2", **MONITOR_VARS)
    assert "IPAddressDeny=any" in unit and "IPAddressAllow=10.11.0.16/32" in unit
    assert "User=zeek-shipper" in unit and "CapabilityBoundingSet=\n" in unit


def test_shipper_env(render):
    env = render(MONITOR, "zeek-shipper.env.j2", **MONITOR_VARS)
    assert "DB_SSLMODE=verify-full" in env and "DB_USER=dbuser_network_monitor" in env
    assert "EVENT_LOGS=dns,http,ssl" in env


def test_zeek_config(render):
    config = render(MONITOR, "local.zeek.j2", **MONITOR_VARS)
    assert "@load policy/tuning/json-logs" in config
    assert "redef Site::local_nets += { 10.64.0.0/10, 10.2.0.0/24 };" in config
    assert 'redef Log::default_rotation_dir = "/var/lib/zeek/archive";' in config
    zeek = shutil.which("zeek") or ("/opt/zeek/bin/zeek" if Path("/opt/zeek/bin/zeek").exists() else None)
    if zeek:
        path = Path(os.environ.get("TMPDIR", "/tmp")) / "worms-local.zeek"
        path.write_text(config)
        subprocess.run([zeek, "--parse-only", str(path)], check=True)
