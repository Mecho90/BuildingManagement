from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Building, WorkOrder
from core.views.health import METRICS_CACHE_KEY


class ForwardingHealthEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user(username="owner", password="pass")
        self.staff = get_user_model().objects.create_user(username="staff", password="pass", is_staff=True)
        self.office = Building.objects.create(
            owner=self.owner,
            name="Office",
            is_system_default=True,
        )
        self.dest = Building.objects.create(owner=self.owner, name="Destination")

    def test_requires_auth_and_staff_permissions(self):
        response = self.client.get(reverse("core:forwarding_health"))
        self.assertEqual(response.status_code, 302)  # redirected to login

        regular_user = get_user_model().objects.create_user(username="user", password="pass")
        self.client.force_login(regular_user)
        response = self.client.get(reverse("core:forwarding_health"))
        self.assertEqual(response.status_code, 403)

    def test_reports_queue_sizes(self):
        self.client.force_login(self.staff)
        cache.delete(METRICS_CACHE_KEY)
        WorkOrder.objects.create(
            building=self.office,
            title="Needs forwarding",
            deadline=timezone.localdate(),
        )
        WorkOrder.objects.create(
            building=self.office,
            title="Forwarded order",
            deadline=timezone.localdate(),
            forwarded_to_building=self.dest,
        )
        WorkOrder.objects.create(
            building=self.dest,
            title="Awaiting approval",
            deadline=timezone.localdate(),
            status=WorkOrder.Status.AWAITING_APPROVAL,
        )

        response = self.client.get(reverse("core:forwarding_health"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["office_building_id"], self.office.pk)
        self.assertEqual(payload["office_queue_size"], 1)
        self.assertEqual(payload["forwarded_pipeline_size"], 1)
        self.assertEqual(payload["awaiting_approval_total"], 1)
        self.assertEqual(payload["office_status"], "ok")
        self.assertFalse(payload["cache_hit"])

    @override_settings(FORWARDING_HEALTH_RATE_LIMIT=(2, 60))
    def test_rate_limit_blocks_excess_calls(self):
        self.client.force_login(self.staff)
        url = reverse("core:forwarding_health")
        for _ in range(2):
            self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 429)

    @override_settings(FORWARDING_HEALTH_RATE_LIMIT=(100, 60), FORWARDING_HEALTH_CACHE_TIMEOUT=60)
    def test_cached_metrics_reused_until_expiry(self):
        self.client.force_login(self.staff)
        cache.delete(METRICS_CACHE_KEY)
        url = reverse("core:forwarding_health")
        self.assertEqual(self.client.get(url).json()["office_queue_size"], 0)
        WorkOrder.objects.create(
            building=self.office,
            title="New pending",
            deadline=timezone.localdate(),
        )
        second = self.client.get(url).json()
        self.assertEqual(second["office_queue_size"], 0)
        self.assertTrue(second["cache_hit"])

    def test_missing_office_gracefully_degrades(self):
        self.client.force_login(self.staff)
        cache.delete(METRICS_CACHE_KEY)
        Building.objects.all().update(is_system_default=False)
        url = reverse("core:forwarding_health")
        payload = self.client.get(url).json()
        self.assertIsNone(payload["office_building_id"])
        self.assertEqual(payload["office_status"], "missing")
        self.assertEqual(payload["office_queue_size"], 0)
        self.assertEqual(payload["forwarded_pipeline_size"], 0)

    def test_duplicate_office_records_reported(self):
        self.client.force_login(self.staff)
        cache.delete(METRICS_CACHE_KEY)
        duplicate = Building.objects.create(
            owner=self.owner,
            name="Office Clone",
            is_system_default=False,
        )
        url = reverse("core:forwarding_health")
        with mock.patch("core.views.health.Building.objects") as mock_manager:
            mock_qs = mock.MagicMock()
            mock_qs.values_list.return_value = [self.office.pk, duplicate.pk]
            mock_manager.filter.return_value = mock_qs
            payload = self.client.get(url).json()
        self.assertEqual(payload["office_status"], "duplicate")
        self.assertIsNone(payload["office_building_id"])
        self.assertCountEqual(
            payload["duplicate_office_ids"],
            [self.office.pk, duplicate.pk],
        )

    @override_settings(FORWARDING_HEALTH_CACHE_TIMEOUT=45)
    def test_high_volume_metrics_remain_cached(self):
        self.client.force_login(self.staff)
        cache.delete(METRICS_CACHE_KEY)
        pending = [
            WorkOrder(
                building=self.office,
                title=f"Pending {idx}",
                deadline=timezone.localdate(),
            )
            for idx in range(50)
        ]
        forwarded = [
            WorkOrder(
                building=self.office,
                title=f"Forwarded {idx}",
                deadline=timezone.localdate(),
                forwarded_to_building=self.dest,
            )
            for idx in range(75)
        ]
        WorkOrder.objects.bulk_create(pending + forwarded)

        url = reverse("core:forwarding_health")
        first = self.client.get(url).json()
        self.assertEqual(first["office_queue_size"], len(pending))
        self.assertEqual(first["forwarded_pipeline_size"], len(forwarded))
        self.assertFalse(first["cache_hit"])

        second = self.client.get(url).json()
        self.assertEqual(second["office_queue_size"], len(pending))
        self.assertEqual(second["forwarded_pipeline_size"], len(forwarded))
        self.assertTrue(second["cache_hit"])
