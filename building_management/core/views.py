# core/views.py

from __future__ import annotations

from datetime import timedelta
from itertools import groupby

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.db.models import Case, When, IntegerField, Q, Count, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce, Lower, Trim, Replace
from django.http import HttpResponse, JsonResponse, Http404, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy, reverse
from django.views import View
from django.views.generic import (
    ListView,
    DetailView,
    CreateView,
    UpdateView,
    DeleteView,
    FormView,
)

from django.utils import formats, timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _, ngettext, gettext_lazy as _lazy
from .forms import (
    BuildingForm,
    UnitForm,
    WorkOrderForm,
    AdminUserCreateForm,
    AdminUserUpdateForm,
    AdminUserPasswordForm,
    MassAssignWorkOrdersForm,
)
from .models import Building, Notification, Unit, WorkOrder, UserSecurityProfile
from .services import NotificationService
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


class CachedObjectMixin:
    """Cache get_object() results within the request lifecycle to avoid duplicate queries."""

    _object_cache_attr = "_cached_object"

    def get_object(self, queryset=None):
        if hasattr(self, self._object_cache_attr):
            return getattr(self, self._object_cache_attr)
        obj = super().get_object(queryset)
        setattr(self, self._object_cache_attr, obj)
        return obj


class UnitsWidgetMixin:
    """Utility mixin to augment the WorkOrder unit field with data attributes for JS widgets."""

    def _prepare_units_widget(self, ctx):
        form = ctx.get("form")
        if not form or "unit" not in form.fields:
            return

        widget = form.fields["unit"].widget
        api_template = ctx.get("units_api_template")
        if api_template:
            widget.attrs.setdefault("data-units-api-template", api_template)

        # Determine the effective building for initial population.
        building_obj = getattr(self, "building", None)
        building_id = None
        if building_obj is not None:
            building_id = building_obj.pk
        elif form.data.get("building"):
            building_id = form.data.get("building")
        elif form.initial.get("building"):
            initial_building = form.initial.get("building")
            if hasattr(initial_building, "pk"):
                building_id = initial_building.pk
            else:
                building_id = initial_building
        elif getattr(form.instance, "building_id", None):
            building_id = form.instance.building_id
        if building_id:
            widget.attrs.setdefault("data-initial-building", str(building_id))

        # Preserve the currently selected unit so JS can re-select it after refresh.
        selected_unit = (
            form.data.get("unit")
            or form.initial.get("unit")
            or getattr(form.instance, "unit_id", "")
        )
        if selected_unit:
            widget.attrs.setdefault("data-selected-unit", str(selected_unit))

        empty_label = getattr(form.fields["unit"], "empty_label", None)
        if empty_label:
            widget.attrs.setdefault("data-empty-label", str(empty_label))

        widget.attrs.setdefault("aria-live", "polite")
        widget.attrs.setdefault("data-loading-text", str(_("Loading units…")))

class AdminRequiredMixin(UserPassesTestMixin):
    """Limit access to superusers (primary administrator role)."""

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and user.is_superuser

    def handle_no_permission(self):
        # Always bounce users back to the login screen so they can authenticate with
        # proper credentials instead of exposing a 403 page.
        return redirect_to_login(
            self.request.get_full_path(),
            self.get_login_url(),
            self.get_redirect_field_name(),
        )


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

        notifications: list[dict[str, str | bool]] = []

        service = NotificationService(user)
        deadline_notifications = list(service.sync_work_order_deadlines())
        new_flags = {note.key: note.first_seen_at is None for note in deadline_notifications}
        if deadline_notifications:
            service.mark_seen([note.key for note in deadline_notifications])

        level_weights = {
            Notification.Level.DANGER: 0,
            Notification.Level.WARNING: 1,
            Notification.Level.INFO: 2,
        }

        for note in deadline_notifications:
            notifications.append(
                {
                    "id": note.key,
                    "level": note.level,
                    "message": note.body,
                    "category": note.category,
                    "is_new": new_flags.get(note.key, False),
                    "dismissible": True,
                    "_priority_weight": level_weights.get(note.level, 99),
                }
            )

        mass_notifications = service.sync_recent_mass_assign()
        if mass_notifications:
            service.mark_seen([note.key for note in mass_notifications])
        for note in mass_notifications:
            notifications.append(
                {
                    "id": note.key,
                    "level": note.level,
                    "message": note.body,
                    "category": note.category,
                    "is_new": False,
                    "dismissible": True,
                    "_priority_weight": 3,
                }
            )

        is_admin = user.is_staff or user.is_superuser
        today = timezone.localdate()

        if is_admin:
            locked_accounts = (
                UserSecurityProfile.objects.select_related("user")
                .filter(
                    user__is_active=False,
                    lock_reason=UserSecurityProfile.LockReason.FAILED_ATTEMPTS,
                )
                .order_by("-locked_at")[:20]
            )

            for profile in locked_accounts:
                locked_at = profile.locked_at
                locked_display = (
                    formats.date_format(timezone.localtime(locked_at), "DATETIME_FORMAT")
                    if locked_at
                    else _("an unknown time")
                )
                locked_user = profile.user
                display_name = locked_user.get_full_name() or locked_user.username
                message = _(
                    "%(user)s was locked after too many failed login attempts on %(locked)s. Reactivate the account and set a new password to restore access."
                ) % {"user": display_name, "locked": locked_display}
                notifications.append(
                    {
                        "id": f"user-locked-{locked_user.pk}",
                        "level": "danger",
                        "message": message,
                        "category": "account_lock",
                        "is_new": False,
                        "dismissible": False,
                    }
                )

        notifications.sort(key=lambda item: (item.get("_priority_weight", 99), item.get("id")))
        for item in notifications:
            item.pop("_priority_weight", None)
            item.setdefault("is_new", False)
            item.setdefault("dismissible", False)
        return notifications

class BuildingDetailView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DetailView):
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


class BuildingUpdateView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, UpdateView):
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


class BuildingDeleteView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DeleteView):
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

class UnitDetailView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DetailView):
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


class UnitUpdateView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, UpdateView):
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


class UnitDeleteView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DeleteView):
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

class WorkOrderDetailView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DetailView):
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


class WorkOrderCreateView(LoginRequiredMixin, UnitsWidgetMixin, CreateView):
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
        self._prepare_units_widget(ctx)
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


class WorkOrderUpdateView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, UnitsWidgetMixin, UpdateView):
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
        self._prepare_units_widget(ctx)
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


class WorkOrderDeleteView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DeleteView):
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
    http_method_names = ["post"]

    def get_queryset(self):
        # Respect per-user visibility
        return WorkOrder.objects.visible_to(self.request.user)

    def get_object(self):
        if not hasattr(self, "_object_cache"):
            self._object_cache = get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])
        return self._object_cache

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


class MassAssignWorkOrdersView(LoginRequiredMixin, AdminRequiredMixin, FormView):
    template_name = "core/work_orders_mass_assign.html"
    form_class = MassAssignWorkOrdersForm
    success_url = reverse_lazy("work_orders_mass_assign")

    def get_queryset(self):
        if not hasattr(self, "_building_queryset"):
            self._building_queryset = (
                Building.objects.filter(role=Building.Role.TECH_SUPPORT)
                .select_related("owner")
                .order_by("name", "id")
            )
        return self._building_queryset

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["buildings_queryset"] = self.get_queryset()
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        buildings = list(self.get_queryset())
        ctx["technical_support_buildings"] = buildings
        ctx["technical_support_count"] = len(buildings)
        return ctx

    def form_valid(self, form):
        selected_buildings = list(form.cleaned_data.get("buildings") or [])
        if not selected_buildings:
            messages.info(self.request, _("No buildings with the Technical Support role are available."))
            return super().form_valid(form)

        title = form.cleaned_data["title"].strip()
        description = (form.cleaned_data.get("description") or "").strip()
        deadline = timezone.localdate() + timedelta(days=30)

        created = 0
        skipped = 0
        created_names = []

        with transaction.atomic():
            for building in selected_buildings:
                exists = (
                    WorkOrder.objects.filter(
                        building=building,
                        title=title,
                        mass_assigned=True,
                        status__in=[WorkOrder.Status.OPEN, WorkOrder.Status.IN_PROGRESS],
                        archived_at__isnull=True,
                    )
                    .order_by("-created_at")
                    .exists()
                )
                if exists:
                    skipped += 1
                    continue

                WorkOrder.objects.create(
                    building=building,
                    title=title,
                    description=description,
                    status=WorkOrder.Status.OPEN,
                    priority=WorkOrder.Priority.LOW,
                    deadline=deadline,
                    mass_assigned=True,
                )
                created += 1
                created_names.append(building.name)

        if created:
            building_list = ", ".join(created_names[:5])
            if created > 5:
                building_list += ", …"
            messages.success(
                self.request,
                ngettext(
                    "Created %(count)s work order for %(buildings)s.",
                    "Created %(count)s work orders (first few: %(buildings)s).",
                    created,
                )
                % {
                    "count": created,
                    "buildings": building_list or _("selected buildings"),
                },
            )

        if skipped:
            messages.warning(
                self.request,
                ngettext(
                    "%(count)s building already has an open mass-assigned work order with this title.",
                    "%(count)s buildings already have an open mass-assigned work order with this title.",
                    skipped,
                )
                % {"count": skipped},
            )

        return super().form_valid(form)


class ArchivedWorkOrderListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """
    Staff-only list of archived work orders, grouped by building for easy browsing.
    """

    model = WorkOrder
    template_name = "core/work_orders_archive.html"
    context_object_name = "orders"

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

    def handle_no_permission(self):
        # Redirect to login whenever access is denied to avoid serving a 403 page.
        return redirect_to_login(
            self.request.get_full_path(),
            self.get_login_url(),
            self.get_redirect_field_name(),
        )

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
# Administration – Users
# ----------------------------------------------------------------------


class AdminUserListView(LoginRequiredMixin, AdminRequiredMixin, ListView):
    model = User
    template_name = "core/users_list.html"
    context_object_name = "users"
    paginate_by = 20

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
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = getattr(self, "_search", "")
        return ctx


class AdminUserCreateView(LoginRequiredMixin, AdminRequiredMixin, CreateView):
    model = User
    form_class = AdminUserCreateForm
    template_name = "core/user_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("User created."))
        return response

    def get_success_url(self):
        return reverse("users_list")


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
        return reverse("users_list")


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
        return reverse("users_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["managed_user"] = self.user_obj
        return ctx


class AdminUserDeleteView(LoginRequiredMixin, AdminRequiredMixin, DeleteView):
    model = User
    template_name = "core/user_confirm_delete.html"
    success_url = reverse_lazy("users_list")

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


class NotificationSnoozeView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, key: str, *args, **kwargs):
        service = NotificationService(request.user)
        try:
            note = Notification.objects.get(user=request.user, key=key)
        except Notification.DoesNotExist:
            return JsonResponse({"error": "not_found"}, status=404)

        is_hx = bool(request.headers.get("Hx-Request"))
        next_url = _safe_next_url(request) or request.META.get("HTTP_REFERER") or reverse("buildings_list")

        if note.category == "mass_assign":
            note.acknowledge()
            if is_hx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "notifications:updated"
                return response
            messages.info(request, _( "Notification dismissed." ))
            return HttpResponseRedirect(next_url)

        note = service.snooze_until(key, target_date=timezone.localdate() + timedelta(days=1))

        if is_hx:
            response = HttpResponse(status=204)
            response["HX-Trigger"] = "notifications:updated"
            return response

        messages.info(
            request,
            _("Notification dismissed until %(date)s.") % {"date": note.snoozed_until.strftime("%Y-%m-%d")},
        )
        return HttpResponseRedirect(next_url)


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
