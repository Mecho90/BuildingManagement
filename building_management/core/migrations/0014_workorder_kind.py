from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_fill_technician_subrole"),
    ]

    operations = [
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
    ]
