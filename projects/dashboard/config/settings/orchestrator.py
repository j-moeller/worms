from config.settings.base import *  # noqa: F401,F403
from config.settings.base import DEBUG, LOG_LEVEL, LOGGING, env, postgres_database

# Application definition

INSTALLED_APPS += ["orchestrator"]
ROOT_URLCONF = "orchestrator.urls"

# Database: least-privilege orchestrator role for normal operation, migrator
# role only for migrations. Credentials come from the environment.
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

# Proxmox API access (proxmoxer). Host, port and token are injected from the
# environment; the API token value is a secret delivered via the vault. The
# token should be scoped to the least privilege the orchestrator needs
# (VM.Audit + VM.PowerMgmt on the relevant pool).
PROXMOX = {
    "host": env("PROXMOX_HOST", required=not DEBUG),
    "port": int(env("PROXMOX_PORT", "8006")),
    "node": env("PROXMOX_NODE", "worms-pve"),
    "user": env("PROXMOX_USER", required=not DEBUG),
    "token_name": env("PROXMOX_TOKEN_NAME", required=not DEBUG),
    "token_value": env("PROXMOX_TOKEN_VALUE", required=not DEBUG),
    # Verify the Proxmox API certificate against the system trust store (the
    # internal CA is installed on the VM). Set PROXMOX_VERIFY_SSL=0 only for
    # local experimentation.
    "verify_ssl": env("PROXMOX_VERIFY_SSL", "1") == "1",
}

LOGGING["loggers"]["orchestrator"] = {
    "handlers": ["console"],
    "level": LOG_LEVEL,
    "propagate": False,
}
