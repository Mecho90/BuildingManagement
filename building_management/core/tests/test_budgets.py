from __future__ import annotations

from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    BudgetFeatureFlag,
    BudgetRequest,
    Building,
    BuildingMembership,
    Expense,
    ExpenseCategory,
    MembershipRole,
)


User = get_user_model()


class BudgetRequestModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tech", password="pass")
        self.building = Building.objects.create(owner=self.user, name="Alpha")
        BudgetFeatureFlag.objects.create(key="budgets", is_enabled=True)
        self.category = ExpenseCategory.objects.create(code="materials", label="Materials", requires_receipt=False)

    def test_remaining_amount_tracks_expenses(self):
        budget = BudgetRequest.objects.create(
            requester=self.user,
            building=self.building,
            requested_amount=Decimal("100.00"),
            approved_amount=Decimal("80.00"),
            status=BudgetRequest.Status.APPROVED,
        )
        Expense.objects.create(
            budget_request=budget,
            expense_type=self.category,
            label="Paint",
            amount=Decimal("30.00"),
            status=Expense.Status.LOGGED,
            incurred_on=timezone.localdate(),
        )
        budget.refresh_from_db()
        self.assertEqual(budget.spent_amount, Decimal("30.00"))
        self.assertEqual(budget.remaining_amount, Decimal("50.00"))
        Expense.objects.create(
            budget_request=budget,
            expense_type=self.category,
            label="Brushes",
            amount=Decimal("60.00"),
            status=Expense.Status.LOGGED,
            incurred_on=timezone.localdate(),
        )
        budget.refresh_from_db()
        self.assertTrue(budget.has_overage)


class BudgetApiTests(TestCase):
    def setUp(self):
        self.client: Client = Client()
        self.factory = RequestFactory()
        self.technician = User.objects.create_user(username="tech", password="pass")
        self.approver = User.objects.create_user(username="reviewer", password="pass")
        self.building = Building.objects.create(owner=self.technician, name="Bravo")
        BudgetFeatureFlag.objects.create(key="budgets", is_enabled=True)
        BuildingMembership.objects.create(user=self.technician, building=self.building, role=MembershipRole.TECHNICIAN)
        BuildingMembership.objects.create(user=self.approver, building=self.building, role=MembershipRole.BACKOFFICE)
        self.category = ExpenseCategory.objects.create(code="fuel", label="Fuel", requires_receipt=True)

    def _login(self, user):
        self.client.force_login(user)

    def test_budget_creation_flow(self):
        self._login(self.technician)
        payload = {
            "building": str(self.building.pk),
            "requested_amount": "120.00",
            "currency": "USD",
            "notes": "Site visit cap",
        }
        response = self.client.post(reverse("core:api_budget_requests"), payload)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        budget_id = data["id"]
        self.assertEqual(data["status"], BudgetRequest.Status.PENDING_REVIEW)

        # Reviewer approves
        self._login(self.approver)
        approve_resp = self.client.patch(
            reverse("core:api_budget_request_detail", args=[budget_id]),
            data={"approved_amount": "100.00", "status": BudgetRequest.Status.APPROVED},
            content_type="application/json",
        )
        self.assertEqual(approve_resp.status_code, 200, approve_resp.content)

    def test_expense_logging_requires_permission(self):
        budget = BudgetRequest.objects.create(
            requester=self.technician,
            building=self.building,
            requested_amount=Decimal("90.00"),
            approved_amount=Decimal("90.00"),
            status=BudgetRequest.Status.APPROVED,
        )
        url = reverse("core:api_budget_expenses", args=[budget.pk])
        response = self.client.post(
            url,
            {"label": "Diesel", "amount": "20.00", "expense_type": str(self.category.pk), "status": Expense.Status.LOGGED},
        )
        self.assertEqual(response.status_code, 403)

        self._login(self.technician)
        ok = self.client.post(
            url,
            {"label": "Diesel", "amount": "20.00", "expense_type": str(self.category.pk), "status": Expense.Status.LOGGED},
        )
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(ok.json()["amount"], "20.00")


class BudgetArchiveViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.technician = User.objects.create_user(username="owner", password="pass")
        self.backoffice = User.objects.create_user(username="approver", password="pass")
        self.building = Building.objects.create(owner=self.backoffice, name="Archive")
        BudgetFeatureFlag.objects.create(key="budgets", is_enabled=True)
        BuildingMembership.objects.create(
            user=self.technician,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        BuildingMembership.objects.create(
            user=self.backoffice,
            building=self.building,
            role=MembershipRole.BACKOFFICE,
        )
        self.category = ExpenseCategory.objects.create(code="fuel", label="Fuel", requires_receipt=False)

    def _make_budget(self, amount="50.00"):
        return BudgetRequest.objects.create(
            requester=self.technician,
            building=self.building,
            requested_amount=Decimal(amount),
            approved_amount=Decimal(amount),
            status=BudgetRequest.Status.APPROVED,
        )

    def test_owner_can_archive_fully_spent_budget(self):
        budget = self._make_budget()
        Expense.objects.create(
            budget_request=budget,
            expense_type=self.category,
            label="Fuel",
            amount=Decimal("50.00"),
            status=Expense.Status.LOGGED,
        )
        self.client.force_login(self.technician)
        response = self.client.post(reverse("core:budget_archive", args=[budget.pk]))
        self.assertEqual(response.status_code, 302)
        budget.refresh_from_db()
        self.assertIsNotNone(budget.archived_at)
        self.assertEqual(budget.status, BudgetRequest.Status.CLOSED)

    def test_cannot_archive_with_remaining_balance(self):
        budget = self._make_budget(amount="80.00")
        Expense.objects.create(
            budget_request=budget,
            expense_type=self.category,
            label="Fuel",
            amount=Decimal("40.00"),
            status=Expense.Status.LOGGED,
        )
        self.client.force_login(self.technician)
        response = self.client.post(reverse("core:budget_archive", args=[budget.pk]))
        self.assertEqual(response.status_code, 302)
        budget.refresh_from_db()
        self.assertIsNone(budget.archived_at)
        self.assertEqual(budget.status, BudgetRequest.Status.APPROVED)

    def test_archived_list_requires_approver(self):
        budget = self._make_budget()
        budget.archived_at = timezone.now()
        budget.status = BudgetRequest.Status.CLOSED
        budget.save(update_fields=["archived_at", "status"])
        self.client.force_login(self.backoffice)
        response = self.client.get(reverse("core:budget_archived_list"))
        self.assertEqual(response.status_code, 200)
        owner_groups = response.context["owner_groups"]
        self.assertEqual(len(owner_groups), 1)
        self.assertEqual(owner_groups[0]["owner_name"], self.technician.get_username())


class BudgetArchivePurgeViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.requester = User.objects.create_user(username="req", password="pass")
        self.backoffice = User.objects.create_user(username="approver-purge", password="pass")
        self.building = Building.objects.create(owner=self.requester, name="Delta")
        BudgetFeatureFlag.objects.create(key="budgets", is_enabled=True)
        BuildingMembership.objects.create(
            user=self.backoffice,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )

    def _create_archived_budget(self, days_ago: int):
        budget = BudgetRequest.objects.create(
            requester=self.requester,
            building=self.building,
            requested_amount=Decimal("100.00"),
            approved_amount=Decimal("100.00"),
            status=BudgetRequest.Status.CLOSED,
        )
        budget.archived_at = timezone.now() - timedelta(days=days_ago)
        budget.save(update_fields=["archived_at", "status"])
        return budget

    def test_requires_backoffice_role(self):
        technician = User.objects.create_user(username="tech-purge", password="pass")
        self.client.force_login(technician)
        response = self.client.post(
            reverse("core:budget_archived_purge"),
            {
                "from_date": timezone.localdate().isoformat(),
                "to_date": timezone.localdate().isoformat(),
                "confirm": "on",
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_backoffice_deletes_budgets_in_range(self):
        old_budget = self._create_archived_budget(days_ago=90)
        recent_budget = self._create_archived_budget(days_ago=5)
        self.client.force_login(self.backoffice)
        today = timezone.localdate()
        response = self.client.post(
            reverse("core:budget_archived_purge"),
            {
                "from_date": (today - timedelta(days=10)).isoformat(),
                "to_date": today.isoformat(),
                "confirm": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(BudgetRequest.objects.filter(pk=old_budget.pk).exists())
        self.assertFalse(BudgetRequest.objects.filter(pk=recent_budget.pk).exists())
