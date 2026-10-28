"""Existing Requests CA environment settings remain effective for the client."""
import os
import json
import subprocess
import sys

import pytest
from conftest import REPO


@pytest.mark.parametrize("overrides,expected", [
    ({"REQUESTS_CA_BUNDLE": "/fixture/requests-ca.pem"}, "/fixture/requests-ca.pem"),
    ({"CURL_CA_BUNDLE": "/fixture/curl-ca.pem"}, "/fixture/curl-ca.pem"),
    ({"REQUESTS_CA_BUNDLE": "/fixture/requests-ca.pem", "ORCHESTRATOR_CA_BUNDLE": "/fixture/orchestrator-ca.pem"},
     "/fixture/orchestrator-ca.pem"),
])
def test_ca_environment_overrides_the_system_default(overrides, expected):
    env = {k:v for k,v in os.environ.items() if not k.startswith(("DB_", "DJANGO_", "ORCHESTRATOR_"))
           and k not in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")}
    env.update(PRODUCTION="1", DJANGO_SECRET_KEY="fixture", DJANGO_ALLOWED_HOSTS="localhost",
               DB_HOST="localhost", DB_NAME="unused", DB_USER="unused", DB_PASSWORD="unused", **overrides)
    result = subprocess.run([sys.executable, "-c", "from config.settings.dashboard import ORCHESTRATOR_API; "
        "print(ORCHESTRATOR_API['ca_bundle'])"], cwd=REPO / "projects/dashboard", env=env,
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("overrides,expected", [
    ({}, "system"),
    ({"REQUESTS_CA_BUNDLE": "/fixture/requests.pem"}, "/fixture/requests.pem"),
    ({"CURL_CA_BUNDLE": "/fixture/curl.pem"}, "/fixture/curl.pem"),
    ({"PROXMOX_CA_BUNDLE": "/fixture/pve.pem", "REQUESTS_CA_BUNDLE": "/fixture/requests.pem"},
     "/fixture/pve.pem"),
    ({"PROXMOX_VERIFY_SSL": "0"}, False),
    ({"PROXMOX_CA_BUNDLE": "/fixture/pve.pem", "PROXMOX_VERIFY_SSL": "0"}, "/fixture/pve.pem"),
])
def test_power_and_topology_share_the_selected_ca(tmp_path, overrides, expected):
    bundle = tmp_path / "system.pem"
    bundle.touch()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PROXMOX_", "DB_", "DJANGO_"))
           and k not in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE")}
    env.update(PRODUCTION="1", DJANGO_SECRET_KEY="fixture", DJANGO_ALLOWED_HOSTS="localhost",
               DB_HOST="unused", DB_NAME="unused", DB_USER="unused", DB_PASSWORD="unused",
               DB_MIGRATOR_USER="unused", DB_MIGRATOR_PASSWORD="unused",
               PROXMOX_HOST="unused", PROXMOX_USER="unused", PROXMOX_TOKEN_NAME="unused",
               PROXMOX_TOKEN_VALUE="unused", SSL_CERT_FILE=str(bundle), **overrides)
    result = subprocess.run([sys.executable, "-c",
        "import json; from config.settings.orchestrator import PROXMOX, PROXMOX_READONLY; "
        "print(json.dumps([PROXMOX['verify_ssl'], PROXMOX_READONLY['verify_ssl']]))"],
        cwd=REPO / "projects/dashboard", env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    value = str(bundle) if expected == "system" else expected
    assert json.loads(result.stdout) == [value, value]
