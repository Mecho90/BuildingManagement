from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.authz import CapabilityResolver
from core.models import (
    Building,
    BuildingMembership,
    Capability,
    MembershipRole,
)


class BuildingMembershipCapabilityOverrideTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="member", password="pass1234")
        self.building = Building.objects.create(
            owner=self.user,
            name="Test Building",
            address="123 Street",
        )

    def test_valid_overrides_normalize_duplicates(self):
        membership = BuildingMembership(
            user=self.user,
            building=self.building,
            role=MembershipRole.BACKOFFICE,
            capabilities_override={
                "add": [
                    Capability.MANAGE_BUILDINGS,
                    Capability.MANAGE_BUILDINGS,
                    Capability.VIEW_USERS,
                ],
                "remove": [
                    Capability.MASS_ASSIGN,
                    Capability.MASS_ASSIGN,
                ],
            },
        )
        membership.save()
        overrides = membership.capabilities_override
        self.assertEqual(
            overrides["add"],
            [Capability.MANAGE_BUILDINGS, Capability.VIEW_USERS],
        )
        self.assertEqual(overrides["remove"], [Capability.MASS_ASSIGN])

    def test_invalid_add_capability_raises(self):
        membership = BuildingMembership(
            user=self.user,
            building=self.building,
            role=MembershipRole.BACKOFFICE,
            capabilities_override={"add": ["unknown_cap"]},
        )
        with self.assertRaises(ValidationError):
            membership.save()

    def test_invalid_remove_capability_raises(self):
        membership = BuildingMembership(
            user=self.user,
            building=self.building,
            role=MembershipRole.BACKOFFICE,
            capabilities_override={"remove": ["another_fake_cap"]},
        )
        with self.assertRaises(ValidationError):
            membership.save()

    def test_lawyer_always_has_create_units_capability(self):
        membership = BuildingMembership.objects.create(
            user=self.user,
            building=None,
            role=MembershipRole.LAWYER,
            capabilities_override={"remove": [Capability.CREATE_UNITS]},
        )
        self.assertIn(Capability.CREATE_UNITS, membership.resolved_capabilities)
        resolver = CapabilityResolver(self.user)
        self.assertTrue(resolver.has(Capability.CREATE_UNITS, building_id=self.building.pk))
