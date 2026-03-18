from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Building,
    BuildingMembership,
    MembershipRole,
    WorkOrder,
)


class WorkOrderArchiveViewTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.owner = self.User.objects.create_user(username="owner", password="pass1234")
        self.building = Building.objects.create(
            owner=self.owner,
            name="Archive Tower",
            address="1 Main",
        )

    def _create_order(self, status=WorkOrder.Status.DONE):
        return WorkOrder.objects.create(
            building=self.building,
            title="Fix HVAC",
            deadline=timezone.localdate(),
            status=status,
        )

    def test_user_with_manage_capability_can_archive(self):
        user = self.User.objects.create_user(username="manager", password="pass1234")
        BuildingMembership.objects.create(
            user=user,
            building=self.building,
            role=MembershipRole.TECHNICIAN,  # grants MANAGE_BUILDINGS but not APPROVE
        )
        order = self._create_order()
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:work_order_archive", args=[order.pk])
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertIsNotNone(order.archived_at)


class ArchivedWorkOrderPurgeViewTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.owner = self.User.objects.create_user(username="owner2", password="pass1234")
        self.building = Building.objects.create(
            owner=self.owner,
            name="Purge Tower",
            address="2 Main",
        )
        self.admin = self.User.objects.create_user(username="admin", password="pass1234")
        BuildingMembership.objects.create(
            user=self.admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )

    def _create_archived_order(self, days_ago: int):
        order = WorkOrder.objects.create(
            building=self.building,
            title=f"Order-{days_ago}",
            deadline=timezone.localdate(),
            status=WorkOrder.Status.DONE,
        )
        order.archived_at = timezone.now() - timedelta(days=days_ago)
        order.save(update_fields=["archived_at"])
        return order

    def test_requires_admin_or_backoffice_role(self):
        lawyer = self.User.objects.create_user(username="lawyer-purge", password="pass1234")
        BuildingMembership.objects.create(
            user=lawyer,
            building=None,
            role=MembershipRole.LAWYER,
        )
        self.client.force_login(lawyer)
        response = self.client.post(
            reverse("core:work_orders_archive_purge"),
            {
                "from_date": timezone.localdate().isoformat(),
                "to_date": timezone.localdate().isoformat(),
                "confirm": "on",
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_can_purge_orders_in_range(self):
        old_order = self._create_archived_order(days_ago=30)
        recent_order = self._create_archived_order(days_ago=3)
        self.client.force_login(self.admin)
        today = timezone.localdate()
        response = self.client.post(
            reverse("core:work_orders_archive_purge"),
            {
                "from_date": (today - timedelta(days=7)).isoformat(),
                "to_date": today.isoformat(),
                "confirm": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(WorkOrder.objects.filter(pk=old_order.pk).exists())
        self.assertFalse(WorkOrder.objects.filter(pk=recent_order.pk).exists())

    def test_lawyer_denied_for_non_lawyer_only_order(self):
        user = self.User.objects.create_user(username="lawyer", password="pass1234")
        BuildingMembership.objects.create(
            user=user,
            building=self.building,
            role=MembershipRole.LAWYER,  # can view but not manage/approve
        )
        order = self._create_order()
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:work_order_archive", args=[order.pk])
        )

        self.assertIn(response.status_code, (302, 403))
        order.refresh_from_db()
        self.assertIsNone(order.archived_at)

    def test_lawyer_can_archive_lawyer_only_order(self):
        user = self.User.objects.create_user(username="lawyer2", password="pass1234")
        BuildingMembership.objects.create(
            user=user,
            building=self.building,
            role=MembershipRole.LAWYER,
        )
        order = self._create_order()
        order.lawyer_only = True
        order.save(update_fields=["lawyer_only"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:work_order_archive", args=[order.pk])
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertIsNotNone(order.archived_at)
