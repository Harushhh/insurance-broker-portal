from django.db import migrations


# A frozen copy, not an import from insurance/access.py -- migrations must not
# depend on app code that can change later (same reason 0021 and 0053 keep
# their own lists).
#
# Can_View_Team_Activity opens the Team Activity page (/team-activity/). It is
# only the page gate: what a person sees on it is decided by who reports to
# them (services/team_scope.py), and ADMIN members see every team.
#
# Nobody is granted it here; an admin ticks it per person in Access Control.
TEAM_ACTIVITY_GROUPS = ["Can_View_Team_Activity"]


def seed_team_activity_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in TEAM_ACTIVITY_GROUPS:
        Group.objects.get_or_create(name=name)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("insurance", "0060_activity_event"),
    ]

    operations = [
        migrations.RunPython(seed_team_activity_group, noop),
    ]
