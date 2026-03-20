from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from functools import lru_cache
from urllib.parse import quote_plus, urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import FieldDoesNotExist
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Max, Min, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.conf import settings
from django.utils import formats, timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _, gettext_lazy as _lazy, ngettext
from django.template.defaultfilters import filesizeformat
from django.template.loader import render_to_string
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, FormView, ListView, UpdateView

from ..authz import Capability, CapabilityResolver, log_workorder_action
from ..forms import ArchivePurgeForm, MassAssignWorkOrdersForm, WorkOrderBudgetChargeForm, WorkOrderForm
from ..models import (
    BudgetFeatureFlag,
    Building,
    BuildingMembership,
    MembershipRole,
    WorkOrder,
    WorkOrderAttachment,
    WorkOrderAuditLog,
)
from ..utils.metrics import log_duration
from ..utils.roles import (
    user_can_approve_work_orders,
    user_has_role,
    user_is_admin_or_backoffice,
    user_is_lawyer,
)
from ..services.notifications import (
    notify_approvers_of_pending_order,
    notify_building_technicians_of_mass_assignment,
    notify_forwarded_work_order,
    notify_forwarding_reset,
)
from .common import (
    CachedObjectMixin,
    CapabilityRequiredMixin,
    attach_expense_totals_by_metadata,
    _querystring_without,
    _safe_next_url,
    _user_can_access_building,
    _user_has_building_capability,
    format_attachment_delete_confirm,
)

logger = logging.getLogger(__name__)
User = get_user_model()


__all__ = [
    "LawyerWorkOrderListView",
    "WorkOrderListView",
    "WorkOrderDetailView",
    "WorkOrderCreateView",
    "WorkOrderUpdateView",
    "WorkOrderDeleteView",
    "WorkOrderArchiveView",
    "MassAssignWorkOrdersView",
    "ArchivedWorkOrderListView",
    "ArchivedWorkOrderDetailView",
    "WorkOrderAttachmentDeleteView",
    "WorkOrderApprovalDecisionView",
    "WorkOrderBudgetChargeView",
    "ArchivedWorkOrderPurgeView",
]


def _user_can_filter_owner(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return BuildingMembership.objects.filter(
        user=user,
        role__in=[MembershipRole.ADMINISTRATOR, MembershipRole.BACKOFFICE],
    ).exists()


def _maybe_handle_forwarding_change(request, form, work_order):
    if not getattr(form, "forwarding_changed", False):
        return
    actor = request.user if request.user.is_authenticated else None
    target = getattr(work_order, "forwarded_to_building", None)
    previous_target_id = getattr(form, "forwarding_previous_id", None)
    previous_target = None
    if previous_target_id:
        previous_target = Building.objects.filter(pk=previous_target_id).first()
    payload = {
        "target_id": getattr(target, "pk", None),
        "target_name": getattr(target, "name", ""),
        "previous_target_id": previous_target_id,
        "previous_target_name": getattr(previous_target, "name", "") if previous_target else "",
        "note": work_order.forward_note or "",
        "cleared": target is None,
    }
    log_workorder_action(
        actor=actor,
        work_order=work_order,
        action=WorkOrderAuditLog.Action.REASSIGNED,
        payload=payload,
    )
    if target:
        notify_forwarded_work_order(work_order, actor=actor)
    else:
        notify_forwarding_reset(
            work_order,
            previous_building=previous_target,
            actor=actor,
        )


def _is_forwarded_office_order(order: WorkOrder) -> bool:
    return bool(
        getattr(order, "forwarded_to_building_id", None)
        and getattr(getattr(order, "building", None), "is_system_default", False)
    )


def _is_technician_only_user(user) -> bool:
    return user_has_role(user, MembershipRole.TECHNICIAN) and not (
        user_has_role(user, MembershipRole.BACKOFFICE)
        or user_has_role(user, MembershipRole.ADMINISTRATOR)
    )


def _technician_readonly_for_forwarded_office_order(user, order: WorkOrder) -> bool:
    # Once Office-forwarded orders are in office-review/final states,
    # technician-only users should no longer edit/archive them.
    return (
        _is_forwarded_office_order(order)
        and _is_technician_only_user(user)
        and order.status in {
            WorkOrder.Status.AWAITING_APPROVAL,
            WorkOrder.Status.DONE,
            WorkOrder.Status.APPROVED,
        }
    )


class LawyerOrAdminRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return user_is_lawyer(user) or user_has_role(user, MembershipRole.ADMINISTRATOR)


class LawyerWorkOrderListView(LoginRequiredMixin, LawyerOrAdminRequiredMixin, ListView):
    model = WorkOrder
    template_name = "core/lawyer_work_orders.html"
    context_object_name = "work_orders"
    paginate_by = 25
    _per_choices = (25, 50, 100, 200)

    def get_queryset(self):
        request = self.request
        user = request.user
        resolver = CapabilityResolver(user)
        self._can_view_all = resolver.has(Capability.VIEW_ALL_BUILDINGS)

        # pagination size
        try:
            per = int(request.GET.get("per", self.paginate_by))
        except (TypeError, ValueError):
            per = self.paginate_by
        self._per = per

        search = (request.GET.get("q") or "").strip()
        status = (request.GET.get("status") or "").strip().upper()
        priority = (request.GET.get("priority") or "").strip().upper()
        deadline_range_raw = (request.GET.get("deadline_range") or "").strip()
        deadline_from = None
        deadline_to = None
        if deadline_range_raw:
            normalized = deadline_range_raw.replace(" to ", "/").replace("–", "/").replace("—", "/")
            parts = [part.strip() for part in normalized.split("/") if part.strip()]
            if parts:
                deadline_from = parse_date(parts[0])
                if len(parts) > 1:
                    deadline_to = parse_date(parts[1])

        valid_status = {choice[0] for choice in WorkOrder.Status.choices}
        valid_priority = {choice[0] for choice in WorkOrder.Priority.choices}
        if status and status not in valid_status:
            status = ""
        if priority and priority not in valid_priority:
            priority = ""

        qs = (
            WorkOrder.objects.visible_to(user)
            .filter(lawyer_only=True, archived_at__isnull=True)
            .select_related("building__owner", "created_by", "unit", "forwarded_to_building", "forwarded_by")
        )

        # owner choices
        self._owner_choices = []
        owner_ids = [
            oid
            for oid in qs.annotate(
                owner_choice_id=Coalesce("forwarded_to_building__owner_id", "building__owner_id")
            ).values_list("owner_choice_id", flat=True).distinct()
            if oid
        ]
        self._owner_choices = _cached_owner_choices(tuple(sorted(owner_ids)))

        qs = qs.annotate(
            priority_order=Case(
                When(priority__iexact="high", then=0),
                When(priority__iexact="medium", then=1),
                When(priority__iexact="low", then=2),
                default=3,
                output_field=IntegerField(),
            ),
            effective_owner_id=Case(
                When(forwarded_to_building__owner_id__isnull=False, then=F("forwarded_to_building__owner_id")),
                default=F("building__owner_id"),
                output_field=IntegerField(),
            ),
        )

        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(description__icontains=search))
        if status:
            qs = qs.filter(status=status)
        if priority:
            qs = qs.filter(priority=priority)
        if deadline_from:
            qs = qs.filter(deadline__gte=deadline_from)
        if deadline_to:
            qs = qs.filter(deadline__lte=deadline_to)
        if (deadline_from is None) and (deadline_to is None):
            deadline_range_raw = ""

        owner_param = (request.GET.get("owner") or "").strip()
        owner_filter = None
        if owner_param:
            try:
                owner_filter = int(owner_param)
            except (TypeError, ValueError):
                owner_param = ""
                owner_filter = None
        if owner_filter:
            qs = qs.filter(effective_owner_id=owner_filter)
        else:
            owner_param = ""

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

        qs = qs.order_by(*sort_map[sort_param])

        self._search = search
        self._status = status
        self._priority = priority
        self._owner = owner_param
        self._sort = sort_param
        self._deadline_range = deadline_range_raw

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["work_orders"] = attach_expense_totals_by_metadata(
            ctx.get("work_orders"),
            metadata_key="work_order_id",
            metadata_keys=("work_order_id", "work_order"),
            include_work_order_text_fallback=True,
            target_attr="expense_total",
        )
        paginator = ctx.get("paginator")
        if paginator is not None:
            ctx["lawyer_orders_total"] = paginator.count
        else:
            ctx["lawyer_orders_total"] = len(ctx.get("work_orders") or [])
        ctx.update(
            {
                "q": getattr(self, "_search", ""),
                "status": getattr(self, "_status", ""),
                "priority": getattr(self, "_priority", ""),
                "deadline_range": getattr(self, "_deadline_range", ""),
                "deadline_from": getattr(self, "_deadline_from", ""),
                "deadline_to": getattr(self, "_deadline_to", ""),
                "status_choices": WorkOrder.Status.choices,
                "priority_choices": WorkOrder.Priority.choices,
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
                    ("owner", _("Отговорник (A → Z)")),
                    ("owner_desc", _("Отговорник (Z → A)")),
                ],
                "pagination_query": _querystring_without(self.request, "page"),
                "show_owner_info": True,
                "can_mass_delete_lawyer_orders": bool(getattr(self.request.user, "is_superuser", False)),
                "mass_delete_lawyer_orders_url": reverse("core:mass_delete_lawyer_work_orders"),
                "can_add_lawyer_order": not (
                    bool(getattr(self.request.user, "is_superuser", False))
                    or user_has_role(self.request.user, MembershipRole.ADMINISTRATOR)
                ),
            }
        )
        return ctx

    def get_paginate_by(self, queryset):
        per = getattr(self, "_per", self.paginate_by)
        if per not in self._per_choices:
            per = self.paginate_by
        return per


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


def _office_viewer_enabled(request) -> bool:
    if not getattr(settings, "ATTACHMENTS_OFFICE_VIEWER_ENABLED", True):
        return False
    host = request.get_host().split(":", 1)[0]
    if host in {"127.0.0.1", "localhost"}:
        return False
    return True


def _build_attachment_panel_context(request, order: WorkOrder | None):
    attachment_items: list[dict[str, object]] = []
    can_manage = False
    attachments_api_url = ""
    delete_template = ""
    upload_disabled_reason = ""
    doc_extensions = {"doc", "docx", "odt", "rtf", "txt"}
    office_protocol_map = {
        "doc": "ms-word:ofe|u|{url}",
        "docx": "ms-word:ofe|u|{url}",
        "xls": "ms-excel:ofe|u|{url}",
        "xlsx": "ms-excel:ofe|u|{url}",
        "ppt": "ms-powerpoint:ofv|u|{url}",
        "pptx": "ms-powerpoint:ofv|u|{url}",
        "pps": "ms-powerpoint:ofv|u|{url}",
        "ppsx": "ms-powerpoint:ofv|u|{url}",
    }

    office_viewer_allowed = _office_viewer_enabled(request)

    if order and getattr(order, "pk", None):
        attachments = list(order.attachments.order_by("-created_at"))
        for attachment in attachments:
            url = ""
            try:
                url = attachment.file.url
            except ValueError:
                url = ""
            absolute_url = request.build_absolute_uri(url) if url else ""

            filename = (attachment.original_name or "").strip()
            if not filename:
                file_attr = getattr(attachment.file, "name", "")
                if file_attr:
                    filename = Path(file_attr).name.strip()
            if not filename:
                filename = _("Attachment %(id)s") % {"id": attachment.pk}

            mime = (attachment.content_type or "").lower()
            is_image = mime.startswith("image/")
            extension = Path(filename).suffix.lower().lstrip(".")
            size_raw = getattr(attachment, "size", 0) or 0
            size_label = filesizeformat(size_raw) if size_raw else ""
            created = timezone.localtime(attachment.created_at)

            if is_image:
                category = "image"
            elif extension == "pdf":
                category = "pdf"
            elif extension in doc_extensions:
                category = "doc"
            else:
                category = "file"

            office_exts = {
                "doc",
                "docx",
                "xls",
                "xlsx",
                "ppt",
                "pptx",
                "pps",
                "ppsx",
                "odt",
                "ods",
                "odp",
            }
            office_viewer_template = getattr(
                settings,
                "ATTACHMENTS_OFFICE_VIEWER_URL",
                "https://view.officeapps.live.com/op/embed.aspx?src={url}",
            )
            preview_url = None
            preview_external = False
            if is_image:
                preview_url = url
            elif category == "pdf":
                preview_url = url
            elif category == "doc" and absolute_url:
                if office_viewer_allowed:
                    preview_url = office_viewer_template.format(url=quote_plus(absolute_url))
                else:
                    proto = office_protocol_map.get(extension)
                    if proto:
                        preview_url = proto.format(url=absolute_url)
                        preview_external = True

            delete_confirm_message = format_attachment_delete_confirm(filename, order)
            delete_url = reverse(
                "core:workorder_attachment_delete",
                args=[order.pk, attachment.pk],
            )
            current_target = request.get_full_path()
            if current_target:
                delete_url = f"{delete_url}?{urlencode({'next': current_target})}"

            attachment_items.append(
                {
                    "attachment": attachment,
                    "url": url,
                    "absolute_url": absolute_url,
                    "filename": filename,
                    "mime": mime,
                    "is_image": is_image,
                    "extension": extension,
                    "size_label": size_label,
                    "category": category,
                    "created_display": created.strftime("%Y-%m-%d %H:%M"),
                    "created_iso": created.isoformat(),
                    "preview_url": preview_url,
                    "preview_external": preview_external,
                    "delete_confirm": delete_confirm_message,
                    "delete_url": delete_url,
                }
            )

        if request.user.is_authenticated and order.building_id:
            can_manage = _user_has_building_capability(
                request.user,
                order.building,
                Capability.MANAGE_BUILDINGS,
                Capability.CREATE_WORK_ORDERS,
            )

        attachments_api_url = reverse("core:api_workorder_attachments", args=[order.pk])
    else:
        upload_disabled_reason = _("Save this work order before adding attachments.")
        if request.user.is_authenticated:
            can_manage = True

    attachment_i18n = {
        "zoom_in": _("Zoom in"),
        "zoom_out": _("Zoom out"),
        "close": _("Close"),
        "reset": _("Reset zoom"),
        "open": _("Open original"),
        "loading": _("Loading..."),
        "tap_hint": _("Tap to zoom"),
        "download": _("Download"),
        "zoom": _("Zoom"),
        "empty": _("No attachments uploaded yet."),
        "uploaded_at": _("Uploaded %(date)s"),
        "delete": _("Delete"),
        "delete_confirm": _("Are you sure you want to delete this attachment?"),
        "delete_title": _("Delete attachment"),
        "delete_note": _("This action cannot be undone."),
        "delete_confirm_button": _("Yes, delete"),
        "cancel": _("Cancel"),
        "upload_button": _("Upload files"),
        "upload_hint": _("Select one or more files to upload without leaving this page."),
        "uploading": _("Uploading…"),
        "uploaded": _("Uploaded"),
        "failed": _("Upload failed"),
        "preview": _("Preview"),
        "doc_loading": _("Loading preview…"),
    }

    has_order = bool(order and getattr(order, "pk", None))
    show_upload_controls = (has_order and can_manage) or not has_order
    upload_enabled = bool(attachments_api_url)
    draft_mode = not has_order

    return {
        "attachment_items": attachment_items,
        "attachment_i18n": attachment_i18n,
        "can_manage_attachments": can_manage and has_order,
        "attachments_api_url": attachments_api_url,
        "attachments_show_upload": show_upload_controls,
        "attachments_upload_enabled": upload_enabled,
        "attachments_upload_disabled_reason": upload_disabled_reason,
        "attachment_panel_title": _("Attachments"),
        "attachments_draft_mode": draft_mode,
        "new_attachments_field": None,
        "remove_attachments_field": None,
    }


def _render_attachment_panel(request, *, order=None, form=None) -> dict[str, object]:
    context = _build_attachment_panel_context(request, order)

    if form is not None:
        try:
            context["new_attachments_field"] = form["new_attachments"]
        except KeyError:
            context["new_attachments_field"] = None
        try:
            context["remove_attachments_field"] = form["remove_attachments"]
        except KeyError:
            context["remove_attachments_field"] = None

    html = render_to_string(
        "core/includes/attachments_panel.html",
        context,
        request=request,
    )
    context["attachment_panel_html"] = html
    return context


def _log_attachment_activity(*, actor, work_order, changes: dict[str, list[str]] | None):
    if not changes:
        return
    added = [name for name in (changes.get("added") or []) if name]
    removed = [name for name in (changes.get("removed") or []) if name]
    if not added and not removed:
        return
    log_workorder_action(
        actor=actor,
        work_order=work_order,
        action=WorkOrderAuditLog.Action.ATTACHMENTS,
        payload={"attachments": {"added": added, "removed": removed}},
    )


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
    paginate_by = 25

    _per_choices = (25, 50, 100, 200)

    def get_queryset(self):
        extra = {"user_id": getattr(getattr(self.request, "user", None), "pk", None)}
        with log_duration(logger, "work_orders.list_queryset", extra=extra):
            request = self.request
            user = request.user
            resolver = CapabilityResolver(user) if user.is_authenticated else None
            self._can_view_all = resolver.has(Capability.VIEW_ALL_BUILDINGS) if resolver else False
            self._can_filter_owner = self._can_view_all or _user_can_filter_owner(user)

            # Page size (validated later in get_paginate_by)
            try:
                per = int(request.GET.get("per", self.paginate_by))
            except (TypeError, ValueError):
                per = self.paginate_by
            self._per = per

            search = (request.GET.get("q") or "").strip()
            status = (request.GET.get("status") or "").strip().upper()
            priority = (request.GET.get("priority") or "").strip().upper()
            deadline_from_raw = (request.GET.get("deadline_from") or "").strip()
            deadline_to_raw = (request.GET.get("deadline_to") or "").strip()
            deadline_range_raw = (request.GET.get("deadline_range") or "").strip()
            deadline_from = parse_date(deadline_from_raw) if deadline_from_raw else None
            deadline_to = parse_date(deadline_to_raw) if deadline_to_raw else None

            if not (deadline_from or deadline_to) and deadline_range_raw:
                normalized = deadline_range_raw.replace(" to ", "/").replace("–", "/").replace("—", "/")
                parts = [part.strip() for part in normalized.split("/") if part.strip()]
                if parts:
                    deadline_from = parse_date(parts[0])
                    if len(parts) > 1:
                        deadline_to = parse_date(parts[1])
                if deadline_from and not deadline_from_raw:
                    deadline_from_raw = deadline_from.isoformat()
                if deadline_to and not deadline_to_raw:
                    deadline_to_raw = deadline_to.isoformat()

            valid_status = {choice[0] for choice in WorkOrder.Status.choices}
            valid_priority = {choice[0] for choice in WorkOrder.Priority.choices}
            if status and status not in valid_status:
                status = ""
            if priority and priority not in valid_priority:
                priority = ""

            # Use visibility helper + pull related objects
            base_qs = (
                WorkOrder.objects.visible_to(user)
                .filter(archived_at__isnull=True)
            )
            qs = base_qs.select_related("building__owner", "unit", "forwarded_to_building", "forwarded_by")
            if _is_technician_only_user(user):
                qs = qs.exclude(
                    building__is_system_default=True,
                    forwarded_to_building__isnull=False,
                    status__in=[WorkOrder.Status.DONE, WorkOrder.Status.APPROVED],
                )

            # Build owner choices for staff (before additional filters)
            self._owner_choices: list[dict[str, str]] = []
            if self._can_filter_owner:
                owner_ids = [
                    oid
                    for oid in base_qs.annotate(
                        owner_choice_id=Coalesce("forwarded_to_building__owner_id", "building__owner_id")
                    ).values_list("owner_choice_id", flat=True).distinct()
                    if oid
                ]
                self._owner_choices = _cached_owner_choices(tuple(sorted(owner_ids)))
            else:
                self._owner_choices = []

            # Priority ordering: High > Medium > Low, then by deadline asc, then newest
            qs = qs.annotate(
                priority_order=Case(
                    When(priority__iexact="high", then=0),
                    When(priority__iexact="medium", then=1),
                    When(priority__iexact="low", then=2),
                    default=3,
                    output_field=IntegerField(),
                ),
                effective_owner_id=Case(
                    When(forwarded_to_building__owner_id__isnull=False, then=F("forwarded_to_building__owner_id")),
                    default=F("building__owner_id"),
                    output_field=IntegerField(),
                ),
            )

            if search:
                qs = qs.filter(
                    Q(title__icontains=search) | Q(description__icontains=search)
                )

            if status:
                qs = qs.filter(status=status)
            if priority:
                qs = qs.filter(priority=priority)
            if deadline_from:
                qs = qs.filter(deadline__gte=deadline_from)
            if deadline_to:
                qs = qs.filter(deadline__lte=deadline_to)
            if (deadline_from is None) and (deadline_to is None):
                deadline_range_raw = ""

            owner_param = (request.GET.get("owner") or "").strip()
            owner_filter = None
            if owner_param:
                try:
                    owner_filter = int(owner_param)
                except (TypeError, ValueError):
                    owner_param = ""
                    owner_filter = None

            if owner_filter:
                if self._can_filter_owner:
                    qs = qs.filter(effective_owner_id=owner_filter)
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
                "owner_desc": ("-building__owner__username", "priority_order", "-pk"),
            }
            if sort_param not in sort_map:
                sort_param = "priority"

            if sort_param in {"owner", "owner_desc"} and not self._can_filter_owner:
                sort_param = "priority"

            qs = qs.order_by(*sort_map[sort_param])

            self._search = search
            self._status = status
            self._priority = priority
            self._owner = owner_param
            self._sort = sort_param
            self._deadline_range = deadline_range_raw
            self._deadline_from = deadline_from_raw
            self._deadline_to = deadline_to_raw

            return qs

    def get_paginate_by(self, queryset):
        per = getattr(self, "_per", self.paginate_by)
        if per not in self._per_choices:
            per = self.paginate_by
        return per

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["orders"] = attach_expense_totals_by_metadata(
            ctx.get("orders"),
            metadata_key="work_order_id",
            metadata_keys=("work_order_id", "work_order"),
            include_work_order_text_fallback=True,
            target_attr="expense_total",
        )
        paginator = ctx.get("paginator")
        total_orders = 0
        if paginator is not None:
            total_orders = paginator.count
        else:
            object_list = getattr(self, "object_list", None)
            total_orders = len(object_list) if object_list is not None else 0
        ctx.update(
            {
                "q": getattr(self, "_search", ""),
                "status": getattr(self, "_status", ""),
                "priority": getattr(self, "_priority", ""),
                "deadline_range": getattr(self, "_deadline_range", ""),
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
                    ("owner", _("Отговорник (A → Z)")),
                    ("owner_desc", _("Отговорник (Z → A)")),
                ],
                "show_owner_info": getattr(self, "_can_filter_owner", False),
                "pagination_query": _querystring_without(self.request, "page"),
                "total_orders": total_orders,
            }
        )
        if not getattr(self, "_can_filter_owner", False):
            ctx["sort_choices"] = [
                choice for choice in ctx["sort_choices"]
                if not choice[0].startswith("owner")
            ]
        return ctx

class WorkOrderDetailView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DetailView):
    model = WorkOrder
    template_name = "core/work_order_detail.html"
    context_object_name = "order"

    def get_queryset(self):
        base = WorkOrder.objects.visible_to(self.request.user)
        return (
            base.select_related("building__owner", "unit", "forwarded_to_building", "forwarded_by")
            .prefetch_related("attachments", "audit_entries__actor")
        )

    def _manageable_building_for_user(self, order: WorkOrder):
        if _technician_readonly_for_forwarded_office_order(self.request.user, order):
            return None
        if _user_has_building_capability(
            self.request.user,
            order.building,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_WORK_ORDERS,
        ):
            return order.building
        destination = getattr(order, "forwarded_to_building", None)
        if destination and _user_has_building_capability(
            self.request.user,
            destination,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_WORK_ORDERS,
        ):
            return destination
        return None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        next_url = _safe_next_url(self.request)
        ctx["next_url"] = next_url
        if next_url:
            ctx["cancel_url"] = next_url
        ctx.update(_render_attachment_panel(self.request, order=self.object))
        manageable_building = self._manageable_building_for_user(self.object)
        ctx["can_edit_order"] = manageable_building is not None
        ctx["can_reroute_order"] = ctx["can_edit_order"] and not user_has_role(
            self.request.user,
            MembershipRole.TECHNICIAN,
        )
        ctx["can_add_budget"] = ctx["can_edit_order"] and BudgetFeatureFlag.is_enabled_for(
            self.request.user,
            building_id=getattr(manageable_building, "pk", self.object.building_id),
        )
        ctx["can_delete_order"] = (
            not _technician_readonly_for_forwarded_office_order(self.request.user, self.object)
            and _user_has_building_capability(
                self.request.user,
                self.object.building,
                Capability.MANAGE_BUILDINGS,
            )
        )
        ctx["replacement_request_note"] = self.object.replacement_request_note
        ctx["awaiting_requested_by"] = self.object.awaiting_approval_by
        audit_entries = list(
            self.object.audit_entries.select_related("actor").order_by("created_at", "id")
        )
        status_labels = dict(WorkOrder.Status.choices)
        history_entries: list[dict[str, object]] = []
        participant_labels: list[str] = []
        seen_participants: set[object] = set()
        approval_actor = None
        for entry in audit_entries:
            actor = entry.actor
            if actor:
                actor_label = actor.get_full_name() or actor.username
                participant_key = actor.pk
            else:
                actor_label = _("System")
                participant_key = "system"
            if participant_key not in seen_participants:
                participant_labels.append(actor_label)
                seen_participants.add(participant_key)
            payload = entry.payload or {}
            action = entry.action
            from_code = payload.get("from")
            to_code = payload.get("to")
            from_label = status_labels.get(from_code, from_code)
            to_label = status_labels.get(to_code, to_code)
            description = entry.get_action_display()
            changes_payload = payload.get("fields") or {}
            formatted_changes = self._format_change_payload(changes_payload)
            attachments_payload = payload.get("attachments") or {}
            if action == WorkOrderAuditLog.Action.CREATED:
                description = _("Work order created")
            elif action == WorkOrderAuditLog.Action.UPDATED:
                field_labels = self._format_audit_field_labels(changes_payload.keys())
                field_list = ", ".join(field_labels)
                if field_list:
                    description = _("Updated: %(fields)s") % {"fields": field_list}
            elif action == WorkOrderAuditLog.Action.ARCHIVED:
                description = _("Archived")
            if action in {WorkOrderAuditLog.Action.STATUS_CHANGED, WorkOrderAuditLog.Action.APPROVAL} and (
                from_label or to_label
            ):
                if from_label and to_label:
                    description = _("Status changed: %(from)s → %(to)s") % {
                        "from": from_label,
                        "to": to_label,
                    }
                elif to_label:
                    description = _("Status set to %(to)s") % {"to": to_label}
            history_entries.append(
                {
                    "timestamp": timezone.localtime(entry.created_at),
                    "actor": actor_label,
                    "description": description,
                    "from": from_label,
                    "to": to_label,
                    "action": action,
                    "is_approval": action == WorkOrderAuditLog.Action.APPROVAL,
                    "changes": formatted_changes,
                    "attachments": attachments_payload,
                }
            )
            if action == WorkOrderAuditLog.Action.APPROVAL:
                approval_actor = actor_label
        if not history_entries:
            history_entries.append(
                {
                    "timestamp": timezone.localtime(self.object.created_at),
                    "actor": _("System"),
                    "description": _("Work order created"),
                    "from": "",
                    "to": "",
                    "action": WorkOrderAuditLog.Action.CREATED,
                    "is_approval": False,
                    "changes": [],
                    "attachments": {},
                }
            )
            participant_labels.append(_("System"))
            seen_participants.add("system")
        ctx["history_entries"] = history_entries
        ctx["history_participants"] = participant_labels
        ctx["history_approval_actor"] = approval_actor
        if ctx.get("can_add_budget"):
            charge_url = reverse("core:work_order_budget_charge", args=[self.object.pk])
            if next_url:
                charge_url = f"{charge_url}?{urlencode({'next': next_url})}"
            ctx["add_budget_url"] = charge_url

        deadline_status = None
        if self.object.deadline and self.object.archived_at:
            completion_date = timezone.localtime(self.object.archived_at).date()
            delta_days = (completion_date - self.object.deadline).days
            if delta_days > 0:
                label = ngettext(
                    "Missed deadline by %(days)s day.",
                    "Missed deadline by %(days)s days.",
                    delta_days,
                ) % {"days": delta_days}
                state = "missed"
            else:
                label = _("Completed on time on %(date)s") % {
                    "date": formats.date_format(completion_date, "DATE_FORMAT")
                }
                state = "met"
            deadline_status = {
                "state": state,
                "label": label,
                "completion_date": completion_date,
                "days_delta": delta_days,
            }
        ctx["deadline_status"] = deadline_status
        return ctx

    def _format_audit_field_labels(self, field_names):
        labels = []
        for field in sorted(field_names):
            labels.append(self._get_audit_field_label(field))
        return labels

    def _format_change_payload(self, changes_payload: dict[str, dict[str, str]]):
        formatted = []
        priority_labels = dict(WorkOrder.Priority.choices)
        status_labels = dict(WorkOrder.Status.choices)

        def _translate_value(field: str, value: str | None):
            if not value:
                return value
            if field == "priority":
                return priority_labels.get(value, value)
            if field == "status":
                return status_labels.get(value, value)
            return value

        for field in sorted(changes_payload.keys()):
            label = self._get_audit_field_label(field)
            change = changes_payload.get(field) or {}
            formatted.append(
                {
                    "label": label,
                    "from": _translate_value(field, change.get("from")),
                    "to": _translate_value(field, change.get("to")),
                }
            )
        return formatted

    @staticmethod
    def _get_audit_field_label(field_name: str) -> str:
        if not field_name:
            return ""
        try:
            field = WorkOrder._meta.get_field(field_name)
            return str(field.verbose_name)
        except FieldDoesNotExist:
            readable = field_name.replace("_", " ").strip()
            return readable.capitalize() if readable else field_name

    def test_func(self):
        return WorkOrder.objects.visible_to(self.request.user).filter(pk=self.kwargs.get("pk")).exists()


class WorkOrderCreateView(LoginRequiredMixin, UnitsWidgetMixin, CreateView):
    model = WorkOrder
    form_class = WorkOrderForm
    template_name = "core/work_order_form.html"

    def _is_lawyer_only_request(self) -> bool:
        value = (self.request.GET.get("lawyer_only") or "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["force_lawyer_only"] = self._is_lawyer_only_request()

        # If opening from a building context (?building=<id>), lock to it
        self.building = None
        building_id = self.request.GET.get("building")
        if building_id:
            try:
                self.building = Building.objects.visible_to(self.request.user).get(pk=int(building_id))
                if not self._user_can_modify(self.building):
                    raise Http404()
                kwargs["building"] = self.building
            except (ValueError, Building.DoesNotExist):
                pass
        return kwargs

    def _user_can_modify(self, building):
        return _user_has_building_capability(
            self.request.user,
            building,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_WORK_ORDERS,
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["building"] = getattr(self, "building", None)
        safe_next = _safe_next_url(self.request)
        ctx["next_url"] = safe_next
        if safe_next:
            ctx["cancel_url"] = safe_next
        else:
            ctx["cancel_url"] = (
                reverse("core:building_detail", args=[self.building.pk])
                if getattr(self, "building", None)
                else reverse("core:work_orders_list")
            )
        ctx["units_api_template"] = reverse("core:api_units", args=[0]).replace("/0/", "/{id}/")
        self._prepare_units_widget(ctx)
        form = ctx.get("form")
        if form is not None:
            ctx.update(_render_attachment_panel(self.request, order=form.instance, form=form))
        else:
            ctx.update(_render_attachment_panel(self.request, order=None))
        return ctx

    def form_valid(self, form):
        prospective = form.save(commit=False)
        if not _user_can_access_building(self.request.user, prospective.building):
            raise Http404()
        if not self._user_can_modify(prospective.building):
            raise Http404()
        with transaction.atomic():
            response = super().form_valid(form)
            attachment_changes = form.save_attachments(self.object)
            budget_expense = form.apply_budget_charge(work_order=self.object, actor=self.request.user)
        self._log_creation()
        _maybe_handle_forwarding_change(self.request, form, self.object)
        actor = self.request.user if self.request.user.is_authenticated else None
        _log_attachment_activity(
            actor=actor,
            work_order=self.object,
            changes=attachment_changes,
        )
        if budget_expense:
            amount_label = formats.number_format(budget_expense.amount, 2)
            messages.info(
                self.request,
                _("Charged %(amount)s %(currency)s to %(title)s.")
                % {
                    "amount": amount_label,
                    "currency": budget_expense.budget_request.currency,
                    "title": budget_expense.budget_request.title or _("Budget #%(id)s") % {"id": budget_expense.budget_request_id},
                },
            )
        messages.success(self.request, _("Work order created."))
        return response

    def get_success_url(self):
        next_url = _safe_next_url(self.request)
        if next_url:
            return next_url
        return f"{reverse('core:building_detail', args=[self.object.building_id])}?tab=work_orders"

    def _log_creation(self):
        if not getattr(self, "object", None):
            return
        actor = self.request.user if self.request.user.is_authenticated else None
        log_workorder_action(
            actor=actor,
            work_order=self.object,
            action=WorkOrderAuditLog.Action.CREATED,
            payload={
                "status": self.object.status,
                "priority": self.object.priority,
            },
        )


class WorkOrderUpdateView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, UnitsWidgetMixin, UpdateView):
    model = WorkOrder
    form_class = WorkOrderForm
    template_name = "core/work_order_form.html"

    def get_queryset(self):
        base = WorkOrder.objects.visible_to(self.request.user)
        return base.select_related("building", "unit")

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
            ctx["cancel_url"] = reverse("core:building_detail", args=[building.pk])
        ctx["units_api_template"] = reverse("core:api_units", args=[0]).replace("/0/", "/{id}/")
        self._prepare_units_widget(ctx)
        order_obj = ctx.get("object", getattr(self, "object", None))
        form = ctx.get("form")
        if form is not None:
            ctx.update(_render_attachment_panel(self.request, order=order_obj, form=form))
        else:
            ctx.update(_render_attachment_panel(self.request, order=order_obj))
        return ctx

    def _can_manage_order(self, order: WorkOrder) -> bool:
        if _technician_readonly_for_forwarded_office_order(self.request.user, order):
            return False
        if _user_has_building_capability(
            self.request.user,
            order.building,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_WORK_ORDERS,
        ):
            return True
        destination = getattr(order, "forwarded_to_building", None)
        if destination is None:
            return False
        return _user_has_building_capability(
            self.request.user,
            destination,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_WORK_ORDERS,
        )

    def test_func(self):
        wo = self.get_object()
        return self._can_manage_order(wo)

    def form_valid(self, form):
        previous_obj = None
        previous_status = WorkOrder.Status.OPEN
        if form.instance.pk:
            try:
                previous_obj = WorkOrder.objects.select_related("unit", "building").get(pk=form.instance.pk)
                previous_status = previous_obj.status
            except WorkOrder.DoesNotExist:
                previous_obj = None
        obj = form.save(commit=False)
        if not WorkOrder.objects.visible_to(self.request.user).filter(pk=obj.pk).exists():
            raise Http404()
        if not self._can_manage_order(obj):
            raise Http404()
        if obj.archived_at and obj.status not in {WorkOrder.Status.DONE, WorkOrder.Status.APPROVED}:
            obj.archived_at = None
        with transaction.atomic():
            obj.save()
            attachment_changes = form.save_attachments(obj)
            budget_expense = form.apply_budget_charge(work_order=obj, actor=self.request.user)
        self.object = obj
        _maybe_handle_forwarding_change(self.request, form, obj)
        self._after_status_change(previous_status)
        if previous_obj:
            self._log_general_updates(previous_obj, obj, form.changed_data or [])
        actor = self.request.user if self.request.user.is_authenticated else None
        _log_attachment_activity(actor=actor, work_order=obj, changes=attachment_changes)
        if budget_expense:
            amount_label = formats.number_format(budget_expense.amount, 2)
            messages.info(
                self.request,
                _("Charged %(amount)s %(currency)s to %(title)s.")
                % {
                    "amount": amount_label,
                    "currency": budget_expense.budget_request.currency,
                    "title": budget_expense.budget_request.title or _("Budget #%(id)s") % {"id": budget_expense.budget_request_id},
                },
            )
        messages.warning(self.request, _("Work order updated."))
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        next_url = _safe_next_url(self.request)
        if next_url:
            return next_url
        return reverse("core:building_detail", args=[self.object.building_id])

    def _after_status_change(self, previous_status):
        obj = getattr(self, "object", None)
        if not obj:
            return
        actor = self.request.user if self.request.user.is_authenticated else None
        if previous_status != obj.status:
            action = WorkOrderAuditLog.Action.STATUS_CHANGED
            if (
                previous_status == WorkOrder.Status.AWAITING_APPROVAL
                and obj.status in {WorkOrder.Status.DONE, WorkOrder.Status.APPROVED}
            ):
                action = WorkOrderAuditLog.Action.APPROVAL
            elif previous_status != WorkOrder.Status.AWAITING_APPROVAL and obj.status == WorkOrder.Status.AWAITING_APPROVAL:
                action = WorkOrderAuditLog.Action.STATUS_CHANGED
            log_workorder_action(
                actor=actor,
                work_order=obj,
                action=action,
                payload={"from": previous_status, "to": obj.status},
            )
        if (
            previous_status != WorkOrder.Status.AWAITING_APPROVAL
            and obj.status == WorkOrder.Status.AWAITING_APPROVAL
        ):
            notify_approvers_of_pending_order(obj, exclude_user_id=getattr(self.request.user, "id", None))

    def _log_general_updates(self, previous_obj, updated_obj, changed_fields):
        if not changed_fields:
            return
        tracked_changes = {}
        ignored_fields = {
            "status",
            "new_attachments",
            "remove_attachments",
        }
        for field in changed_fields:
            if field in ignored_fields:
                continue
            old_value = getattr(previous_obj, field, None)
            new_value = getattr(updated_obj, field, None)
            if field == "unit":
                old_value = getattr(previous_obj.unit, "number", None) if previous_obj else None
                new_value = getattr(updated_obj.unit, "number", None)
                if old_value is not None:
                    old_value = f"#{old_value}"
                if new_value is not None:
                    new_value = f"#{new_value}"
            elif field == "building":
                old_value = getattr(previous_obj.building, "name", None) if previous_obj else None
                new_value = getattr(updated_obj.building, "name", None)
            tracked_changes[field] = {
                "from": self._serialize_change_value(old_value),
                "to": self._serialize_change_value(new_value),
            }
        if not tracked_changes:
            return
        actor = self.request.user if self.request.user.is_authenticated else None
        log_workorder_action(
            actor=actor,
            work_order=updated_obj,
            action=WorkOrderAuditLog.Action.UPDATED,
            payload={"fields": tracked_changes},
        )

    @staticmethod
    def _serialize_change_value(value):
        if value is None:
            return ""
        if isinstance(value, datetime):
            return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")
        if isinstance(value, date):
            return value.isoformat()
        return str(value)


class WorkOrderDeleteView(LoginRequiredMixin, UserPassesTestMixin, CachedObjectMixin, DeleteView):
    model = WorkOrder
    template_name = "core/work_order_confirm_delete.html"   # <- new specific template

    def get_queryset(self):
        base = WorkOrder.objects.visible_to(self.request.user)
        return base.select_related("building")

    def test_func(self):
        wo = self.get_object()
        if _technician_readonly_for_forwarded_office_order(self.request.user, wo):
            return False
        return _user_has_building_capability(
            self.request.user,
            wo.building,
            Capability.MANAGE_BUILDINGS,
        )

    def get_success_url(self):
        next_url = _safe_next_url(self.request)
        if next_url:
            return next_url
        return reverse_lazy("core:building_detail", args=[self.object.building_id])

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
            ctx.setdefault("cancel_url", reverse("core:building_detail", args=[obj.building_id]))
        return ctx


class WorkOrderApprovalDecisionView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        order = get_object_or_404(
            WorkOrder.objects.visible_to(request.user).select_related("building"),
            pk=kwargs["pk"],
        )
        if not user_can_approve_work_orders(request.user, order.building_id):
            raise Http404()
        if order.status != WorkOrder.Status.AWAITING_APPROVAL:
            messages.error(request, _("This work order is no longer awaiting approval."))
            return redirect(self._next_url(request))

        decision = request.POST.get("decision")
        if decision not in {"approve", "reject"}:
            messages.error(request, _("Invalid approval choice."))
            return redirect(self._next_url(request))

        target_status = (
            WorkOrder.Status.APPROVED if decision == "approve" else WorkOrder.Status.REJECTED
        )
        previous_status = order.status
        audit_action = (
            WorkOrderAuditLog.Action.APPROVAL
            if target_status == WorkOrder.Status.APPROVED
            else WorkOrderAuditLog.Action.STATUS_CHANGED
        )
        success_message = (
            _("Work order approved.") if target_status == WorkOrder.Status.APPROVED else _("Work order rejected.")
        )

        with transaction.atomic():
            order.status = target_status
            order.awaiting_approval_by = None
            order.save(update_fields=["status", "awaiting_approval_by", "updated_at"])
            log_workorder_action(
                actor=request.user if request.user.is_authenticated else None,
                work_order=order,
                action=audit_action,
                payload={"from": previous_status, "to": target_status},
            )

        messages.success(request, success_message)
        return redirect(self._next_url(request))

    @staticmethod
    def _next_url(request):
        return _safe_next_url(request) or reverse("core:dashboard")

class WorkOrderArchiveView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Archive a work order by setting archived_at (via WorkOrder.archive()).
    - Users with Capability.APPROVE_WORK_ORDERS or Capability.MANAGE_BUILDINGS
      (e.g., building owners/managers) can archive.
    - Only allowed when the work order status is DONE or APPROVED.
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
        if _technician_readonly_for_forwarded_office_order(self.request.user, wo):
            return False
        if _user_has_building_capability(
            self.request.user,
            wo.building,
            Capability.APPROVE_WORK_ORDERS,
            Capability.MANAGE_BUILDINGS,
        ):
            return True
        destination = getattr(wo, "forwarded_to_building", None)
        if destination and _user_has_building_capability(
            self.request.user,
            destination,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_WORK_ORDERS,
        ):
            return True
        if wo.lawyer_only and user_is_lawyer(self.request.user, building_id=wo.building_id):
            return True
        return False

    def post(self, request, *args, **kwargs):
        wo = self.get_object()

        if wo.status not in {WorkOrder.Status.DONE, WorkOrder.Status.APPROVED}:
            raise Http404(_("Only completed work orders can be archived."))

        archive_destination = getattr(wo, "forwarded_to_building", None)
        if not wo.is_archived:
            # Forwarded Office orders should live under their destination building
            # once archived so they appear in the destination archive context.
            if getattr(wo.building, "is_system_default", False) and archive_destination is not None:
                wo.building_id = archive_destination.pk
                wo.forwarded_to_building = None
                wo.forwarded_by = None
                wo.forward_note = ""
                wo.save(
                    update_fields=[
                        "building",
                        "forwarded_to_building",
                        "forwarded_by",
                        "forward_note",
                        "updated_at",
                    ],
                    validate=False,
                )
            wo.archive()
            messages.success(request, _("Work order archived."))
            actor = request.user if request.user.is_authenticated else None
            log_workorder_action(
                actor=actor,
                work_order=wo,
                action=WorkOrderAuditLog.Action.ARCHIVED,
                payload={
                    "status": wo.status,
                    "archived_under_building_id": wo.building_id,
                },
            )

        next_url = _safe_next_url(request)
        if next_url:
            return redirect(next_url)
        requested_building_id = request.GET.get("building")
        if requested_building_id:
            try:
                requested_building_id = int(requested_building_id)
            except (TypeError, ValueError):
                requested_building_id = None
            if requested_building_id is not None:
                target_building = Building.objects.filter(pk=requested_building_id).first()
                if target_building and _user_can_access_building(request.user, target_building):
                    return redirect("core:building_detail", requested_building_id)

        destination = getattr(wo, "forwarded_to_building", None)
        if destination and _user_can_access_building(request.user, destination):
            return redirect("core:building_detail", destination.pk)
        return redirect("core:building_detail", wo.building_id)


class MassAssignWorkOrdersView(CapabilityRequiredMixin, LoginRequiredMixin, FormView):
    template_name = "core/work_orders_mass_assign.html"
    form_class = MassAssignWorkOrdersForm
    success_url = reverse_lazy("core:work_orders_mass_assign")
    required_capabilities = (Capability.MASS_ASSIGN,)

    def get_queryset(self):
        if hasattr(self, "_building_queryset"):
            return self._building_queryset
        qs = (
            Building.objects.filter(role=Building.Role.TECH_SUPPORT)
            .select_related("owner")
            .order_by("name", "id")
        )
        resolver = CapabilityResolver(self.request.user)
        visible_ids = resolver.visible_building_ids()
        if visible_ids is not None:
            qs = qs.filter(pk__in=visible_ids or [])

        if resolver.has(Capability.MANAGE_BUILDINGS):
            self._building_queryset = qs
            return self._building_queryset

        manageable_ids = []
        for pk in qs.values_list("pk", flat=True):
            if resolver.has(Capability.MANAGE_BUILDINGS, building_id=pk):
                manageable_ids.append(pk)
        self._building_queryset = qs.filter(pk__in=manageable_ids)
        return self._building_queryset

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["buildings_queryset"] = self.get_queryset()
        kwargs["user"] = self.request.user
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
        deadline = form.cleaned_data["deadline"]
        priority = form.cleaned_data["priority"]

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
                        status__in=[
                            WorkOrder.Status.OPEN,
                            WorkOrder.Status.IN_PROGRESS,
                            WorkOrder.Status.AWAITING_APPROVAL,
                        ],
                        archived_at__isnull=True,
                    )
                    .order_by("-created_at")
                    .exists()
                )
                if exists:
                    skipped += 1
                    continue

                order = WorkOrder.objects.create(
                    building=building,
                    title=title,
                    description=description,
                    status=WorkOrder.Status.OPEN,
                    priority=priority,
                    deadline=deadline,
                    mass_assigned=True,
                    created_by=self.request.user if self.request.user.is_authenticated else None,
                )
                created += 1
                created_names.append(building.name)
                notify_building_technicians_of_mass_assignment(order)

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

    SUMMARY_PER_CHOICES = (25, 50, 100, 200)
    SUMMARY_PER_DEFAULT = 25
    DETAIL_PER_CHOICES = (25, 50, 100, 200)
    DETAIL_PER_DEFAULT = 25

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
            .select_related("building__owner", "unit", "forwarded_to_building", "forwarded_by")
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

        archived_from_raw = (request.GET.get("archived_from") or "").strip()
        archived_to_raw = (request.GET.get("archived_to") or "").strip()
        archived_from = parse_date(archived_from_raw) if archived_from_raw else None
        archived_to = parse_date(archived_to_raw) if archived_to_raw else None
        archived_range_raw = (request.GET.get("archived_range") or "").strip()
        if not (archived_from or archived_to) and archived_range_raw:
            normalized = archived_range_raw.replace(" to ", "/").replace("–", "/").replace("—", "/")
            parts = [part.strip() for part in normalized.split("/") if part.strip()]
            if parts:
                archived_from = parse_date(parts[0])
                if len(parts) > 1:
                    archived_to = parse_date(parts[1])
        if archived_from and archived_to and archived_to < archived_from:
            archived_from, archived_to = archived_to, archived_from
        if archived_from:
            qs = qs.filter(archived_at__date__gte=archived_from)
        if archived_to:
            qs = qs.filter(archived_at__date__lte=archived_to)
        use_range_text = archived_range_raw if archived_range_raw and not (archived_from_raw or archived_to_raw) else ""
        self._archived_range = use_range_text
        self._archived_from_raw = archived_from_raw
        self._archived_to_raw = archived_to_raw
        self._has_archived_filter = bool(archived_from or archived_to or archived_range_raw)

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
            forwarded_total=Count(
                "id",
                filter=Q(forwarded_to_building__isnull=False),
            ),
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
        ctx["archived_range"] = getattr(self, "_archived_range", "")
        ctx["archived_from"] = getattr(self, "_archived_from_raw", "")
        ctx["archived_to"] = getattr(self, "_archived_to_raw", "")
        ctx["has_archived_filter"] = getattr(self, "_has_archived_filter", False)
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
                        "forwarded_total": item.get("forwarded_total") or 0,
                    }
                )
            summary_page.object_list = processed
        ctx["building_summary_page"] = summary_page
        ctx["building_summary_total"] = summary_paginator.count if summary_paginator else 0
        ctx["building_summary_query"] = _querystring_without(self.request, "b_page")
        ctx["building_summary_per"] = summary_per
        ctx["building_summary_per_choices"] = self.SUMMARY_PER_CHOICES
        ctx["archive_work_orders_url"] = reverse("core:work_orders_archive")
        ctx["archive_budgets_url"] = ""
        user = self.request.user
        resolver = CapabilityResolver(user) if user.is_authenticated else None
        if (
            resolver
            and resolver.has(Capability.APPROVE_BUDGETS)
            and BudgetFeatureFlag.is_enabled_for(user)
        ):
            try:
                ctx["archive_budgets_url"] = reverse("core:budget_archived_list")
            except Exception:
                ctx["archive_budgets_url"] = ""
        can_purge = user_is_admin_or_backoffice(user)
        ctx["can_purge_archives"] = can_purge
        if can_purge:
            ctx["archive_purge_form"] = ArchivePurgeForm()
            ctx["archive_purge_action"] = reverse("core:work_orders_archive_purge")
            ctx["archive_purge_preview_url"] = reverse("core:work_orders_archive_purge_preview")
            ctx["archive_building_delete_action"] = reverse("core:work_orders_archive_building_delete")
        return ctx


class ArchivedWorkOrderListView(
    CapabilityRequiredMixin,
    LoginRequiredMixin,
    ArchivedWorkOrderFilterMixin,
    ListView,
):
    """
    Staff-only list of archived work orders grouped by building summary.
    """

    template_name = "core/work_orders_archive_list.html"
    paginate_by = None

    required_capabilities = (Capability.VIEW_ALL_BUILDINGS,)

    def get_queryset(self):
        # Pre-compute filters but avoid fetching the full order list.
        self.get_filtered_queryset()
        return WorkOrder.objects.none()


class ArchivedWorkOrderDetailView(
    CapabilityRequiredMixin,
    LoginRequiredMixin,
    ArchivedWorkOrderFilterMixin,
    ListView,
):
    """
    Archived work orders for a single building.
    """

    template_name = "core/work_orders_archive_detail.html"
    paginate_by = None
    required_capabilities = (Capability.VIEW_ALL_BUILDINGS,)

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
        first_order = qs.first()
        if first_order is None:
            raise Http404()
        self._current_building = first_order.building
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
        ctx["back_url"] = reverse("core:work_orders_archive")
        ctx["back_query"] = ctx["detail_page_query"]
        ctx["detail_per"] = getattr(self, "_detail_per", self.DETAIL_PER_DEFAULT)
        ctx["detail_per_choices"] = self.DETAIL_PER_CHOICES
        return ctx


class ArchivedWorkOrderPurgeView(LoginRequiredMixin, View):
    """
    Permanently delete archived work orders within a date range.
    """

    form_class = ArchivePurgeForm

    def post(self, request, *args, **kwargs):
        if not user_is_admin_or_backoffice(request.user):
            raise Http404()
        form = self.form_class(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect("core:work_orders_archive")
        if not form.cleaned_data.get("confirm"):
            messages.error(request, _("Please confirm the permanent deletion."))
            return redirect("core:work_orders_archive")

        start = form.cleaned_data["from_date"]
        end = form.cleaned_data["to_date"]
        qs = WorkOrder.objects.filter(archived_at__isnull=False)
        if start:
            qs = qs.filter(archived_at__date__gte=start)
        if end:
            qs = qs.filter(archived_at__date__lte=end)
        deleted = qs.count()
        if not deleted:
            messages.info(request, _("No archived work orders matched the selected range."))
            return redirect("core:work_orders_archive")
        qs.delete()
        if deleted:
            messages.success(
                request,
                ngettext(
                    "Deleted %(count)s archived work order permanently.",
                    "Deleted %(count)s archived work orders permanently.",
                    deleted,
                )
                % {"count": deleted},
            )
        else:
            messages.info(request, _("No archived work orders matched the selected range."))
        return redirect("core:work_orders_archive")


class ArchivedWorkOrderBuildingDeleteView(LoginRequiredMixin, View):
    """
    Permanently delete archived work orders for selected buildings.
    """

    def post(self, request, *args, **kwargs):
        if not user_is_admin_or_backoffice(request.user):
            raise Http404()
        building_ids_raw = request.POST.getlist("building_ids")
        building_ids: list[int] = []
        for value in building_ids_raw:
            try:
                building_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        building_ids = sorted(set(building_ids))
        if not building_ids:
            messages.error(request, _("Select at least one archived building."))
            return redirect("core:work_orders_archive")
        if not request.POST.get("confirm"):
            messages.error(request, _("Please confirm the permanent deletion."))
            return redirect("core:work_orders_archive")

        qs = WorkOrder.objects.filter(
            archived_at__isnull=False,
            building_id__in=building_ids,
        )
        deleted = qs.count()
        if not deleted:
            messages.info(request, _("No archived work orders matched the selected buildings."))
            return redirect("core:work_orders_archive")
        qs.delete()
        messages.success(
            request,
            ngettext(
                "Deleted %(count)s archived work order permanently.",
                "Deleted %(count)s archived work orders permanently.",
                deleted,
            )
            % {"count": deleted},
        )
        return redirect("core:work_orders_archive")


class ArchivedWorkOrderPurgePreviewView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        if not user_is_admin_or_backoffice(request.user):
            raise Http404()
        form = ArchivePurgeForm(request.GET)
        if not form.is_valid():
            return JsonResponse({"count": 0}, status=400)
        start = form.cleaned_data["from_date"]
        end = form.cleaned_data["to_date"]
        qs = WorkOrder.objects.filter(archived_at__isnull=False)
        if start:
            qs = qs.filter(archived_at__date__gte=start)
        if end:
            qs = qs.filter(archived_at__date__lte=end)
        return JsonResponse({"count": qs.count()})


class WorkOrderAttachmentDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = WorkOrderAttachment
    template_name = "core/attachment_confirm_delete.html"
    pk_url_kwarg = "attachment_pk"
    raise_exception = True

    def dispatch(self, request, *args, **kwargs):
        self.work_order = get_object_or_404(
            WorkOrder.objects.visible_to(request.user).select_related("building"),
            pk=kwargs.get("order_pk"),
        )
        self.next_url = _safe_next_url(request)
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset().select_related("work_order", "work_order__building")
        return qs.filter(work_order=self.work_order)

    def test_func(self):
        return _user_has_building_capability(
            self.request.user,
            self.work_order.building,
            Capability.CREATE_WORK_ORDERS,
            Capability.MANAGE_BUILDINGS,
        )

    def get_success_url(self):
        if getattr(self, "next_url", None):
            return self.next_url
        return reverse("core:work_order_detail", args=[self.work_order.pk])

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        name = self._attachment_display_name(self.object)
        success_url = self.get_success_url()
        self.object.delete()
        actor = request.user if request.user.is_authenticated else None
        _log_attachment_activity(
            actor=actor,
            work_order=self.work_order,
            changes={"added": [], "removed": [name]},
        )
        messages.error(
            request,
            _("Attachment \"%(name)s\" deleted.") % {"name": name},
        )
        return HttpResponseRedirect(success_url)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        attachment = ctx.get("object") or getattr(self, "object", None)
        display_name = self._attachment_display_name(attachment)
        ctx["work_order"] = self.work_order
        cancel_target = self.next_url or self.get_success_url()
        ctx["cancel_url"] = cancel_target
        ctx["next_url"] = self.next_url
        ctx["delete_message"] = format_attachment_delete_confirm(display_name, self.work_order)
        ctx["attachment_name"] = display_name
        if attachment is not None:
            meta = attachment._meta
            ctx.setdefault("object_verbose_name", meta.verbose_name)
            ctx.setdefault("object_model_name", meta.model_name)
        return ctx

    def _attachment_display_name(self, attachment):
        if not attachment:
            return ""
        name = (attachment.original_name or "").strip()
        if not name:
            file_attr = getattr(attachment.file, "name", "")
            if file_attr:
                name = Path(file_attr).name.strip()
        if not name:
            name = _("Attachment %(id)s") % {"id": attachment.pk}
        return name


class WorkOrderBudgetChargeView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "core/work_order_budget_charge.html"
    form_class = WorkOrderBudgetChargeForm

    def dispatch(self, request, *args, **kwargs):
        self.work_order = get_object_or_404(
            WorkOrder.objects.visible_to(request.user).select_related("building"),
            pk=kwargs["pk"],
        )
        if not BudgetFeatureFlag.is_enabled_for(request.user, building_id=self.work_order.building_id):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def test_func(self):
        if _technician_readonly_for_forwarded_office_order(self.request.user, self.work_order):
            return False
        if _user_has_building_capability(
            self.request.user,
            self.work_order.building,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_WORK_ORDERS,
        ):
            return True
        destination = getattr(self.work_order, "forwarded_to_building", None)
        if destination is None:
            return False
        return _user_has_building_capability(
            self.request.user,
            destination,
            Capability.MANAGE_BUILDINGS,
            Capability.CREATE_WORK_ORDERS,
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["work_order"] = self.work_order
        return kwargs

    def form_valid(self, form):
        form.save(actor=self.request.user)
        messages.success(self.request, _("Expense logged to the selected budget."))
        return redirect(self.get_success_url())

    def get_success_url(self):
        next_url = self.request.GET.get("next")
        if next_url:
            return next_url
        return reverse("core:work_order_detail", args=[self.work_order.pk])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["work_order"] = self.work_order
        ctx["building"] = self.work_order.building
        ctx["next_url"] = self.request.GET.get("next") or reverse(
            "core:work_order_detail", args=[self.work_order.pk]
        )
        return ctx
