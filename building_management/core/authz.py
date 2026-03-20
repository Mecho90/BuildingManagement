from __future__ import annotations

import logging
from typing import Iterable, Optional, Set

from django.utils.functional import cached_property

from .models import (
    Building,
    BuildingMembership,
    Capability,
    MembershipRole,
    RoleAuditLog,
    WorkOrderAuditLog,
)

logger = logging.getLogger(__name__)


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

    @cached_property
    def _office_building_id(self) -> int | None:
        office_id = Building.system_default_id()
        if office_id:
            return office_id
        if not self._has_office_visibility_role:
            return None
        return self._bootstrap_office_building()

    @cached_property
    def _has_office_visibility_role(self) -> bool:
        allowed = {
            MembershipRole.BACKOFFICE,
            MembershipRole.ADMINISTRATOR,
        }
        for membership in self._memberships:
            if membership.role in allowed:
                return True
        return False

    def _bootstrap_office_building(self) -> int | None:
        try:
            from core.services.office import ensure_office_building
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[core.authz] Unable to import Office service: %s", exc)
            return None

        try:
            result = ensure_office_building(strict_owner=False)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[core.authz] Failed to bootstrap Office building: %s", exc)
            return None
        if result and getattr(result.building, "pk", None):
            return result.building.pk
        return Building.system_default_id()

    def visible_building_ids(self) -> Optional[Set[int]]:
        if Capability.VIEW_ALL_BUILDINGS in self._global_capabilities:
            return None
        building_ids = {m.building_id for m in self._memberships if m.building_id}
        if self._office_building_id and self._has_office_visibility_role:
            building_ids.add(self._office_building_id)
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
