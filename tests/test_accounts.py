"""Admin/competition init and student-account creation, against a real
PostgreSQL with the real migrations and least-privilege grants (tests/pg.py).

Covers:
  * manage.py init: creates the admin (is_staff + superuser) and the default
    competition, is idempotent, and writes through the migrator connection;
  * manage.py create_users: one account per CSV row with a one-time password
    and must_change_password, skips existing emails, prints email,password;
  * the dashboard DB role may set a user's password and clear the flag (the
    first-login change), but nothing else on the users table.
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from pg import DB_NAME, MIGRATOR, PASSWORD, REPO  # noqa: E402

DASHBOARD = REPO / "projects" / "dashboard"


def manage(cluster, *args):
    """Run manage.py with the orchestrator settings against the cluster (the
    migrator credentials are present, as on the real orchestrator)."""
    import os

    env = dict(
        os.environ,
        PRODUCTION="1",
        DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
        DJANGO_SECRET_KEY="test",
        DJANGO_ALLOWED_HOSTS="localhost",
        DB_MIGRATOR_USER=MIGRATOR,
        DB_MIGRATOR_PASSWORD=PASSWORD,
        PROXMOX_HOST="x", PROXMOX_USER="x", PROXMOX_TOKEN_NAME="x", PROXMOX_TOKEN_VALUE="x",
        **cluster.env("django_orchestrator"),
    )  # fmt: skip
    proc = subprocess.run(
        [sys.executable, "manage.py", *args, "--skip-checks"],
        cwd=DASHBOARD,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def run(cluster, role, sql, params=None):
    conn = cluster.connect(role, PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


# --- init ---------------------------------------------------------------------


def test_init_creates_admin_and_competition(database):
    manage(database, "init", "--competition", "WORMS TEST")

    admin = run(
        database, "postgres",
        "SELECT is_staff, is_superuser, is_active FROM users WHERE username = 'admin'",
    )  # fmt: skip
    assert admin == [(True, True, True)]

    comp = run(database, "postgres", "SELECT count(*) FROM competitions WHERE name = 'WORMS TEST'")
    assert comp[0][0] == 1
    current = run(
        database, "postgres",
        "SELECT c.name FROM application_setting a JOIN competitions c "
        "ON c.id = a.current_competition_id WHERE a.key = 'current'",
    )  # fmt: skip
    assert current == [("WORMS TEST",)]


def test_init_is_idempotent(database):
    manage(database, "init", "--competition", "WORMS TEST")
    manage(database, "init", "--competition", "WORMS TEST")

    assert run(database, "postgres", "SELECT count(*) FROM users WHERE username = 'admin'")[0][0] == 1
    assert run(database, "postgres", "SELECT count(*) FROM competitions WHERE name = 'WORMS TEST'")[0][0] == 1


# --- create_users -------------------------------------------------------------


def test_create_users_from_csv(database, tmp_path):
    csv_path = tmp_path / "students.csv"
    csv_path.write_text(
        "first name,surname,email\n"
        "Ada,Lovelace,ada@uni.example\n"
        "Alan,Turing,alan@uni.example\n"
    )
    proc = manage(database, "create_users", str(csv_path))

    rows = list(csv.DictReader(io.StringIO(proc.stdout)))
    emails = {r["email"]: r["password"] for r in rows}
    assert set(emails) == {"ada@uni.example", "alan@uni.example"}
    assert all(len(p) >= 10 for p in emails.values())

    users = run(
        database, "postgres",
        "SELECT username, must_change_password, is_staff, length(password) > 0 "
        "FROM users WHERE username LIKE '%@uni.example' ORDER BY username",
    )  # fmt: skip
    assert users == [
        ("ada@uni.example", True, False, True),
        ("alan@uni.example", True, False, True),
    ]


def test_create_users_skips_existing(database, tmp_path):
    csv_path = tmp_path / "s.csv"
    csv_path.write_text("first name,surname,email\nGrace,Hopper,grace@uni.example\n")
    first = manage(database, "create_users", str(csv_path))
    assert len(list(csv.DictReader(io.StringIO(first.stdout)))) == 1

    # Second run: the email already exists -> no new account, no new password.
    second = manage(database, "create_users", str(csv_path))
    assert list(csv.DictReader(io.StringIO(second.stdout))) == []
    assert run(database, "postgres", "SELECT count(*) FROM users WHERE username = 'grace@uni.example'")[0][0] == 1


def test_german_headers(database, tmp_path):
    csv_path = tmp_path / "de.csv"
    csv_path.write_text("Vorname,Nachname,E-Mail-Adresse\nKonrad,Zuse,zuse@uni.example\n")
    manage(database, "create_users", str(csv_path))
    assert run(database, "postgres", "SELECT first_name, last_name FROM users WHERE username = 'zuse@uni.example'") == [
        ("Konrad", "Zuse")
    ]


# --- the dashboard's first-login password change ------------------------------


def test_dashboard_may_change_password_only(database):
    # A user to act on.
    run(
        database, MIGRATOR,
        "INSERT INTO users (uuid, password, is_superuser, username, is_staff, is_active, "
        "date_joined, first_name, last_name, email, must_change_password, created_at) "
        "VALUES (gen_random_uuid(), 'x', false, 'pwtest', false, true, now(), '', '', "
        "'pwtest@uni.example', true, now())",
    )  # fmt: skip

    # Allowed: set the password and clear the one-time flag (first-login change).
    run(database, "django_dashboard", "UPDATE users SET password = 'new', must_change_password = false WHERE username = 'pwtest'")

    # Allowed: staff edit a student's name and email and deactivate it
    # (dashboard/views/staff_users.py).
    run(database, "django_dashboard", "UPDATE users SET first_name = 'P', email = 'p@x', is_active = false WHERE username = 'pwtest'")

    # Denied: the public role must not grant itself staff or touch other columns.
    for column in ("is_staff = true", "is_superuser = true", "username = 'renamed'"):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            run(database, "django_dashboard", f"UPDATE users SET {column} WHERE username = 'pwtest'")


@pytest.mark.parametrize("staff, superuser", [(False, False), (True, False), (False, True)])
def test_dashboard_creates_student_accounts_only(database, staff, superuser):
    """The staff pages create student accounts as the dashboard's role; a
    staff or superuser row is refused by the database (a trigger), so even a
    compromised dashboard cannot mint one. The console's admin role can."""
    insert = (
        "INSERT INTO users (uuid, password, is_superuser, username, is_staff, is_active, date_joined, "
        "first_name, last_name, email, must_change_password, created_at) VALUES (gen_random_uuid(), 'x', "
        f"{str(superuser).lower()}, %s, {str(staff).lower()}, true, now(), '', '', '', true, now())"
    )
    name = f"guard-{staff}-{superuser}"
    if staff or superuser:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="may not create staff"):
            run(database, "django_dashboard", insert % f"'{name}'")
        run(database, MIGRATOR, insert % f"'{name}-console'")
    else:
        run(database, "django_dashboard", insert % f"'{name}'")
        assert run(database, "postgres", "SELECT count(*) FROM users WHERE username = %s", (name,))[0][0] == 1
