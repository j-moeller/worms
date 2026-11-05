import ssl

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
# The deployment resolves the certificate hostname through /etc/hosts because
# there is no DNS on the infrastructure side. The URL must cover that name.
ORCHESTRATOR_API = {
    "url": env("ORCHESTRATOR_URL", ""),
    "tls_name": env("ORCHESTRATOR_TLS_NAME", "orchestrator.worms.internal"),
    "token": env("ORCHESTRATOR_API_TOKEN", ""),
    # Keep explicit CA overrides supported by earlier deployments; otherwise
    # the standard-library client verifies against the system trust store.
    "ca_bundle": (env("ORCHESTRATOR_CA_BUNDLE") or env("REQUESTS_CA_BUNDLE")
                  or env("CURL_CA_BUNDLE") or ssl.get_default_verify_paths().cafile),
}

LOGGING["loggers"]["dashboard"] = {
    "handlers": ["console"],
    "level": LOG_LEVEL,
    "propagate": False,
}


MIDDLEWARE += [
    "dashboard.middleware.EnvironmentTransitionMiddleware",
    "dashboard.middleware.UserMetadataMiddleware",
    # After UserMetadataMiddleware (needs request.user): force students off
    # their one-time password before they can use anything else.
    "dashboard.middleware.ForcePasswordChangeMiddleware",
]
