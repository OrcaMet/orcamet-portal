"""
OrcaMet Portal — Django Settings
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv, find_dotenv
import dj_database_url

# Load .env file for local development
ENV_FILE = find_dotenv()
if ENV_FILE:
    load_dotenv(ENV_FILE)


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean-ish environment variable."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> list:
    """Read a comma-separated environment variable into a list."""
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]

# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent


# ============================================================
# SECURITY
# ============================================================

# Treat anything running on Render as production. DJANGO_PRODUCTION lets
# other hosts (staging, containers) opt in explicitly, so a deployment that
# is not on Render no longer silently runs with DEBUG enabled.
IS_PRODUCTION = bool(os.environ.get("RENDER")) or _env_flag("DJANGO_PRODUCTION")

# Debug defaults to on for local development, off in production. It can be
# overridden explicitly, but never defaults to on for a production host.
DEBUG = _env_flag("DJANGO_DEBUG", default=not IS_PRODUCTION)

# SECRET_KEY must be supplied in production. Falling back to a hardcoded
# value there would let anyone forge sessions and signed data, so fail loudly
# instead. Render supplies this via `generateValue: true` in render.yaml.
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-insecure-dev-only-change-me-in-production"
    else:
        raise ImproperlyConfigured(
            "SECRET_KEY environment variable is required when DEBUG is False."
        )

ALLOWED_HOSTS = []
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)

# Additional hostnames (e.g. a custom domain such as portal.orcamet.co.uk).
ALLOWED_HOSTS += _env_list("DJANGO_ALLOWED_HOSTS")

# Allow localhost in development
if DEBUG:
    ALLOWED_HOSTS += ["localhost", "127.0.0.1"]

# CSRF trusted origins (required for Auth0 callback)
CSRF_TRUSTED_ORIGINS = []
if RENDER_EXTERNAL_HOSTNAME:
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_EXTERNAL_HOSTNAME}")
for _host in _env_list("DJANGO_ALLOWED_HOSTS"):
    CSRF_TRUSTED_ORIGINS.append(f"https://{_host}")


# ============================================================
# TRANSPORT SECURITY (production only)
# ============================================================
# Render terminates TLS at its proxy and forwards X-Forwarded-Proto, so
# Django needs the proxy header to know a request arrived over HTTPS.
# Without it, SECURE_SSL_REDIRECT would cause a redirect loop.

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    # Start with a short HSTS window. Raise to 31536000 (and consider
    # preload) once HTTPS is confirmed working on every hostname served —
    # HSTS is cached by browsers and hard to walk back.
    SECURE_HSTS_SECONDS = 3600
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False


# ============================================================
# APPLICATIONS
# ============================================================

INSTALLED_APPS = [
    # OrcaMet apps
    "accounts.apps.AccountsConfig",
    "sites.apps.SitesConfig",
    "forecasts.apps.ForecastsConfig",
    "dashboard.apps.DashboardConfig",
    # Django built-ins
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]


# ============================================================
# MIDDLEWARE
# ============================================================

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ============================================================
# URLS & TEMPLATES
# ============================================================

ROOT_URLCONF = "orcamet_portal.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR / "orcamet_portal" / "templates",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                # Exposes TIME_ZONE so client-side date formatting can use the
                # same zone as the server instead of assuming UTC, or the
                # viewer's own zone when they are travelling.
                "django.template.context_processors.tz",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "orcamet_portal.wsgi.application"
ASGI_APPLICATION = "orcamet_portal.asgi.application"


# ============================================================
# DATABASE
# ============================================================

DATABASES = {
    "default": dj_database_url.config(
        default="postgresql://postgres:postgres@localhost:5432/orcamet_portal",
        conn_max_age=600,
    )
}


# ============================================================
# AUTH
# ============================================================

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ============================================================
# AUTH0 CONFIGURATION
# ============================================================

AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
AUTH0_CLIENT_ID = os.environ.get("AUTH0_CLIENT_ID", "")
AUTH0_CLIENT_SECRET = os.environ.get("AUTH0_CLIENT_SECRET", "")


# ============================================================
# SANDBOX / TRIAL ACCOUNTS
# ============================================================

# How many active sites one invite-provisioned sandbox client may create.
# Every new site fires a live Open-Meteo forecast run, so this is a cost and
# rate-limit guard, not a product tier. Real clients are not affected.
SANDBOX_MAX_SITES = int(os.environ.get("SANDBOX_MAX_SITES", "3"))


# ============================================================
# INTERNATIONALISATION
# ============================================================

LANGUAGE_CODE = "en-gb"

# Local time for everyone who reads this portal. Timestamps are still stored
# in UTC (USE_TZ) — this governs how they are interpreted and displayed.
#
# It matters beyond cosmetics: the work window below is applied to the hour
# of day, so under UTC the 07:00-18:00 window silently ran an hour late for
# the ~7 months of British Summer Time, scoring 08:00-19:00 local instead.
TIME_ZONE = "Europe/London"

USE_I18N = True
USE_TZ = True


# ============================================================
# STATIC FILES
# ============================================================

STATIC_URL = "/static/"

STATICFILES_DIRS = [
    BASE_DIR / "orcamet_portal" / "static",
]

STATIC_ROOT = BASE_DIR / "staticfiles"

# STATICFILES_STORAGE was removed in Django 5.1 (deprecated in 4.2). This
# project runs 5.2, so the old setting was being read by nobody: static files
# were served by the plain StaticFilesStorage, without content-hashed names.
# That meant no cache busting — browsers kept serving a stale portal.css
# across deploys — and the brotli/gzip variants whitenoise[brotli] builds
# were never used. Django does not warn about unknown settings, so this was
# silent.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        # The manifest backend requires collectstatic to have run, which the
        # build does but a dev checkout has not.
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}


# ============================================================
# DEFAULT PRIMARY KEY
# ============================================================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ============================================================
# LOGGING
# ============================================================

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "accounts": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}


# ============================================================
# FORECAST ENGINE SETTINGS
# ============================================================

OPENMETEO_API_KEY = os.environ.get("OPENMETEO_API_KEY", "")

# CARTO now watermarks unauthenticated basemap tiles with a diagonal
# "API KEY REQUIRED" across every tile. The key is a client-side
# credential by nature — it ships in the map page — so restrict it to the
# portal's domains in the CARTO dashboard rather than treating it as a
# secret. Without it the map still works, just watermarked.
CARTO_API_KEY = os.environ.get("CARTO_API_KEY", "")

# How many forecast runs one web worker will do in the background at once.
# Each fetches four models from Open-Meteo and does numpy work inside a
# gunicorn process, and trial accounts can trigger runs from the browser, so
# this is the ceiling that stops a handful of testers saturating a 512 MB
# instance. Sites over the ceiling are left to the scheduled cron.
FORECAST_MAX_CONCURRENT_THREADS = int(
    os.environ.get("FORECAST_MAX_CONCURRENT_THREADS", "2")
)

# Work window for rope access operations, in local time (see TIME_ZONE).
# Inclusive of the end hour: 7..18 covers 07:00 up to 18:59.
FORECAST_WORK_START_HOUR = 7
FORECAST_WORK_END_HOUR = 18

# Forecast generation schedule (UTC) — informational only; the actual
# schedule is set on the Render cron jobs (orcamet-portal_site_forecasts,
# orcamet-portal_risk_grid), currently every 6 hours.
FORECAST_RUN_TIMES = ["00:00", "06:00", "12:00", "18:00"]

# Number of forecast days
FORECAST_NUM_DAYS = 3
