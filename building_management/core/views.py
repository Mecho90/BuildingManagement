# path: core/views.py
from __future__ import annotations

from typing import Any, Dict

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Q, Count
from django.http import Http404, JsonResponse, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import NoReverseMatch, reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import BuildingForm, UnitForm, WorkOrderForm
from .models import Building, Unit, WorkOrder


# -----------------------------
# Helpers / shared mixins
# -----------------------------
class NavContextMixin:
    """
    Adds nav-related flags so templates don't call {% url %} for routes
    that may not exist, preventing NoReverseMatch.
    """
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["work_orders_enabled"] = False
        ctx["work_orders_url"] = None
        try:
            url = reverse("work_orders_list")
        except NoReverseMatch:
            url = None
        else:
            ctx["work_orders_enabled"] = True
            ctx["work_orders_url"] = url
        return ctx


def is_admin(user) -> bool:
    return bool(user and user.is_staff)


def user_owns_building(user, building: Building) -> bool:
    return building.owner_id == getattr(user, "id", None)


# -----------------------------
# Buildings
# -----------------------------
class BuildingListView(NavContextMixin, LoginRequiredMixin, ListView):
    model = Building
    template_name = "core/buildings_list.html"
    context_object_name = "buildings"
    ALLOWED_SORT = {"name", "address", "units_count", "-name", "-address", "-units_count"}

    def _session_key_per(self) -> str:
        return "b_per"

    def get_queryset(self):
        user = self.request.user
        manager = Building.objects
        if hasattr(manager, "with_unit_stats"):
            qs = manager.with_unit_stats().select_related("owner")
        else:
            qs = manager.all().select_related("owner").annotate(
                units_count=Count("units", distinct=True)
            )

        if not is_admin(user):
            qs = qs.filter(owner=user)

        q = (self.request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(address__icontains=q)
                | Q(description__icontains=q)
                | Q(owner__username__icontains=q)
            )

        sort = (self.request.GET.get("sort") or "name").strip()
        if sort not in self.ALLOWED_SORT:
            sort = "name"
        return qs.order_by(sort)

    def get_paginate_by(self, queryset):
        req = self.request
        per = req.GET.get("per")
        skey = self._session_key_per()
        if per is not None:
            per = int(per) if str(per).isdigit() else 10
            req.session[skey] = per
        else:
            per = int(req.session.get(skey, 10))
        return max(1, per)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        req = self.request
        ctx["q"] = (req.GET.get("q") or "").strip()
        ctx["sort"] = (req.GET.get("sort") or "name").strip()
        ctx["per"] = self.get_paginate_by(self.get_queryset())
        ctx["per_choices"] = [10, 20, 50, 100]
        ctx["show_owner_col"] = is_admin(req.user)
        return ctx


class BuildingDetailView(NavContextMixin, LoginRequiredMixin, DetailView):
    """
    Detail page:
      - permission check for non-admin owners
      - Units: ?u_q, ?u_sort, ?u_per, ?u_page
      - Work Orders (scoped to this building): ?w_q, ?w_status, ?w_per, ?w_page
    """
    model = Building
    template_name = "core/building_detail.html"
    context_object_name = "building"

    # UI alias → DB field
    ALLOWED_UNIT_SORT = {
        "number", "floor", "is_occupied",
        "-number", "-floor", "-is_occupied",
        "occupied", "-occupied",
    }
    SORT_ALIAS = {"occupied": "is_occupied", "-occupied": "-is_occupied"}

    def _check_permission(self, building: Building):
        if is_admin(self.request.user):
            return
        if not user_owns_building(self.request.user, building):
            raise Http404("Not found")

    def _session_key_u_per(self) -> str:
        return "u_per"

    def _session_key_w_per(self) -> str:
        return "w_per"

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        resp = super().get(request, *args, **kwargs)
        self._check_permission(self.object)
        return resp

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        req = self.request
        building: Building = self.object

        # ---------- Units: search + sort + pagination ----------
        units_qs = Unit.objects.filter(building=building)

        u_q = (req.GET.get("u_q") or "").strip()
        if u_q:
            filters = Q(number__icontains=u_q) | Q(description__icontains=u_q)
            if u_q.isdigit():
                filters |= Q(floor=int(u_q))
            units_qs = units_qs.filter(filters)

        u_sort_raw = (req.GET.get("u_sort") or "number").strip()
        u_sort = self.SORT_ALIAS.get(u_sort_raw, u_sort_raw)
        if u_sort not in self.ALLOWED_UNIT_SORT:
            u_sort = "number"
        units_qs = units_qs.order_by(u_sort)

        skey_u = self._session_key_u_per()
        raw_u_per = req.GET.get("u_per")
        if raw_u_per is not None:
            try:
                u_per = max(1, int(raw_u_per))
            except ValueError:
                u_per = 10
            req.session[skey_u] = u_per
        else:
            u_per = int(req.session.get(skey_u, 10))

        u_paginator = Paginator(units_qs, u_per)
        u_page_num = req.GET.get("u_page") or 1
        units_page = u_paginator.get_page(u_page_num)

        # ---------- Work Orders: search + status + pagination ----------
        w_q = (req.GET.get("w_q") or "").strip()
        w_status = (req.GET.get("w_status") or "").strip()

        w_qs = (
            WorkOrder.objects.filter(unit__building=building)
            .select_related("unit", "unit__building")
            .order_by("-created_at")
        )
        if w_q:
            w_qs = w_qs.filter(Q(title__icontains=w_q) | Q(description__icontains=w_q))

        choices_map = dict(WorkOrder.Status.choices)
        if w_status in choices_map:
            w_qs = w_qs.filter(status=w_status)

        skey_w = self._session_key_w_per()
        raw_w_per = req.GET.get("w_per")
        if raw_w_per is not None:
            try:
                w_per = max(1, int(raw_w_per))
            except ValueError:
                w_per = 10
            req.session[skey_w] = w_per
        else:
            w_per = int(req.session.get(skey_w, 10))

        w_paginator = Paginator(w_qs, w_per)
        w_page_num = req.GET.get("w_page") or 1
        work_orders_page = w_paginator.get_page(w_page_num)

        # ---------- expose to template ----------
        # Units
        ctx["units_page"] = units_page
        ctx["u_q"] = u_q
        ctx["u_sort"] = u_sort_raw  # keep UI value, not alias
        ctx["u_per"] = u_per

        # Work orders
        ctx["w_q"] = w_q
        ctx["w_status"] = w_status
        ctx["w_status_choices"] = WorkOrder.Status.choices
        ctx["w_per"] = w_per
        ctx["work_orders_page"] = work_orders_page

        # Permissions (enables Add buttons)
        ctx["can_edit"] = is_admin(req.user) or user_owns_building(req.user, building)

        return ctx


class BuildingCreateView(NavContextMixin, LoginRequiredMixin, CreateView):
    model = Building
    form_class = BuildingForm
    template_name = "core/building_form.html"

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        if not is_admin(self.request.user):
            form.instance.owner = self.request.user
        return super().form_valid(form)

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.object.pk])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["cancel_url"] = reverse("buildings_list")
        return ctx


class BuildingUpdateView(NavContextMixin, LoginRequiredMixin, UpdateView):
    model = Building
    form_class = BuildingForm
    template_name = "core/building_form.html"

    def get_queryset(self):
        qs = super().get_queryset()
        if not is_admin(self.request.user):
            qs = qs.filter(owner=self.request.user)
        return qs

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.object.pk])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["cancel_url"] = reverse("building_detail", args=[self.object.pk])
        return ctx


class BuildingDeleteView(NavContextMixin, LoginRequiredMixin, DeleteView):
    model = Building
    template_name = "core/building_confirm_delete.html"
    success_url = reverse_lazy("buildings_list")

    def get_queryset(self):
        qs = super().get_queryset()
        if not is_admin(self.request.user):
            qs = qs.filter(owner=self.request.user)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["cancel_url"] = reverse("buildings_list")
        return ctx


# -----------------------------
# Units
# -----------------------------
class UnitCreateView(NavContextMixin, LoginRequiredMixin, CreateView):
    model = Unit
    form_class = UnitForm
    template_name = "core/unit_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.building = get_object_or_404(Building, pk=kwargs.get("building_id"))
        if not (is_admin(request.user) or user_owns_building(request.user, self.building)):
            raise Http404("Not found")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = self.building
        ctx["cancel_url"] = reverse("building_detail", args=[self.building.pk])
        return ctx

    def form_valid(self, form):
        form.instance.building = self.building
        return super().form_valid(form)

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.building.pk])


class UnitUpdateView(NavContextMixin, LoginRequiredMixin, UpdateView):
    model = Unit
    form_class = UnitForm
    template_name = "core/unit_form.html"

    def get_queryset(self):
        qs = super().get_queryset().select_related("building")
        if not is_admin(self.request.user):
            qs = qs.filter(building__owner=self.request.user)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = self.object.building
        ctx["cancel_url"] = reverse("building_detail", args=[self.object.building_id])
        return ctx

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.object.building_id])


class UnitDeleteView(NavContextMixin, LoginRequiredMixin, DeleteView):
    model = Unit
    template_name = "core/unit_confirm_delete.html"

    def get_queryset(self):
        qs = super().get_queryset().select_related("building")
        if not is_admin(self.request.user):
            qs = qs.filter(building__owner=self.request.user)
        return qs

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.object.building_id])


# -----------------------------
# Lightweight APIs
# -----------------------------
def api_buildings(request: HttpRequest) -> JsonResponse:
    """
    Basic JSON list of buildings the user can see, including units_count.
    """
    manager = Building.objects
    if hasattr(manager, "with_unit_stats"):
        qs = manager.with_unit_stats()
    else:
        qs = manager.all().annotate(units_count=Count("units", distinct=True))

    if not is_admin(request.user):
        qs = qs.filter(owner=request.user)

    data = [
        {
            "id": b.id,
            "name": b.name,
            "address": b.address,
            "owner": b.owner.username if b.owner_id else None,
            "units_count": getattr(b, "units_count", 0),
        }
        for b in qs.select_related("owner")
    ]
    return JsonResponse({"results": data})


def api_units(request: HttpRequest) -> JsonResponse:
    """
    JSON list of units for a building if provided (?building=<id>).
    """
    qs = Unit.objects.all().select_related("building")
    b_id = request.GET.get("building")
    if b_id and str(b_id).isdigit():
        qs = qs.filter(building_id=int(b_id))

    if not is_admin(request.user):
        qs = qs.filter(building__owner=request.user)

    data = [
        {
            "id": u.id,
            "building_id": u.building_id,
            "number": u.number,
            "floor": u.floor,
            "occupied": u.occupied,  # property mapped to model's is_occupied
            "description": u.description,
        }
        for u in qs
    ]
    return JsonResponse({"results": data})


# -----------------------------
# WorkOrders
# -----------------------------
class WorkOrderDetailView(NavContextMixin, LoginRequiredMixin, DetailView):
    model = WorkOrder
    template_name = "core/work_order_detail.html"
    context_object_name = "order"

    def get_queryset(self):
        qs = super().get_queryset().select_related("unit", "unit__building")
        if not is_admin(self.request.user):
            qs = qs.filter(unit__building__owner=self.request.user)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Prefer cancel to building if provided
        bid = self.request.GET.get("building")
        if bid and str(bid).isdigit():
            ctx["cancel_url"] = reverse("building_detail", args=[int(bid)])
        else:
            ctx["cancel_url"] = reverse("work_orders_list")
        return ctx
    
class WorkOrderListView(NavContextMixin, LoginRequiredMixin, ListView):
    model = WorkOrder
    template_name = "core/work_orders_list.html"
    context_object_name = "orders"

    def get_queryset(self):
        qs = (
            WorkOrder.objects.all()
            .select_related("unit", "unit__building")
            .order_by("-created_at")
        )
        user = self.request.user
        if not is_admin(user):
            qs = qs.filter(unit__building__owner=user)

        q = (self.request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))

        st = (self.request.GET.get("status") or "").strip()
        if st in dict(WorkOrder.Status.choices):
            qs = qs.filter(status=st)

        return qs

    def get_paginate_by(self, queryset):
        per = self.request.GET.get("per")
        return int(per) if (per and per.isdigit()) else 10

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        req = self.request
        ctx["q"] = (req.GET.get("q") or "").strip()
        ctx["status"] = (req.GET.get("status") or "").strip()
        ctx["per"] = self.get_paginate_by(self.get_queryset())
        ctx["per_choices"] = [10, 20, 50, 100]
        ctx["status_choices"] = WorkOrder.Status.choices
        return ctx


class _WorkOrderBase(NavContextMixin, LoginRequiredMixin):
    model = WorkOrder
    form_class = WorkOrderForm
    success_url = reverse_lazy("work_orders_list")
    
    def _building_for_context(self):
        """Resolve a Building to return to (prefer ?building=, else from object)."""
        b = self._requested_building()
        if not b and getattr(self, "object", None) and self.object.unit_id:
            b = self.object.unit.building
        return b

    def _requested_building(self):
        bid = self.request.GET.get("building")
        if bid and str(bid).isdigit():
            return get_object_or_404(Building, pk=int(bid))
        return None

    def get_success_url(self):
        """After create/update/delete, return to the building page if we can."""
        b = self._building_for_context()
        if b:
            return reverse("building_detail", args=[b.pk])
        return super().get_success_url()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        # Prefer explicit ?building=, otherwise derive from selected unit (on update)
        b = self._requested_building()
        if b is None and getattr(self, "object", None) and self.object.unit_id:
            b = self.object.unit.building
        if b is not None:
            kwargs["building"] = b
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        unit_id = self.request.GET.get("unit")
        if unit_id and unit_id.isdigit():
            initial["unit"] = int(unit_id)
        return initial

    def get_queryset(self):
        qs = super().get_queryset().select_related("unit", "unit__building")
        if not is_admin(self.request.user):
            qs = qs.filter(unit__building__owner=self.request.user)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Prefer cancel to building detail if we know the building
        b = self._requested_building()
        if not b and getattr(self, "object", None) and self.object.unit_id:
            b = self.object.unit.building
        ctx["cancel_url"] = reverse("building_detail", args=[b.pk]) if b else reverse("work_orders_list")
        return ctx


class WorkOrderCreateView(_WorkOrderBase, CreateView):
    template_name = "core/work_order_form.html"


class WorkOrderUpdateView(_WorkOrderBase, UpdateView):
    template_name = "core/work_order_form.html"


class WorkOrderDeleteView(_WorkOrderBase, DeleteView):
    template_name = "core/work_order_confirm_delete.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["cancel_url"] = reverse("work_orders_list")
        return ctx
