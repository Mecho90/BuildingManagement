from __future__ import annotations

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Building, WorkOrder, WorkOrderAttachment

UserModel = get_user_model()


@override_settings(
    WORK_ORDER_ATTACHMENT_ALLOWED_TYPES=("image/png", "application/pdf"),
    WORK_ORDER_ATTACHMENT_ALLOWED_PREFIXES=("image/",),
)
class WorkOrderAttachmentAPITests(TestCase):
    def setUp(self):
        self._media_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._media_dir, ignore_errors=True))
        media_override = override_settings(
            MEDIA_ROOT=self._media_dir,
            DEFAULT_FILE_STORAGE="django.core.files.storage.FileSystemStorage",
        )
        media_override.enable()
        self.addCleanup(media_override.disable)

        self.owner = UserModel.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="pass1234",
        )
        self.other_user = UserModel.objects.create_user(
            username="viewer",
            email="viewer@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.owner,
            name="Upload Plaza",
            address="123 Attachment St",
        )
        self.work_order = WorkOrder.objects.create(
            building=self.building,
            title="Fix elevators",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.MEDIUM,
            deadline="2030-01-01",
        )

        self.attachments_url = reverse("api_workorder_attachments", args=[self.work_order.pk])

    def test_requires_authentication(self):
        response = self.client.get(self.attachments_url)
        self.assertEqual(response.status_code, 404)

    def test_list_attachments_returns_metadata(self):
        self.client.login(username="owner", password="pass1234")
        file_data = SimpleUploadedFile(
            "photo.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 10,
            content_type="image/png",
        )
        attachment = WorkOrderAttachment.objects.create(
            work_order=self.work_order,
            file=file_data,
            original_name="photo.png",
            content_type="image/png",
            size=file_data.size,
        )

        response = self.client.get(self.attachments_url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("attachments", payload)
        self.assertEqual(len(payload["attachments"]), 1)
        item = payload["attachments"][0]
        self.assertEqual(item["id"], attachment.pk)
        self.assertEqual(item["name"], "photo.png")
        self.assertEqual(item["content_type"], "image/png")
        self.assertTrue(item["url"])
        self.assertTrue(item["created_at"])

    def test_upload_creates_attachment(self):
        self.client.login(username="owner", password="pass1234")
        file_data = SimpleUploadedFile(
            "new.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 20,
            content_type="image/png",
        )

        response = self.client.post(self.attachments_url, {"files": file_data})
        self.assertEqual(response.status_code, 201)

        payload = response.json()
        self.assertIn("attachments", payload)
        self.assertEqual(len(payload["attachments"]), 1)

        attachment = WorkOrderAttachment.objects.get(work_order=self.work_order)
        self.assertEqual(attachment.original_name, "new.png")
        self.assertEqual(attachment.content_type, "image/png")

    def test_upload_rejects_invalid_type(self):
        self.client.login(username="owner", password="pass1234")
        file_data = SimpleUploadedFile(
            "bad.exe",
            b"MZ" + b"\x00" * 10,
            content_type="application/octet-stream",
        )

        response = self.client.post(self.attachments_url, {"files": file_data})
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertIn("errors", payload)
        self.assertEqual(WorkOrderAttachment.objects.count(), 0)

    def test_delete_attachment(self):
        self.client.login(username="owner", password="pass1234")
        file_data = SimpleUploadedFile(
            "delete.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 15,
            content_type="image/png",
        )
        attachment = WorkOrderAttachment.objects.create(
            work_order=self.work_order,
            file=file_data,
            original_name="delete.png",
            content_type="image/png",
            size=file_data.size,
        )
        url = reverse("api_workorder_attachment_detail", args=[self.work_order.pk, attachment.pk])

        response = self.client.delete(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            WorkOrderAttachment.objects.filter(pk=attachment.pk).exists()
        )

    def test_upload_forbidden_for_non_owner(self):
        self.client.login(username="viewer", password="pass1234")
        file_data = SimpleUploadedFile(
            "blocked.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 10,
            content_type="image/png",
        )

        response = self.client.post(self.attachments_url, {"files": file_data})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(WorkOrderAttachment.objects.count(), 0)
