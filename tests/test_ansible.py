"""Static checks of the Ansible side: every playbook parses, every role
template renders with the generated inventory, and the rendered files are
valid for the tool that will read them (nft, bash/shellcheck, systemd).
"""

from __future__ import annotations

import configparser
import os
import re
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
    for vm in ("database", "dashboard", "orchestrator", "scoring", "logging", "monitor", "dns", "syslog"):
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


def test_host_firewall_publishes_the_dashboard(render, tmp_path):
    """<WAN>:443 reaches the dashboard's :443, and nothing else of it."""
    require("nft")
    inventory = yaml.safe_load((ANSIBLE / "inventory.yml").read_text())
    assert inventory["all"]["children"]["proxmox_host"]["hosts"]["proxmox.local"]["host_publish_dashboard"] is True
    text = render("ansible/roles/host/host_network", "worms.nft.j2", host_publish_dashboard=True)
    assert 'iifname "vmbr0" fib daddr type local tcp dport 443 dnat ip to 10.10.0.18:443' in text
    assert 'iifname "vmbr0" oifname "vmbr20" ip daddr 10.10.0.18 tcp dport 443' in text
    path = tmp_path / "worms.nft"
    path.write_text(text)
    proc = subprocess.run(["nft", "-c", "-f", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        errors = [line for line in proc.stderr.splitlines() if "Error:" in line]
        if errors and all("Operation not supported" in e or "Permission denied" in e for e in errors):
            pytest.skip("this kernel cannot check the bridge family (nft -c): " + errors[0])
    assert proc.returncode == 0, proc.stderr


def _dashboard_vars(tmp_path, node):
    """tls_dns_name and django_allowed_hosts of group_vars/dashboard.yml on a node."""
    out = tmp_path / "out.yml"
    playbook = [{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": {"proxmox": {"node": node}, "infrastructure_ips": {"dashboard": "10.10.0.18"}},
        "tasks": [
            {"ansible.builtin.include_vars": {"file": str(ANSIBLE / "group_vars" / "all.yml")}},
            {"ansible.builtin.include_vars": {"file": str(ANSIBLE / "group_vars" / "dashboard.yml")}},
            {"ansible.builtin.copy": {
                "content": "{{ {'name': tls_dns_name, 'hosts': django_allowed_hosts} | to_yaml }}",
                "dest": str(out),
            }},
        ],
    }]  # fmt: skip
    (tmp_path / "play.yml").write_text(yaml.safe_dump(playbook))
    proc = subprocess.run(["ansible-playbook", "play.yml"], cwd=tmp_path, capture_output=True, text=True)
    return proc, out


@pytest.mark.parametrize(
    "node, public_name",
    [("worms-dev", "worms-dev.mlsec.tu-berlin.de"), ("worms-pve", "worms.mlsec.tu-berlin.de")],
)
def test_dashboard_has_one_public_name_per_node(tmp_path, node, public_name):
    """The name on the certificate and the only name it answers to. Live, it is
    not the node's own: worms-pve serves worms.mlsec.tu-berlin.de."""
    require("ansible-playbook")
    proc, out = _dashboard_vars(tmp_path, node)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert yaml.safe_load(out.read_text()) == {"name": public_name, "hosts": public_name}


def test_dashboard_on_an_unknown_node_fails_loudly(tmp_path):
    require("ansible-playbook")
    proc, _ = _dashboard_vars(tmp_path, "some-other-node")
    assert proc.returncode != 0


def test_a_lost_handler_cannot_leave_the_ca_untrusted():
    """In the live run the play that copied the CA was interrupted, so its handler
    never ran. Later runs found the file unchanged, and TLS to scoring kept failing."""
    tasks = yaml.safe_load((ANSIBLE / "roles/pki/ca_distribute/tasks/main.yml").read_text())
    check = next(t for t in tasks if t["name"] == "Check the trust store holds the CA")
    assert check["ansible.builtin.stat"]["path"] == "/etc/ssl/certs/worms-internal-ca.pem"
    assert check["notify"] == "Update CA trust" and "not " in check["changed_when"]
    assert any(t.get("ansible.builtin.meta") == "flush_handlers" for t in tasks)


def test_a_devbox_vm_installing_nothing_is_not_silent():
    """One uncached package aborts the whole install. `failed_when: false` hid that
    in the live run: nothing was installed and the job still reported success."""
    tasks = yaml.safe_load((ANSIBLE / "roles/applications/devbox/tasks/main.yml").read_text())
    install = next(t for t in tasks if t["name"] == "Install expected packages")
    assert "failed_when" not in install
    assert install["ansible.builtin.apt"]["install_recommends"] is False


def test_per_team_service_names_resolve():
    entries = yaml.safe_load((ANSIBLE / "group_vars" / "dns.yml").read_text())["dns_entries"]
    names = {entry["name"] for entry in entries}
    assert {"*.scoring.worms.internal", "*.logging.worms.internal"} <= names


def test_a_team_key_can_jump_through_the_ssh_gateway_but_run_nothing(render):
    """`restrict` does not stop commands. In the live run a team login got a shell
    on the gateway, reaching every team's VMs and syslog, and scp and sftp worked."""
    keys = [{"public_key": "ssh-ed25519 AAAA me", "comment": "team=t4", "permitopen": ["10.7.0.11:22"]}]
    text = render("ansible/roles/applications/ssh_gateway", "authorized_keys.j2", ssh_gateway_authorized_keys=keys)
    line = next(row for row in text.splitlines() if "ssh-ed25519" in row)
    options = line.split(" ssh-ed25519")[0]
    assert options == 'restrict,port-forwarding,command="/usr/sbin/nologin",permitopen="10.7.0.11:22"'
    tasks = yaml.safe_load((ANSIBLE / "roles/applications/ssh_gateway/tasks/main.yml").read_text())
    account = next(t for t in tasks if "ansible.builtin.user" in t)["ansible.builtin.user"]
    assert account["shell"] == "/usr/sbin/nologin"


def test_a_code_deploy_can_skip_everything_that_needs_the_internet():
    """With devboxes up the internet window stays closed, and pip then hangs for minutes
    (docs/management.md). `--skip-tags packages` must leave out every package step."""
    tasks = yaml.safe_load((ANSIBLE / "roles/applications/django_app/tasks/main.yml").read_text())
    installers = [t for t in tasks if "ansible.builtin.pip" in t or "ansible.builtin.apt" in t]
    assert len(installers) == 3
    for task in installers:
        assert "packages" in task["tags"], task["name"]


def test_the_orchestrator_is_told_where_teams_reach_the_ssh_gateway():
    """Without it, a team's dev view says "still being built" forever and never
    shows the `ssh -J` command. Found by the live end-to-end run."""
    env = yaml.safe_load((ANSIBLE / "group_vars" / "orchestrator.yml").read_text())["django_extra_env"]
    assert env["DEVBOX_SSH_GATEWAY_HOST"] == "{{ dashboard_public_names[proxmox.node] }}"


def test_django_nginx_serves_https_only(render):
    """Nothing answers on port 80: no redirect, no listener."""
    conf = render(NGINX_DJANGO, "worms-tls.conf.j2", tls_dns_name="worms-dev.mlsec.tu-berlin.de")
    assert "listen 443 ssl;" in conf
    assert "listen 80" not in conf and "[::]:80" not in conf


def test_host_publishes_https_only(render):
    text = render("ansible/roles/host/host_network", "worms.nft.j2", host_publish_dashboard=True)
    published = [line for line in text.splitlines() if "10.10.0.18" in line and 'iifname "vmbr0"' in line]
    assert len(published) == 2 and all("tcp dport 443" in line for line in published)


def test_registry_is_read_only_once_the_internet_window_closes(render):
    role = "ansible/roles/private_internet/docker_registry"
    online = yaml.safe_load(render(role, "config.yml.j2", internet_enabled=True))
    offline = yaml.safe_load(render(role, "config.yml.j2", internet_enabled=False))
    assert "maintenance" not in online["storage"]  # still being filled
    assert offline["storage"]["maintenance"]["readonly"]["enabled"] is True


def test_devpi_only_lets_root_modify_and_root_has_a_generated_password(render):
    for internet in (True, False):
        unit = render("ansible/roles/private_internet/devpi", "devpi.service.j2", internet_enabled=internet)
        assert "--restrict-modify root" in unit
    tasks = (ANSIBLE / "roles" / "private_internet" / "devpi" / "tasks" / "main.yml").read_text()
    assert "devpi_root_password" in tasks
    defaults = yaml.safe_load((ANSIBLE / "roles" / "private_internet" / "devpi" / "defaults" / "main.yml").read_text())
    assert "vault_devpi_root_password" in defaults["devpi_root_password"]


def test_host_firewall_publishes_the_devbox_ssh_gateway(render, tmp_path):
    """With the devbox SSH gateway published, the host DNATs teams to it and
    SNATs that same connection to its own fabric address, so the gateway sees
    only the host as the SSH source -- a devbox VM on vmbr7 cannot pose as a
    team to it (docs/security-assessment.md, devbox isolation)."""
    require("nft")
    inventory = yaml.safe_load((ANSIBLE / "inventory.yml").read_text())
    assert inventory["all"]["children"]["proxmox_host"]["hosts"]["proxmox.local"]["host_publish_ssh_gateway"] is True
    path = tmp_path / "worms.nft"
    text = render("ansible/roles/host/host_network", "worms.nft.j2", host_publish_ssh_gateway=True)
    # From the WAN, only 2222 is forwarded, and only to the gateway's SSH port.
    assert 'iifname "vmbr0" fib daddr type local tcp dport 2222 dnat ip to 10.7.0.2:22' in text
    from_wan = [line for line in text.splitlines() if "10.7.0.2" in line and 'iifname "vmbr0"' in line]
    assert len(from_wan) == 2 and 'oifname "vmbr7" ip daddr 10.7.0.2 tcp dport 22 ct status dnat accept' in from_wan[0]
    path.write_text(text)
    # Assert the rendered rules first (runs on every kernel): teams are DNATed
    # in, then that same connection is SNATed (masqueraded) to the host.
    assert "dnat ip to 10.7.0.2:22" in text
    assert 'oifname "vmbr7" ip daddr 10.7.0.2 tcp dport 22 ct status dnat masquerade' in text
    proc = subprocess.run(["nft", "-c", "-f", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        errors = [line for line in proc.stderr.splitlines() if "Error:" in line]
        if errors and all("Operation not supported" in e or "Permission denied" in e for e in errors):
            pytest.skip("this kernel cannot check the bridge family (nft -c): " + errors[0])
    assert proc.returncode == 0, proc.stderr


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


def test_zeek_can_rotate_its_logs(render):
    """Each ReadWritePaths entry is its own bind mount, and Zeek rotates by rename().
    With the log and archive directories listed apart, the first rotation failed with
    EXDEV and ended every log writer, while the service stayed active."""
    unit = render(MONITOR, "zeek.service.j2", **MONITOR_VARS)
    defaults = yaml.safe_load((ANSIBLE / "roles/applications/network_monitor/defaults/main.yml").read_text())
    log_dir, archive_dir = Path(defaults["network_monitor_log_dir"]), Path(defaults["network_monitor_archive_dir"])
    assert log_dir.parent == archive_dir.parent
    lines = [line.removeprefix("ReadWritePaths=") for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert lines == [str(log_dir.parent)]


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


def test_dashboard_nginx_limits_sign_ins(render, tmp_path):
    """The staff pages are on the public dashboard: sign-in POSTs are limited
    per client address (password guessing), page views are not."""
    dashboard = yaml.safe_load((REPO / "ansible" / "group_vars" / "dashboard.yml").read_text())
    orchestrator = yaml.safe_load((REPO / "ansible" / "group_vars" / "orchestrator.yml").read_text())
    assert dashboard.get("nginx_login_rate") and not orchestrator.get("nginx_login_rate")

    conf = render(NGINX_DJANGO, "worms-tls.conf.j2", nginx_login_rate="10r/m", tls_dns_name="dashboard.worms.internal")
    assert "map $request_method $worms_login_key {" in conf
    assert re.search(r"POST\s+\$binary_remote_addr;\s+default\s+\"\";", conf)
    assert "limit_req_zone $worms_login_key zone=login:10m rate=10r/m;" in conf
    login = conf[conf.index("location = /login/"):]
    login = login[:login.index("}")]
    assert "limit_req zone=login burst=5 nodelay;" in login and "limit_req_status 429;" in login
    assert "proxy_set_header Host $http_host;" in login

    off = render(NGINX_DJANGO, "worms-tls.conf.j2", tls_dns_name="orchestrator.worms.internal")
    assert "zone=login" not in off and "location = /login/" not in off


def test_sandbox_api_nginx_limits_are_per_source(render):
    """The sandbox API's nginx limits are keyed on the source address (the SNAT
    gateway), so one team's devbox-gw cannot spend the round's (sandbox-gw's) or
    another team's budget (docs/security-assessment.md, S5)."""
    conf = render("ansible/roles/applications/sandbox_api", "nginx-site.j2", tls_dns_name="scoring.worms.internal")
    assert "limit_req_zone $binary_remote_addr zone=sandbox_api_req" in conf
    assert "limit_conn_zone $binary_remote_addr zone=sandbox_api_conn" in conf
    assert "$server_name zone=sandbox_api" not in conf  # not a single global budget


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
    "WaitForVMs.yml", "SetupDNS.yml", "UpdateVMs.yml", "HardenSSH.yml", "CreateCA.yml", "DistributeCA.yml",
    "ConfigureNginx.yml", "ConfigureNginxDjango.yml", "SetupDbAndDashboard.yml", "SetupSandboxApi.yml",
    "SetupNetworkMonitor.yml", "SetupPrivateInternet.yml", "SetupAudit.yml", "SetupRoundAgent.yml",
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
    "syslog": ["syslog_server", "auditd"],
    **{pi: ["apt_client", pi, "auditd"] for pi in ("apt_cache", "devpi", "docker_registry", "manpages", "ntp",
                                                   "cve_search", "wikipedia")},
}  # fmt: skip
# Every VM first gets apt_updates (UpdateVMs.yml).
for _vm in set(ROLES) - {"proxmox_host", "localhost"}:
    ROLES[_vm] = ["apt_updates", *ROLES[_vm]]
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


@pytest.mark.parametrize("playbook", ["SetupSSHGateway.yml", "SetupDevbox.yml", "SetupSandbox.yml"])
def test_a_vm_just_built_gets_time_to_boot(playbook):
    """These run right after `tofu apply`, outside WaitForVMs.yml. Gathering facts
    needs SSH, so a VM still in its first boot would fail the play as unreachable
    before the wait for cloud-init. First boot took 3 minutes in a live build."""
    wait, *configure = yaml.safe_load((ANSIBLE / playbook).read_text()) or []
    assert wait["gather_facts"] is False
    assert "ansible.builtin.wait_for_connection" in wait["tasks"][0]
    for play in configure:
        assert play["hosts"] in wait["hosts"].split(":")
        assert play["any_errors_fatal"] == wait["any_errors_fatal"]


@pytest.mark.parametrize(
    "playbook, gateways, vms",
    [("SetupDevbox.yml", "devbox_gw", "devbox"), ("SetupSandbox.yml", "sandbox_gw", "sandbox")],
)
def test_untrusted_gateways_get_their_rules_before_their_vms_need_them(playbook, gateways, vms):
    """The cloud image has no nftables, so without this a gateway has no NAT and
    no filter: its VMs reach nothing, not even DNS, and apt hangs on them."""
    plays = [p for p in yaml.safe_load((ANSIBLE / playbook).read_text()) if p.get("roles")]
    assert [p["hosts"] for p in plays] == [gateways, vms]
    gateway_roles, vm_roles = ([r["role"] for r in p["roles"]] for p in plays)
    assert gateway_roles == ["apt_client", "untrusted_gateway"]
    assert "apt_client" in vm_roles


def test_untrusted_vms_use_the_private_internet_for_packages():
    inventory = yaml.safe_load((ANSIBLE / "inventory.yml").read_text())
    untrusted = inventory["all"]["children"]["untrusted"]["children"]
    assert set(untrusted) == {"sandbox", "sandbox_gw", "devbox", "devbox_gw"}
    settings = yaml.safe_load((ANSIBLE / "group_vars" / "untrusted.yml").read_text())
    # Not left to `groups['apt_cache']`: the round agent's inventory lacks that group.
    assert settings["apt_client_use_cache"] is True and settings["apt_client_refresh_offline"] is True
    # The cache does not hold the backports index: fatal for Ansible's apt module.
    assert "backports" not in settings["apt_client_suites"]


def test_the_cache_is_filled_with_what_untrusted_vms_install():
    """With the internet closed the cache serves only what it already holds."""
    roles = ANSIBLE / "roles"
    seed = yaml.safe_load((roles / "private_internet/apt_cache/defaults/main.yml").read_text())["apt_cache_seed_packages"]
    wanted = yaml.safe_load((roles / "applications/devbox/defaults/main.yml").read_text())["devbox_packages"]
    assert {"nftables", *wanted} <= set(seed)
    plays = yaml.safe_load((ANSIBLE / "SetupPrivateInternet.yml").read_text())
    seeding = [t for p in plays for t in p.get("tasks", []) if t.get("ansible.builtin.include_role", {}).get("tasks_from") == "seed"]
    assert [t["when"] for t in seeding] == ["internet_enabled | bool"]


def test_a_new_gateway_gets_as_far_as_installing_nftables():
    """The image has no nftables. A runcmd using it left cloud-init in `error`, which stopped
    SetupDevbox.yml before the role that installs it, on every freshly built gateway."""
    stacks = ANSIBLE.parent / "opentofu" / "stacks"
    for template in (stacks / "devbox/templates/devbox-gw.tftpl", stacks / "sandbox/templates/sandbox-gw.tftpl"):
        text = template.read_text()
        assert "path: /etc/nftables.conf" in text, template
        commands = text.split("\nruncmd:\n")[1]
        assert "nft" not in commands, template
    for playbook in ("SetupDevbox.yml", "SetupSandbox.yml"):
        gateway_play = next(p for p in yaml.safe_load((ANSIBLE / playbook).read_text()) if "untrusted_gateway" in str(p.get("roles")))
        # A real cloud-init failure must still stop the play.
        assert "[0, 2]" in str(gateway_play["pre_tasks"])


def test_the_pip_mirror_is_filled_while_the_internet_is_open():
    """devpi was empty after `make`, so every `pip install` on a devbox failed."""
    role = ANSIBLE / "roles/private_internet/devpi"
    assert yaml.safe_load((role / "defaults/main.yml").read_text())["devpi_seed_packages"]
    tasks = yaml.safe_load((role / "tasks/main.yml").read_text())
    seeding = [t for t in tasks if t.get("ansible.builtin.include_tasks") == "seed.yml"]
    assert [t["when"] for t in seeding] == ["internet_enabled | bool"]
    # After the wait for devpi and the password step: it needs the server running.
    assert tasks[-1] is seeding[0]
    # Without it, devpi tries to fetch PyPI's whole project list within a 5-second
    # timeout, and answers "does not exist" for every project.
    patch = next(t for t in tasks if t.get("ansible.builtin.uri", {}).get("method") == "PATCH" and t["ansible.builtin.uri"]["url"].endswith("/root/pypi"))
    assert patch["ansible.builtin.uri"]["body"] == ["mirror_no_project_list=True"]
    assert tasks.index(patch) < tasks.index(seeding[0])


def test_a_plain_pip_install_works_on_a_devbox():
    """Debian's pip refuses to install anything without this, before it asks the index."""
    tasks = yaml.safe_load((ANSIBLE / "roles/applications/devbox/tasks/main.yml").read_text())
    pip_conf = next(t for t in tasks if t.get("ansible.builtin.copy", {}).get("dest") == "/etc/pip.conf")
    assert "break-system-packages = true" in pip_conf["ansible.builtin.copy"]["content"]


# --- speed (ansible.cfg, apt) --------------------------------------------------


def test_speed_settings():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(ANSIBLE / "ansible.cfg")
    defaults, ssh = cfg["defaults"], cfg["ssh_connection"]
    assert int(defaults["forks"]) >= 16
    assert defaults["gathering"] == "smart" and defaults["fact_caching"] == "jsonfile"
    assert defaults["fact_caching_connection"] == ".facts"
    assert ".facts/" in (ANSIBLE / ".gitignore").read_text()
    assert ssh["pipelining"] == "true" and "ControlPersist=10m" in ssh["ssh_args"]


def test_synchronize_tasks_use_the_connections_ssh_args():
    """ansible.posix.synchronize spawns its own rsync->ssh, which ignores the
    connection's ssh args unless use_ssh_args is set. The VMs are reached
    through the node (ProxyJump) with the WORMS key in a private agent
    (IdentityAgent=$WORMS_SSH_AUTH_SOCK); without use_ssh_args rsync falls back
    to default keys and fails with "Permission denied (publickey)". Every
    synchronize task must opt in, so a sync against a jumped host keeps working.
    """
    found = 0
    for path in (ANSIBLE / "roles").rglob("*.yml"):
        for task in yaml.safe_load(path.read_text()) or []:
            if not isinstance(task, dict):
                continue
            sync = task.get("ansible.posix.synchronize") or task.get("synchronize")
            if isinstance(sync, dict):
                found += 1
                assert sync.get("use_ssh_args") is True, (
                    f"{path.relative_to(ANSIBLE)}: {task.get('name')} must set use_ssh_args: true"
                )
    assert found >= 2, "expected the django_app and round_agent synchronize tasks"


def test_apt_refreshes_its_index_at_most_hourly():
    """apt-get update on every run costs seconds per host and task; each apt
    task that refreshes the index says how old it may be."""
    for path in (ANSIBLE / "roles").rglob("*.yml"):
        for task in yaml.safe_load(path.read_text()) or []:
            if not isinstance(task, dict):
                continue
            apt = task.get("ansible.builtin.apt") or task.get("apt")
            if isinstance(apt, dict) and apt.get("update_cache") not in (None, False):
                assert "cache_valid_time" in apt, f"{path.relative_to(ANSIBLE)}: {task.get('name')}"


def test_apt_does_not_refresh_its_index_offline():
    """In the apt module cache_valid_time turns update_cache on, whatever
    update_cache says. A task that refreshes only while internet access is
    allowed must make cache_valid_time conditional too, or it tries the internet
    offline, and fails, once its index is an hour old."""
    for path in (ANSIBLE / "roles").rglob("*.yml"):
        for task in yaml.safe_load(path.read_text()) or []:
            if not isinstance(task, dict):
                continue
            apt = task.get("ansible.builtin.apt") or task.get("apt")
            if isinstance(apt, dict) and "internet_enabled" in str(apt.get("update_cache")):
                assert "internet_enabled" in str(apt.get("cache_valid_time")), f"{path.relative_to(ANSIBLE)}: {task.get('name')}"


def test_orchestrator_verifies_the_proxmox_api_with_both_tokens():
    """PVE's certificate names the node, not its address on the core bridge, and
    is issued by PVE's own CA. So the orchestrator connects by the node's name
    with that CA, for the power token as well as the read-only one."""
    import sys

    pytest.importorskip("django")
    env = dict(
        os.environ,
        PRODUCTION="1",
        DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
        DJANGO_SECRET_KEY="test",
        DJANGO_ALLOWED_HOSTS="localhost",
        DB_MIGRATOR_USER="x", DB_MIGRATOR_PASSWORD="x", DB_USER="x", DB_PASSWORD="x",
        DB_HOST="x", DB_NAME="x",
        PROXMOX_HOST="x", PROXMOX_USER="x", PROXMOX_TOKEN_NAME="x", PROXMOX_TOKEN_VALUE="x",
        PROXMOX_CA_BUNDLE="/some/ca.pem",
        PYTHONPATH=str(REPO / "projects" / "dashboard"),
    )  # fmt: skip
    proc = subprocess.run(
        [sys.executable, "-c",
         "import config.settings.orchestrator as s; print(s.PROXMOX['verify_ssl'], s.PROXMOX_READONLY['verify_ssl'])"],
        env=env, capture_output=True, text=True,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split()[-2:] == ["/some/ca.pem", "/some/ca.pem"]
    orchestrator = yaml.safe_load((ANSIBLE / "group_vars" / "orchestrator.yml").read_text())
    assert orchestrator["django_extra_env"]["PROXMOX_HOST"] == "{{ proxmox.node }}"
    assert "PROXMOX_CA_BUNDLE" in orchestrator["django_extra_env"]


def test_the_agents_workspace_does_not_look_for_a_vault_key():
    """The workspace holds no vault, by design, but the synced ansible.cfg names
    its key, and a missing key fails every playbook there (live, on the first
    devbox build)."""
    tasks = yaml.safe_load((ANSIBLE / "roles" / "host" / "round_agent" / "tasks" / "workspace.yml").read_text())
    names = [t.get("name") for t in tasks]
    sync = names.index("Sync the code")
    fixes = [
        i for i, t in enumerate(tasks)
        if (t.get("ansible.builtin.lineinfile") or {}).get("regexp", "").startswith("^vault_password_file")
        and t["ansible.builtin.lineinfile"].get("state") == "absent"
    ]  # fmt: skip
    assert fixes and fixes[0] > sync
