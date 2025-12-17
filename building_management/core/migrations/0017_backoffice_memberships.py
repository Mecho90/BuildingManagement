from django.db import migrations


def assign_backoffice_memberships(apps, schema_editor):
    Building = apps.get_model("core", "Building")
    BuildingMembership = apps.get_model("core", "BuildingMembership")

    buildings = list(Building.objects.all())
    if not buildings:
        return

    backoffice_memberships = BuildingMembership.objects.filter(
        building__isnull=True,
        role="BACKOFFICE",
    ).select_related("user")

    for membership in backoffice_memberships:
        user = membership.user
        if not getattr(user, "pk", None):
            continue
        for building in buildings:
            BuildingMembership.objects.get_or_create(
                user=user,
                building=building,
                role="BACKOFFICE",
            )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_owner_membership_subroles"),
    ]

    operations = [
        migrations.RunPython(assign_backoffice_memberships, migrations.RunPython.noop),
    ]
