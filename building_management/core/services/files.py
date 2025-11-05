from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Iterable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext_lazy as _
from django.utils.module_loading import import_string


@dataclass(frozen=True)
class AttachmentValidationConfig:
    max_bytes: int
    allowed_mime_types: tuple[str, ...]
    allowed_mime_prefixes: tuple[str, ...]
    enforce_type_check: bool


def _config_from_settings() -> AttachmentValidationConfig:
    max_bytes = getattr(settings, "WORK_ORDER_ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024)
    if max_bytes <= 0:
        raise ImproperlyConfigured("WORK_ORDER_ATTACHMENT_MAX_BYTES must be a positive integer.")

    raw_types = getattr(
        settings,
        "WORK_ORDER_ATTACHMENT_ALLOWED_TYPES",
        (
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/csv",
            "application/zip",
            "application/x-zip-compressed",
            "application/x-7z-compressed",
            "application/x-tar",
            "application/gzip",
        ),
    )
    if isinstance(raw_types, str):
        tokens = raw_types.split(",")
    else:
        tokens = raw_types
    allowed_types = tuple(sorted({str(t).strip().lower() for t in tokens if str(t).strip()}))

    raw_prefixes = getattr(
        settings,
        "WORK_ORDER_ATTACHMENT_ALLOWED_PREFIXES",
        ("image/", "application/zip", "application/x-7z", "application/x-gzip"),
    )
    if isinstance(raw_prefixes, str):
        tokens = raw_prefixes.split(",")
    else:
        tokens = raw_prefixes
    allowed_prefixes = tuple(sorted({str(p).strip().lower() for p in tokens if str(p).strip()}))

    if not allowed_prefixes:
        allowed_prefixes = ("image/",)

    return AttachmentValidationConfig(
        max_bytes=max_bytes,
        allowed_mime_types=allowed_types,
        allowed_mime_prefixes=allowed_prefixes,
        enforce_type_check=bool(allowed_types or allowed_prefixes),
    )


def _sniff_mime(uploaded_file) -> str:
    mime = getattr(uploaded_file, "content_type", "") or ""
    mime = mime.lower()
    if not mime or mime == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(getattr(uploaded_file, "name", ""))
        if guessed:
            mime = guessed.lower()
    return mime


def run_antivirus_scan(uploaded_file) -> None:
    """
    Placeholder hook for AV scanning.

    Projects can override the handler via the WORK_ORDER_ATTACHMENT_SCAN_HANDLER
    setting with a dotted import path to a callable that raises ValidationError
    on failure. The default implementation is a no-op.
    """
    handler = getattr(settings, "WORK_ORDER_ATTACHMENT_SCAN_HANDLER", None)
    if not handler:
        return

    scan_callable = handler
    if isinstance(handler, str):
        scan_callable = import_string(handler)
    if not callable(scan_callable):
        raise ImproperlyConfigured(
            "WORK_ORDER_ATTACHMENT_SCAN_HANDLER must be a callable or dotted path."
        )
    scan_callable(uploaded_file)


def _matches_type(mime: str, allowed_types: Iterable[str], allowed_prefixes: Iterable[str]) -> bool:
    mime = (mime or "").lower()
    if mime and any(mime == t for t in allowed_types):
        return True
    if mime and any(mime.startswith(prefix) for prefix in allowed_prefixes):
        return True
    return False


def validate_work_order_attachment(uploaded_file) -> None:
    """
    Validate uploaded files for WorkOrderAttachment.
    Raises ValidationError when input violates policies.
    """
    config = _config_from_settings()

    size = getattr(uploaded_file, "size", 0) or 0
    if size > config.max_bytes:
        raise ValidationError(
            _("Files must be smaller than %(size)s."),
            params={"size": filesizeformat(config.max_bytes)},
            code="file_too_large",
        )

    mime = _sniff_mime(uploaded_file)
    if config.enforce_type_check and not _matches_type(
        mime, config.allowed_mime_types, config.allowed_mime_prefixes
    ):
        raise ValidationError(
            _(
                "Unsupported file type: %(mime)s. Allowed types: %(types)s or %(prefixes)s."
            ),
            params={
                "mime": mime or _("unknown"),
                "types": ", ".join(config.allowed_mime_types),
                "prefixes": ", ".join(config.allowed_mime_prefixes),
            },
            code="invalid_file_type",
        )

    run_antivirus_scan(uploaded_file)
