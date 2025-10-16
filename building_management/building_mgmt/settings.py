# path: building_mgmt/settings.py
from __future__ import annotations

import os
from pathlib import Path

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
            ],
        },
    },
]

# --- Database (SQLite for dev) ---
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
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
