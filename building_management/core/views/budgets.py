from __future__ import annotations

from decimal import Decimal
import re

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Prefetch
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy, reverse
from django.utils.translation import gettext as _, ngettext
from django.utils.dateparse import parse_date
from django.views.generic import DetailView, FormView, ListView, TemplateView, CreateView, UpdateView, View
from django.core.paginator import Paginator

from ..authz import Capability, CapabilityResolver
from ..forms import (
    ArchivePurgeForm,
    BudgetExpenseForm,
    BudgetFilterForm,
    BudgetRequestApprovalForm,
    BudgetRequestForm,
    MassAssignBudgetsForm,
)
from ..models import (
    BudgetFeatureFlag,
    BudgetRequest,
    BudgetRequestEvent,
    BuildingMembership,
    Expense,
    MembershipRole,
)
from ..services import BudgetExporter, NotificationPayload, NotificationService
from ..utils.roles import user_is_admin_or_backoffice
from .common import CapabilityRequiredMixin, _querystring_without

User = get_user_model()


def _primary_membership_role(user) -> str | None:
    if not user or not getattr(user, "is_authenticated", False):
        return None
    cached = getattr(user, "_primary_membership_role", None)
    if cached is not None:
        return cached
    ordering = models.Case(
        models.When(building__isnull=True, then=0),
        default=1,
    )
    role = (
        BuildingMembership.objects.filter(user=user)
        .order_by(ordering, "id")
        .values_list("role", flat=True)
        .first()
    )
    setattr(user, "_primary_membership_role", role)
    return role


def _user_can_request_budget(user) -> bool:
    role = _primary_membership_role(user)
    return role in {MembershipRole.TECHNICIAN, MembershipRole.BACKOFFICE}


def _user_can_mass_assign_budgets(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return BuildingMembership.objects.filter(
        user=user,
        role=MembershipRole.ADMINISTRATOR,
    ).exists()


def _user_is_budget_admin(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return BuildingMembership.objects.filter(
        user=user,
        role=MembershipRole.ADMINISTRATOR,
    ).exists()


def _budget_can_be_reviewed_by(budget: BudgetRequest, reviewer) -> bool:
    if not reviewer or not getattr(reviewer, "is_authenticated", False):
        return False
    reviewer_role = _primary_membership_role(reviewer)
    requester_role = _primary_membership_role(budget.requester)
    if reviewer_role is None:
        return False
    if requester_role == MembershipRole.BACKOFFICE:
        return reviewer_role == MembershipRole.ADMINISTRATOR
    if requester_role == MembershipRole.TECHNICIAN:
        return reviewer_role in {MembershipRole.ADMINISTRATOR, MembershipRole.BACKOFFICE}
    # Default to administrator-only approvals.
    return reviewer_role == MembershipRole.ADMINISTRATOR


def _user_can_delete_budget(budget: BudgetRequest, user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    is_mass_assigned = budget.events.filter(
        event_type=BudgetRequestEvent.EventType.COMMENT,
        payload__action="mass_assigned",
    ).exists()
    if is_mass_assigned:
        return _user_is_budget_admin(user)
    if budget.status == BudgetRequest.Status.APPROVED:
        resolver = CapabilityResolver(user)
        return resolver.has(Capability.APPROVE_BUDGETS, building_id=budget.building_id)
    return (
        budget.requester_id == getattr(user, "pk", None)
        and budget.status == BudgetRequest.Status.PENDING_REVIEW
    )


def _user_can_archive_budget(budget: BudgetRequest, user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if budget.is_archived:
        return False
    if budget.status not in {
        BudgetRequest.Status.APPROVED,
        BudgetRequest.Status.CLOSED,
    }:
        return False
    if budget.remaining_amount > Decimal("0.00"):
        return False
    if budget.requester_id == getattr(user, "pk", None):
        return True
    resolver = CapabilityResolver(user)
    return resolver.has(Capability.APPROVE_BUDGETS, building_id=budget.building_id)

def _user_can_log_budget_expense(budget: BudgetRequest, user) -> bool:
    if not budget or not user or not getattr(user, "is_authenticated", False):
        return False
    if budget.status != BudgetRequest.Status.APPROVED:
        return False
    if budget.requester_id == getattr(user, "pk", None):
        return True
    resolver = CapabilityResolver(user)
    return resolver.has(Capability.MANAGE_BUDGETS, building_id=budget.building_id)

def _coerce_int(value):
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

_WORK_ORDER_PATTERN = re.compile(r"work\s*order\s+#?(\d+)", re.IGNORECASE)

def _extract_work_order_id_from_text(*texts):
    for text in texts:
        if not text:
            continue
        match = _WORK_ORDER_PATTERN.search(str(text))
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


class BudgetFeatureRequiredMixin(CapabilityRequiredMixin):
    required_capabilities = (Capability.VIEW_BUDGETS,)

    def dispatch(self, request, *args, **kwargs):
        if not BudgetFeatureFlag.is_enabled_for(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def _check_budget_visibility(self, budget: BudgetRequest):
        if not budget:
            raise Http404()
        if getattr(self.request.user, "is_superuser", False):
            return
        allowed = BudgetRequest.objects.visible_to(self.request.user).filter(pk=budget.pk).exists()
        if not allowed:
            raise Http404()


class BudgetListView(LoginRequiredMixin, BudgetFeatureRequiredMixin, TemplateView):
    template_name = "core/budgets_list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        resolver = CapabilityResolver(user)
        base_budget_qs = (
            BudgetRequest.objects.visible_to(self.request.user)
            .active()
            .select_related("building", "requester")
            .with_totals()
        )
        budget_qs = base_budget_qs
        filter_form = BudgetFilterForm(self.request.GET or None, user=self.request.user)
        if filter_form.is_valid():
            data = filter_form.cleaned_data
            if data.get("status"):
                budget_qs = budget_qs.filter(status=data["status"])
            if data.get("technician"):
                budget_qs = budget_qs.filter(requester=data["technician"])
            if data.get("q"):
                query = data["q"].strip()
                budget_qs = budget_qs.filter(
                    models.Q(description__icontains=query)
                    | models.Q(project_code__icontains=query)
                    | models.Q(title__icontains=query)
                )
        totals_expr = models.Case(
            models.When(approved_amount__isnull=False, then=models.F("approved_amount")),
            default=models.F("requested_amount"),
            output_field=models.DecimalField(max_digits=12, decimal_places=2),
        )
        is_budget_admin = _user_is_budget_admin(user)
        technician_requester_ids: list[int] = []
        if is_budget_admin:
            technician_requester_ids = list(
                BuildingMembership.objects.filter(role=MembershipRole.TECHNICIAN)
                .values_list("user_id", flat=True)
                .distinct()
            )
        summary_qs = (
            base_budget_qs.filter(requester_id__in=technician_requester_ids)
            if is_budget_admin
            else base_budget_qs.filter(requester=user)
        )
        totals = summary_qs.aggregate(
            requested_total=models.Sum("requested_amount"),
            approved_total=models.Sum(totals_expr),
            spent_total=models.Sum("spent_amount"),
            remaining_total=models.Sum(
                models.ExpressionWrapper(
                    totals_expr - models.F("spent_amount"),
                    output_field=models.DecimalField(max_digits=12, decimal_places=2),
                )
            ),
        )
        summary_by_requester = []
        if is_budget_admin:
            requester_totals = (
                summary_qs.values("requester_id", "requester__username", "requester__first_name", "requester__last_name")
                .annotate(
                    remaining_total=models.Sum(
                        models.ExpressionWrapper(
                            totals_expr - models.F("spent_amount"),
                            output_field=models.DecimalField(max_digits=12, decimal_places=2),
                        )
                    )
                )
                .order_by("requester__username")
            )
            for row in requester_totals:
                full_name = " ".join(
                    [part for part in [row.get("requester__first_name"), row.get("requester__last_name")] if part]
                ).strip()
                summary_by_requester.append(
                    {
                        "requester_id": row.get("requester_id"),
                        "requester_label": full_name or row.get("requester__username") or _("Unknown"),
                        "remaining_total": row.get("remaining_total") or Decimal("0.00"),
                    }
                )
        # `with_totals()` adds aggregation; keep a deterministic order before paginating.
        budget_qs = budget_qs.order_by("-created_at", "-id")
        paginator = Paginator(budget_qs, 20)
        page_number = self.request.GET.get("page") or 1
        page_obj = paginator.get_page(page_number)
        reviewable_budget_ids = {
            budget.pk
            for budget in page_obj.object_list
            if budget.status == BudgetRequest.Status.PENDING_REVIEW
            and _budget_can_be_reviewed_by(budget, self.request.user)
        }
        archiveable_ids = [
            budget.pk
            for budget in page_obj.object_list
            if getattr(budget, "spent_total", Decimal("0.00")) > Decimal("0.00")
            and _user_can_archive_budget(budget, self.request.user)
        ]
        budget_delete_ids = [
            budget.pk
            for budget in page_obj.object_list
            if _user_can_delete_budget(budget, self.request.user)
        ]
        ctx.update(
            {
                "budget_list": page_obj.object_list,
                "budget_page": page_obj,
                "budget_query": _querystring_without(self.request, "page"),
                "filter_form": filter_form,
                "can_export_budgets": resolver.has(Capability.EXPORT_BUDGETS),
                "technician_summary_url": reverse_lazy("core:budget_technicians"),
                "summary_total_remaining": totals.get("remaining_total") or Decimal("0.00"),
                "summary_total_spent": totals.get("spent_total") or Decimal("0.00"),
                "archived_budgets_url": reverse_lazy("core:budget_archived_list")
                if resolver.has(Capability.APPROVE_BUDGETS)
                else "",
                "can_create_budget": _user_can_request_budget(self.request.user),
                "can_review_budgets": resolver.has(Capability.APPROVE_BUDGETS),
                "can_mass_assign_budgets": _user_can_mass_assign_budgets(self.request.user),
                "budget_archive_ids": archiveable_ids,
                "budget_delete_ids": budget_delete_ids,
                "reviewable_budget_ids": reviewable_budget_ids,
                "is_budget_admin": is_budget_admin,
                "summary_by_requester": summary_by_requester,
            }
        )
        return ctx


class BudgetMassAssignView(LoginRequiredMixin, BudgetFeatureRequiredMixin, FormView):
    template_name = "core/budgets_mass_assign.html"
    form_class = MassAssignBudgetsForm
    success_url = reverse_lazy("core:budget_list")

    def dispatch(self, request, *args, **kwargs):
        if not _user_can_mass_assign_budgets(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_assignee_queryset(self):
        assignee_ids = (
            BuildingMembership.objects.filter(
                role__in=[
                    MembershipRole.TECHNICIAN,
                    MembershipRole.BACKOFFICE,
                ]
            )
            .values_list("user_id", flat=True)
            .distinct()
        )
        administrator_ids = (
            BuildingMembership.objects.filter(role=MembershipRole.ADMINISTRATOR)
            .values_list("user_id", flat=True)
            .distinct()
        )
        return (
            User.objects.filter(pk__in=assignee_ids, is_active=True)
            .exclude(pk__in=administrator_ids)
            .order_by("username")
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user_queryset"] = self.get_assignee_queryset()
        return kwargs

    def form_valid(self, form):
        selected_users = list(form.cleaned_data.get("users") or [])
        title = (form.cleaned_data.get("title") or "").strip()
        requested_amount = form.cleaned_data["requested_amount"]
        description = (form.cleaned_data.get("description") or "").strip()
        created = 0
        for assignee in selected_users:
            budget = BudgetRequest.objects.create(
                requester=assignee,
                title=title,
                description=description,
                requested_amount=requested_amount,
                status=BudgetRequest.Status.DRAFT,
            )
            budget.transition(
                status=BudgetRequest.Status.APPROVED,
                actor=self.request.user,
                comment=_("Budget request auto-approved via mass assignment."),
            )
            budget.log_event(
                actor=self.request.user,
                event_type=BudgetRequestEvent.EventType.COMMENT,
                notes=_("Budget assigned in bulk."),
                payload={
                    "action": "mass_assigned",
                    "assigned_to_user_id": assignee.pk,
                },
            )
            created += 1

        if created:
            messages.success(
                self.request,
                ngettext(
                    "Created %(count)s budget request.",
                    "Created %(count)s budget requests.",
                    created,
                )
                % {"count": created},
            )
        else:
            messages.info(self.request, _("No users were selected."))
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        queryset = self.get_assignee_queryset()
        ctx.update(
            {
                "assignee_count": queryset.count(),
            }
        )
        return ctx


class BudgetDetailView(LoginRequiredMixin, BudgetFeatureRequiredMixin, DetailView):
    model = BudgetRequest
    template_name = "core/budget_detail.html"
    context_object_name = "budget"

    def get_queryset(self):
        return (
            BudgetRequest.objects.visible_to(self.request.user)
            .select_related("building", "requester", "approved_by")
            .prefetch_related("events", "expenses__attachments")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        budget: BudgetRequest = ctx["budget"]
        building_filter_raw = (self.request.GET.get("building") or "").strip()
        building_filter_id: int | None = None
        if building_filter_raw:
            try:
                building_filter_id = int(building_filter_raw)
            except (TypeError, ValueError):
                building_filter_id = None
        expenses = (
            budget.expenses.select_related("expense_type", "created_by")
            .prefetch_related("attachments")
            .order_by("-incurred_on", "-id")
        )
        if building_filter_id:
            expenses = expenses.filter(metadata__building_id=building_filter_id)
        expenses = list(expenses)
        current_user_id = getattr(self.request.user, "pk", None)
        can_manage_expenses = _user_can_log_budget_expense(budget, self.request.user)
        for expense in expenses:
            meta = expense.metadata or {}
            work_order_id = _coerce_int(meta.get("work_order_id"))
            if work_order_id is None:
                work_order_id = _coerce_int(meta.get("work_order"))
            if work_order_id is None:
                work_order_id = _extract_work_order_id_from_text(
                    meta.get("work_order_title"),
                    meta.get("work_order_label"),
                    expense.label,
                    expense.notes,
                )
            work_order_title = (meta.get("work_order_title") or meta.get("work_order_name") or "").strip()
            building_id = _coerce_int(meta.get("building_id"))
            if building_id is None:
                building_id = _coerce_int(meta.get("building"))
            work_order_url = ""
            building_url = ""
            work_order_label = ""
            work_order_number_label = ""
            building_label = ""
            primary_url = ""
            primary_label = ""
            primary_kind = ""
            if work_order_id:
                work_order_url = reverse("core:work_order_detail", args=[work_order_id])
                work_order_number_label = _("Work order #%(id)s") % {"id": work_order_id}
                work_order_label = work_order_title or work_order_number_label
                primary_url = work_order_url
                primary_label = work_order_label
                primary_kind = "work_order"
                if work_order_label == work_order_number_label:
                    raw_label = (expense.label or "").strip()
                    if raw_label and "·" in raw_label:
                        prefix, suffix = raw_label.split("·", 1)
                        candidate = suffix.strip()
                        if candidate:
                            work_order_label = candidate
            if building_id:
                building_url = reverse("core:building_detail", args=[building_id])
                building_label = meta.get("building_name") or _("Building %(id)s") % {"id": building_id}
                if not primary_url:
                    primary_url = building_url
                    primary_label = building_label
                    primary_kind = "building"
            expense.primary_link_url = primary_url
            expense.primary_link_label = primary_label
            expense.primary_link_kind = primary_kind
            expense.work_order_url = work_order_url
            expense.building_url = building_url
            expense.work_order_label = work_order_label
            expense.work_order_number_label = work_order_number_label
            expense.building_label = building_label
            expense.display_label = work_order_label or (expense.label or "")
            auto_note_template = ""
            if work_order_id:
                auto_note_template = _("Logged automatically from work order %(id)s.") % {"id": work_order_id}
            note_text = (expense.notes or "").strip()
            expense.show_notes = bool(note_text and note_text != auto_note_template.strip())

            can_delete = can_manage_expenses or (
                current_user_id and expense.created_by_id == current_user_id
            )
            expense.can_delete = can_delete
            if can_delete:
                expense.delete_url = reverse(
                    "core:budget_expense_delete",
                    args=[budget.pk, expense.pk],
                )
        ctx["expenses"] = expenses
        building_options_qs = (
            budget.expenses.filter(metadata__building_id__isnull=False)
            .values_list("metadata__building_id", "metadata__building_name")
            .distinct()
        )
        building_options: list[dict[str, object]] = []
        for option_id, option_name in building_options_qs:
            if not option_id:
                continue
            building_options.append(
                {
                    "id": int(option_id),
                    "name": option_name or _("Building %(id)s") % {"id": option_id},
                }
            )
        building_options.sort(key=lambda item: item["name"])
        ctx["expense_building_options"] = building_options
        ctx["selected_expense_building"] = building_filter_id
        can_manage_expenses = _user_can_log_budget_expense(budget, self.request.user)
        ctx["can_log_expense"] = can_manage_expenses
        if can_manage_expenses:
            ctx["expense_form"] = BudgetExpenseForm(user=self.request.user, budget=budget)
        ctx["events"] = budget.events.select_related("actor")
        resolver = CapabilityResolver(self.request.user)
        ctx["can_review_budget"] = (
            budget.status == BudgetRequest.Status.PENDING_REVIEW
            and resolver.has(
                Capability.APPROVE_BUDGETS,
                building_id=budget.building_id,
            )
            and _budget_can_be_reviewed_by(budget, self.request.user)
        )
        ctx["can_delete_budget"] = _user_can_delete_budget(budget, self.request.user)
        ctx["can_archive_budget"] = _user_can_archive_budget(budget, self.request.user)
        ctx["archive_budget_url"] = reverse("core:budget_archive", args=[budget.pk])
        return ctx


class BudgetCreateView(LoginRequiredMixin, BudgetFeatureRequiredMixin, CreateView):
    template_name = "core/budget_form.html"
    form_class = BudgetRequestForm
    success_url = reverse_lazy("core:budget_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def dispatch(self, request, *args, **kwargs):
        if not _user_can_request_budget(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        self.object.transition(
            status=BudgetRequest.Status.PENDING_REVIEW,
            actor=self.request.user,
            comment="",
        )
        messages.success(self.request, _("Budget request submitted for review."))
        return response


class BudgetUpdateView(LoginRequiredMixin, BudgetFeatureRequiredMixin, UpdateView):
    model = BudgetRequest
    template_name = "core/budget_form.html"
    form_class = BudgetRequestForm
    success_url = reverse_lazy("core:budget_list")

    def get_queryset(self):
        return BudgetRequest.objects.filter(requester=self.request.user).select_related("building")

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if obj.status != BudgetRequest.Status.DRAFT:
            raise Http404()
        return obj

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        if self.object.status != BudgetRequest.Status.DRAFT:
            form.add_error(None, _("Only draft budgets can be edited."))
            return self.form_invalid(form)
        messages.success(self.request, _("Budget request updated."))
        return super().form_valid(form)


class BudgetExpenseCreateView(LoginRequiredMixin, BudgetFeatureRequiredMixin, FormView):
    form_class = BudgetExpenseForm

    def dispatch(self, request, *args, **kwargs):
        self.budget = get_object_or_404(
            BudgetRequest.objects.visible_to(request.user),
            pk=kwargs["pk"],
        )
        if not _user_can_log_budget_expense(self.budget, request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["budget"] = self.budget
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        try:
            form.save()
        except ValidationError as exc:
            self._handle_validation_errors(exc)
            return redirect(self.get_success_url())
        messages.success(self.request, _("Expense logged."))
        return redirect(self.get_success_url())

    def form_invalid(self, form):
        for errors in form.errors.values():
            for error in errors:
                messages.error(self.request, error)
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("core:budget_detail", args=[self.budget.pk])

    def _handle_validation_errors(self, error: ValidationError):
        if hasattr(error, "error_dict"):
            for field_errors in error.error_dict.values():
                for message in field_errors:
                    messages.error(self.request, message)
        else:
            for message in error.messages:
                messages.error(self.request, message)


class BudgetExpenseDeleteView(LoginRequiredMixin, BudgetFeatureRequiredMixin, View):
    def post(self, request, pk, expense_id):
        budget = get_object_or_404(
            BudgetRequest.objects.visible_to(request.user),
            pk=pk,
        )
        expense = get_object_or_404(
            Expense.objects.filter(budget_request=budget),
            pk=expense_id,
        )
        if not self._can_delete_expense(request.user, budget, expense):
            raise Http404()
        expense.delete()
        messages.success(request, _("Expense removed."))
        return redirect("core:budget_detail", pk=budget.pk)

    def _can_delete_expense(self, user, budget, expense):
        if _user_can_log_budget_expense(budget, user):
            return True
        if not user or not getattr(user, "is_authenticated", False):
            return False
        return expense.created_by_id == getattr(user, "pk", None)


class BudgetReviewQueueView(LoginRequiredMixin, BudgetFeatureRequiredMixin, ListView):
    template_name = "core/budget_review_queue.html"
    context_object_name = "pending_budgets"

    def get_queryset(self):
        resolver = CapabilityResolver(self.request.user)
        if not resolver.has(Capability.APPROVE_BUDGETS):
            raise Http404()
        qs = (
            BudgetRequest.objects.pending_review()
            .visible_to(self.request.user)
            .select_related("building", "requester")
        )
        reviewable_ids = [
            budget.pk for budget in qs
            if _budget_can_be_reviewed_by(budget, self.request.user)
        ]
        if not reviewable_ids:
            return qs.none()
        return qs.filter(pk__in=reviewable_ids).order_by("-updated_at")


class BudgetReviewDecisionView(LoginRequiredMixin, BudgetFeatureRequiredMixin, FormView):
    form_class = BudgetRequestApprovalForm
    template_name = "core/budget_review_detail.html"

    def dispatch(self, request, *args, **kwargs):
        self.budget = get_object_or_404(
            BudgetRequest.objects.visible_to(request.user),
            pk=kwargs["pk"],
        )
        if self.budget.status != BudgetRequest.Status.PENDING_REVIEW:
            raise Http404()
        if not _budget_can_be_reviewed_by(self.budget, request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.budget
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.save()
        messages.success(self.request, _("Decision recorded."))
        return redirect("core:budget_detail", pk=self.budget.pk)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["budget"] = self.budget
        ctx["events"] = self.budget.events.select_related("actor").order_by("-created_at")
        return ctx


class BudgetDeleteView(LoginRequiredMixin, BudgetFeatureRequiredMixin, TemplateView):
    template_name = "core/budget_confirm_delete.html"

    def dispatch(self, request, *args, **kwargs):
        self.budget = get_object_or_404(
            BudgetRequest.objects.visible_to(request.user),
            pk=kwargs["pk"],
        )
        if not _user_can_delete_budget(self.budget, request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["budget"] = self.budget
        ctx["cancel_url"] = reverse("core:budget_detail", args=[self.budget.pk])
        return ctx

    def post(self, request, *args, **kwargs):
        self._notify_reviewers(self.budget, request.user)
        self.budget.delete()
        messages.error(request, _("Budget request deleted."))
        return redirect("core:budget_list")

    def _notify_reviewers(self, budget: BudgetRequest, actor):
        if budget.building_id:
            memberships = BuildingMembership.objects.filter(
                building=budget.building,
                role__in=[MembershipRole.BACKOFFICE, MembershipRole.ADMINISTRATOR],
            )
        else:
            memberships = BuildingMembership.objects.filter(
                building__isnull=True,
                role__in=[MembershipRole.BACKOFFICE, MembershipRole.ADMINISTRATOR],
            )
        memberships = memberships.select_related("user")
        for membership in memberships:
            user = getattr(membership, "user", None)
            if not user or not user.is_active:
                continue
            service = NotificationService(user)
            payload = NotificationPayload(
                key=f"budget:{budget.pk}:deleted",
                category="budgets",
                title=_("Budget request deleted"),
                body=_(
                    "%(requester)s deleted budget request %(budget_id)s for %(building)s."
                )
                % {
                    "requester": actor.get_full_name() or actor.username,
                    "budget_id": budget.pk,
                    "building": getattr(budget.building, "name", _("Unassigned")),
                },
            )
            service.upsert(payload)


class BudgetArchiveView(LoginRequiredMixin, BudgetFeatureRequiredMixin, View):
    def post(self, request, pk: int):
        budget = get_object_or_404(
            BudgetRequest.objects.visible_to(request.user),
            pk=pk,
        )
        self._check_budget_visibility(budget)
        budget.update_spent_amount()
        if budget.is_archived:
            messages.info(request, _("Budget already archived."))
            return redirect("core:budget_detail", pk=budget.pk)
        if not _user_can_archive_budget(budget, request.user):
            messages.error(request, _("Budget can be archived only after it is fully spent."))
            return redirect("core:budget_detail", pk=budget.pk)
        budget.archive(actor=request.user)
        messages.success(request, _("Budget archived."))
        return redirect("core:budget_detail", pk=budget.pk)


class BudgetArchivedListView(LoginRequiredMixin, BudgetFeatureRequiredMixin, TemplateView):
    template_name = "core/budgets_archived.html"
    PER_CHOICES = (25, 50, 100, 200)
    PER_DEFAULT = 25
    SORT_CHOICES = [
        ("archived_desc", _("Archived (Newest first)")),
        ("archived_asc", _("Archived (Oldest first)")),
        ("requester", _("Requester (A → Z)")),
        ("requester_desc", _("Requester (Z → A)")),
    ]

    def dispatch(self, request, *args, **kwargs):
        resolver = CapabilityResolver(request.user)
        if not resolver.has(Capability.APPROVE_BUDGETS):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        base_qs = (
            BudgetRequest.objects.visible_to(request.user)
            .archived()
            .select_related("requester")
            .with_totals()
        )
        budgets = base_qs
        search = (request.GET.get("q") or "").strip()
        if search:
            budgets = budgets.filter(
                models.Q(title__icontains=search)
                | models.Q(description__icontains=search)
                | models.Q(project_code__icontains=search)
                | models.Q(requester__first_name__icontains=search)
                | models.Q(requester__last_name__icontains=search)
                | models.Q(requester__username__icontains=search)
            )
        owner_ids = [oid for oid in base_qs.values_list("requester_id", flat=True).distinct() if oid]
        owner_users = User.objects.filter(pk__in=owner_ids).only("id", "first_name", "last_name", "username")
        owner_label_map = {user.pk: user.get_full_name() or user.get_username() for user in owner_users}
        owner_choices = [
            {"id": str(pk), "label": owner_label_map[pk]}
            for pk in sorted(owner_label_map.keys(), key=lambda key: owner_label_map[key].lower())
        ]
        owner_param = (request.GET.get("owner") or "").strip()
        owner_filter = None
        if owner_param:
            try:
                owner_filter = int(owner_param)
            except (TypeError, ValueError):
                owner_param = ""
                owner_filter = None
        if owner_filter and owner_filter in owner_label_map:
            budgets = budgets.filter(requester_id=owner_filter)
        else:
            owner_param = ""

        archived_from_raw = (request.GET.get("archived_from") or "").strip()
        archived_to_raw = (request.GET.get("archived_to") or "").strip()
        archived_from = parse_date(archived_from_raw) if archived_from_raw else None
        archived_to = parse_date(archived_to_raw) if archived_to_raw else None
        if archived_from and archived_to and archived_to < archived_from:
            archived_from, archived_to = archived_to, archived_from
        if archived_from:
            budgets = budgets.filter(archived_at__date__gte=archived_from)
        if archived_to:
            budgets = budgets.filter(archived_at__date__lte=archived_to)
        has_archived_filter = bool(archived_from or archived_to)

        sort_param = (request.GET.get("sort") or "archived_desc").strip()
        sort_map = {
            "archived_desc": ("-archived_at", "-id"),
            "archived_asc": ("archived_at", "-id"),
            "requester": ("requester__username", "-archived_at"),
            "requester_desc": ("-requester__username", "-archived_at"),
        }
        if sort_param not in sort_map:
            sort_param = "archived_desc"
        budgets = budgets.order_by(*sort_map[sort_param])

        per_param = request.GET.get("per")
        try:
            per_value = int(per_param)
        except (TypeError, ValueError):
            per_value = self.PER_DEFAULT
        if per_value not in self.PER_CHOICES:
            per_value = self.PER_DEFAULT

        groups = []
        group_map = {}
        for budget in budgets:
            key = budget.requester_id or 0
            if key not in group_map:
                owner = budget.requester
                if owner:
                    name = owner.get_full_name() or owner.get_username()
                else:
                    name = _("Unknown requester")
                group = {
                    "owner": owner,
                    "owner_name": name,
                    "budgets": [],
                    "total_requested": Decimal("0.00"),
                    "total_spent": Decimal("0.00"),
                }
                group_map[key] = group
                groups.append(group)
            entry = group_map[key]
            entry["budgets"].append(budget)
            entry["total_requested"] += Decimal(budget.requested_amount or 0)
            entry["total_spent"] += budget.spent_total
        paginator = Paginator(groups, per_value)
        page_number = request.GET.get("page")
        groups_page = paginator.get_page(page_number)
        ctx["owner_groups_page"] = groups_page
        ctx["owner_groups_total"] = paginator.count
        ctx["pagination_query"] = _querystring_without(request, "page")
        ctx["back_url"] = reverse("core:work_orders_archive")
        ctx["requester_total"] = paginator.count
        ctx["q"] = search
        ctx["archived_from"] = archived_from_raw
        ctx["archived_to"] = archived_to_raw
        ctx["has_archived_filter"] = has_archived_filter
        ctx["owner_choices"] = owner_choices
        ctx["owner_filter"] = owner_param
        ctx["sort"] = sort_param
        ctx["sort_choices"] = self.SORT_CHOICES
        ctx["per"] = per_value
        ctx["per_choices"] = self.PER_CHOICES
        ctx["per_default"] = self.PER_DEFAULT
        can_purge = user_is_admin_or_backoffice(self.request.user)
        ctx["can_purge_archives"] = can_purge
        if can_purge:
            ctx["archive_purge_form"] = ArchivePurgeForm()
            ctx["archive_purge_action"] = reverse("core:budget_archived_purge")
            ctx["archive_purge_preview_url"] = reverse("core:budget_archived_purge_preview")
            ctx["archive_requester_delete_action"] = reverse("core:budget_archived_requester_delete")
        return ctx


class BudgetArchivePurgeView(LoginRequiredMixin, BudgetFeatureRequiredMixin, View):
    form_class = ArchivePurgeForm

    def dispatch(self, request, *args, **kwargs):
        resolver = CapabilityResolver(request.user)
        if not resolver.has(Capability.APPROVE_BUDGETS) or not user_is_admin_or_backoffice(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        form = self.form_class(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect("core:budget_archived_list")
        if not form.cleaned_data.get("confirm"):
            messages.error(request, _("Please confirm the permanent deletion."))
            return redirect("core:budget_archived_list")

        start = form.cleaned_data["from_date"]
        end = form.cleaned_data["to_date"]
        qs = (
            BudgetRequest.objects.visible_to(request.user)
            .filter(archived_at__isnull=False)
        )
        if start:
            qs = qs.filter(archived_at__date__gte=start)
        if end:
            qs = qs.filter(archived_at__date__lte=end)
        deleted = qs.count()
        if not deleted:
            messages.info(request, _("No archived budgets matched the selected date range."))
            return redirect("core:budget_archived_list")
        qs.delete()
        if deleted:
            messages.success(
                request,
                ngettext(
                    "Deleted %(count)s archived budget permanently.",
                    "Deleted %(count)s archived budgets permanently.",
                    deleted,
                )
                % {"count": deleted},
            )
        else:
            messages.info(request, _("No archived budgets matched the selected date range."))
        return redirect("core:budget_archived_list")


class BudgetArchivePurgePreviewView(LoginRequiredMixin, BudgetFeatureRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        resolver = CapabilityResolver(request.user)
        if not resolver.has(Capability.APPROVE_BUDGETS) or not user_is_admin_or_backoffice(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = ArchivePurgeForm(request.GET)
        if not form.is_valid():
            return JsonResponse({"count": 0}, status=400)
        start = form.cleaned_data["from_date"]
        end = form.cleaned_data["to_date"]
        qs = (
            BudgetRequest.objects.visible_to(request.user)
            .filter(archived_at__isnull=False)
        )
        if start:
            qs = qs.filter(archived_at__date__gte=start)
        if end:
            qs = qs.filter(archived_at__date__lte=end)
        return JsonResponse({"count": qs.count()})


class BudgetArchivedRequesterDeleteView(LoginRequiredMixin, BudgetFeatureRequiredMixin, View):
    """
    Permanently delete archived budgets for selected requesters.
    """

    def dispatch(self, request, *args, **kwargs):
        resolver = CapabilityResolver(request.user)
        if not resolver.has(Capability.APPROVE_BUDGETS) or not user_is_admin_or_backoffice(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        requester_ids_raw = request.POST.getlist("requester_ids")
        requester_ids: list[int] = []
        for value in requester_ids_raw:
            try:
                requester_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        requester_ids = sorted(set(requester_ids))
        if not requester_ids:
            messages.error(request, _("Select at least one requester."))
            return redirect("core:budget_archived_list")
        if not request.POST.get("confirm"):
            messages.error(request, _("Please confirm the permanent deletion."))
            return redirect("core:budget_archived_list")

        qs = (
            BudgetRequest.objects.visible_to(request.user)
            .filter(archived_at__isnull=False, requester_id__in=requester_ids)
        )
        deleted = qs.count()
        if not deleted:
            messages.info(request, _("No archived budgets matched the selected requesters."))
            return redirect("core:budget_archived_list")
        qs.delete()
        messages.success(
            request,
            ngettext(
                "Deleted %(count)s archived budget permanently.",
                "Deleted %(count)s archived budgets permanently.",
                deleted,
            )
            % {"count": deleted},
        )
        return redirect("core:budget_archived_list")


class BudgetTechnicianSummaryView(LoginRequiredMixin, BudgetFeatureRequiredMixin, TemplateView):
    template_name = "core/budget_technicians.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = BudgetRequest.objects.visible_to(self.request.user)
        approved_expr = models.Case(
            models.When(approved_amount__isnull=False, then=models.F("approved_amount")),
            default=models.F("requested_amount"),
            output_field=models.DecimalField(max_digits=12, decimal_places=2),
        )
        totals = (
            qs.values("requester_id")
            .annotate(
                budgets_count=models.Count("id"),
                requested_total=models.Sum("requested_amount"),
                approved_total=models.Sum(approved_expr),
                spent_total=models.Sum("spent_amount"),
            )
            .order_by("-requested_total")
        )
        user_ids = [row["requester_id"] for row in totals if row["requester_id"]]
        user_map = {
            user.pk: user
            for user in User.objects.filter(pk__in=user_ids).only("id", "first_name", "last_name", "username")
        }
        rows = []
        for row in totals:
            user = user_map.get(row["requester_id"])
            display_name = user.get_full_name() or user.get_username() if user else _("Unknown")
            rows.append(
                {
                    "user": user,
                    "name": display_name,
                    "budgets_count": row["budgets_count"],
                    "requested_total": row["requested_total"] or 0,
                    "approved_total": row["approved_total"] or 0,
                    "spent_total": row["spent_total"] or 0,
                }
            )
        ctx["technician_rows"] = rows
        expense_prefetch = Prefetch(
            "expenses",
            queryset=Expense.objects.select_related("expense_type").order_by("-incurred_on", "-id"),
        )
        my_budgets = (
            BudgetRequest.objects.filter(requester=self.request.user)
            .select_related("building")
            .prefetch_related(expense_prefetch)
            .order_by("-created_at")
        )
        ctx["my_budgets"] = my_budgets
        ctx["my_expenses"] = (
            Expense.objects.filter(budget_request__requester=self.request.user)
            .select_related("budget_request", "expense_type")
            .order_by("-incurred_on", "-id")[:50]
        )
        return ctx


class BudgetExportView(LoginRequiredMixin, BudgetFeatureRequiredMixin, View):
    def get(self, request, *args, **kwargs) -> HttpResponse:
        resolver = CapabilityResolver(request.user)
        if not resolver.has(Capability.EXPORT_BUDGETS):
            raise Http404()
        qs = (
            BudgetRequest.objects.visible_to(request.user)
            .select_related("building", "requester")
            .with_totals()
        )
        exporter = BudgetExporter(qs)
        return exporter.as_csv_response()


class BudgetTimelineApiView(LoginRequiredMixin, BudgetFeatureRequiredMixin, View):
    def get(self, request, pk: int):
        budget = get_object_or_404(
            BudgetRequest.objects.visible_to(request.user).prefetch_related("events__actor"),
            pk=pk,
        )
        timeline = []
        for event in budget.events.select_related("actor").order_by("-created_at"):
            timeline.append(
                {
                    "id": event.pk,
                    "type": event.event_type,
                    "notes": event.notes,
                    "actor": getattr(event.actor, "get_full_name", lambda: None)() or getattr(event.actor, "username", ""),
                    "created_at": event.created_at.isoformat(),
                    "payload": event.payload,
                }
            )
        return JsonResponse({"results": timeline})
