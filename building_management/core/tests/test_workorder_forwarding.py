from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Building, WorkOrder, WorkOrderForwarding


class WorkOrderForwardingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.forwarder = User.objects.create_user(username="forwarder", password="pass")
        self.dest_owner = User.objects.create_user(username="destination-owner", password="pass")
        self.office = Building.objects.create(owner=self.owner, name="Office", is_system_default=True)
        self.dest = Building.objects.create(owner=self.dest_owner, name="Destination")

    def _base_order(self, building=None):
        return WorkOrder(
            building=building or self.office,
            title="Leak",
            description="",
            deadline=timezone.localdate(),
        )

    def test_non_office_building_cannot_forward(self):
        order = self._base_order(building=self.dest)
        order.forwarded_to_building = self.dest
        with self.assertRaises(ValidationError):
            order.full_clean()

    def test_forward_target_must_differ_from_origin(self):
        order = self._base_order()
        order.forwarded_to_building = self.office
        with self.assertRaises(ValidationError):
            order.full_clean()

    def test_forward_metadata_requires_destination(self):
        order = self._base_order()
        order.forward_note = "Handle ASAP"
        with self.assertRaises(ValidationError):
            order.full_clean()

    def test_history_entry_created_on_forward(self):
        order = self._base_order()
        order.save()
        order.forwarded_to_building = self.dest
        order.forwarded_by = self.forwarder
        order.forward_note = "Please dispatch"
        order.save()
        history = WorkOrderForwarding.objects.filter(work_order=order).first()
        self.assertIsNotNone(history)
        self.assertEqual(history.from_building, self.office)
        self.assertEqual(history.to_building, self.dest)
        self.assertEqual(history.forwarded_by, self.forwarder)
        self.assertEqual(history.note, "Please dispatch")
        self.assertIsNotNone(order.forwarded_at)

    def test_destination_owner_sees_forwarded_orders_on_building_page(self):
        order = self._base_order()
        order.save()
        order.forwarded_to_building = self.dest
        order.forwarded_by = self.forwarder
        order.save()
        self.client.force_login(self.dest_owner)
        response = self.client.get(reverse("core:building_detail", args=[self.dest.pk]))
        self.assertContains(response, order.title)

    def test_forwarded_order_stays_visible_after_destination_deleted(self):
        order = self._base_order()
        order.save()
        order.forwarded_to_building = self.dest
        order.forwarded_by = self.forwarder
        order.save()

        # Deleting the destination should nullify the forwarding target.
        self.dest.delete()
        order.refresh_from_db()
        self.assertIsNone(order.forwarded_to_building)

        # Office owner should still see the work order on the Office page.
        self.client.force_login(self.owner)
        response = self.client.get(reverse("core:building_detail", args=[self.office.pk]))
        self.assertContains(response, order.title)
