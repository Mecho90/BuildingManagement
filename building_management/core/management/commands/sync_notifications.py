from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import Notification
from core.services import NotificationService


class Command(BaseCommand):
    help = "Synchronise persistent notifications for all users (deadline alerts, etc.)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--today",
            metavar="YYYY-MM-DD",
            help="Override today's date (useful for tests).",
        )

    def handle(self, *args, **options):
        override_today = options.get("today")
        if override_today:
            today = timezone.datetime.fromisoformat(override_today).date()
        else:
            today = timezone.localdate()

        self.stdout.write(self.style.NOTICE(f"Syncing notifications for {today.isoformat()}"))

        expired = (
            Notification.objects.filter(snoozed_until__isnull=False, snoozed_until__lte=today)
            .update(snoozed_until=None)
        )
        if expired:
            self.stdout.write(f"Cleared snoozes for {expired} notification(s).")

        stale = (
            Notification.objects.filter(expires_at__isnull=False, expires_at__lt=timezone.now())
            .delete()[0]
        )
        if stale:
            self.stdout.write(f"Deleted {stale} expired notification(s).")

        User = get_user_model()
        total_users = User.objects.filter(is_active=True).count()
        self.stdout.write(f"Processing notifications for {total_users} active user(s)...")

        processed = 0
        for user in User.objects.filter(is_active=True).iterator():
            with transaction.atomic():
                service = NotificationService(user)
                service.prune_acknowledged()
                service.sync_work_order_deadlines(today=today)
            processed += 1

        self.stdout.write(self.style.SUCCESS(f"Updated notifications for {processed} user(s)."))
