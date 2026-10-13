from config.settings.base import *  # noqa: F401,F403
from config.settings.base import DEBUG, LOG_LEVEL, LOGGING, env, postgres_database

# Application definition

INSTALLED_APPS += ["dashboard"]
ROOT_URLCONF = "dashboard.urls"

# Database: least-privilege application role for normal operation, and the
# migrator role (used only by `migrate --database=migrator`). Credentials come
# from the environment (systemd EnvironmentFile rendered from the vault).
if not DEBUG:
    DATABASES = {
        "default": postgres_database(
            env("DB_USER", required=True),
            env("DB_PASSWORD", required=True),
        ),
        "migrator": postgres_database(
            env("DB_MIGRATOR_USER", required=True),
            env("DB_MIGRATOR_PASSWORD", required=True),
        ),
    }

LOGGING["loggers"]["dashboard"] = {
    "handlers": ["console"],
    "level": LOG_LEVEL,
    "propagate": False,
}


MIDDLEWARE += [
    "dashboard.middleware.EnvironmentTransitionMiddleware",
    "dashboard.middleware.UserMetadataMiddleware",
]
