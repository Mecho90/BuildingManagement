from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_budgetrequest_title"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
