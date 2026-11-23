"""Settings for the `worms` management CLI (src/manage.py).

The CLI bootstraps Django with this module so it can use the same models and
scoring logic as the dashboard -- the database is the single source of truth,
and we never duplicate its schema or the leaderboard formula in the CLI.

Both apps are installed (every model is available) and the database connection
comes from the environment (WORMS_DB_* / DB_*): the CLI sets it from the vault
(the admin role; from a laptop through an SSH tunnel via the host), or from
explicit WORMS_DB_* settings; see docs/03-manage-py.md.
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
# config does not collide with a service env that may be present (a DB_HOST
# left in the shell must not send the CLI to another database).
_user = os.environ.get("WORMS_DB_USER") or os.environ.get("DB_USER")
_password = os.environ.get("WORMS_DB_PASSWORD") or os.environ.get("DB_PASSWORD")
for _key in ("DB_NAME", "DB_HOST", "DB_PORT", "DB_SSLMODE", "DB_SSLROOTCERT"):
    _wk = "WORMS_" + _key
    if os.environ.get(_wk):
        os.environ[_key] = os.environ[_wk]

DATABASES = {"default": postgres_database(_user or "", _password or "")}
# Through an SSH tunnel (from a laptop): connect to WORMS_DB_HOSTADDR, check the
# server certificate against DB_HOST (libpq's hostaddr).
if os.environ.get("WORMS_DB_HOSTADDR"):
    DATABASES["default"]["OPTIONS"]["hostaddr"] = os.environ["WORMS_DB_HOSTADDR"]

# A round runs from the CLI on the host (`worms round start`, src/worms_cli/rounds.py): the
# gameplans are the host's copy (the round agent's workspace, which `worms apply gameplans`
# deploys and its handlers read), the framework this checkout's, the power control this host's
# own Proxmox API (rounds.Pvesh: PROXMOX names the node, set by the CLI from its config).
from config.settings.base import ROUND_TIMING  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
GAMEPLAN_DIR = env("WORMS_GAMEPLAN_DIR", "/opt/worms/gameplans")
GAMEPLAN_LIB = env("WORMS_GAMEPLAN_LIB", os.path.join(_REPO, "src"))
PROXMOX = {"node": env("WORMS_PROXMOX_NODE", "")}
ROUND_CONDUCTOR = {
    "worm_start_delay": ROUND_TIMING["worm_start_delay"],
    "end_drain": ROUND_TIMING["end_drain"],
    "slot_overhead": ROUND_TIMING["slot_overhead"],
    "capture_grace": int(env("ROUND_CAPTURE_GRACE", "120")),
}
