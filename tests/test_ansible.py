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


def test_monitor_role_in_configure_sequence():
    # The build pipeline (src/worms_cli/pipeline.py) owns the configure order
    # now; the monitor is set up after the database/dashboard.
    pipeline = (REPO / "src" / "worms_cli" / "pipeline.py").read_text()
    assert "SetupNetworkMonitor.yml" in pipeline
    assert pipeline.index("SetupDbAndDashboard.yml") < pipeline.index("SetupNetworkMonitor.yml")


def test_makefile_is_a_thin_wrapper_over_the_cli():
    # All functionality lives in src/manage.py; the Makefile only delegates.
    import re

    makefile = (REPO / "Makefile").read_text()
    targets = set(re.findall(r"^([A-Za-z0-9_-]+):", makefile, re.MULTILINE))
    assert targets == {"all", "destroy", "destroy_all"}, targets
    # Each target calls the CLI (python3 src/manage.py), nothing else builds.
    assert "src/manage.py" in makefile
    assert "$(WORMS) build" in makefile
    assert "$(WORMS) destroy" in makefile
    assert "$(WORMS) destroy-all" in makefile
    assert "tofu" not in makefile and "ansible" not in makefile


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


NGINX_DJANGO = "ansible/roles/web/nginx_tls_django"


def test_dashboard_nginx_rate_limits_the_team_api(render):
    """The team API (docs/team_api.md) is rate-limited by nginx per client
    address and per token, GET only, before Django -- on the dashboard only."""
    dashboard = yaml.safe_load((REPO / "ansible" / "group_vars" / "dashboard.yml").read_text())
    orchestrator = yaml.safe_load((REPO / "ansible" / "group_vars" / "orchestrator.yml").read_text())
    assert dashboard.get("nginx_team_api") is True
    assert not orchestrator.get("nginx_team_api")

    conf = render(NGINX_DJANGO, "worms-tls.conf.j2", nginx_team_api=True, tls_dns_name="dashboard.worms.internal")
    assert "limit_req_zone $binary_remote_addr zone=team_api_ip:10m rate=5r/s;" in conf
    assert "limit_req_zone $http_authorization zone=team_api_token:10m rate=3r/s;" in conf
    api = conf[conf.index("location /api/"):conf.index("location / {")]
    for directive in ("limit_except GET", "limit_req zone=team_api_ip", "limit_req zone=team_api_token",
                      "limit_conn team_api_conn", "limit_req_status 429", "proxy_read_timeout 15s"):
        assert directive in api, directive

    off = render(NGINX_DJANGO, "worms-tls.conf.j2", tls_dns_name="orchestrator.worms.internal")
    assert "limit_req" not in off and "location /api/" not in off and "location / {" in off


def test_orchestrator_power_acl_covers_user_and_token():
    """A privilege-separated Proxmox token gets the intersection of its own
    and its user's ACL: granting the token alone would give it nothing."""
    tasks = yaml.safe_load((REPO / "ansible" / "roles" / "host" / "round_agent" / "tasks" / "main.yml").read_text())
    grant = next(t for t in tasks if "acl modify" in str(t.get("ansible.builtin.command", {}).get("cmd", "")))
    cmd = grant["ansible.builtin.command"]["cmd"]
    assert "/pool/{{ round_agent_sandbox_pool }}" in cmd
    assert "-users '{{ proxmox_api_user }}'" in cmd and "-tokens '{{ round_agent_power_token }}'" in cmd


def test_django_nginx_forwards_the_host_with_its_port(render):
    """Django's CSRF check compares Origin (https://name:port) with the Host it
    receives; `$host` drops the port and broke every login through a forwarded
    port (tests/test_dashboard_login.py)."""
    for api in (True, False):
        conf = render(NGINX_DJANGO, "worms-tls.conf.j2", nginx_team_api=api, tls_dns_name="dashboard.worms.internal")
        hosts = [line.strip() for line in conf.splitlines() if "proxy_set_header Host" in line]
        assert hosts and all(line == "proxy_set_header Host $http_host;" for line in hosts), hosts
        assert conf.count("proxy_pass ") == len(hosts)  # every location forwards it


# --- the configure sequence (static; before/after guard for refactorings) ---

# Roles every host gets from the configure sequence, and the sequence itself.
CONFIGURE = [
    "WaitForVMs.yml", "SetupDNS.yml", "CreateCA.yml", "DistributeCA.yml", "ConfigureNginx.yml",
    "ConfigureNginxDjango.yml", "SetupDbAndDashboard.yml", "SetupSandboxApi.yml", "SetupNetworkMonitor.yml",
    "SetupPrivateInternet.yml", "SetupAudit.yml", "SetupRoundAgent.yml",
]  # fmt: skip
TLS_WEB = ["ca_distribute", "nginx_tls"]
ROLES = {
    "proxmox_host": ["round_agent"],
    "localhost": ["ca_create"],
    "database": ["ca_distribute", "setup_db_base", "setup_db_permissions", "auditd"],
    "dashboard": [*TLS_WEB, "nginx_tls_django", "django_app", "auditd"],
    "orchestrator": [*TLS_WEB, "nginx_tls_django", "django_app", "auditd"],
    "scoring": [*TLS_WEB, "sandbox_api", "auditd"],
    "logging": [*TLS_WEB, "sandbox_api", "auditd"],
    "monitor": ["ca_distribute", "network_monitor", "auditd"],
    "dns": ["setup_dns", "auditd"],
    "wazuh": ["wazuh_server"],
    **{pi: ["apt_client", pi, "auditd"] for pi in ("apt_cache", "devpi", "docker_registry", "manpages", "ntp",
                                                   "cve_search", "wikipedia")},
}  # fmt: skip
# Plays that wait for cloud-init on each of their VMs before their roles.
CLOUD_INIT_WAITS = {
    ("WaitForVMs.yml", "all:!proxmox_host:!sandbox:!sandbox_gw"), ("SetupDNS.yml", "dns_hosts"),
    ("DistributeCA.yml", "tls_hosts"), ("ConfigureNginx.yml", "nginx_hosts"),
    ("ConfigureNginxDjango.yml", "django_hosts"), ("SetupSandboxApi.yml", "sandbox_api_hosts"),
    ("SetupNetworkMonitor.yml", "network_monitor_hosts"),
    *{("SetupPrivateInternet.yml", f"{pi}_hosts") for pi in ("apt_cache", "devpi", "docker_registry", "manpages",
                                                             "ntp", "cve_search", "wikipedia")},
}  # fmt: skip


def test_configure_sequence_is_unchanged():
    from ansible_static import configure_playbooks

    assert configure_playbooks() == CONFIGURE


@pytest.mark.parametrize("host", sorted(ROLES))
def test_every_host_gets_its_roles(host):
    from ansible_static import groups_of, play_roles, plays

    groups = {"localhost"} if host == "localhost" else groups_of(host)
    roles = [role for _playbook, play in plays(groups) for role in play_roles(play)]
    assert sorted(roles) == sorted(ROLES[host])


def test_plays_still_wait_for_cloud_init():
    """WaitForVMs.yml first, for every VM; the plays that wait again per VM
    keep doing so (a VM rebuilt later, e.g. by `worms build`, may still be
    in its first boot)."""
    from ansible_static import configure_playbooks

    waiting = set()
    for playbook in configure_playbooks():
        for play in yaml.safe_load((ANSIBLE / playbook).read_text()) or []:
            tasks = (play.get("pre_tasks") or []) + (play.get("tasks") or [])
            if any("cloud-init status --wait" in str(task) for task in tasks):
                waiting.add((playbook, play["hosts"]))
    assert CLOUD_INIT_WAITS <= waiting
    assert configure_playbooks()[0] == "WaitForVMs.yml"
