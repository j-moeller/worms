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
# (VM.Audit + VM.PowerMgmt on the `sandbox` pool).
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

# Separate, READ-ONLY Proxmox API token used only to collect the topology (VMs,
# bridges, firewall) for the dashboard visualisation (`manage.py
# collect_topology`). It needs the PVEAuditor role on `/` and nothing else, so
# a leak exposes configuration but cannot change it. Kept apart from the token
# above (which needs VM.PowerMgmt) on purpose.
#
# PROXMOX_CA_BUNDLE optionally points at the CA that signed the Proxmox API
# certificate (e.g. a copy of /etc/pve/pve-root-ca.pem); it takes precedence
# over PROXMOX_VERIFY_SSL.
PROXMOX_READONLY = {
    "host": PROXMOX["host"],
    "port": PROXMOX["port"],
    "node": PROXMOX["node"],
    "user": env("PROXMOX_READONLY_USER", ""),
    "token_name": env("PROXMOX_READONLY_TOKEN_NAME", ""),
    "token_value": env("PROXMOX_READONLY_TOKEN_VALUE", ""),
    "verify_ssl": env("PROXMOX_CA_BUNDLE") or PROXMOX["verify_ssl"],
    "timeout": int(env("PROXMOX_TIMEOUT", "10")),
}

# Bearer tokens for the orchestrator REST API, one per caller (scope). Empty =
# that caller is disabled. See orchestrator/api.py.
ORCHESTRATOR_TOKENS = {
    # Dashboard: VM power control for sandbox VMs.
    "dashboard": env("ORCHESTRATOR_DASHBOARD_TOKEN", ""),
}

# Round conductor (orchestrator/rounds, manage.py conduct_rounds). Times in
# seconds. See docs/rounds.md.
ROUND_CONDUCTOR = {
    "worm_start_delay": int(env("ROUND_WORM_START_DELAY", "300")),
    "round_duration": int(env("ROUND_DURATION", "3600")),
    "end_drain": int(env("ROUND_END_DRAIN", "120")),
    "job_timeout": int(env("ROUND_JOB_TIMEOUT", "1800")),
    "capture_grace": int(env("ROUND_CAPTURE_GRACE", "120")),
}

# Devbox (docs/devbox.md): how a team reaches the shared SSH gateway. The host
# publishes the gateway on its WAN at DEVBOX_SSH_GATEWAY_PORT (DNAT -> gateway:22);
# set DEVBOX_SSH_GATEWAY_HOST to the address/name teams connect to. Applied to
# the team's Devbox record when a devbox job completes (orchestrator/rounds/devbox.py).
DEVBOX = {
    "ssh_gateway_host": env("DEVBOX_SSH_GATEWAY_HOST", ""),
    "ssh_gateway_port": int(env("DEVBOX_SSH_GATEWAY_PORT", "2222")),
    "ssh_gateway_user": env("DEVBOX_SSH_GATEWAY_USER", "team"),
}

LOGGING["loggers"]["orchestrator"] = {
    "handlers": ["console"],
    "level": LOG_LEVEL,
    "propagate": False,
}
