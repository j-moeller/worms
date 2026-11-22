import ssl

from django.utils.csp import CSP

from config.settings.base import *  # noqa: F401,F403
from config.settings.base import DEBUG, LOG_LEVEL, LOGGING, MIDDLEWARE, env, postgres_database

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
    # The event tables (core/events.py): their own account, which may only INSERT into
    # them; the main role cannot write them at all. Without it, events are not recorded.
    if env("EVENTS_DB_USER"):
        DATABASES["events"] = postgres_database(env("EVENTS_DB_USER"), env("EVENTS_DB_PASSWORD", required=True))

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


# Content-Security-Policy, on every response (Django's middleware, right after
# SecurityMiddleware: the responses of the middleware further in, CSRF's 403 and the
# redirects, get it too). Only this origin, and nothing inline or evaluated: the
# templates hold no <script> without src, no <style>, no style="" and no on*= handler,
# and the JS builds the page with createElement and textContent
# (tests/test_dashboard_frontend.py). nginx serves static/ on this origin. No frames,
# plugins, workers or fonts at all; forms post here only; no <base>.
SECURE_CSP = {
    "default-src": [CSP.NONE],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF],
    "connect-src": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "base-uri": [CSP.NONE],
}
_security = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1
MIDDLEWARE = [*MIDDLEWARE[:_security], "django.middleware.csp.ContentSecurityPolicyMiddleware", *MIDDLEWARE[_security:]]

MIDDLEWARE += [
    # First: everything after it sees the user being viewed as request.user.
    "dashboard.middleware.ViewAsMiddleware",
    "dashboard.middleware.EnvironmentTransitionMiddleware",
    "dashboard.middleware.UserMetadataMiddleware",
    # After UserMetadataMiddleware (needs request.user): force students off
    # their one-time password before they can use anything else.
    "dashboard.middleware.ForcePasswordChangeMiddleware",
]
