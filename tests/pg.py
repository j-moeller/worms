"""A throwaway PostgreSQL cluster for the tests, set up like the database VM:
the Django migrations (run as the migrator), the application roles from
ansible/group_vars/all.yml and the table privileges rendered from
ansible/roles/database/setup_db_permissions (privileges.sql.j2).

TLS and pg_hba are not reproduced (local socket/TCP with passwords); those are
checked on the live system (ansible/Simulate.yml).
"""

from __future__ import annotations

import glob
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
DB_NAME = "mydb"
MIGRATOR = "django_migrator"
PASSWORD = "test-password"


def pg_bindir() -> str | None:
    if shutil.which("pg_config"):
        out = subprocess.run(["pg_config", "--bindir"], capture_output=True, text=True).stdout.strip()
        if out and Path(out, "initdb").exists():
            return out
    found = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))
    if found:
        return str(Path(found[-1]).parent)
    initdb = shutil.which("initdb")
    return str(Path(initdb).parent) if initdb else None


def application_roles() -> list[dict]:
    """postgresql_users from group_vars/all.yml (names and connection limits)."""
    data = yaml.safe_load((REPO / "ansible" / "group_vars" / "all.yml").read_text())
    return [{"name": u["name"], "conn_limit": u.get("conn_limit")} for u in data["postgresql_users"]]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cluster:
    def __init__(self, bindir: str):
        self.bindir = bindir
        self.dir = Path(tempfile.mkdtemp(prefix="worms-pg-"))
        self.port = free_port()
        self.user = None
        if os.geteuid() == 0:  # PostgreSQL refuses to run as root
            self.user = "nobody"
            os.chmod(self.dir, 0o755)
            shutil.chown(self.dir, user=self.user)

    def _run(self, *args):
        cmd = [f"{self.bindir}/{args[0]}", *args[1:]]
        if self.user:
            cmd = ["runuser", "-u", self.user, "--", *cmd]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    def start(self):
        data = self.dir / "data"
        self._run("initdb", "-D", str(data), "-U", "postgres", "-A", "trust", "--no-sync")
        self._run(
            "pg_ctl", "-D", str(data), "-l", str(self.dir / "log"), "-w",
            "-o", f"-p {self.port} -k {self.dir} -c listen_addresses=127.0.0.1 -c fsync=off",
            "start",
        )  # fmt: skip

    def stop(self):
        try:
            self._run("pg_ctl", "-D", str(self.dir / "data"), "-m", "immediate", "stop")
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)

    def connect(self, user="postgres", password=None, dbname=DB_NAME):
        import psycopg2

        conn = psycopg2.connect(
            host="127.0.0.1", port=self.port, user=user, password=password, dbname=dbname
        )
        conn.autocommit = True
        return conn

    def env(self, user, password=PASSWORD) -> dict:
        """Connection settings as the services' env files have them."""
        return {
            "DB_HOST": "127.0.0.1",
            "DB_PORT": str(self.port),
            "DB_NAME": DB_NAME,
            "DB_USER": user,
            "DB_PASSWORD": password,
            "DB_SSLMODE": "disable",
        }


def setup(cluster: Cluster, privileges_sql: str) -> None:
    """Database, roles (setup_db_base), migrations, privileges (setup_db_permissions)."""
    admin = cluster.connect(dbname="postgres")
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DB_NAME}")
        cur.execute(f"CREATE ROLE {MIGRATOR} LOGIN PASSWORD %s", (PASSWORD,))
        for role in application_roles():
            limit = f" CONNECTION LIMIT {role['conn_limit']}" if role["conn_limit"] else ""
            cur.execute(f'CREATE ROLE "{role["name"]}" LOGIN PASSWORD %s{limit}', (PASSWORD,))
    admin.close()

    # pg_hba accepts passwords from now on (trust was for the setup only).
    hba = cluster.dir / "data" / "pg_hba.conf"
    hba.write_text(
        "local all postgres trust\n"
        "host all postgres 127.0.0.1/32 trust\n"
        "host all all 127.0.0.1/32 scram-sha-256\n"
    )
    db = cluster.connect()
    with db.cursor() as cur:
        cur.execute("SELECT pg_reload_conf()")
        cur.execute(f"REVOKE CONNECT, TEMPORARY ON DATABASE {DB_NAME} FROM PUBLIC")
        cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cur.execute(f"GRANT CONNECT ON DATABASE {DB_NAME} TO {MIGRATOR}")
        cur.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {MIGRATOR}")
        for role in application_roles():
            cur.execute(f'GRANT CONNECT ON DATABASE {DB_NAME} TO "{role["name"]}"')
            cur.execute(f'GRANT USAGE ON SCHEMA public TO "{role["name"]}"')
    time.sleep(0.2)

    # The Django migrations, as the orchestrator runs them.
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
        [sys.executable, "manage.py", "migrate", "--noinput", "--skip-checks", "--database=migrator"],
        cwd=REPO / "projects" / "dashboard",
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    with db.cursor() as cur:
        db.autocommit = False
        cur.execute(privileges_sql)
        db.commit()
    db.close()
