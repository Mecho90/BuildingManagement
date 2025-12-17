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
