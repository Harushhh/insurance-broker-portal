from django.db import migrations


# Frozen copies, not imports from insurance/access.py -- migrations must not
# depend on app code that can change later (same reason 0021 and 0044 keep
# their own lists).
#
# Can_View_Life_Payout_Grid is an old name: it opened the Life Payout Grid
# service until that was switched off on 2026-10-11, and no migration ever
# created it (user_management() made it on first visit). It now opens the Life
# Rate Checker, so it is created here for a fresh database. Can_View_Life_Rate_Master
# is new: it opens the page that uploads the Life workbook and edits its rows.
#
# Nobody is granted either group here. Whoever held the old Life group (if
# anyone still does) keeps it, and ADMIN members get both for free via
# page_access_required / sidebar_access.
LIFE_GROUPS = ["Can_View_Life_Payout_Grid", "Can_View_Life_Rate_Master"]


def seed_life_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in LIFE_GROUPS:
        Group.objects.get_or_create(name=name)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("insurance", "0052_lifegridupload_liferate_lifeinsurernote"),
    ]

    operations = [
        migrations.RunPython(seed_life_groups, noop),
    ]
