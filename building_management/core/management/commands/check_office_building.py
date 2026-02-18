from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import Building


class Command(BaseCommand):
    help = "Verify that the singleton Office building exists and report its primary key."

    def handle(self, *args, **options):
        office_id = Building.system_default_id(force_refresh=True)
        if not office_id:
            raise CommandError("Office building is missing. Run `ensure_office_building` to recreate it.")
        self.stdout.write(self.style.SUCCESS(f"Office building is present (id={office_id})."))
