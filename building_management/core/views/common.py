from __future__ import annotations

from typing import Iterable

from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _

__all__ = [
    "AdminRequiredMixin",
    "CachedObjectMixin",
    "_safe_next_url",
    "_querystring_without",
    "_user_can_access_building",
    "format_attachment_delete_confirm",
]


def _user_can_access_building(user, building) -> bool:
    """Staff can access everything; others only their own buildings."""
    return user.is_staff or building.owner_id == user.id


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
