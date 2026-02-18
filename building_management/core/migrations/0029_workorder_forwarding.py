from __future__ import annotations

from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def backfill_forwarding_events(apps, schema_editor):
    WorkOrder = apps.get_model("core", "WorkOrder")
    WorkOrderForwarding = apps.get_model("core", "WorkOrderForwarding")
    Building = apps.get_model("core", "Building")

    office = Building.objects.filter(is_system_default=True).first()
    if not office:
        return

    entries = []
    now = timezone.now()
    for order in WorkOrder.objects.filter(building=office).only(
        "id", "building_id", "forwarded_to_building_id", "forwarded_by_id", "forwarded_at", "forward_note", "created_at"
    ):
        forwarded_at = order.forwarded_at or order.created_at or now
        entries.append(
            WorkOrderForwarding(
                work_order_id=order.id,
                from_building_id=office.id,
                to_building_id=getattr(order, "forwarded_to_building_id", None),
                forwarded_by_id=getattr(order, "forwarded_by_id", None),
                forwarded_at=forwarded_at,
                note=getattr(order, "forward_note", "") or "",
            )
        )
    if entries:
        WorkOrderForwarding.objects.bulk_create(entries, batch_size=500)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_office_building"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="workorder",
            name="forward_note",
            field=models.TextField(blank=True, default="", verbose_name="Forwarding note"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="workorder",
            name="forwarded_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Forwarded at"),
        ),
        migrations.AddField(
            model_name="workorder",
            name="forwarded_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name="work_orders_forwarded", to=settings.AUTH_USER_MODEL, verbose_name="Forwarded by"),
        ),
        migrations.AddField(
            model_name="workorder",
            name="forwarded_to_building",
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name="forwarded_work_orders", to="core.building", verbose_name="Forwarded to building"),
        ),
        migrations.AddIndex(
            model_name="workorder",
            index=models.Index(fields=("forwarded_to_building", "archived_at"), name="core_wo_forwarded_archived_idx"),
        ),
        migrations.CreateModel(
            name="WorkOrderForwarding",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("forwarded_at", models.DateTimeField(default=timezone.now)),
                ("note", models.TextField(blank=True)),
                ("from_building", models.ForeignKey(on_delete=models.CASCADE, related_name="forwarding_origins", to="core.building")),
                ("to_building", models.ForeignKey(blank=True, null=True, on_delete=models.CASCADE, related_name="forwarding_targets", to="core.building")),
                ("forwarded_by", models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name="forwarding_events", to=settings.AUTH_USER_MODEL)),
                ("work_order", models.ForeignKey(on_delete=models.CASCADE, related_name="forwarding_history", to="core.workorder")),
            ],
            options={
                "verbose_name": "Work order forwarding event",
                "verbose_name_plural": "Work order forwarding events",
                "ordering": ("-forwarded_at", "-id"),
            },
        ),
        migrations.AddIndex(
            model_name="workorderforwarding",
            index=models.Index(fields=("to_building", "forwarded_at"), name="core_forwarding_target_idx"),
        ),
        migrations.RunPython(backfill_forwarding_events, noop),
    ]
