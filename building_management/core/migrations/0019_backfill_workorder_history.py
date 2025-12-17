from django.db import migrations
from django.utils import timezone


def backfill_created_logs(apps, schema_editor):
    WorkOrder = apps.get_model("core", "WorkOrder")
    WorkOrderAuditLog = apps.get_model("core", "WorkOrderAuditLog")

    work_orders = WorkOrder.objects.select_related("building").all().iterator()
    for order in work_orders:
        has_created = WorkOrderAuditLog.objects.filter(
            work_order=order,
            action="created",
        ).exists()
        if has_created:
            continue
        log = WorkOrderAuditLog.objects.create(
            actor=None,
            work_order=order,
            building=order.building,
            action="created",
            payload={"status": order.status, "priority": order.priority},
        )
        WorkOrderAuditLog.objects.filter(pk=log.pk).update(
            created_at=order.created_at or timezone.now(),
            updated_at=order.created_at or timezone.now(),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_normalize_workorder_kind"),
    ]

    operations = [
        migrations.RunPython(backfill_created_logs, migrations.RunPython.noop),
    ]
