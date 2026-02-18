from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Building, BuildingMembership, MembershipRole, Unit
from core.utils.ownership import owner_capability_overrides


class Command(BaseCommand):
    help = "Ensure the default Office building, owner, and memberships stay in sync."

    def add_arguments(self, parser):
        parser.add_argument(
            "--owner",
            dest="owner_username",
            help="Username of the canonical Office owner (falls back to env DJANGO_OFFICE_OWNER_USERNAME).",
        )
        parser.add_argument(
            "--owner-email",
            dest="owner_email",
            help="Email of the canonical Office owner (falls back to env DJANGO_OFFICE_OWNER_EMAIL).",
        )
        parser.add_argument(
            "--name",
            dest="name",
            help="Override the Office building name (falls back to env DJANGO_OFFICE_BUILDING_NAME).",
        )
        parser.add_argument("--address", dest="address", help="Optional Office address override.")
        parser.add_argument(
            "--description",
            dest="description",
            help="Optional Office description override.",
        )

    def handle(self, *args, **options):
        owner = self._resolve_owner(options)
        if not owner:
            raise CommandError(
                "Unable to resolve an administrator owner. Provide --owner/--owner-email or "
                "set DJANGO_OFFICE_OWNER_USERNAME."
            )

        office_name = self._resolve_name(options)
        office_address = self._resolve_setting(options, "address", "DJANGO_OFFICE_BUILDING_ADDRESS", "")
        office_description = self._resolve_setting(
            options, "description", "DJANGO_OFFICE_BUILDING_DESCRIPTION", "Office workspace"
        )

        with transaction.atomic():
            building, created, updated_fields = self._ensure_building(
                owner,
                office_name,
                office_address,
                office_description,
            )
            removed_units = self._purge_units(building)
            owner_memberships = self._ensure_owner_memberships(building)
            admin_memberships = self._ensure_role_memberships(building, MembershipRole.ADMINISTRATOR)
            backoffice_memberships = self._ensure_role_memberships(building, MembershipRole.BACKOFFICE)
        Building.clear_system_default_cache()

        status_bits = []
        if created:
            status_bits.append("created Office building")
        if updated_fields:
            status_bits.append(f"updated fields: {', '.join(sorted(updated_fields))}")
        if removed_units:
            status_bits.append(f"removed {removed_units} units")
        status_bits.append(f"owner memberships synced: {owner_memberships}")
        status_bits.append(f"administrator memberships synced: {admin_memberships}")
        status_bits.append(f"backoffice memberships synced: {backoffice_memberships}")
        self.stdout.write(
            self.style.SUCCESS(
                "Office building synchronized (" + "; ".join(status_bits) + ")"
            )
        )

    # ------------------------------------------------------------------ helpers

    def _resolve_setting(self, options, option_key: str, env_key: str, default: str) -> str:
        if options.get(option_key):
            return options[option_key]
        env_value = os.environ.get(env_key)
        if env_value is not None:
            return env_value
        return default

    def _resolve_name(self, options) -> str:
        return (options.get("name") or os.environ.get("DJANGO_OFFICE_BUILDING_NAME") or "Office").strip() or "Office"

    def _resolve_owner(self, options):
        User = get_user_model()
        username = options.get("owner_username") or os.environ.get("DJANGO_OFFICE_OWNER_USERNAME") or ""
        email = options.get("owner_email") or os.environ.get("DJANGO_OFFICE_OWNER_EMAIL") or ""
        username = username.strip()
        email = email.strip()

        if username:
            lookup = {User.USERNAME_FIELD: username}
            try:
                return User.objects.get(**lookup)
            except User.DoesNotExist:
                raise CommandError(f"User with {User.USERNAME_FIELD}='{username}' not found.")

        email_field = getattr(User, "EMAIL_FIELD", None)
        if email and email_field:
            try:
                return User.objects.get(**{email_field: email})
            except User.DoesNotExist:
                raise CommandError(f"User with {email_field}='{email}' not found.")

        for qs_kwargs in (
            {"is_superuser": True},
            {"is_staff": True},
            {},
        ):
            candidate = User.objects.filter(**qs_kwargs).order_by("id").first()
            if candidate:
                return candidate
        return None

    def _ensure_building(self, owner, name: str, address: str, description: str):
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

    def _purge_units(self, building) -> int:
        qs = Unit.objects.filter(building=building)
        count = qs.count()
        if count:
            qs.delete()
        return count

    def _ensure_owner_memberships(self, building) -> int:
        User = get_user_model()
        admin_ids = set(
            User.objects.filter(is_superuser=True).values_list("id", flat=True)
        )
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
            membership, created = BuildingMembership.objects.get_or_create(
                user_id=user_id,
                building=building,
                role=MembershipRole.TECHNICIAN,
                defaults=defaults,
            )
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

    def _ensure_role_memberships(self, building, role: str) -> int:
        global_memberships = BuildingMembership.objects.filter(
            building__isnull=True,
            role=role,
        ).values_list("user_id", flat=True)
        synced = 0
        for user_id in global_memberships:
            _, created = BuildingMembership.objects.get_or_create(
                user_id=user_id,
                building=building,
                role=role,
            )
            if created:
                synced += 1
        return synced
