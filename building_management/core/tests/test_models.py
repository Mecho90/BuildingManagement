from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from core.authz import Capability, CapabilityResolver
from core.models import (
    Building,
    BuildingMembership,
    MembershipRole,
    RoleAuditLog,
    Unit,
    WorkOrder,
)


class BuildingQuerySetTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="pass1234",
        )
        self.other_owner = user_model.objects.create_user(
            username="other",
            email="other@example.com",
            password="pass1234",
        )

        self.building = Building.objects.create(
            owner=self.owner,
            name="Alpha Complex",
            address="1 Main Street",
        )
        self.other_building = Building.objects.create(
            owner=self.other_owner,
            name="Beta Complex",
            address="2 Main Street",
        )

        Unit.objects.create(building=self.building, number="1A")
        Unit.objects.create(building=self.building, number="2A")
        Unit.objects.create(building=self.other_building, number="101")

        today = timezone.localdate()
        WorkOrder.objects.create(
            building=self.building,
            unit=self.building.units.first(),
            title="Fix elevator",
            priority=WorkOrder.Priority.HIGH,
            status=WorkOrder.Status.OPEN,
            deadline=today + timedelta(days=3),
        )
        WorkOrder.objects.create(
            building=self.building,
            unit=self.building.units.last(),
            title="Paint lobby",
            priority=WorkOrder.Priority.MEDIUM,
            status=WorkOrder.Status.IN_PROGRESS,
            deadline=today + timedelta(days=10),
        )
        WorkOrder.objects.create(
            building=self.building,
            title="Awaiting parts",
            priority=WorkOrder.Priority.MEDIUM,
            status=WorkOrder.Status.AWAITING_APPROVAL,
            deadline=today + timedelta(days=5),
        )
        archived = WorkOrder.objects.create(
            building=self.building,
            title="Replace bulbs",
            priority=WorkOrder.Priority.LOW,
            status=WorkOrder.Status.DONE,
            deadline=today - timedelta(days=1),
        )
        archived.status = WorkOrder.Status.DONE
        archived.archive()

    def test_with_unit_stats_annotates_counts(self):
        annotated = {
            b.pk: b
            for b in Building.objects.order_by("pk").with_unit_stats()
        }

        alpha = annotated[self.building.pk]
        beta = annotated[self.other_building.pk]

        self.assertEqual(alpha.units_count, 2)
        self.assertEqual(alpha.work_orders_count, 3)  # awaiting approval counted
        self.assertEqual(beta.units_count, 1)
        self.assertEqual(beta.work_orders_count, 0)

    def test_visible_to_uses_memberships(self):
        BuildingMembership.objects.create(
            user=self.other_owner,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )

        visible = list(Building.objects.visible_to(self.other_owner))
        self.assertEqual(visible, [self.building])


class WorkOrderSaveTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="workowner",
            email="workowner@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.owner,
            name="Gamma Complex",
            address="3 Main Street",
        )
        self.unit = Unit.objects.create(building=self.building, number="301")

    def test_save_calls_full_clean_by_default(self):
        with mock.patch("core.models.WorkOrder.full_clean") as full_clean:
            full_clean.return_value = None
            order = WorkOrder(
                building=self.building,
                unit=self.unit,
                title="Inspect HVAC",
                status=WorkOrder.Status.OPEN,
                priority=WorkOrder.Priority.MEDIUM,
                deadline=timezone.localdate() + timedelta(days=5),
            )
            order.save()
        full_clean.assert_called_once()

    def test_archive_skips_full_clean_when_updating_timestamp(self):
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Seal windows",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.HIGH,
            deadline=timezone.localdate() + timedelta(days=7),
        )
        order.status = WorkOrder.Status.DONE

        with mock.patch("core.models.WorkOrder.full_clean") as full_clean:
            order.archive()

        self.assertTrue(order.is_archived)
        full_clean.assert_not_called()


class WorkOrderVisibilityTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user("law-owner", password="pass1234")
        self.lawyer = user_model.objects.create_user("law-viewer", password="pass1234")
        BuildingMembership.objects.create(user=self.lawyer, building=None, role=MembershipRole.LAWYER)
        self.building = Building.objects.create(owner=self.owner, name="Legal Tower")
        self.unit = Unit.objects.create(building=self.building, number="1L")
        today = timezone.localdate()
        self.confidential = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Legal follow-up",
            priority=WorkOrder.Priority.HIGH,
            status=WorkOrder.Status.OPEN,
            deadline=today + timedelta(days=5),
            lawyer_only=True,
            created_by=self.lawyer,
        )
        self.regular = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="General maintenance",
            priority=WorkOrder.Priority.LOW,
            status=WorkOrder.Status.OPEN,
            deadline=today + timedelta(days=7),
            created_by=self.owner,
        )

    def _create_user_with_role(self, username: str, role: str):
        user = get_user_model().objects.create_user(username=username, password="pass1234")
        BuildingMembership.objects.create(user=user, building=None, role=role)
        return user

    def test_technician_cannot_see_confidential_orders(self):
        tech = get_user_model().objects.create_user("tech-view", password="pass1234")
        BuildingMembership.objects.create(user=tech, building=self.building, role=MembershipRole.TECHNICIAN)
        visible = WorkOrder.objects.visible_to(tech)
        self.assertEqual(list(visible), [self.regular])

    def test_lawyer_can_see_confidential_orders(self):
        visible = WorkOrder.objects.visible_to(self.lawyer)
        self.assertCountEqual(list(visible), [self.confidential, self.regular])

    def test_admin_can_see_confidential_orders(self):
        admin = self._create_user_with_role("admin-view", MembershipRole.ADMINISTRATOR)
        visible = WorkOrder.objects.visible_to(admin)
        self.assertCountEqual(list(visible), [self.confidential, self.regular])


class UnitConstraintTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="unitowner",
            email="unitowner@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.owner,
            name="Delta Complex",
            address="4 Main Street",
        )
        Unit.objects.create(building=self.building, number="1A")

    def test_duplicate_unit_number_raises_custom_message(self):
        dupe = Unit(building=self.building, number="1a")
        with self.assertRaisesMessage(
            ValidationError,
            "Apartment number must be unique within this building.",
        ):
            dupe.full_clean()


class CapabilityResolverTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="resolver",
            email="resolver@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.user,
            name="Resolver Complex",
            address="5 Main Street",
        )

    def test_membership_override_applies(self):
        membership = BuildingMembership.objects.create(
            user=self.user,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
            capabilities_override={"add": [Capability.MANAGE_BUILDINGS], "remove": [Capability.CREATE_WORK_ORDERS]},
        )
        caps = membership.resolved_capabilities
        self.assertIn(Capability.MANAGE_BUILDINGS, caps)
        self.assertNotIn(Capability.CREATE_WORK_ORDERS, caps)

    def test_resolver_with_global_membership_allows_all_buildings(self):
        BuildingMembership.objects.create(
            user=self.user,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )
        resolver = CapabilityResolver(self.user)
        self.assertIsNone(resolver.visible_building_ids())
        self.assertTrue(resolver.has(Capability.VIEW_ALL_BUILDINGS))
