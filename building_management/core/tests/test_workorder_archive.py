from __future__ import annotations

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

    def test_user_without_manage_or_approve_is_denied(self):
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
