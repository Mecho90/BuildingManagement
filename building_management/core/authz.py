from __future__ import annotations

from typing import Iterable, Optional, Set

from django.utils.functional import cached_property

from .models import BuildingMembership, Capability, RoleAuditLog, WorkOrderAuditLog


class CapabilityResolver:
    """Resolves role-based capabilities for a user."""

    def __init__(self, user):
        self.user = user

    @cached_property
    def _memberships(self) -> Iterable[BuildingMembership]:
        if not self.user or not getattr(self.user, "is_authenticated", False):
            return []
        return list(
            BuildingMembership.objects.filter(user=self.user).select_related("building")
        )

    @cached_property
    def _global_capabilities(self) -> Set[str]:
        caps: Set[str] = set()
        for membership in self._memberships:
            if membership.building_id is None:
                caps |= membership.resolved_capabilities
        return caps

    def visible_building_ids(self) -> Optional[Set[int]]:
        if Capability.VIEW_ALL_BUILDINGS in self._global_capabilities:
            return None
        building_ids = {m.building_id for m in self._memberships if m.building_id}
        return building_ids

    def capabilities_for(self, building_id: Optional[int] = None) -> Set[str]:
        caps = set(self._global_capabilities)
        if building_id is None:
            return caps
        for membership in self._memberships:
            if membership.building_id == building_id:
                caps |= membership.resolved_capabilities
        return caps

    def has(self, capability: str, *, building_id: Optional[int] = None) -> bool:
        caps = self.capabilities_for(building_id)
        return capability in caps


def log_role_action(*, actor, target_user, building, role: str, action: str, payload=None):
    payload = payload or {}
    return RoleAuditLog.objects.create(
        actor=actor,
        target_user=target_user,
        building=building,
        role=role,
        action=action,
        payload=payload,
    )


def log_workorder_action(*, actor, work_order, action: str, payload=None):
    payload = payload or {}
    return WorkOrderAuditLog.objects.create(
        actor=actor,
        work_order=work_order,
        building=work_order.building,
        action=action,
        payload=payload,
    )
