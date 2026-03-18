from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction

from core.models import Building, BuildingMembership, MembershipRole, Unit
from core.utils.ownership import owner_capability_overrides

logger = logging.getLogger(__name__)


class OfficeOwnerResolutionError(Exception):
    """Raised when an Office owner cannot be resolved automatically."""


@dataclass
class OfficeSyncResult:
    building: Building
    created: bool
    updated_fields: set[str]
    removed_units: int
    owner_memberships_synced: int
    admin_memberships_synced: int
    backoffice_memberships_synced: int


def ensure_office_building(
    *,
    owner=None,
    owner_username: Optional[str] = None,
    owner_email: Optional[str] = None,
    name: Optional[str] = None,
    address: Optional[str] = None,
    description: Optional[str] = None,
    strict_owner: bool = True,
) -> OfficeSyncResult | None:
    """Ensure the singleton Office building exists and memberships stay in sync."""

    owner_obj = _resolve_owner(owner, owner_username, owner_email)
    if not owner_obj:
        if strict_owner:
            raise OfficeOwnerResolutionError(
                "Unable to resolve an administrator owner. Provide --owner/--owner-email or set DJANGO_OFFICE_OWNER_USERNAME."
            )
        return None

    office_name = _resolve_value(name, "DJANGO_OFFICE_BUILDING_NAME", "Office")
    office_address = _resolve_value(address, "DJANGO_OFFICE_BUILDING_ADDRESS", "")
    office_description = _resolve_value(
        description, "DJANGO_OFFICE_BUILDING_DESCRIPTION", "Office workspace"
    )

    with transaction.atomic():
        building, created, updated_fields = _ensure_building(
            owner_obj,
            office_name,
            office_address,
            office_description,
        )
        removed_units = _purge_units(building)
        owner_memberships = _ensure_owner_memberships(building)
        admin_memberships = _ensure_role_memberships(building, MembershipRole.ADMINISTRATOR)
        backoffice_memberships = _ensure_role_memberships(building, MembershipRole.BACKOFFICE)

    Building.clear_system_default_cache()
    return OfficeSyncResult(
        building=building,
        created=created,
        updated_fields=updated_fields,
        removed_units=removed_units,
        owner_memberships_synced=owner_memberships,
        admin_memberships_synced=admin_memberships,
        backoffice_memberships_synced=backoffice_memberships,
    )


def _resolve_value(value: Optional[str], env_key: str, default: str) -> str:
    if value is not None and value.strip():
        return value.strip()
    env_value = os.environ.get(env_key, "")
    if env_value.strip():
        return env_value.strip()
    return default


def _resolve_owner(owner, owner_username: Optional[str], owner_email: Optional[str]):
    if owner and getattr(owner, "pk", None):
        return owner

    User = get_user_model()
    username = (owner_username or os.environ.get("DJANGO_OFFICE_OWNER_USERNAME", "")).strip()
    email_field = getattr(User, "EMAIL_FIELD", None)
    email = (owner_email or os.environ.get("DJANGO_OFFICE_OWNER_EMAIL", "")).strip()

    if username:
        lookup = {User.USERNAME_FIELD: username}
        try:
            return User.objects.get(**lookup)
        except User.DoesNotExist:
            raise OfficeOwnerResolutionError(
                f"User with {User.USERNAME_FIELD}='{username}' not found."
            )

    if email and email_field:
        try:
            return User.objects.get(**{email_field: email})
        except User.DoesNotExist:
            raise OfficeOwnerResolutionError(f"User with {email_field}='{email}' not found.")

    for qs_kwargs in ("is_superuser", "is_staff", None):
        filters = {}
        if qs_kwargs:
            filters[qs_kwargs] = True
        candidate = User.objects.filter(**filters).order_by("id").first()
        if candidate:
            return candidate

    return None


def _ensure_building(owner, name: str, address: str, description: str):
    building = Building.objects.filter(is_system_default=True).order_by("id").first()
    created = False
    updated_fields: set[str] = set()

    if not building:
        building = Building.objects.create(
            owner=owner,
            name=name,
            address=address,
            description=description,
            role=Building.Role.TECH_SUPPORT,
            is_system_default=True,
        )
        created = True
    else:
        if building.owner_id != owner.id:
            building.owner = owner
            updated_fields.add("owner")
        if building.name != name:
            building.name = name
            updated_fields.add("name")
        if building.address != address:
            building.address = address
            updated_fields.add("address")
        if building.description != description:
            building.description = description
            updated_fields.add("description")
        if not building.is_system_default:
            building.is_system_default = True
            updated_fields.add("is_system_default")
        if updated_fields:
            building.save(update_fields=list(updated_fields) + ["updated_at"])

    Building.objects.filter(is_system_default=True).exclude(pk=building.pk).update(is_system_default=False)
    return building, created, updated_fields


def _purge_units(building) -> int:
    qs = Unit.objects.filter(building=building)
    count = qs.count()
    if count:
        qs.delete()
    return count


def _ensure_owner_memberships(building) -> int:
    User = get_user_model()
    admin_ids = set(User.objects.filter(is_superuser=True).values_list("id", flat=True))
    global_admin_ids = set(
        BuildingMembership.objects.filter(building__isnull=True, role=MembershipRole.ADMINISTRATOR)
        .values_list("user_id", flat=True)
    )
    target_ids = sorted(admin_ids | global_admin_ids)
    synced = 0
    for user_id in target_ids:
        defaults = {
            "capabilities_override": owner_capability_overrides({})[0],
            "technician_subrole": building.role or "",
        }
        try:
            membership, created = BuildingMembership.objects.get_or_create(
                user_id=user_id,
                building=building,
                role=MembershipRole.TECHNICIAN,
                defaults=defaults,
            )
        except ValidationError as exc:  # user already has another global role
            logger.debug(
                "[core.office] Skipping technician membership for user %s: %s",
                user_id,
                exc,
            )
            continue
        overrides, changed = owner_capability_overrides(membership.capabilities_override)
        updates = []
        if changed:
            membership.capabilities_override = overrides
            updates.append("capabilities_override")
        desired_subrole = building.role or ""
        if membership.technician_subrole != desired_subrole:
            membership.technician_subrole = desired_subrole
            updates.append("technician_subrole")
        if updates:
            membership.save(update_fields=updates + ["updated_at"])
        if created or updates:
            synced += 1
    return synced


def _ensure_role_memberships(building, role: str) -> int:
    global_memberships = BuildingMembership.objects.filter(
        building__isnull=True,
        role=role,
    ).values_list("user_id", flat=True)
    synced = 0
    for user_id in global_memberships:
        try:
            _, created = BuildingMembership.objects.get_or_create(
                user_id=user_id,
                building=building,
                role=role,
            )
        except ValidationError as exc:
            logger.debug(
                "[core.office] Skipping %s membership for user %s: %s",
                role,
                user_id,
                exc,
            )
            continue
        if created:
            synced += 1
    return synced
