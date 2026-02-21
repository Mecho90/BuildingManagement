from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Case, Count, DecimalField, ExpressionWrapper, F, Q, Sum, When
from django.db.models.functions import Lower
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views.generic import CreateView, DeleteView, FormView, ListView, UpdateView

from ..forms import AdminUserCreateForm, AdminUserPasswordForm, AdminUserUpdateForm
from ..models import Building, BuildingMembership, BudgetRequest, MembershipRole, WorkOrder
from .common import AdminRequiredMixin, _querystring_without

User = get_user_model()


__all__ = [
    "AdminUserListView",
    "AdminUserCreateView",
    "AdminUserUpdateView",
    "AdminUserPasswordView",
    "AdminUserDeleteView",
]

class AdminUserListView(LoginRequiredMixin, AdminRequiredMixin, ListView):
    model = User
    template_name = "core/users_list.html"
    context_object_name = "users"
    paginate_by = 25

    _per_choices = (25, 50, 100, 200)

    def get_paginate_by(self, queryset):
        try:
            per = int(self.request.GET.get("per", self.paginate_by))
        except (TypeError, ValueError):
            per = self.paginate_by
        if per not in self._per_choices:
            per = self.paginate_by
        self._per = per
        return per

    def get_queryset(self):
        qs = User.objects.all().order_by("username")
        search = (self.request.GET.get("q") or "").strip()
        if search:
            qs = qs.filter(
                Q(username__icontains=search)
                | Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        self._search = search

        owner_param = (self.request.GET.get("owner") or "").strip()
        owner_options = self._get_owner_options()
        owner_ids = {opt["value"] for opt in owner_options}
        if owner_param and owner_param in owner_ids:
            qs = qs.filter(pk=int(owner_param))
            self._owner_filter = owner_param
        else:
            self._owner_filter = ""
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = getattr(self, "_search", "")
        ctx["pagination_query"] = _querystring_without(self.request, "page")
        ctx["per"] = getattr(self, "_per", self.paginate_by)
        ctx["owner_options"] = self._get_owner_options()
        ctx["owner_filter"] = getattr(self, "_owner_filter", "")
        object_list = list(ctx.get("object_list") or [])
        self._attach_roles(object_list)
        overview_data = self._build_overview(object_list)
        if overview_data:
            ctx["overview_rows"] = overview_data["rows"]
            ctx["overview_totals"] = overview_data["totals"]
        paginator = ctx.get("paginator")
        if paginator is not None:
            ctx["users_total"] = paginator.count
        else:
            ctx["users_total"] = len(object_list)
        return ctx

    def _get_owner_options(self) -> list[dict[str, str]]:
        if hasattr(self, "_owner_options"):
            return self._owner_options
        owner_ids = (
            Building.objects.exclude(owner__isnull=True)
            .values_list("owner_id", flat=True)
            .distinct()
        )
        owner_ids = [oid for oid in owner_ids if oid]
        if not owner_ids:
            self._owner_options = []
            return self._owner_options
        owners = (
            User.objects.filter(pk__in=owner_ids)
            .order_by(Lower("first_name"), Lower("last_name"), Lower("username"))
        )
        self._owner_options = [
            {
                "value": str(owner.pk),
                "label": owner.get_full_name() or owner.username,
            }
            for owner in owners
        ]
        return self._owner_options

    def _attach_roles(self, users):
        user_ids = [user.pk for user in users if user.pk]
        if not user_ids:
            return
        labels = dict(MembershipRole.choices)
        memberships = BuildingMembership.objects.filter(
            user_id__in=user_ids,
            building__isnull=True,
        ).values("user_id", "role")
        role_map: dict[int, list[str]] = {user_id: [] for user_id in user_ids}
        for membership in memberships:
            role_map.setdefault(membership["user_id"], []).append(labels.get(membership["role"], membership["role"]))
        for user in users:
            setattr(user, "global_roles", role_map.get(user.pk, []))

    def _build_overview(self, users) -> dict[str, object] | None:
        if not users:
            return None

        user_ids = [user.pk for user in users if user.pk is not None]
        if not user_ids:
            return None

        def _empty_stats():
            return {
                "buildings": 0,
                "units": 0,
                "priority_high": 0,
                "priority_medium": 0,
                "priority_low": 0,
                "archived": 0,
                "budget_remaining": Decimal("0.00"),
            }

        aggregated_stats = (
            User.objects.filter(pk__in=user_ids)
            .annotate(
                buildings_total=Count("buildings", distinct=True),
                units_total=Count("buildings__units", distinct=True),
            )
            .values(
                "pk",
                "buildings_total",
                "units_total",
            )
        )

        stats = {
            row["pk"]: {
                **_empty_stats(),
                "buildings": row["buildings_total"],
                "units": row["units_total"],
            }
            for row in aggregated_stats
        }
        self._apply_work_order_stats(stats, user_ids)
        self._apply_budget_stats(stats, user_ids)

        totals = defaultdict(int)
        overview_rows: list[dict[str, object]] = []
        for user in users:
            user_stats = stats.get(user.pk)
            if not user_stats:
                user_stats = _empty_stats()
            for key, value in user_stats.items():
                if key == "budget_remaining":
                    current = totals.get(key, Decimal("0.00"))
                    totals[key] = Decimal(current) + Decimal(value or 0)
                else:
                    totals[key] += value
            overview_rows.append(
                {
                    "user": user,
                    "is_admin": user.is_superuser,
                    **user_stats,
                }
            )

        return {"rows": overview_rows, "totals": dict(totals)}

    def _apply_work_order_stats(self, stats: dict[int, dict[str, int]], user_ids: list[int]) -> None:
        if not stats:
            return

        work_orders = (
            WorkOrder.objects.filter(
                Q(building__owner_id__in=user_ids) | Q(created_by_id__in=user_ids)
            )
            .values(
                "lawyer_only",
                "created_by_id",
                "building__owner_id",
                "priority",
                "archived_at",
            )
        )

        for row in work_orders:
            responsible_id = row["building__owner_id"]
            if row["lawyer_only"] and row["created_by_id"] in stats:
                responsible_id = row["created_by_id"]
            if responsible_id not in stats or responsible_id is None:
                continue
            if row["archived_at"]:
                stats[responsible_id]["archived"] += 1
                continue
            priority = row["priority"]
            if priority == WorkOrder.Priority.HIGH:
                stats[responsible_id]["priority_high"] += 1
            elif priority == WorkOrder.Priority.MEDIUM:
                stats[responsible_id]["priority_medium"] += 1
            elif priority == WorkOrder.Priority.LOW:
                stats[responsible_id]["priority_low"] += 1

    def _apply_budget_stats(self, stats: dict[int, dict[str, object]], user_ids: list[int]) -> None:
        if not stats:
            return
        if not user_ids:
            return
        amount_field = DecimalField(max_digits=12, decimal_places=2)
        approved_expr = Case(
            When(approved_amount__isnull=False, then=F("approved_amount")),
            default=F("requested_amount"),
            output_field=amount_field,
        )
        remaining_expr = ExpressionWrapper(
            approved_expr - F("spent_amount"),
            output_field=amount_field,
        )
        budgets = (
            BudgetRequest.objects.filter(requester_id__in=user_ids)
            .values("requester_id")
            .annotate(remaining_total=Sum(remaining_expr))
        )
        for row in budgets:
            user_id = row.get("requester_id")
            if user_id not in stats:
                continue
            stats[user_id]["budget_remaining"] = row.get("remaining_total") or Decimal("0.00")


class AdminUserCreateView(LoginRequiredMixin, AdminRequiredMixin, CreateView):
    model = User
    form_class = AdminUserCreateForm
    template_name = "core/user_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("User created."))
        return response

    def get_success_url(self):
        return reverse("core:users_list")


class AdminUserUpdateView(LoginRequiredMixin, AdminRequiredMixin, UpdateView):
    model = User
    form_class = AdminUserUpdateForm
    template_name = "core/user_form.html"
    context_object_name = "managed_user"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.warning(self.request, _("User updated."))
        return response

    def get_success_url(self):
        return reverse("core:users_list")


class AdminUserPasswordView(LoginRequiredMixin, AdminRequiredMixin, FormView):
    template_name = "core/user_password_form.html"
    form_class = AdminUserPasswordForm

    def dispatch(self, request, *args, **kwargs):
        self.user_obj = get_object_or_404(User, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.user_obj
        return kwargs

    def form_valid(self, form):
        form.save()
        messages.success(self.request, _("Password reset successfully."))
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse("core:users_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["managed_user"] = self.user_obj
        return ctx


class AdminUserDeleteView(LoginRequiredMixin, AdminRequiredMixin, DeleteView):
    model = User
    template_name = "core/user_confirm_delete.html"
    success_url = reverse_lazy("core:users_list")

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.pk == request.user.pk:
            messages.error(request, _("You cannot delete your own account."))
            return HttpResponseRedirect(self.success_url)
        messages.error(request, _("User deleted."))
        return super().post(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        # Ensure we destroy the object after messaging in post()
        return super().delete(request, *args, **kwargs)
