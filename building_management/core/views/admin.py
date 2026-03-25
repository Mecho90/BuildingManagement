from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import (
    BooleanField,
    CharField,
    Case,
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Sum,
    When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Lower, Cast
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _, ngettext
from django.views.generic import CreateView, DeleteView, FormView, ListView, UpdateView

from ..forms import (
    AdminUserCreateForm,
    AdminUserPasswordForm,
    AdminUserUpdateForm,
    BuildingBulkDeleteForm,
    WorkOrderBulkArchiveForm,
    WorkOrderBulkDeleteForm,
    UserBulkDeleteForm,
)
from ..models import Building, BuildingMembership, BudgetRequest, Expense, MembershipRole, Unit, WorkOrder
from .common import AdminRequiredMixin, _querystring_without

User = get_user_model()


__all__ = [
    "AdminUserListView",
    "AdminUserCreateView",
    "AdminUserUpdateView",
    "AdminUserPasswordView",
    "AdminUserDeleteView",
    "AdminBuildingBulkDeleteView",
    "AdminWorkOrderBulkDeleteView",
    "AdminWorkOrderBulkArchiveView",
    "AdminLawyerWorkOrderBulkDeleteView",
    "AdminUserBulkDeleteView",
]


def _inject_form_error_summary(ctx: dict) -> dict:
    form = ctx.get("form")
    invalid_field_items: list[dict[str, str]] = []
    if form is not None and getattr(form, "is_bound", False):
        for bound_field in form:
            if bound_field.errors:
                invalid_field_items.append(
                    {
                        "id": bound_field.id_for_label or "",
                        "label": str(bound_field.label or bound_field.name),
                    }
                )
    ctx["invalid_field_items"] = invalid_field_items
    ctx["invalid_field_count"] = len(invalid_field_items)
    return ctx


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
        def _chip_remove_url(*keys):
            params = self.request.GET.copy()
            for key in keys:
                if key in params:
                    del params[key]
            if "page" in params:
                del params["page"]
            encoded = params.urlencode()
            return f"{self.request.path}?{encoded}" if encoded else self.request.path

        active_filter_chips: list[dict[str, str]] = []
        search_value = (ctx["q"] or "").strip()
        if search_value:
            active_filter_chips.append(
                {
                    "label": _("Search: %(value)s") % {"value": search_value},
                    "remove_url": _chip_remove_url("q"),
                }
            )
        owner_value = (ctx["owner_filter"] or "").strip()
        if owner_value:
            owner_label = ""
            for option in ctx["owner_options"]:
                if option.get("value") == owner_value:
                    owner_label = option.get("label") or owner_value
                    break
            active_filter_chips.append(
                {
                    "label": _("Owner: %(value)s") % {"value": owner_label or owner_value},
                    "remove_url": _chip_remove_url("owner"),
                }
            )
        per_value = int(ctx["per"] or self.paginate_by)
        if per_value != self.paginate_by:
            active_filter_chips.append(
                {
                    "label": _("Page size: %(value)s") % {"value": per_value},
                    "remove_url": _chip_remove_url("per"),
                }
            )
        ctx["active_filter_chips"] = active_filter_chips
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

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        return _inject_form_error_summary(ctx)


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

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        role_labels = dict(MembershipRole.choices)
        current_roles = list(
            BuildingMembership.objects.filter(
                user=self.object,
                building__isnull=True,
            )
            .values_list("role", flat=True)
            .distinct()
        )
        if not current_roles and getattr(self.object, "is_superuser", False):
            current_roles = [MembershipRole.ADMINISTRATOR]
        ctx["current_global_roles"] = [role_labels.get(role, role) for role in current_roles]
        return _inject_form_error_summary(ctx)


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


class BulkDeleteView(LoginRequiredMixin, AdminRequiredMixin, FormView):
    template_name = "core/mass_delete.html"
    form_class = None
    page_title = ""
    intro_text = ""
    empty_text = ""
    submit_label = ""
    success_url_name = ""
    warning_text = _("Deletion is permanent and cannot be undone.")
    left_actions_layout = False
    full_width_page_size = False
    items_grid_class = "space-y-3"
    paginate_choices = (25, 50, 100, 200)
    default_paginate_by = 25

    def _get_request_value(self, param):
        value = self.request.GET.get(param)
        if value is None and self.request.method == "POST":
            value = self.request.POST.get(param)
        return value

    def get_base_queryset(self):
        if hasattr(self, "_base_queryset"):
            return self._base_queryset
        self._base_queryset = self.get_queryset()
        return self._base_queryset

    def get_success_url(self):
        return reverse(self.success_url_name)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["queryset"] = self.get_base_queryset()
        kwargs["display_queryset"] = self.get_paginated_items()
        return kwargs

    def get_queryset(self):
        raise NotImplementedError("BulkDeleteView subclasses must implement get_queryset().")

    def get_page_size(self):
        if hasattr(self, "_page_size"):
            return self._page_size
        raw = self._get_request_value("per")
        try:
            per = int(raw) if raw not in (None, "") else int(self.default_paginate_by)
        except (TypeError, ValueError):
            per = self.default_paginate_by
        if per not in self.paginate_choices:
            per = self.default_paginate_by
        self._page_size = per
        return per

    def get_page_number(self):
        if hasattr(self, "_page_number"):
            return self._page_number
        raw = self._get_request_value("page")
        try:
            page = int(raw) if raw not in (None, "") else 1
        except (TypeError, ValueError):
            page = 1
        if page < 1:
            page = 1
        self._page_number = page
        return page

    def get_paginated_items(self):
        if hasattr(self, "_paginated_items"):
            return self._paginated_items
        queryset = self.get_base_queryset()
        paginator = Paginator(queryset, self.get_page_size())
        page_obj = paginator.get_page(self.get_page_number())
        self._paginator = paginator
        self._page_obj = page_obj
        self._paginated_items = page_obj.object_list
        return self._paginated_items

    def form_valid(self, form):
        queryset = form.cleaned_data["items"]
        count = queryset.count()
        if count:
            queryset.delete()
            messages.success(
                self.request,
                ngettext(
                    "Deleted %(count)s record.",
                    "Deleted %(count)s records.",
                    count,
                )
                % {"count": count},
            )
        else:
            messages.info(self.request, _("No records selected."))
        return HttpResponseRedirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx.get("form")
        paginator = getattr(self, "_paginator", None)
        total_count = paginator.count if paginator else 0
        has_options = total_count > 0
        ctx.update(
            {
                "page_title": self.page_title,
                "intro_text": self.intro_text,
                "empty_text": self.empty_text,
                "submit_label": self.submit_label or _("Delete selected"),
                "has_options": has_options,
                "page_obj": getattr(self, "_page_obj", None),
                "paginator": paginator,
                "per": self.get_page_size(),
                "per_choices": self.paginate_choices,
                "page_query": _querystring_without(self.request, "page"),
                "warning_text": self.warning_text,
                "left_actions_layout": self.left_actions_layout,
                "full_width_page_size": self.full_width_page_size,
                "items_grid_class": self.items_grid_class,
            }
        )
        return ctx


class AdminBuildingBulkDeleteView(BulkDeleteView):
    form_class = BuildingBulkDeleteForm
    page_title = _("Mass delete buildings")
    intro_text = ""
    empty_text = _("There are no buildings available to delete.")
    submit_label = _("Delete")
    success_url_name = "core:mass_delete_buildings"
    left_actions_layout = True
    full_width_page_size = True
    items_grid_class = "grid gap-3 sm:grid-cols-2 lg:grid-cols-4"

    def get_queryset(self):
        budget_exists = BudgetRequest.objects.filter(building_id=OuterRef("pk"))
        expense_exists = Expense.objects.annotate(
            metadata_building_id_text=KeyTextTransform("building_id", "metadata"),
        ).filter(
            Q(metadata_building_id_text=Cast(OuterRef("pk"), CharField()))
            | Q(budget_request__building_id=OuterRef("pk"))
        )
        lawsuit_exists = WorkOrder.objects.filter(lawyer_only=True).filter(
            Q(building_id=OuterRef("pk")) | Q(forwarded_to_building_id=OuterRef("pk"))
        )
        qs = (
            Building.objects.visible_to(self.request.user)
            .annotate(
                total_units=Count("units", distinct=True),
                total_work_orders=Count("work_orders", distinct=True),
                has_budgets=ExpressionWrapper(
                    Exists(budget_exists) | Exists(expense_exists),
                    output_field=BooleanField(),
                ),
                has_lawsuits=Exists(lawsuit_exists),
            )
            .order_by(Lower("name"))
        )
        office_id = Building.system_default_id()
        if office_id:
            qs = qs.exclude(pk=office_id)
        return qs

    def form_valid(self, form):
        queryset = form.cleaned_data["items"]
        building_ids = list(queryset.values_list("pk", flat=True))
        if building_ids:
            with transaction.atomic():
                WorkOrder.objects.filter(building_id__in=building_ids).delete()
                Unit.objects.filter(building_id__in=building_ids).delete()
                return super().form_valid(form)
        return super().form_valid(form)


class AdminWorkOrderBulkDeleteView(BulkDeleteView):
    form_class = WorkOrderBulkDeleteForm
    page_title = _("Mass delete work orders")
    intro_text = ""
    empty_text = _("There are no work orders available to delete.")
    submit_label = _("Delete")
    success_url_name = "core:mass_delete_work_orders"
    left_actions_layout = True
    full_width_page_size = True
    items_grid_class = "grid gap-3 sm:grid-cols-2 lg:grid-cols-4"

    def get_queryset(self):
        return (
            WorkOrder.objects.visible_to(self.request.user)
            .select_related("building")
            .order_by("-created_at")
        )


class AdminWorkOrderBulkArchiveView(BulkDeleteView):
    form_class = WorkOrderBulkArchiveForm
    page_title = _("Mass archive work orders")
    intro_text = ""
    empty_text = _("There are no completed work orders available to archive.")
    submit_label = _("Save")
    success_url_name = "core:mass_archive_work_orders"
    warning_text = _("Archiving moves selected work orders to the archive list.")
    left_actions_layout = True
    full_width_page_size = True
    items_grid_class = "grid gap-3 sm:grid-cols-2 lg:grid-cols-4"

    def get_queryset(self):
        return (
            WorkOrder.objects.visible_to(self.request.user)
            .filter(archived_at__isnull=True, status=WorkOrder.Status.DONE)
            .select_related("building")
            .order_by("-created_at")
        )

    def form_valid(self, form):
        queryset = form.cleaned_data["items"]
        archived_count = 0
        if queryset.exists():
            with transaction.atomic():
                for order in queryset.select_related("building", "forwarded_to_building"):
                    if order.archived_at is not None or order.status != WorkOrder.Status.DONE:
                        continue
                    order.archive()
                    archived_count += 1
        if archived_count:
            messages.success(
                self.request,
                ngettext(
                    "Archived %(count)s work order.",
                    "Archived %(count)s work orders.",
                    archived_count,
                )
                % {"count": archived_count},
            )
        else:
            messages.info(self.request, _("No eligible work orders were selected."))
        return HttpResponseRedirect(self.get_success_url())


class AdminLawyerWorkOrderBulkDeleteView(BulkDeleteView):
    form_class = WorkOrderBulkDeleteForm
    page_title = _("Mass delete lawyer work orders")
    intro_text = ""
    empty_text = _("There are no lawyer work orders available to delete.")
    submit_label = _("Delete")
    success_url_name = "core:mass_delete_lawyer_work_orders"
    left_actions_layout = True
    full_width_page_size = True
    items_grid_class = "grid gap-3 sm:grid-cols-2 lg:grid-cols-4"

    def get_queryset(self):
        return (
            WorkOrder.objects.visible_to(self.request.user)
            .filter(
                lawyer_only=True,
                archived_at__isnull=True,
                status__in=(WorkOrder.Status.OPEN, WorkOrder.Status.IN_PROGRESS),
            )
            .select_related("building")
            .order_by("-created_at")
        )


class AdminUserBulkDeleteView(BulkDeleteView):
    form_class = UserBulkDeleteForm
    page_title = _("Mass delete users")
    intro_text = _("Select the users you want to delete permanently. Your account never appears in this list.")
    empty_text = _("There are no additional user accounts available to delete.")
    submit_label = _("Delete users")
    success_url_name = "core:mass_delete_users"

    def get_queryset(self):
        return User.objects.exclude(pk=self.request.user.pk).order_by(Lower("username"))
