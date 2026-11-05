"""
Django settings for the EventGate project.

Configuration is environment-driven via django-environ. The app boots with
sane defaults in development (`DEBUG=True` via .env) and refuses to start
in production unless the required secrets are supplied via environment.
"""

from __future__ import annotations

import os
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

# ---------------------------------------------------------------------------
# Paths & environment
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, True),                 # dev default; MUST be False in prod
    DJANGO_SECURE_SSL_REDIRECT=(bool, False),  # True in prod (behind a proxy)
    DJANGO_EMAIL_USE_TLS=(bool, True),
    DJANGO_EMAIL_PORT=(int, 587),
    DJANGO_EMAIL_USE_CONSOLE=(bool, True),     # True in dev; False in prod
    DJANGO_SESSION_COOKIE_SECURE=(bool, False),
    DJANGO_CSRF_COOKIE_SECURE=(bool, False),
    DJANGO_SECURE_HSTS_SECONDS=(int, 0),
    TICKET_ENCRYPTION_CURRENT_VERSION=(int, 1),
)

# .env is for local development only; production must set real env vars.
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(env_file)


# ---------------------------------------------------------------------------
# Core security
# ---------------------------------------------------------------------------
DEBUG = env("DJANGO_DEBUG", default=True)

# In production the secret key must be supplied. In development a generated
# insecure key is fine, but it is regenerated on every restart, which would
# invalidate QR signatures and password-reset tokens across restarts. So
# developers should set DJANGO_SECRET_KEY in .env too.
SECRET_KEY = env("DJANGO_SECRET_KEY", default="")
if not SECRET_KEY:
    if DEBUG:
        from django.utils.crypto import get_random_string
        SECRET_KEY = "dev-" + get_random_string(50)
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set in production."
        )

SECRET_KEY_FALLBACKS = env.list("DJANGO_SECRET_KEY_FALLBACKS", default=[])

ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"],
)

if DEBUG:
    ALLOWED_HOSTS = list(ALLOWED_HOSTS) + ["testserver"]
elif "testserver" in ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS must not contain 'testserver' in production."
    )

CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# If behind a proxy that terminates TLS, trust the X-Forwarded-Proto header.
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if env.bool("DJANGO_BEHIND_PROXY", default=False)
    else None
)


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    # Local apps before contrib.auth so our UserAdmin registration wins.
    "accounts.apps.AccountsConfig",

    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Local apps
    "events.apps.EventsConfig",
    "orders.apps.OrdersConfig",
    "tickets.apps.TicketsConfig",
    "advertisements.apps.AdvertisementsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",

    "accounts.middleware.ScannerOnlyAccessMiddleware",
]

ROOT_URLCONF = "config.urls"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "advertisements.context_processors.active_advertisements",
                "accounts.context_processors.user_roles",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

import dj_database_url

# Use the DATABASE_URL environment variable set in Vercel
DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get("PRISMA_DATABASE_URL", ""),
        conn_max_age=600,  # Keep connections alive for 10 minutes
        conn_health_checks=True, # Django 4.1+; validates broken connections
    )
}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Both are namespaced. There is no top-level "login" / "signup" / "logout"
# route — see config/urls.py.
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "events:home"
LOGOUT_REDIRECT_URL = "events:home"


# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Freetown"
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------
# 30 MB is the largest single request we accept (25 MB video + fields +
# multipart overhead). Anything larger is rejected before it hits a view.
DATA_UPLOAD_MAX_MEMORY_SIZE = 40 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------
SITE_BASE_URL = env("SITE_BASE_URL", default="http://127.0.0.1:8000")

DEFAULT_FROM_EMAIL = env(
    "DJANGO_DEFAULT_FROM_EMAIL", default="EventGate <no-reply@eventgate.sl>",
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
if DEBUG or env.bool("DJANGO_EMAIL_USE_CONSOLE", default=False):
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("DJANGO_EMAIL_HOST")
    EMAIL_PORT = env("DJANGO_EMAIL_PORT", default=587)
    EMAIL_USE_TLS = env("DJANGO_EMAIL_USE_TLS", default=True)
    EMAIL_HOST_USER = env("DJANGO_EMAIL_HOST_USER", default="")
    EMAIL_HOST_PASSWORD = env("DJANGO_EMAIL_HOST_PASSWORD", default="")
    EMAIL_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Sessions & cookies
# ---------------------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14           # two weeks
SESSION_COOKIE_SECURE = env("DJANGO_SESSION_COOKIE_SECURE", default=not DEBUG)

CSRF_COOKIE_HTTPONLY = False                      # required so JS can read the token
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = env("DJANGO_CSRF_COOKIE_SECURE", default=not DEBUG)


# ---------------------------------------------------------------------------
# HTTPS / security headers
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = env("DJANGO_SECURE_SSL_REDIRECT", default=not DEBUG)
SECURE_HSTS_SECONDS = env("DJANGO_SECURE_HSTS_SECONDS", default=0 if DEBUG else 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"


# ---------------------------------------------------------------------------
# Ticket encryption
# ---------------------------------------------------------------------------
# Both TICKET_ENCRYPTION_KEYS and TICKET_ENCRYPTION_CURRENT_VERSION are read
# here. Keys must be comma-separated "version:key" pairs in production:
#   TICKET_ENCRYPTION_KEYS="1:abc==,2:def=="
def _parse_encryption_keys(
    raw: str,
    default: dict[int, str],
    *,
    single_version: int = 1,
) -> dict[int, str]:
    """
    Parse TICKET_ENCRYPTION_KEYS from env.

    Accepts both:
      - "1:keyA,2:keyB"  -> {1: "keyA", 2: "keyB"}   (multi-key, versioned)
      - "keyA"           -> {single_version: "keyA"} (single bare key)

    A bare key is convenient for local development and single-key deploys.
    For rotation, use the explicit "version:key" form so old ciphertexts
    remain readable under their recorded ``token_key_version``.
    """
    if not raw:
        return default

    out: dict[int, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue

        if ":" in entry:
            try:
                version_s, key = entry.split(":", 1)
                out[int(version_s)] = key
            except (ValueError, TypeError) as exc:
                raise ImproperlyConfigured(
                    f"TICKET_ENCRYPTION_KEYS entry {entry!r} is not 'version:key'."
                ) from exc
        else:
            # Bare key — assume the current version.
            out[single_version] = entry

    if not out:
        raise ImproperlyConfigured("TICKET_ENCRYPTION_KEYS is empty.")
    return out




_dev_keys = {
    # Generated with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # DO NOT use this key in production. Production must set the env var.
    1: "Uu4cVdBFsFwO8-KYqM3a8bN2h4Yk1xv0Z0nZq1p5y2A=",
}


TICKET_ENCRYPTION_KEYS = _parse_encryption_keys(
    env("TICKET_ENCRYPTION_KEYS", default=""),
    default=_dev_keys,
    single_version=env.int("TICKET_ENCRYPTION_CURRENT_VERSION", default=1),
)

TICKET_ENCRYPTION_CURRENT_VERSION = env.int(
    "TICKET_ENCRYPTION_CURRENT_VERSION", default=1,
)

if TICKET_ENCRYPTION_CURRENT_VERSION not in TICKET_ENCRYPTION_KEYS:
    raise ImproperlyConfigured(
        f"TICKET_ENCRYPTION_CURRENT_VERSION="
        f"{TICKET_ENCRYPTION_CURRENT_VERSION} is not present in "
        f"TICKET_ENCRYPTION_KEYS (have {sorted(TICKET_ENCRYPTION_KEYS)})."
    )



# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env("DJANGO_LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "tickets": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "orders": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Production safety checks
# ---------------------------------------------------------------------------
if not DEBUG:
    # Every one of these would silently weaken security if left at a dev default.
    if DEBUG:                                                   # defensive
        raise ImproperlyConfigured("DEBUG must be False in production.")
    if not SECURE_SSL_REDIRECT:
        raise ImproperlyConfigured(
            "SECURE_SSL_REDIRECT must be True in production. Set "
            "DJANGO_SECURE_SSL_REDIRECT=True."
        )
    if not SESSION_COOKIE_SECURE:
        raise ImproperlyConfigured(
            "SESSION_COOKIE_SECURE must be True in production. Set "
            "DJANGO_SESSION_COOKIE_SECURE=True."
        )
    if not CSRF_COOKIE_SECURE:
        raise ImproperlyConfigured(
            "CSRF_COOKIE_SECURE must be True in production. Set "
            "DJANGO_CSRF_COOKIE_SECURE=True."
        )
    if SECURE_HSTS_SECONDS < 3600:
        raise ImproperlyConfigured(
            "SECURE_HSTS_SECONDS should be at least 1 hour in production. "
            "Set DJANGO_SECURE_HSTS_SECONDS=31536000."
        )
    if _dev_keys is TICKET_ENCRYPTION_KEYS:
        raise ImproperlyConfigured(
            "Refusing to boot in production with the development ticket-"
            "encryption key. Set TICKET_ENCRYPTION_KEYS='1:<fernet-key>'."
        )


