from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .authz import log_role_action
from .models import (
    Building,
    BuildingMembership,
    MembershipRole,
    RoleAuditLog,
    UserSecurityProfile,
)


@receiver(post_save, sender=get_user_model())
def ensure_security_profile(sender, instance, created, **kwargs):
    if created:
        UserSecurityProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=Building)
def ensure_owner_membership(sender, instance: Building, created, **kwargs):
    if not instance.owner_id:
        return
    membership, created_membership = BuildingMembership.objects.get_or_create(
        user=instance.owner,
        building=instance,
        role=MembershipRole.BACKOFFICE,
        defaults={"capabilities_override": {}},
    )
    if created_membership:
        log_role_action(
            actor=None,
            target_user=instance.owner,
            building=instance,
            role=MembershipRole.BACKOFFICE,
            action=RoleAuditLog.Action.ROLE_ADDED,
            payload={"reason": "owner_auto_assign"},
        )
