"""The sandbox-facing REST APIs (ansible/roles/applications/sandbox_api,
files/app.py) against the test database, as their INSERT-only roles:
a valid submission lands in the database; everything else is refused before
it gets there.

nginx (body limits, TLS, global rate limits) is not part of this test; the live
simulation (ansible/Simulate.yml) goes through it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import uuid

import pytest
import yaml

pytest.importorskip("flask")
psycopg2 = pytest.importorskip("psycopg2")

from conftest import REPO  # noqa: E402

APP = REPO / "ansible" / "roles" / "applications" / "sandbox_api" / "files" / "app.py"
SQL = REPO / "ansible" / "files" / "simulation"


@pytest.fixture
def load_app(monkeypatch):
    """load_app(kind, env): a fresh instance of app.py (module state: token
    cache, rate limits), configured through the environment like the service."""

    def _load(kind, env):
        group_vars = yaml.safe_load((REPO / "ansible" / "group_vars" / f"{kind}.yml").read_text())
        settings = {**env, "SANDBOX_API_KIND": kind, "MAX_BODY_BYTES": str(group_vars["sandbox_api_max_body_bytes"]), "TEAM_RATE_PER_MINUTE": "60", "TEAM_BURST": "3", "TOKEN_REFRESH_SECONDS": "0"}
        for key, value in settings.items():
            monkeypatch.setenv(key, value)
        spec = importlib.util.spec_from_file_location(f"sandbox_api_{kind}_{uuid.uuid4().hex}", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.app.test_client()

    return _load


@pytest.fixture
def team(database):
    """A team with a valid token, created and removed with the SQL the live
    simulation uses."""
    token = f"sim-{uuid.uuid4().hex}"
    params = {
        "run_id": uuid.uuid4().hex[:8],
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "flag_hash": hashlib.sha256(token.encode() + b"-flag").hexdigest(),
    }
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute((SQL / "setup.sql").read_text(), params)
        assert cur.fetchone()[0] > 0
    yield {"token": token, **params}
    with conn.cursor() as cur:
        cur.execute((SQL / "cleanup.sql").read_text(), params)
        cur.execute("SELECT count(*) FROM teams WHERE name = %s", ["simulation-" + params["run_id"]])
        assert cur.fetchone()[0] == 0
    conn.close()


def rows(database, table, token_hash):
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE team_token_hash = %s", [token_hash])
        result = cur.fetchall()
    conn.close()
    return result


def test_scoring_stores_a_flag(database, team, load_app):
    client = load_app("scoring", database.env("dbuser_scoring"))
    r = client.post("/submit", json={"team_api_token": team["token"], "vm_flag": "FLAG{simulation}"})
    assert r.status_code == 201, r.get_json()
    stored = rows(database, "flag_capture_attempts", team["token_hash"])
    assert len(stored) == 1
    assert hashlib.sha256(b"FLAG{simulation}").hexdigest() in stored[0]


def test_logging_stores_a_log(database, team, load_app):
    client = load_app("logging", database.env("dbuser_logging"))
    r = client.post("/submit", json={"team_api_token": team["token"], "log_data": {"event": "simulation", "n": 1}})
    assert r.status_code == 201, r.get_json()
    stored = rows(database, "logging_attempts", team["token_hash"])
    assert len(stored) == 1


@pytest.mark.parametrize(
    "kind, body, status",
    [
        ("scoring", {"team_api_token": "wrong", "vm_flag": "x"}, 401),
        ("scoring", {"vm_flag": "x"}, 400),
        ("scoring", {"team_api_token": "{token}", "vm_flag": ""}, 400),
        ("scoring", {"team_api_token": "{token}", "vm_flag": "x" * 65}, 400),
        ("scoring", {"team_api_token": "{token}", "vm_flag": "a\nb"}, 400),
        ("scoring", ["not", "an", "object"], 400),
        ("logging", {"team_api_token": "{token}", "log_data": "text"}, 400),
        ("logging", {"team_api_token": "{token}", "log_data": {}}, 400),
        ("logging", {"team_api_token": "{token}", "log_data": {"a": "x" * 9000}}, 400),
        ("logging", {"team_api_token": "{token}", "log_data": {"a": [[[[[[[[[[1]]]]]]]]]]}}, 400),
        ("logging", {"team_api_token": "{token}", "log_data": {str(i): i for i in range(300)}}, 400),
    ],
)  # fmt: skip
def test_rejected_before_the_database(database, team, kind, body, status, load_app):
    client = load_app(kind, database.env(f"dbuser_{kind}"))
    if isinstance(body, dict) and body.get("team_api_token") == "{token}":
        body = {**body, "team_api_token": team["token"]}
    r = client.post("/submit", json=body)
    assert r.status_code == status, r.get_json()
    table = "flag_capture_attempts" if kind == "scoring" else "logging_attempts"
    assert rows(database, table, team["token_hash"]) == []


def test_only_post_submit(database, load_app):
    client = load_app("scoring", database.env("dbuser_scoring"))
    assert client.get("/submit").status_code == 405
    assert client.post("/admin", json={}).status_code == 404
    assert client.post("/submit", data="x", content_type="text/plain").status_code == 415


def test_revoked_token_is_refused(database, team, load_app):
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute("UPDATE access_tokens SET revoked = true WHERE access_token_hash = %s", [team["token_hash"]])
    conn.close()
    client = load_app("scoring", database.env("dbuser_scoring"))
    r = client.post("/submit", json={"team_api_token": team["token"], "vm_flag": "x"})
    assert r.status_code == 401


def test_team_rate_limit(database, team, load_app):
    client = load_app("scoring", database.env("dbuser_scoring"))
    codes = [
        client.post("/submit", json={"team_api_token": team["token"], "vm_flag": f"f{i}"}).status_code
        for i in range(6)
    ]
    assert codes[:3] == [201, 201, 201]
    assert 429 in codes[3:]


def test_database_down_is_a_503(team, load_app):
    env = {"DB_HOST": "127.0.0.1", "DB_PORT": "1", "DB_NAME": "x", "DB_USER": "x", "DB_PASSWORD": "x", "DB_SSLMODE": "disable"}
    client = load_app("scoring", env)
    r = client.post("/submit", json={"team_api_token": team["token"], "vm_flag": "x"})
    # The token cannot be checked without the database: refused, not stored.
    assert r.status_code in (401, 503)
