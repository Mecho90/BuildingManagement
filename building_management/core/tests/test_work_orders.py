from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from core.forms import WorkOrderForm
from core.models import Building, BuildingMembership, MembershipRole, Unit, WorkOrder, WorkOrderAuditLog
from core.views.work_orders import _maybe_handle_forwarding_change


class LawyerOrderIndicatorTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pass")
        self.secondary_owner = User.objects.create_user(username="owner-2", password="pass")
        self.building = Building.objects.create(owner=self.owner, name="Alpha")
        self.other_building = Building.objects.create(owner=self.secondary_owner, name="Beta")
        self.unit_a = Unit.objects.create(building=self.building, number="1A")
        self.unit_b = Unit.objects.create(building=self.building, number="2B")
        self.other_unit = Unit.objects.create(building=self.other_building, number="9Z")

    def _make_order(self, *, unit=None, building=None, lawyer_only=True, archived=False, title_suffix=""):
        if unit is not None:
            building = unit.building
        building = building or self.building
        order = WorkOrder.objects.create(
            building=building,
            unit=unit,
            title=f"Order {WorkOrder.objects.count() + 1}{title_suffix}",
            deadline=timezone.localdate(),
            lawyer_only=lawyer_only,
        )
        if archived:
            order.archived_at = timezone.now()
            order.save(update_fields=["archived_at"])
        return order

    def test_building_annotation_counts_active_lawyer_orders(self):
        self._make_order(unit=self.unit_a)
        self._make_order(unit=self.unit_b, archived=True)
        self._make_order(unit=self.unit_a, lawyer_only=False)
        self._make_order(unit=self.other_unit)

        annotated = (
            Building.objects.filter(pk=self.building.pk)
            .with_lawyer_alerts()
            .get()
        )
        self.assertEqual(annotated.lawyer_orders_count, 1)

    def test_unit_annotation_counts_active_lawyer_orders(self):
        self._make_order(unit=self.unit_a)
        self._make_order(unit=self.unit_a, title_suffix="-2")
        self._make_order(unit=self.unit_a, archived=True, title_suffix="-archived")
        self._make_order(unit=self.unit_b, lawyer_only=False)
        self._make_order(unit=self.other_unit)

        units = (
            Unit.objects.filter(pk__in=[self.unit_a.pk, self.unit_b.pk])
            .with_lawyer_alerts()
        )
        counts = {u.pk: u.lawyer_orders_count for u in units}
        self.assertEqual(counts[self.unit_a.pk], 2)
        self.assertEqual(counts[self.unit_b.pk], 0)


class WorkOrderFormForwardingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="admin", password="pass", is_superuser=True)
        self.backoffice = User.objects.create_user(username="backoffice", password="pass")
        BuildingMembership.objects.create(
            user=self.backoffice,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )
        self.technician = User.objects.create_user(username="tech", password="pass")
        self.office = Building.objects.create(owner=self.admin, name="Office", is_system_default=True)
        self.destination_owner = User.objects.create_user(username="dest-owner", password="pass")
        self.destination = Building.objects.create(owner=self.destination_owner, name="Destination")
        BuildingMembership.objects.create(
            user=self.technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )

    def _base_form_data(self, **overrides):
        data = {
            "title": "Leaky pipe",
            "priority": WorkOrder.Priority.MEDIUM,
            "status": WorkOrder.Status.OPEN,
            "deadline": timezone.localdate().isoformat(),
            "description": "",
            "replacement_request_note": "",
            "office_employee": str(self.backoffice.pk),
            "forward_note": overrides.get("forward_note", "Please dispatch"),
            "forwarded_to_building": overrides.get("forwarded_to_building"),
        }
        data.update(overrides)
        return data

    def test_office_work_order_requires_forward_target(self):
        form = WorkOrderForm(
            data=self._base_form_data(forwarded_to_building=""),
            user=self.admin,
            building=self.office,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("forwarded_to_building", form.errors)

    def test_forwarding_metadata_persists(self):
        form = WorkOrderForm(
            data=self._base_form_data(forwarded_to_building=str(self.destination.pk)),
            user=self.admin,
            building=self.office,
        )
        self.assertTrue(form.is_valid(), form.errors)
        order = form.save()
        self.assertEqual(order.building, self.office)
        self.assertEqual(order.forwarded_to_building, self.destination)
        self.assertEqual(order.forwarded_by, self.admin)
        self.assertEqual(order.forward_note.strip(), "Please dispatch")

    def test_owner_preview_available(self):
        form = WorkOrderForm(
            data=self._base_form_data(forwarded_to_building=str(self.destination.pk)),
            user=self.admin,
            building=self.office,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn(str(self.destination.pk), form.forwarding_owner_map)
        self.assertEqual(
            form.forwarding_owner_map[str(self.destination.pk)],
            self.destination_owner.get_full_name() or self.destination_owner.username,
        )
        self.assertEqual(
            form.forwarding_owner_label,
            self.destination_owner.get_full_name() or self.destination_owner.username,
        )

    def test_building_dropdown_includes_office_for_admin(self):
        form = WorkOrderForm(user=self.admin)
        building_ids = set(form.fields["building"].queryset.values_list("id", flat=True))
        self.assertIn(self.office.pk, building_ids)
        self.assertIn(self.destination.pk, building_ids)
        self.assertIsNone(form.fields["building"].empty_label)

    def test_building_dropdown_includes_office_for_backoffice(self):
        form = WorkOrderForm(user=self.backoffice)
        building_ids = set(form.fields["building"].queryset.values_list("id", flat=True))
        self.assertIn(self.office.pk, building_ids)
        self.assertIn(self.destination.pk, building_ids)

    def test_building_dropdown_translates_office_label_in_bulgarian(self):
        with override("bg"):
            form = WorkOrderForm(user=self.admin)
            office_label = form.fields["building"].label_from_instance(self.office)
        self.assertEqual(office_label, "Офис")

    def test_building_dropdown_excludes_office_for_technician(self):
        form = WorkOrderForm(user=self.technician)
        building_ids = set(form.fields["building"].queryset.values_list("id", flat=True))
        self.assertNotIn(self.office.pk, building_ids)
        self.assertIn(self.destination.pk, building_ids)

    def test_office_employee_field_lists_only_backoffice_users(self):
        non_backoffice = get_user_model().objects.create_user(username="no-backoffice", password="pass")
        form = WorkOrderForm(user=self.admin, building=self.office)
        employee_ids = set(form.fields["office_employee"].queryset.values_list("pk", flat=True))
        self.assertIn(self.backoffice.pk, employee_ids)
        self.assertNotIn(non_backoffice.pk, employee_ids)
        self.assertTrue(form.show_office_employee)

    def test_office_employee_required_for_office_building(self):
        form = WorkOrderForm(
            data=self._base_form_data(
                office_employee="",
                forwarded_to_building=str(self.destination.pk),
            ),
            user=self.admin,
            building=self.office,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("office_employee", form.errors)

    def test_lawyer_only_create_requires_unit_field(self):
        form = WorkOrderForm(
            user=self.admin,
            force_lawyer_only=True,
        )
        self.assertTrue(form.fields["unit"].required)

    def test_lawyer_only_create_without_unit_is_invalid(self):
        form = WorkOrderForm(
            data=self._base_form_data(),
            user=self.admin,
            building=self.destination,
            force_lawyer_only=True,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("unit", form.errors)

    def test_lawyer_only_create_with_unit_is_valid(self):
        destination_unit = Unit.objects.create(building=self.destination, number="A-101")
        form = WorkOrderForm(
            data=self._base_form_data(unit=str(destination_unit.pk)),
            user=self.admin,
            building=self.destination,
            force_lawyer_only=True,
        )
        self.assertTrue(form.is_valid(), form.errors)
        order = form.save()
        self.assertEqual(order.unit_id, destination_unit.pk)
        self.assertTrue(order.lawyer_only)

    def test_existing_forward_target_cannot_be_cleared(self):
        order = WorkOrder.objects.create(
            building=self.office,
            title="Leak",
            deadline=timezone.localdate(),
            forwarded_to_building=self.destination,
            forwarded_by=self.admin,
        )
        form = WorkOrderForm(
            data=self._base_form_data(
                forwarded_to_building="",
                building=str(self.office.pk),
            ),
            user=self.admin,
            building=self.office,
            instance=order,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("forwarded_to_building", form.errors)
        self.assertIn("must stay assigned", form.errors["forwarded_to_building"][0])

    def test_edit_forwarded_order_keeps_existing_forwarding_metadata(self):
        technician = get_user_model().objects.create_user(username="dest-tech-form", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = WorkOrder.objects.create(
            building=self.office,
            title="Forwarded leak",
            deadline=timezone.localdate(),
            forwarded_to_building=self.destination,
            forwarded_by=self.admin,
            forward_note="Keep destination",
        )
        form = WorkOrderForm(
            data={
                "title": order.title,
                "building": str(self.office.pk),
                "priority": order.priority,
                "status": order.status,
                "deadline": order.deadline.isoformat(),
                "description": order.description,
                "replacement_request_note": "",
            },
            user=technician,
            building=self.office,
            instance=order,
        )
        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()
        self.assertEqual(updated.forwarded_to_building_id, self.destination.pk)
        self.assertEqual(updated.forwarded_by_id, self.admin.pk)


class WorkOrderForwardingAuditLogTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.factory = RequestFactory()
        self.admin = User.objects.create_user(username="admin", password="pass", is_superuser=True)
        self.office_owner = User.objects.create_user(username="office-owner", password="pass")
        self.dest_owner = User.objects.create_user(username="dest-owner", password="pass")
        self.office = Building.objects.create(owner=self.office_owner, name="Office", is_system_default=True)
        self.destination = Building.objects.create(owner=self.dest_owner, name="Destination")

    def _request(self):
        request = self.factory.post("/work-orders/1/edit")
        request.user = self.admin
        return request

    def _form_stub(self, *, changed: bool, previous_id):
        return SimpleNamespace(
            forwarding_changed=changed,
            forwarding_previous_id=previous_id,
        )

    def test_audit_log_written_when_forwarded(self):
        order = WorkOrder.objects.create(
            building=self.office,
            title="Escalation",
            deadline=timezone.localdate(),
        )
        order.forwarded_to_building = self.destination
        order.forwarded_by = self.admin
        order.forward_note = "Handle ASAP"
        order.save()

        _maybe_handle_forwarding_change(
            self._request(),
            self._form_stub(changed=True, previous_id=None),
            order,
        )
        log_entry = WorkOrderAuditLog.objects.latest("pk")
        self.assertEqual(log_entry.action, WorkOrderAuditLog.Action.REASSIGNED)
        self.assertEqual(log_entry.payload.get("target_id"), self.destination.pk)
        self.assertFalse(log_entry.payload.get("cleared"))

    def test_audit_log_written_when_forward_target_cleared(self):
        order = WorkOrder.objects.create(
            building=self.office,
            title="Escalation",
            deadline=timezone.localdate(),
            forwarded_to_building=self.destination,
            forwarded_by=self.admin,
        )
        order.forward_note = "original"
        order.save()

        order.forwarded_to_building = None
        order.forwarded_by = None
        order.forward_note = ""
        order.forwarded_at = None
        order.save()

        _maybe_handle_forwarding_change(
            self._request(),
            self._form_stub(changed=True, previous_id=self.destination.pk),
            order,
        )
        log_entry = WorkOrderAuditLog.objects.latest("pk")
        self.assertEqual(log_entry.action, WorkOrderAuditLog.Action.REASSIGNED)
        self.assertTrue(log_entry.payload.get("cleared"))
        self.assertEqual(log_entry.payload.get("previous_target_id"), self.destination.pk)
        self.assertIsNone(log_entry.payload.get("target_id"))


class LawyerWorkOrdersPageTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.client = Client()
        self.lawyer = User.objects.create_user(username="lawyer-ui", password="pass")
        self.admin = User.objects.create_user(username="admin-ui", password="pass")
        BuildingMembership.objects.create(
            user=self.lawyer,
            building=None,
            role=MembershipRole.LAWYER,
        )
        BuildingMembership.objects.create(
            user=self.admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )

    def test_lawyer_page_shows_add_new_lawyer_order_option(self):
        self.client.force_login(self.lawyer)
        response = self.client.get(reverse("core:lawyer_work_orders"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add new lawyer order")

    def test_admin_page_does_not_show_add_new_lawyer_order_option(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("core:lawyer_work_orders"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Add new lawyer order")
