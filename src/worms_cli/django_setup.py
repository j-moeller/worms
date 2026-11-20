"""Bootstrap the Django ORM so the CLI reuses the dashboard's models and
scoring. Imported lazily by the DB-backed commands, so `worms --help` and
`worms apply` work with neither Django nor a database configured.
"""

from __future__ import annotations

import os
import sys

_READY = False
_UNREACHABLE = None  # the error of this run's one failed connection (connected)
CONNECT_TIMEOUT = "10"  # seconds: libpq's PGCONNECT_TIMEOUT, when nothing else sets one


class NotConfigured(RuntimeError):
    pass


def connected(config) -> None:
    """setup(), then one connection: for what reads the database only if it answers (the team
    map, `worms status`). A database that is down costs a run one timeout, not one per question:
    its error is kept and raised again at once (NotConfigured, or the connection's)."""
    global _UNREACHABLE
    if _UNREACHABLE is not None:
        raise _UNREACHABLE
    try:
        setup(config)
        from django.db import connection

        connection.ensure_connection()
    except Exception as exc:
        _UNREACHABLE = exc
        raise


def setup(config) -> None:
    """Configure Django against the database in `config`. Raises NotConfigured
    with a clear message when the database or Django is missing."""
    global _READY
    if _READY:
        return

    from worms_cli.session import SessionError

    try:
        db_env = config.db_env()
    except SessionError as exc:
        raise NotConfigured(f"database: {exc}") from exc
    if not db_env:
        raise NotConfigured(
            "no database configured: no vault in this checkout (set WORMS_SSH=root@<node> "
            "in ~/.worms/config, then `worms show secrets`). See docs/03-manage-py.md."
        )

    dashboard_src = config.repo_root / "projects" / "dashboard"
    if not (dashboard_src / "manage.py").exists():
        raise NotConfigured(f"dashboard project not found under {dashboard_src}")
    sys.path.insert(0, str(dashboard_src))

    for key, value in db_env.items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("PGCONNECT_TIMEOUT", CONNECT_TIMEOUT)  # a database that is down: no minutes-long wait
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.manage"
    os.environ["PRODUCTION"] = "1"  # the deployed settings, DEBUG off: on both systems
    _name_the_system(config)
    # base.py requires these at import time under PRODUCTION; the CLI never
    # serves requests, so throwaway values are fine.
    os.environ.setdefault("DJANGO_SECRET_KEY", "worms-cli-not-a-web-process")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "localhost")

    try:
        import django
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise NotConfigured(
            "Django is not installed in this environment (pip install django "
            "psycopg2-binary). See docs/03-manage-py.md."
        ) from exc

    django.setup()
    _READY = True


def _name_the_system(config) -> None:
    """For the settings' banner: which system the database belongs to (the switch,
    worms_environments). Display only: the database commands do not depend on it."""
    from worms_cli.session import SessionError, for_config

    try:
        system = for_config(config).environment()
    except SessionError:
        return
    os.environ["WORMS_ENVIRONMENT"] = f"{system['name']}: {system['node']}"
