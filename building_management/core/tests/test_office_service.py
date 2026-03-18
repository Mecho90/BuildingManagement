from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Building, BuildingMembership, MembershipRole, Unit
from core.services.office import (
    OfficeOwnerResolutionError,
    ensure_office_building,
)


class OfficeServiceTests(TestCase):
    def setUp(self):
        self.User = get_user_model()

    def test_missing_owner_strict_mode_raises(self):
        with self.assertRaises(OfficeOwnerResolutionError):
            ensure_office_building()

    def test_missing_owner_non_strict_returns_none(self):
        self.assertIsNone(ensure_office_building(strict_owner=False))

    def test_creates_office_and_memberships(self):
        owner = self.User.objects.create_user(username="owner", password="pass", is_superuser=True)
        backoffice_user = self.User.objects.create_user(username="backoffice", password="pass")
        BuildingMembership.objects.create(
            user=backoffice_user,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )

        result = ensure_office_building(owner=owner)
        self.assertIsNotNone(result)
        office = result.building
        self.assertTrue(office.is_system_default)
        self.assertEqual(office.owner, owner)

        self.assertTrue(
            BuildingMembership.objects.filter(
                user=backoffice_user,
                building=office,
                role=MembershipRole.BACKOFFICE,
            ).exists()
        )
        self.assertTrue(
            BuildingMembership.objects.filter(
                user=owner,
                building=office,
                role=MembershipRole.TECHNICIAN,
            ).exists()
        )

    def test_purges_units_and_is_idempotent(self):
        owner = self.User.objects.create_user(username="owner2", password="pass", is_superuser=True)
        result = ensure_office_building(owner=owner)
        office = result.building
        Unit.objects.create(building=office, number="1A")

        result_again = ensure_office_building(owner=owner)
        office.refresh_from_db()
        self.assertFalse(Unit.objects.filter(building=office).exists())
        self.assertEqual(result_again.building.pk, office.pk)
        self.assertFalse(result_again.created)
        self.assertEqual(result_again.removed_units, 1)

    def test_owner_with_admin_role_does_not_raise(self):
        owner = self.User.objects.create_user(username="owner3", password="pass", is_superuser=True)
        BuildingMembership.objects.create(
            user=owner,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )

        result = ensure_office_building(owner=owner)
        self.assertIsNotNone(result)
        self.assertTrue(Building.objects.filter(is_system_default=True).exists())
        self.assertFalse(
            BuildingMembership.objects.filter(user=owner, role=MembershipRole.TECHNICIAN).exists()
        )

    def test_global_admin_backoffice_memberships_do_not_error(self):
        owner = self.User.objects.create_user(username="owner4", password="pass", is_superuser=True)
        admin = self.User.objects.create_user(username="global-admin", password="pass")
        backoffice = self.User.objects.create_user(username="global-backoffice", password="pass")
        BuildingMembership.objects.create(user=admin, building=None, role=MembershipRole.ADMINISTRATOR)
        BuildingMembership.objects.create(user=backoffice, building=None, role=MembershipRole.BACKOFFICE)

        result = ensure_office_building(owner=owner)
        self.assertIsNotNone(result)
        self.assertTrue(Building.objects.filter(is_system_default=True).exists())
