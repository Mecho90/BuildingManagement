from __future__ import annotations

import os
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Building, Unit, WorkOrder


class Command(BaseCommand):
    help = "Seed deterministic data required by Playwright smoke tests."

    def handle(self, *args, **options):
        User = get_user_model()
        username = os.environ.get("PLAYWRIGHT_SEED_USERNAME", "playwright")
        password = os.environ.get("PLAYWRIGHT_SEED_PASSWORD", "playwright123")

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "email": f"{username}@example.com",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created or not user.is_active:
            user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        building, _ = Building.objects.get_or_create(
            name="Playwright Towers",
            defaults={
                "owner": user,
                "address": "1 Testing Plaza",
                "description": "Seeded building for Playwright smoke tests.",
            },
        )
        if building.owner_id != user.id:
            building.owner = user
            building.save(update_fields=["owner"])

        unit, _ = Unit.objects.get_or_create(
            building=building,
            number="P-101",
            defaults={
                "floor": 10,
                "owner_name": "Playwright QA",
                "description": "Seeded unit for smoke tests.",
                "is_occupied": True,
            },
        )

        WorkOrder.objects.get_or_create(
            building=building,
            unit=unit,
            title="Inspect seeded smoke wiring",
            defaults={
                "priority": WorkOrder.Priority.MEDIUM,
                "status": WorkOrder.Status.DONE,
                "deadline": timezone.localdate() + timedelta(days=7),
                "description": "Ensures at least one work order exists for tests.",
                "archived_at": timezone.now(),
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Playwright seed ready (user={username!r}, password={password!r})."
            )
        )
