import os
import socket
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name, default=None, *, required=False):
    """Read a configuration value from the environment.

    Secrets (SECRET_KEY, DB passwords, Proxmox token, ...) are injected into the
    service environment by systemd from a root-owned 0600 EnvironmentFile that
    Ansible renders from the encrypted vault. Nothing sensitive is hardcoded.
    """
    value = os.environ.get(name, default)
    if required and (value is None or value == ""):
        raise ImproperlyConfigured(f"Missing required environment variable: {name}")
    return value


def postgres_database(user, password):
    """Build a TLS-verified PostgreSQL connection dict for the given role."""
    options = {"sslmode": env("DB_SSLMODE", "verify-full")}
    sslrootcert = env("DB_SSLROOTCERT")
    if sslrootcert:
        options["sslrootcert"] = sslrootcert

    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", required=True),
        "USER": user,
        "PASSWORD": password,
        "HOST": env("DB_HOST", required=True),
        "PORT": env("DB_PORT", "5432"),
        "OPTIONS": options,
    }


MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MiB
CHUNK_SIZE = 1024 * 1024  # 1 MiB

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get("PRODUCTION", None) != "1"

if DEBUG:
    print("[DEBUG] We are running in DEBUG mode")
else:
    print("[PRODUCTION] We are running in PRODUCTION mode")

# SECRET_KEY and ALLOWED_HOSTS: a throwaway value for local development, and
# strictly environment-provided values in production (no hardcoded secrets).
if DEBUG:
    SECRET_KEY = "django-insecure-@@iayl=pwga9ptu0rjh476a#jhfs#274vkky#avp80b5auytf$"
    ALLOWED_HOSTS = ["worms-pve.mlsec.tu-berlin.de", "localhost", "127.0.0.1"]
else:
    SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
    ALLOWED_HOSTS = [
        host.strip()
        for host in env("DJANGO_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    ]
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production")

    # nginx terminates TLS and forwards X-Forwarded-Proto.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    CSRF_TRUSTED_ORIGINS = [
        f"https://{host}" for host in ALLOWED_HOSTS if not host[0].isdigit()
    ]

INSTALLED_APPS = [
    "core",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

if DEBUG:
    assert socket.gethostname() == "worms-pve"
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

LOGIN_URL = "/login/"

AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

STATIC_URL = "static/"


# Email
# https://docs.djangoproject.com/en/6.1/topics/email/#topic-email-configuration

# MAILERS = {
#     'default': {
#         'BACKEND': 'django.core.mail.backends.console.EmailBackend',
#     },
# }

LOG_LEVEL = os.getenv("DJANGO_LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {
            "()": "django.utils.log.RequireDebugFalse",
        },
    },
    "formatters": {
        "verbose": {
            "format": (
                "{asctime} {levelname} " "[{name}] {process:d} {thread:d} " "{message}"
            ),
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "mail_admins": {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
            "filters": ["require_debug_false"],
        },
    },
    "loggers": {
        # Your application logs.
        "django": {
            "handlers": ["console", "mail_admins"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "mail_admins"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # Database queries. Keep this WARNING in production.
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        # Security-related events.
        "django.security": {
            "handlers": ["console", "mail_admins"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
