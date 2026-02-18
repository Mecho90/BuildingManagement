from __future__ import annotations

import os
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


def _resolve_owner_user(apps):
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    username_hint = os.environ.get("DJANGO_OFFICE_OWNER_USERNAME", "").strip()
    email_hint = os.environ.get("DJANGO_OFFICE_OWNER_EMAIL", "").strip()

    if username_hint:
        lookup = {User.USERNAME_FIELD: username_hint}
        try:
            return User.objects.get(**lookup)
        except User.DoesNotExist:
            pass

    email_field = getattr(User, "EMAIL_FIELD", None)
    if email_hint and email_field:
        try:
            return User.objects.get(**{email_field: email_hint})
        except User.DoesNotExist:
            pass

    for qs_kwargs in (
        {"is_superuser": True},
        {"is_staff": True},
        {},
    ):
        try:
            candidate = User.objects.filter(**qs_kwargs).order_by("id").first()
        except Exception:
            candidate = None
        if candidate:
            return candidate
    return None


def create_office_building(apps, schema_editor):
    Building = apps.get_model("core", "Building")
    Unit = apps.get_model("core", "Unit")
    owner = _resolve_owner_user(apps)
    if not owner:
        return

    office_name = os.environ.get("DJANGO_OFFICE_BUILDING_NAME", "Office").strip() or "Office"
    building = Building.objects.filter(is_system_default=True).order_by("id").first()
    if building:
        updates = {}
        if building.name != office_name:
            updates["name"] = office_name
        if building.owner_id != owner.id:
            updates["owner_id"] = owner.id
        if updates:
            for field, value in updates.items():
                setattr(building, field, value)
            building.save(update_fields=list(updates.keys()))
    else:
        building = Building.objects.filter(name=office_name).order_by("id").first()
        if building:
            updates = {"is_system_default": True}
            if building.owner_id != owner.id:
                updates["owner_id"] = owner.id
            for field, value in updates.items():
                setattr(building, field, value)
            building.save(update_fields=list(updates.keys()))
        else:
            building = Building.objects.create(
                owner=owner,
                name=office_name,
                address=os.environ.get("DJANGO_OFFICE_BUILDING_ADDRESS", ""),
                description=os.environ.get("DJANGO_OFFICE_BUILDING_DESCRIPTION", "Office workspace"),
                role="TECH_SUPPORT",
                is_system_default=True,
            )

    Unit.objects.filter(building=building).delete()
    Building.objects.filter(is_system_default=True).exclude(pk=building.pk).update(is_system_default=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("core", "0027_todoweeksnapshot"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="building",
            name="is_system_default",
            field=models.BooleanField(default=False, help_text="Marks the singleton Office building that remains unit-less.", db_index=True),
        ),
        migrations.AddConstraint(
            model_name="building",
            constraint=models.UniqueConstraint(
                condition=Q(is_system_default=True),
                fields=("is_system_default",),
                name="unique_system_default_building",
            ),
        ),
        migrations.RunPython(create_office_building, noop, atomic=False),
    ]
