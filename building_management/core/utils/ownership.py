from __future__ import annotations

from core.models import Capability

OWNER_CAPABILITY_SLUGS = {
    Capability.MASS_ASSIGN,
    Capability.APPROVE_WORK_ORDERS,
    Capability.MANAGE_MEMBERSHIPS,
}


def owner_capability_overrides(current_override=None):
    override = current_override or {}
    add = set(override.get("add") or [])
    updated = False
    for capability in OWNER_CAPABILITY_SLUGS:
        if capability not in add:
            add.add(capability)
            updated = True
    override["add"] = sorted(add)
    remove = override.get("remove") or []
    override["remove"] = list(dict.fromkeys(remove))
    return override, updated
