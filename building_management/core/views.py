# core/views.py

from __future__ import annotations

from datetime import timedelta
import json
from itertools import groupby

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Case, When, IntegerField, Q, Count, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce, Lower, Trim, Replace
from django.http import JsonResponse, Http404, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy, reverse
from django.views import View
from django.views.generic import (
    ListView,
    DetailView,
    CreateView,
    UpdateView,
    DeleteView,
)

from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _, ngettext, gettext_lazy as _lazy
from .forms import BuildingForm, UnitForm, WorkOrderForm
from .models import Building, Unit, WorkOrder
from .views_theme import toggle_theme

User = get_user_model()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _user_can_access_building(user, building: Building) -> bool:
    """Staff can access everything; others only their own buildings."""
    return user.is_staff or building.owner_id == user.id


def _safe_next_url(request):
    """Return a user-supplied 'next' URL if it's safe; otherwise None."""
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return None


# ----------------------------------------------------------------------
# Buildings
# ----------------------------------------------------------------------

class BuildingListView(LoginRequiredMixin, ListView):
    model = Building
    template_name = "core/buildings_list.html"
    context_object_name = "buildings"

    def get_paginate_by(self, queryset):
        try:
            return int(self.request.GET.get("per", 10))
        except (TypeError, ValueError):
            return 10

    def get_queryset(self):
        user = self.request.user

        # Per-user visibility + annotate the exact stats the template uses
        qs = (
            Building.objects.visible_to(user)
            .with_unit_stats()
            .select_related("owner")
        )

        # Search
        q = (self.request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(address__icontains=q)
                | Q(owner__username__icontains=q)
            )

        # Sorting
        sort = (self.request.GET.get("sort") or "name").strip()
        allow_owner_sort = user.is_staff or user.is_superuser
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
        return qs.order_by(sort_field, "id")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = (self.request.GET.get("q") or "").strip()
        try:
            ctx["per"] = int(self.request.GET.get("per", 10))
        except (TypeError, ValueError):
            ctx["per"] = 10
        ctx["sort"] = getattr(self, "_effective_sort", "name")
        ctx["show_owner_column"] = self.request.user.is_staff or self.request.user.is_superuser
        ctx["notifications"] = self._build_notifications()
        return ctx

    def _build_notifications(self):
        user = self.request.user
        if not user.is_authenticated:
            return []

        visible_buildings = (
            Building.objects.visible_to(user).values_list("id", flat=True)
        )
        visible_ids = list(visible_buildings)
        if not visible_ids:
            return []

        is_admin = user.is_staff or user.is_superuser
        today = timezone.localdate()

        notifications: list[dict[str, str]] = []

        # ---- Upcoming deadlines ----
        base_open = (
            WorkOrder.objects.filter(
                building_id__in=visible_ids,
                archived_at__isnull=True,
                status__in=[WorkOrder.Status.OPEN, WorkOrder.Status.IN_PROGRESS],
            )
            .select_related("building__owner", "unit")
        )

        thresholds = {
            WorkOrder.Priority.HIGH: 7,
            WorkOrder.Priority.MEDIUM: 7,
            WorkOrder.Priority.LOW: 30,
        }
        priority_levels = {
            WorkOrder.Priority.HIGH: "danger",
            WorkOrder.Priority.MEDIUM: "warning",
            WorkOrder.Priority.LOW: "info",
        }

        for priority, window in thresholds.items():
            window_end = today + timedelta(days=window)
            upcoming = (
                base_open.filter(priority=priority, deadline__gte=today, deadline__lte=window_end)
                .order_by("deadline")[:10]
            )
            for wo in upcoming:
                building = wo.building
                building_id = wo.building_id
                building_name = building.name if building_id else _("your building")
                owner_label = None
                if is_admin and building_id:
                    owner = getattr(building, "owner", None)
                    if owner:
                        owner_label = owner.get_full_name() or owner.username
                days_left = (wo.deadline - today).days
                if days_left < 0:
                    overdue_days = abs(days_left)
                    due_text = ngettext(
                        "overdue by %(count)s day",
                        "overdue by %(count)s days",
                        overdue_days,
                    ) % {"count": overdue_days}
                elif days_left == 0:
                    due_text = _("due today")
                elif days_left == 1:
                    due_text = _("due tomorrow")
                else:
                    due_text = ngettext(
                        "due in %(count)s day",
                        "due in %(count)s days",
                        days_left,
                    ) % {"count": days_left}
                message = _(
                    '%(priority)s priority work order "%(title)s" in %(building)s is %(due)s '
                    "(deadline %(deadline)s)"
                ) % {
                    "priority": wo.get_priority_display(),
                    "title": wo.title,
                    "building": building_name,
                    "due": due_text,
                    "deadline": wo.deadline,
                }
                if owner_label:
                    message += _(" (owner: %(owner)s)") % {"owner": owner_label}
                notifications.append(
                    {
                        "id": f"wo-deadline-{wo.id}",
                        "level": priority_levels[priority],
                        "message": message + ".",
                        "category": "deadline",
                    }
                )

        return notifications

class BuildingDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = Building
    template_name = "core/building_detail.html"
    context_object_name = "building"

    # Only buildings the user may see
    def get_queryset(self):
        return Building.objects.visible_to(self.request.user)

    # Owner or staff
    def test_func(self):
        b = self.get_object()
        return self.request.user.is_staff or b.owner_id == self.request.user.id

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

        # ========================= Units =========================
        u_q = (self.request.GET.get("u_q") or "").strip()
        u_per = self._get_int("u_per", 10)
        u_sort = (self.request.GET.get("u_sort") or "number").strip()
        if u_sort.lstrip("-") not in {"number", "floor", "owner_name"}:
            u_sort = "number"

        units_qs = Unit.objects.filter(building=bld)

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

        ctx.update(
            {
                "units_page": units_page,
                "u_q": u_q,
                "u_per": u_per,
                "u_sort": u_sort,
            }
        )

        # ===================== Work Orders =======================
        # EXACTLY the names your template uses
        w_q = (self.request.GET.get("w_q") or "").strip()
        w_per = self._get_int("w_per", 10)
        w_status = (self.request.GET.get("w_status") or "").strip().upper()

        wo_qs = (
            WorkOrder.objects.visible_to(self.request.user)
            .filter(building=bld, archived_at__isnull=True)  # show active work orders
            .select_related("unit")
            .annotate(
                priority_order=Case(
                    When(priority__iexact="HIGH", then=0),
                    When(priority__iexact="MEDIUM", then=1),
                    When(priority__iexact="LOW", then=2),
                    default=3,
                    output_field=IntegerField(),
                )
            )
        )

        if w_q:
            wo_qs = wo_qs.filter(Q(title__icontains=w_q) | Q(description__icontains=w_q))

        valid_status = {choice[0] for choice in WorkOrder.Status.choices}
        if w_status and w_status in valid_status:
            wo_qs = wo_qs.filter(status=w_status)

        wo_qs = wo_qs.order_by("priority_order", "deadline", "-id")
        workorders_page = Paginator(wo_qs, w_per).get_page(self.request.GET.get("w_page"))

        ctx.update(
            {
                "workorders_page": workorders_page,
                "w_q": w_q,
                "w_per": w_per,
                "w_status": w_status,
                "w_status_choices": WorkOrder.Status.choices,
            }
        )

        return ctx


class BuildingCreateView(LoginRequiredMixin, CreateView):
    model = Building
    form_class = BuildingForm
    template_name = "core/building_form.html"
    success_url = reverse_lazy("buildings_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        obj = form.save(commit=False)
        # Non-staff must own what they create
        if not self.request.user.is_staff:
            obj.owner = self.request.user
        obj.save()
        messages.success(self.request, _( "Building created." ))
        return super().form_valid(form)


class BuildingUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Building
    form_class = BuildingForm
    template_name = "core/building_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def test_func(self):
        building = self.get_object()
        return _user_can_access_building(self.request.user, building)

    def form_valid(self, form):
        obj = form.save(commit=False)
        if not self.request.user.is_staff:
            # Safety: prevent tampering with owner
            obj.owner = self.request.user
        obj.save()
        messages.warning(self.request, _( "Building updated." ))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("building_detail", args=[self.object.pk])


class BuildingDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = Building
    template_name = "core/building_confirm_delete.html"
    success_url = reverse_lazy("buildings_list")

    def test_func(self):
        building = self.get_object()
        return _user_can_access_building(self.request.user, building)

    def post(self, request, *args, **kwargs):
        messages.error(request, _( "Building deleted." ))
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj = getattr(self, "object", None) or self.get_object()
        meta = obj._meta
        ctx.setdefault("object_verbose_name", meta.verbose_name)
        ctx.setdefault("object_model_name", meta.model_name)
        ctx.setdefault("cancel_url", reverse("building_detail", args=[obj.pk]))
        return ctx


# ----------------------------------------------------------------------
# Units
# ----------------------------------------------------------------------

class UnitDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = Unit
    template_name = "core/unit_detail.html"
    context_object_name = "unit"

    def get_queryset(self):
        return Unit.objects.select_related("building")

    def test_func(self):
        unit = self.get_object()
        return _user_can_access_building(self.request.user, unit.building)


class UnitCreateView(LoginRequiredMixin, CreateView):
    model = Unit
    form_class = UnitForm
    template_name = "core/unit_form.html"

    # Resolve the building up-front using a user-aware queryset
    def dispatch(self, request, *args, **kwargs):
        self.building = get_object_or_404(
            Building.objects.visible_to(request.user), pk=self.kwargs["pk"]
        )
        # security: only building owner or staff may add units to this building
        if not (request.user.is_staff or self.building.owner_id == request.user.id):
            return HttpResponseForbidden("You don't have permission to add units here.")
        return super().dispatch(request, *args, **kwargs)

    # Only the building owner or staff can add units
    def test_func(self):
        return self.request.user.is_staff or self.building.owner_id == self.request.user.id

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
        messages.success(self.request, _( "Unit created." ))
        return redirect("building_detail", pk=self.building.pk)

    def get_success_url(self):
        return reverse("building_detail", args=[self.building.pk])
    
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = self.building
        ctx["cancel_url"] = reverse("building_detail", args=[self.building.pk])
        return ctx


class UnitUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Unit
    form_class = UnitForm
    template_name = "core/unit_form.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        self.building = obj.building
        return obj

    def test_func(self):
        unit = self.get_object()
        return self.request.user.is_staff or unit.building.owner_id == self.request.user.id

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["building"] = self.building
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.warning(self.request, _( "Unit updated." ))
        return response

    def get_success_url(self):
        return reverse("building_detail", args=[self.building.pk])


class UnitDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = Unit
    template_name = "core/unit_confirm_delete.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        self.building = obj.building
        return obj

    def test_func(self):
        unit = self.get_object()
        return _user_can_access_building(self.request.user, unit.building)

    def get_success_url(self):
        return reverse("building_detail", args=[self.building.pk])

    def post(self, request, *args, **kwargs):
        messages.error(request, _( "Unit deleted." ))
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj = getattr(self, "object", None) or self.get_object()
        meta = obj._meta
        ctx.setdefault("object_verbose_name", meta.verbose_name)
        ctx.setdefault("object_model_name", meta.model_name)
        ctx.setdefault("cancel_url", reverse("building_detail", args=[self.building.pk]))
        return ctx


# ----------------------------------------------------------------------
# Work Orders
# ----------------------------------------------------------------------
class WorkOrderListView(LoginRequiredMixin, ListView):
    model = WorkOrder
    template_name = "core/work_orders_list.html"
    context_object_name = "orders"
    paginate_by = 10

    _per_choices = (10, 20, 50, 100)

    def get_queryset(self):
        request = self.request
        user = request.user

        # Page size (validated later in get_paginate_by)
        try:
            per = int(request.GET.get("per", self.paginate_by))
        except (TypeError, ValueError):
            per = self.paginate_by
        self._per = per

        search = (request.GET.get("q") or "").strip()
        status = (request.GET.get("status") or "").strip().upper()
        valid_status = {choice[0] for choice in WorkOrder.Status.choices}
        if status and status not in valid_status:
            status = ""

        # Use visibility helper + pull related objects
        qs = (
            WorkOrder.objects.visible_to(user)
            .filter(archived_at__isnull=True)
            .select_related("building__owner", "unit")
        )

        # Build owner choices for staff (before additional filters)
        self._owner_choices: list[dict[str, str]] = []
        if user.is_staff or user.is_superuser:
            owner_ids = list(
                qs.values_list("building__owner_id", flat=True)
                  .distinct()
            )
            owner_ids = [oid for oid in owner_ids if oid]
            if owner_ids:
                owner_qs = (
                    User.objects.filter(id__in=owner_ids)
                    .order_by("first_name", "last_name", "username")
                )
                for owner in owner_qs:
                    label = owner.get_full_name() or owner.username
                    self._owner_choices.append(
                        {"id": str(owner.id), "label": label}
                    )

        # Priority ordering: High > Medium > Low, then by deadline asc, then newest
        qs = qs.annotate(
            priority_order=Case(
                When(priority__iexact="high", then=0),
                When(priority__iexact="medium", then=1),
                When(priority__iexact="low", then=2),
                default=3,
                output_field=IntegerField(),
            )
        )

        if search:
            qs = qs.filter(
                Q(title__icontains=search) | Q(description__icontains=search)
            )

        if status:
            qs = qs.filter(status=status)

        owner_param = (request.GET.get("owner") or "").strip()
        owner_filter = None
        if owner_param:
            try:
                owner_filter = int(owner_param)
            except (TypeError, ValueError):
                owner_param = ""
                owner_filter = None

        if owner_filter:
            if user.is_staff or user.is_superuser:
                qs = qs.filter(building__owner_id=owner_filter)
            elif owner_filter == user.id:
                qs = qs.filter(building__owner_id=owner_filter)
            else:
                owner_param = ""
                owner_filter = None

        sort_param = (request.GET.get("sort") or "priority").strip()
        sort_map = {
            "priority": ("priority_order", "deadline", "-pk"),
            "priority_desc": ("-priority_order", "-deadline", "-pk"),
            "deadline": ("deadline", "priority_order", "-pk"),
            "deadline_desc": ("-deadline", "priority_order", "-pk"),
            "created": ("-created_at",),
            "created_asc": ("created_at",),
            "building": ("building__name", "priority_order", "deadline", "-pk"),
            "building_desc": ("-building__name", "priority_order", "deadline", "-pk"),
            "owner": ("building__owner__username", "priority_order", "deadline", "-pk"),
            "owner_desc": ("-building__owner__username", "priority_order", "deadline", "-pk"),
        }
        if sort_param not in sort_map:
            sort_param = "priority"

        if sort_param in {"owner", "owner_desc"} and not (user.is_staff or user.is_superuser):
            sort_param = "priority"

        qs = qs.order_by(*sort_map[sort_param])

        self._search = search
        self._status = status
        self._owner = owner_param
        self._sort = sort_param

        return qs

    def get_paginate_by(self, queryset):
        per = getattr(self, "_per", self.paginate_by)
        if per not in self._per_choices:
            per = self.paginate_by
        return per

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            {
                "q": getattr(self, "_search", ""),
                "status": getattr(self, "_status", ""),
                "status_choices": WorkOrder.Status.choices,
                "per": self.get_paginate_by(self.object_list),
                "per_choices": self._per_choices,
                "owner": getattr(self, "_owner", ""),
                "owner_choices": getattr(self, "_owner_choices", []),
                "sort": getattr(self, "_sort", "priority"),
                "sort_choices": [
                    ("priority", _("Priority (High → Low)")),
                    ("priority_desc", _("Priority (Low → High)")),
                    ("deadline", _("Deadline (Soon → Late)")),
                    ("deadline_desc", _("Deadline (Late → Soon)")),
                    ("created", _("Created (Newest first)")),
                    ("created_asc", _("Created (Oldest first)")),
                    ("building", _("Building (A → Z)")),
                    ("building_desc", _("Building (Z → A)")),
                    ("owner", _("Owner (A → Z)")),
                    ("owner_desc", _("Owner (Z → A)")),
                ],
                "show_owner_info": self.request.user.is_staff or self.request.user.is_superuser,
            }
        )
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            ctx["sort_choices"] = [
                choice for choice in ctx["sort_choices"]
                if not choice[0].startswith("owner")
            ]
        return ctx

class WorkOrderDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = WorkOrder
    template_name = "core/work_order_detail.html"
    context_object_name = "order"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        next_url = _safe_next_url(self.request)
        ctx["next_url"] = next_url
        if next_url:
            ctx["cancel_url"] = next_url
        return ctx

    def test_func(self):
        wo = self.get_object()
        return _user_can_access_building(self.request.user, wo.building)


class WorkOrderCreateView(LoginRequiredMixin, CreateView):
    model = WorkOrder
    form_class = WorkOrderForm
    template_name = "core/work_order_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user

        # If opening from a building context (?building=<id>), lock to it
        self.building = None
        building_id = self.request.GET.get("building")
        if building_id:
            try:
                self.building = Building.objects.visible_to(self.request.user).get(pk=int(building_id))
                kwargs["building"] = self.building
            except (ValueError, Building.DoesNotExist):
                pass
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = getattr(self, "building", None)
        safe_next = _safe_next_url(self.request)
        ctx["next_url"] = safe_next
        if safe_next:
            ctx["cancel_url"] = safe_next
        else:
            ctx["cancel_url"] = (
                reverse("building_detail", args=[self.building.pk])
                if getattr(self, "building", None)
                else reverse("work_orders_list")
            )
        ctx["units_api_template"] = reverse("api_units", args=[0]).replace("/0/", "/{id}/")

        units_qs = (
            Unit.objects.visible_to(self.request.user)
            .select_related("building")
            .order_by("building__name", "number")
        )
        if getattr(self, "building", None):
            units_qs = units_qs.filter(building=self.building)

        ctx["units_dataset"] = json.dumps(
            [
                {
                    "id": u.id,
                    "number": u.number,
                    "label": f"{u.building.name} — #{u.number}" if u.building_id else (u.number or ""),
                    "building_id": u.building_id,
                }
                for u in units_qs
            ],
            cls=DjangoJSONEncoder,
        )
        return ctx

    def form_valid(self, form):
        obj = form.save(commit=False)
        if not _user_can_access_building(self.request.user, obj.building):
            raise Http404()
        obj.save()
        messages.success(self.request, _( "Work order created." ))
        return super().form_valid(form)

    def get_success_url(self):
        next_url = _safe_next_url(self.request)
        if next_url:
            return next_url
        return reverse("building_detail", args=[self.object.building_id])


class WorkOrderUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = WorkOrder
    form_class = WorkOrderForm
    template_name = "core/work_order_form.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        self.building = obj.building  # lock form to this building on edit
        return obj

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["building"] = getattr(self, "building", None)
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = getattr(self, "building", None)
        building = getattr(self, "building", None)
        safe_next = _safe_next_url(self.request)
        ctx["next_url"] = safe_next
        if safe_next:
            ctx["cancel_url"] = safe_next
        elif building is not None:
            ctx["cancel_url"] = reverse("building_detail", args=[building.pk])
        ctx["units_api_template"] = reverse("api_units", args=[0]).replace("/0/", "/{id}/")
        units_qs = (
            Unit.objects.visible_to(self.request.user)
            .select_related("building")
            .order_by("building__name", "number")
        )
        if building is not None:
            units_qs = units_qs.filter(building=building)

        ctx["units_dataset"] = json.dumps(
            [
                {
                    "id": u.id,
                    "number": u.number,
                    "label": f"{u.building.name} — #{u.number}" if building is None else f"#{u.number}",
                    "building_id": u.building_id,
                }
                for u in units_qs
            ],
            cls=DjangoJSONEncoder,
        )
        return ctx

    def test_func(self):
        wo = self.get_object()
        return _user_can_access_building(self.request.user, wo.building)

    def form_valid(self, form):
        obj = form.save(commit=False)
        if not _user_can_access_building(self.request.user, obj.building):
            raise Http404()
        if obj.archived_at and obj.status != WorkOrder.Status.DONE:
            obj.archived_at = None
        obj.save()
        self.object = obj
        messages.warning(self.request, _( "Work order updated." ))
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        next_url = _safe_next_url(self.request)
        if next_url:
            return next_url
        return reverse("building_detail", args=[self.object.building_id])


class WorkOrderDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = WorkOrder
    template_name = "core/work_order_confirm_delete.html"   # <- new specific template

    def test_func(self):
        wo = self.get_object()
        return _user_can_access_building(self.request.user, wo.building)

    def get_success_url(self):
        next_url = _safe_next_url(self.request)
        if next_url:
            return next_url
        return reverse_lazy("building_detail", args=[self.object.building_id])

    def post(self, request, *args, **kwargs):
        messages.error(request, _( "Work order deleted." ))
        return super().post(request, *args, **kwargs)
    
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj = getattr(self, "object", None) or self.get_object()
        meta = obj._meta
        ctx.setdefault("object_verbose_name", meta.verbose_name)
        ctx.setdefault("object_model_name", meta.model_name)
        next_url = _safe_next_url(self.request)
        if next_url:
            ctx["next_url"] = next_url
            ctx["cancel_url"] = next_url
        else:
            ctx.setdefault("cancel_url", reverse("building_detail", args=[obj.building_id]))
        return ctx
    

class WorkOrderArchiveView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Archive a work order by setting archived_at (via WorkOrder.archive()).
    - Only staff/owner of the building can archive.
    - Only allowed when the work order status is DONE.
    """

    def get_queryset(self):
        # Respect per-user visibility
        return WorkOrder.objects.visible_to(self.request.user)

    def get_object(self):
        return get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])

    def test_func(self):
        wo = self.get_object()
        return _user_can_access_building(self.request.user, wo.building)

    def post(self, request, *args, **kwargs):
        wo = self.get_object()

        if wo.status != WorkOrder.Status.DONE:
            raise Http404(_("Only completed work orders can be archived."))

        if not wo.is_archived:
            wo.archive()
            messages.success(request, _( "Work order archived." ))

        next_url = _safe_next_url(request)
        if next_url:
            return redirect(next_url)
        return redirect("building_detail", wo.building_id)

    # Optional: allow GET-triggered archive links; remove to enforce POST-only.
    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)


class ArchivedWorkOrderListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """
    Staff-only list of archived work orders, grouped by building for easy browsing.
    """

    model = WorkOrder
    template_name = "core/work_orders_archive.html"
    context_object_name = "orders"
    raise_exception = True

    _sort_choices = [
        ("archived_desc", _lazy("Archived (Newest first)")),
        ("archived_asc", _lazy("Archived (Oldest first)")),
        ("created_desc", _lazy("Created (Newest first)")),
        ("created_asc", _lazy("Created (Oldest first)")),
        ("priority", _lazy("Priority (High → Low)")),
        ("priority_desc", _lazy("Priority (Low → High)")),
        ("building_asc", _lazy("Building (A → Z)")),
        ("building_desc", _lazy("Building (Z → A)")),
    ]

    def test_func(self):
        user = self.request.user
        return user.is_staff or user.is_superuser

    def get_queryset(self):
        request = self.request
        qs = (
            WorkOrder.objects.visible_to(request.user)
            .filter(archived_at__isnull=False)
            .select_related("building__owner", "unit")
        )

        # Annotate priority for sorting
        qs = qs.annotate(
            priority_order=Case(
                When(priority__iexact="high", then=0),
                When(priority__iexact="medium", then=1),
                When(priority__iexact="low", then=2),
                default=3,
                output_field=IntegerField(),
            )
        )

        # Free-text search across title/description/building
        search = (request.GET.get("q") or "").strip()
        if search:
            qs = qs.filter(
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(building__name__icontains=search)
            )
        self._search = search

        # Owner filter (staff only)
        owner_ids = list(
            qs.values_list("building__owner_id", flat=True).distinct()
        )
        owner_ids = [oid for oid in owner_ids if oid]
        owner_qs = (
            User.objects.filter(id__in=owner_ids)
            .order_by("first_name", "last_name", "username")
        )
        self._owner_choices = [
            {"id": str(owner.id), "label": owner.get_full_name() or owner.username}
            for owner in owner_qs
        ]

        owner_param = (request.GET.get("owner") or "").strip()
        owner_filter = None
        if owner_param:
            try:
                owner_filter = int(owner_param)
            except (TypeError, ValueError):
                owner_param = ""
                owner_filter = None

        if owner_filter and any(choice["id"] == str(owner_filter) for choice in self._owner_choices):
            qs = qs.filter(building__owner_id=owner_filter)
        else:
            owner_param = ""
            owner_filter = None
        self._owner = owner_param

        # Sorting options (always include building so groupby works)
        sort_param = (request.GET.get("sort") or "archived_desc").strip()
        sort_map = {
            "archived_desc": ("building__name", "building_id", "-archived_at", "-id"),
            "archived_asc": ("building__name", "building_id", "archived_at", "-id"),
            "created_desc": ("building__name", "building_id", "-created_at", "-id"),
            "created_asc": ("building__name", "building_id", "created_at", "-id"),
            "priority": ("building__name", "building_id", "priority_order", "-archived_at", "-id"),
            "priority_desc": ("building__name", "building_id", "-priority_order", "-archived_at", "-id"),
            "building_asc": ("building__name", "building_id", "-archived_at", "-id"),
            "building_desc": ("-building__name", "-building_id", "-archived_at", "-id"),
        }
        if sort_param not in sort_map:
            sort_param = "archived_desc"
        self._sort = sort_param
        qs = qs.order_by(*sort_map[sort_param])

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        orders = list(ctx.get("orders", []))

        grouped = []
        for _, items in groupby(orders, key=lambda wo: wo.building):
            items_list = list(items)
            if not items_list:
                continue
            building = items_list[0].building
            grouped.append(
                {
                    "building": building,
                    "orders": items_list,
                }
            )

        ctx["grouped_orders"] = grouped
        ctx["q"] = getattr(self, "_search", "")
        ctx["owner"] = getattr(self, "_owner", "")
        ctx["owner_choices"] = getattr(self, "_owner_choices", [])
        ctx["sort"] = getattr(self, "_sort", "archived_desc")
        ctx["sort_choices"] = self._sort_choices
        return ctx
    
# ----------------------------------------------------------------------
# JSON APIs (simple, function-based)
# ----------------------------------------------------------------------

def api_units(request, building_id: int | None = None):
    """
    JSON list of units visible to the current user.
    Optional filter: ?building=<id> (validated for visibility) or via path parameter.
    """
    if not request.user.is_authenticated:
        raise Http404()

    if request.user.is_staff:
        qs = Unit.objects.select_related("building").all()
        bld_qs = Building.objects.all()
    else:
        qs = Unit.objects.select_related("building").filter(building__owner=request.user)
        bld_qs = Building.objects.filter(owner=request.user)

    param = building_id if building_id is not None else request.GET.get("building")
    if param is not None:
        try:
            b_id = int(param)
        except (TypeError, ValueError):
            return JsonResponse({"error": "Invalid building id."}, status=400)

        if not bld_qs.filter(pk=b_id).exists():
            return JsonResponse({"error": "Building not found."}, status=404)

        qs = qs.filter(building_id=b_id)

    data = list(
        qs.values("id", "number", "floor", "owner_name", "building_id")
          .order_by("building_id", "number", "id")
    )
    return JsonResponse(data, safe=False)

def api_buildings(request):
    """
    JSON list of buildings visible to the current user.
    Staff: all buildings. Non-staff: own buildings.
    """
    if not request.user.is_authenticated:
        raise Http404()

    qs = Building.objects.visible_to(request.user).order_by("name", "id")

    data = list(qs.values("id", "name", "address", "owner_id"))
    return JsonResponse(data, safe=False)
