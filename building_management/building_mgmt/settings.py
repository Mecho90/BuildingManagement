# path: building_mgmt/settings.py
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Core ---
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") not in {"0", "false", "False"}
ALLOWED_HOSTS: list[str] = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "markdownify",
]

AUTO_FIX_CORE_SCHEMA = True  # dev helper

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    # dev-only middleware below is added when DEBUG is True
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
if DEBUG:
    # why: avoid loading EnsureCoreSchemaMiddleware in production
    MIDDLEWARE.insert(3, "core.middleware.EnsureCoreSchemaMiddleware")

ROOT_URLCONF = "building_mgmt.urls"
WSGI_APPLICATION = "building_mgmt.wsgi.application"
ASGI_APPLICATION = "building_mgmt.asgi.application"  # if you run ASGI

# --- Templates ---
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",  # required for active nav
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.theme",
            ],
        },
    },
]

def _database_config_from_env() -> dict[str, object]:
    """
    Parse DATABASE_URL and produce a PostgreSQL Django DATABASES entry.

    Supported schemes: postgres:// or postgresql://
    Query parameters (e.g. sslmode) are passed through to OPTIONS.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ImproperlyConfigured(
            "DATABASE_URL environment variable is required and must point to your "
            "PostgreSQL instance."
        )

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured(
            "Unsupported DATABASE_URL scheme. Expected postgres:// or postgresql://."
        )

    db_name = parsed.path.lstrip("/")
    if not db_name:
        raise ImproperlyConfigured("DATABASE_URL must include a database name.")

    query_params = {k: v[-1] for k, v in parse_qs(parsed.query).items() if v}
    options: dict[str, object] = {}

    sslmode_override = query_params.pop("sslmode", None)
    if sslmode_override:
        options["sslmode"] = sslmode_override

    config: dict[str, object] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": db_name,
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "",
        "PORT": parsed.port or "",
    }


    def _env_int(name: str, *, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ImproperlyConfigured(f"{name} must be an integer.") from exc

    conn_max_age = _env_int("DJANGO_DB_CONN_MAX_AGE", default=60)
    if conn_max_age < 0:
        raise ImproperlyConfigured("DJANGO_DB_CONN_MAX_AGE must be >= 0.")
    if conn_max_age:
        config["CONN_MAX_AGE"] = conn_max_age

    health_checks_env = os.environ.get("DJANGO_DB_CONN_HEALTH_CHECKS")
    if health_checks_env is None:
        enable_health_checks = bool(conn_max_age)
    else:
        enable_health_checks = health_checks_env.lower() in {"1", "true", "yes"}
    if enable_health_checks:
        config["CONN_HEALTH_CHECKS"] = True

    sslmode_env = os.environ.get("DJANGO_DB_SSLMODE")
    if "sslmode" not in options:
        if sslmode_env:
            options["sslmode"] = sslmode_env
        elif parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            options["sslmode"] = "require"

    if options:
        config["OPTIONS"] = options

    application_name = os.environ.get("DJANGO_DB_APP_NAME")
    if application_name:
        config.setdefault("OPTIONS", {})["application_name"] = application_name

    return config


DATABASES: dict[str, dict[str, object]] = {
    "default": _database_config_from_env(),
}

# --- Auth ---
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "buildings_list"
LOGOUT_REDIRECT_URL = "login"

# --- i18n/time ---
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Sofia"
USE_I18N = True
USE_TZ = True

# --- Static files (CSS/JS/images) ---
# why: served by staticfiles in dev; collected to STATIC_ROOT for prod
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]  # expects static/css/app.css
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Markdownify (safe subset) ---
MARKDOWNIFY = {
    "default": {
        "WHITELIST_TAGS": [
            "a", "abbr", "acronym", "b", "blockquote", "code", "em", "i",
            "li", "ol", "pre", "strong", "ul", "h1", "h2", "h3", "p", "img"
        ],
        "WHITELIST_ATTRS": ["href", "src", "alt", "title"],
        "WHITELIST_PROTOCOLS": ["http", "https", "mailto"],
    }
}

# --- Misc ---
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Optional: set via env for deployments behind a domain/proxy
# DJANGO_CSRF_TRUSTED_ORIGINS="https://example.com,https://www.example.com"
_csrf_origins = os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "")
if _csrf_origins:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(",") if o.strip()]
