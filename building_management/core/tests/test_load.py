from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Building, WorkOrder


class ForwardingHealthLoadTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(username="staff", password="pass", is_staff=True)
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.office = Building.objects.create(owner=self.owner, name="Office", is_system_default=True)
        self.dest = Building.objects.create(owner=self.owner, name="Destination")
        self.client.force_login(self.staff)
        WorkOrder.objects.create(
            building=self.office,
            title="Needs routing",
            deadline=timezone.localdate(),
            forwarded_to_building=self.dest,
        )

    @override_settings(FORWARDING_HEALTH_RATE_LIMIT=(10_000, 60))
    def test_forwarding_health_handles_burst_load(self):
        url = reverse("core:forwarding_health")
        responses = []
        for _ in range(250):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            responses.append(response)
        final_payload = responses[-1].json()
        self.assertTrue(final_payload["cache_hit"])
        self.assertEqual(final_payload["office_building_id"], self.office.pk)


class WorkOrderListLoadTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="admin", password="pass", is_superuser=True, is_staff=True)
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.office = Building.objects.create(owner=self.owner, name="Office", is_system_default=True)
        self.other = Building.objects.create(owner=self.owner, name="North Tower")
        for idx in range(60):
            WorkOrder.objects.create(
                building=self.office if idx % 2 == 0 else self.other,
                title=f"Ticket {idx}",
                deadline=timezone.localdate(),
            )
        self.client.force_login(self.admin)

    def test_work_order_list_under_repeated_access(self):
        url = reverse("core:work_orders_list")
        for _ in range(50):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

    def test_building_detail_under_repeated_access(self):
        url = reverse("core:building_detail", args=[self.office.pk])
        for _ in range(50):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
