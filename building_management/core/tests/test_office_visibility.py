from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.authz import CapabilityResolver
from core.models import Building, BuildingMembership, MembershipRole, Unit, WorkOrder


class OfficeVisibilityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.office = Building.objects.create(
            owner=self.owner,
            name="Office",
            is_system_default=True,
        )
        self.another_building = Building.objects.create(
            owner=self.owner,
            name="Tower",
        )

    def _visible_building_ids(self, user):
        qs = Building.objects.visible_to(user)
        return set(qs.values_list("id", flat=True))

    def test_backoffice_global_membership_sees_office(self):
        User = get_user_model()
        user = User.objects.create_user(username="backoffice", password="pass")
        BuildingMembership.objects.create(
            user=user,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )

        resolver = CapabilityResolver(user)
        visible_ids = resolver.visible_building_ids()
        self.assertIn(self.office.id, visible_ids)
        self.assertIn(self.office.id, self._visible_building_ids(user))

    def test_administrator_assignment_gains_office_access(self):
        User = get_user_model()
        user = User.objects.create_user(username="admin-local", password="pass")
        BuildingMembership.objects.create(
            user=user,
            building=self.another_building,
            role=MembershipRole.ADMINISTRATOR,
        )

        resolver = CapabilityResolver(user)
        visible_ids = resolver.visible_building_ids()
        self.assertIn(self.office.id, visible_ids)
        expected = {self.office.id, self.another_building.id}
        self.assertTrue(expected.issubset(self._visible_building_ids(user)))

    def test_technician_does_not_see_office_without_membership(self):
        User = get_user_model()
        tech = User.objects.create_user(username="technician", password="pass")
        BuildingMembership.objects.create(
            user=tech,
            building=self.another_building,
            role=MembershipRole.TECHNICIAN,
        )

        resolver = CapabilityResolver(tech)
        visible_ids = resolver.visible_building_ids()
        self.assertNotIn(self.office.id, visible_ids)
        self.assertNotIn(self.office.id, self._visible_building_ids(tech))

    def test_units_queryset_excludes_office_units(self):
        User = get_user_model()
        user = User.objects.create_user(username="backoffice-2", password="pass")
        BuildingMembership.objects.create(
            user=user,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )
        regular_unit = Unit.objects.create(
            building=self.another_building,
            number="1A",
            floor=1,
        )
        # Force an orphaned office unit (bulk_create bypasses full_clean)
        Unit.objects.bulk_create([Unit(building=self.office, number="O-1")])
        self.assertTrue(Unit.objects.filter(building=self.office).exists())

        visible_units = Unit.objects.visible_to(user)
        self.assertFalse(visible_units.filter(building=self.office).exists())
        self.assertTrue(visible_units.filter(pk=regular_unit.pk).exists())

    def test_office_dashboard_includes_global_awaiting_orders(self):
        User = get_user_model()
        reviewer = User.objects.create_user(username="reviewer", password="pass")
        BuildingMembership.objects.create(
            user=reviewer,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )
        order = WorkOrder.objects.create(
            building=self.another_building,
            title="Needs approval",
            deadline=timezone.localdate(),
            status=WorkOrder.Status.AWAITING_APPROVAL,
        )
        self.client.force_login(reviewer)
        response = self.client.get(reverse("core:building_detail", args=[self.office.pk]))
        self.assertContains(response, order.title)

    def test_office_queue_badges_and_deduplication(self):
        User = get_user_model()
        reviewer = User.objects.create_user(username="queue-reviewer", password="pass")
        BuildingMembership.objects.create(
            user=reviewer,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )
        tower = Building.objects.create(owner=self.owner, name="Tower 2")
        plaza = Building.objects.create(owner=self.owner, name="Central Plaza")

        office_order = WorkOrder.objects.create(
            building=self.office,
            title="Office awaiting",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            deadline=timezone.localdate(),
        )
        WorkOrder.objects.create(
            building=tower,
            title="Tower leak",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            deadline=timezone.localdate(),
        )
        WorkOrder.objects.create(
            building=tower,
            title="Tower HVAC",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            deadline=timezone.localdate(),
        )
        WorkOrder.objects.create(
            building=plaza,
            title="Plaza lights",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            deadline=timezone.localdate(),
        )

        self.client.force_login(reviewer)
        response = self.client.get(reverse("core:building_detail", args=[self.office.pk]))
        page_orders = list(response.context["workorders_page"].object_list)
        ids = [order.id for order in page_orders]
        self.assertIn(office_order.id, ids)
        self.assertEqual(len(ids), len(set(ids)))  # no duplicates

        badges = response.context["awaiting_queue_badges"]
        expected = {("Central Plaza", 1), ("Tower 2", 2)}
        observed = {(badge["origin"], badge["count"]) for badge in badges}
        self.assertSetEqual(expected, observed)

        self.assertContains(response, "Очаква одобрение от бекофиса · Tower 2")
        self.assertContains(response, "Очаква одобрение от бекофиса · Central Plaza")

    def test_destination_owner_sees_forwarded_origin_and_destination_badges(self):
        User = get_user_model()
        destination_owner = User.objects.create_user(username="destination", password="pass")
        destination_building = Building.objects.create(
            owner=destination_owner,
            name="Destination",
        )
        BuildingMembership.objects.create(
            user=destination_owner,
            building=destination_building,
            role=MembershipRole.ADMINISTRATOR,
        )
        order = WorkOrder.objects.create(
            building=self.office,
            title="Forwarded leak",
            deadline=timezone.localdate(),
            forwarded_to_building=destination_building,
        )

        self.client.force_login(destination_owner)
        response = self.client.get(reverse("core:building_detail", args=[destination_building.pk]))
        self.assertContains(response, order.title)
        self.assertContains(response, "Origin · Office")
        self.assertContains(response, f"Destination · {destination_building.name}")

    def test_building_list_bootstraps_office_for_global_staff(self):
        Building.objects.all().delete()
        User = get_user_model()
        admin_user = User.objects.create_user(username="global-admin", password="pass")
        BuildingMembership.objects.create(
            user=admin_user,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )

        self.client.force_login(admin_user)
        response = self.client.get(reverse("core:buildings_list"))
        self.assertTrue(Building.objects.filter(is_system_default=True).exists())
        self.assertContains(response, "Office")
