"""Devbox scoring/logging and the version reset, against a real PostgreSQL with
the real migrations and least-privilege grants (tests/pg.py).

Exercises the real attempt processor (orchestrator `manage.py process_attempts
--once`, as the orchestrator role): devbox flag attempts become DevboxCaptures
of the team's own devbox VMs at the current version, devbox logs become
DevboxLogs, a token counts once per version, and a reset (version bump) starts
a fresh score without deleting the old captures.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from pg import DB_NAME, MIGRATOR, PASSWORD, REPO  # noqa: E402

DASHBOARD = REPO / "projects" / "dashboard"


def sql(cluster, query, params=None, role="postgres", fetch=True):
    conn = cluster.connect(role, PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if (fetch and cur.description) else None
    finally:
        conn.close()


def process_attempts(cluster):
    """Run the real processor once as the orchestrator role."""
    env = dict(
        os.environ,
        PRODUCTION="1",
        DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
        DJANGO_SECRET_KEY="t", DJANGO_ALLOWED_HOSTS="localhost",
        DB_MIGRATOR_USER=MIGRATOR, DB_MIGRATOR_PASSWORD=PASSWORD,
        PROXMOX_HOST="x", PROXMOX_USER="x", PROXMOX_TOKEN_NAME="x", PROXMOX_TOKEN_VALUE="x",
        **cluster.env("django_orchestrator"),
    )  # fmt: skip
    proc = subprocess.run(
        [sys.executable, "manage.py", "process_attempts", "--once", "--skip-checks"],
        cwd=DASHBOARD, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def hash_of(flag):
    return hashlib.sha256(flag.encode()).hexdigest()


@pytest.fixture
def devbox_team(database):
    """A team with a devbox of two VMs (tokens), created as the table owner."""
    team_uuid = str(uuid.uuid4())
    comp = sql(database, "INSERT INTO competitions (name, created_at, is_dev_competition) "
               "VALUES ('devbox-test', now(), false) RETURNING id", role=MIGRATOR)[0][0]
    team_id = sql(database, "INSERT INTO teams (uuid, name, competition_id, max_members, created_at, devbox_version) "
                  "VALUES (%s, 'devbox-team', %s, 2, now(), 1) RETURNING id",
                  [team_uuid, comp], role=MIGRATOR)[0][0]
    devbox_id = sql(database, "INSERT INTO devboxes (team_id, uuid, services_source_ip, ssh_gateway_host, "
                    "ssh_gateway_port, created_at) VALUES (%s, gen_random_uuid(), '10.3.64.7', 'gw', 2222, now()) "
                    "RETURNING id", [team_id], role=MIGRATOR)[0][0]
    flags = {"a": hash_of("FLAG{a}"), "b": hash_of("FLAG{b}")}
    for role, fh in flags.items():
        sql(database, "INSERT INTO devbox_vms (devbox_id, uuid, vm_identifier, role, vm_maintenance_ip, "
            "addresses, networks, ctf_flag_hash, created_at) VALUES (%s, gen_random_uuid(), %s, %s, '', "
            "'[]'::jsonb, '[]'::jsonb, %s, now())",
            [devbox_id, f"devbox-{role}", role, fh], role=MIGRATOR, fetch=False)
    yield {"team_uuid": team_uuid, "team_id": team_id, "flags": flags}
    # Clean up children first (Django cascades are ORM-level, not DB-level).
    for stmt, args in [
        ("DELETE FROM devbox_captures WHERE team_id = %s", [team_id]),
        ("DELETE FROM devbox_logs WHERE team_id = %s", [team_id]),
        ("DELETE FROM flag_capture_attempts WHERE team_uuid = %s", [team_uuid]),
        ("DELETE FROM logging_attempts WHERE team_uuid = %s", [team_uuid]),
        ("DELETE FROM devbox_vms WHERE devbox_id IN (SELECT id FROM devboxes WHERE team_id = %s)", [team_id]),
        ("DELETE FROM devboxes WHERE team_id = %s", [team_id]),
        ("DELETE FROM teams WHERE id = %s", [team_id]),
        ("DELETE FROM competitions WHERE id = %s", [comp]),
    ]:
        sql(database, stmt, args, role=MIGRATOR, fetch=False)


def submit_flag(cluster, team_uuid, flag_hash, devbox=True):
    sql(cluster, "INSERT INTO flag_capture_attempts (team_uuid, vm_flag_hash, devbox) VALUES (%s, %s, %s)",
        [team_uuid, flag_hash, devbox], role=MIGRATOR, fetch=False)


def devbox_captures(cluster, team_id, version):
    return sql(cluster, "SELECT count(*) FROM devbox_captures WHERE team_id = %s AND devbox_version = %s",
               [team_id, version])[0][0]


def test_devbox_tokens_scored_at_current_version(database, devbox_team):
    t = devbox_team
    submit_flag(database, t["team_uuid"], t["flags"]["a"])
    submit_flag(database, t["team_uuid"], t["flags"]["b"])
    process_attempts(database)
    assert devbox_captures(database, t["team_id"], 1) == 2


def test_token_counts_once_per_version(database, devbox_team):
    t = devbox_team
    submit_flag(database, t["team_uuid"], t["flags"]["a"])
    submit_flag(database, t["team_uuid"], t["flags"]["a"])  # same token twice
    process_attempts(database)
    assert devbox_captures(database, t["team_id"], 1) == 1


def test_reset_starts_fresh_score_but_keeps_records(database, devbox_team):
    t = devbox_team
    submit_flag(database, t["team_uuid"], t["flags"]["a"])
    process_attempts(database)
    assert devbox_captures(database, t["team_id"], 1) == 1

    # Reset: bump the version (what the orchestrator does on reset).
    sql(database, "UPDATE teams SET devbox_version = 2 WHERE id = %s", [t["team_id"]], role=MIGRATOR, fetch=False)

    # A new submission scores at v2; v1's capture is untouched.
    submit_flag(database, t["team_uuid"], t["flags"]["a"])
    process_attempts(database)
    assert devbox_captures(database, t["team_id"], 1) == 1  # old record kept
    assert devbox_captures(database, t["team_id"], 2) == 1  # fresh score


def test_foreign_token_not_scored(database, devbox_team):
    t = devbox_team
    submit_flag(database, t["team_uuid"], hash_of("FLAG{not-mine}"))
    process_attempts(database)
    assert devbox_captures(database, t["team_id"], 1) == 0


def test_devbox_logs_tagged_with_version(database, devbox_team):
    t = devbox_team
    sql(database, "INSERT INTO logging_attempts (uuid, team_uuid, log_data, devbox) "
        "VALUES (gen_random_uuid(), %s, '{\"e\": 1}'::jsonb, true)", [t["team_uuid"]], role=MIGRATOR, fetch=False)
    process_attempts(database)
    assert sql(database, "SELECT count(*) FROM devbox_logs WHERE team_id = %s AND devbox_version = 1",
               [t["team_id"]])[0][0] == 1


def test_orchestrator_role_may_write_devbox_tables(database, devbox_team):
    """The least-privilege orchestrator role (not the owner) must be able to
    insert devbox captures/logs and bump the version -- the processor runs as
    it."""
    t = devbox_team
    submit_flag(database, t["team_uuid"], t["flags"]["a"])
    process_attempts(database)  # runs as django_orchestrator
    assert devbox_captures(database, t["team_id"], 1) == 1
