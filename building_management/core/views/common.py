from __future__ import annotations

from typing import Iterable

from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest
from django.utils.http import url_has_allowed_host_and_scheme

__all__ = [
    "AdminRequiredMixin",
    "CachedObjectMixin",
    "_safe_next_url",
    "_querystring_without",
    "_user_can_access_building",
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
