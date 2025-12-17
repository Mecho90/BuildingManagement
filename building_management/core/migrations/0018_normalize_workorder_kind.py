from django.db import migrations


def normalize_workorder_kind(apps, schema_editor):
    WorkOrder = apps.get_model("core", "WorkOrder")
    valid_kinds = {"MAINTENANCE", "MASS_ASSIGN"}
    WorkOrder.objects.exclude(kind__in=valid_kinds).update(kind="MAINTENANCE")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_backoffice_memberships"),
    ]

    operations = [
        migrations.RunPython(normalize_workorder_kind, migrations.RunPython.noop),
    ]
