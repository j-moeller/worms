"""Shared fixtures for the offline test suite (`make test`).

Nothing here talks to Proxmox or a VM. Tests that need a tool that is not
installed (ansible-playbook, tofu, PostgreSQL, ip/tc, root) are skipped with
the reason, so the suite runs everywhere and does as much as it can.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def require(*tools: str) -> None:
    """Skip the calling test unless every tool is on PATH."""
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"not installed: {', '.join(missing)}")


def require_root() -> None:
    if os.geteuid() != 0:
        pytest.skip("needs root (network namespaces / PostgreSQL as another user)")


@pytest.fixture(scope="session")
def platform_outputs() -> dict:
    """`tofu output -json` of the platform stack, generated from
    opentofu/shared/shared-variables.tf (see test_tofu.py)."""
    return json.loads((FIXTURES / "platform-outputs.json").read_text())


@pytest.fixture(scope="session")
def infrastructure(platform_outputs) -> dict:
    return platform_outputs["infrastructure"]["value"]


@pytest.fixture(scope="session")
def generated_inventory(tmp_path_factory) -> Path:
    """ansible/inventory.generated.yml as scripts/generate_inventory.py writes
    it from the fixture (addressing only, no hosts)."""
    out = tmp_path_factory.mktemp("inventory") / "inventory.generated.yml"
    subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "generate_inventory.py"),
            "--repo-root",
            str(tmp_path_factory.mktemp("no-stacks")),
            "--platform-json",
            str(FIXTURES / "platform-outputs.json"),
            "--output",
            str(out),
            "--quiet",
        ],
        check=True,
        capture_output=True,
    )
    return out


@pytest.fixture(scope="session")
def render(generated_inventory, tmp_path_factory):
    """render(role_dir, template, **extra_vars) -> rendered text.

    Uses Ansible's own templating (tests/ansible/render.yml) with the role
    defaults and the generated inventory, like a real run."""
    require("ansible-playbook")
    out_dir = tmp_path_factory.mktemp("rendered")

    def _render(role_dir: str, template: str, **extra) -> str:
        dest = out_dir / f"{Path(role_dir).name}-{template}"
        cmd = [
            "ansible-playbook",
            "-i",
            str(generated_inventory),
            str(REPO / "tests" / "ansible" / "render.yml"),
            "-e",
            json.dumps(
                {"role_dir": str(REPO / role_dir), "template": template, "dest": str(dest), **extra}
            ),
        ]
        env = dict(os.environ, ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_NOCOLOR="1")
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return dest.read_text()

    return _render


@pytest.fixture(scope="session")
def database(render):
    """A PostgreSQL cluster set up like the database VM (tests/pg.py)."""
    import pg

    pytest.importorskip("psycopg2")
    pytest.importorskip("django")
    bindir = pg.pg_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL server binaries (initdb) not installed")
    privileges = render("ansible/roles/database/setup_db_permissions", "privileges.sql.j2")
    cluster = pg.Cluster(bindir)
    cluster.start()
    try:
        pg.setup(cluster, privileges)
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture(scope="session")
def tofu_copy(tmp_path_factory):
    """The OpenTofu tree without state or .terraform (never touches yours)."""
    require("tofu")
    dest = tmp_path_factory.mktemp("opentofu") / "opentofu"
    shutil.copytree(
        REPO / "opentofu",
        dest,
        symlinks=True,  # shared-variables.tf links stay relative, inside the copy
        ignore=shutil.ignore_patterns(".terraform*", "terraform.tfstate*", "images", "iso"),
    )
    return dest


# --- in-process Django --------------------------------------------------------


@pytest.fixture(scope="session")
def django_env(request):
    """In-process Django (orchestrator settings) against a throwaway
    PostgreSQL with the real migrations; Django connects as the superuser so
    tests can build fixtures freely. Session scope: Django can only be set up
    once per process, so every test module shares it (test_rounds.py,
    test_team_api.py)."""
    pytest.importorskip("django")
    pytest.importorskip("psycopg2")
    sys.path.insert(0, str(REPO / "projects" / "dashboard"))
    from pg import pg_bindir

    if pg_bindir() is None:
        pytest.skip("PostgreSQL server binaries not installed")
    import pg

    bindir = pg_bindir()
    cluster = pg.Cluster(bindir)
    cluster.start()
    try:
        import django

        os.environ.update(
            PRODUCTION="1",
            DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
            DJANGO_SECRET_KEY="test",
            DJANGO_ALLOWED_HOSTS="localhost",
            DB_MIGRATOR_USER=pg.MIGRATOR,
            DB_MIGRATOR_PASSWORD=pg.PASSWORD,
            DB_USER="postgres",
            DB_PASSWORD="trust",
            DB_HOST="127.0.0.1",
            DB_PORT=str(cluster.port),
            DB_NAME=pg.DB_NAME,
            DB_SSLMODE="disable",
            PROXMOX_HOST="x", PROXMOX_USER="x", PROXMOX_TOKEN_NAME="x", PROXMOX_TOKEN_VALUE="x",
        )
        # Create the database and schema (migrations) as the migrator, like the
        # real deployment; then let Django connect as postgres (superuser) so
        # the test can set up fixtures without juggling grants.
        admin = cluster.connect(dbname="postgres")
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE {pg.DB_NAME}")
            cur.execute(f"CREATE ROLE {pg.MIGRATOR} LOGIN PASSWORD %s SUPERUSER", (pg.PASSWORD,))
        admin.close()
        django.setup()
        from django.core.management import call_command

        call_command("migrate", "--no-input", database="migrator", verbosity=0)
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture
def db_clean(django_env):
    """A clean set of tables for each test."""
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            "TRUNCATE run_events, game_plan_steps, host_jobs, flag_captures, logging,"
            " flag_capture_attempts, logging_attempts, virtual_machines, round_runs,"
            " access_tokens, rounds, team_memberships, teams, uploads, malware_artifacts,"
            " application_setting, competitions, users, network_flows, topology_snapshots,"
            " django_session RESTART IDENTITY CASCADE"
        )
    return django_env
