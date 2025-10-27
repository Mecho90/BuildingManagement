from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.forms import UnitForm, WorkOrderForm
from core.models import Building, Unit, WorkOrder


class UnitFormTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="alpha",
            email="alpha@example.com",
            password="pass1234",
        )
        self.other_user = user_model.objects.create_user(
            username="beta",
            email="beta@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.owner,
            name="Skyline Tower",
            address="1 Main St",
        )

    def test_unit_form_prevents_non_owner_edit(self):
        form = UnitForm(
            data={
                "number": "10",
                "floor": 1,
                "owner_name": "John Doe",
                "contact_phone": "+15555555555",
                "is_occupied": True,
                "description": "Test unit",
            },
            user=self.other_user,
            building=self.building,
        )
        self.assertFalse(form.is_valid())
        self.assertIn(
            "You don't have permission to edit this unit.",
            "".join(form.non_field_errors()),
        )

    def test_unit_form_allows_owner(self):
        form = UnitForm(
            data={
                "number": "10",
                "floor": 1,
                "owner_name": "John Doe",
                "contact_phone": "+15555555555",
                "is_occupied": True,
                "description": "Test unit",
            },
            user=self.owner,
            building=self.building,
        )
        self.assertTrue(form.is_valid(), form.errors)
        unit = form.save()
        self.assertEqual(unit.building, self.building)


class WorkOrderFormTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="gamma",
            email="gamma@example.com",
            password="pass1234",
        )
        self.other_user = user_model.objects.create_user(
            username="delta",
            email="delta@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.owner,
            name="Riverfront",
            address="2 River St",
        )
        self.other_building = Building.objects.create(
            owner=self.other_user,
            name="Lakeside",
            address="3 Lake St",
        )
        self.unit = Unit.objects.create(building=self.building, number="101")
        self.other_unit = Unit.objects.create(building=self.other_building, number="201")

    def _base_data(self, **overrides):
        data = {
            "title": "Check HVAC",
            "building": self.building.pk,
            "unit": self.unit.pk,
            "priority": WorkOrder.Priority.MEDIUM,
            "status": WorkOrder.Status.OPEN,
            "deadline": (timezone.localdate() + timedelta(days=7)).isoformat(),
            "description": "Routine maintenance",
        }
        data.update(overrides)
        return data

    def test_non_owner_cannot_target_foreign_building(self):
        form = WorkOrderForm(
            data={
                "title": "Check HVAC",
                "unit": self.unit.pk,
                "priority": WorkOrder.Priority.MEDIUM,
                "status": WorkOrder.Status.OPEN,
                "deadline": (timezone.localdate() + timedelta(days=7)).isoformat(),
                "description": "Routine maintenance",
            },
            user=self.other_user,
            building=self.building,
        )
        self.assertFalse(form.is_valid())
        building_errors = form.errors.get("building", []) + form.non_field_errors()
        self.assertTrue(
            any("You cannot create work orders" in err for err in building_errors),
            building_errors,
        )

    def test_unit_mismatch_adds_error(self):
        form = WorkOrderForm(
            data=self._base_data(unit=self.other_unit.pk),
            user=self.owner,
        )
        self.assertFalse(form.is_valid())
        self.assertTrue(form.errors.get("unit"), form.errors)

    def test_locked_building_applied_on_save(self):
        form = WorkOrderForm(
            data={
                "title": "Inspect roof",
                "unit": self.unit.pk,
                "priority": WorkOrder.Priority.HIGH,
                "status": WorkOrder.Status.OPEN,
                "deadline": (timezone.localdate() + timedelta(days=3)).isoformat(),
                "description": "Urgent work",
            },
            user=self.owner,
            building=self.building,
        )
        self.assertTrue(form.is_valid(), form.errors)
        work_order = form.save()
        self.assertEqual(work_order.building, self.building)
