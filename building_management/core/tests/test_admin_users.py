from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Building


class AdminUserListOwnerFilterTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.admin = self.User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="pass",
        )

    def test_filter_by_owner_limits_query(self):
        owner_one = self.User.objects.create_user(
            username="owner1",
            email="owner1@example.com",
            password="pass",
        )
        owner_two = self.User.objects.create_user(
            username="owner2",
            email="owner2@example.com",
            password="pass",
        )
        Building.objects.create(name="Building One", owner=owner_one)
        Building.objects.create(name="Building Two", owner=owner_two)

        self.client.force_login(self.admin)
        url = reverse("core:users_list")
        response = self.client.get(url, {"owner": str(owner_one.pk)})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["owner_filter"], str(owner_one.pk))
        owner_options = response.context["owner_options"]
        self.assertTrue(any(opt["value"] == str(owner_one.pk) for opt in owner_options))
        self.assertContains(response, "owner1")
        self.assertNotContains(response, "owner2")
