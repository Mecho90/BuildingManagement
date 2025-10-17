from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_alter_workorder_options_remove_workorder_updated_at_and_more'),  # placeholder; we will compute real dependency below
    ]

    operations = [
        migrations.AddField(
            model_name='workorder',
            name='priority',
            field=models.CharField(choices=[('high', 'High'), ('medium', 'Medium'), ('low', 'Low')], db_index=True, default='medium', max_length=10),
        ),
    ]
