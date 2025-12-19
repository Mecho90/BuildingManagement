from __future__ import annotations

from django.urls import NoReverseMatch, reverse

from .authz import Capability, CapabilityResolver
from .models import MembershipRole
from .utils.roles import user_has_role, user_is_lawyer


def theme(request):
    data = {
        "theme": request.session.get("theme", "light"),
        "work_orders_enabled": False,
        "work_orders_url": "",
        "work_orders_archive_enabled": False,
        "work_orders_archive_url": "",
        "mass_assign_work_orders_enabled": False,
        "mass_assign_work_orders_url": "",
        "can_view_user_management": False,
        "user_management_url": "",
        "role_audit_enabled": False,
        "role_audit_url": "",
        "lawyer_orders_enabled": False,
        "lawyer_orders_url": "",
    }

    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        resolver = CapabilityResolver(user)
        try:
            data["work_orders_url"] = reverse("core:work_orders_list")
            data["work_orders_enabled"] = True
        except NoReverseMatch:
            pass

        if resolver.has(Capability.VIEW_ALL_BUILDINGS):
            try:
                data["work_orders_archive_url"] = reverse("core:work_orders_archive")
                data["work_orders_archive_enabled"] = True
            except NoReverseMatch:
                pass
        if resolver.has(Capability.MASS_ASSIGN):
            try:
                data["mass_assign_work_orders_url"] = reverse("core:work_orders_mass_assign")
                data["mass_assign_work_orders_enabled"] = True
            except NoReverseMatch:
                pass
        if user_is_lawyer(user) or user_has_role(user, MembershipRole.ADMINISTRATOR) or getattr(user, "is_superuser", False):
            try:
                data["lawyer_orders_url"] = reverse("core:lawyer_work_orders")
                data["lawyer_orders_enabled"] = True
            except NoReverseMatch:
                pass
        if resolver.has(Capability.VIEW_USERS):
            try:
                data["user_management_url"] = reverse("core:users_list")
                data["can_view_user_management"] = True
            except NoReverseMatch:
                pass
        if resolver.has(Capability.VIEW_AUDIT_LOG):
            try:
                data["role_audit_url"] = reverse("core:audit_trail")
                data["role_audit_enabled"] = True
            except NoReverseMatch:
                pass
    return data
