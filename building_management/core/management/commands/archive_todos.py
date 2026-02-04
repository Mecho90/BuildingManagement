from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.services.todos import TodoArchiveService


class Command(BaseCommand):
    help = "Archive completed to-do items older than N weeks (default 4)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--weeks",
            type=int,
            default=4,
            help="Number of weeks to keep active before archiving (default: 4).",
        )
        parser.add_argument(
            "--today",
            help="Override today's date (YYYY-MM-DD) for testing.",
        )

    def handle(self, *args, **options):
        weeks = options["weeks"]
        today_override = options.get("today")
        if today_override:
            today = timezone.datetime.fromisoformat(today_override).date()
        else:
            today = timezone.localdate()
        service = TodoArchiveService(weeks_to_keep=weeks, today=today)
        archived = service.archive_completed()
        pruned = service.prune_archived_snapshots()
        self.stdout.write(
            self.style.SUCCESS(
                f"Archived {archived} todo item(s) and deleted {pruned} inactive snapshot(s)."
            )
        )
