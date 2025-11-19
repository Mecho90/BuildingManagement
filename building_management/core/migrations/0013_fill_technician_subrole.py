from django.db import migrations


def set_subroles(apps, schema_editor):
    BuildingMembership = apps.get_model("core", "BuildingMembership")
    for membership in BuildingMembership.objects.filter(role="TECHNICIAN", technician_subrole=""):
        building = getattr(membership, "building", None)
        subrole = getattr(building, "role", "") if building else ""
        membership.technician_subrole = subrole or "TECH_SUPPORT"
        membership.save(update_fields=["technician_subrole"])


def reverse(apps, schema_editor):
    BuildingMembership = apps.get_model("core", "BuildingMembership")
    BuildingMembership.objects.filter(role="TECHNICIAN").update(technician_subrole="")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0012_add_technician_subrole"),
    ]

    operations = [
        migrations.RunPython(set_subroles, reverse),
    ]
