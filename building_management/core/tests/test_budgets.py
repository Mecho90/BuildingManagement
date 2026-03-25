from __future__ import annotations

from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from core.forms import BudgetFilterForm
from core.models import (
    BudgetFeatureFlag,
    BudgetRequest,
    BudgetRequestEvent,
    Building,
    BuildingMembership,
    Expense,
    ExpenseCategory,
    MembershipRole,
    WorkOrder,
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


class BudgetMassAssignViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        BudgetFeatureFlag.objects.create(key="budgets", is_enabled=True)
        self.admin = User.objects.create_user(username="admin-budget", password="pass")
        self.tech_one = User.objects.create_user(username="tech-one", password="pass")
        self.tech_two = User.objects.create_user(username="tech-two", password="pass")
        self.backoffice = User.objects.create_user(username="backoffice-budget", password="pass")
        self.lawyer = User.objects.create_user(username="lawyer-budget", password="pass")
        self.building = Building.objects.create(owner=self.admin, name="Mass Budget")
        BuildingMembership.objects.create(
            user=self.admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )
        BuildingMembership.objects.create(
            user=self.tech_one,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        BuildingMembership.objects.create(
            user=self.tech_two,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        BuildingMembership.objects.create(
            user=self.backoffice,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )
        BuildingMembership.objects.create(
            user=self.lawyer,
            building=None,
            role=MembershipRole.LAWYER,
        )
        self.budget = BudgetRequest.objects.create(
            requester=self.tech_one,
            building=self.building,
            title="Initial budget",
            requested_amount=Decimal("150.00"),
            status=BudgetRequest.Status.PENDING_REVIEW,
        )

    def test_non_admin_cannot_open_mass_assign(self):
        self.client.force_login(self.tech_one)
        response = self.client.get(reverse("core:budget_mass_assign"))
        self.assertEqual(response.status_code, 404)

    def test_admin_can_create_budgets_for_selected_users(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("core:budget_mass_assign"),
            {
                "users": [str(self.tech_one.pk), str(self.tech_two.pk)],
                "title": "Mass Assigned Budget",
                "requested_amount": "333.00",
                "description": "Bulk created budget",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("core:budget_list"))
        created_qs = BudgetRequest.objects.filter(
            title="Mass Assigned Budget",
            requested_amount=Decimal("333.00"),
            description="Bulk created budget",
        )
        self.assertEqual(created_qs.count(), 2)
        self.assertSetEqual(
            set(created_qs.values_list("requester_id", flat=True)),
            {self.tech_one.pk, self.tech_two.pk},
        )
        self.assertEqual(
            created_qs.filter(status=BudgetRequest.Status.APPROVED).count(),
            2,
        )
        self.assertEqual(
            created_qs.filter(approved_by=self.admin).count(),
            2,
        )
        event = BudgetRequestEvent.objects.filter(
            budget_request__in=created_qs,
            event_type=BudgetRequestEvent.EventType.COMMENT,
            payload__action="mass_assigned",
        ).count()
        self.assertEqual(event, 2)

    def test_admin_users_are_excluded_from_assignee_list(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("core:budget_mass_assign"))
        self.assertEqual(response.status_code, 200)
        assignee_qs = response.context["form"].fields["users"].queryset
        self.assertFalse(assignee_qs.filter(pk=self.admin.pk).exists())
        self.assertFalse(assignee_qs.filter(pk=self.lawyer.pk).exists())

    def test_only_admin_can_delete_mass_assigned_budgets(self):
        self.client.force_login(self.admin)
        self.client.post(
            reverse("core:budget_mass_assign"),
            {
                "users": [str(self.tech_one.pk)],
                "title": "Mass Assigned Budget Deletable",
                "requested_amount": "120.00",
                "description": "Bulk created budget",
            },
        )
        mass_assigned_budget = BudgetRequest.objects.get(
            requester=self.tech_one,
            title="Mass Assigned Budget Deletable",
        )

        self.client.force_login(self.tech_one)
        response = self.client.get(reverse("core:budget_delete", args=[mass_assigned_budget.pk]))
        self.assertEqual(response.status_code, 404)

        self.client.force_login(self.backoffice)
        response = self.client.get(reverse("core:budget_delete", args=[mass_assigned_budget.pk]))
        self.assertEqual(response.status_code, 404)

        self.client.force_login(self.admin)
        response = self.client.get(reverse("core:budget_delete", args=[mass_assigned_budget.pk]))
        self.assertEqual(response.status_code, 200)

    def test_admin_budget_list_exposes_delete_for_mass_assigned_budgets(self):
        self.client.force_login(self.admin)
        self.client.post(
            reverse("core:budget_mass_assign"),
            {
                "users": [str(self.tech_one.pk)],
                "title": "Mass Assigned Budget List Delete",
                "requested_amount": "90.00",
                "description": "Bulk created budget",
            },
        )
        mass_assigned_budget = BudgetRequest.objects.get(
            requester=self.tech_one,
            title="Mass Assigned Budget List Delete",
        )
        response = self.client.get(reverse("core:budget_list"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(mass_assigned_budget.pk, set(response.context["budget_delete_ids"]))


class BudgetSummaryVisibilityTests(TestCase):
    def setUp(self):
        self.client = Client()
        BudgetFeatureFlag.objects.create(key="budgets", is_enabled=True)
        self.admin = User.objects.create_user(username="budget-admin", password="pass")
        self.backoffice = User.objects.create_user(username="budget-backoffice", password="pass")
        self.tech = User.objects.create_user(username="budget-tech", password="pass")
        self.building = Building.objects.create(owner=self.admin, name="Budget Summary")
        BuildingMembership.objects.create(user=self.admin, building=None, role=MembershipRole.ADMINISTRATOR)
        BuildingMembership.objects.create(user=self.backoffice, building=None, role=MembershipRole.BACKOFFICE)
        BuildingMembership.objects.create(user=self.tech, building=self.building, role=MembershipRole.TECHNICIAN)

        BudgetRequest.objects.create(
            requester=self.backoffice,
            building=self.building,
            title="Backoffice budget",
            requested_amount=Decimal("200.00"),
            approved_amount=Decimal("200.00"),
            spent_amount=Decimal("50.00"),
            status=BudgetRequest.Status.APPROVED,
        )
        BudgetRequest.objects.create(
            requester=self.tech,
            building=self.building,
            title="Technician budget",
            requested_amount=Decimal("300.00"),
            approved_amount=Decimal("300.00"),
            spent_amount=Decimal("100.00"),
            status=BudgetRequest.Status.APPROVED,
        )

    def test_backoffice_summary_shows_only_own_remaining(self):
        self.client.force_login(self.backoffice)
        response = self.client.get(reverse("core:budget_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary_total_remaining"], Decimal("150.00"))
        self.assertEqual(response.context["summary_by_requester"], [])

    def test_admin_summary_shows_all_and_per_requester(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("core:budget_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary_total_remaining"], Decimal("350.00"))
        labels = {entry["requester_label"] for entry in response.context["summary_by_requester"]}
        self.assertIn(self.backoffice.username, labels)
        self.assertIn(self.tech.username, labels)


class BudgetReviewPermissionTests(TestCase):
    def setUp(self):
        self.client = Client()
        BudgetFeatureFlag.objects.create(key="budgets", is_enabled=True)
        self.backoffice = User.objects.create_user(username="backoffice-review", password="pass")
        self.admin = User.objects.create_user(username="admin-review", password="pass")
        self.tech = User.objects.create_user(username="tech-review", password="pass")
        self.building = Building.objects.create(owner=self.admin, name="Review Building")
        BuildingMembership.objects.create(user=self.backoffice, building=None, role=MembershipRole.BACKOFFICE)
        BuildingMembership.objects.create(user=self.admin, building=None, role=MembershipRole.ADMINISTRATOR)
        BuildingMembership.objects.create(user=self.tech, building=self.building, role=MembershipRole.TECHNICIAN)

        self.backoffice_budget = BudgetRequest.objects.create(
            requester=self.backoffice,
            building=self.building,
            title="Backoffice request",
            requested_amount=Decimal("100.00"),
            status=BudgetRequest.Status.PENDING_REVIEW,
        )
        self.tech_budget = BudgetRequest.objects.create(
            requester=self.tech,
            building=self.building,
            title="Technician request",
            requested_amount=Decimal("120.00"),
            status=BudgetRequest.Status.PENDING_REVIEW,
        )

    def test_backoffice_list_hides_review_for_own_budget(self):
        self.client.force_login(self.backoffice)
        response = self.client.get(reverse("core:budget_list"))
        self.assertEqual(response.status_code, 200)
        reviewable_ids = set(response.context["reviewable_budget_ids"])
        self.assertNotIn(self.backoffice_budget.pk, reviewable_ids)
        self.assertIn(self.tech_budget.pk, reviewable_ids)

    def test_backoffice_review_queue_excludes_own_budget(self):
        self.client.force_login(self.backoffice)
        response = self.client.get(reverse("core:budget_review_queue"))
        self.assertEqual(response.status_code, 200)
        pending_ids = {budget.pk for budget in response.context["pending_budgets"]}
        self.assertNotIn(self.backoffice_budget.pk, pending_ids)
        self.assertIn(self.tech_budget.pk, pending_ids)

    def test_backoffice_cannot_open_own_budget_review_page(self):
        self.client.force_login(self.backoffice)
        response = self.client.get(reverse("core:budget_review_decision", args=[self.backoffice_budget.pk]))
        self.assertEqual(response.status_code, 404)


class BudgetFilterFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="form-backoffice", password="pass")
        BuildingMembership.objects.create(user=self.user, building=None, role=MembershipRole.BACKOFFICE)

    def test_requester_defaults_to_current_user_without_empty_option(self):
        form = BudgetFilterForm(user=self.user)
        self.assertTrue(form.show_requester)
        technician_field = form.fields["technician"]
        self.assertIsNone(technician_field.empty_label)
        self.assertEqual(form.initial.get("technician"), self.user.pk)
        self.assertTrue(technician_field.queryset.filter(pk=self.user.pk).exists())

    def test_admin_without_budgets_still_visible_in_requester_filter(self):
        admin = User.objects.create_user(username="form-admin", password="pass")
        BuildingMembership.objects.create(user=admin, building=None, role=MembershipRole.ADMINISTRATOR)
        form = BudgetFilterForm(user=admin)
        technician_field = form.fields["technician"]
        self.assertTrue(technician_field.queryset.filter(pk=admin.pk).exists())
        self.assertEqual(form.initial.get("technician"), admin.pk)

    def test_requester_label_prefers_full_name(self):
        self.user.first_name = "Господин"
        self.user.last_name = "Лимон"
        self.user.save(update_fields=["first_name", "last_name"])
        form = BudgetFilterForm(user=self.user)
        technician_field = form.fields["technician"]
        self.assertEqual(
            technician_field.label_from_instance(self.user),
            "Господин Лимон",
        )


class BudgetDetailExpenseLinksTests(TestCase):
    def setUp(self):
        self.client = Client()
        BudgetFeatureFlag.objects.create(key="budgets", is_enabled=True)
        self.tech = User.objects.create_user(username="budget-tech-links", password="pass")
        self.owner = User.objects.create_user(username="budget-owner-links", password="pass")
        self.building = Building.objects.create(owner=self.owner, name="Budget Link Building")
        BuildingMembership.objects.create(
            user=self.tech,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        BuildingMembership.objects.create(
            user=self.tech,
            building=None,
            role=MembershipRole.TECHNICIAN,
        )
        self.budget = BudgetRequest.objects.create(
            requester=self.tech,
            building=self.building,
            title="Linked budget",
            requested_amount=Decimal("100.00"),
            approved_amount=Decimal("100.00"),
            status=BudgetRequest.Status.APPROVED,
        )

    def test_hidden_work_order_link_when_user_cannot_access_order(self):
        restricted_work_order = WorkOrder.objects.create(
            building=self.building,
            title="Lawyer order",
            deadline=timezone.localdate(),
            lawyer_only=True,
        )
        Expense.objects.create(
            budget_request=self.budget,
            label="Expense on hidden order",
            amount=Decimal("20.00"),
            status=Expense.Status.LOGGED,
            metadata={"work_order_id": restricted_work_order.pk},
        )

        self.client.force_login(self.tech)
        response = self.client.get(reverse("core:budget_detail", args=[self.budget.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response,
            reverse("core:work_order_detail", args=[restricted_work_order.pk]),
        )
