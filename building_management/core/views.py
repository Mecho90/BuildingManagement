# core/views.py
from __future__ import annotations

from typing import Any, Dict

from django.views import View
from django.http import JsonResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Q, Count, Case, When, IntegerField
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import (
    ListView,
    DetailView,
    CreateView,
    UpdateView,
    DeleteView,
)

from .models import Building, Unit, WorkOrder
from .forms import BuildingForm, UnitForm, WorkOrderForm


# =============================================================================
# Buildings
# =============================================================================
class BuildingListView(LoginRequiredMixin, ListView):
    """
    Buildings index with search, sorting, and safe annotations.

    Annotations (names avoid clashing with potential @property attributes):
      - units_total
      - work_orders_open

    Sorting map keeps old links working:
      units_count -> units_total
      work_orders_count -> work_orders_open
    """
    model = Building
    template_name = "core/buildings_list.html"
    context_object_name = "buildings"
    paginate_by = 10  # default, can be overridden by ?per=

    # ---- Query building ----
    def get_queryset(self):
        q = (self.request.GET.get("q") or "").strip()
        sort = self.request.GET.get("sort") or "name"

        qs = Building.objects.all()

        # Search across name + address (extend as needed)
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(address__icontains=q))

        # Annotations — use names that don't collide with properties
        qs = qs.annotate(
            units_total=Count("units", distinct=True),
            work_orders_open=Count(
                "work_orders",
                filter=Q(work_orders__status__in=[
                    WorkOrder.Status.OPEN,
                    WorkOrder.Status.IN_PROGRESS,
                ]),
                distinct=True,
            ),
        )

        # Legacy sort key mapping → new annotation names
        sort_map = {
            "units_count": "units_total",
            "-units_count": "-units_total",
            "work_orders_count": "work_orders_open",
            "-work_orders_count": "-work_orders_open",
        }
        sort = sort_map.get(sort, sort)

        allowed_sorts = {
            "name", "-name",
            "address", "-address",
            "units_total", "-units_total",
            "work_orders_open", "-work_orders_open",
        }
        if sort not in allowed_sorts:
            sort = "name"

        return qs.order_by(sort)

    # ---- Context ----
    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        per = int(self.request.GET.get("per") or self.paginate_by)
        # Reset paginator with chosen page size
        paginator = Paginator(ctx["buildings"], per)
        page = self.request.GET.get("page")
        page_obj = paginator.get_page(page)

        ctx.update(
            {
                "q": self.request.GET.get("q", ""),
                "sort": self.request.GET.get("sort", "name"),
                "per": per,
                "page_obj": page_obj,
                "paginator": paginator,
                "is_paginated": page_obj.has_other_pages(),
                "buildings": page_obj,  # what templates iterate over
            }
        )
        return ctx


class BuildingDetailView(LoginRequiredMixin, DetailView):
    """
    Building details with:
      - Units table: search, sort(number/floor), page size
      - Work Orders table: search, status filter, page size
        ordered by priority (High > Medium > Low) then newest
    """
    model = Building
    template_name = "core/building_detail.html"
    context_object_name = "building"

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        bld: Building = self.object
        request = self.request

        # ===== Units filters
        u_q = (request.GET.get("u_q") or "").strip()
        u_sort = request.GET.get("u_sort") or "number"
        u_per = int(request.GET.get("u_per") or 10)

        units_qs = Unit.objects.filter(building=bld)
        if u_q:
            units_qs = units_qs.filter(
                Q(number__icontains=u_q)
                | Q(floor__icontains=u_q)
                | Q(owner_name__icontains=u_q)
                | Q(contact_phone__icontains=u_q)
            )

        if u_sort.lstrip("-") in {"number", "floor"}:
            units_qs = units_qs.order_by(u_sort)

        units_paginator = Paginator(units_qs, u_per)
        units_page = units_paginator.get_page(request.GET.get("u_page"))

        # ===== Work Orders filters
        w_q = (request.GET.get("w_q") or "").strip()
        w_status = request.GET.get("w_status") or ""
        w_per = int(request.GET.get("w_per") or 10)

        w_qs = WorkOrder.objects.filter(building=bld, archived_at__isnull=True)

        if w_q:
            w_qs = w_qs.filter(Q(title__icontains=w_q) | Q(description__icontains=w_q))
        if w_status:
            w_qs = w_qs.filter(status=w_status)

        # Priority rank: High (3) > Medium (2) > Low (1)
        w_qs = w_qs.annotate(
            priority_rank=Case(
                When(priority=WorkOrder.PRIORITY_HIGH, then=3),
                When(priority=WorkOrder.PRIORITY_MEDIUM, then=2),
                When(priority=WorkOrder.PRIORITY_LOW, then=1),
                default=0,
                output_field=IntegerField(),
            )
        ).order_by("-priority_rank", "-created_at")

        work_orders_paginator = Paginator(
            w_qs.select_related("unit", "building"), w_per
        )
        work_orders_page = work_orders_paginator.get_page(request.GET.get("w_page"))

        ctx.update(
            {
                # Units bits
                "u_q": u_q,
                "u_sort": u_sort,
                "u_per": u_per,
                "units_page": units_page,
                # Work Orders bits
                "w_q": w_q,
                "w_status": w_status,
                "w_per": w_per,
                "w_status_choices": WorkOrder.Status.choices,
                "work_orders_page": work_orders_page,
                # template flags
                "can_edit": True,
            }
        )
        return ctx


class BuildingCreateView(LoginRequiredMixin, CreateView):
    model = Building
    form_class = BuildingForm
    template_name = "core/building_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.object.pk])


class BuildingUpdateView(LoginRequiredMixin, UpdateView):
    model = Building
    form_class = BuildingForm
    template_name = "core/building_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.object.pk])


class BuildingDeleteView(LoginRequiredMixin, DeleteView):
    model = Building
    template_name = "core/confirm_delete.html"
    success_url = reverse_lazy("buildings_list")


# =============================================================================
# Units
# =============================================================================
class UnitCreateView(LoginRequiredMixin, CreateView):
    """
    Creates a Unit for a specific Building.

    Accepts building id from:
      - path kwarg: pk / building_id
      - querystring: ?building=<id>
    """
    model = Unit
    form_class = UnitForm
    template_name = "core/unit_form.html"

    def _pick_building_id(self) -> int | None:
        return (
            self.kwargs.get("pk")
            or self.kwargs.get("building_id")
            or self.request.GET.get("building")
        )

    def dispatch(self, request, *args, **kwargs):
        bid = self._pick_building_id()
        self.building = get_object_or_404(Building, pk=bid)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["building"] = self.building
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = self.building
        ctx["cancel_url"] = reverse("building_detail", args=[self.building.pk])
        return ctx

    def form_valid(self, form):
        obj: Unit = form.save(commit=False)
        obj.building = self.building
        obj.save()
        form.save_m2m()
        return redirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.building.pk])


class UnitUpdateView(LoginRequiredMixin, UpdateView):
    model = Unit
    form_class = UnitForm
    template_name = "core/unit_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["building"] = self.object.building
        return kwargs

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.object.building_id])


class UnitDeleteView(LoginRequiredMixin, DeleteView):
    model = Unit
    template_name = "core/confirm_delete.html"

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.object.building_id])


# =============================================================================
# Work Orders
# =============================================================================
class WorkOrderListView(LoginRequiredMixin, ListView):
    """
    Global Work Orders list.

    Query params:
      - q:         free text (title, description, building name, unit number)
      - status:    one of WorkOrder.Status values
      - building:  building id to filter by
      - sort:      one of:
                     priority (default) | -priority
                     created  | -created
                     deadline | -deadline    (NULLs last)
                     status   | -status
                     building | -building
                     unit     | -unit
                     title    | -title
      - per:       page size (default 20)

    Sorting 'priority' shows High → Medium → Low, then newest.
    """
    model = WorkOrder
    template_name = "core/work_orders_list.html"
    context_object_name = "work_orders"
    paginate_by = 20

    def get_paginate_by(self, queryset):
        try:
            return int(self.request.GET.get("per") or self.paginate_by)
        except (TypeError, ValueError):
            return self.paginate_by

    def get_queryset(self):
        r = self.request
        q = (r.GET.get("q") or "").strip()
        status = r.GET.get("status") or ""
        building_id = r.GET.get("building") or ""
        sort = r.GET.get("sort") or "priority"

        qs = WorkOrder.objects.select_related("building", "unit").filter(archived_at__isnull=True)

        if building_id:
            qs = qs.filter(building_id=building_id)

        if status:
            qs = qs.filter(status=status)

        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(description__icontains=q)
                | Q(building__name__icontains=q)
                | Q(unit__number__icontains=q)
            )

        # Priority rank (High 3 > Medium 2 > Low 1)
        qs = qs.annotate(
            priority_rank=Case(
                When(priority=WorkOrder.PRIORITY_HIGH, then=3),
                When(priority=WorkOrder.PRIORITY_MEDIUM, then=2),
                When(priority=WorkOrder.PRIORITY_LOW, then=1),
                default=0,
                output_field=IntegerField(),
            ),
            # For NULLS LAST handling on deadline asc/desc
            deadline_isnull=Case(
                When(deadline__isnull=True, then=1),
                default=0,
                output_field=IntegerField(),
            ),
        )

        # Map logical sort keys to actual ORDER BY
        sort_map = {
            "priority": ("-priority_rank", "-created_at"),
            "-priority": ("priority_rank", "-created_at"),

            "created": ("-created_at",),
            "-created": ("created_at",),

            # NULLS LAST on ascending deadline: sort by isnull first, then deadline
            "deadline": ("deadline_isnull", "deadline"),
            "-deadline": ("-deadline_isnull", "-deadline"),

            "status": ("status", "building__name"),
            "-status": ("-status", "building__name"),

            "building": ("building__name", "-created_at"),
            "-building": ("-building__name", "-created_at"),

            "unit": ("unit__number", "-created_at"),
            "-unit": ("-unit__number", "-created_at"),

            "title": ("title",),
            "-title": ("-title",),
        }
        order_by = sort_map.get(sort, sort_map["priority"])
        return qs.order_by(*order_by)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        r = self.request
        ctx.update({
            "q": r.GET.get("q", ""),
            "status": r.GET.get("status", ""),
            "building_filter": r.GET.get("building", ""),
            "sort": r.GET.get("sort", "priority"),
            "per": int(r.GET.get("per") or self.paginate_by),
            "status_choices": WorkOrder.Status.choices,
            "priority_choices": WorkOrder.PRIORITY_CHOICES,
            "buildings": Building.objects.only("id", "name").order_by("name"),
        })
        return ctx


class WorkOrderDetailView(LoginRequiredMixin, DetailView):
    """
    Detail page for a single Work Order.

    Context extras:
      - building:            the related Building
      - back_to_building_url: URL to the building detail page
      - status_label:        human-readable status
      - priority_label:      human-readable priority
    """
    model = WorkOrder
    template_name = "core/work_order_detail.html"
    context_object_name = "work_order"

    def get_queryset(self):
        # Pull related objects to avoid N+1
        return (
            super()
            .get_queryset()
            .select_related("building", "unit")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        wo: WorkOrder = self.object
        ctx.update({
            "building": wo.building,
            "back_to_building_url": reverse("building_detail", args=[wo.building_id]),
            "status_label": wo.get_status_display(),
            "priority_label": wo.get_priority_display(),
        })
        return ctx
    

class WorkOrderCreateView(LoginRequiredMixin, CreateView):
    """
    Create a Work Order for a Building; building provided via query (?building=<id>)
    or as a path kwarg if your URL pattern includes it.
    """
    model = WorkOrder
    form_class = WorkOrderForm
    template_name = "core/work_order_form.html"

    def dispatch(self, request, *args, **kwargs):
        bid = request.GET.get("building") or kwargs.get("building_id") or kwargs.get("pk")
        self.building = get_object_or_404(Building, pk=bid)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["building"] = self.building   # set in dispatch()
        return kwargs

    def form_valid(self, form):
        obj: WorkOrder = form.save(commit=False)
        if not obj.building_id:
            obj.building = self.building
        obj.save()
        form.save_m2m()
        return redirect(self.get_success_url())

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.building.pk])


class WorkOrderDeleteView(LoginRequiredMixin, DeleteView):
    model = WorkOrder
    template_name = "core/confirm_delete.html"

    def get_success_url(self) -> str:
        # keep return to source building even if called from anywhere
        bpk = self.request.GET.get("building") or self.object.building_id
        return reverse("building_detail", args=[bpk])


# (Optional) keep update view available even if you hide the Edit button in UI.
class WorkOrderUpdateView(LoginRequiredMixin, UpdateView):
    model = WorkOrder
    form_class = WorkOrderForm
    template_name = "core/work_order_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["building"] = self.object.building
        return kwargs

    def get_success_url(self) -> str:
        return reverse("building_detail", args=[self.object.building_id])


class WorkOrderArchiveView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        wo = get_object_or_404(WorkOrder, pk=pk)
        # Allow archive only if status is DONE
        if wo.status == WorkOrder.Status.DONE:
            wo.archive()
        # send back to the building detail, preserving some filters if present
        bpk = wo.building_id
        return redirect(
            f"{reverse('building_detail', args=[bpk])}"
        )


class ApiBuildingsView(LoginRequiredMixin, View):
    """
    GET /api/buildings/?q=&sort=&per=&page=

    - q:     search in name/address
    - sort:  name|-name|address|-address|units_total|-units_total|work_orders_open|-work_orders_open
    - per:   page size (default 20)
    - page:  page number (default 1)

    Returns:
    {
      "count": <int>,
      "pages": <int>,
      "page": <int>,
      "per": <int>,
      "results": [
         {
           "id": 1,
           "name": "...",
           "address": "...",
           "units_total": 3,
           "work_orders_open": 1,
           "url": "/buildings/1/"
         },
         ...
      ]
    }
    """
    def get(self, request, *args, **kwargs):
        # Query params
        q = (request.GET.get("q") or "").strip()
        sort = request.GET.get("sort") or "name"

        try:
            per = int(request.GET.get("per") or 20)
        except (TypeError, ValueError):
            per = 20
        per = max(1, min(per, 200))  # basic guard

        try:
            page_num = int(request.GET.get("page") or 1)
        except (TypeError, ValueError):
            page_num = 1
        page_num = max(1, page_num)

        qs = Building.objects.all()

        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(address__icontains=q))

        # Keep annotation names unique (no clash with properties)
        qs = qs.annotate(
            units_total=Count("units", distinct=True),
            work_orders_open=Count(
                "work_orders",
                filter=Q(work_orders__status__in=[
                    WorkOrder.Status.OPEN,
                    WorkOrder.Status.IN_PROGRESS,
                ]),
                distinct=True,
            ),
        )

        # Map legacy keys → current keys (optional)
        sort_map = {
            "units_count": "units_total",
            "-units_count": "-units_total",
            "work_orders_count": "work_orders_open",
            "-work_orders_count": "-work_orders_open",
        }
        sort = sort_map.get(sort, sort)

        allowed = {
            "name", "-name",
            "address", "-address",
            "units_total", "-units_total",
            "work_orders_open", "-work_orders_open",
        }
        if sort not in allowed:
            sort = "name"

        qs = qs.order_by(sort)

        # Paginate
        paginator = Paginator(qs, per)
        page_obj = paginator.get_page(page_num)

        # Build results
        results = []
        for b in page_obj.object_list:
            results.append({
                "id": b.id,
                "name": b.name,
                "address": b.address,
                "units_total": getattr(b, "units_total", 0),
                "work_orders_open": getattr(b, "work_orders_open", 0),
                "url": reverse("building_detail", args=[b.id]),
            })

        data = {
            "count": paginator.count,
            "pages": paginator.num_pages,
            "page": page_obj.number,
            "per": per,
            "results": results,
        }
        return JsonResponse(data, status=200)
    
class ApiUnitsView(LoginRequiredMixin, View):
    """
    GET /api/units/?building=&q=&occupied=&sort=&per=&page=

    Query params
    - building:  Building ID to filter by (optional)
    - q:         free text; searches owner/contact and, if numeric, number/floor
    - occupied:  true/false/1/0/yes/no (optional)
    - sort:      number | -number | floor | -floor | owner_name | -owner_name
                 contact | -contact | occupied | -occupied | building | -building
                 (aliases: owner -> owner_name, contact -> contact_phone)
    - per:       page size (default 20, max 200)
    - page:      page number (default 1)

    Response
    {
      "count": <int>,
      "pages": <int>,
      "page": <int>,
      "per": <int>,
      "results": [
        {
          "id": 12,
          "building": {"id": 3, "name": "Sunset Court"},
          "number": 101,
          "floor": 1,
          "owner_name": "Jane Doe",
          "contact_phone": "+359888123456",
          "is_occupied": true,
          "urls": {
            "building": "/buildings/3/",
            "update": "/units/12/edit/",
            "delete": "/units/12/delete/"
          }
        },
        ...
      ]
    }
    """
    def get(self, request, *args, **kwargs):
        # ----- Parse inputs
        r = request
        building_id = r.GET.get("building")
        q = (r.GET.get("q") or "").strip()
        occupied_raw = (r.GET.get("occupied") or "").strip().lower()
        sort = (r.GET.get("sort") or "number").strip()

        try:
            per = int(r.GET.get("per") or 20)
        except (TypeError, ValueError):
            per = 20
        per = max(1, min(per, 200))

        try:
            page_num = int(r.GET.get("page") or 1)
        except (TypeError, ValueError):
            page_num = 1
        page_num = max(1, page_num)

        # ----- Base queryset
        qs = Unit.objects.select_related("building")

        if building_id:
            qs = qs.filter(building_id=building_id)

        # occupied filter
        truthy = {"1", "true", "yes", "y"}
        falsy = {"0", "false", "no", "n"}
        if occupied_raw in truthy:
            qs = qs.filter(is_occupied=True)
        elif occupied_raw in falsy:
            qs = qs.filter(is_occupied=False)

        # search
        if q:
            numeric = q.isdigit()
            cond = (
                Q(owner_name__icontains=q) |
                Q(contact_phone__icontains=q) |
                Q(building__name__icontains=q)
            )
            if numeric:
                # match exact on number/floor when q is numeric
                cond = cond | Q(number=int(q)) | Q(floor=int(q))
            qs = qs.filter(cond)

        # ----- Sorting (map friendly keys to ORM fields)
        sort_map = {
            "number": "number",
            "-number": "-number",
            "floor": "floor",
            "-floor": "-floor",
            "owner": "owner_name",
            "-owner": "-owner_name",
            "owner_name": "owner_name",
            "-owner_name": "-owner_name",
            "contact": "contact_phone",
            "-contact": "-contact_phone",
            "contact_phone": "contact_phone",
            "-contact_phone": "-contact_phone",
            "occupied": "is_occupied",
            "-occupied": "-is_occupied",
            "building": "building__name",
            "-building": "-building__name",
        }
        order_by = sort_map.get(sort, "number")
        qs = qs.order_by(order_by, "id")  # stable secondary key

        # ----- Paginate
        paginator = Paginator(qs, per)
        page_obj = paginator.get_page(page_num)

        # ----- Build payload
        results = []
        for u in page_obj.object_list:
            results.append({
                "id": u.id,
                "building": {"id": u.building_id, "name": u.building.name},
                "number": u.number,
                "floor": u.floor,
                "owner_name": u.owner_name or "",
                "contact_phone": u.contact_phone or "",
                "is_occupied": bool(u.is_occupied),
                "urls": {
                    "building": reverse("building_detail", args=[u.building_id]),
                    # keep these even if Edit button is hidden in UI
                    "update": reverse("unit_update", args=[u.id]),
                    "delete": reverse("unit_delete", args=[u.id]),
                },
            })

        data = {
            "count": paginator.count,
            "pages": paginator.num_pages,
            "page": page_obj.number,
            "per": per,
            "results": results,
        }
        return JsonResponse(data, status=200)
    