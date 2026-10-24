"""The `worms` management CLI (src/manage.py), DB-backed commands against a
real PostgreSQL with the real migrations and grants (tests/pg.py).

The infrastructure probes (Proxmox, SSH) are not exercised here -- they degrade
to `n/a`/`unknown` without configuration, which is covered by the unit check of
the unconfigured status output below.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from pg import DB_NAME, MIGRATOR, PASSWORD, REPO  # noqa: E402

MANAGE = REPO / "src" / "manage.py"


def worms(cluster, *args, check=True):
    """Run `worms <args>` with the CLI pointed at the test cluster as the
    migrator (owns the tables: can read all and manage teams)."""
    env = dict(
        os.environ,
        WORMS_DB_HOST="127.0.0.1",
        WORMS_DB_PORT=str(cluster.port),
        WORMS_DB_NAME=DB_NAME,
        WORMS_DB_USER=MIGRATOR,
        WORMS_DB_PASSWORD=PASSWORD,
        WORMS_DB_SSLMODE="disable",
        NO_COLOR="1",
    )
    proc = subprocess.run(
        [sys.executable, str(MANAGE), *args],
        env=env, capture_output=True, text=True,
    )  # fmt: skip
    if check:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def sql(cluster, query, params=None, role="postgres"):
    conn = cluster.connect(role, PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


@pytest.fixture
def initialised(database):
    """A database with the admin + default competition (manage.py init)."""
    env = dict(
        os.environ,
        PRODUCTION="1",
        DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
        DJANGO_SECRET_KEY="t", DJANGO_ALLOWED_HOSTS="localhost",
        DB_MIGRATOR_USER=MIGRATOR, DB_MIGRATOR_PASSWORD=PASSWORD,
        PROXMOX_HOST="x", PROXMOX_USER="x", PROXMOX_TOKEN_NAME="x", PROXMOX_TOKEN_VALUE="x",
        DJANGO_ADMIN_PASSWORD="pw",
        **database.env("django_orchestrator"),
    )  # fmt: skip
    subprocess.run(
        [sys.executable, "manage.py", "init", "--competition", "WORMS CLI", "--skip-checks"],
        cwd=REPO / "projects" / "dashboard", env=env, capture_output=True, text=True, check=True,
    )
    return database


def test_help_runs_without_db():
    proc = subprocess.run([sys.executable, str(MANAGE), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0 and "worms" in proc.stdout


def test_status_degrades_without_config():
    proc = subprocess.run(
        [sys.executable, str(MANAGE), "status"],
        capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("WORMS_")} | {"NO_COLOR": "1"},
    )
    assert proc.returncode == 0
    assert "no Proxmox API configured" in proc.stdout
    assert "no database configured" in proc.stdout


def test_teams_crud_and_listing(initialised):
    cluster = initialised
    worms(cluster, "teams", "create", "Alpha")
    worms(cluster, "teams", "create", "Beta")
    assert sql(cluster, "SELECT count(*) FROM teams WHERE name IN ('Alpha','Beta')")[0][0] == 2

    # Duplicate create is refused.
    dup = worms(cluster, "teams", "create", "Alpha", check=False)
    assert dup.returncode != 0 and "already exists" in (dup.stdout + dup.stderr)

    worms(cluster, "teams", "rename", "Alpha", "Gamma")
    assert sql(cluster, "SELECT count(*) FROM teams WHERE name = 'Gamma'")[0][0] == 1

    listing = worms(cluster, "teams")
    assert "Gamma" in listing.stdout and "Beta" in listing.stdout

    worms(cluster, "teams", "delete", "Beta", "--yes")
    assert sql(cluster, "SELECT count(*) FROM teams WHERE name = 'Beta'")[0][0] == 0


def test_delete_team_keeps_users(initialised):
    cluster = initialised
    worms(cluster, "teams", "create", "WithMembers")
    # A user and a membership (as the table owner).
    sql(cluster, "INSERT INTO users (uuid, password, is_superuser, username, is_staff, is_active, "
        "date_joined, first_name, last_name, email, must_change_password, created_at) VALUES "
        "(gen_random_uuid(), 'x', false, 'stud@x', false, true, now(), '', '', 'stud@x', true, now())",
        role=MIGRATOR)
    sql(cluster, "INSERT INTO team_memberships (user_id, team_id, created_at) SELECT u.id, t.id, now() "
        "FROM users u, teams t WHERE u.username = 'stud@x' AND t.name = 'WithMembers'", role=MIGRATOR)

    worms(cluster, "teams", "delete", "WithMembers", "--yes")
    assert sql(cluster, "SELECT count(*) FROM teams WHERE name = 'WithMembers'")[0][0] == 0
    # The user survives.
    assert sql(cluster, "SELECT count(*) FROM users WHERE username = 'stud@x'")[0][0] == 1


def test_status_and_leaderboard_with_db(initialised):
    cluster = initialised
    # (The DB is a shared session fixture, so the current competition name may
    # have been set by another test; assert structure, not a specific name.)
    worms(cluster, "teams", "create", "Solo")
    status = worms(cluster, "status")
    assert "competition" in status.stdout
    assert "teams" in status.stdout

    board = worms(cluster, "leaderboard")
    assert "leaderboard" in board.stdout
    assert "Solo" in board.stdout


def test_users_listing(initialised):
    cluster = initialised
    sql(cluster, "INSERT INTO users (uuid, password, is_superuser, username, is_staff, is_active, "
        "date_joined, first_name, last_name, email, must_change_password, created_at) VALUES "
        "(gen_random_uuid(), 'x', false, 'newbie@x', false, true, now(), '', '', 'newbie@x', true, now())",
        role=MIGRATOR)
    out = worms(cluster, "users").stdout
    assert "newbie@x" in out and "never" in out


def test_reset_database(initialised):
    cluster = initialised
    worms(cluster, "teams", "create", "Doomed")
    assert sql(cluster, "SELECT count(*) FROM teams")[0][0] >= 1
    worms(cluster, "reset-database", "--yes")
    # Teams wiped; init recreated the admin + default competition.
    assert sql(cluster, "SELECT count(*) FROM teams")[0][0] == 0
    assert sql(cluster, "SELECT count(*) FROM users WHERE username = 'admin'")[0][0] == 1
    assert sql(cluster, "SELECT count(*) FROM competitions")[0][0] == 1
