"""The worm contract (docs/worm-contract.md): the sample worm runs under bash and
what it sends is accepted by the real scoring API, and the dashboard no longer
tells teams to send a field the servers ignore."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import REPO

WORM = REPO / "worm.sh"
TEMPLATES = REPO / "projects" / "dashboard" / "dashboard" / "templates"
FLAG = "FLAG{sample-worm}"
# Answers as the server would, and notes what it was asked to send.
FAKE_CURL = """#!/bin/bash
while [ $# -gt 0 ]; do
  case "$1" in
    -d) body=$2; shift 2 ;;
    https://*) url=$1; shift ;;
    *) shift ;;
  esac
done
printf '%s %s\\n' "$url" "$body" >> "$CURL_LOG"
printf 201
"""


def run_the_worm(tmp_path) -> list[tuple[str, str]]:
    """What worm.sh posts when run as the VM runs it, `bash main.sh`: [(url, body)]."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    flag = tmp_path / "flag"
    flag.write_text(FLAG + "\n")
    log = tmp_path / "curl.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FLAG_PATH": str(flag), "CURL_LOG": str(log)}
    subprocess.run(["bash", str(WORM)], check=True, env=env, cwd=tmp_path, capture_output=True, text=True)
    return [tuple(line.split(" ", 1)) for line in log.read_text().splitlines()]


def test_the_sample_worm_posts_its_flag_and_one_log(tmp_path):
    source = WORM.read_text()
    assert "/etc/worms/flag" in source
    assert "team_api_token" not in source  # scoring and logging ignore it (docs/rounds.md)
    (scoring_url, scoring_body), (logging_url, logging_body) = run_the_worm(tmp_path)
    assert scoring_url == "https://scoring.worms.internal/submit"
    assert json.loads(scoring_body) == {"vm_flag": FLAG}
    assert logging_url == "https://logging.worms.internal/submit"
    assert json.loads(logging_body)["log_data"]["scoring_status"] == 201


def test_the_sample_worm_captures_its_vm_against_the_real_api(tmp_path, monkeypatch):
    """Post worm.sh's own body to the scoring API (app.py) with the sandbox-gw source,
    as the round makes it: the flag becomes one attempt."""
    pytest.importorskip("flask")
    sys.path.insert(0, str(REPO / "ansible" / "roles" / "applications" / "sandbox_api" / "files"))

    import importlib

    flag = FLAG
    current_team = tmp_path / "current-team"
    team_uuid = "11111111-2222-3333-4444-555555555555"
    current_team.write_text(team_uuid)
    monkeypatch.setenv("SANDBOX_API_KIND", "scoring")
    monkeypatch.setenv("CURRENT_TEAM_FILE", str(current_team))
    monkeypatch.setenv("DEVBOX_TEAMS_FILE", str(tmp_path / "devbox-teams"))
    monkeypatch.setenv("SANDBOX_GW_IP", "10.2.0.10")
    monkeypatch.setenv("CLIENT_IP_HEADER", "X-Client-IP")

    inserted = []
    import app as sandbox_app

    importlib.reload(sandbox_app)
    monkeypatch.setattr(sandbox_app, "execute", lambda sql, params: inserted.append(params))

    # worm.sh's own body, posted the way the sandbox-gw source would.
    (_, body), _ = run_the_worm(tmp_path)
    client = sandbox_app.app.test_client()
    response = client.post("/submit", data=body,
                           content_type="application/json", headers={"X-Client-IP": "10.2.0.10"})  # fmt: skip
    assert response.status_code == 201, response.data
    # One attempt: the running team's uuid and the flag's hash (not the flag).
    import hashlib

    team, vm_hash, devbox = inserted[0]
    assert team == team_uuid and vm_hash == hashlib.sha256(flag.encode()).hexdigest() and devbox is False


def test_no_template_advertises_the_removed_token_field():
    for path in TEMPLATES.rglob("*.html"):
        assert "team_api_token" not in path.read_text(), path
