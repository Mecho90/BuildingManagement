from __future__ import annotations

from typing import Iterable

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connections


class Command(BaseCommand):
    help = (
        "Verify key PostgreSQL schema elements exist and optionally display row counts "
        "to assist with data parity checks."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help="Database connection alias to inspect (defaults to 'default').",
        )
        parser.add_argument(
            "--show-counts",
            action="store_true",
            help="Display record counts for core models to verify data imports.",
        )

    def handle(self, *args, **options):
        database: str = options["database"]
        connection = connections[database]

        if connection.vendor != "postgresql":
            self.stdout.write(
                self.style.WARNING(
                    f"Connection '{database}' is using '{connection.vendor}'. "
                    "PostgreSQL-specific checks were skipped."
                )
            )
            return

        checks = []
        for label, index in [
            ("unique_unit_number_ci_per_building index", "unique_unit_number_ci_per_building"),
            ("core_unit_buildin_dc6512_idx index", "core_unit_buildin_dc6512_idx"),
        ]:
            checks.append(
                (
                    label,
                    """
                    SELECT 1
                    FROM pg_indexes
                    WHERE tablename = %s
                      AND indexname = %s
                    """,
                    ("core_unit", index),
                )
            )

        failures: list[str] = []
        with connection.cursor() as cursor:
            for label, query, params in checks:
                cursor.execute(query, params)
                if cursor.fetchone():
                    self.stdout.write(self.style.SUCCESS(f"✔ {label} present"))
                else:
                    failures.append(label)
                    self.stderr.write(self.style.ERROR(f"✘ {label} missing"))

        if options["show_counts"]:
            self.stdout.write("")
            self.stdout.write("Record counts:")
            for model in self._core_models():
                total = model.objects.using(database).count()
                self.stdout.write(f"  - {model._meta.label}: {total}")

        if failures:
            joined = ", ".join(failures)
            raise CommandError(f"PostgreSQL schema verification failed: {joined}")

    @staticmethod
    def _core_models() -> Iterable[type]:
        """Return models whose counts help verify data parity."""
        for label in ["core.Building", "core.Unit", "core.WorkOrder"]:
            try:
                yield apps.get_model(label)
            except LookupError:
                continue
