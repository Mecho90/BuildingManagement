from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import models
from django.db.models import Prefetch
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy, reverse
from django.utils.translation import gettext as _
from django.views.generic import DetailView, FormView, ListView, TemplateView, CreateView, UpdateView, View
from django.core.paginator import Paginator

from ..authz import Capability, CapabilityResolver
from ..forms import (
    BudgetExpenseForm,
    BudgetFilterForm,
    BudgetRequestApprovalForm,
    BudgetRequestForm,
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
from .common import CapabilityRequiredMixin, _querystring_without

User = get_user_model()


def _user_can_delete_budget(budget: BudgetRequest, user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
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
        resolver = CapabilityResolver(self.request.user)
        budget_qs = (
            BudgetRequest.objects.visible_to(self.request.user)
            .active()
            .select_related("building", "requester")
            .with_totals()
        )
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
                    | models.Q(notes__icontains=query)
                    | models.Q(project_code__icontains=query)
                    | models.Q(title__icontains=query)
                )
        totals_expr = models.Case(
            models.When(approved_amount__isnull=False, then=models.F("approved_amount")),
            default=models.F("requested_amount"),
            output_field=models.DecimalField(max_digits=12, decimal_places=2),
        )
        totals = budget_qs.aggregate(
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
        paginator = Paginator(budget_qs, 20)
        page_number = self.request.GET.get("page") or 1
        page_obj = paginator.get_page(page_number)
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
        can_log_expense = self._can_log_expense(budget)
        ctx["can_log_expense"] = can_log_expense
        if can_log_expense:
            ctx["expense_form"] = BudgetExpenseForm(user=self.request.user, budget=budget)
        ctx["events"] = budget.events.select_related("actor")
        resolver = CapabilityResolver(self.request.user)
        ctx["can_review_budget"] = resolver.has(
            Capability.APPROVE_BUDGETS,
            building_id=budget.building_id,
        )
        ctx["can_delete_budget"] = _user_can_delete_budget(budget, self.request.user)
        ctx["can_archive_budget"] = _user_can_archive_budget(budget, self.request.user)
        ctx["archive_budget_url"] = reverse("core:budget_archive", args=[budget.pk])
        return ctx

    def _can_log_expense(self, budget: BudgetRequest) -> bool:
        if budget.status != BudgetRequest.Status.APPROVED:
            return False
        if budget.requester_id == getattr(self.request.user, "pk", None):
            return True
        resolver = CapabilityResolver(self.request.user)
        building_id = getattr(budget.building, "pk", None)
        return resolver.has(Capability.MANAGE_BUDGETS, building_id=building_id)


class BudgetCreateView(LoginRequiredMixin, BudgetFeatureRequiredMixin, CreateView):
    template_name = "core/budget_form.html"
    form_class = BudgetRequestForm
    success_url = reverse_lazy("core:budget_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        self.object.transition(
            status=BudgetRequest.Status.PENDING_REVIEW,
            actor=self.request.user,
            comment=self.object.notes or "",
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
        if not self._can_log_expense(self.budget, request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["budget"] = self.budget
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.save()
        messages.success(self.request, _("Expense logged."))
        return redirect(self.get_success_url())

    def form_invalid(self, form):
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(self.request, error)
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("core:budget_detail", args=[self.budget.pk])

    def _can_log_expense(self, budget, user) -> bool:
        if budget.status != BudgetRequest.Status.APPROVED:
            return False
        if budget.requester_id == getattr(user, "pk", None):
            return True
        resolver = CapabilityResolver(user)
        return resolver.has(Capability.MANAGE_BUDGETS, building_id=budget.building_id)


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
        return qs.order_by("-updated_at")


class BudgetReviewDecisionView(LoginRequiredMixin, BudgetFeatureRequiredMixin, FormView):
    form_class = BudgetRequestApprovalForm
    template_name = "core/budget_review_detail.html"

    def dispatch(self, request, *args, **kwargs):
        self.budget = get_object_or_404(
            BudgetRequest.objects.visible_to(request.user),
            pk=kwargs["pk"],
        )
        resolver = CapabilityResolver(request.user)
        if not resolver.has(Capability.APPROVE_BUDGETS, building_id=self.budget.building_id):
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
    PER_CHOICES = (10, 25, 50, 100)
    PER_DEFAULT = 25

    def dispatch(self, request, *args, **kwargs):
        resolver = CapabilityResolver(request.user)
        if not resolver.has(Capability.APPROVE_BUDGETS):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        budgets = (
            BudgetRequest.objects.visible_to(self.request.user)
            .archived()
            .select_related("requester")
            .with_totals()
            .order_by("requester__username", "-archived_at")
        )
        requester_summaries = budgets.values(
            "requester_id",
            "requester__first_name",
            "requester__last_name",
            "requester__username",
        ).annotate(
            total_requested=models.Sum("requested_amount"),
            total_spent=models.Sum("spent_amount"),
        ).order_by("requester__username", "requester_id")
        try:
            per_value = int(self.request.GET.get("per", self.PER_DEFAULT))
        except (TypeError, ValueError):
            per_value = self.PER_DEFAULT
        if per_value not in self.PER_CHOICES:
            per_value = self.PER_DEFAULT
        page_obj = Paginator(requester_summaries, per_value).get_page(self.request.GET.get("page") or 1)
        requester_ids = [row["requester_id"] for row in page_obj.object_list if row["requester_id"]]
        budgets_by_requester: dict[int, list[BudgetRequest]] = {}
        owner_map: dict[int, object] = {}
        if requester_ids:
            for budget in budgets.filter(requester_id__in=requester_ids):
                budgets_by_requester.setdefault(budget.requester_id, []).append(budget)
                if budget.requester_id not in owner_map:
                    owner_map[budget.requester_id] = budget.requester
        groups = []
        for row in page_obj.object_list:
            requester_id = row.get("requester_id")
            owner = owner_map.get(requester_id) if requester_id else None
            owner_name = " ".join(
                [part for part in [row.get("requester__first_name"), row.get("requester__last_name")] if part]
            ).strip()
            if not owner_name:
                owner_name = row.get("requester__username") or _("Unknown requester")
            groups.append(
                {
                    "owner": owner,
                    "owner_name": owner_name,
                    "budgets": budgets_by_requester.get(requester_id, []),
                    "total_requested": row.get("total_requested") or Decimal("0.00"),
                    "total_spent": row.get("total_spent") or Decimal("0.00"),
                }
            )
        ctx["owner_groups"] = groups
        ctx["owner_groups_page"] = page_obj
        ctx["pagination_query"] = _querystring_without(self.request, "page")
        ctx["per"] = per_value
        ctx["per_choices"] = self.PER_CHOICES
        ctx["back_url"] = reverse("core:work_orders_archive")
        ctx["requester_total"] = page_obj.paginator.count
        return ctx


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
