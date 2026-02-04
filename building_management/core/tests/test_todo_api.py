from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import BuildingMembership, MembershipRole, TodoItem


class TodoApiTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.user = self.User.objects.create_user(username="member", password="pass1234")
        self.url = reverse("core:api_todos")
        self.client.force_login(self.user)

    def _post(self, payload: dict) -> dict:
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        return {"status": response.status_code, "body": response.json()}

    def _make_admin(self, username="admin-user"):
        admin = self.User.objects.create_user(username=username, password="pass1234")
        BuildingMembership.objects.create(
            user=admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )
        return admin

    def test_due_date_in_past_rejected(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        result = self._post(
            {
                "title": "Past task",
                "status": "pending",
                "due_date": yesterday.isoformat(),
            }
        )

        self.assertEqual(result["status"], 400)
        self.assertIn("past", result["body"]["error"])

    def test_week_start_in_past_rejected(self):
        last_week = timezone.localdate() - timedelta(days=7)
        result = self._post(
            {
                "title": "Week start test",
                "status": "pending",
                "due_date": None,
                "week_start": last_week.isoformat(),
            }
        )

        self.assertEqual(result["status"], 400)
        self.assertIn("past", result["body"]["error"])

    def test_list_supports_pagination(self):
        today = timezone.localdate()
        for idx in range(30):
            TodoItem.objects.create(
                user=self.user,
                title=f"Task {idx}",
                due_date=today + timedelta(days=idx),
                week_start=today,
            )

        response = self.client.get(f"{self.url}?per=5&page=2")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["results"]), 5)
        self.assertEqual(payload["count"], 30)
        self.assertEqual(payload["pagination"]["page"], 2)
        self.assertEqual(payload["pagination"]["per"], 5)
        self.assertTrue(payload["pagination"]["has_previous"])
        self.assertTrue(payload["pagination"]["has_next"])

    def test_list_search_by_title(self):
        today = timezone.localdate()
        TodoItem.objects.create(user=self.user, title="Alpha task", due_date=today, week_start=today)
        TodoItem.objects.create(user=self.user, title="Beta work", due_date=today, week_start=today)

        response = self.client.get(f"{self.url}?q=beta")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["title"], "Beta work")

    def test_list_search_matches_description(self):
        today = timezone.localdate()
        TodoItem.objects.create(user=self.user, title="Alpha task", description="Check generator", due_date=today, week_start=today)
        TodoItem.objects.create(user=self.user, title="Beta task", description="Paint hallway", due_date=today, week_start=today)

        response = self.client.get(f"{self.url}?q=paint")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["title"], "Beta task")

    def test_admin_must_select_owner(self):
        admin = self._make_admin("admin-a")
        self.client.force_login(admin)
        result = self._post({"title": "Needs owner", "status": "pending"})
        self.assertEqual(result["status"], 400)
        self.assertIn("owner", result["body"]["error"].lower())

    def test_admin_cannot_assign_self(self):
        admin = self._make_admin("admin-b")
        self.client.force_login(admin)
        payload = {"title": "Self assign", "status": "pending", "owner": admin.pk}
        result = self._post(payload)
        self.assertEqual(result["status"], 400)
        self.assertIn("select a different owner", result["body"]["error"].lower())

    def test_admin_can_assign_other_owner(self):
        admin = self._make_admin("admin-c")
        technician = self.User.objects.create_user(username="tech-user", password="pass1234")
        self.client.force_login(admin)
        payload = {"title": "Delegate task", "status": "pending", "owner": technician.pk}
        result = self._post(payload)
        self.assertEqual(result["status"], 201)
        self.assertEqual(result["body"]["owner"]["id"], technician.pk)

    def test_admin_list_defaults_to_self(self):
        admin = self._make_admin("admin-default")
        other = self.User.objects.create_user(username="owner-two", password="pass1234")
        today = timezone.localdate()
        TodoItem.objects.create(user=admin, title="Mine", due_date=today, week_start=today)
        TodoItem.objects.create(user=other, title="Theirs", due_date=today, week_start=today)
        self.client.force_login(admin)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        titles = {item["title"] for item in payload["results"]}
        self.assertEqual(titles, {"Mine"})

    def test_admin_owner_filter_parameter(self):
        admin = self._make_admin("admin-filter")
        other = self.User.objects.create_user(username="owner-three", password="pass1234")
        today = timezone.localdate()
        TodoItem.objects.create(user=admin, title="Admin task", due_date=today, week_start=today)
        TodoItem.objects.create(user=other, title="Other task", due_date=today, week_start=today)
        self.client.force_login(admin)

        response = self.client.get(f"{self.url}?owner={other.pk}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        titles = {item["title"] for item in payload["results"]}
        self.assertEqual(titles, {"Other task"})

    def test_admin_owner_filter_all(self):
        admin = self._make_admin("admin-all")
        other = self.User.objects.create_user(username="owner-four", password="pass1234")
        today = timezone.localdate()
        TodoItem.objects.create(user=admin, title="Alpha", due_date=today, week_start=today)
        TodoItem.objects.create(user=other, title="Beta", due_date=today, week_start=today)
        self.client.force_login(admin)

        response = self.client.get(f"{self.url}?owner=all")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        titles = {item["title"] for item in payload["results"]}
        self.assertEqual(titles, {"Alpha", "Beta"})
