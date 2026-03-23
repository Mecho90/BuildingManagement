from __future__ import annotations

import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import BooleanField, Case, Count, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Lower
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.dateparse import parse_date
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from ..authz import Capability, CapabilityResolver, log_role_action
from ..forms import BuildingForm, BuildingMembershipForm, TechnicianSubroleForm, UnitForm
from ..models import (
    Building,
    BuildingMembership,
    MembershipRole,
    RoleAuditLog,
    Unit,
    WorkOrder,
)
from ..utils.metrics import log_duration
from ..utils.roles import user_has_role
from .common import (
    CachedObjectMixin,
    CapabilityRequiredMixin,
    attach_expense_totals_by_metadata,
    _querystring_without,
    _user_can_access_building,
    _user_has_building_capability,
)


__all__ = [
    "BuildingListView",
    "BuildingDetailView",
    "BuildingCreateView",
    "BuildingUpdateView",
    "BuildingDeleteView",
    "BuildingMembershipManageView",
    "BuildingMembershipDeleteView",
    "UnitDetailView",
    "UnitCreateView",
    "UnitUpdateView",
    "UnitDeleteView",
]

logger = logging.getLogger(__name__)
User = get_user_model()


def _user_can_filter_building_owner(user) -> bool:
    # All authenticated users can narrow their view by owner; visibility
    # is still constrained by Building.objects.visible_to in the queryset.
    return bool(user and getattr(user, "is_authenticated", False))

class BuildingListView(LoginRequiredMixin, ListView):
    model = Building
    template_name = "core/buildings_list.html"
    context_object_name = "buildings"

    def get_paginate_by(self, queryset):
        allowed = {25, 50, 100, 200}
        try:
            value = int(self.request.GET.get("per", 25))
        except (TypeError, ValueError):
            value = 25
        return value if value in allowed else 25

    def get_queryset(self):
        user = self.request.user
        resolver = CapabilityResolver(user) if user.is_authenticated else None
        today = timezone.localdate()

        self._ensure_office_building_for_user(user)

        self._can_manage_buildings = resolver.has(Capability.MANAGE_BUILDINGS) if resolver else False
        self._can_filter_owner = _user_can_filter_building_owner(user)

        # Per-user visibility + annotate the exact stats the template uses
        qs = (
            Building.objects.visible_to(user)
            .with_unit_stats()
            .with_lawyer_alerts()
            .annotate(
                occupied_units_count=Count("units", filter=Q(units__is_occupied=True), distinct=True),
                overdue_items_count=Count(
                    "work_orders",
                    filter=Q(
                        work_orders__archived_at__isnull=True,
                        work_orders__deadline__lt=today,
                    ),
                    distinct=True,
                ),
            )
            .select_related("owner")
        )
        office_id = Building.system_default_id()
        office_priority = Case(
            When(pk=office_id, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        qs = qs.annotate(_office_priority=office_priority)

        # Search
        q = (self.request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(address__icontains=q)
                | Q(owner__username__icontains=q)
            )

        owner_param = (self.request.GET.get("owner") or "").strip()
        role_param = (self.request.GET.get("role") or "").strip()
        self._owner_filter = ""
        self._owner_options: list[dict[str, str]] = []
        self._role_filter = ""
        self._role_options = [{"value": value, "label": str(label)} for value, label in Building.Role.choices]
        if self._can_filter_owner:
            owner_ids = list(qs.values_list("owner_id", flat=True).distinct())
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
                if owner.pk
            ]
            if owner_param:
                try:
                    owner_id = int(owner_param)
                except (TypeError, ValueError):
                    owner_param = ""
                else:
                    qs = qs.filter(owner_id=owner_id)
                    self._owner_filter = str(owner_id)
        else:
            owner_param = ""
        valid_roles = {value for value, _ in Building.Role.choices}
        if role_param and role_param in valid_roles:
            qs = qs.filter(role=role_param)
            self._role_filter = role_param

        # Sorting
        sort = (self.request.GET.get("sort") or "name").strip()
        allow_owner_sort = self._can_manage_buildings
        allowed = {
            "name",
            "-name",
            "role",
            "-role",
            "address",
            "-address",
            "units_count",
            "-units_count",
            "work_orders_count",
            "-work_orders_count",
        }
        if allow_owner_sort:
            allowed.update({"owner", "-owner"})
        if sort not in allowed:
            sort = "name"

        # Map template-friendly keys -> annotated field names
        sort_map = {
            "units_count": "_units_count",
            "work_orders_count": "_work_orders_count",
            "owner": "owner__username",
        }
        sort_field = sort
        if sort.lstrip("-") in sort_map:
            base = sort_map[sort.lstrip("-")]
            sort_field = "-" + base if sort.startswith("-") else base

        self._effective_sort = sort
        self._filtered_metrics = qs.aggregate(
            total_buildings=Count("id", distinct=True),
            occupied_units=Sum("occupied_units_count"),
            open_work_orders=Sum("_work_orders_count"),
            overdue_items=Sum("overdue_items_count"),
        )
        return qs.order_by("-_office_priority", "-is_system_default", sort_field, "id")

    def _ensure_office_building_for_user(self, user) -> None:
        if not user or not user.is_authenticated:
            return
        if Building.system_default_id():
            return
        has_global_role = BuildingMembership.objects.filter(
            user=user,
            building__isnull=True,
            role__in=(MembershipRole.ADMINISTRATOR, MembershipRole.BACKOFFICE),
        ).exists()
        if not has_global_role:
            return
        try:
            from ..services.office import ensure_office_building

            ensure_office_building(strict_owner=False)
            Building.system_default_id(force_refresh=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[core] Unable to bootstrap Office building for user %s: %s", user.pk, exc)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["buildings"] = attach_expense_totals_by_metadata(
            ctx.get("buildings"),
            metadata_key="building_id",
            metadata_keys=("building_id", "building"),
            include_budget_building_fallback=True,
            include_work_order_text_fallback=True,
            target_attr="expense_total",
        )
        ctx["q"] = (self.request.GET.get("q") or "").strip()
        try:
            ctx["per"] = int(self.request.GET.get("per", 25))
        except (TypeError, ValueError):
            ctx["per"] = 25
        ctx["sort"] = getattr(self, "_effective_sort", "name")
        can_manage_buildings = getattr(self, "_can_manage_buildings", False)
        ctx["show_owner_column"] = can_manage_buildings
        ctx["can_manage_buildings"] = can_manage_buildings
        ctx["owner_options"] = getattr(self, "_owner_options", [])
        ctx["owner_filter"] = getattr(self, "_owner_filter", "")
        ctx["role_options"] = getattr(self, "_role_options", [])
        ctx["role_filter"] = getattr(self, "_role_filter", "")

        ctx["pagination_query"] = _querystring_without(self.request, "page")
        paginator = ctx.get("paginator")
        if paginator is not None:
            ctx["buildings_total"] = paginator.count
        else:
            object_list = ctx.get("object_list") or []
            ctx["buildings_total"] = len(object_list)
        page_obj = ctx.get("page_obj")
        total_buildings = ctx["buildings_total"]
        ctx["result_start"] = page_obj.start_index() if page_obj and total_buildings else 0
        ctx["result_end"] = page_obj.end_index() if page_obj and total_buildings else 0

        metrics = getattr(self, "_filtered_metrics", {}) or {}
        ctx["summary_total_buildings"] = metrics.get("total_buildings") or 0
        ctx["summary_occupied_units"] = metrics.get("occupied_units") or 0
        ctx["summary_open_work_orders"] = metrics.get("open_work_orders") or 0
        ctx["summary_overdue_items"] = metrics.get("overdue_items") or 0

        def _remove_filter_url(*keys: str) -> str:
            params = self.request.GET.copy()
            for key in keys:
                params.pop(key, None)
            params.pop("page", None)
            encoded = params.urlencode()
            base = reverse("core:buildings_list")
            return f"{base}?{encoded}" if encoded else base

        active_filter_chips: list[dict[str, str]] = []
        q_value = ctx.get("q", "")
        if q_value:
            active_filter_chips.append(
                {"label": _("Search: %(value)s") % {"value": q_value}, "remove_url": _remove_filter_url("q")}
            )
        owner_value = ctx.get("owner_filter", "")
        if owner_value:
            owner_label = owner_value
            for opt in ctx.get("owner_options", []):
                if str(opt.get("value")) == str(owner_value):
                    owner_label = opt.get("label") or owner_label
                    break
            active_filter_chips.append(
                {"label": _("Owner: %(value)s") % {"value": owner_label}, "remove_url": _remove_filter_url("owner")}
            )
        role_value = ctx.get("role_filter", "")
        if role_value:
            role_label = role_value
            for opt in ctx.get("role_options", []):
                if opt.get("value") == role_value:
                    role_label = opt.get("label") or role_label
                    break
            active_filter_chips.append(
                {"label": _("Role: %(value)s") % {"value": role_label}, "remove_url": _remove_filter_url("role")}
            )
        if ctx.get("sort", "name") != "name":
            active_filter_chips.append(
                {"label": _("Sort changed"), "remove_url": _remove_filter_url("sort")}
            )
        if ctx.get("per", 25) != 25:
            active_filter_chips.append(
                {
                    "label": _("Page size: %(value)s") % {"value": ctx.get("per", 25)},
                    "remove_url": _remove_filter_url("per"),
                }
            )
        ctx["active_filter_chips"] = active_filter_chips
        ctx["has_active_filters"] = bool(active_filter_chips)
        return ctx

class BuildingDetailView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DetailView):
    model = Building
    template_name = "core/building_detail.html"
    context_object_name = "building"

    # Only buildings the user may see
    def get_queryset(self):
        return (
            Building.objects.visible_to(self.request.user)
            .with_lawyer_alerts()
        )

    # Owner/assigned members only
    def test_func(self):
        b = self.get_object()
        return _user_can_access_building(self.request.user, b)

    # Helpers ---------------------------------------------------------------

    def _get_int(self, key: str, default: int) -> int:
        try:
            return int(self.request.GET.get(key, default))
        except (TypeError, ValueError):
            return default

    # View ------------------------------------------------------------------

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        bld = self.object
        request = self.request
        today = timezone.localdate()
        building_with_expenses = attach_expense_totals_by_metadata(
            [bld],
            metadata_key="building_id",
            metadata_keys=("building_id", "building"),
            include_budget_building_fallback=True,
            include_work_order_text_fallback=True,
            target_attr="expense_total",
        )
        if building_with_expenses:
            ctx["building"] = building_with_expenses[0]
            bld = ctx["building"]
        total_expense_amount = getattr(bld, "expense_total", Decimal("0.00")) or Decimal("0.00")
        lawyer_orders_count = int(getattr(bld, "lawyer_orders_count", 0) or 0)
        lawyer_orders = attach_expense_totals_by_metadata(
            WorkOrder.objects.visible_to(request.user).filter(
                building=bld,
                archived_at__isnull=True,
                lawyer_only=True,
            ),
            metadata_key="work_order_id",
            metadata_keys=("work_order_id", "work_order"),
            include_work_order_text_fallback=True,
            target_attr="expense_total",
        )
        lawyer_orders_total_expense = sum(
            (
                getattr(work_order, "expense_total", Decimal("0.00")) or Decimal("0.00")
                for work_order in lawyer_orders
            ),
            Decimal("0.00"),
        )
        owner = getattr(bld, "owner", None)
        owner_display = "—"
        if owner:
            owner_display = owner.get_full_name().strip() or owner.username

        total_units = Unit.objects.filter(building=bld).count()
        occupied_units = Unit.objects.filter(building=bld, is_occupied=True).count()
        open_work_orders = WorkOrder.objects.visible_to(request.user).filter(
            building=bld,
            archived_at__isnull=True,
            status__in=[
                WorkOrder.Status.OPEN,
                WorkOrder.Status.IN_PROGRESS,
                WorkOrder.Status.AWAITING_APPROVAL,
            ],
        ).count()
        overdue_work_orders = WorkOrder.objects.visible_to(request.user).filter(
            building=bld,
            archived_at__isnull=True,
            deadline__lt=today,
            status__in=[
                WorkOrder.Status.OPEN,
                WorkOrder.Status.IN_PROGRESS,
                WorkOrder.Status.AWAITING_APPROVAL,
            ],
        ).count()
        if total_units == 0:
            building_status = _("No units")
            building_status_tone = "info"
        elif occupied_units == total_units:
            building_status = _("Fully occupied")
            building_status_tone = "success"
        elif occupied_units > 0:
            building_status = _("Partially occupied")
            building_status_tone = "warning"
        else:
            building_status = _("Vacant")
            building_status_tone = "danger"
        ctx.update(
            {
                "summary_total_units": total_units,
                "summary_occupied_units": occupied_units,
                "summary_open_work_orders": open_work_orders,
                "summary_overdue_work_orders": overdue_work_orders,
                "building_status_label": building_status,
                "building_status_tone": building_status_tone,
                "building_display_name": _("Office") if bld.is_system_default else bld.name,
                "owner_display_name": owner_display,
                "owner_role_label": bld.get_role_display(),
                "total_expense_amount": total_expense_amount,
                "has_total_expense_amount": bool(total_expense_amount and total_expense_amount > 0),
                "lawyer_orders_count": lawyer_orders_count,
                "has_lawyer_orders": lawyer_orders_count > 0,
                "lawyer_orders_total_expense": lawyer_orders_total_expense,
                "has_lawyer_orders_total_expense": bool(
                    lawyer_orders_total_expense and lawyer_orders_total_expense > 0
                ),
            }
        )

        show_units_tab = not bld.is_system_default
        ctx["show_units_tab"] = show_units_tab

        resolver = CapabilityResolver(request.user) if request.user.is_authenticated else None
        is_technician_user = user_has_role(request.user, MembershipRole.TECHNICIAN)
        can_manage_building = resolver.has(Capability.MANAGE_BUILDINGS, building_id=bld.pk) if resolver else False
        can_manage_units = can_manage_building or (resolver.has(Capability.CREATE_UNITS, building_id=bld.pk) if resolver else False)
        can_manage_work_orders = can_manage_building or (resolver.has(Capability.CREATE_WORK_ORDERS, building_id=bld.pk) if resolver else False)
        ctx["can_manage_building"] = can_manage_building
        ctx["can_manage_units"] = can_manage_units
        ctx["can_manage_work_orders"] = can_manage_work_orders
        can_manage_members = resolver.has(Capability.MANAGE_MEMBERSHIPS, building_id=bld.pk) if resolver else False
        ctx["can_manage_memberships"] = can_manage_members
        is_admin_for_building = False
        if request.user.is_authenticated:
            admin_memberships = BuildingMembership.objects.filter(
                user=request.user,
                role=MembershipRole.ADMINISTRATOR,
            ).filter(Q(building__isnull=True) | Q(building=bld))
            is_admin_for_building = admin_memberships.exists()
        ctx["can_delete_building"] = is_admin_for_building or getattr(request.user, "is_superuser", False)
        ctx["show_manage_team_button"] = can_manage_members and is_admin_for_building
        ctx["is_technician_user"] = is_technician_user
        if can_manage_members:
            ctx["memberships_manage_url"] = reverse("core:building_memberships", args=[bld.pk])
        tech_membership = None
        if request.user.is_authenticated:
            tech_membership = BuildingMembership.objects.filter(
                building=bld,
                user=request.user,
                role=MembershipRole.TECHNICIAN,
            ).first()
        ctx["technician_membership"] = tech_membership
        if tech_membership:
            ctx["technician_subrole_label"] = dict(Building.Role.choices).get(tech_membership.technician_subrole, "")
            ctx["technician_subrole_url"] = reverse("core:technician_subrole", args=[bld.pk])

        active_tab = request.GET.get("tab", "").strip().lower()

        def _tab_hint_from_params() -> str:
            work_keys = {"w_q", "w_status", "w_per", "w_page"}
            params = set(request.GET.keys())
            if params & work_keys:
                return "work_orders"
            return "work_orders"

        if active_tab == "overview":
            active_tab = "work_orders"

        if not show_units_tab:
            active_tab = "work_orders"
        elif active_tab not in {"units", "work_orders"}:
            active_tab = _tab_hint_from_params()

        ctx["active_tab"] = active_tab

        def _tab_url(target: str) -> str:
            params = request.GET.copy()
            params["tab"] = target
            if target == "units":
                for key in list(params.keys()):
                    if key.startswith("w_"):
                        params.pop(key, None)
            elif target == "work_orders":
                for key in list(params.keys()):
                    if key.startswith("u_"):
                        params.pop(key, None)
            query = params.urlencode()
            base = request.path
            return f"{base}?{query}" if query else base

        tab_urls = {
            "work_orders": _tab_url("work_orders"),
        }
        if show_units_tab:
            tab_urls["units"] = _tab_url("units")
        ctx["tab_urls"] = tab_urls

        def _remove_filter_url(target_tab: str, *keys: str) -> str:
            params = request.GET.copy()
            params["tab"] = target_tab
            for key in keys:
                params.pop(key, None)
            if target_tab == "units":
                params.pop("u_page", None)
            if target_tab == "work_orders":
                params.pop("w_page", None)
            encoded = params.urlencode()
            return f"{request.path}?{encoded}" if encoded else request.path

        # ========================= Units =========================
        if show_units_tab:
            u_q = (self.request.GET.get("u_q") or "").strip()
            u_per = self._get_int("u_per", 25)
            u_sort = (self.request.GET.get("u_sort") or "number").strip()
            if u_sort.lstrip("-") not in {"number", "floor", "owner_name"}:
                u_sort = "number"

            units_qs = (
                Unit.objects.filter(building=bld)
                .with_lawyer_alerts()
            )

            if u_q:
                filters = (
                    Q(number__icontains=u_q)
                    | Q(owner_name__icontains=u_q)
                    | Q(contact_phone__icontains=u_q)
                )
                # allow numeric floor match
                try:
                    filters |= Q(floor=int(u_q))
                except (TypeError, ValueError):
                    pass
                units_qs = units_qs.filter(filters)

            units_qs = units_qs.order_by(u_sort, "id")
            units_page = Paginator(units_qs, u_per).get_page(self.request.GET.get("u_page"))
            units_total = units_page.paginator.count if units_page and units_page.paginator else 0
            units_result_start = units_page.start_index() if units_total else 0
            units_result_end = units_page.end_index() if units_total else 0
            units_active_filter_chips: list[dict[str, str]] = []
            if u_q:
                units_active_filter_chips.append(
                    {"label": _("Search: %(value)s") % {"value": u_q}, "remove_url": _remove_filter_url("units", "u_q")}
                )
            if u_sort != "number":
                units_active_filter_chips.append(
                    {"label": _("Sort changed"), "remove_url": _remove_filter_url("units", "u_sort")}
                )
            if u_per != 25:
                units_active_filter_chips.append(
                    {"label": _("Page size: %(value)s") % {"value": u_per}, "remove_url": _remove_filter_url("units", "u_per")}
                )

            ctx.update(
                {
                    "units_page": units_page,
                    "u_q": u_q,
                    "u_per": u_per,
                    "u_sort": u_sort,
                    "u_active": active_tab == "units",
                    "units_total": units_total,
                    "units_result_start": units_result_start,
                    "units_result_end": units_result_end,
                    "units_active_filter_chips": units_active_filter_chips,
                    "units_has_active_filters": bool(units_active_filter_chips),
                }
            )
            ctx["units_pagination_query"] = _querystring_without(self.request, "u_page")

        with log_duration(logger, "building_detail.work_orders", extra={"building_id": bld.pk}):
            # ===================== Work Orders =======================
            # EXACTLY the names your template uses
            w_q = (self.request.GET.get("w_q") or "").strip()
            w_per = self._get_int("w_per", 25)
            w_status = (self.request.GET.get("w_status") or "").strip().upper()
            w_deadline_from_raw = (self.request.GET.get("w_deadline_from") or "").strip()
            w_deadline_to_raw = (self.request.GET.get("w_deadline_to") or "").strip()
            w_deadline_range_raw = (self.request.GET.get("w_deadline_range") or "").strip()
            w_deadline_from = parse_date(w_deadline_from_raw) if w_deadline_from_raw else None
            w_deadline_to = parse_date(w_deadline_to_raw) if w_deadline_to_raw else None
            if not (w_deadline_from or w_deadline_to) and w_deadline_range_raw:
                normalized = w_deadline_range_raw.replace(" to ", "/").replace("–", "/").replace("—", "/")
                parts = [part.strip() for part in normalized.split("/") if part.strip()]
                if parts:
                    w_deadline_from = parse_date(parts[0])
                    if len(parts) > 1:
                        w_deadline_to = parse_date(parts[1])
                if w_deadline_from and not w_deadline_from_raw:
                    w_deadline_from_raw = w_deadline_from.isoformat()
                if w_deadline_to and not w_deadline_to_raw:
                    w_deadline_to_raw = w_deadline_to.isoformat()

            filter_expr = Q(building=bld)
            awaiting_filter = Q(
                status=WorkOrder.Status.AWAITING_APPROVAL,
                archived_at__isnull=True,
            )
            awaiting_inbox_filter = awaiting_filter & ~Q(building_id=bld.pk)
            awaiting_queue_badges: list[dict[str, object]] = []
            if bld.is_system_default:
                office_queue_filter = Q(building=bld, forwarded_to_building__isnull=True)
                filter_expr = office_queue_filter | awaiting_inbox_filter
                awaiting_counts = (
                    WorkOrder.objects.visible_to(self.request.user)
                    .filter(awaiting_inbox_filter)
                    .values("building_id", "building__name")
                    .annotate(total=Count("id"))
                    .order_by(Lower("building__name"))
                )
                for entry in awaiting_counts:
                    origin_name = entry.get("building__name") or _("Unknown building")
                    awaiting_queue_badges.append(
                        {
                            "origin": origin_name,
                            "count": entry["total"],
                        }
                    )
            else:
                # Include Office-origin orders that were forwarded to this building.
                # Relying solely on the Office ID can fail when the default record is missing,
                # so we fall back to checking the destination FK directly.
                forwarded_filter = Q(forwarded_to_building=bld)
                filter_expr |= forwarded_filter

            wo_qs = (
                WorkOrder.objects.visible_to(self.request.user)
                .filter(filter_expr)
                .select_related("building", "unit", "forwarded_to_building")
                .annotate(
                    priority_order=Case(
                        When(priority__iexact="HIGH", then=0),
                        When(priority__iexact="MEDIUM", then=1),
                        When(priority__iexact="LOW", then=2),
                        default=3,
                        output_field=IntegerField(),
                    ),
                    forwarded_from_office=Case(
                        When(forwarded_to_building_id=bld.pk, then=Value(True)),
                        default=Value(False),
                        output_field=BooleanField(),
                    ),
                    awaiting_queue_entry=Case(
                        When(
                            awaiting_inbox_filter,
                            then=Value(True),
                        ),
                        default=Value(False),
                        output_field=BooleanField(),
                    ),
                )
            )
            if (
                user_has_role(request.user, MembershipRole.TECHNICIAN)
                and not user_has_role(request.user, MembershipRole.BACKOFFICE)
                and not user_has_role(request.user, MembershipRole.ADMINISTRATOR)
            ):
                wo_qs = wo_qs.exclude(
                    building__is_system_default=True,
                    forwarded_to_building__isnull=False,
                    status__in=[WorkOrder.Status.DONE, WorkOrder.Status.APPROVED],
                )

            wo_qs = wo_qs.filter(archived_at__isnull=True)

            if w_q:
                wo_qs = wo_qs.filter(Q(title__icontains=w_q) | Q(description__icontains=w_q))

            valid_status = {choice[0] for choice in WorkOrder.Status.choices}
            if w_status and w_status in valid_status:
                wo_qs = wo_qs.filter(status=w_status)
            if w_deadline_from:
                wo_qs = wo_qs.filter(deadline__gte=w_deadline_from)
            if w_deadline_to:
                wo_qs = wo_qs.filter(deadline__lte=w_deadline_to)
            if not (w_deadline_from or w_deadline_to):
                w_deadline_range_raw = ""

            wo_qs = wo_qs.order_by("priority_order", "deadline", "-id")
            workorders_page = Paginator(wo_qs, w_per).get_page(self.request.GET.get("w_page"))
            workorders_total = workorders_page.paginator.count if workorders_page and workorders_page.paginator else 0
            workorders_result_start = workorders_page.start_index() if workorders_total else 0
            workorders_result_end = workorders_page.end_index() if workorders_total else 0
            workorders_page.object_list = attach_expense_totals_by_metadata(
                workorders_page.object_list,
                metadata_key="work_order_id",
                metadata_keys=("work_order_id", "work_order"),
                include_work_order_text_fallback=True,
                target_attr="expense_total",
            )
            for work_order in workorders_page.object_list:
                work_order.is_overdue = bool(work_order.deadline and work_order.deadline < today)
                work_order.overdue_days = (today - work_order.deadline).days if work_order.is_overdue else 0
                technician_readonly_forwarded_order = bool(
                    is_technician_user
                    and getattr(work_order, "forwarded_to_building_id", None)
                    and getattr(getattr(work_order, "building", None), "is_system_default", False)
                    and work_order.status == WorkOrder.Status.AWAITING_APPROVAL
                )
                can_edit_from_origin = _user_has_building_capability(
                    request.user,
                    work_order.building,
                    Capability.MANAGE_BUILDINGS,
                    Capability.CREATE_WORK_ORDERS,
                )
                destination_building = getattr(work_order, "forwarded_to_building", None)
                can_edit_from_destination = destination_building is not None and _user_has_building_capability(
                    request.user,
                    destination_building,
                    Capability.MANAGE_BUILDINGS,
                    Capability.CREATE_WORK_ORDERS,
                )
                work_order.can_edit_in_building_list = (
                    not technician_readonly_forwarded_order
                    and (can_edit_from_origin or can_edit_from_destination)
                )
                work_order.can_delete_in_building_list = (
                    not technician_readonly_forwarded_order
                    and _user_has_building_capability(
                        request.user,
                        work_order.building,
                        Capability.MANAGE_BUILDINGS,
                    )
                )
            workorders_active_filter_chips: list[dict[str, str]] = []
            if w_q:
                workorders_active_filter_chips.append(
                    {"label": _("Search: %(value)s") % {"value": w_q}, "remove_url": _remove_filter_url("work_orders", "w_q")}
                )
            if w_status:
                status_map = dict(WorkOrder.Status.choices)
                workorders_active_filter_chips.append(
                    {
                        "label": _("Status: %(value)s") % {"value": status_map.get(w_status, w_status)},
                        "remove_url": _remove_filter_url("work_orders", "w_status"),
                    }
                )
            if w_deadline_from:
                workorders_active_filter_chips.append(
                    {"label": _("From: %(value)s") % {"value": w_deadline_from_raw}, "remove_url": _remove_filter_url("work_orders", "w_deadline_from")}
                )
            if w_deadline_to:
                workorders_active_filter_chips.append(
                    {"label": _("To: %(value)s") % {"value": w_deadline_to_raw}, "remove_url": _remove_filter_url("work_orders", "w_deadline_to")}
                )
            if w_per != 25:
                workorders_active_filter_chips.append(
                    {"label": _("Page size: %(value)s") % {"value": w_per}, "remove_url": _remove_filter_url("work_orders", "w_per")}
                )
            ctx.update(
                {
                    "workorders_page": workorders_page,
                    "w_q": w_q,
                    "w_per": w_per,
                    "w_status": w_status,
                    "w_status_choices": WorkOrder.Status.choices,
                    "w_deadline_range": w_deadline_range_raw,
                    "w_deadline_from": w_deadline_from_raw,
                    "w_deadline_to": w_deadline_to_raw,
                    "w_active": active_tab == "work_orders",
                    "awaiting_queue_badges": awaiting_queue_badges,
                    "workorders_total": workorders_total,
                    "workorders_result_start": workorders_result_start,
                    "workorders_result_end": workorders_result_end,
                    "workorders_active_filter_chips": workorders_active_filter_chips,
                    "workorders_has_active_filters": bool(workorders_active_filter_chips),
                }
            )
            ctx["workorders_pagination_query"] = _querystring_without(self.request, "w_page")

        return ctx


class BuildingCreateView(CapabilityRequiredMixin, LoginRequiredMixin, CreateView):
    model = Building
    form_class = BuildingForm
    template_name = "core/building_form.html"
    success_url = reverse_lazy("core:buildings_list")
    required_capabilities = (Capability.MANAGE_BUILDINGS,)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.save()
        messages.success(self.request, _("Building created."))
        return super().form_valid(form)


class BuildingUpdateView(CapabilityRequiredMixin, LoginRequiredMixin, CachedObjectMixin, UpdateView):
    model = Building
    form_class = BuildingForm
    template_name = "core/building_form.html"
    required_capabilities = (Capability.MANAGE_BUILDINGS,)
    capability_building_kwarg = "pk"

    def get_queryset(self):
        return Building.objects.visible_to(self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.save()
        messages.warning(self.request, _("Building updated."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("core:building_detail", args=[self.object.pk])


class BuildingDeleteView(CapabilityRequiredMixin, LoginRequiredMixin, CachedObjectMixin, DeleteView):
    model = Building
    template_name = "core/building_confirm_delete.html"
    success_url = reverse_lazy("core:buildings_list")
    required_capabilities = (Capability.MANAGE_BUILDINGS,)
    capability_building_kwarg = "pk"

    def get_queryset(self):
        return Building.objects.visible_to(self.request.user)

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not self._user_can_delete(obj):
            raise PermissionDenied(_("Only administrators can delete buildings."))
        return obj

    def _user_can_delete(self, building):
        user = self.request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        allowed_roles = (MembershipRole.ADMINISTRATOR,)
        return BuildingMembership.objects.filter(
            user=user,
            role__in=allowed_roles,
        ).filter(Q(building=building) | Q(building__isnull=True)).exists()

    def post(self, request, *args, **kwargs):
        messages.error(request, _("Building deleted."))
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj = getattr(self, "object", None) or self.get_object()
        meta = obj._meta
        ctx.setdefault("object_verbose_name", meta.verbose_name)
        ctx.setdefault("object_model_name", meta.model_name)
        ctx.setdefault("cancel_url", reverse("core:building_detail", args=[obj.pk]))
        return ctx

class UnitDetailView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DetailView):
    model = Unit
    template_name = "core/unit_detail.html"
    context_object_name = "unit"
    pk_url_kwarg = "unit_pk"

    def dispatch(self, request, *args, **kwargs):
        self.building = get_object_or_404(
            Building.objects.visible_to(request.user),
            pk=self.kwargs["building_pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return Unit.objects.select_related("building").filter(building=self.building)

    def test_func(self):
        return _user_can_access_building(self.request.user, self.building)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = self.building
        return ctx


class UnitCreateView(LoginRequiredMixin, CreateView):
    model = Unit
    form_class = UnitForm
    template_name = "core/unit_form.html"

    # Resolve the building up-front using a user-aware queryset
    def dispatch(self, request, *args, **kwargs):
        self.building = get_object_or_404(
            Building.objects.visible_to(request.user), pk=self.kwargs["building_pk"]
        )
        if not _user_has_building_capability(
            request.user,
            self.building,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_UNITS,
        ):
            return HttpResponseForbidden(_("You don't have permission to add units here."))
        return super().dispatch(request, *args, **kwargs)

    # Pre-fill the form with this building and restrict unit choices to it
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["building"] = self.building
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        initial["building"] = self.building
        return initial

    def form_valid(self, form):
        # form.save() already sets the building; no need to reassign
        form.save()
        messages.success(self.request, _("Unit created."))
        return redirect("core:building_detail", pk=self.building.pk)

    def get_success_url(self):
        return reverse("core:building_detail", args=[self.building.pk])
    
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = self.building
        ctx["cancel_url"] = reverse("core:building_detail", args=[self.building.pk])
        return ctx


class UnitUpdateView(LoginRequiredMixin, CachedObjectMixin, UpdateView):
    model = Unit
    form_class = UnitForm
    template_name = "core/unit_form.html"
    pk_url_kwarg = "unit_pk"

    def dispatch(self, request, *args, **kwargs):
        self.building = get_object_or_404(
            Building.objects.visible_to(request.user),
            pk=self.kwargs["building_pk"],
        )
        if not _user_has_building_capability(
            request.user,
            self.building,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_UNITS,
        ):
            return HttpResponseForbidden(_("You don't have permission to edit units here."))
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return Unit.objects.select_related("building").filter(building=self.building)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["building"] = self.building
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.warning(self.request, _("Unit updated."))
        return response

    def get_success_url(self):
        return reverse("core:building_detail", args=[self.building.pk])


class UnitDeleteView(LoginRequiredMixin, CachedObjectMixin, DeleteView):
    model = Unit
    template_name = "core/unit_confirm_delete.html"
    pk_url_kwarg = "unit_pk"

    def dispatch(self, request, *args, **kwargs):
        self.building = get_object_or_404(
            Building.objects.visible_to(request.user),
            pk=self.kwargs["building_pk"],
        )
        if not _user_has_building_capability(
            request.user,
            self.building,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_UNITS,
        ):
            return HttpResponseForbidden(_("You don't have permission to delete units here."))
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return Unit.objects.select_related("building").filter(building=self.building)

    def get_success_url(self):
        return reverse("core:building_detail", args=[self.building.pk])

    def post(self, request, *args, **kwargs):
        messages.error(request, _("Unit deleted."))
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj = getattr(self, "object", None) or self.get_object()
        meta = obj._meta
        ctx.setdefault("object_verbose_name", meta.verbose_name)
        ctx.setdefault("object_model_name", meta.model_name)
        ctx.setdefault("cancel_url", reverse("core:building_detail", args=[self.building.pk]))
        return ctx


class BuildingMembershipManageView(CapabilityRequiredMixin, LoginRequiredMixin, CachedObjectMixin, TemplateView):
    template_name = "core/building_memberships.html"
    required_capabilities = (Capability.MANAGE_MEMBERSHIPS,)
    capability_building_kwarg = "pk"

    def dispatch(self, request, *args, **kwargs):
        self.building = get_object_or_404(
            Building.objects.visible_to(request.user),
            pk=kwargs["pk"],
        )
        resolver = CapabilityResolver(request.user) if request.user.is_authenticated else None
        self._can_manage_memberships = (
            resolver.has(Capability.MANAGE_MEMBERSHIPS, building_id=self.building.pk)
            if resolver
            else False
        )
        self._is_admin_for_building = self._user_is_admin(request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        memberships = list(
            self.building.memberships.select_related("user").order_by("user__username", "role")
        )
        show_add_form = self._can_manage_memberships and (
            self.request.GET.get("show_add") == "1" or kwargs.get("form") is not None
        )
        form = None
        if show_add_form:
            form = kwargs.get("form") or BuildingMembershipForm(building=self.building)
        ctx.update(
            {
                "building": self.building,
                "memberships": memberships,
                "form": form,
                "show_add_form": show_add_form,
                "can_add_members": self._can_manage_memberships,
                "add_member_url": self._build_add_url(),
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        if not self._can_manage_memberships:
            return HttpResponseForbidden(_("You do not have permission to assign roles."))

        form = BuildingMembershipForm(request.POST, building=self.building)
        if form.is_valid():
            memberships = form.save()
            for membership in memberships:
                log_role_action(
                    actor=request.user,
                    target_user=membership.user,
                    building=self.building,
                    role=membership.role,
                    action=RoleAuditLog.Action.ROLE_ADDED,
                    payload={"reason": "manual_add"},
                )
            if memberships:
                messages.success(
                    request,
                    _("Assigned %(count)s member(s).") % {"count": len(memberships)},
                )
            else:
                messages.info(request, _("No new members were added."))
            return redirect("core:building_memberships", pk=self.building.pk)
        context = self.get_context_data(form=form)
        return self.render_to_response(context)

    def _user_is_admin(self, user):
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return BuildingMembership.objects.filter(
            user=user,
            role=MembershipRole.ADMINISTRATOR,
        ).filter(Q(building__isnull=True) | Q(building=self.building)).exists()

    def _build_add_url(self):
        params = self.request.GET.copy()
        params["show_add"] = "1"
        query = params.urlencode()
        base = self.request.path
        return f"{base}?{query}" if query else base


class BuildingMembershipDeleteView(CapabilityRequiredMixin, LoginRequiredMixin, DeleteView):
    model = BuildingMembership
    template_name = "core/building_membership_confirm_delete.html"
    pk_url_kwarg = "membership_pk"
    required_capabilities = (Capability.MANAGE_MEMBERSHIPS,)
    capability_building_kwarg = "building_pk"

    def dispatch(self, request, *args, **kwargs):
        self.building = get_object_or_404(
            Building.objects.visible_to(request.user),
            pk=kwargs["building_pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return (
            BuildingMembership.objects.select_related("user", "building")
            .filter(building=self.building)
        )

    def get_success_url(self):
        next_url = self.request.GET.get("next")
        if next_url:
            return next_url
        return reverse("core:building_memberships", args=[self.building.pk])

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        membership = self.object
        target_user = membership.user
        role = membership.role
        response = super().delete(request, *args, **kwargs)
        log_role_action(
            actor=request.user,
            target_user=target_user,
            building=self.building,
            role=role,
            action=RoleAuditLog.Action.ROLE_REMOVED,
            payload={"reason": "manual_remove"},
        )
        messages.success(
            request,
            _("Removed %(role)s access for %(user)s.") % {
                "role": membership.get_role_display(),
                "user": target_user.get_full_name() or target_user.username,
            },
        )
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        membership = self.object
        ctx["building"] = self.building
        ctx["member_user"] = membership.user
        ctx["member_role"] = membership.get_role_display()
        ctx["cancel_url"] = self.request.GET.get(
            "next", reverse("core:building_memberships", args=[self.building.pk])
        )
        return ctx


class TechnicianSubroleUpdateView(LoginRequiredMixin, TemplateView):
    template_name = "core/technician_subrole.html"

    def dispatch(self, request, *args, **kwargs):
        self.building = get_object_or_404(
            Building.objects.visible_to(request.user),
            pk=kwargs["pk"],
        )
        self.membership = BuildingMembership.objects.filter(
            building=self.building,
            user=request.user,
            role=MembershipRole.TECHNICIAN,
        ).first()
        if not self.membership:
            return HttpResponseForbidden(_("Only assigned technicians can edit their sub-role."))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = self.building
        ctx["form"] = kwargs.get("form") or TechnicianSubroleForm(instance=self.membership)
        return ctx

    def post(self, request, *args, **kwargs):
        form = TechnicianSubroleForm(request.POST, instance=self.membership)
        if form.is_valid():
            form.save()
            log_role_action(
                actor=request.user,
                target_user=request.user,
                building=self.building,
                role=MembershipRole.TECHNICIAN,
                action=RoleAuditLog.Action.CAPABILITY_UPDATED,
                payload={"technician_subrole": form.cleaned_data["technician_subrole"]},
            )
            messages.success(request, _("Sub-role updated."))
            return redirect("core:building_detail", pk=self.building.pk)
        context = self.get_context_data(form=form)
        return self.render_to_response(context)
