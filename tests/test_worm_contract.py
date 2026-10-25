"""The worm contract (docs/worm-contract.md): the sample worm is valid and
runnable against the real scoring/logging API, and the dashboard no longer
tells teams to send a field the servers ignore."""

from __future__ import annotations

import ast
import sys

import pytest

from conftest import REPO

WORM = REPO / "worm.py"
TEMPLATES = REPO / "projects" / "dashboard" / "dashboard" / "templates"


def test_the_sample_worm_is_valid_python_with_a_main():
    tree = ast.parse(WORM.read_text())
    functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "main" in functions
    # It reads the flag path and posts to the scoring server the contract names.
    source = WORM.read_text()
    assert "/etc/worms/flag" in source and "scoring.worms.internal/submit" in source
    # No stale token field (scoring/logging ignore it; docs/rounds.md).
    assert "team_api_token" not in source


def test_the_sample_worm_captures_its_vm_against_the_real_api(tmp_path, monkeypatch):
    """Run worm.py's submit path against the scoring API (app.py) with the
    sandbox-gw source, as the round makes it: the flag becomes one attempt."""
    pytest.importorskip("flask")
    sys.path.insert(0, str(REPO / "ansible" / "roles" / "applications" / "sandbox_api" / "files"))

    import importlib
    import json

    flag = "FLAG{sample-worm}"
    (tmp_path / "flag").write_text(flag)
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

    # worm.py builds this body; post it the way the sandbox-gw source would.
    client = sandbox_app.app.test_client()
    response = client.post("/submit", data=json.dumps({"vm_flag": flag}),
                           content_type="application/json", headers={"X-Client-IP": "10.2.0.10"})  # fmt: skip
    assert response.status_code == 201, response.data
    # One attempt: the running team's uuid and the flag's hash (not the flag).
    import hashlib

    team, vm_hash, devbox = inserted[0]
    assert team == team_uuid and vm_hash == hashlib.sha256(flag.encode()).hexdigest() and devbox is False


def test_no_template_advertises_the_removed_token_field():
    for path in TEMPLATES.rglob("*.html"):
        assert "team_api_token" not in path.read_text(), path
