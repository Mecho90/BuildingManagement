from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.auth_views import RoleAwareLoginView
from core.models import Building, Notification, Unit, WorkOrder, UserSecurityProfile
from core.services import NotificationService


class WorkOrderArchiveViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="archive-owner",
            email="archive@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.user,
            name="Archive Plaza",
            address="123 Archive St",
        )
        self.unit = Unit.objects.create(
            building=self.building,
            number="A-1",
        )

    def test_archive_done_work_order(self):
        work_order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Ready for archive",
            status=WorkOrder.Status.DONE,
            priority=WorkOrder.Priority.LOW,
            deadline=timezone.localdate(),
        )

        self.client.login(username="archive-owner", password="pass1234")
        response = self.client.post(
            reverse("work_order_archive", args=[work_order.pk]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        work_order.refresh_from_db()
        self.assertIsNotNone(work_order.archived_at)

    def test_archive_rejects_non_done_work_order(self):
        work_order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Still open",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate(),
        )

        self.client.login(username="archive-owner", password="pass1234")
        response = self.client.post(reverse("work_order_archive", args=[work_order.pk]))
        self.assertEqual(response.status_code, 404)
        work_order.refresh_from_db()
        self.assertIsNone(work_order.archived_at)


class LoginLockoutTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.username = "lockout-user"
        self.password = "lock-me"
        self.user = User.objects.create_user(
            username=self.username,
            email="lockout@example.com",
            password=self.password,
            is_active=True,
        )

    def test_user_locked_after_threshold(self):
        login_url = reverse("login")
        for _ in range(RoleAwareLoginView.lock_threshold):
            response = self.client.post(
                login_url, {"username": self.username, "password": "wrong"}
            )
            self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        profile = UserSecurityProfile.objects.get(user=self.user)
        self.assertFalse(self.user.is_active)
        self.assertEqual(profile.lock_reason, UserSecurityProfile.LockReason.FAILED_ATTEMPTS)
        self.assertIsNotNone(profile.locked_at)

        response = self.client.post(
            login_url, {"username": self.username, "password": "wrong-again"}
        )
        self.assertContains(
            response,
            "Your account has been locked after too many failed attempts.",
            status_code=200,
        )


class NotificationSnoozeViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="notify-owner",
            email="notify@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.user,
            name="Notify Plaza",
            address="456 Notify St",
        )
        self.today = timezone.localdate()
        self.work_order = WorkOrder.objects.create(
            building=self.building,
            title="Inspect pumps",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.HIGH,
            deadline=self.today + timedelta(days=1),
        )
        service = NotificationService(self.user)
        service.sync_work_order_deadlines(today=self.today)
        self.note_key = f"wo-deadline-{self.work_order.pk}"

    def test_requires_auth(self):
        response = self.client.post(reverse("notification_snooze", args=[self.note_key]))
        self.assertEqual(response.status_code, 302)

    def test_snoozes_until_tomorrow(self):
        self.client.login(username="notify-owner", password="pass1234")
        url = reverse("notification_snooze", args=[self.note_key])
        response = self.client.post(url, follow=False)
        self.assertEqual(response.status_code, 302)
        note = Notification.objects.get(user=self.user, key=self.note_key)
        self.assertEqual(note.snoozed_until, self.today + timedelta(days=1))

    def test_not_found_returns_404(self):
        self.client.login(username="notify-owner", password="pass1234")
        response = self.client.post(reverse("notification_snooze", args=["missing-key"]))
        self.assertEqual(response.status_code, 404)

    def test_mass_assign_dismiss_acknowledges(self):
        mass_order = WorkOrder.objects.create(
            building=self.building,
            title="Mass alert",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=self.today + timedelta(days=3),
            mass_assigned=True,
        )
        service = NotificationService(self.user)
        service.sync_recent_mass_assign(today=self.today)
        key = f"wo-mass-{mass_order.pk}"

        self.client.login(username="notify-owner", password="pass1234")
        response = self.client.post(reverse("notification_snooze", args=[key]), follow=False)
        self.assertEqual(response.status_code, 302)

        note = Notification.objects.get(user=self.user, key=key)
        self.assertIsNotNone(note.acknowledged_at)
        self.assertFalse(note.is_active(on=self.today))
