from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.authz import CapabilityResolver
from core.models import (
    BudgetFeatureFlag,
    Building,
    BuildingMembership,
    MembershipRole,
    WorkOrder,
)


class WorkOrderVisibilityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.client = Client()
        self.office_owner = User.objects.create_user(username="office-admin", password="pass", is_superuser=True)
        self.destination_owner = User.objects.create_user(username="destination-owner", password="pass")
        self.backoffice_user = User.objects.create_user(username="global-backoffice", password="pass")
        BuildingMembership.objects.create(user=self.backoffice_user, building=None, role=MembershipRole.BACKOFFICE)
        self.office = Building.objects.create(owner=self.office_owner, name="Office", is_system_default=True)
        self.destination = Building.objects.create(owner=self.destination_owner, name="Destination")
        BudgetFeatureFlag.objects.create(key="budgets", is_enabled=True)

    def _create_forwarded_order(self, **overrides):
        data = {
            "building": self.office,
            "title": overrides.get("title", "Office Request"),
            "deadline": overrides.get("deadline", timezone.localdate()),
            "forwarded_to_building": overrides.get("forwarded_to_building", self.destination),
            "forwarded_by": overrides.get("forwarded_by", self.office_owner),
            "forward_note": overrides.get("forward_note", ""),
            "lawyer_only": overrides.get("lawyer_only", False),
        }
        data.update({k: v for k, v in overrides.items() if k not in data})
        return WorkOrder.objects.create(**data)

    def test_destination_owner_can_see_forwarded_office_orders(self):
        order = self._create_forwarded_order()
        resolver = CapabilityResolver(self.destination_owner)
        self.assertIn(self.destination.pk, resolver.visible_building_ids())
        qs = WorkOrder.objects.visible_to(self.destination_owner)
        self.assertIn(order.pk, qs.values_list("pk", flat=True))

    def test_global_backoffice_sees_all_forwarded_orders(self):
        order = self._create_forwarded_order(title="Escalation")
        qs = WorkOrder.objects.visible_to(self.backoffice_user)
        self.assertIn(order.pk, qs.values_list("pk", flat=True))

    def test_confidential_forwarded_order_hidden_from_destination_technician(self):
        User = get_user_model()
        technician = User.objects.create_user(username="dest-tech", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = self._create_forwarded_order(lawyer_only=True, title="Confidential")
        qs = WorkOrder.objects.visible_to(technician)
        self.assertNotIn(order.pk, qs.values_list("pk", flat=True))

    def test_destination_technician_sees_forwarded_non_confidential_order(self):
        User = get_user_model()
        technician = User.objects.create_user(username="dest-tech-open", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = self._create_forwarded_order(lawyer_only=False, title="Forwarded for technician")
        qs = WorkOrder.objects.visible_to(technician)
        self.assertIn(order.pk, qs.values_list("pk", flat=True))

    def test_destination_technician_can_edit_forwarded_office_order_before_review(self):
        User = get_user_model()
        technician = User.objects.create_user(username="dest-tech-edit", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = self._create_forwarded_order(lawyer_only=False, title="Forwarded editable")
        self.client.force_login(technician)
        response = self.client.get(reverse("core:work_order_update", args=[order.pk]))
        self.assertEqual(response.status_code, 200)

    def test_destination_technician_cannot_delete_forwarded_office_order(self):
        User = get_user_model()
        technician = User.objects.create_user(username="dest-tech-delete", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = self._create_forwarded_order(lawyer_only=False, title="Forwarded undeletable")
        self.client.force_login(technician)
        response = self.client.get(reverse("core:work_order_delete", args=[order.pk]))
        self.assertEqual(response.status_code, 403)

    def test_destination_technician_can_add_expense_forwarded_office_order_before_review(self):
        User = get_user_model()
        technician = User.objects.create_user(username="dest-tech-expense", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = self._create_forwarded_order(lawyer_only=False, title="Forwarded no expense")
        self.client.force_login(technician)
        response = self.client.get(reverse("core:work_order_budget_charge", args=[order.pk]))
        self.assertEqual(response.status_code, 200)

    def test_destination_technician_cannot_edit_or_add_expense_after_sending_to_backoffice(self):
        User = get_user_model()
        technician = User.objects.create_user(username="dest-tech-awaiting", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = self._create_forwarded_order(
            lawyer_only=False,
            title="Forwarded awaiting review",
            status=WorkOrder.Status.AWAITING_APPROVAL,
        )
        self.client.force_login(technician)
        edit_response = self.client.get(reverse("core:work_order_update", args=[order.pk]))
        expense_response = self.client.get(reverse("core:work_order_budget_charge", args=[order.pk]))
        self.assertEqual(edit_response.status_code, 403)
        self.assertEqual(expense_response.status_code, 403)

    def test_destination_technician_can_archive_done_forwarded_office_order(self):
        User = get_user_model()
        technician = User.objects.create_user(username="dest-tech-archive", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = self._create_forwarded_order(
            lawyer_only=False,
            title="Forwarded done",
            status=WorkOrder.Status.DONE,
        )
        self.client.force_login(technician)
        response = self.client.post(reverse("core:work_order_archive", args=[order.pk]))
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertIsNotNone(order.archived_at)
        self.assertEqual(order.building_id, self.destination.pk)
        self.assertIsNone(order.forwarded_to_building_id)

    def test_destination_technician_can_open_forwarded_order_detail(self):
        User = get_user_model()
        technician = User.objects.create_user(username="dest-tech-detail", password="pass")
        BuildingMembership.objects.create(
            user=technician,
            building=self.destination,
            role=MembershipRole.TECHNICIAN,
        )
        order = self._create_forwarded_order(lawyer_only=False, title="Forwarded detail")
        self.client.force_login(technician)
        response = self.client.get(
            reverse("core:work_order_detail", args=[order.pk]),
            HTTP_REFERER=reverse("core:building_detail", args=[self.destination.pk]),
        )
        self.assertEqual(response.status_code, 200)

    def test_technician_owner_sees_backoffice_created_orders_for_owned_building(self):
        User = get_user_model()
        technician_owner = User.objects.create_user(username="owned-tech", password="pass")
        BuildingMembership.objects.create(
            user=technician_owner,
            building=None,
            role=MembershipRole.TECHNICIAN,
        )
        assigned_building = Building.objects.create(owner=technician_owner, name="Assigned Tower")
        order = WorkOrder.objects.create(
            building=assigned_building,
            title="Assigned by Backoffice",
            deadline=timezone.localdate(),
            created_by=self.backoffice_user,
        )
        self.assertFalse(
            BuildingMembership.objects.filter(
                user=technician_owner,
                building=assigned_building,
            ).exists()
        )
        qs = WorkOrder.objects.visible_to(technician_owner)
        self.assertIn(order.pk, qs.values_list("pk", flat=True))


class OfficeBootstrapTests(TestCase):
    def setUp(self):
        Building.objects.all().delete()
        Building.clear_system_default_cache()
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="bootstrap-admin",
            password="pass",
            is_superuser=True,
        )
        BuildingMembership.objects.create(
            user=self.admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )

    def tearDown(self):
        Building.clear_system_default_cache()

    def test_office_is_created_on_demand_for_admins(self):
        resolver = CapabilityResolver(self.admin)
        office_id = resolver._office_building_id  # pylint: disable=protected-access
        self.assertIsNotNone(office_id)
        self.assertTrue(
            Building.objects.filter(pk=office_id, is_system_default=True).exists()
        )
