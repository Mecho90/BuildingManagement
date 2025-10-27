from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from core.models import Building, Unit, WorkOrder


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
        self.assertEqual(alpha.work_orders_count, 2)  # archived order excluded
        self.assertEqual(beta.units_count, 1)
        self.assertEqual(beta.work_orders_count, 0)


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
