from __future__ import annotations

from decimal import Decimal
import re
from typing import Iterable

from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.db.models import Q
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _

from ..authz import Capability, CapabilityResolver

__all__ = [
    "AdminRequiredMixin",
    "CachedObjectMixin",
    "_safe_next_url",
    "_querystring_without",
    "_user_can_access_building",
    "_user_has_capability",
    "_user_has_building_capability",
    "CapabilityRequiredMixin",
    "format_attachment_delete_confirm",
    "attach_expense_totals_by_metadata",
]


def _user_can_access_building(user, building) -> bool:
    """Use membership visibility rules to determine access to a building."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    resolver = CapabilityResolver(user)
    building_id = getattr(building, "pk", None)
    if building_id is None:
        return False
    visible_ids = resolver.visible_building_ids()
    if visible_ids is None:
        return True
    return building_id in visible_ids


def _safe_next_url(request: HttpRequest) -> str | None:
    """Return a user-supplied 'next' URL if it's safe; otherwise ``None``."""
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return None


def _querystring_without(request: HttpRequest, *keys: str) -> str:
    params = request.GET.copy()
    for key in keys:
        params.pop(key, None)
    return params.urlencode()


def format_attachment_delete_confirm(filename: str | None, order=None) -> str:
    """
    Build a human-friendly confirmation message for deleting an attachment,
    matching the wording used across other delete confirmations.
    """
    name = (filename or "").strip() or _("this attachment")
    if order is not None:
        order_title = getattr(order, "title", "").strip()
        building = getattr(order, "building", None)
        building_name = getattr(building, "name", "").strip() if building else ""
        if order_title and building_name:
            return format_html(
                _(
                    "Are you sure you want to delete <strong>{filename}</strong> from <strong>{order}</strong> for <strong>{building}</strong>?"
                ),
                filename=name,
                order=order_title,
                building=building_name,
            )
        if order_title:
            return format_html(
                _(
                    "Are you sure you want to delete <strong>{filename}</strong> from <strong>{order}</strong>?"
                ),
                filename=name,
                order=order_title,
            )
    return format_html(
        _("Are you sure you want to delete <strong>{filename}</strong>?"),
        filename=name,
    )


def _coerce_metadata_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_WORK_ORDER_PATTERN = re.compile(r"work\s*order\s+#?(\d+)", re.IGNORECASE)


def _extract_work_order_id_from_text(*texts) -> int | None:
    for text in texts:
        if not text:
            continue
        match = _WORK_ORDER_PATTERN.search(str(text))
        if match:
            return _coerce_metadata_int(match.group(1))
    return None


def attach_expense_totals_by_metadata(
    objects,
    *,
    object_id_attr: str = "id",
    metadata_key: str = "work_order_id",
    metadata_keys: tuple[str, ...] | None = None,
    include_budget_building_fallback: bool = False,
    include_work_order_text_fallback: bool = False,
    target_attr: str = "expense_total",
):
    """
    Attach aggregated Expense amounts to each object using Expense.metadata links.

    Returns a list with mutated objects so callers can safely reassign context
    object lists used by templates.
    """
    items = list(objects or [])
    if not items:
        return items

    object_ids: set[int] = set()
    for item in items:
        value = _coerce_metadata_int(getattr(item, object_id_attr, None))
        if value is not None:
            object_ids.add(value)
    if not object_ids:
        for item in items:
            setattr(item, target_attr, Decimal("0.00"))
        return items

    from ..models import Expense, WorkOrder

    totals: dict[int, Decimal] = {object_id: Decimal("0.00") for object_id in object_ids}
    keys = tuple(metadata_keys or (metadata_key,))
    id_values: list[object] = list(object_ids) + [str(object_id) for object_id in object_ids]
    expense_filters = Q()
    for key in keys:
        expense_filters |= Q(**{f"metadata__{key}__in": id_values})
    if include_budget_building_fallback:
        expense_filters |= Q(budget_request__building_id__in=list(object_ids))
    if include_work_order_text_fallback:
        expense_filters |= Q(label__iregex=r"work\s*order\s*#?\d+") | Q(notes__iregex=r"work\s*order\s*#?\d+")
    expenses = (
        Expense.objects.filter(
            status__in=[Expense.Status.LOGGED, Expense.Status.APPROVED],
        )
        .filter(expense_filters)
        .select_related("budget_request")
        .only("amount", "metadata", "label", "notes", "budget_request__building_id")
        .iterator()
    )
    pending_work_order_expenses: list[tuple[Decimal, int]] = []
    for expense in expenses:
        metadata = expense.metadata or {}
        object_id = None
        for key in keys:
            object_id = _coerce_metadata_int(metadata.get(key))
            if object_id is not None:
                break
        if object_id is None and include_budget_building_fallback:
            object_id = _coerce_metadata_int(getattr(expense.budget_request, "building_id", None))
        if object_id is None and include_work_order_text_fallback:
            parsed_work_order_id = _extract_work_order_id_from_text(
                getattr(expense, "label", ""),
                getattr(expense, "notes", ""),
            )
            if parsed_work_order_id is not None:
                if "building" in keys:
                    pending_work_order_expenses.append((expense.amount or Decimal("0.00"), parsed_work_order_id))
                else:
                    object_id = parsed_work_order_id
        if object_id is None or object_id not in totals:
            continue
        totals[object_id] += expense.amount or Decimal("0.00")

    if include_work_order_text_fallback and pending_work_order_expenses and "building" in keys:
        work_order_ids = sorted({work_order_id for _, work_order_id in pending_work_order_expenses})
        work_order_to_building = dict(
            WorkOrder.objects.filter(pk__in=work_order_ids).values_list("pk", "building_id")
        )
        for amount, work_order_id in pending_work_order_expenses:
            building_id = _coerce_metadata_int(work_order_to_building.get(work_order_id))
            if building_id is None or building_id not in totals:
                continue
            totals[building_id] += amount

    for item in items:
        object_id = _coerce_metadata_int(getattr(item, object_id_attr, None))
        setattr(item, target_attr, totals.get(object_id, Decimal("0.00")))
    return items


class CachedObjectMixin:
    """Cache ``get_object`` results within the request lifecycle."""

    _object_cache_attr = "_cached_object"

    def get_object(self, queryset=None):  # type: ignore[override]
        if hasattr(self, self._object_cache_attr):
            return getattr(self, self._object_cache_attr)
        obj = super().get_object(queryset)
        setattr(self, self._object_cache_attr, obj)
        return obj


class AdminRequiredMixin(UserPassesTestMixin):
    """Restrict access to superusers."""

    def test_func(self) -> bool:
        user = self.request.user
        return user.is_authenticated and user.is_superuser

    def handle_no_permission(self):
        return redirect_to_login(
            self.request.get_full_path(),
            self.get_login_url(),
            self.get_redirect_field_name(),
        )


def _user_has_capability(user, capability: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    resolver = CapabilityResolver(user)
    return resolver.has(capability)


def _user_has_building_capability(user, building, *capabilities: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if building is None:
        return False
    building_id = getattr(building, "pk", None)
    if building_id is None:
        return False
    resolver = CapabilityResolver(user)
    for capability in capabilities or (Capability.MANAGE_BUILDINGS,):
        if resolver.has(capability, building_id=building_id):
            return True
    return False


class CapabilityRequiredMixin(UserPassesTestMixin):
    """Require at least one capability (optionally scoped to a building)."""

    required_capabilities: tuple[str, ...] = tuple()
    capability_building_kwarg: str | None = None
    raise_exception = True
    permission_denied_message = _("You do not have permission to access this page.")

    def get_required_capabilities(self) -> tuple[str, ...]:
        return tuple(self.required_capabilities or ())

    def get_capability_building_id(self):
        kwarg = self.capability_building_kwarg
        if kwarg and kwarg in self.kwargs:
            try:
                return int(self.kwargs[kwarg])
            except (TypeError, ValueError):
                return None
        return None

    def test_func(self) -> bool:
        user = self.request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        resolver = CapabilityResolver(user)
        building_id = self.get_capability_building_id()
        capabilities = self.get_required_capabilities()
        if not capabilities:
            return True
        return any(resolver.has(cap, building_id=building_id) for cap in capabilities)

    def handle_no_permission(self):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                self.get_login_url(),
                self.get_redirect_field_name(),
            )
        return super().handle_no_permission()
