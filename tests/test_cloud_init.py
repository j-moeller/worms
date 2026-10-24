"""Cloud-init snippets of the platform stack (module cloud_init) as tofu plans
them, and where every infrastructure VM gets its packages from (cloud-init or
the Ansible roles that run on it).

A snippet's file name is part of every VM built from it: the VM's
user_data_file_id is "<datastore>:snippets/<file name>", and the provider
replaces a VM whose user_data_file_id changes. So a snippet a stack uses
keeps its file name, or the database VM would be rebuilt empty.

The plan needs tofu and the bpg/proxmox provider (see test_tofu.py); it runs
offline with a fake endpoint (-refresh=false, nothing is contacted).
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest
import yaml

from ansible_static import ansible_packages
from conftest import REPO

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIWormsCloudInitRegressionTestKey test@worms"
HARDENING = (
    "printf 'PermitRootLogin prohibit-password\\nPasswordAuthentication no\\nKbdInteractiveAuthentication no\\n"
    "ChallengeResponseAuthentication no\\nPubkeyAuthentication yes\\nPermitEmptyPasswords no\\n'"
    " > /etc/ssh/sshd_config.d/10-worms-hardening.conf"
)
DONE = 'echo "done" > /tmp/cloud-config.done'

# Output key of the cloud_init module -> (file name, hostname, data disk mount).
SNIPPETS = {
    "blank": ("vm-blank.yml", "vm", None),
    "postgresql": ("vm-postgresq.yml", "postgresql", None),
    "django": ("vm-django.yml", "django", None),
    "wazuh": ("vm-wazuh.yml", "wazuh", None),
    "dns": ("vm-dns.yml", "dns", None),
    "flask": ("vm-flask.yml", "flask", None),
    "network_monitor": ("vm-network-monitor.yml", "network-monitor", None),
    "apt_cache": ("vm-apt-cache.yml", "apt-cache", "/var/cache/apt-cacher-ng"),
    "devpi": ("vm-devpi.yml", "devpi", "/var/lib/devpi"),
    "docker_registry": ("vm-docker-registry.yml", "docker-registry", "/var/lib/docker-registry"),
    "manpages": ("vm-manpages.yml", "manpages", None),
    "ntp": ("vm-ntp.yml", "ntp", None),
    "cve_search": ("vm-cve-search.yml", "cve-search", "/var/lib/mongodb"),
    "wikipedia": ("vm-wikipedia.yml", "wikipedia", "/var/lib/kiwix"),
}

# The packages each VM had after its first boot when cloud-init installed
# them (the templates of commit 6bb490a); now cloud-init or Ansible must.
PACKAGES = {
    "database": ["postgresql", "python3-psycopg2", "acl"],
    "dashboard": ["nginx", "python3", "python3-venv", "python3-pip", "build-essential", "fail2ban",
                  "openssl", "ca-certificates", "acl"],
    "scoring": ["nginx", "python3", "python3-venv", "python3-pip", "fail2ban", "git", "curl", "acl",
                "openssl", "ca-certificates"],
    "monitor": ["tcpdump", "tshark", "iproute2", "ethtool", "python3", "python3-venv", "ca-certificates",
                "curl", "gpg"],
    "dns": ["dnsmasq", "ca-certificates", "bind9-dnsutils"],
    "wazuh": ["curl", "ca-certificates", "gnupg"],
    "apt_cache": ["apt-cacher-ng", "ca-certificates"],
    "devpi": ["python3-pip", "python3-venv", "acl", "ca-certificates"],
    "docker_registry": ["docker-registry", "ca-certificates"],
    "manpages": ["apache2", "man2html", "man-db", "manpages", "manpages-dev", "ca-certificates"],
    "ntp": ["chrony", "ca-certificates"],
    "cve_search": ["git", "python3-pip", "python3-venv", "python3-dev", "build-essential", "ca-certificates"],
    "wikipedia": ["curl", "aria2", "kiwix-tools", "ca-certificates"],
}  # fmt: skip
PACKAGES["orchestrator"] = PACKAGES["dashboard"]
PACKAGES["logging"] = PACKAGES["scoring"]

# VM group -> its snippet.
VM_SNIPPET = {
    "database": "postgresql", "dashboard": "django", "orchestrator": "django", "wazuh": "wazuh",
    "dns": "dns", "scoring": "flask", "logging": "flask", "monitor": "network_monitor",
    **{pi: pi for pi in ("apt_cache", "devpi", "docker_registry", "manpages", "ntp", "cve_search", "wikipedia")},
}  # fmt: skip


@pytest.fixture(scope="module")
def platform_plan(tofu_copy, tmp_path_factory):
    stack = tofu_copy / "stacks" / "platform"
    key = tmp_path_factory.mktemp("key") / "id.pub"
    key.write_text(KEY + "\n")
    run = lambda *a: subprocess.run(["tofu", *a], cwd=stack, capture_output=True, text=True)  # noqa: E731
    init = run("init", "-backend=false", "-input=false", "-no-color")
    if init.returncode != 0:
        pytest.skip("cannot install the providers (registry/mirror): " + init.stderr.strip().splitlines()[-1])
    plan = run("plan", "-input=false", "-no-color", "-refresh=false", "-out=plan.bin",
               "-var", "proxmox_api_url=https://127.0.0.1:1/", "-var", "proxmox_api_token=x@pam!x=x",
               "-var", f"ssh_public_key={key}")  # fmt: skip
    assert plan.returncode == 0, plan.stdout + plan.stderr
    show = run("show", "-json", "plan.bin")
    assert show.returncode == 0, show.stderr
    return json.loads(show.stdout)


def snippets(plan) -> dict:
    """Output key -> (file name, content) of every snippet in the module."""
    out = {}
    for change in plan["resource_changes"]:
        if change["type"] != "proxmox_virtual_environment_file" or not change["address"].startswith("module.cloud_init."):
            continue
        # One resource per snippet (<key>_vm_static) or one for_each over them.
        key = change.get("index") if isinstance(change.get("index"), str) else change["name"].removesuffix("_vm_static")
        raw = change["change"]["after"]["source_raw"][0]
        out[key] = (raw["file_name"], raw["data"])
    return out


def used_snippets() -> set:
    """Snippets the VM stacks refer to (local.platform.cloud_init.<key>.file_id)."""
    used = set()
    for tf in (REPO / "opentofu" / "stacks").rglob("*.tf"):
        if ".terraform" not in tf.parts:
            used |= set(re.findall(r"cloud_init\.(\w+)\.file_id", tf.read_text()))
    return used


def test_every_used_snippet_keeps_its_file_name(platform_plan):
    used = used_snippets()
    assert used <= set(SNIPPETS), f"declare the new snippets in SNIPPETS: {used - set(SNIPPETS)}"
    planned = snippets(platform_plan)
    for key in sorted(used):
        assert key in planned, key
        assert planned[key][0] == SNIPPETS[key][0], f"{key}: renaming its file replaces its VMs"
    # ... and the platform output still hands each of them to the stacks.
    output = platform_plan["output_changes"]["platform"]
    exported = set(output["after"].get("cloud_init", {})) | set(output["after_unknown"].get("cloud_init", {}))
    assert used <= exported


def test_snippet_contract(platform_plan):
    """Hostname, key-only SSH as root, hardened sshd, the completion marker,
    and the data disk of the private-internet services."""
    planned = snippets(platform_plan)
    for key in sorted(used_snippets()):
        _file, hostname, mount = SNIPPETS[key]
        data = planned[key][1]
        assert data.startswith("#cloud-config\n"), key
        config = yaml.safe_load(data)
        assert config["hostname"] == hostname, key
        assert config["ssh_pwauth"] is False, key
        assert config["ssh_authorized_keys"] == [KEY], key
        assert HARDENING in config["runcmd"], key
        assert config["runcmd"][-1] == DONE, key
        if mount is None:
            assert "mounts" not in config and "disk_setup" not in config, key
        else:
            assert config["disk_setup"] == {"/dev/vdb": {"table_type": "gpt", "layout": True, "overwrite": False}}, key
            assert config["fs_setup"] == [{"device": "/dev/vdb1", "filesystem": "ext4", "label": "data",
                                           "overwrite": False}], key  # fmt: skip
            assert config["mounts"] == [["LABEL=data", mount, "ext4", "defaults,nofail", "0", "2"]], key


def test_every_vm_gets_its_packages(platform_plan):
    planned = snippets(platform_plan)
    for vm, packages in sorted(PACKAGES.items()):
        installed = set(yaml.safe_load(planned[VM_SNIPPET[vm]][1]).get("packages") or [])
        installed |= ansible_packages(vm)
        missing = set(packages) - installed
        assert not missing, f"{vm}: nothing installs {sorted(missing)}"


def test_static_reading_sees_the_known_installs():
    """The reader above finds what the roles install today (guards it from
    silently finding nothing)."""
    assert {"auditd", "rsyslog"} <= ansible_packages("database")
    assert {"python3", "python3-venv", "nginx"} <= ansible_packages("scoring")
    assert "python3-psycopg2" in ansible_packages("monitor")
    assert {"manpages-posix", "manpages-posix-dev"} <= ansible_packages("manpages")
