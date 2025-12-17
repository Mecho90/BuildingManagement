from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_workorder_rejected_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workorder",
            name="status",
            field=models.CharField(
                choices=[
                    ("OPEN", "Open"),
                    ("IN_PROGRESS", "In progress"),
                    ("AWAITING_APPROVAL", "Awaiting approval"),
                    ("APPROVED", "Approved"),
                    ("REJECTED", "Rejected"),
                    ("DONE", "Done"),
                ],
                db_index=True,
                default="OPEN",
                max_length=20,
            ),
        ),
    ]
