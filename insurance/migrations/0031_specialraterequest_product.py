# Generated manually to add the Product classification (Motor/Life/Health/
# Non-Motor) used to route Special Rate Requests to the right approvers.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('insurance', '0030_add_misfile_cancel_support'),
    ]

    operations = [
        migrations.AddField(
            model_name='specialraterequest',
            name='product',
            field=models.CharField(
                choices=[('MOTOR', 'Motor'), ('LIFE', 'Life'), ('HEALTH', 'Health'), ('NON_MOTOR', 'Non-Motor')],
                db_index=True,
                default='MOTOR',
                max_length=20,
            ),
        ),
    ]
