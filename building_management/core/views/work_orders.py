from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Max, Min, OuterRef, Q, Subquery, Value, When
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import formats, timezone
from django.utils.translation import gettext as _, gettext_lazy as _lazy, ngettext
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, FormView, ListView, UpdateView

from ..forms import MassAssignWorkOrdersForm, WorkOrderForm
from ..models import Building, Notification, Unit, WorkOrder
from ..services import NotificationService
from .common import (
    AdminRequiredMixin,
    CachedObjectMixin,
    _querystring_without,
    _safe_next_url,
    _user_can_access_building,
)

User = get_user_model()


__all__ = [
    "WorkOrderListView",
    "WorkOrderDetailView",
    "WorkOrderCreateView",
    "WorkOrderUpdateView",
    "WorkOrderDeleteView",
    "WorkOrderArchiveView",
    "MassAssignWorkOrdersView",
    "ArchivedWorkOrderListView",
    "ArchivedWorkOrderDetailView",
]


@lru_cache(maxsize=256)
def _cached_owner_choices(owner_ids: tuple[int, ...]) -> list[dict[str, str]]:
    if not owner_ids:
        return []
    owners = (
        User.objects.filter(id__in=owner_ids)
        .order_by("first_name", "last_name", "username")
    )
    return [
        {"id": str(owner.id), "label": owner.get_full_name() or owner.username}
        for owner in owners
    ]


class UnitsWidgetMixin:
    """Augment the WorkOrder unit field with client-side metadata."""

    def _prepare_units_widget(self, ctx):
        form = ctx.get("form")
        if not form or "unit" not in form.fields:
            return

        widget = form.fields["unit"].widget
        api_template = ctx.get("units_api_template")
        if api_template:
            widget.attrs.setdefault("data-units-api-template", api_template)

        building_obj = getattr(self, "building", None)
        building_id = None
        if building_obj is not None:
            building_id = building_obj.pk
        elif form.data.get("building"):
            building_id = form.data.get("building")
        elif form.initial.get("building"):
            initial_building = form.initial.get("building")
            building_id = initial_building.pk if hasattr(initial_building, "pk") else initial_building
        elif getattr(form.instance, "building_id", None):
            building_id = form.instance.building_id
        if building_id:
            widget.attrs.setdefault("data-initial-building", str(building_id))

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
                qs.values_list("building__owner_id", flat=True).distinct()
            )
            owner_ids = [oid for oid in owner_ids if oid]
            self._owner_choices = _cached_owner_choices(tuple(sorted(owner_ids)))

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
                "pagination_query": _querystring_without(self.request, "page"),
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
        messages.success(self.request, _("Work order created."))
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
        messages.warning(self.request, _("Work order updated."))
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
        messages.error(request, _("Work order deleted."))
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
            messages.success(request, _("Work order archived."))

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
        page = self.request.GET.get("b_page")
        paginator = Paginator(buildings, 30)
        try:
            buildings_page = paginator.get_page(page)
        except (TypeError, ValueError):
            buildings_page = paginator.get_page(1)

        params = self.request.GET.copy()
        params.pop("b_page", None)
        ctx["technical_support_buildings"] = buildings_page.object_list
        ctx["buildings_page"] = buildings_page
        ctx["buildings_page_query"] = params.urlencode()
        ctx["technical_support_count"] = len(buildings)
        ctx["mass_select_open"] = self.request.method == "POST" or bool(self.request.GET.get("b_page"))

        form = ctx.get("form")
        page_widgets = []
        if form is not None:
            widget_map = {}
            for checkbox in form["buildings"]:
                widget_data = getattr(checkbox, "data", {}) or {}
                widget_value = widget_data.get("value")
                if widget_value is None:
                    continue
                widget_map[widget_value] = checkbox

            buildings_field = form.fields.get("buildings")
            if buildings_field is not None:
                for building in buildings_page.object_list:
                    prepared_value = buildings_field.prepare_value(building.pk)
                    checkbox_widget = widget_map.get(prepared_value)
                    if checkbox_widget is not None:
                        page_widgets.append(checkbox_widget)
        ctx["building_checkboxes_page"] = page_widgets
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


class ArchivedWorkOrderFilterMixin:
    """
    Shared filtering helpers for archived work order views.
    """

    SUMMARY_PER_CHOICES = (10, 20, 50, 100)
    SUMMARY_PER_DEFAULT = 10
    DETAIL_PER_CHOICES = (10, 20, 50, 100)
    DETAIL_PER_DEFAULT = 10

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

    model = WorkOrder
    context_object_name = "orders"
    paginate_by = 20

    def _parse_per_param(self, value, choices, default):
        try:
            per = int(value)
        except (TypeError, ValueError):
            return default
        if per not in choices:
            return default
        return per

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

    def get_filtered_queryset(self):
        if hasattr(self, "_filtered_queryset"):
            return self._filtered_queryset

        request = self.request
        qs = (
            WorkOrder.objects.visible_to(request.user)
            .filter(archived_at__isnull=False)
            .select_related("building__owner", "unit")
        )

        self._summary_per = self._parse_per_param(
            request.GET.get("b_per"),
            self.SUMMARY_PER_CHOICES,
            self.SUMMARY_PER_DEFAULT,
        )

        qs = qs.annotate(
            priority_order=Case(
                When(priority__iexact="high", then=0),
                When(priority__iexact="medium", then=1),
                When(priority__iexact="low", then=2),
                default=3,
                output_field=IntegerField(),
            )
        )

        search = (request.GET.get("q") or "").strip()
        if search:
            qs = qs.filter(
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(building__name__icontains=search)
            )
        self._search = search

        owner_ids = list(qs.values_list("building__owner_id", flat=True).distinct())
        owner_ids = [oid for oid in owner_ids if oid]
        self._owner_choices = _cached_owner_choices(tuple(sorted(owner_ids)))

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

        summary_qs = qs.values(
            "building_id",
            "building__name",
            "building__owner__first_name",
            "building__owner__last_name",
            "building__owner__username",
        ).annotate(
            total=Count("id"),
            latest_archived=Max("archived_at"),
            earliest_archived=Min("archived_at"),
            latest_created=Max("created_at"),
            earliest_created=Min("created_at"),
            min_priority=Min("priority_order"),
            max_priority=Max("priority_order"),
        )

        summary_sort_map = {
            "archived_desc": ("-latest_archived", "-earliest_archived", "building__name", "building_id"),
            "archived_asc": ("earliest_archived", "building__name", "building_id"),
            "created_desc": ("-latest_created", "building__name", "building_id"),
            "created_asc": ("earliest_created", "building__name", "building_id"),
            "priority": ("min_priority", "building__name", "building_id"),
            "priority_desc": ("-max_priority", "building__name", "building_id"),
            "building_asc": ("building__name", "building_id"),
            "building_desc": ("-building__name", "-building_id"),
        }
        summary_order = summary_sort_map.get(sort_param, ("building__name", "building_id"))
        summary_qs = summary_qs.order_by(*summary_order)

        self._building_summary_qs = summary_qs
        self._archive_query = _querystring_without(request, "page")

        self._filtered_queryset = qs
        return qs

    def get_queryset(self):
        return self.get_filtered_queryset()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = getattr(self, "_search", "")
        ctx["owner"] = getattr(self, "_owner", "")
        ctx["owner_choices"] = getattr(self, "_owner_choices", [])
        ctx["sort"] = getattr(self, "_sort", "archived_desc")
        ctx["sort_choices"] = self._sort_choices
        ctx["archive_page_query"] = getattr(self, "_archive_query", "")
        summary_per = getattr(self, "_summary_per", self.SUMMARY_PER_DEFAULT)
        summary_qs = getattr(self, "_building_summary_qs", None)
        summary_paginator = Paginator(summary_qs, summary_per) if summary_qs is not None else None
        summary_page = None
        if summary_paginator:
            page_number = self.request.GET.get("b_page")
            try:
                summary_page = summary_paginator.get_page(page_number)
            except (TypeError, ValueError):
                summary_page = summary_paginator.get_page(1)
            processed = []
            for item in summary_page.object_list:
                owner_label = (
                    (item.get("building__owner__first_name") or "")
                    + " "
                    + (item.get("building__owner__last_name") or "")
                ).strip()
                if not owner_label:
                    owner_label = item.get("building__owner__username")
                processed.append(
                    {
                        "id": item.get("building_id"),
                        "name": item.get("building__name"),
                        "total": item.get("total"),
                        "owner": owner_label,
                    }
                )
            summary_page.object_list = processed
        ctx["building_summary_page"] = summary_page
        ctx["building_summary_total"] = summary_paginator.count if summary_paginator else 0
        ctx["building_summary_query"] = _querystring_without(self.request, "b_page")
        ctx["building_summary_per"] = summary_per
        ctx["building_summary_per_choices"] = self.SUMMARY_PER_CHOICES
        return ctx


class ArchivedWorkOrderListView(
    ArchivedWorkOrderFilterMixin,
    LoginRequiredMixin,
    UserPassesTestMixin,
    ListView,
):
    """
    Staff-only list of archived work orders grouped by building summary.
    """

    template_name = "core/work_orders_archive_list.html"
    paginate_by = None

    def get_queryset(self):
        # Pre-compute filters but avoid fetching the full order list.
        self.get_filtered_queryset()
        return WorkOrder.objects.none()


class ArchivedWorkOrderDetailView(
    ArchivedWorkOrderFilterMixin,
    LoginRequiredMixin,
    UserPassesTestMixin,
    ListView,
):
    """
    Archived work orders for a single building.
    """

    template_name = "core/work_orders_archive_detail.html"
    paginate_by = None

    def get_paginate_by(self, queryset):
        per = self._parse_per_param(
            self.request.GET.get("per"),
            self.DETAIL_PER_CHOICES,
            self.DETAIL_PER_DEFAULT,
        )
        self._detail_per = per
        return per

    def get_queryset(self):
        qs = self.get_filtered_queryset()
        building_id = self.kwargs.get("building_id")
        qs = qs.filter(building_id=building_id)
        if not qs.exists():
            raise Http404()
        self._current_building = qs[0].building
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        orders = list(ctx.get("orders", []))
        ctx["orders"] = orders
        page_obj = ctx.get("page_obj")
        ctx["pagination_object"] = page_obj
        building = getattr(self, "_current_building", None)
        if not building and orders:
            building = orders[0].building
        if not building:
            building = get_object_or_404(
                Building.objects.select_related("owner"),
                pk=self.kwargs.get("building_id"),
            )
        ctx["building"] = building
        total = page_obj.paginator.count if page_obj is not None else len(orders)
        ctx["total_archived"] = total
        detail_params = self.request.GET.copy()
        detail_params.pop("page", None)
        ctx["detail_page_query"] = detail_params.urlencode()
        ctx["back_url"] = reverse("work_orders_archive")
        ctx["back_query"] = ctx["detail_page_query"]
        ctx["detail_per"] = getattr(self, "_detail_per", self.DETAIL_PER_DEFAULT)
        ctx["detail_per_choices"] = self.DETAIL_PER_CHOICES
        return ctx
