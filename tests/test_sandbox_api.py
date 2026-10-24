"""The sandbox-facing REST APIs (ansible/roles/applications/sandbox_api,
files/app.py) against the test database, as their INSERT-only roles.

No access token: the API records the running team from CURRENT_TEAM_FILE (the
round agent writes it at deploy). A valid submission lands in the database with
that team's uuid; everything else is refused before it gets there; with no
current team, submissions are refused (409).

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
def load_app(monkeypatch, tmp_path):
    """load_app(kind, env, team_uuid=...) -> (client, team_file). A fresh
    instance of app.py configured through the environment like the service;
    team_uuid (when given) is written to the current-team file."""

    def _load(kind, env, team_uuid=None, devbox_map=None):
        group_vars = yaml.safe_load((REPO / "ansible" / "group_vars" / f"{kind}.yml").read_text())
        team_file = tmp_path / f"current-team-{uuid.uuid4().hex}"
        team_file.write_text(team_uuid or "")
        devbox_file = tmp_path / f"devbox-teams-{uuid.uuid4().hex}"
        devbox_file.write_text(
            "".join(f"{ip} {tid}\n" for ip, tid in (devbox_map or {}).items())
        )
        settings = {
            **env, "SANDBOX_API_KIND": kind,
            "MAX_BODY_BYTES": str(group_vars["sandbox_api_max_body_bytes"]),
            "TEAM_RATE_PER_MINUTE": "60", "TEAM_BURST": "3",
            "CURRENT_TEAM_FILE": str(team_file),
            "DEVBOX_TEAMS_FILE": str(devbox_file),
            "CLIENT_IP_HEADER": "X-Client-IP",
        }
        for key, value in settings.items():
            monkeypatch.setenv(key, value)
        spec = importlib.util.spec_from_file_location(f"sandbox_api_{kind}_{uuid.uuid4().hex}", APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.app.test_client(), team_file

    return _load


@pytest.fixture
def team(database):
    """A team, round, run and VM created with the SQL the live simulation uses
    (the team's uuid is what the API records)."""
    run_id = uuid.uuid4().hex[:8]
    team_uuid = str(uuid.uuid4())
    params = {
        "run_id": run_id,
        "token_hash": hashlib.sha256(run_id.encode()).hexdigest(),
        "flag_hash": hashlib.sha256((run_id + "-flag").encode()).hexdigest(),
        "team_uuid": team_uuid,
    }
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute((SQL / "setup.sql").read_text(), params)
        assert cur.fetchone()[0] > 0
    yield {"uuid": team_uuid, **params}
    with conn.cursor() as cur:
        cur.execute((SQL / "cleanup.sql").read_text(), {"team_uuid": team_uuid})
        cur.execute("SELECT count(*) FROM teams WHERE name = %s", ["simulation-" + run_id])
        assert cur.fetchone()[0] == 0
    conn.close()


def rows(database, table, team_uuid):
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE team_uuid = %s", [team_uuid])
        result = cur.fetchall()
    conn.close()
    return result


def test_scoring_stores_a_flag(database, team, load_app):
    client, _ = load_app("scoring", database.env("dbuser_scoring"), team_uuid=team["uuid"])
    r = client.post("/submit", json={"vm_flag": "FLAG{simulation}"})
    assert r.status_code == 201, r.get_json()
    stored = rows(database, "flag_capture_attempts", team["uuid"])
    assert len(stored) == 1
    assert hashlib.sha256(b"FLAG{simulation}").hexdigest() in stored[0]


def test_logging_stores_a_log(database, team, load_app):
    client, _ = load_app("logging", database.env("dbuser_logging"), team_uuid=team["uuid"])
    r = client.post("/submit", json={"log_data": {"event": "simulation", "n": 1}})
    assert r.status_code == 201, r.get_json()
    assert len(rows(database, "logging_attempts", team["uuid"])) == 1


def test_devbox_submission_attributed_by_source(database, team, load_app):
    # A request from a known devbox-gateway address is a devbox submission for
    # that team, even with no active round (empty current-team file).
    devbox_ip = "10.3.64.7"
    client, _ = load_app(
        "scoring", database.env("dbuser_scoring"),
        team_uuid=None, devbox_map={devbox_ip: team["uuid"]},
    )  # fmt: skip
    r = client.post("/submit", json={"vm_flag": "FLAG{dev}"}, headers={"X-Client-IP": devbox_ip})
    assert r.status_code == 201, r.get_json()
    stored = rows(database, "flag_capture_attempts", team["uuid"])
    assert len(stored) == 1
    # Recorded as a devbox attempt (the `devbox` column is true).
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT devbox FROM flag_capture_attempts WHERE team_uuid = %s", [team["uuid"]])
        assert cur.fetchone()[0] is True
    conn.close()


def test_unknown_source_falls_back_to_round(database, team, load_app):
    # An address not in the devbox map is treated as round (sandbox) traffic:
    # it needs the current-team file.
    client, _ = load_app(
        "scoring", database.env("dbuser_scoring"),
        team_uuid=team["uuid"], devbox_map={"10.3.64.7": str(uuid.uuid4())},
    )  # fmt: skip
    r = client.post("/submit", json={"vm_flag": "FLAG{round}"}, headers={"X-Client-IP": "10.9.9.9"})
    assert r.status_code == 201, r.get_json()
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT devbox FROM flag_capture_attempts WHERE team_uuid = %s", [team["uuid"]])
        assert cur.fetchone()[0] is False  # attributed to the round, not devbox
    conn.close()


def test_no_active_round_is_refused(database, team, load_app):
    # Empty current-team file: no round is running, so nothing is accepted.
    client, _ = load_app("scoring", database.env("dbuser_scoring"), team_uuid=None)
    r = client.post("/submit", json={"vm_flag": "FLAG{x}"})
    assert r.status_code == 409, r.get_json()
    assert rows(database, "flag_capture_attempts", team["uuid"]) == []


def test_current_team_is_picked_up_when_the_file_changes(database, team, load_app):
    client, team_file = load_app("scoring", database.env("dbuser_scoring"), team_uuid=None)
    assert client.post("/submit", json={"vm_flag": "a"}).status_code == 409
    team_file.write_text(team["uuid"])  # the agent sets the running team
    assert client.post("/submit", json={"vm_flag": "a"}).status_code == 201


@pytest.mark.parametrize(
    "kind, body, status",
    [
        ("scoring", {"vm_flag": ""}, 400),
        ("scoring", {"vm_flag": "x" * 65}, 400),
        ("scoring", {"vm_flag": "a\nb"}, 400),
        ("scoring", {}, 400),
        ("scoring", ["not", "an", "object"], 400),
        ("logging", {"log_data": "text"}, 400),
        ("logging", {"log_data": {}}, 400),
        ("logging", {"log_data": {"a": "x" * 9000}}, 400),
        ("logging", {"log_data": {"a": [[[[[[[[[[1]]]]]]]]]]}}, 400),
        ("logging", {"log_data": {str(i): i for i in range(300)}}, 400),
    ],
)  # fmt: skip
def test_rejected_before_the_database(database, team, kind, body, status, load_app):
    client, _ = load_app(kind, database.env(f"dbuser_{kind}"), team_uuid=team["uuid"])
    r = client.post("/submit", json=body)
    assert r.status_code == status, r.get_json()
    table = "flag_capture_attempts" if kind == "scoring" else "logging_attempts"
    assert rows(database, table, team["uuid"]) == []


def test_only_post_submit(database, team, load_app):
    client, _ = load_app("scoring", database.env("dbuser_scoring"), team_uuid=team["uuid"])
    assert client.get("/submit").status_code == 405
    assert client.post("/admin", json={}).status_code == 404
    assert client.post("/submit", data="x", content_type="text/plain").status_code == 415


def test_team_rate_limit(database, team, load_app):
    client, _ = load_app("scoring", database.env("dbuser_scoring"), team_uuid=team["uuid"])
    codes = [client.post("/submit", json={"vm_flag": f"f{i}"}).status_code for i in range(6)]
    assert codes[:3] == [201, 201, 201]
    assert 429 in codes[3:]


def test_database_down_is_a_503(team, load_app):
    env = {"DB_HOST": "127.0.0.1", "DB_PORT": "1", "DB_NAME": "x", "DB_USER": "x", "DB_PASSWORD": "x", "DB_SSLMODE": "disable"}
    client, _ = load_app("scoring", env, team_uuid=team["uuid"])
    # The team is known (from the file), but the insert cannot reach the DB.
    r = client.post("/submit", json={"vm_flag": "x"})
    assert r.status_code == 503
