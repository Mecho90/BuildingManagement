from django.db import migrations, models


def set_kind_defaults(apps, schema_editor):
    schema_editor.execute("UPDATE core_workorder SET kind = 'MAINTENANCE' WHERE kind IS NULL;")
    if schema_editor.connection.vendor != "sqlite":
        schema_editor.execute("ALTER TABLE core_workorder ALTER COLUMN kind SET DEFAULT 'MAINTENANCE';")


def unset_kind_defaults(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        schema_editor.execute("ALTER TABLE core_workorder ALTER COLUMN kind DROP DEFAULT;")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_fill_technician_subrole"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(set_kind_defaults, unset_kind_defaults),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="workorder",
                    name="kind",
                    field=models.CharField(
                        choices=[("MAINTENANCE", "Maintenance"), ("MASS_ASSIGN", "Mass assignment")],
                        db_index=True,
                        default="MAINTENANCE",
                        max_length=32,
                    ),
                ),
            ],
        ),
    ]
