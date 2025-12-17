from django.db import migrations


OWNER_CAPABILITIES = {"mass_assign", "approve_work_orders", "manage_memberships"}


def _ensure_overrides(override):
    override = override or {}
    add = set(override.get("add") or [])
    changed = False
    for capability in OWNER_CAPABILITIES:
        if capability not in add:
            add.add(capability)
            changed = True
    override["add"] = sorted(add)
    remove = override.get("remove") or []
    override["remove"] = list(dict.fromkeys(remove))
    return override, changed


def assign_owner_memberships(apps, schema_editor):
    Building = apps.get_model("core", "Building")
    BuildingMembership = apps.get_model("core", "BuildingMembership")

    for building in Building.objects.select_related("owner").all():
        owner = getattr(building, "owner", None)
        if not owner:
            continue
        desired_subrole = building.role or ""
        membership = BuildingMembership.objects.filter(
            user=owner,
            building=building,
            role="TECHNICIAN",
        ).first()
        if membership:
            overrides, changed = _ensure_overrides(membership.capabilities_override)
            fields = []
            if membership.technician_subrole != desired_subrole:
                membership.technician_subrole = desired_subrole
                fields.append("technician_subrole")
            if changed:
                membership.capabilities_override = overrides
                fields.append("capabilities_override")
            if fields:
                membership.save(update_fields=fields)
            continue

        legacy = BuildingMembership.objects.filter(
            user=owner,
            building=building,
            role="BACKOFFICE",
        ).first()
        overrides, _ = _ensure_overrides(getattr(legacy, "capabilities_override", None))
        if legacy:
            legacy.role = "TECHNICIAN"
            legacy.technician_subrole = desired_subrole
            legacy.capabilities_override = overrides
            legacy.save(update_fields=["role", "technician_subrole", "capabilities_override"])
            continue

        BuildingMembership.objects.create(
            user=owner,
            building=building,
            role="TECHNICIAN",
            technician_subrole=desired_subrole,
            capabilities_override=overrides,
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_alter_buildingmembership_role_and_more"),
    ]

    operations = [
        migrations.RunPython(assign_owner_memberships, noop),
    ]
