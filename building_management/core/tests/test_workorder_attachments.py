from __future__ import annotations

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Building,
    BuildingMembership,
    Capability,
    MembershipRole,
    WorkOrder,
    WorkOrderAttachment,
    WorkOrderAuditLog,
)


class WorkOrderAttachmentTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)

        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pass1234")
        self.allowed = User.objects.create_user(username="allowed", password="pass1234")
        self.viewer = User.objects.create_user(username="viewer", password="pass1234")

        self.building = Building.objects.create(
            owner=self.owner,
            name="Tower",
            address="1 Main",
        )
        BuildingMembership.objects.create(
            user=self.allowed,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        BuildingMembership.objects.create(
            user=self.viewer,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
            capabilities_override={"remove": [Capability.CREATE_WORK_ORDERS, Capability.MANAGE_BUILDINGS]},
        )
        self.work_order = WorkOrder.objects.create(
            building=self.building,
            title="Fix pump",
            deadline=timezone.localdate(),
        )

    def _upload_file(self, name="report.txt", content=b"content"):
        return SimpleUploadedFile(name, content, content_type="text/plain")

    def test_api_upload_requires_capability(self):
        self.client.force_login(self.viewer)
        url = reverse("core:api_workorder_attachments", args=[self.work_order.pk])
        response = self.client.post(url, {"files": self._upload_file()})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(WorkOrderAttachment.objects.count(), 0)

    def test_api_upload_logs_audit_for_authorized_user(self):
        self.client.force_login(self.allowed)
        url = reverse("core:api_workorder_attachments", args=[self.work_order.pk])
        response = self.client.post(url, {"files": self._upload_file("notes.txt")})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(WorkOrderAttachment.objects.filter(work_order=self.work_order).count(), 1)

        log_entry = WorkOrderAuditLog.objects.latest("pk")
        self.assertEqual(log_entry.action, WorkOrderAuditLog.Action.ATTACHMENTS)
        self.assertEqual(log_entry.actor, self.allowed)
        payload = log_entry.payload.get("attachments")
        self.assertEqual(payload["added"], ["notes.txt"])
        self.assertEqual(payload["removed"], [])

    def test_api_delete_logs_audit(self):
        attachment = WorkOrderAttachment.objects.create(
            work_order=self.work_order,
            file=self._upload_file("remove.txt", b"delete"),
            original_name="remove.txt",
        )
        self.client.force_login(self.allowed)
        url = reverse(
            "core:api_workorder_attachment_detail",
            args=[self.work_order.pk, attachment.pk],
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WorkOrderAttachment.objects.filter(pk=attachment.pk).exists())

        log_entry = WorkOrderAuditLog.objects.latest("pk")
        self.assertEqual(log_entry.action, WorkOrderAuditLog.Action.ATTACHMENTS)
        payload = log_entry.payload.get("attachments")
        self.assertEqual(payload["added"], [])
        self.assertEqual(payload["removed"], ["remove.txt"])

    def test_html_delete_view_enforces_capabilities(self):
        attachment = WorkOrderAttachment.objects.create(
            work_order=self.work_order,
            file=self._upload_file("panel.txt", b"panel"),
            original_name="panel.txt",
        )
        url = reverse(
            "core:workorder_attachment_delete",
            args=[self.work_order.pk, attachment.pk],
        )

        # Viewer lacks manage/create capability and cannot delete.
        self.client.force_login(self.viewer)
        response = self.client.post(url)
        self.assertNotEqual(response.status_code, 200)
        self.assertTrue(WorkOrderAttachment.objects.filter(pk=attachment.pk).exists())

        # Authorized member can delete and generates audit log.
        self.client.force_login(self.allowed)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(WorkOrderAttachment.objects.filter(pk=attachment.pk).exists())
        log_entry = WorkOrderAuditLog.objects.latest("pk")
        payload = log_entry.payload.get("attachments")
        self.assertEqual(payload["removed"], ["panel.txt"])
