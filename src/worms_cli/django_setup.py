"""Bootstrap the Django ORM so the CLI reuses the dashboard's models and
scoring. Imported lazily by the DB-backed commands, so `worms --help` and
`worms build` work with neither Django nor a database configured.
"""

from __future__ import annotations

import os
import sys

_READY = False


class NotConfigured(RuntimeError):
    pass


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
            "in ~/.worms/config, then `worms secrets`). See docs/management.md."
        )

    dashboard_src = config.repo_root / "projects" / "dashboard"
    if not (dashboard_src / "manage.py").exists():
        raise NotConfigured(f"dashboard project not found under {dashboard_src}")
    sys.path.insert(0, str(dashboard_src))

    for key, value in db_env.items():
        os.environ.setdefault(key, value)
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.manage"
    os.environ["PRODUCTION"] = "1"
    # base.py requires these at import time under PRODUCTION; the CLI never
    # serves requests, so throwaway values are fine.
    os.environ.setdefault("DJANGO_SECRET_KEY", "worms-cli-not-a-web-process")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "localhost")

    try:
        import django
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise NotConfigured(
            "Django is not installed in this environment (pip install django "
            "psycopg2-binary). See docs/management.md."
        ) from exc

    django.setup()
    _READY = True
