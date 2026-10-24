"""Settings for the `worms` management CLI (src/manage.py).

The CLI bootstraps Django with this module so it can use the same models and
scoring logic as the dashboard -- the database is the single source of truth,
and we never duplicate its schema or the leaderboard formula in the CLI.

Both apps are installed (every model is available) and the database connection
comes from the environment (WORMS_DB_* / DB_*), typically the admin or migrator
role so the CLI can create and delete teams. Run it where the database is
reachable (on the Proxmox host, or through an SSH tunnel to it); see
docs/management.md.
"""

import os

from config.settings.base import *  # noqa: F401,F403
from config.settings.base import INSTALLED_APPS, env, postgres_database

# Not a web process: no DEBUG, no server-only middleware needed. We only use
# the ORM, so a throwaway key is fine (never serves requests).
DEBUG = False
SECRET_KEY = env("DJANGO_SECRET_KEY", "worms-cli-not-a-web-process")
ALLOWED_HOSTS: list[str] = []

# Every model, whichever app defined it (dashboard + orchestrator URLconfs are
# not loaded; importing the apps is enough for the ORM).
INSTALLED_APPS = INSTALLED_APPS + ["dashboard", "orchestrator"]

# The CLI connects as a privileged role (admin/migrator) so it can read
# everything and manage teams. WORMS_DB_* take precedence over DB_* so the CLI
# config does not collide with a service env that may be present.
_user = os.environ.get("WORMS_DB_USER") or os.environ.get("DB_USER")
_password = os.environ.get("WORMS_DB_PASSWORD") or os.environ.get("DB_PASSWORD")
for _key in ("DB_NAME", "DB_HOST", "DB_PORT", "DB_SSLMODE", "DB_SSLROOTCERT"):
    _wk = "WORMS_" + _key
    if os.environ.get(_wk) and not os.environ.get(_key):
        os.environ[_key] = os.environ[_wk]

DATABASES = {"default": postgres_database(_user or "", _password or "")}
