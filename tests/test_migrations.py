"""The Django migrations as a whole (projects/dashboard/core/migrations).

- Models and migrations agree (`makemigrations --check`).
- A database created by an older release upgrades to exactly the schema a
  fresh install gets, and keeps its rows. The older release's migration
  files come from git (BASELINE). This is what protects the live database
  when migrations are squashed or models are removed: Django must treat the
  old migrations as applied (the squashed migration `replaces` them) and run
  only what is new.

Tables that were removed on purpose since BASELINE are listed in REMOVED; an
upgrade drops them, a fresh install never creates them.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from conftest import REPO

pytest.importorskip("django")
pytest.importorskip("psycopg2")

DASHBOARD = REPO / "projects" / "dashboard"
MIGRATIONS = "projects/dashboard/core/migrations"
BASELINE = "6bb490a"  # the last commit with the original 13 migrations
REMOVED = {"worm_deployment_plans", "worm_deployments"}  # unused models, dropped
# The squash and the clean-up after it end here; migrations after this one
# change the schema on purpose (test_baseline_schema_is_kept compares at it).
SQUASH_ERA_END = "0003_alter_hostjob_kind"


@pytest.fixture(scope="module")
def cluster():
    import pg

    bindir = pg.pg_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL server binaries (initdb) not installed")
    cluster = pg.Cluster(bindir)
    cluster.start()
    admin = cluster.connect(dbname="postgres")
    with admin.cursor() as cur:
        cur.execute(f"CREATE ROLE {pg.MIGRATOR} LOGIN PASSWORD %s SUPERUSER", (pg.PASSWORD,))
    admin.close()
    try:
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(scope="module")
def baseline_migrations(tmp_path_factory):
    """The migration files of BASELINE as an importable package."""
    names = subprocess.run(
        ["git", "ls-tree", "--name-only", f"{BASELINE}:{MIGRATIONS}"],
        cwd=REPO, capture_output=True, text=True,
    )  # fmt: skip
    if names.returncode != 0:
        pytest.skip(f"git history without {BASELINE} (shallow clone?)")
    root = tmp_path_factory.mktemp("baseline")
    package = root / "baseline_migrations"
    package.mkdir()
    for name in names.stdout.split():
        if name.endswith(".py"):
            blob = subprocess.run(["git", "show", f"{BASELINE}:{MIGRATIONS}/{name}"], cwd=REPO,
                                  capture_output=True, check=True).stdout  # fmt: skip
            (package / name).write_bytes(blob)
    (root / "baseline_settings.py").write_text(
        "from config.settings.orchestrator import *  # noqa\n"
        "MIGRATION_MODULES = {'core': 'baseline_migrations'}\n"
    )
    return root


def django(cluster, dbname, *command, extra_path=None, settings="config.settings.orchestrator"):
    """manage.py <command> against database `dbname`, as the migrator."""
    import pg

    env = dict(
        os.environ,
        PRODUCTION="1",
        DJANGO_SETTINGS_MODULE=settings,
        DJANGO_SECRET_KEY="test",
        DJANGO_ALLOWED_HOSTS="localhost",
        DB_MIGRATOR_USER=pg.MIGRATOR,
        DB_MIGRATOR_PASSWORD=pg.PASSWORD,
        DB_USER=pg.MIGRATOR,
        DB_PASSWORD=pg.PASSWORD,
        DB_HOST="127.0.0.1",
        DB_PORT=str(cluster.port),
        DB_NAME=dbname,
        DB_SSLMODE="disable",
        PYTHONPATH=os.pathsep.join(p for p in (str(extra_path or ""), str(DASHBOARD)) if p),
        PROXMOX_HOST="x", PROXMOX_USER="x", PROXMOX_TOKEN_NAME="x", PROXMOX_TOKEN_VALUE="x",
    )  # fmt: skip
    proc = subprocess.run([sys.executable, "manage.py", *command], cwd=DASHBOARD, env=env,
                          capture_output=True, text=True)  # fmt: skip
    return proc


def create_database(cluster, name):
    admin = cluster.connect(dbname="postgres")
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {name}")
        cur.execute(f"CREATE DATABASE {name}")
    admin.close()


def schema(cluster, dbname) -> dict:
    """Tables, columns, constraints and indexes of the public schema.

    Constraint and index *names* are left out: Django derives them from the
    operation that created them (a squashed CreateModel names a unique
    constraint differently from a later AlterField), which does not matter."""
    conn = cluster.connect(dbname=dbname)
    out = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type, character_maximum_length, numeric_precision,"
            " is_nullable, column_default, is_identity"
            " FROM information_schema.columns WHERE table_schema = 'public'"
        )
        for table, *column in cur.fetchall():
            out.setdefault(table, {"columns": set(), "constraints": set(), "indexes": set()})
            out[table]["columns"].add(tuple(column))
        cur.execute(
            "SELECT c.conrelid::regclass::text, c.contype, pg_get_constraintdef(c.oid)"
            " FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
            " WHERE n.nspname = 'public' AND c.conrelid <> 0"
        )
        for table, kind, definition in cur.fetchall():
            out[table]["constraints"].add((kind, definition))
        cur.execute(
            "SELECT tablename, indexname, regexp_replace(indexdef, '^CREATE (UNIQUE )?INDEX \\S+ ', 'CREATE \\1INDEX ')"
            " FROM pg_indexes WHERE schemaname = 'public'"
        )
        for table, _name, definition in cur.fetchall():
            out[table]["indexes"].add(definition)
    conn.close()
    return out


def test_models_match_the_migrations(cluster):
    create_database(cluster, "check_models")
    proc = django(cluster, "check_models", "makemigrations", "--check", "--dry-run", "core")
    assert proc.returncode == 0, "models changed without a migration:\n" + proc.stdout + proc.stderr


def test_an_old_database_upgrades_to_the_fresh_schema(cluster, baseline_migrations):
    # 1. A database as the BASELINE release left it, with some rows.
    create_database(cluster, "upgraded")
    proc = django(cluster, "upgraded", "migrate", "--no-input", "--database=migrator",
                  extra_path=baseline_migrations, settings="baseline_settings")  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    conn = cluster.connect(dbname="upgraded")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO competitions (name, created_at, is_dev_competition) VALUES ('WORMS 2025', now(), false) RETURNING id")
        competition = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO teams (name, competition_id, uuid, max_members, created_at)"
            " VALUES ('red', %s, gen_random_uuid(), 2, now())",
            (competition,),
        )
        cur.execute("SELECT count(*) FROM django_migrations WHERE app = 'core'")
        recorded = cur.fetchone()[0]
    conn.close()
    assert recorded == 13  # 0001_initial .. 0013_team_api_indexes

    # 2. The current migrations on top: only what is new may run.
    proc = django(cluster, "upgraded", "migrate", "--no-input", "--database=migrator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc = django(cluster, "upgraded", "migrate", "--check", "--database=migrator")
    assert proc.returncode == 0, "unapplied migrations after the upgrade:\n" + proc.stdout

    # 3. A fresh install of the current release.
    create_database(cluster, "fresh")
    proc = django(cluster, "fresh", "migrate", "--no-input", "--database=migrator")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    upgraded, fresh = schema(cluster, "upgraded"), schema(cluster, "fresh")
    assert set(upgraded) == set(fresh)
    for table in sorted(fresh):
        assert upgraded[table] == fresh[table], table

    conn = cluster.connect(dbname="upgraded")
    with conn.cursor() as cur:
        cur.execute("SELECT t.name, c.name FROM teams t JOIN competitions c ON c.id = t.competition_id")
        assert cur.fetchall() == [("red", "WORMS 2025")]
    conn.close()


def test_baseline_schema_is_kept(cluster, baseline_migrations):
    """Apart from the removed tables, a fresh install at the end of the squash
    era has the BASELINE schema (squashing must not change a column,
    constraint or index). It also proves the later migrations reverse."""
    create_database(cluster, "baseline")
    proc = django(cluster, "baseline", "migrate", "--no-input", "--database=migrator",
                  extra_path=baseline_migrations, settings="baseline_settings")  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    create_database(cluster, "current")
    proc = django(cluster, "current", "migrate", "--no-input", "--database=migrator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Back to the end of the squash era (later migrations add to the schema).
    proc = django(cluster, "current", "migrate", "core", SQUASH_ERA_END, "--no-input", "--database=migrator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    baseline, current = schema(cluster, "baseline"), schema(cluster, "current")
    for table in REMOVED:  # their foreign keys were theirs; nothing else pointed at them
        baseline.pop(table, None)
        current.pop(table, None)
    assert set(current) == set(baseline)
    for table in sorted(baseline):
        assert current[table] == baseline[table], table


def test_removed_tables_are_gone(cluster, baseline_migrations):
    """Fresh installs never create the removed tables; an upgrade drops them."""
    create_database(cluster, "gone_fresh")
    assert django(cluster, "gone_fresh", "migrate", "--no-input", "--database=migrator").returncode == 0
    create_database(cluster, "gone_upgraded")
    proc = django(cluster, "gone_upgraded", "migrate", "--no-input", "--database=migrator",
                  extra_path=baseline_migrations, settings="baseline_settings")  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert REMOVED <= set(schema(cluster, "gone_upgraded"))
    assert django(cluster, "gone_upgraded", "migrate", "--no-input", "--database=migrator").returncode == 0
    for db in ("gone_fresh", "gone_upgraded"):
        assert not REMOVED & set(schema(cluster, db)), db
