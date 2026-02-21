from __future__ import annotations

from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .authz import Capability, CapabilityResolver
from .models import Building, BudgetFeatureFlag, MembershipRole, TodoItem, start_of_week
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
        "todos_enabled": False,
        "todos_url": "",
        "todos_badge_count": 0,
        "office_building_enabled": False,
        "office_building_url": "",
        "office_building_pattern": "",
        "budgets_enabled": False,
        "budgets_url": "",
        "budgets_archived_enabled": False,
        "budgets_archived_url": "",
        "budget_review_enabled": False,
        "budget_review_url": "",
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
        elif getattr(user, "is_superuser", False):
            try:
                data["user_management_url"] = reverse("core:users_list")
                data["can_view_user_management"] = True
            except NoReverseMatch:
                pass
        if resolver.has(Capability.VIEW_BUDGETS) and BudgetFeatureFlag.is_enabled_for(user):
            try:
                data["budgets_url"] = reverse("core:budget_list")
                data["budgets_enabled"] = True
            except NoReverseMatch:
                pass
            if resolver.has(Capability.APPROVE_BUDGETS):
                try:
                    data["budgets_archived_url"] = reverse("core:budget_archived_list")
                    data["budgets_archived_enabled"] = True
                except NoReverseMatch:
                    pass
        if resolver.has(Capability.APPROVE_BUDGETS) and BudgetFeatureFlag.is_enabled_for(user):
            try:
                data["budget_review_url"] = reverse("core:budget_review_queue")
                data["budget_review_enabled"] = True
            except NoReverseMatch:
                pass
        if resolver.has(Capability.VIEW_AUDIT_LOG):
            try:
                data["role_audit_url"] = reverse("core:audit_trail")
                data["role_audit_enabled"] = True
            except NoReverseMatch:
                pass
        try:
            data["todos_url"] = reverse("core:todo_list")
            data["todos_enabled"] = True
        except NoReverseMatch:
            pass
        try:
            week_start = start_of_week()
            badge_count = (
                TodoItem.objects.filter(
                    user=user,
                    status__in=[TodoItem.Status.PENDING, TodoItem.Status.IN_PROGRESS],
                    week_start=week_start,
                )
                .only("id")
                .count()
            )
            data["todos_badge_count"] = badge_count
        except Exception:
            data["todos_badge_count"] = 0

        is_admin_role = user_has_role(user, MembershipRole.ADMINISTRATOR)
        is_backoffice_role = user_has_role(user, MembershipRole.BACKOFFICE)
        if is_admin_role or is_backoffice_role or getattr(user, "is_superuser", False):
            office_id = None
            try:
                office_id = Building.system_default_id()
            except Exception:
                office_id = None
            if office_id:
                try:
                    data["office_building_url"] = reverse("core:building_detail", args=[office_id])
                    data["office_building_pattern"] = f"/buildings/{office_id}/"
                    data["office_building_enabled"] = True
                except NoReverseMatch:
                    pass
    return data
