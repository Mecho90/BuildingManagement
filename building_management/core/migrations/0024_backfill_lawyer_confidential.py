from django.db import migrations


def mark_lawyer_orders(apps, schema_editor):
    WorkOrder = apps.get_model("core", "WorkOrder")
    WorkOrderAuditLog = apps.get_model("core", "WorkOrderAuditLog")
    BuildingMembership = apps.get_model("core", "BuildingMembership")

    lawyer_user_ids = list(
        BuildingMembership.objects.filter(role="LAWYER").values_list("user_id", flat=True)
    )
    if not lawyer_user_ids:
        return

    created_order_ids = list(
        WorkOrderAuditLog.objects.filter(
            action="created",
            actor_id__in=lawyer_user_ids,
        ).values_list("work_order_id", flat=True)
    )
    if not created_order_ids:
        return

    WorkOrder.objects.filter(
        pk__in=created_order_ids,
        lawyer_only=False,
    ).update(lawyer_only=True)


def reverse_mark_lawyer_orders(apps, schema_editor):
    # Do nothing on reverse; we don't want to automatically clear the flag.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_workorder_lawyer_only_alter_buildingmembership_role_and_more"),
    ]

    operations = [
        migrations.RunPython(mark_lawyer_orders, reverse_mark_lawyer_orders),
    ]
