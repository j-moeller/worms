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


def test_status_degrades_without_config(tmp_path):
    # A checkout without a vault (a developer's own checkout may have one).
    proc = subprocess.run(
        [sys.executable, str(MANAGE), "status"],
        capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("WORMS_")}
        | {"NO_COLOR": "1", "WORMS_REPO_ROOT": str(tmp_path), "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0
    assert "no Proxmox API configured" in proc.stdout
    assert "no database configured" in proc.stdout


def add_team(cluster, name):
    """A team in the current competition (teams are created on the dashboard,
    not by the CLI any more: dashboard/views/staff_teams.py)."""
    sql(cluster, "INSERT INTO teams (uuid, name, competition_id, max_members, created_at, devbox_version) "
        "SELECT gen_random_uuid(), %s, current_competition_id, 2, now(), 1 FROM application_setting "
        "WHERE key = 'current'", (name,), role=MIGRATOR)


def test_teams_listing_and_delete(initialised):
    cluster = initialised
    add_team(cluster, "Alpha")
    add_team(cluster, "Beta")
    listing = worms(cluster, "teams")
    assert "Alpha" in listing.stdout and "Beta" in listing.stdout

    worms(cluster, "teams", "delete", "Beta", "--yes")
    assert sql(cluster, "SELECT count(*) FROM teams WHERE name = 'Beta'")[0][0] == 0


def test_teams_are_created_on_the_dashboard_not_here(initialised):
    proc = worms(initialised, "teams", "create", "Nope", check=False)
    assert proc.returncode != 0
    assert "dashboard" in worms(initialised, "teams", "--help").stdout


def test_devbox_provision_enqueues_a_host_job(initialised):
    """Building devboxes is infrastructure work: the console enqueues it for
    the round agent (the dashboard only creates the team)."""
    cluster = initialised
    add_team(cluster, "DevTeam")
    worms(cluster, "devbox", "provision")
    # A Devbox row with a stable index + source address, and a provision job.
    row = sql(cluster, "SELECT index, services_source_ip FROM devboxes d "
              "JOIN teams t ON t.id = d.team_id WHERE t.name = 'DevTeam'")
    assert row and row[0][0] is not None and row[0][1].startswith("10.2.0.")
    jobs = sql(cluster, "SELECT count(*) FROM host_jobs WHERE kind = 'devbox_provision' AND state = 'queued'")[0][0]
    assert jobs >= 1
    # The same team list again: the queued job already covers it.
    worms(cluster, "devbox", "provision")
    assert sql(cluster, "SELECT count(*) FROM host_jobs WHERE kind = 'devbox_provision' AND state = 'queued'")[0][0] == jobs


def test_delete_team_keeps_users(initialised):
    cluster = initialised
    add_team(cluster, "WithMembers")
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
    add_team(cluster, "Solo")
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
    add_team(cluster, "Doomed")
    assert sql(cluster, "SELECT count(*) FROM teams")[0][0] >= 1
    worms(cluster, "reset-database", "--yes")
    # Teams wiped; init recreated the admin + default competition.
    assert sql(cluster, "SELECT count(*) FROM teams")[0][0] == 0
    assert sql(cluster, "SELECT count(*) FROM users WHERE username = 'admin'")[0][0] == 1
    assert sql(cluster, "SELECT count(*) FROM competitions")[0][0] == 1


# --- configuration defaults (no database needed) ---------------------------------


def _cli_module():
    import importlib.util

    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("worms_manage", MANAGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_internet_probe_runs_locally_on_the_node(tmp_path, monkeypatch):
    """Without WORMS_SSH the CLI is on the node (the build steps run locally
    too), so `status` reads the host flag directly."""
    cli = _cli_module()
    from worms_cli.config import Config

    config = Config(env={})
    monkeypatch.setattr(cli, "PVE_CONFIG_DIR", str(tmp_path / "missing"))
    assert "set WORMS_SSH" in cli._internet_status(config)[1]
    monkeypatch.setattr(cli, "PVE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "INTERNET_FLAG", str(tmp_path / "internet-allowed"))
    assert "denied" in cli._internet_status(config)[1]
    (tmp_path / "internet-allowed").touch()
    assert "ALLOWED" in cli._internet_status(config)[1]


def test_database_ca_defaults_to_the_internal_ca(tmp_path):
    sys.path.insert(0, str(REPO / "src"))
    from worms_cli.config import Config

    config = Config(env={"WORMS_REPO_ROOT": str(tmp_path), "WORMS_DB_HOST": "10.11.0.16"})
    assert config.db_env()["WORMS_DB_SSLROOTCERT"] == "/etc/ssl/certs/ca-certificates.crt"
    ca = tmp_path / "ansible" / "pki" / "worms" / "ca.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text("x")
    assert config.db_env()["WORMS_DB_SSLROOTCERT"] == str(ca)
    assert config.db_env()["WORMS_DB_USER"] == "admin" and config.db_env()["WORMS_DB_NAME"] == "mydb"


def test_proxmox_url_keeps_its_port(monkeypatch):
    import types

    sys.path.insert(0, str(REPO / "src"))
    from worms_cli.config import Config
    from worms_cli.proxmox import Infra

    seen = {}
    fake = types.ModuleType("proxmoxer")
    fake.ProxmoxAPI = lambda host, **kw: seen.update(host=host, **kw) or object()
    monkeypatch.setitem(sys.modules, "proxmoxer", fake)
    for url, host, port in [("https://10.11.0.1:8006/", "10.11.0.1", 8006), ("https://pve.lab:9443", "pve.lab", 9443),
                            ("127.0.0.1", "127.0.0.1", 8006)]:
        seen.clear()
        Infra(Config(env={"WORMS_PROXMOX_URL": url, "WORMS_PROXMOX_USER": "topology@pve",
                          "WORMS_PROXMOX_TOKEN_NAME": "readonly", "WORMS_PROXMOX_TOKEN_VALUE": "x"})).api()
        assert (seen["host"], seen["port"]) == (host, port), url
        assert seen["user"] == "topology@pve" and seen["token_name"] == "readonly"


def test_worms_db_settings_win_over_a_service_env(initialised):
    """DB_* left in the shell (e.g. a sourced service env file) must not send
    the CLI to another database: WORMS_DB_* win."""
    import os as _os

    stale = dict(DB_HOST="192.0.2.1", DB_PORT="1", DB_NAME="elsewhere", DB_SSLMODE="require")
    old = {k: _os.environ.get(k) for k in stale}
    _os.environ.update(stale)
    try:
        out = worms(initialised, "status").stdout
        # Reached the test database (192.0.2.1:1 would not answer).
        assert "competition  ● WORMS" in out and "error" not in out.lower()
    finally:
        for k, v in old.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v



# --- admin one-time password ---------------------------------------------------


def admin_row(cluster):
    return sql(cluster, "SELECT password, must_change_password, is_superuser, is_staff FROM users "
               "WHERE username = 'admin'")  # fmt: skip


def check_password(password, encoded):
    """Django's default PBKDF2 hash, checked without configuring Django here."""
    import base64
    import hashlib
    import hmac

    algorithm, iterations, salt, digest = encoded.split("$")
    assert algorithm == "pbkdf2_sha256"
    computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(base64.b64encode(computed).decode(), digest)


def test_init_leaves_the_admin_without_a_password(initialised):
    encoded, must_change, *_ = admin_row(initialised)[0]
    assert encoded.startswith("!")  # Django's unusable password: login disabled


def test_admin_reset_password_prints_a_one_time_password_once(initialised):
    proc = worms(initialised, "admin", "reset-password")
    password = proc.stdout.strip()
    assert len(password) == 20 and "\n" not in password
    assert "shown once" in proc.stderr and password not in proc.stderr
    encoded, must_change, is_superuser, is_staff = admin_row(initialised)[0]
    assert check_password(password, encoded) and password not in encoded
    assert must_change and is_superuser and is_staff

    # A changed (real) password is only replaced after confirmation.
    sql(initialised, "UPDATE users SET must_change_password = false WHERE username = 'admin'", role=MIGRATOR)
    refused = worms(initialised, "admin", "reset-password", check=False)
    assert refused.returncode != 0 and admin_row(initialised)[0][0] == encoded
    again = worms(initialised, "admin", "reset-password", "--yes").stdout.strip()
    assert again != password and check_password(again, admin_row(initialised)[0][0])


def test_admin_reset_password_creates_a_missing_admin(initialised):
    sql(initialised, "DELETE FROM users WHERE username = 'admin'", role=MIGRATOR)
    assert worms(initialised, "admin", "reset-password").stdout.strip()
    assert admin_row(initialised)[0][1:] == (True, True, True)


def test_admin_reset_password_never_promotes_a_student(initialised):
    sql(initialised, "INSERT INTO users (uuid, password, is_superuser, username, is_staff, is_active, "
        "date_joined, first_name, last_name, email, must_change_password, created_at) VALUES "
        "(gen_random_uuid(), 'x', false, 'stud@x', false, true, now(), '', '', 'stud@x', false, now())",
        role=MIGRATOR)  # fmt: skip
    proc = worms(initialised, "admin", "reset-password", "--username", "stud@x", "--yes", check=False)
    assert proc.returncode != 0 and "not an admin account" in proc.stderr
    assert sql(initialised, "SELECT is_superuser, password FROM users WHERE username = 'stud@x'") == [(False, "x")]


def test_admin_create_staff_prints_a_one_time_password(initialised):
    """Staff (tutor) accounts are made on the console: the dashboard's role
    cannot create one (setup_db_permissions)."""
    cluster = initialised
    proc = worms(cluster, "admin", "create-staff", "tutor@uni.example", "--first-name", "Tu")
    password = proc.stdout.strip()
    assert len(password) >= 10 and "\n" not in password
    row = sql(cluster, "SELECT is_staff, is_superuser, must_change_password, first_name FROM users "
              "WHERE username = 'tutor@uni.example'")
    assert row == [(True, False, True, "Tu")]
    again = worms(cluster, "admin", "create-staff", "tutor@uni.example", check=False)
    assert again.returncode != 0 and "already exists" in again.stderr
