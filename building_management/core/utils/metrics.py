from __future__ import annotations

import hashlib
import logging
import time
from contextlib import contextmanager
from typing import Iterator

from django.conf import settings

SENSITIVE_KEY_TOKENS = ("user", "email", "owner", "technician")


def _hash_value(value: object) -> str:
    secret = getattr(settings, "SECRET_KEY", "")
    raw = f"{secret}:{value}".encode("utf-8", errors="ignore")
    digest = hashlib.sha256(raw).hexdigest()
    return f"hash:{digest[:16]}"


def _scrub_extra(extra: dict | None) -> dict:
    if not extra:
        return {}
    payload: dict = {}
    for key, value in extra.items():
        lower_key = key.lower()
        if any(token in lower_key for token in SENSITIVE_KEY_TOKENS):
            payload[key] = _hash_value(value)
        else:
            payload[key] = value
    return payload


@contextmanager
def log_duration(logger: logging.Logger, label: str, *, extra: dict | None = None) -> Iterator[None]:
    """
    Lightweight timing helper for ad-hoc instrumentation.

    Usage:

        with log_duration(logger, "work_orders.list_queryset", extra={"user_id": request.user.pk}):
            ...
    """
    start = time.perf_counter()
    scrubbed_extra = _scrub_extra(extra)
    status = "ok"
    error: Exception | None = None
    try:
        yield
    except Exception as exc:  # pragma: no cover - exercised in tests via logger stub
        status = "error"
        error = exc
        raise
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        min_duration = getattr(settings, "TIMING_LOG_MIN_DURATION_MS", 0) or 0
        payload = {
            "metric": label,
            "duration_ms": round(elapsed_ms, 2),
            "status": status,
        }
        if scrubbed_extra:
            payload.update(scrubbed_extra)
        if error:
            payload["error_type"] = error.__class__.__name__

        if status == "ok" and min_duration and elapsed_ms < min_duration:
            return

        log_fn = logger.error if status == "error" else logger.info
        log_fn("timing.%s %s", label, status, extra=payload)
