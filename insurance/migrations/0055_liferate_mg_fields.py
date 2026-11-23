from django.db import migrations, models


class Migration(migrations.Migration):
    """The Final MG (what the insurer pays the broker) beside each stored BG payout of a Life row. Blank for the
    uploads made before this migration (the workbook file is not kept, so upload it again to fill them in)."""

    dependencies = [
        ("insurance", "0054_remove_liferate_category_group"),
    ]

    operations = [
        migrations.AddField(model_name="liferate", name="mg_yr1", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="liferate", name="mg_deferred", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="liferate", name="mg_yr2", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="liferate", name="mg_yr3", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="liferate", name="mg_yr4", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="liferate", name="mg_yr5", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="liferate", name="mg_yr6_plus", field=models.FloatField(blank=True, null=True)),
    ]
