from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import quote_plus

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import models, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from ..authz import Capability
from ..forms import BudgetExpenseForm, BudgetRequestApprovalForm, BudgetRequestForm
from ..models import (
    BudgetRequest,
    BudgetRequestEvent,
    Building,
    TodoActivity,
    TodoItem,
    Expense,
    ExpenseAttachment,
    ExpenseCategory,
    Unit,
    WorkOrder,
    WorkOrderAttachment,
    start_of_week,
)
from ..services import NotificationPayload, NotificationService, validate_work_order_attachment
from .common import _user_has_building_capability, format_attachment_delete_confirm
from .work_orders import _log_attachment_activity

User = get_user_model()

__all__ = [
    "core:api_units",
    "core:api_buildings",
    "core:api_workorder_attachments",
    "core:api_workorder_attachment_detail",
    "core:api_todos",
    "core:api_todo_detail",
    "core:api_todo_completed_clear",
    "core:api_todo_summary",
    "core:todo_ics_feed",
    "core:api_todo_calendar",
    "core:api_budget_requests",
    "core:api_budget_request_detail",
    "core:api_budget_expenses",
    "core:api_budget_expense_attachments",
]


def _get_work_order_or_404(request, pk: int) -> WorkOrder:
    if not request.user.is_authenticated:
        raise Http404()
    qs = WorkOrder.objects.visible_to(request.user).select_related("building")
    return get_object_or_404(qs, pk=pk)


def _office_viewer_enabled(request) -> bool:
    if not getattr(settings, "ATTACHMENTS_OFFICE_VIEWER_ENABLED", True):
        return False
    host = request.get_host().split(":", 1)[0]
    if host in {"127.0.0.1", "localhost"}:
        return False
    return True


def _attachment_payload(
    request,
    attachment: WorkOrderAttachment,
    order: WorkOrder | None = None,
) -> dict[str, object]:
    url = ""
    try:
        url = attachment.file.url
    except ValueError:
        url = ""
    absolute_url = request.build_absolute_uri(url) if url else ""
    office_viewer_allowed = _office_viewer_enabled(request)

    filename = (attachment.original_name or "").strip()
    if not filename:
        file_attr = getattr(attachment.file, "name", "")
        if file_attr:
            filename = Path(file_attr).name.strip()
    if not filename:
        filename = _("Attachment %(id)s") % {"id": attachment.pk}

    content_type = (attachment.content_type or "").lower()
    size_bytes = attachment.size or 0

    extension = Path(filename).suffix.lower().lstrip(".")
    doc_extensions = {"doc", "docx", "odt", "rtf", "txt"}
    if content_type.startswith("image/"):
        category = "image"
    elif extension == "pdf":
        category = "pdf"
    elif extension in doc_extensions:
        category = "doc"
    else:
        category = "file"

    created = timezone.localtime(attachment.created_at)

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
    office_viewer_template = getattr(
        settings,
        "ATTACHMENTS_OFFICE_VIEWER_URL",
        "https://view.officeapps.live.com/op/embed.aspx?src={url}",
    )
    preview_url = None
    preview_external = False
    if content_type.startswith("image/"):
        preview_url = url
    elif extension == "pdf":
        preview_url = url
    elif extension in office_exts and absolute_url:
        if office_viewer_allowed:
            preview_url = office_viewer_template.format(url=quote_plus(absolute_url))
        else:
            proto = office_protocol_map.get(extension)
            if proto:
                preview_url = proto.format(url=absolute_url)
                preview_external = True

    work_order = order
    if work_order is None and hasattr(attachment, "work_order"):
        work_order = attachment.work_order

    delete_url = ""
    if work_order and getattr(work_order, "pk", None):
        delete_url = reverse(
            "core:workorder_attachment_delete",
            args=[work_order.pk, attachment.pk],
        )

    return {
        "id": attachment.pk,
        "name": filename,
        "content_type": content_type,
        "size": size_bytes,
        "size_display": filesizeformat(size_bytes),
        "url": url,
        "preview_url": preview_url,
        "preview_external": preview_external,
        "created_at": created.isoformat(),
        "created_display": created.strftime("%Y-%m-%d %H:%M"),
        "is_image": content_type.startswith("image/"),
        "category": category,
        "extension": extension,
        "delete_url": delete_url,
        "delete_confirm": format_attachment_delete_confirm(filename, work_order),
    }


def _budget_queryset(request):
    if not request.user.is_authenticated:
        raise Http404()
    return (
        BudgetRequest.objects.visible_to(request.user)
        .select_related("building", "requester", "approved_by")
        .with_totals()
    )


def _budget_payload(budget: BudgetRequest) -> dict[str, object]:
    building = budget.building
    requester = budget.requester
    approver = budget.approved_by
    return {
        "id": budget.pk,
        "building": {"id": getattr(building, "pk", None), "name": getattr(building, "name", "")},
        "requester": {
            "id": getattr(requester, "pk", None),
            "name": requester.get_full_name() if requester else "",
        },
        "status": budget.status,
        "requested_amount": str(budget.requested_amount),
        "approved_amount": str(budget.approved_total),
        "spent_amount": str(budget.spent_total),
        "remaining_amount": str(budget.remaining_amount),
        "currency": budget.currency,
        "allow_overage": budget.allow_overage,
        "allow_post_close_expense": budget.allow_post_close_expense,
        "approved_by": {
            "id": getattr(approver, "pk", None),
            "name": approver.get_full_name() if approver else "",
        },
        "approved_at": budget.approved_at.isoformat() if budget.approved_at else None,
        "created_at": budget.created_at.isoformat(),
        "updated_at": budget.updated_at.isoformat(),
        "feature_flag": budget.feature_flag,
        "project_code": budget.project_code,
        "notes": budget.notes,
    }


def _expense_payload(expense: Expense) -> dict[str, object]:
    category = expense.expense_type
    return {
        "id": expense.pk,
        "budget_id": expense.budget_request_id,
        "label": expense.label,
        "amount": str(expense.amount),
        "status": expense.status,
        "notes": expense.notes,
        "incurred_on": expense.incurred_on.isoformat() if expense.incurred_on else None,
        "category": {"id": getattr(category, "pk", None), "label": getattr(category, "label", "")},
        "attachments": [
            _expense_attachment_payload(attachment)
            for attachment in expense.attachments.all()
        ],
    }


def _user_can_log_budget_expense(user, budget: BudgetRequest) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if budget.requester_id == getattr(user, "pk", None):
        return True
    resolver = CapabilityResolver(user)
    return resolver.has(Capability.MANAGE_BUDGETS, building_id=budget.building_id)


def _get_budget_or_404(request, pk: int) -> BudgetRequest:
    qs = _budget_queryset(request)
    return get_object_or_404(qs, pk=pk)


def _get_expense_or_404(request, budget_id: int, expense_id: int) -> Expense:
    if not request.user.is_authenticated:
        raise Http404()
    qs = (
        Expense.objects.filter(budget_request__in=_budget_queryset(request))
        .select_related("budget_request", "expense_type")
        .prefetch_related("attachments")
    )
    return get_object_or_404(qs, pk=expense_id, budget_request_id=budget_id)


def _expense_attachment_payload(attachment: ExpenseAttachment) -> dict[str, object]:
    return {
        "id": attachment.pk,
        "name": attachment.original_name,
        "url": attachment.file.url if attachment.file else "",
        "content_type": attachment.mime_type,
        "size": attachment.size,
        "uploaded_at": attachment.created_at.isoformat(),
    }


def _parse_bool_param(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_week_param(raw: str | None):
    if not raw:
        return None
    parsed = parse_date(raw)
    if not parsed:
        return None
    return start_of_week(parsed)


def _bounded_int_param(raw_value, *, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _todo_queryset_for(request):
    return (
        TodoItem.objects.visible_to(request.user)
        .filter(user=request.user)
        .select_related("todo_list", "user")
        .prefetch_related("activities__actor")
    )


def _user_can_assign_todo_owner(user) -> bool:
    # Task assignment is intentionally self-only for all roles.
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return False


def _user_can_view_all_todos(user) -> bool:
    return False


def _resolve_todo_owner(request, raw_owner, *, required: bool = False, allow_self: bool = False):
    if raw_owner in (None, "", "null"):
        return request.user
    try:
        owner_id = int(raw_owner)
    except (TypeError, ValueError):
        raise ValidationError(_("Invalid owner."))
    current_user_id = getattr(request.user, "pk", None)
    if owner_id != current_user_id:
        raise ValidationError(_("You can only assign tasks to yourself."))
    try:
        owner = User.objects.get(pk=owner_id, is_active=True)
    except User.DoesNotExist as exc:  # pragma: no cover - defensive
        raise ValidationError(_("Отговорник not found.")) from exc
    return owner


def _activity_payload(activity: TodoActivity) -> dict[str, object]:
    created = timezone.localtime(activity.created_at)
    return {
        "id": activity.pk,
        "action": activity.action,
        "actor": activity.actor_id,
        "metadata": activity.metadata or {},
        "created_at": created.isoformat(),
    }


def _todo_payload(item: TodoItem) -> dict[str, object]:
    activities = getattr(item, "_prefetched_objects_cache", {}).get("activities")
    if activities is None:
        activities = item.activities.all()
    owner = getattr(item, "user", None)
    owner_display = ""
    owner_username = ""
    if owner:
        owner_display = owner.get_full_name() or owner.get_username()
        owner_username = owner.get_username()
    return {
        "id": item.pk,
        "title": item.title,
        "description": item.description,
        "status": item.status,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "week_start": item.week_start.isoformat(),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "activities": [_activity_payload(act) for act in activities],
        "owner": {
            "id": item.user_id,
            "name": owner_display,
            "username": owner_username,
        },
    }


def _format_todo_due_label(item: TodoItem) -> str:
    due_date = item.due_date or item.week_start
    if not due_date:
        return _("unscheduled")
    return formats.date_format(due_date, "DATE_FORMAT")


def _todo_event_notification(item: TodoItem, action: str, *, fields=None, include_status: bool = False) -> NotificationPayload:
    action = action.lower()
    titles = {
        "created": _("Task created"),
        "updated": _("Task updated"),
        "deleted": _("Task deleted"),
    }
    due_label = _format_todo_due_label(item)
    base_body = _("“%(title)s” scheduled for %(date)s.") % {"title": item.title, "date": due_label}
    details: list[str] = []
    if action == "updated":
        if fields:
            details.append(
                _("Fields updated: %(fields)s.") % {"fields": ", ".join(fields)}
            )
        if include_status:
            details.append(_("Status: %(status)s.") % {"status": item.get_status_display()})
    elif action == "deleted":
        details.append(_("Task was removed."))

    body = " ".join([base_body] + details).strip()
    levels = {"deleted": "warning"}
    return NotificationPayload(
        key=f"todo-{item.pk}-{action}-{uuid4().hex}",
        category="todo",
        title=titles.get(action, titles["updated"]),
        body=body,
        level=levels.get(action, "info"),
    )


def _publish_todo_notification(user, item: TodoItem, action: str, **kwargs):
    if not user or not user.is_authenticated:
        return
    payload = _todo_event_notification(item, action, **kwargs)
    NotificationService(user).upsert(payload)


def _load_json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(_("Invalid JSON payload.")) from exc


def _parse_date_field(value, field_label: str | None = None):
    if value in (None, "", "null"):
        return None
    parsed = parse_date(str(value))
    if not parsed:
        label = field_label or "date"
        raise ValueError(_("Invalid %(field)s. Use YYYY-MM-DD.") % {"field": label})
    return parsed


def _format_ics_datetime(value: datetime) -> str:
    aware = timezone.localtime(value).astimezone(datetime_timezone.utc)
    return aware.strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _status_to_ical(status: str) -> str:
    mapping = {
        TodoItem.Status.PENDING: "NEEDS-ACTION",
        TodoItem.Status.IN_PROGRESS: "IN-PROCESS",
        TodoItem.Status.DONE: "COMPLETED",
        TodoItem.Status.ARCHIVED: "CANCELLED",
    }
    return mapping.get(status, "NEEDS-ACTION")


def _get_todo_or_404(request, pk: int) -> TodoItem:
    if not request.user.is_authenticated:
        raise Http404()
    qs = _todo_queryset_for(request)
    return get_object_or_404(qs, pk=pk)


def _todo_list_view(request):
    if not request.user.is_authenticated:
        raise Http404()

    params = request.GET
    include_history = _parse_bool_param(params.get("include_history") or params.get("history"))
    upcoming_only = _parse_bool_param(params.get("upcoming"))
    created_only = _parse_bool_param(params.get("created_only") or params.get("created"))
    can_view_all = _user_can_view_all_todos(request.user)
    if created_only:
        include_history = True
    week_filter = None
    if "week_start" in params:
        week_filter = _parse_week_param(params.get("week_start"))
        if params.get("week_start") and week_filter is None:
            return JsonResponse({"error": _("Invalid week_start. Use YYYY-MM-DD.")}, status=400)
    due_date_filter = None
    if "due_date" in params:
        due_date_filter = parse_date((params.get("due_date") or "").strip())
        if params.get("due_date") and due_date_filter is None:
            return JsonResponse({"error": _("Invalid due_date. Use YYYY-MM-DD.")}, status=400)

    status_param = (params.get("status") or "").strip()
    status_requested: set[str] | None = None
    if status_param:
        requested = {value.strip() for value in status_param.split(",") if value.strip()}
        valid = {choice[0] for choice in TodoItem.Status.choices}
        invalid = requested - valid
        if invalid:
            return JsonResponse({"error": _("Invalid status filter.")}, status=400)
        status_requested = requested
    if created_only:
        status_requested = {TodoItem.Status.PENDING, TodoItem.Status.IN_PROGRESS}

    qs = _todo_queryset_for(request)
    if can_view_all and (not status_requested or TodoItem.Status.DONE not in status_requested):
        qs = qs.exclude(status=TodoItem.Status.DONE)
    if upcoming_only:
        include_history = True
    if not include_history or week_filter is not None:
        target_week = week_filter or start_of_week()
        qs = qs.for_week(target_week)
    if upcoming_only:
        upcoming_days = _bounded_int_param(
            params.get("upcoming_days"),
            default=7,
            min_value=1,
            max_value=60,
        )
        today = timezone.localdate()
        qs = qs.filter(
            due_date__gt=today,
            due_date__lte=today + timedelta(days=upcoming_days),
        )

    if status_requested:
        qs = qs.filter(status__in=status_requested)

    owner_param = (params.get("owner") or "").strip()
    owner_filter_value: int | None = None
    if can_view_all:
        if owner_param:
            if owner_param.lower() == "all":
                owner_filter_value = None
            else:
                try:
                    owner_filter_value = int(owner_param)
                except (TypeError, ValueError):
                    return JsonResponse({"error": _("Invalid owner filter.")}, status=400)
        else:
            owner_filter_value = request.user.pk
    else:
        owner_filter_value = getattr(request.user, "pk", None)
    if owner_filter_value:
        qs = qs.filter(user_id=owner_filter_value)

    query = (params.get("q") or "").strip()
    if query:
        qs = qs.filter(models.Q(title__icontains=query) | models.Q(description__icontains=query))
    if due_date_filter is not None:
        qs = qs.filter(due_date=due_date_filter)

    try:
        per_value = int(params.get("per", 25))
    except (TypeError, ValueError):
        per_value = 25
    per_value = max(5, min(per_value, 200))
    try:
        page_number = int(params.get("page", 1))
    except (TypeError, ValueError):
        page_number = 1
    page_number = max(1, page_number)

    ordering_fields = ["week_start", "due_date", "pk"]
    if created_only:
        ordering_fields = ["-created_at", "-id"]
    ordered_qs = qs.order_by(*ordering_fields)
    paginator = Paginator(ordered_qs, per_value)
    page_obj = paginator.get_page(page_number)
    total_pages = max(paginator.num_pages, 1)
    results = [_todo_payload(item) for item in page_obj.object_list]
    body = {
        "results": results,
        "count": paginator.count,
        "pagination": {
            "page": page_obj.number,
            "per": per_value,
            "pages": total_pages,
            "has_previous": page_obj.has_previous(),
            "has_next": page_obj.has_next(),
        },
        "ics_url": request.build_absolute_uri(reverse("core:todo_ics_feed")),
    }
    return JsonResponse(body, status=200)


def _todo_create_view(request):
    if not request.user.is_authenticated:
        raise Http404()
    try:
        payload = _load_json_body(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    title = (payload.get("title") or "").strip()
    if not title:
        return JsonResponse({"error": {"title": [_("This field is required.")]}}, status=400)
    description = (payload.get("description") or "").strip()

    try:
        due_date = _parse_date_field(payload.get("due_date"), "due_date")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    today = timezone.localdate()
    current_week_start = start_of_week(today)
    if due_date and due_date < today:
        return JsonResponse({"error": _("Due date cannot be in the past.")}, status=400)

    week_override = payload.get("week_start")
    if week_override:
        week_start = _parse_week_param(week_override)
        if week_start is None:
            return JsonResponse({"error": _("Invalid week_start. Use YYYY-MM-DD.")}, status=400)
        if week_start < current_week_start:
            return JsonResponse({"error": _("Week start cannot be in the past.")}, status=400)
    else:
        week_start = None

    status_value = payload.get("status") or TodoItem.Status.PENDING
    valid_statuses = {choice[0] for choice in TodoItem.Status.choices}
    if status_value not in valid_statuses:
        return JsonResponse({"error": _("Invalid status value.")}, status=400)

    try:
        owner = _resolve_todo_owner(
            request,
            payload.get("owner"),
            required=False,
            allow_self=True,
        )
    except ValidationError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    item = TodoItem(
        user=owner,
        title=title,
        description=description,
        due_date=due_date,
        status=status_value,
    )
    if week_start:
        item.week_start = week_start
    item.save()
    item.log_activity(
        action=TodoActivity.Action.CREATED,
        actor=request.user,
        metadata={"status": item.status},
    )
    _publish_todo_notification(request.user, item, "created")
    return JsonResponse(_todo_payload(item), status=201)


def _todo_update_view(request, item: TodoItem):
    try:
        payload = _load_json_body(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    valid_statuses = {choice[0] for choice in TodoItem.Status.choices}
    status_change = None
    fields_changed: list[str] = []

    if "title" in payload:
        new_title = (payload.get("title") or "").strip()
        if not new_title:
            return JsonResponse({"error": {"title": [_("This field is required.")]}}, status=400)
        if new_title != item.title:
            item.title = new_title
            fields_changed.append("title")

    if "description" in payload:
        new_desc = (payload.get("description") or "").strip()
        if new_desc != item.description:
            item.description = new_desc
            fields_changed.append("description")

    if "due_date" in payload:
        try:
            new_due = _parse_date_field(payload.get("due_date"), "due_date")
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if new_due and new_due < timezone.localdate():
            return JsonResponse({"error": _("Due date cannot be in the past.")}, status=400)
        if new_due != item.due_date:
            item.due_date = new_due
            fields_changed.append("due_date")

    if "week_start" in payload:
        week_override = payload.get("week_start")
        if week_override:
            new_week = _parse_week_param(week_override)
            if new_week is None:
                return JsonResponse({"error": _("Invalid week_start. Use YYYY-MM-DD.")}, status=400)
        else:
            new_week = start_of_week()
        current_week = start_of_week(timezone.localdate())
        if new_week and new_week < current_week:
            return JsonResponse({"error": _("Week start cannot be in the past.")}, status=400)
        if new_week != item.week_start:
            item.week_start = new_week
            fields_changed.append("week_start")

    if "status" in payload:
        new_status = payload.get("status") or TodoItem.Status.PENDING
        if new_status not in valid_statuses:
            return JsonResponse({"error": _("Invalid status value.")}, status=400)
        if new_status != item.status:
            status_change = new_status

    if "owner" in payload:
        try:
            new_owner = _resolve_todo_owner(
                request,
                payload.get("owner"),
                required=True,
                allow_self=True,
            )
        except ValidationError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if new_owner != item.user:
            item.user = new_owner
            fields_changed.append("owner")

    if fields_changed:
        item.save()
        item.log_activity(
            action=TodoActivity.Action.UPDATED,
            actor=request.user,
            metadata={"fields": fields_changed},
        )

    if status_change:
        item.set_status(status_change, actor=request.user)

    if fields_changed or status_change:
        _publish_todo_notification(
            request.user,
            item,
            "updated",
            fields=fields_changed,
            include_status=bool(status_change),
        )

    return JsonResponse(_todo_payload(item), status=200)


@require_http_methods(["GET", "POST"])
def api_todos(request):
    if request.method == "GET":
        return _todo_list_view(request)
    return _todo_create_view(request)


@require_http_methods(["GET", "PATCH", "PUT", "DELETE"])
def api_todo_detail(request, pk: int):
    item = _get_todo_or_404(request, pk)
    if request.method == "GET":
        return JsonResponse(_todo_payload(item), status=200)
    if request.method in {"PATCH", "PUT"}:
        return _todo_update_view(request, item)

    if item.user_id != getattr(request.user, "pk", None):
        return JsonResponse({"error": _("You can delete only your own tasks.")}, status=403)

    item.log_activity(
        action=TodoActivity.Action.DELETED,
        actor=request.user,
        metadata={"title": item.title},
    )
    _publish_todo_notification(request.user, item, "deleted")
    item.delete()
    return HttpResponse(status=204)


@require_http_methods(["DELETE"])
def api_todo_completed_clear(request):
    if not request.user.is_authenticated:
        raise Http404()

    params = request.GET
    qs = _todo_queryset_for(request).filter(status=TodoItem.Status.DONE)

    raw_ids = params.getlist("ids")
    parsed_ids: set[int] = set()
    if raw_ids:
        for chunk in raw_ids:
            values = [part.strip() for part in str(chunk).split(",") if part and part.strip()]
            for value in values:
                try:
                    parsed_ids.add(int(value))
                except (TypeError, ValueError):
                    return JsonResponse({"error": _("Invalid ids filter.")}, status=400)
        if parsed_ids:
            qs = qs.filter(pk__in=parsed_ids)

    owner_param = (params.get("owner") or "").strip()
    owner = request.user
    owner_is_all = False
    if owner_param:
        owner_token = owner_param.lower()
        if owner_token == "all":
            return JsonResponse(
                {"error": _("You can clear only your own completed tasks.")},
                status=403,
            )
        elif owner_token in {"me", "self"}:
            owner_id = getattr(request.user, "pk", None)
        else:
            try:
                owner_id = int(owner_param)
            except (TypeError, ValueError):
                return JsonResponse({"error": _("Invalid owner filter.")}, status=400)
        if not owner_is_all:
            if owner_id != getattr(request.user, "pk", None):
                return JsonResponse(
                    {"error": _("You can clear only your own completed tasks.")},
                    status=403,
                )
            try:
                owner = User.objects.get(pk=owner_id)
            except User.DoesNotExist:
                return JsonResponse({"error": _("Отговорник not found.")}, status=404)
    if not owner_is_all:
        qs = qs.filter(user=owner)

    week_start_param = params.get("week_start")
    if week_start_param:
        week_start_value = _parse_week_param(week_start_param)
        if week_start_value is None:
            return JsonResponse({"error": _("Invalid week_start. Use YYYY-MM-DD.")}, status=400)
        qs = qs.filter(week_start=week_start_value)

    status_filter = params.get("status")
    if status_filter:
        requested = {value.strip() for value in status_filter.split(",") if value.strip()}
        qs = qs.filter(status__in=requested)

    items = list(qs)
    if not items:
        return JsonResponse({"deleted": 0}, status=200)

    deleted = 0
    with transaction.atomic():
        for item in items:
            item.log_activity(
                action=TodoActivity.Action.DELETED,
                actor=request.user,
                metadata={"title": item.title, "bulk": True},
            )
            _publish_todo_notification(request.user, item, "deleted")
            item.delete()
            deleted += 1

    return JsonResponse({"deleted": deleted}, status=200)


@require_http_methods(["GET"])
def api_todo_summary(request):
    if not request.user.is_authenticated:
        raise Http404()

    today = timezone.localdate()
    next_week = today + timedelta(days=7)
    qs = _todo_queryset_for(request).filter(user=request.user)
    active_statuses = [TodoItem.Status.PENDING, TodoItem.Status.IN_PROGRESS]

    summary = {
        "due_today": qs.filter(status__in=active_statuses, due_date=today).count(),
        "overdue": qs.filter(status__in=active_statuses, due_date__lt=today).count(),
        "due_next_7_days": qs.filter(
            status__in=active_statuses,
            due_date__gt=today,
            due_date__lte=next_week,
        ).count(),
        "completed": qs.filter(status=TodoItem.Status.DONE).count(),
    }
    return JsonResponse(summary, status=200)


@require_http_methods(["GET"])
def todo_ics_feed(request):
    if not request.user.is_authenticated:
        raise Http404()
    qs = (
        TodoItem.objects.visible_to(request.user)
        .filter(user=request.user)
        .exclude(status=TodoItem.Status.ARCHIVED)
        .order_by("due_date", "pk")
    )
    now = timezone.now()
    cal_name = request.user.get_full_name() or request.user.get_username()
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//BuildingMgmt//Todo//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(cal_name)} To-Dos",
    ]
    for item in qs:
        due_date = item.due_date or item.week_start
        lines.append("BEGIN:VTODO")
        lines.append(f"UID:todo-{item.pk}@building-mgmt")
        lines.append(f"DTSTAMP:{_format_ics_datetime(now)}")
        lines.append(f"LAST-MODIFIED:{_format_ics_datetime(item.updated_at)}")
        lines.append(f"SUMMARY:{_ics_escape(item.title)}")
        if due_date:
            lines.append(f"DUE;VALUE=DATE:{due_date.strftime('%Y%m%d')}")
        if item.description:
            lines.append(f"DESCRIPTION:{_ics_escape(item.description)}")
        lines.append(f"STATUS:{_status_to_ical(item.status)}")
        if item.completed_at:
            lines.append(f"COMPLETED:{_format_ics_datetime(item.completed_at)}")
        lines.append("END:VTODO")
    lines.append("END:VCALENDAR")
    content = "\r\n".join(lines) + "\r\n"
    response = HttpResponse(content, content_type="text/calendar")
    response["Content-Disposition"] = f'attachment; filename=\"todos-{request.user.pk}.ics\"'
    return response


@require_http_methods(["GET"])
def api_todo_calendar(request):
    if not request.user.is_authenticated:
        raise Http404()
    try:
        start_date = _parse_date_field(request.GET.get("start"), "start")
        end_date = _parse_date_field(request.GET.get("end"), "end")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if not start_date or not end_date:
        return JsonResponse({"error": _("Both start and end dates are required.")}, status=400)
    if end_date < start_date:
        return JsonResponse({"error": _("End date must be after start date.")}, status=400)

    qs = (
        TodoItem.objects.visible_to(request.user)
        .filter(user=request.user)
        .filter(
            models.Q(due_date__range=(start_date, end_date))
            | models.Q(due_date__isnull=True, week_start__range=(start_date, end_date))
        )
        .only("id", "title", "status", "due_date", "week_start", "completed_at")
    )
    events = []
    for item in qs:
        event_date = item.due_date or item.week_start
        events.append(
            {
                "id": item.pk,
                "title": item.title,
                "status": item.status,
                "date": event_date.isoformat(),
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
            }
        )
    return JsonResponse({"events": events}, status=200)

def api_units(request, building_id: int | None = None):
    """
    JSON list of units visible to the current user.
    Optional filter: ?building=<id> (validated for visibility) or via path parameter.
    """
    if not request.user.is_authenticated:
        raise Http404()

    qs = Unit.objects.visible_to(request.user).select_related("building")
    bld_qs = Building.objects.visible_to(request.user)

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


@require_http_methods(["GET", "POST"])
def api_budget_requests(request):
    qs = _budget_queryset(request)
    if request.method == "GET":
        status_filter = (request.GET.get("status") or "").strip()
        if status_filter:
            qs = qs.filter(status=status_filter)
        building_param = request.GET.get("building")
        if building_param:
            try:
                qs = qs.filter(building_id=int(building_param))
            except (TypeError, ValueError):
                return JsonResponse({"error": _("Invalid building filter.")}, status=400)
        requester_param = request.GET.get("requester")
        if requester_param:
            if requester_param.lower() == "me":
                qs = qs.filter(requester=request.user)
            else:
                try:
                    qs = qs.filter(requester_id=int(requester_param))
                except (TypeError, ValueError):
                    return JsonResponse({"error": _("Invalid requester filter.")}, status=400)
        results = [_budget_payload(budget) for budget in qs.order_by("-created_at")[:200]]
        return JsonResponse({"results": results}, status=200)

    data = request.POST or None
    if request.content_type and "application/json" in request.content_type:
        try:
            data = _load_json_body(request)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
    form = BudgetRequestForm(data=data, user=request.user)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)
    budget = form.save()
    budget.transition(
        status=BudgetRequest.Status.PENDING_REVIEW,
        actor=request.user,
        comment=budget.notes or "",
    )
    return JsonResponse(_budget_payload(budget), status=201)


@require_http_methods(["GET", "PATCH"])
def api_budget_request_detail(request, pk: int):
    budget = _get_budget_or_404(request, pk)
    if request.method == "GET":
        return JsonResponse(_budget_payload(budget), status=200)
    resolver = CapabilityResolver(request.user)
    if not resolver.has(Capability.APPROVE_BUDGETS, building_id=budget.building_id):
        return JsonResponse({"error": _("You cannot approve this budget.")}, status=403)
    try:
        data = _load_json_body(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    form = BudgetRequestApprovalForm(data=data, instance=budget, user=request.user)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)
    form.save()
    budget.refresh_from_db()
    return JsonResponse(_budget_payload(budget), status=200)


@require_http_methods(["GET", "POST"])
def api_budget_expenses(request, budget_id: int):
    budget = _get_budget_or_404(request, budget_id)
    if request.method == "GET":
        per_value = _bounded_int_param(request.GET.get("per"), default=50, min_value=10, max_value=200)
        page_value = _bounded_int_param(request.GET.get("page"), default=1, min_value=1, max_value=10_000)
        expenses_qs = (
            budget.expenses.select_related("expense_type")
            .prefetch_related("attachments")
            .order_by("-incurred_on", "-id")
        )
        paginator = Paginator(expenses_qs, per_value)
        page_obj = paginator.get_page(page_value)
        return JsonResponse(
            {
                "results": [_expense_payload(expense) for expense in page_obj.object_list],
                "count": paginator.count,
                "pagination": {
                    "page": page_obj.number,
                    "per": per_value,
                    "pages": max(1, paginator.num_pages),
                    "has_previous": page_obj.has_previous(),
                    "has_next": page_obj.has_next(),
                },
            },
            status=200,
        )
    if not _user_can_log_budget_expense(request.user, budget):
        return JsonResponse({"error": _("You cannot add expenses to this budget.")}, status=403)
    data = request.POST or None
    files = request.FILES or None
    if request.content_type and "application/json" in request.content_type:
        try:
            data = _load_json_body(request)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        files = None
    form = BudgetExpenseForm(data=data, files=files, user=request.user, budget=budget)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)
    expense = form.save()
    expense.refresh_from_db()
    return JsonResponse(_expense_payload(expense), status=201)


@require_http_methods(["GET", "POST", "DELETE"])
def api_budget_expense_attachments(request, budget_id: int, expense_id: int):
    expense = _get_expense_or_404(request, budget_id, expense_id)
    if request.method == "GET":
        attachments = [_expense_attachment_payload(item) for item in expense.attachments.all()]
        return JsonResponse({"results": attachments}, status=200)
    if not _user_can_log_budget_expense(request.user, expense.budget_request):
        return JsonResponse({"error": _("You cannot modify attachments for this expense.")}, status=403)
    if request.method == "POST":
        uploaded = request.FILES.get("file")
        if not uploaded:
            return JsonResponse({"error": _("Upload a file via the `file` field.")}, status=400)
        validate_work_order_attachment(uploaded)
        attachment = ExpenseAttachment.objects.create(
            expense=expense,
            file=uploaded,
            original_name=uploaded.name,
            uploaded_by=request.user,
        )
        return JsonResponse({"attachment": _expense_attachment_payload(attachment)}, status=201)
    attachment_id = request.GET.get("attachment")
    if not attachment_id:
        return JsonResponse({"error": _("Specify an `attachment` id to delete.")}, status=400)
    try:
        attachment = expense.attachments.get(pk=int(attachment_id))
    except (ExpenseAttachment.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"error": _("Attachment not found.")}, status=404)
    attachment.delete()
    return HttpResponse(status=204)


@require_http_methods(["GET", "POST"])
def api_workorder_attachments(request, pk: int):
    order = _get_work_order_or_404(request, pk)

    if request.method == "GET":
        per_value = _bounded_int_param(request.GET.get("per"), default=50, min_value=10, max_value=200)
        page_value = _bounded_int_param(request.GET.get("page"), default=1, min_value=1, max_value=10_000)
        attachments_qs = order.attachments.order_by("-created_at")
        paginator = Paginator(attachments_qs, per_value)
        page_obj = paginator.get_page(page_value)
        attachments = [
            _attachment_payload(request, obj, order)
            for obj in page_obj.object_list
        ]
        return JsonResponse(
            {
                "attachments": attachments,
                "count": paginator.count,
                "pagination": {
                    "page": page_obj.number,
                    "per": per_value,
                    "pages": max(1, paginator.num_pages),
                    "has_previous": page_obj.has_previous(),
                    "has_next": page_obj.has_next(),
                },
            },
            status=200,
        )

    if not _user_has_building_capability(
        request.user,
        order.building,
        Capability.CREATE_WORK_ORDERS,
        Capability.MANAGE_BUILDINGS,
    ):
        return JsonResponse(
            {"error": _("You do not have permission to modify attachments for this work order.")},
            status=403,
        )

    files = request.FILES.getlist("files") or request.FILES.getlist("file")
    if not files:
        return JsonResponse({"error": _("No files were provided.")}, status=400)

    errors: list[dict[str, object]] = []
    valid_files = []
    for uploaded in files:
        try:
            validate_work_order_attachment(uploaded)
        except ValidationError as exc:
            errors.append(
                {
                    "name": getattr(uploaded, "name", ""),
                    "errors": exc.messages,
                }
            )
        else:
            valid_files.append(uploaded)

    if errors and not valid_files:
        return JsonResponse({"errors": errors}, status=400)

    created_payloads: list[dict[str, object]] = []
    added_names: list[str] = []
    for uploaded in valid_files:
        attachment = WorkOrderAttachment(
            work_order=order,
            file=uploaded,
            original_name=getattr(uploaded, "name", ""),
        )
        attachment.save()
        display_name = (attachment.original_name or "").strip()
        if not display_name and attachment.file:
            display_name = Path(attachment.file.name).name
        added_names.append(display_name)
        created_payloads.append(_attachment_payload(request, attachment, order))

    body: dict[str, object] = {"attachments": created_payloads}
    if errors:
        body["errors"] = errors
    actor = request.user if request.user.is_authenticated else None
    _log_attachment_activity(
        actor=actor,
        work_order=order,
        changes={"added": added_names, "removed": []},
    )
    return JsonResponse(body, status=207 if errors else 201)


@require_http_methods(["DELETE"])
def api_workorder_attachment_detail(request, pk: int, attachment_id: int):
    order = _get_work_order_or_404(request, pk)

    if not _user_has_building_capability(
        request.user,
        order.building,
        Capability.CREATE_WORK_ORDERS,
        Capability.MANAGE_BUILDINGS,
    ):
        return JsonResponse(
            {"error": _("You do not have permission to modify attachments for this work order.")},
            status=403,
        )

    attachment = get_object_or_404(order.attachments, pk=attachment_id)
    display_name = (attachment.original_name or "").strip()
    if not display_name and attachment.file:
        display_name = Path(attachment.file.name).name
    attachment.delete()
    actor = request.user if request.user.is_authenticated else None
    _log_attachment_activity(
        actor=actor,
        work_order=order,
        changes={"added": [], "removed": [display_name]},
    )
    return JsonResponse({"status": "deleted"}, status=200)
