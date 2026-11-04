"""The Django migrations as a whole (projects/dashboard/core/migrations).

No deployed database needs upgrading, so there is a single initial migration
and no upgrade path to protect. Once a database that must keep its data exists,
new migrations build on this one, and these tests should grow to cover upgrades.

- Models and migrations agree (`makemigrations --check`).
- A fresh database migrates as the migrator, as `make` does it.
- Data migrations use the migrating connection.
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


def django(cluster, dbname, *command):
    """manage.py <command> against database `dbname`, as the migrator."""
    import pg

    env = dict(
        os.environ,
        PRODUCTION="1",
        DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
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
        PYTHONPATH=str(DASHBOARD),
        PROXMOX_HOST="x", PROXMOX_USER="x", PROXMOX_TOKEN_NAME="x", PROXMOX_TOKEN_VALUE="x",
    )  # fmt: skip
    proc = subprocess.run([sys.executable, "manage.py", *command], cwd=DASHBOARD, env=env,
                          capture_output=True, text=True, timeout=300)  # fmt: skip
    return proc


def create_database(cluster, name):
    admin = cluster.connect(dbname="postgres")
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {name}")
        cur.execute(f"CREATE DATABASE {name}")
    admin.close()


def test_data_migrations_use_the_migrating_connection():
    """`migrate --database=migrator` holds its DDL locks in an open transaction.
    An ORM call without .using(schema_editor.connection.alias) goes through the
    default connection instead, blocks on those locks, and the migration can
    never finish (a former migration did this: a hang on every fresh database).
    Needs no PostgreSQL, unlike the rest of this file."""
    unrouted = [
        f"{path.relative_to(REPO)}:{number}: {line.strip()}"
        for path in sorted(DASHBOARD.glob("*/migrations/*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if ".objects." in line and ".objects.using(" not in line
    ]
    assert not unrouted, "\n".join(unrouted)


def test_models_match_the_migrations(cluster):
    create_database(cluster, "check_models")
    proc = django(cluster, "check_models", "makemigrations", "--check", "--dry-run", "core")
    assert proc.returncode == 0, "models changed without a migration:\n" + proc.stdout + proc.stderr


def test_a_fresh_database_migrates(cluster):
    create_database(cluster, "fresh")
    proc = django(cluster, "fresh", "migrate", "--no-input", "--database=migrator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc = django(cluster, "fresh", "migrate", "--check", "--database=migrator")
    assert proc.returncode == 0, "unapplied migrations after migrating:\n" + proc.stdout
