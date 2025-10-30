"""
Gunicorn configuration tuned for the Building Management project.

Read more: https://docs.gunicorn.org/en/stable/settings.html
"""

from __future__ import annotations

import multiprocessing
import os


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in {None, ""}:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
default_workers = max((multiprocessing.cpu_count() * 2) + 1, 3)
workers = _env_int("GUNICORN_WORKERS", default_workers)
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
threads = _env_int("GUNICORN_THREADS", 4)
timeout = _env_int("GUNICORN_TIMEOUT", 60)
graceful_timeout = _env_int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _env_int("GUNICORN_KEEPALIVE", 5)
preload_app = _env_bool("GUNICORN_PRELOAD", True)

accesslog = os.environ.get("GUNICORN_ACCESSLOG", "-")
errorlog = os.environ.get("GUNICORN_ERRORLOG", "-")
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")

# Ensure forwarded headers from a reverse proxy (e.g., nginx) are honoured.
forwarded_allow_ips = os.environ.get("GUNICORN_FORWARDED_ALLOW_IPS", "*")
