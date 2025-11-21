from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_fill_technician_subrole"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "ALTER TABLE core_workorder ALTER COLUMN kind SET DEFAULT 'MAINTENANCE';",
                    "ALTER TABLE core_workorder ALTER COLUMN kind DROP DEFAULT;",
                ),
                migrations.RunSQL(
                    "UPDATE core_workorder SET kind = 'MAINTENANCE' WHERE kind IS NULL;",
                    migrations.RunSQL.noop,
                ),
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
