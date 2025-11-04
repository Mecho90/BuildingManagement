from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from urllib.parse import quote_plus

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from ..models import Building, Unit, WorkOrder, WorkOrderAttachment
from ..services import validate_work_order_attachment
from .common import _user_can_access_building, format_attachment_delete_confirm

__all__ = [
    "api_units",
    "api_buildings",
    "api_workorder_attachments",
    "api_workorder_attachment_detail",
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
            "workorder_attachment_delete",
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


@require_http_methods(["GET", "POST"])
def api_workorder_attachments(request, pk: int):
    order = _get_work_order_or_404(request, pk)

    if request.method == "GET":
        attachments = [
            _attachment_payload(request, obj, order)
            for obj in order.attachments.order_by("-created_at")
        ]
        return JsonResponse({"attachments": attachments}, status=200)

    if not _user_can_access_building(request.user, order.building):
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
    for uploaded in valid_files:
        attachment = WorkOrderAttachment(
            work_order=order,
            file=uploaded,
            original_name=getattr(uploaded, "name", ""),
        )
        attachment.save()
        created_payloads.append(_attachment_payload(request, attachment, order))

    body: dict[str, object] = {"attachments": created_payloads}
    if errors:
        body["errors"] = errors
    return JsonResponse(body, status=207 if errors else 201)


@require_http_methods(["DELETE"])
def api_workorder_attachment_detail(request, pk: int, attachment_id: int):
    order = _get_work_order_or_404(request, pk)

    if not _user_can_access_building(request.user, order.building):
        return JsonResponse(
            {"error": _("You do not have permission to modify attachments for this work order.")},
            status=403,
        )

    attachment = get_object_or_404(order.attachments, pk=attachment_id)
    attachment.delete()
    return JsonResponse({"status": "deleted"}, status=200)
