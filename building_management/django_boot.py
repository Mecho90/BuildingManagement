import os
from pathlib import Path

import django
from django.conf import settings


def setup_django() -> None:
    """Configure Django (templates + hashers) with robust template dir resolution."""
    if settings.configured:
        return

    pkg_dir = Path(__file__).resolve().parent            # .../app
    project_root = pkg_dir.parent                        # project root
    env_dir = os.getenv("TEMPLATES_DIR")

    # Candidate template dirs (absolute). Order matters.
    candidates: list[Path | None] = [
        Path(env_dir).resolve() if env_dir else None,    # explicit override
        project_root / "templates",                      # <root>/templates
        pkg_dir / "templates",                           # app/templates
    ]
    template_dirs = [str(p) for p in candidates if p and p.exists()]

    if not template_dirs:
        tried = [str(p) for p in candidates if p is not None]
        raise RuntimeError(
            "Django templates directory not found. Create one of these and put 'home.html' there:\n"
            f" - {project_root / 'templates'}\n"
            f" - {pkg_dir / 'templates'}\n"
            "Or set TEMPLATES_DIR to an absolute path.\n"
            f"Tried: {tried}"
        )

    # Argon2 is optional; fall back cleanly if the lib is missing.
    try:
        import argon2  # noqa: F401
        password_hashers = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
    except Exception:
        password_hashers = []

    password_hashers += [
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
        "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    ]

    settings.configure(
        DEBUG=os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"},
        SECRET_KEY=os.getenv("DJANGO_SECRET_KEY", "change-me"),
        INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes"],
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": template_dirs,   # absolute, verified dirs
                "APP_DIRS": False,       # not using Django app templates
                "OPTIONS": {"debug": False},
            }
        ],
        PASSWORD_HASHERS=password_hashers,
        DEFAULT_CHARSET="utf-8",
    )
    django.setup()