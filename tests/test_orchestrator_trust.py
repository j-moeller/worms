"""Existing Requests CA environment settings remain effective for the client."""
import os
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
