from django.db import migrations

def backfill_workorder_building(apps, schema_editor):
    WorkOrder = apps.get_model("core", "WorkOrder")
    Unit = apps.get_model("core", "Unit")
    Building = apps.get_model("core", "Building")

    # pick a fallback
    fallback = Building.objects.order_by("id").first()

    # Iterate safely using historical models
    for wo in WorkOrder.objects.filter(building__isnull=True).iterator():
        if wo.unit_id:
            # resolve unit's building id without touching current models
            unit = Unit.objects.only("building_id").get(pk=wo.unit_id)
            wo.building_id = unit.building_id
        elif fallback:
            wo.building_id = fallback.id
        wo.save(update_fields=["building"])
        
class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_workorder_archived_at_alter_workorder_building_and_more'),
    ]


    operations = [
        migrations.RunPython(backfill_workorder_building, migrations.RunPython.noop),
    ]
