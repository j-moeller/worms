"""The Django migrations as a whole (projects/dashboard/core/migrations).

Main's initial migration remains unchanged. Additive migrations preserve the
existing accounts, uploaded artifacts and log data; collision tests prove an
upgrade refuses ambiguous identities without deleting data.

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


def test_enrollment_upgrade_preserves_populated_student_data(cluster):
    """Upgrade main's initial schema, including uploads, credentials and results."""
    create_database(cluster, "enrollment_upgrade")
    proc = django(cluster, "enrollment_upgrade", "migrate", "core", "0001_initial", "--no-input", "--database=migrator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    seed = '''
from django.utils import timezone
from core.models.user import User
from core.models.competition import Competition, Round
from core.models.team import Team, TeamMembership
from core.models.worms import MalwareArtifact, Upload
from core.models.logging import LoggingAttempt, Logging
u = User.objects.create(username='Student@Example.test', password='preserve-this-password-hash',
                        first_name='Müller', must_change_password=False)
c = Competition.objects.create(name='Existing competition')
t = Team.objects.create(name='MiXeD Team', competition=c)
TeamMembership.objects.create(user=u, team=t)
a = MalwareArtifact.objects.create(content_hash='a'*64, storage_key='existing-fixture.zip')
Upload.objects.create(user=u, team=t, artifact=a)
r = Round.objects.create(name='Existing round', competition=c, seed=123)
attempt = LoggingAttempt.objects.create(team_uuid=str(t.uuid), log_data={'text':'keep ü 🐛'}, processed=True)
Logging.objects.create(team=t, round=r, logging_attempt=attempt)
'''
    proc = django(cluster, "enrollment_upgrade", "shell", "-c", seed)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    conn = cluster.connect(dbname="enrollment_upgrade")
    query = ("SELECT u.username,u.password,u.first_name,u.must_change_password,t.name,a.content_hash,"
             " l.log_data FROM users u JOIN team_memberships m ON m.user_id=u.id JOIN teams t ON t.id=m.team_id"
             " JOIN uploads up ON up.team_id=t.id AND up.user_id=u.id JOIN malware_artifacts a ON a.id=up.artifact_id"
             " JOIN logging_attempts l ON l.team_uuid=t.uuid::text")
    with conn.cursor() as cur:
        cur.execute(query)
        before = cur.fetchall()
    assert len(before) == 1
    proc = django(cluster, "enrollment_upgrade", "migrate", "--no-input", "--database=migrator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with conn.cursor() as cur:
        cur.execute(query)
        assert cur.fetchall() == before
    conn.close()


@pytest.mark.parametrize("kind", ["account", "team"])
def test_case_collision_blocks_the_upgrade_without_deleting_anything(cluster, kind):
    db = "collision_" + kind
    create_database(cluster, db)
    proc = django(cluster, db, "migrate", "core", "0001_initial", "--no-input", "--database=migrator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    seed = '''
from core.models.user import User
from core.models.competition import Competition
from core.models.team import Team
c = Competition.objects.create(name='Preserve everything')
'''
    if kind == "account":
        seed += "User.objects.create(username='Alice@Example.test')\nUser.objects.create(username='alice@example.test')\n"
        table, column = "users", "username"
    else:
        seed += "Team.objects.create(name='Red Team',competition=c)\nTeam.objects.create(name='red team',competition=c)\n"
        table, column = "teams", "name"
    proc = django(cluster, db, "shell", "-c", seed)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    conn = cluster.connect(dbname=db)
    with conn.cursor() as cur:
        cur.execute(f"SELECT id,{column} FROM {table} ORDER BY id")
        before = cur.fetchall()
    proc = django(cluster, db, "migrate", "--no-input", "--database=migrator")
    assert proc.returncode != 0 and "case_insensitive" in proc.stderr
    with conn.cursor() as cur:
        cur.execute(f"SELECT id,{column} FROM {table} ORDER BY id")
        assert cur.fetchall() == before
        cur.execute("SELECT count(*) FROM django_migrations WHERE app='core' AND name='0002_case_insensitive_accounts_and_teams'")
        assert cur.fetchone()[0] == 0
        # An operator resolves a spelling conflict; the migration then succeeds.
        cur.execute(f"UPDATE {table} SET {column}=%s WHERE id=%s", ["resolved-duplicate", before[-1][0]])
    proc = django(cluster, db, "migrate", "--no-input", "--database=migrator")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 2
    conn.close()


@pytest.mark.parametrize('collision', [False, True])
def test_existing_ssh_fingerprints_upgrade_atomically(cluster, collision):
    import base64
    import hashlib
    db = 'ssh_collision' if collision else 'ssh_upgrade'
    create_database(cluster, db)
    proc = django(cluster, db, 'migrate', 'core', '0001_initial', '--no-input', '--database=migrator')
    assert proc.returncode == 0, proc.stderr
    canonical = base64.b64encode(b'x' * 40).decode()
    alternate = canonical[:-3] + 'B=='
    assert base64.b64decode(canonical) == base64.b64decode(alternate)
    values = [alternate, canonical] if collision else [alternate]
    seed = '''
import hashlib
from core.models.user import User
from core.models.team import Team
from core.models.competition import Competition
from core.models.devbox import DevboxSSHKey
u=User.objects.create(username='ssh-owner')
c=Competition.objects.create(name='keys')
t=Team.objects.create(name='same team',competition=c)
'''
    seed += f"values={values!r}\n"
    seed += '''
for encoded in values:
    key='ssh-ed25519 '+encoded
    DevboxSSHKey.objects.bulk_create([DevboxSSHKey(team=t,created_by=u,public_key=key,
        key_fingerprint=hashlib.sha256(key.encode()).hexdigest())])
'''
    proc = django(cluster, db, 'shell', '-c', seed)
    assert proc.returncode == 0, proc.stderr
    conn = cluster.connect(dbname=db)
    query = 'SELECT id,public_key,key_fingerprint FROM devbox_ssh_keys ORDER BY id'
    with conn.cursor() as cursor:
        cursor.execute(query); before = cursor.fetchall()
    proc = django(cluster, db, 'migrate', '--no-input', '--database=migrator')
    with conn.cursor() as cursor:
        cursor.execute(query); after = cursor.fetchall()
    if collision:
        assert proc.returncode != 0 and 'same identity' in proc.stderr
        assert before == after
    else:
        assert proc.returncode == 0, proc.stderr
        assert [row[:2] for row in before] == [row[:2] for row in after]
        assert after[0][2] == hashlib.sha256(('ssh-ed25519 '+canonical).encode()).hexdigest()
    conn.close()
