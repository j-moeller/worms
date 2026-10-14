from config.settings.base import *  # noqa: F401,F403
from config.settings.base import DEBUG, LOG_LEVEL, LOGGING, env, postgres_database

# Application definition

INSTALLED_APPS += ["dashboard"]
ROOT_URLCONF = "dashboard.urls"

# Database: least-privilege application role only. The dashboard is public
# facing, so it does NOT hold the migrator credentials (the migrator owns the
# tables); migrations run on the orchestrator. Credentials come from the
# environment (systemd EnvironmentFile rendered from the vault).
if not DEBUG:
    DATABASES = {
        "default": postgres_database(
            env("DB_USER", required=True),
            env("DB_PASSWORD", required=True),
        ),
    }

# Orchestrator REST API (dashboard -> orchestrator:443, bearer token).
# Connect by IP (no DNS on the infrastructure side) and verify the certificate
# for ORCHESTRATOR_TLS_NAME.
ORCHESTRATOR_API = {
    "url": env("ORCHESTRATOR_URL", ""),
    "tls_name": env("ORCHESTRATOR_TLS_NAME", "orchestrator.worms.internal"),
    "token": env("ORCHESTRATOR_API_TOKEN", ""),
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
