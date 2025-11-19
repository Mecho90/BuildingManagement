from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, Q, When
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
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
from .common import (
    CachedObjectMixin,
    CapabilityRequiredMixin,
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

class BuildingListView(LoginRequiredMixin, ListView):
    model = Building
    template_name = "core/buildings_list.html"
    context_object_name = "buildings"

    def get_paginate_by(self, queryset):
        try:
            return int(self.request.GET.get("per", 25))
        except (TypeError, ValueError):
            return 25

    def get_queryset(self):
        user = self.request.user
        resolver = CapabilityResolver(user) if user.is_authenticated else None
        self._can_manage_buildings = resolver.has(Capability.MANAGE_BUILDINGS) if resolver else False

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
        return qs.order_by(sort_field, "id")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = (self.request.GET.get("q") or "").strip()
        try:
            ctx["per"] = int(self.request.GET.get("per", 25))
        except (TypeError, ValueError):
            ctx["per"] = 25
        ctx["sort"] = getattr(self, "_effective_sort", "name")
        can_manage_buildings = getattr(self, "_can_manage_buildings", False)
        ctx["show_owner_column"] = can_manage_buildings
        ctx["can_manage_buildings"] = can_manage_buildings

        ctx["pagination_query"] = _querystring_without(self.request, "page")
        paginator = ctx.get("paginator")
        if paginator is not None:
            ctx["buildings_total"] = paginator.count
        else:
            object_list = ctx.get("object_list") or []
            ctx["buildings_total"] = len(object_list)
        return ctx

class BuildingDetailView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DetailView):
    model = Building
    template_name = "core/building_detail.html"
    context_object_name = "building"

    # Only buildings the user may see
    def get_queryset(self):
        return Building.objects.visible_to(self.request.user)

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

        resolver = CapabilityResolver(request.user) if request.user.is_authenticated else None
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
        ctx["show_manage_team_button"] = can_manage_members and is_admin_for_building
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
            return "units"

        if active_tab == "overview":
            active_tab = "units"

        if active_tab not in {"units", "work_orders"}:
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

        ctx["tab_urls"] = {
            "units": _tab_url("units"),
            "work_orders": _tab_url("work_orders"),
        }

        # ========================= Units =========================
        u_q = (self.request.GET.get("u_q") or "").strip()
        u_per = self._get_int("u_per", 25)
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
                "u_active": active_tab == "units",
            }
        )
        ctx["units_pagination_query"] = _querystring_without(self.request, "u_page")

        # ===================== Work Orders =======================
        # EXACTLY the names your template uses
        w_q = (self.request.GET.get("w_q") or "").strip()
        w_per = self._get_int("w_per", 25)
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
                "w_active": active_tab == "work_orders",
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
