from __future__ import annotations
import threading
from typing import Iterable, Set

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import connection


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