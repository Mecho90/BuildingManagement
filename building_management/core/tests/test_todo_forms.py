from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.forms import TodoItemForm
from core.models import BuildingMembership, MembershipRole, TodoItem


class TodoItemFormTests(TestCase):
    def setUp(self):
        self.User = get_user_model()

    def test_regular_user_defaults_to_self(self):
        owner = self.User.objects.create_user(username="regular", password="pass1234")
        form = TodoItemForm(
            data={
                "title": "Inspect lobby lights",
                "status": TodoItem.Status.PENDING,
                "due_date": "2026-02-05",
                "description": "",
            },
            user=owner,
        )

        self.assertNotIn("owner", form.fields)
        self.assertTrue(form.is_valid(), form.errors)

        todo = form.save()
        self.assertEqual(todo.user, owner)

    def test_admin_can_assign_owner(self):
        admin = self.User.objects.create_user(username="admin-user", password="pass1234")
        BuildingMembership.objects.create(
            user=admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )
        technician = self.User.objects.create_user(username="tech-user", password="pass1234")

        form = TodoItemForm(
            data={
                "title": "Replace hallway light",
                "status": TodoItem.Status.IN_PROGRESS,
                "due_date": "2026-02-06",
                "description": "",
                "owner": str(technician.pk),
            },
            user=admin,
        )

        self.assertIn("owner", form.fields)
        self.assertTrue(form.is_valid(), form.errors)

        todo = form.save()
        self.assertEqual(todo.user, technician)

    def test_admin_must_choose_owner_explicitly(self):
        admin = self.User.objects.create_user(username="admin-two", password="pass1234")
        BuildingMembership.objects.create(
            user=admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )
        form = TodoItemForm(
            data={
                "title": "Needs owner",
                "status": TodoItem.Status.PENDING,
                "due_date": (timezone.localdate() + timedelta(days=1)).isoformat(),
                "description": "",
            },
            user=admin,
        )

        self.assertIn("owner", form.fields)
        self.assertFalse(form.is_valid())
        self.assertIn("owner", form.errors)

    def test_form_rejects_past_due_date(self):
        user = self.User.objects.create_user(username="blocked", password="pass1234")
        yesterday = timezone.localdate() - timedelta(days=1)
        form = TodoItemForm(
            data={
                "title": "Old task",
                "status": TodoItem.Status.PENDING,
                "due_date": yesterday.isoformat(),
                "description": "",
            },
            user=user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("due_date", form.errors)

    def test_admin_owner_queryset_excludes_self_on_create(self):
        admin = self.User.objects.create_user(username="manager", password="pass1234")
        BuildingMembership.objects.create(user=admin, building=None, role=MembershipRole.ADMINISTRATOR)
        technician = self.User.objects.create_user(username="tech-a", password="pass1234")

        form = TodoItemForm(user=admin)
        owner_field = form.fields.get("owner")
        self.assertIsNotNone(owner_field)
        self.assertFalse(owner_field.queryset.filter(pk=admin.pk).exists())
        self.assertTrue(owner_field.queryset.filter(pk=technician.pk).exists())

    def test_admin_editing_task_can_pick_self(self):
        admin = self.User.objects.create_user(username="manager2", password="pass1234")
        BuildingMembership.objects.create(user=admin, building=None, role=MembershipRole.ADMINISTRATOR)
        task = TodoItem.objects.create(
            user=admin,
            title="Self task",
            status=TodoItem.Status.PENDING,
            week_start=timezone.localdate(),
            due_date=timezone.localdate(),
        )

        form = TodoItemForm(instance=task, user=admin)
        owner_field = form.fields.get("owner")
        self.assertIsNotNone(owner_field)
        self.assertTrue(owner_field.queryset.filter(pk=admin.pk).exists())
