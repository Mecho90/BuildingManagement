from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.forms import (
    AdminUserCreateForm,
    AdminUserUpdateForm,
    BuildingMembershipForm,
    UnitForm,
    WorkOrderForm,
)
from core.models import Building, BuildingMembership, MembershipRole, Unit, WorkOrder


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
        BuildingMembership.objects.create(
            user=self.owner,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )

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
            any("You cannot create work orders for buildings" in err for err in building_errors),
            building_errors,
        )

    def test_building_queryset_respects_membership(self):
        form = WorkOrderForm(user=self.other_user)
        self.assertEqual(list(form.fields["building"].queryset), [self.other_building])

    def test_technician_cannot_skip_to_awaiting(self):
        BuildingMembership.objects.create(
            user=self.other_user,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Test",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
        )
        form = WorkOrderForm(
            data={
                "title": order.title,
                "building": self.building.pk,
                "unit": self.unit.pk,
                "priority": WorkOrder.Priority.MEDIUM,
                "status": WorkOrder.Status.AWAITING_APPROVAL,
                "deadline": order.deadline,
                "description": order.description,
                "replacement_request_note": "Need parts",
            },
            instance=order,
            user=self.other_user,
            building=self.building,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("You cannot select this status.", "".join(form.errors.get("status", [])))

    def test_technician_sets_awaiting_records_request(self):
        BuildingMembership.objects.create(
            user=self.other_user,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Test",
            status=WorkOrder.Status.IN_PROGRESS,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
        )
        form = WorkOrderForm(
            data={
                "title": order.title,
                "building": self.building.pk,
                "unit": self.unit.pk,
                "priority": WorkOrder.Priority.MEDIUM,
                "status": WorkOrder.Status.AWAITING_APPROVAL,
                "deadline": order.deadline,
                "description": order.description,
                "replacement_request_note": "Replace filter",
            },
            instance=order,
            user=self.other_user,
            building=self.building,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.awaiting_approval_by, self.other_user)
        self.assertEqual(saved.replacement_request_note, "Replace filter")

    def test_same_user_cannot_approve_without_capability(self):
        BuildingMembership.objects.create(
            user=self.other_user,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Awaiting",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            awaiting_approval_by=self.other_user,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
        )
        form = WorkOrderForm(
            data={
                "title": order.title,
                "building": self.building.pk,
                "unit": self.unit.pk,
                "priority": WorkOrder.Priority.MEDIUM,
                "status": WorkOrder.Status.APPROVED,
                "deadline": order.deadline,
                "description": order.description,
                "replacement_request_note": order.replacement_request_note,
            },
            instance=order,
            user=self.other_user,
            building=self.building,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("do not have permission", "".join(form.errors.get("status", [])))

    def test_same_user_cannot_reject_without_capability(self):
        BuildingMembership.objects.create(
            user=self.other_user,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Awaiting",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            awaiting_approval_by=self.owner,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
            replacement_request_note="Need pump",
        )
        form = WorkOrderForm(
            data={
                "title": order.title,
                "building": self.building.pk,
                "unit": self.unit.pk,
                "priority": WorkOrder.Priority.MEDIUM,
                "status": WorkOrder.Status.REJECTED,
                "deadline": order.deadline,
                "description": order.description,
                "replacement_request_note": order.replacement_request_note,
            },
            instance=order,
            user=self.other_user,
            building=self.building,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("do not have permission", "".join(form.errors.get("status", [])))

    def test_backoffice_can_approve(self):
        BuildingMembership.objects.create(
            user=self.other_user,
            building=self.building,
            role=MembershipRole.BACKOFFICE,
        )
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Awaiting",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            awaiting_approval_by=self.owner,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
            replacement_request_note="Need pump",
        )
        form = WorkOrderForm(
            data={
                "title": order.title,
                "building": self.building.pk,
                "unit": self.unit.pk,
                "priority": WorkOrder.Priority.MEDIUM,
                "status": WorkOrder.Status.APPROVED,
                "deadline": order.deadline,
                "description": order.description,
                "replacement_request_note": order.replacement_request_note,
            },
            instance=order,
            user=self.other_user,
            building=self.building,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.status, WorkOrder.Status.APPROVED)
        self.assertIsNone(saved.awaiting_approval_by)

    def test_backoffice_status_choices_for_awaiting(self):
        BuildingMembership.objects.create(
            user=self.other_user,
            building=self.building,
            role=MembershipRole.BACKOFFICE,
        )
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Awaiting",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
        )
        form = WorkOrderForm(instance=order, user=self.other_user, building=self.building)
        statuses = [value for value, _ in form.fields["status"].choices]
        self.assertEqual(statuses, [WorkOrder.Status.REJECTED, WorkOrder.Status.APPROVED])

    def test_non_approver_status_choices_for_awaiting(self):
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Awaiting",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
        )
        form = WorkOrderForm(instance=order, user=self.other_user, building=self.building)
        statuses = [value for value, _ in form.fields["status"].choices]
        self.assertEqual(statuses, [WorkOrder.Status.AWAITING_APPROVAL])

    def test_backoffice_can_reject(self):
        BuildingMembership.objects.create(
            user=self.other_user,
            building=self.building,
            role=MembershipRole.BACKOFFICE,
        )
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Awaiting",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
            replacement_request_note="Need pump",
        )
        form = WorkOrderForm(
            data={
                "title": order.title,
                "building": self.building.pk,
                "unit": self.unit.pk,
                "priority": WorkOrder.Priority.MEDIUM,
                "status": WorkOrder.Status.REJECTED,
                "deadline": order.deadline,
                "description": order.description,
                "replacement_request_note": order.replacement_request_note,
            },
            instance=order,
            user=self.other_user,
            building=self.building,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.status, WorkOrder.Status.REJECTED)

    def test_awaiting_requires_available_approver(self):
        BuildingMembership.objects.filter(role=MembershipRole.ADMINISTRATOR).delete()
        order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Test",
            status=WorkOrder.Status.IN_PROGRESS,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
        )
        form = WorkOrderForm(
            data={
                "title": order.title,
                "building": self.building.pk,
                "unit": self.unit.pk,
                "priority": WorkOrder.Priority.MEDIUM,
                "status": WorkOrder.Status.AWAITING_APPROVAL,
                "deadline": order.deadline,
                "description": order.description,
                "replacement_request_note": "Need approval",
            },
            instance=order,
            user=self.other_user,
            building=self.building,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("No approvers", "".join(form.errors.get("status", [])))


class BuildingMembershipFormTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("bm-admin", "bm-admin@example.com", "pass1234")
        self.tech_user = User.objects.create_user("tech-user", "tech@example.com", "pass1234")
        self.backoffice_user = User.objects.create_user("bo-user", "bo@example.com", "pass1234")
        BuildingMembership.objects.create(user=self.tech_user, building=None, role=MembershipRole.TECHNICIAN)
        BuildingMembership.objects.create(user=self.backoffice_user, building=None, role=MembershipRole.BACKOFFICE)
        self.building = Building.objects.create(owner=self.admin, name="Form Plaza", address="Form St")

    def test_requires_subrole_for_technician(self):
        form = BuildingMembershipForm(
            data={"user": [self.tech_user.pk], "role": MembershipRole.TECHNICIAN},
            building=self.building,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Select a sub-role", "".join(form.errors.get("user", [])))

    def test_accepts_subrole_for_technician(self):
        subrole_key = f"subrole_user_{self.tech_user.pk}"
        form = BuildingMembershipForm(
            data={
                "user": [self.tech_user.pk],
                "role": MembershipRole.TECHNICIAN,
                subrole_key: Building.Role.TECH_SUPPORT,
            },
            building=self.building,
        )
        self.assertTrue(form.is_valid(), form.errors)
        memberships = form.save()
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].user, self.tech_user)
        self.assertEqual(memberships[0].technician_subrole, Building.Role.TECH_SUPPORT)

    def test_backoffice_assignment_valid(self):
        form = BuildingMembershipForm(
            data={
                "user": [self.backoffice_user.pk],
                "role": MembershipRole.BACKOFFICE,
            },
            building=self.building,
        )
        self.assertTrue(form.is_valid(), form.errors)
        created = form.save()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].user, self.backoffice_user)


class AdminUserFormTests(TestCase):
    def test_create_assigns_role_membership(self):
        form = AdminUserCreateForm(
            data={
                "username": "new-admin",
                "password1": "SuperSecret123",
                "password2": "SuperSecret123",
                "roles": [MembershipRole.BACKOFFICE],
                "is_active": True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        membership = BuildingMembership.objects.get(user=user, building__isnull=True)
        self.assertEqual(membership.role, MembershipRole.BACKOFFICE)
        self.assertFalse(user.is_superuser)

    def test_update_respects_existing_role(self):
        user = get_user_model().objects.create_user("edit-user", password="pass1234")
        BuildingMembership.objects.create(user=user, building=None, role=MembershipRole.BACKOFFICE)
        form = AdminUserUpdateForm(
            instance=user,
            data={
                "username": "edit-user",
                "roles": [MembershipRole.ADMINISTRATOR],
                "is_active": True,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()
        membership = BuildingMembership.objects.get(user=updated, building__isnull=True)
        self.assertEqual(membership.role, MembershipRole.ADMINISTRATOR)
        updated.refresh_from_db()
        self.assertTrue(updated.is_superuser)

    def test_technician_status_choices_exclude_done(self):
        BuildingMembership.objects.create(
            user=self.other_user,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        form = WorkOrderForm(user=self.other_user, building=self.building)
        statuses = {value for value, _ in form.fields["status"].choices}
        self.assertNotIn(WorkOrder.Status.DONE, statuses)
        self.assertNotIn(WorkOrder.Status.APPROVED, statuses)

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
