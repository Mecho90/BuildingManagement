# path: building_mgmt/settings.py
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Core ---
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") not in {"0", "false", "False"}
_hosts_raw = os.environ.get("DJANGO_ALLOWED_HOSTS", "*")
ALLOWED_HOSTS: list[str] = [host.strip() for host in _hosts_raw.split(",") if host.strip()]
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["*"]


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ImproperlyConfigured(f"{name} must be an integer.") from exc
    if minimum is not None and value < minimum:
        raise ImproperlyConfigured(f"{name} must be >= {minimum}.")
    return value


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

AUTO_FIX_CORE_SCHEMA = os.environ.get("DJANGO_AUTO_FIX_CORE_SCHEMA", "").lower() in {"1", "true", "yes"}

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    # dev-only middleware below is added when DEBUG is True
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.SessionIdleTimeoutMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
if DEBUG and AUTO_FIX_CORE_SCHEMA:
    # why: avoid loading EnsureCoreSchemaMiddleware in production
    MIDDLEWARE.insert(4, "core.middleware.EnsureCoreSchemaMiddleware")

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
    Parse DATABASE_URL and produce a Django DATABASES entry.

    Supported schemes: postgres:// or postgresql://
    Query parameters (e.g. sslmode) are passed through to OPTIONS.
    Falls back to SQLite when DATABASE_URL is not provided.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }

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


    conn_max_age = _env_int("DJANGO_DB_CONN_MAX_AGE", default=60, minimum=0)
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
LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", _("English")),
    ("bg", _("Bulgarian")),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Europe/Sofia"
USE_I18N = True
USE_L10N = True
USE_TZ = True

# --- Static files (CSS/JS/images) ---
# why: served by staticfiles in dev; collected to STATIC_ROOT for prod
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]  # expects static/css/app.css
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
WHITENOISE_MANIFEST_STRICT = False

# --- Media storage ---
_media_root = os.environ.get("DJANGO_MEDIA_ROOT")
MEDIA_ROOT = Path(_media_root).expanduser() if _media_root else BASE_DIR / "media"
MEDIA_URL = os.environ.get("DJANGO_MEDIA_URL", "/media/")

FILE_STORAGE_BACKEND = os.environ.get("DJANGO_FILE_STORAGE", "local").strip().lower()
if FILE_STORAGE_BACKEND in {"", "local", "filesystem"}:
    DEFAULT_FILE_STORAGE = "django.core.files.storage.FileSystemStorage"
elif FILE_STORAGE_BACKEND in {"s3", "aws"}:
    DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
    try:
        import storages  # noqa: F401
    except ImportError as exc:  # pragma: no cover - configuration error path
        raise ImproperlyConfigured(
            "DJANGO_FILE_STORAGE='s3' requires django-storages[boto3] to be installed."
        ) from exc
    AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME")
    if not AWS_STORAGE_BUCKET_NAME:
        raise ImproperlyConfigured(
            "AWS_STORAGE_BUCKET_NAME must be set when DJANGO_FILE_STORAGE='s3'."
        )
    AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME")
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
    AWS_S3_CUSTOM_DOMAIN = os.environ.get("AWS_S3_CUSTOM_DOMAIN")
    AWS_QUERYSTRING_AUTH = _env_bool("AWS_QUERYSTRING_AUTH", default=False)
    AWS_DEFAULT_ACL = os.environ.get("AWS_DEFAULT_ACL", "private")
    AWS_S3_OBJECT_PARAMETERS = {
        "CacheControl": os.environ.get("AWS_S3_CACHE_CONTROL", "max-age=86400"),
    }
else:
    raise ImproperlyConfigured(
        "Unsupported DJANGO_FILE_STORAGE backend. Use 'local' or 's3'."
    )

# --- Work order attachment policy ---
WORK_ORDER_ATTACHMENT_MAX_BYTES = _env_int(
    "DJANGO_ATTACHMENT_MAX_BYTES",
    default=10 * 1024 * 1024,
    minimum=1,
)

_allowed_types = os.environ.get(
    "DJANGO_ATTACHMENT_ALLOWED_TYPES",
    "application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document",
)
WORK_ORDER_ATTACHMENT_ALLOWED_TYPES = tuple(
    sorted({token.strip().lower() for token in _allowed_types.split(",") if token.strip()})
)

_allowed_prefixes = os.environ.get(
    "DJANGO_ATTACHMENT_ALLOWED_PREFIXES",
    "image/",
)
WORK_ORDER_ATTACHMENT_ALLOWED_PREFIXES = tuple(
    sorted({token.strip().lower() for token in _allowed_prefixes.split(",") if token.strip()})
)
WORK_ORDER_ATTACHMENT_SCAN_HANDLER = os.environ.get("DJANGO_ATTACHMENT_SCAN_HANDLER", "")

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", default=False)
SESSION_COOKIE_SECURE = _env_bool("DJANGO_SESSION_COOKIE_SECURE", default=SECURE_SSL_REDIRECT)
CSRF_COOKIE_SECURE = _env_bool("DJANGO_CSRF_COOKIE_SECURE", default=SECURE_SSL_REDIRECT)

_hsts_seconds = os.environ.get("DJANGO_SECURE_HSTS_SECONDS")
if _hsts_seconds:
    try:
        SECURE_HSTS_SECONDS = int(_hsts_seconds)
    except ValueError as exc:
        raise ImproperlyConfigured("DJANGO_SECURE_HSTS_SECONDS must be an integer.") from exc
else:
    SECURE_HSTS_SECONDS = 0
if SECURE_HSTS_SECONDS:
    SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
    SECURE_HSTS_PRELOAD = _env_bool("DJANGO_SECURE_HSTS_PRELOAD", default=True)

SECURE_REFERRER_POLICY = os.environ.get("DJANGO_SECURE_REFERRER_POLICY", "strict-origin-when-cross-origin")

X_FRAME_OPTIONS = os.environ.get("DJANGO_X_FRAME_OPTIONS", "SAMEORIGIN")

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
CSRF_TRUSTED_ORIGINS: list[str] = []
_csrf_origins = os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "")
if _csrf_origins:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(",") if o.strip()]

# --- Sessions ---
SESSION_IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes inactivity window
SESSION_COOKIE_AGE = SESSION_IDLE_TIMEOUT_SECONDS
SESSION_SAVE_EVERY_REQUEST = _env_bool("DJANGO_SESSION_SAVE_EVERY_REQUEST", default=True)  # sliding expiry keeps active users signed in
SESSION_IDLE_TIMEOUT_EXEMPT_PATHS: tuple[str, ...] = ()
