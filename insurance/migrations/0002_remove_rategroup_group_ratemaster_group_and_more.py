from django.db import migrations, models
import uuid


def fill_mpg_id(apps, schema_editor):
    RateMaster = apps.get_model("insurance", "RateMaster")

    # Fill mpg_id for all existing rows that are NULL/empty
    for row in RateMaster.objects.filter(mpg_id__isnull=True):
        row.mpg_id = uuid.uuid4()
        row.save(update_fields=["mpg_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("insurance", "0001_initial"),
    ]

    operations = [
        # ✅ Add group field in RateMaster
        migrations.AddField(
            model_name="ratemaster",
            name="group",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="rates",
                to="insurance.rategroup",
            ),
        ),

        # ✅ Step-1: add mpg_id as NULL (NOT unique yet)
        migrations.AddField(
            model_name="ratemaster",
            name="mpg_id",
            field=models.UUIDField(null=True, blank=True, editable=False),
        ),

        # ✅ Step-2: fill unique UUID for old rows
        migrations.RunPython(fill_mpg_id, reverse_code=migrations.RunPython.noop),

        # ✅ Step-3: now make it unique + default uuid for new rows
        migrations.AlterField(
            model_name="ratemaster",
            name="mpg_id",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                unique=True,
            ),
        ),
    ]
