from config.settings.base import *

if DEBUG:
    SECRET_KEY = "django-insecure-@@iayl=pwga9ptu0rjh476a#jhfs#274vkky#avp80b5auytf$"
    ALLOWED_HOSTS = ["worms-pve.mlsec.tu-berlin.de"]
else:
    print("WARNING: HARDCODED SECRET")
    # TODO
    # PASSWORD
    SECRET_KEY = "INSECURE-e3dcb93337060fe3646cfefd60ac25131698c1fc9d89ce54b4a3fea81c21523c5d9d3fe63e91baa336ab35d4ed9434d91854f52c98cf6bc1f8c1be0e83d19889"

    # TODO
    print("WARNING: worms-pve.mlsec.tu-berlin.de as allowed host")
    ALLOWED_HOSTS = [
        "worms-pve.mlsec.tu-berlin.de",  # TODO
        "dashboard.worms.internal",
        "10.3.0.18",
    ]

# Application definition

INSTALLED_APPS += ["dashboard"]
ROOT_URLCONF = "dashboard.urls"

# TODO # PASSWORD
if not DEBUG:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "mydb",  # TODO
            "USER": "django_dashboard",  # TODO # PASSWORD
            "PASSWORD": "test",  # TODO # PASSWORD
            "HOST": "10.3.0.16",  # TODO
            "PORT": "5432",
            "OPTIONS": {
                "sslmode": "verify-full",
                "sslrootcert": "/etc/ssl/certs/ca-certificates.crt",
            },
        },
        "migrator": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "mydb",  # TODO
            "USER": "django_migrator",  # TODO # PASSWORD
            "PASSWORD": "test",  # TODO # PASSWORD
            "HOST": "10.3.0.16",  # TODO
            "PORT": "5432",
            "OPTIONS": {
                "sslmode": "verify-full",
                "sslrootcert": "/etc/ssl/certs/ca-certificates.crt",
            },
        },
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
