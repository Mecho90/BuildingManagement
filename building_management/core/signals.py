from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .authz import log_role_action
from .models import (
    Building,
    BuildingMembership,
    Capability,
    MembershipRole,
    RoleAuditLog,
    UserSecurityProfile,
)

OWNER_OVERRIDE_CAPS = {
    Capability.MASS_ASSIGN,
    Capability.APPROVE_WORK_ORDERS,
    Capability.MANAGE_MEMBERSHIPS,
}


def _owner_capability_overrides(current_override=None):
    override = current_override or {}
    add = set(override.get("add") or [])
    updated = False
    for capability in OWNER_OVERRIDE_CAPS:
        if capability not in add:
            add.add(capability)
            updated = True
    override["add"] = sorted(add)
    remove = override.get("remove") or []
    override["remove"] = list(dict.fromkeys(remove))
    return override, updated


def _assign_backoffice_membership(user, building):
    if not user or not building:
        return
    membership, created = BuildingMembership.objects.get_or_create(
        user=user,
        building=building,
        role=MembershipRole.BACKOFFICE,
    )
    if created:
        log_role_action(
            actor=None,
            target_user=user,
            building=building,
            role=MembershipRole.BACKOFFICE,
            action=RoleAuditLog.Action.ROLE_ADDED,
            payload={"reason": "backoffice_auto_assign"},
        )


def ensure_backoffice_memberships_for_user(user):
    if not user or not getattr(user, "pk", None):
        return
    for building in Building.objects.all():
        _assign_backoffice_membership(user, building)


def ensure_backoffice_memberships_for_building(building):
    if not building or not getattr(building, "pk", None):
        return
    backoffice_memberships = BuildingMembership.objects.filter(
        building__isnull=True,
        role=MembershipRole.BACKOFFICE,
    ).select_related("user")
    for membership in backoffice_memberships:
        _assign_backoffice_membership(membership.user, building)


@receiver(post_save, sender=get_user_model())
def ensure_security_profile(sender, instance, created, **kwargs):
    if created:
        UserSecurityProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=Building)
def ensure_owner_membership(sender, instance: Building, created, **kwargs):
    if not instance.owner_id:
        return
    desired_role = MembershipRole.TECHNICIAN
    cap_overrides, _ = _owner_capability_overrides({})
    defaults = {
        "capabilities_override": cap_overrides,
        "technician_subrole": instance.role or "",
    }
    membership, created_membership = BuildingMembership.objects.get_or_create(
        user=instance.owner,
        building=instance,
        role=desired_role,
        defaults=defaults,
    )
    updated_fields = []
    desired_subrole = instance.role or ""
    overrides, overrides_changed = _owner_capability_overrides(membership.capabilities_override)
    if overrides_changed:
        membership.capabilities_override = overrides
        updated_fields.append("capabilities_override")
    if membership.technician_subrole != desired_subrole:
        membership.technician_subrole = desired_subrole
        updated_fields.append("technician_subrole")
    if updated_fields:
        membership.save(update_fields=updated_fields + ["updated_at"])
        log_role_action(
            actor=None,
            target_user=instance.owner,
            building=instance,
            role=desired_role,
            action=RoleAuditLog.Action.CAPABILITY_UPDATED,
            payload={"technician_subrole": desired_subrole},
        )
    elif created_membership:
        log_role_action(
            actor=None,
            target_user=instance.owner,
            building=instance,
            role=desired_role,
            action=RoleAuditLog.Action.ROLE_ADDED,
            payload={"reason": "owner_auto_assign"},
        )
    ensure_backoffice_memberships_for_building(instance)


@receiver(post_save, sender=BuildingMembership)
def ensure_backoffice_membership_entries(sender, instance: BuildingMembership, created, **kwargs):
    if not created:
        return
    if instance.role != MembershipRole.BACKOFFICE:
        return
    if instance.building_id is not None:
        return
    ensure_backoffice_memberships_for_user(instance.user)


@receiver(post_delete, sender=BuildingMembership)
def cleanup_backoffice_memberships(sender, instance: BuildingMembership, **kwargs):
    if instance.role != MembershipRole.BACKOFFICE:
        return
    if instance.building_id is not None:
        return
    BuildingMembership.objects.filter(
        user=instance.user,
        role=MembershipRole.BACKOFFICE,
        building__isnull=False,
    ).delete()
