from __future__ import annotations

from django.db.models import Q

from core.models import BuildingMembership, MembershipRole


def user_can_approve_work_orders(user, building_id: int | None) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    approver_roles = {MembershipRole.BACKOFFICE, MembershipRole.ADMINISTRATOR}
    qs = BuildingMembership.objects.filter(user=user)
    if building_id:
        qs = qs.filter(Q(building__isnull=True) | Q(building_id=building_id))
    else:
        qs = qs.filter(building__isnull=True)
    return qs.filter(role__in=approver_roles).exists()


def user_has_role(user, role: str, building_id: int | None = None) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    qs = BuildingMembership.objects.filter(user=user, role=role)
    if building_id:
        qs = qs.filter(Q(building__isnull=True) | Q(building_id=building_id))
    return qs.exists()


def user_is_lawyer(user, building_id: int | None = None) -> bool:
    return user_has_role(user, MembershipRole.LAWYER, building_id=building_id)
