from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.utils import timezone

from core.services import NotificationService
from core.services.todos import TodoReminderService


class Command(BaseCommand):
    help = "Create daily to-do digest notifications and optional emails."

    def add_arguments(self, parser):
        parser.add_argument(
            "--today",
            help="Override today's date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--email",
            action="store_true",
            help="Also send the digest via email when the user has an address.",
        )

    def handle(self, *args, **options):
        today = options.get("today")
        if today:
            today = timezone.datetime.fromisoformat(today).date()
        else:
            today = timezone.localdate()
        send_email = options["email"]

        User = get_user_model()
        qs = User.objects.filter(is_active=True)
        processed = 0
        for user in qs.iterator():
            reminder = TodoReminderService(user)
            payload = reminder.build_digest(today=today)
            if not payload:
                continue
            NotificationService(user).upsert(payload)
            if send_email and user.email:
                send_mail(
                    subject=payload.title,
                    message=payload.body,
                    from_email=None,
                    recipient_list=[user.email],
                    fail_silently=True,
                )
            processed += 1
        self.stdout.write(self.style.SUCCESS(f"Generated digests for {processed} user(s)."))
