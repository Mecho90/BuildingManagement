from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.forms import BuildingForm
from core.models import Building, BuildingMembership, MembershipRole


class BuildingFormTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.client = Client()
        self.admin = User.objects.create_user(
            username="building-admin",
            password="pass1234",
            is_superuser=True,
            is_staff=True,
        )
        self.technician = User.objects.create_user(username="building-tech", password="pass1234")
        BuildingMembership.objects.create(
            user=self.technician,
            building=None,
            role=MembershipRole.TECHNICIAN,
        )

    def test_create_form_shows_role_field_and_expected_order(self):
        form = BuildingForm(user=self.admin)

        self.assertIn("owner", form.fields)
        self.assertIn("role", form.fields)
        self.assertIsNone(form.fields["owner"].empty_label)
        self.assertFalse(isinstance(form.fields["role"].widget, forms.HiddenInput))
        self.assertEqual(
            list(form.fields.keys()),
            ["owner", "role", "name", "address", "description"],
        )

    def test_create_form_allows_role_selection_for_technician_owner(self):
        form = BuildingForm(
            data={
                "owner": str(self.technician.pk),
                "role": Building.Role.PROPERTY_MANAGER,
                "name": "New Building",
                "address": "Main Street",
                "description": "Description",
            },
            user=self.admin,
        )

        self.assertFalse(form.fields["role"].disabled)
        self.assertTrue(form.is_valid(), form.errors)
        building = form.save()
        self.assertEqual(building.role, Building.Role.PROPERTY_MANAGER)

    def test_create_page_renders_role_after_owner_before_name_and_address(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("core:building_create"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        owner_idx = content.find('name="owner"')
        role_idx = content.find('name="role"')
        name_idx = content.find('name="name"')
        address_idx = content.find('name="address"')
        description_idx = content.find('name="description"')

        self.assertGreaterEqual(owner_idx, 0)
        self.assertGreaterEqual(role_idx, 0)
        self.assertGreaterEqual(name_idx, 0)
        self.assertGreaterEqual(address_idx, 0)
        self.assertGreaterEqual(description_idx, 0)
        self.assertLess(owner_idx, role_idx)
        self.assertLess(role_idx, name_idx)
        self.assertLess(name_idx, address_idx)
        self.assertLess(address_idx, description_idx)
