from django.conf import settings
from django.db import migrations


ADMIN_ROLE = "ADMINISTRATOR"
BACKOFFICE_ROLE = "BACKOFFICE"
ROLE_ADDED = "role_added"


def seed_memberships(apps, schema_editor):
    Building = apps.get_model("core", "Building")
    BuildingMembership = apps.get_model("core", "BuildingMembership")
    RoleAuditLog = apps.get_model("core", "RoleAuditLog")
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)

    staff_qs = User.objects.filter(is_staff=True)
    for user in staff_qs.iterator():
        membership, created = BuildingMembership.objects.get_or_create(
            user=user,
            building=None,
            role=ADMIN_ROLE,
            defaults={"capabilities_override": {}},
        )
        if created:
            RoleAuditLog.objects.create(
                actor=None,
                target_user=user,
                building=None,
                role=ADMIN_ROLE,
                action=ROLE_ADDED,
                payload={"reason": "seed_admin_membership"},
            )

    for building in Building.objects.select_related("owner").iterator():
        if not building.owner_id:
            continue
        membership, created = BuildingMembership.objects.get_or_create(
            user=building.owner,
            building=building,
            role=BACKOFFICE_ROLE,
            defaults={"capabilities_override": {}},
        )
        if created:
            RoleAuditLog.objects.create(
                actor=None,
                target_user=building.owner,
                building=building,
                role=BACKOFFICE_ROLE,
                action=ROLE_ADDED,
                payload={"reason": "seed_building_owner_membership"},
            )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_roleauditlog_buildingmembership"),
    ]

    operations = [
        migrations.RunPython(seed_memberships, migrations.RunPython.noop),
    ]
