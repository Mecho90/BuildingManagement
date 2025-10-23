# core/views.py

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Case, When, IntegerField, Q, Count, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce, Lower, Trim, Replace
from django.http import JsonResponse, Http404, HttpResponseForbidden
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
from .forms import BuildingForm, UnitForm, WorkOrderForm
from .models import Building, Unit, WorkOrder
from .views_theme import toggle_theme


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _user_can_access_building(user, building: Building) -> bool:
    """Staff can access everything; others only their own buildings."""
    return user.is_staff or building.owner_id == user.id


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
                building_name = building.name if building_id else "your building"
                owner_label = None
                if is_admin and building_id:
                    owner = getattr(building, "owner", None)
                    if owner:
                        owner_label = owner.get_full_name() or owner.username
                days_left = (wo.deadline - today).days
                if days_left < 0:
                    due_text = f"overdue by {abs(days_left)} days"
                elif days_left == 0:
                    due_text = "due today"
                elif days_left == 1:
                    due_text = "due tomorrow"
                else:
                    due_text = f"due in {days_left} days"
                message = (
                    f"{wo.get_priority_display()} priority work order \"{wo.title}\" "
                    f"in {building_name} is {due_text} (deadline {wo.deadline})"
                )
                if owner_label:
                    message += f" (owner: {owner_label})"
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
        messages.success(self.request, "Building created.")
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
        messages.warning(self.request, "Building updated.")
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
        messages.error(request, "Building deleted.")
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
        messages.success(self.request, "Unit created.")
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
        messages.warning(self.request, "Unit updated.")
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
        messages.error(request, "Unit deleted.")
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
    template_name = "core/workorders_list.html"
    context_object_name = "workorders"

    def get_queryset(self):
        # Use visibility helper + pull related objects
        qs = WorkOrder.objects.visible_to(self.request.user).select_related("building", "unit")

        # Priority ordering: High > Medium > Low, then by deadline asc, then newest
        qs = qs.annotate(
            priority_order=Case(
                When(priority__iexact="high", then=0),
                When(priority__iexact="medium", then=1),
                When(priority__iexact="low", then=2),
                default=3,
                output_field=IntegerField(),
            )
        ).order_by("priority_order", "deadline", "-pk")
        return qs


class WorkOrderDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = WorkOrder
    template_name = "core/workorder_detail.html"
    context_object_name = "workorder"

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
        ctx["cancel_url"] = (
            reverse("building_detail", args=[self.building.pk])
            if getattr(self, "building", None)
            else reverse("workorders_list")
        )
        return ctx

    def form_valid(self, form):
        obj = form.save(commit=False)
        if not _user_can_access_building(self.request.user, obj.building):
            raise Http404()
        obj.save()
        messages.success(self.request, "Work order created.")
        return super().form_valid(form)

    def get_success_url(self):
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
        return ctx

    def test_func(self):
        wo = self.get_object()
        return _user_can_access_building(self.request.user, wo.building)

    def form_valid(self, form):
        obj = form.save(commit=False)
        if not _user_can_access_building(self.request.user, obj.building):
            raise Http404()
        obj.save()
        response = super().form_valid(form)
        messages.warning(self.request, "Work order updated.")
        return response

    def get_success_url(self):
        return reverse("building_detail", args=[self.object.building_id])


class WorkOrderDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = WorkOrder
    template_name = "core/work_order_confirm_delete.html"   # <- new specific template

    def test_func(self):
        wo = self.get_object()
        return _user_can_access_building(self.request.user, wo.building)

    def get_success_url(self):
        # after delete, go back to the building detail
        return reverse_lazy("building_detail", args=[self.object.building_id])

    def post(self, request, *args, **kwargs):
        messages.error(request, "Work order deleted.")
        return super().post(request, *args, **kwargs)
    
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj = getattr(self, "object", None) or self.get_object()
        meta = obj._meta
        ctx.setdefault("object_verbose_name", meta.verbose_name)
        ctx.setdefault("object_model_name", meta.model_name)
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
            raise Http404("Only completed work orders can be archived.")

        if not wo.is_archived:
            wo.archive()
            messages.success(request, "Work order archived.")

        return redirect("building_detail", wo.building_id)

    # Optional: allow GET-triggered archive links; remove to enforce POST-only.
    def get(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)
    
# ----------------------------------------------------------------------
# JSON APIs (simple, function-based)
# ----------------------------------------------------------------------

def api_units(request):
    """
    JSON list of units visible to the current user.
    Optional filter: ?building=<id> (validated for visibility).
    """
    if not request.user.is_authenticated:
        raise Http404()

    if request.user.is_staff:
        qs = Unit.objects.select_related("building").all()
        bld_qs = Building.objects.all()
    else:
        qs = Unit.objects.select_related("building").filter(building__owner=request.user)
        bld_qs = Building.objects.filter(owner=request.user)

    building_id = request.GET.get("building")
    if building_id:
        try:
            b_id = int(building_id)
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
