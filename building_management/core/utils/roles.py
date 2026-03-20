from __future__ import annotations

from core.models import BuildingMembership, MembershipRole


def _cached_memberships(user):
    if not user or not getattr(user, "is_authenticated", False):
        return []
    cached = getattr(user, "_membership_rows_cache", None)
    if cached is not None:
        return cached
    rows = list(
        BuildingMembership.objects.filter(user=user).values_list("building_id", "role")
    )
    setattr(user, "_membership_rows_cache", rows)
    return rows


def user_can_approve_work_orders(user, building_id: int | None) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    approver_roles = {MembershipRole.BACKOFFICE, MembershipRole.ADMINISTRATOR}
    for membership_building_id, membership_role in _cached_memberships(user):
        if membership_role not in approver_roles:
            continue
        if building_id:
            if membership_building_id is None or membership_building_id == building_id:
                return True
        elif membership_building_id is None:
            return True
    return False


def user_has_role(user, role: str, building_id: int | None = None) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    for membership_building_id, membership_role in _cached_memberships(user):
        if membership_role != role:
            continue
        if building_id:
            if membership_building_id is None or membership_building_id == building_id:
                return True
        else:
            return True
    return False


def user_is_lawyer(user, building_id: int | None = None) -> bool:
    return user_has_role(user, MembershipRole.LAWYER, building_id=building_id)


def user_is_admin_or_backoffice(user, building_id: int | None = None) -> bool:
    """
    Return True when the user holds a global Administrator/Backoffice role or is a superuser.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return user_has_role(user, MembershipRole.ADMINISTRATOR, building_id=building_id) or user_has_role(
        user,
        MembershipRole.BACKOFFICE,
        building_id=building_id,
    )
