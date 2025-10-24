from __future__ import annotations

from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.test import TransactionTestCase

from core.models import Building, Unit


class UnitConstraintTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.user,
            name="Sunset Plaza",
            address="123 Main St",
        )

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL-specific assertion")
    def test_unit_number_case_insensitive_unique_constraint(self):
        Unit.objects.create(
            building=self.building,
            number="1A",
        )
        with self.assertRaises(IntegrityError):
            Unit.objects.create(
                building=self.building,
                number="1a",
            )
