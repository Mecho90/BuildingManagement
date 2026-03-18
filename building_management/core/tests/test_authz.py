from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.authz import CapabilityResolver
from core.models import (
    Building,
    BuildingMembership,
    MembershipRole,
    WorkOrder,
)


class WorkOrderVisibilityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.office_owner = User.objects.create_user(username="office-admin", password="pass", is_superuser=True)
        self.destination_owner = User.objects.create_user(username="destination-owner", password="pass")
        self.backoffice_user = User.objects.create_user(username="global-backoffice", password="pass")
        BuildingMembership.objects.create(user=self.backoffice_user, building=None, role=MembershipRole.BACKOFFICE)
        self.office = Building.objects.create(owner=self.office_owner, name="Office", is_system_default=True)
        self.destination = Building.objects.create(owner=self.destination_owner, name="Destination")

    def _create_forwarded_order(self, **overrides):
        data = {
            "building": self.office,
            "title": overrides.get("title", "Office Request"),
            "deadline": overrides.get("deadline", timezone.localdate()),
            "forwarded_to_building": overrides.get("forwarded_to_building", self.destination),
            "forwarded_by": overrides.get("forwarded_by", self.office_owner),
            "forward_note": overrides.get("forward_note", ""),
            "lawyer_only": overrides.get("lawyer_only", False),
        }
        data.update({k: v for k, v in overrides.items() if k not in data})
        return WorkOrder.objects.create(**data)

    def test_destination_owner_can_see_forwarded_office_orders(self):
        order = self._create_forwarded_order()
        resolver = CapabilityResolver(self.destination_owner)
        self.assertIn(self.destination.pk, resolver.visible_building_ids())
        qs = WorkOrder.objects.visible_to(self.destination_owner)
        self.assertIn(order.pk, qs.values_list("pk", flat=True))

    def test_global_backoffice_sees_all_forwarded_orders(self):
        order = self._create_forwarded_order(title="Escalation")
        qs = WorkOrder.objects.visible_to(self.backoffice_user)
        self.assertIn(order.pk, qs.values_list("pk", flat=True))

    def test_confidential_forwarded_order_hidden_from_destination_technician(self):
        User = get_user_model()
        technician = User.objects.create_user(username="dest-tech", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = self._create_forwarded_order(lawyer_only=True, title="Confidential")
        qs = WorkOrder.objects.visible_to(technician)
        self.assertNotIn(order.pk, qs.values_list("pk", flat=True))


class OfficeBootstrapTests(TestCase):
    def setUp(self):
        Building.objects.all().delete()
        Building.clear_system_default_cache()
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="bootstrap-admin",
            password="pass",
            is_superuser=True,
        )
        BuildingMembership.objects.create(
            user=self.admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )

    def tearDown(self):
        Building.clear_system_default_cache()

    def test_office_is_created_on_demand_for_admins(self):
        resolver = CapabilityResolver(self.admin)
        office_id = resolver._office_building_id  # pylint: disable=protected-access
        self.assertIsNotNone(office_id)
        self.assertTrue(
            Building.objects.filter(pk=office_id, is_system_default=True).exists()
        )
