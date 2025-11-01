from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, Q, When
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from ..forms import BuildingForm, UnitForm
from ..models import Building, Notification, Unit, WorkOrder, UserSecurityProfile
from ..services import NotificationService
from .common import CachedObjectMixin, _querystring_without, _user_can_access_building


__all__ = [
    "BuildingListView",
    "BuildingDetailView",
    "BuildingCreateView",
    "BuildingUpdateView",
    "BuildingDeleteView",
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

        notifications_list = self._build_notifications()
        note_paginator = Paginator(notifications_list, 5)
        note_page_number = self.request.GET.get("note_page")
        try:
            notifications_page = note_paginator.get_page(note_page_number)
        except (TypeError, ValueError):
            notifications_page = note_paginator.get_page(1)

        params = self.request.GET.copy()
        params.pop("note_page", None)
        ctx["notifications_page"] = notifications_page
        ctx["note_page_query"] = params.urlencode()
        ctx["notifications"] = notifications_page.object_list
        ctx["pagination_query"] = _querystring_without(self.request, "page")
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

        request_language = getattr(self.request, "LANGUAGE_CODE", translation.get_language())
        with translation.override(request_language):
            level_labels = {
                Notification.Level.INFO.value: _("Info"),
                Notification.Level.WARNING.value: _("Warning"),
                Notification.Level.DANGER.value: _("Danger"),
            }

        level_weights = {
            Notification.Level.DANGER.value: 0,
            Notification.Level.WARNING.value: 1,
            Notification.Level.INFO.value: 2,
        }

        for note in deadline_notifications:
            notifications.append(
                {
                    "id": note.key,
                    "level": note.level,
                    "level_label": level_labels.get(note.level, note.get_level_display()),
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
                    "level_label": level_labels.get(note.level, note.get_level_display()),
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
                        "level": Notification.Level.DANGER.value,
                        "level_label": level_labels.get(Notification.Level.DANGER.value, Notification.Level.DANGER.label),
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
        ctx["units_pagination_query"] = _querystring_without(self.request, "u_page")

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
        ctx["workorders_pagination_query"] = _querystring_without(self.request, "w_page")

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
        messages.success(self.request, _("Building created."))
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
        messages.warning(self.request, _("Building updated."))
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
        messages.error(request, _("Building deleted."))
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj = getattr(self, "object", None) or self.get_object()
        meta = obj._meta
        ctx.setdefault("object_verbose_name", meta.verbose_name)
        ctx.setdefault("object_model_name", meta.model_name)
        ctx.setdefault("cancel_url", reverse("building_detail", args=[obj.pk]))
        return ctx

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
        messages.success(self.request, _("Unit created."))
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
        messages.warning(self.request, _("Unit updated."))
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
        messages.error(request, _("Unit deleted."))
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        obj = getattr(self, "object", None) or self.get_object()
        meta = obj._meta
        ctx.setdefault("object_verbose_name", meta.verbose_name)
        ctx.setdefault("object_model_name", meta.model_name)
        ctx.setdefault("cancel_url", reverse("building_detail", args=[self.building.pk]))
        return ctx
