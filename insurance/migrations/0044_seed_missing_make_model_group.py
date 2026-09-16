from django.db import migrations


# Frozen copies, not imports from insurance/views.py — migrations must not
# depend on app code that can change later (same reason
# 0021_seed_page_access_groups keeps its own PAGE_GROUPS list).
NEW_GROUP = "Can_View_Missing_Make_Model"

# The Missing Make/Model page is worked by whoever already handles the MIS
# failure queue or maintains the make/model master, so it is granted alongside
# those two groups rather than to every active user. 0021 granted its groups to
# everyone only because it was retro-fitting enforcement onto pages that were
# already open to all — that reasoning doesn't apply to a brand-new page, and
# this one carries a write path into MakeModelMaster data. ADMIN members get it
# for free via page_access_required/sidebar_access either way; everyone else
# gets it from Access Control.
IMPLYING_GROUPS = ["Can_View_Rate_Master_Health", "Can_View_Make_Model_Dashboard"]


def seed_missing_make_model_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")

    group, _ = Group.objects.get_or_create(name=NEW_GROUP)
    for user in User.objects.filter(is_active=True, groups__name__in=IMPLYING_GROUPS).distinct():
        user.groups.add(group)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("insurance", "0043_supportticket_ticket_type"),
    ]

    operations = [
        migrations.RunPython(seed_missing_make_model_group, noop),
    ]
