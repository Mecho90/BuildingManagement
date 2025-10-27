from django.apps import AppConfig

class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        """
        Dev-only safety net: if ANY tables for models in `core` are missing,
        run `migrate` once to create them.
        """
        from . import signals  # noqa: F401

        import os
        import sys
        from django.conf import settings

        if not getattr(settings, "AUTO_CREATE_CORE_TABLES", False):
            return  # disabled
        # Avoid double-run from autoreloader parent
        if os.environ.get("RUN_MAIN") != "true":
            return
        # Don't interfere with management commands that already manage DB
        skip_cmds = {
            "migrate", "makemigrations", "collectstatic", "test",
            "createsuperuser", "shell", "dbshell", "flush", "inspectdb"
        }
        argv = " ".join(sys.argv).lower()
        if any(cmd in argv for cmd in skip_cmds):
            return

        try:
            from django.apps import apps
            from django.db import connection
            from django.core.management import call_command

            # Dynamically collect all db_table names for models in 'core'
            core_models = list(apps.get_app_config("core").get_models())
            required = {m._meta.db_table for m in core_models}

            existing = set(connection.introspection.table_names())
            missing = required - existing
            if missing:
                print(f"[core] Missing tables: {sorted(missing)} → running migrations…")
                call_command("migrate", interactive=False, verbosity=1)
                print("[core] Auto-migrate complete. Core tables are ready.")
        except Exception as exc:
            # Why: don't block server start in dev; surface issue in logs.
            print(f"[core] Auto-migrate skipped due to error: {exc}")
