import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('insurance', '0047_makemodelmaster_timestamps'),
    ]

    operations = [
        migrations.AddField(
            model_name='rtomaster',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='rtomaster',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
    ]
