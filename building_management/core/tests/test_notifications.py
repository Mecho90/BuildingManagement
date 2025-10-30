from __future__ import annotations

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import Building, Notification, WorkOrder
from core.services import NotificationPayload, NotificationService


class NotificationModelTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="notify-user",
            email="notify@example.com",
            password="pass1234",
        )

    def test_is_active_respects_acknowledge_and_snooze(self):
        note = Notification.objects.create(
            user=self.user,
            key="demo",
            category="demo",
            title="Demo",
            body="Demo body",
        )
        today = timezone.localdate()
        self.assertTrue(note.is_active(on=today))

        note.snooze_until(today + timedelta(days=1))
        self.assertFalse(note.is_active(on=today))
        self.assertTrue(note.is_active(on=today + timedelta(days=1)))

        note.acknowledge()
        self.assertFalse(note.is_active(on=today + timedelta(days=2)))

    def test_snooze_until_rejects_past_date(self):
        note = Notification.objects.create(
            user=self.user,
            key="demo",
            category="demo",
            title="Demo",
            body="Demo body",
        )
        with self.assertRaises(ValidationError):
            note.snooze_until(timezone.localdate() - timedelta(days=1))


class NotificationServiceTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="service-user",
            email="service@example.com",
            password="pass1234",
        )
        self.service = NotificationService(self.user)
        self.building = Building.objects.create(
            owner=self.user,
            name="Omega Complex",
            address="123 Main",
        )
        self.today = timezone.localdate()

    def _create_work_order(self, **kwargs):
        defaults = {
            "building": self.building,
            "title": "Inspect pumps",
            "status": WorkOrder.Status.OPEN,
            "priority": WorkOrder.Priority.HIGH,
            "deadline": self.today + timedelta(days=1),
        }
        defaults.update(kwargs)
        return WorkOrder.objects.create(**defaults)

    def test_upsert_creates_and_updates_single_record(self):
        payload = NotificationPayload(
            key="wo-1",
            category="deadline",
            title="Work order due",
            body="Fix the HVAC",
            level=Notification.Level.WARNING,
        )
        obj = self.service.upsert(payload)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(obj.level, Notification.Level.WARNING)

        payload_updated = NotificationPayload(
            key="wo-1",
            category="deadline",
            title="Work order updated",
            body="New body",
            level=Notification.Level.DANGER,
        )
        updated = self.service.upsert(payload_updated)
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(updated.title, "Work order updated")
        self.assertEqual(updated.level, Notification.Level.DANGER)

    def test_acknowledge_and_mark_seen(self):
        payload = NotificationPayload(
            key="wo-2",
            category="deadline",
            title="Check boiler",
            body="Boiler needs inspection",
        )
        self.service.upsert(payload)

        self.assertEqual(self.service.mark_seen(["wo-2"]), 1)
        note = Notification.objects.get(user=self.user, key="wo-2")
        self.assertIsNotNone(note.first_seen_at)

        self.assertEqual(self.service.acknowledge(["wo-2"]), 1)
        note.refresh_from_db()
        self.assertIsNotNone(note.acknowledged_at)

    def test_snooze_until(self):
        payload = NotificationPayload(
            key="wo-3",
            category="deadline",
            title="Check roof",
            body="Roof inspection due",
        )
        self.service.upsert(payload)
        target_date = date.today() + timedelta(days=2)
        note = self.service.snooze_until("wo-3", target_date=target_date)
        self.assertEqual(note.snoozed_until, target_date)

    def test_sync_creates_deadline_notification(self):
        self._create_work_order()
        notes = list(self.service.sync_work_order_deadlines(today=self.today))
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note.category, "deadline")
        self.assertEqual(note.snoozed_until, self.today)

    def test_sync_respects_future_snooze(self):
        wo = self._create_work_order()
        self.service.sync_work_order_deadlines(today=self.today)
        note = Notification.objects.get(user=self.user, key=f"wo-deadline-{wo.pk}")
        future = self.today + timedelta(days=1)
        note.snoozed_until = future
        note.save(update_fields=["snoozed_until"])

        self.service.sync_work_order_deadlines(today=self.today)
        note.refresh_from_db()
        self.assertEqual(note.snoozed_until, future)

    def test_sync_cleans_up_completed_orders(self):
        wo = self._create_work_order()
        self.service.sync_work_order_deadlines(today=self.today)
        self.assertTrue(Notification.objects.filter(user=self.user, key=f"wo-deadline-{wo.pk}").exists())

        wo.status = WorkOrder.Status.DONE
        wo.save(update_fields=["status"])

        self.service.sync_work_order_deadlines(today=self.today)
        self.assertFalse(Notification.objects.filter(user=self.user, key=f"wo-deadline-{wo.pk}").exists())

    def test_management_command_refreshes_snoozed_notifications(self):
        wo = self._create_work_order(deadline=self.today + timedelta(days=2))
        self.service.sync_work_order_deadlines(today=self.today)
        key = f"wo-deadline-{wo.pk}"
        note = Notification.objects.get(user=self.user, key=key)
        note.snoozed_until = self.today + timedelta(days=1)
        note.save(update_fields=["snoozed_until"])

        next_day = self.today + timedelta(days=1)
        call_command("sync_notifications", today=next_day.isoformat())
        note.refresh_from_db()
        self.assertEqual(note.snoozed_until, next_day)
        self.assertTrue(note.is_active(on=next_day))
