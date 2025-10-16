from __future__ import annotations
from pathlib import Path
from django.conf import settings
from django.core.management import BaseCommand, call_command


class Command(BaseCommand):
    help = "Create a fresh SQLite database if missing and apply all migrations."

    def handle(self, *args, **options):
        db_name = settings.DATABASES["default"]["NAME"]
        db_path = Path(db_name) if isinstance(db_name, str) else db_name
        if not db_path.exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self.stdout.write(self.style.WARNING(f"Creating new database at {db_path}"))
        # Always run migrations; safe if already applied.
        call_command("migrate", interactive=False, verbosity=1)
        self.stdout.write(self.style.SUCCESS(f"Database ready: {db_path}"))