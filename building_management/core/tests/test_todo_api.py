from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import BuildingMembership, MembershipRole, TodoItem, start_of_week


class TodoApiTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.user = self.User.objects.create_user(username="member", password="pass1234")
        self.url = reverse("core:api_todos")
        self.clear_url = reverse("core:api_todo_completed_clear")
        self.client.force_login(self.user)

    def _post(self, payload: dict) -> dict:
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        return {"status": response.status_code, "body": response.json()}

    def _patch(self, item: TodoItem, payload: dict):
        url = reverse("core:api_todo_detail", args=[item.pk])
        return self.client.patch(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _delete_completed(self, query=""):
        target = self.clear_url
        if query:
            target = f"{target}?{query}"
        return self.client.delete(target)

    def _make_admin(self, username="admin-user"):
        admin = self.User.objects.create_user(username=username, password="pass1234")
        BuildingMembership.objects.create(
            user=admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )
        return admin

    def _make_backoffice(self, username="backoffice-user"):
        user = self.User.objects.create_user(username=username, password="pass1234")
        BuildingMembership.objects.create(
            user=user,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )
        return user

    def _make_technician(self, username="tech-user"):
        user = self.User.objects.create_user(username=username, password="pass1234")
        BuildingMembership.objects.create(
            user=user,
            building=None,
            role=MembershipRole.TECHNICIAN,
        )
        return user

    def _make_lawyer(self, username="lawyer-user"):
        user = self.User.objects.create_user(username=username, password="pass1234")
        BuildingMembership.objects.create(
            user=user,
            building=None,
            role=MembershipRole.LAWYER,
        )
        return user

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

    def test_backoffice_created_only_all_owners(self):
        backoffice = self._make_backoffice("backoffice-all")
        technician = self._make_technician("owner-tech")
        lawyer = self._make_lawyer("owner-lawyer")
        other = self.User.objects.create_user(username="owner-five", password="pass1234")
        today = timezone.localdate()
        current_week = start_of_week(today)
        last_week = current_week - timedelta(days=7)
        TodoItem.objects.create(user=backoffice, title="Mine task", due_date=last_week, week_start=last_week)
        TodoItem.objects.create(user=technician, title="Tech task", due_date=last_week, week_start=last_week)
        TodoItem.objects.create(user=lawyer, title="Lawyer task", due_date=last_week, week_start=last_week)
        TodoItem.objects.create(user=other, title="тст3", due_date=last_week, week_start=last_week)
        TodoItem.objects.create(user=backoffice, title="Current task", due_date=today, week_start=current_week)
        self.client.force_login(backoffice)

        response = self.client.get(f"{self.url}?created_only=1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        titles = {item["title"] for item in payload["results"]}
        self.assertEqual(titles, {"Mine task", "Tech task", "Lawyer task", "Current task"})

    def test_technician_sees_only_own_todos(self):
        technician = self._make_technician("tech-only")
        backoffice = self._make_backoffice("backoffice-other")
        today = timezone.localdate()
        TodoItem.objects.create(user=technician, title="Mine", due_date=today, week_start=today)
        TodoItem.objects.create(user=backoffice, title="Other", due_date=today, week_start=today)
        self.client.force_login(technician)

        response = self.client.get(f"{self.url}?owner=all")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        titles = {item["title"] for item in payload["results"]}
        self.assertEqual(titles, {"Mine"})

    def test_lawyer_sees_only_own_todos(self):
        lawyer = self._make_lawyer("lawyer-only")
        technician = self._make_technician("tech-other")
        today = timezone.localdate()
        TodoItem.objects.create(user=lawyer, title="Mine", due_date=today, week_start=today)
        TodoItem.objects.create(user=technician, title="Other", due_date=today, week_start=today)
        self.client.force_login(lawyer)

        response = self.client.get(f"{self.url}?owner=all")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        titles = {item["title"] for item in payload["results"]}
        self.assertEqual(titles, {"Mine"})

    def test_created_only_lists_open_tasks_by_newest(self):
        today = timezone.localdate()
        current_week = start_of_week(today)
        last_week = current_week - timedelta(days=7)
        older_week = current_week - timedelta(days=14)
        older = TodoItem.objects.create(
            user=self.user,
            title="Old open",
            status=TodoItem.Status.PENDING,
            due_date=older_week,
            week_start=older_week,
        )
        newer = TodoItem.objects.create(
            user=self.user,
            title="New open",
            status=TodoItem.Status.IN_PROGRESS,
            due_date=last_week,
            week_start=last_week,
        )
        TodoItem.objects.create(
            user=self.user,
            title="Done task",
            status=TodoItem.Status.DONE,
            due_date=today,
            week_start=current_week,
        )
        current_pending = TodoItem.objects.create(
            user=self.user,
            title="Current pending",
            status=TodoItem.Status.PENDING,
            due_date=today,
            week_start=current_week,
        )

        now = timezone.now()
        TodoItem.objects.filter(pk=older.pk).update(created_at=now - timedelta(days=2))
        TodoItem.objects.filter(pk=newer.pk).update(created_at=now - timedelta(hours=1))

        response = self.client.get(f"{self.url}?created_only=1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        titles = [item["title"] for item in payload["results"]]
        self.assertEqual(titles, ["Current pending", "New open", "Old open"])
        self.assertEqual(payload["count"], 3)
        for item in payload["results"]:
            self.assertIn(item["status"], {TodoItem.Status.PENDING, TodoItem.Status.IN_PROGRESS})

    def test_update_due_date_in_past_rejected(self):
        today = timezone.localdate()
        item = TodoItem.objects.create(
            user=self.user,
            title="Past blocker",
            due_date=today,
            week_start=start_of_week(today),
        )
        yesterday = today - timedelta(days=1)

        response = self._patch(item, {"due_date": yesterday.isoformat()})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Due date cannot be in the past.", response.json()["error"])

    def test_update_week_start_in_past_rejected(self):
        today = timezone.localdate()
        item = TodoItem.objects.create(
            user=self.user,
            title="Week blocker",
            due_date=today,
            week_start=start_of_week(today),
        )
        last_week = start_of_week(today) - timedelta(days=7)

        response = self._patch(item, {"week_start": last_week.isoformat()})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Week start cannot be in the past.", response.json()["error"])

    def test_completed_clear_only_deletes_self_by_default(self):
        admin = self._make_admin("admin-clear")
        other = self.User.objects.create_user(username="other-clear", password="pass1234")
        today = timezone.localdate()
        week = start_of_week(today)
        TodoItem.objects.create(
            user=admin,
            title="Admin done",
            status=TodoItem.Status.DONE,
            due_date=today,
            week_start=week,
        )
        TodoItem.objects.create(
            user=other,
            title="Other done",
            status=TodoItem.Status.DONE,
            due_date=today,
            week_start=week,
        )
        self.client.force_login(admin)

        response = self._delete_completed()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 1)
        self.assertTrue(
            TodoItem.objects.filter(user=other, status=TodoItem.Status.DONE).exists()
        )

    def test_regular_user_cannot_clear_other_owner(self):
        other = self.User.objects.create_user(username="other-block", password="pass1234")
        today = timezone.localdate()
        TodoItem.objects.create(
            user=other,
            title="Other done",
            status=TodoItem.Status.DONE,
            due_date=today,
            week_start=start_of_week(today),
        )

        response = self._delete_completed(f"owner={other.pk}")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            TodoItem.objects.filter(user=other, status=TodoItem.Status.DONE).exists()
        )

    def test_admin_can_clear_other_owner_with_param(self):
        admin = self._make_admin("admin-bulk-clear")
        other = self.User.objects.create_user(username="other-bulk", password="pass1234")
        today = timezone.localdate()
        for idx in range(2):
            TodoItem.objects.create(
                user=other,
                title=f"Other done {idx}",
                status=TodoItem.Status.DONE,
                due_date=today,
                week_start=start_of_week(today),
            )
        self.client.force_login(admin)

        response = self._delete_completed(f"owner={other.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 2)
        self.assertFalse(
            TodoItem.objects.filter(user=other, status=TodoItem.Status.DONE).exists()
        )
