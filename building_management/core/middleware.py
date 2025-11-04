from __future__ import annotations

import threading
from typing import Set
from urllib.parse import quote, urlparse

from django.apps import apps
from django.conf import settings
from django.contrib.auth import logout
from django.core.management import call_command
from django.db import connection
from django.http import HttpResponseRedirect
from django.shortcuts import resolve_url
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

_lock = threading.Lock()
_bootstrapped = False  # process-level guard


def _model_required_columns(model) -> Set[str]:
    # Use Django metadata so we include FK column names like "owner_id"
    return {f.column for f in model._meta.local_fields}


def _missing_schema_for_core() -> tuple[set[str], dict[str, set[str]]]:
    """
    Returns:
      (missing_tables, missing_columns_by_table)
    """
    with connection.cursor() as cursor:
        existing_tables = set(connection.introspection.table_names(cursor))

    core_models = list(apps.get_app_config("core").get_models())
    table_by_model = {m._meta.db_table: m for m in core_models}
    required_tables = set(table_by_model.keys())

    missing_tables = required_tables - existing_tables
    missing_columns_by_table: dict[str, set[str]] = {}

    for table, model in table_by_model.items():
        if table in missing_tables:
            continue  # table missing; columns check is irrelevant
        # Collect columns present in DB
        with connection.cursor() as cursor:
            desc = connection.introspection.get_table_description(cursor, table)
        existing_cols = {c.name for c in desc}
        required_cols = _model_required_columns(model)
        missing = required_cols - existing_cols
        if missing:
            missing_columns_by_table[table] = missing

    return missing_tables, missing_columns_by_table


class EnsureCoreSchemaMiddleware:
    """
    Dev-only guard: on first request, if ANY core tables OR columns are missing,
    run `makemigrations core` and `migrate` once.

    Why: avoid errors like "no such table: core_building" or
         "no such column: core_building.owner_id" on fresh setups.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        global _bootstrapped
        if getattr(settings, "AUTO_FIX_CORE_SCHEMA", False) and not _bootstrapped:
            with _lock:
                if not _bootstrapped:
                    self._ensure_schema()
                    _bootstrapped = True
        return self.get_response(request)

    @staticmethod
    def _ensure_schema() -> None:
        try:
            missing_tables, missing_columns = _missing_schema_for_core()
            if missing_tables or missing_columns:
                print(f"[core] Schema issues detected. "
                      f"Missing tables: {sorted(missing_tables)}; "
                      f"Missing columns: { {k: sorted(v) for k, v in missing_columns.items()} }")
                # Create migrations if needed, then apply them (idempotent)
                call_command("makemigrations", "core", interactive=False, verbosity=0)
                call_command("migrate", interactive=False, verbosity=1)
                print("[core] Auto-migrate complete. Core schema is ready.")
        except Exception as exc:
            # Non-fatal in dev; logs let you diagnose if needed
            print(f"[core] Auto-migrate skipped due to error: {exc}")


class SessionIdleTimeoutMiddleware:
    """
    Require re-authentication after periods of inactivity, regardless of URL.

    Stores the last activity timestamp in the session. If the elapsed time since
    the last request exceeds the configured timeout, the middleware logs the
    user out and redirects them to the login page.
    """

    session_key = "_last_activity_ts"

    def __init__(self, get_response):
        self.get_response = get_response
        timeout = getattr(
            settings,
            "SESSION_IDLE_TIMEOUT_SECONDS",
            getattr(settings, "SESSION_COOKIE_AGE", 0),
        )
        try:
            self.timeout_seconds = int(timeout) if timeout else 0
        except (TypeError, ValueError):
            self.timeout_seconds = 0
        self._exempt_prefixes: tuple[str, ...] | None = None

    def __call__(self, request):
        if self.timeout_seconds > 0 and getattr(request, "user", None) and request.user.is_authenticated:
            path = request.path_info or "/"
            if not self._is_exempt_path(path):
                now_ts = int(timezone.now().timestamp())
                last_ts = request.session.get(self.session_key)
                try:
                    last_ts_val = int(float(last_ts))
                except (TypeError, ValueError):
                    last_ts_val = None
                if last_ts_val is not None and now_ts - last_ts_val > self.timeout_seconds:
                    redirect_url = self._login_redirect_url(request)
                    logout(request)
                    return HttpResponseRedirect(redirect_url)
                request.session[self.session_key] = now_ts
        else:
            if self.session_key in request.session:
                request.session.pop(self.session_key, None)

        response = self.get_response(request)
        return response

    def _login_redirect_url(self, request) -> str:
        login_url = resolve_url(getattr(settings, "LOGIN_URL", "login"))
        login_path = self._normalize_prefix(login_url)
        next_url = request.get_full_path()
        if next_url and not next_url.startswith(login_path):
            separator = "&" if "?" in login_url else "?"
            login_url = f"{login_url}{separator}next={quote(next_url)}"
        return login_url

    def _is_exempt_path(self, path: str) -> bool:
        for prefix in self._get_exempt_prefixes():
            if prefix and path.startswith(prefix):
                return True
        return False

    def _get_exempt_prefixes(self) -> tuple[str, ...]:
        if self._exempt_prefixes is not None:
            return self._exempt_prefixes

        prefixes: set[str] = set()

        prefixes.add(self._normalize_prefix(resolve_url(getattr(settings, "LOGIN_URL", "login"))))

        logout_url = getattr(settings, "LOGOUT_URL", None)
        if logout_url:
            prefixes.add(self._normalize_prefix(resolve_url(logout_url)))

        try:
            prefixes.add(self._normalize_prefix(reverse("logout_to_login")))
        except NoReverseMatch:
            pass

        static_url = getattr(settings, "STATIC_URL", "")
        if static_url:
            prefixes.add(self._normalize_prefix(static_url))

        media_url = getattr(settings, "MEDIA_URL", "")
        if media_url:
            prefixes.add(self._normalize_prefix(media_url))

        extra = getattr(settings, "SESSION_IDLE_TIMEOUT_EXEMPT_PATHS", ())
        for entry in extra:
            prefixes.add(self._normalize_prefix(entry))

        # Remove empty markers to avoid matching every path
        prefixes.discard("")
        prefixes.discard("/")

        self._exempt_prefixes = tuple(sorted(prefixes, key=len, reverse=True))
        return self._exempt_prefixes

    @staticmethod
    def _normalize_prefix(value: str) -> str:
        if not value:
            return ""
        parsed = urlparse(str(value))
        prefix = parsed.path or str(value)
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        return prefix.rstrip("/") or "/"
