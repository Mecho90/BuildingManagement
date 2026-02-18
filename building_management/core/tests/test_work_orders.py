from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from core.forms import WorkOrderForm
from core.models import Building, WorkOrder, WorkOrderAuditLog
from core.views.work_orders import _maybe_handle_forwarding_change


class WorkOrderFormForwardingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="admin", password="pass", is_superuser=True)
        self.office = Building.objects.create(owner=self.admin, name="Office", is_system_default=True)
        self.destination_owner = User.objects.create_user(username="dest-owner", password="pass")
        self.destination = Building.objects.create(owner=self.destination_owner, name="Destination")

    def _base_form_data(self, **overrides):
        data = {
            "title": "Leaky pipe",
            "priority": WorkOrder.Priority.MEDIUM,
            "status": WorkOrder.Status.OPEN,
            "deadline": timezone.localdate().isoformat(),
            "description": "",
            "replacement_request_note": "",
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
