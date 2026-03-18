from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.services.office import (
    OfficeOwnerResolutionError,
    ensure_office_building,
)


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
        try:
            result = ensure_office_building(
                owner_username=options.get("owner_username"),
                owner_email=options.get("owner_email"),
                name=options.get("name"),
                address=options.get("address"),
                description=options.get("description"),
            )
        except OfficeOwnerResolutionError as exc:
            raise CommandError(str(exc)) from exc

        status_bits = []
        if result.created:
            status_bits.append("created Office building")
        if result.updated_fields:
            status_bits.append(f"updated fields: {', '.join(sorted(result.updated_fields))}")
        if result.removed_units:
            status_bits.append(f"removed {result.removed_units} units")
        status_bits.append(f"owner memberships synced: {result.owner_memberships_synced}")
        status_bits.append(f"administrator memberships synced: {result.admin_memberships_synced}")
        status_bits.append(f"backoffice memberships synced: {result.backoffice_memberships_synced}")
        self.stdout.write(
            self.style.SUCCESS(
                "Office building synchronized (" + "; ".join(status_bits) + ")"
            )
        )
