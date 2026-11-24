import ast
import json
from datetime import timedelta

from django.db import migrations
from django.db.models import Min
from django.utils import timezone

# Frozen copies, not imports from app code (migrations must not depend on code that can change later).
SYSTEM_USERNAME = "(system)"
# audit action -> (event action, the sentence, the page name): what services/points_audit.py writes when it cannot tell
# which page or API endpoint a search came from (which is the case for every row copied here).
POINTS_ACTIONS = {
    "MOTOR_POINTS_SEARCH": ("motor.eligibility.checked", "Ran a Motor eligibility check", "Motor eligibility check"),
    "HEALTH_POINTS_SEARCH": ("health.eligibility.checked", "Ran a Health eligibility check", "Health eligibility check"),
}
CHECK_ACTIONS = ["motor.eligibility.checked", "policy_lock.checked", "health.eligibility.checked"]
# The searches' ids of Product, Sub Product, Class and Fuel are shown by name, as the Points Logs page shows them.
ID_FIELDS = {
    "product": "ProductMaster", "sub_product": "SubProductMaster",
    "make_model_class": "MakeModelClassMaster", "fuel": "FuelTypeMaster",
}


def _parameters(text, names):
    """The search values stored in an audit row's text (a Python dict written with str()), or None if unreadable."""
    try:
        data = ast.literal_eval((text or "").replace("Eligibility Check Parameters: ", ""))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    shown = {str(key): (value[0] if isinstance(value, list) and value else value) for key, value in data.items()}
    for key, model_name in ID_FIELDS.items():
        value = shown.get(key)
        if value and str(value).isdigit():
            shown[key] = names[model_name].get(str(int(value)), shown[key])
    return json.loads(json.dumps(shown, default=str))


def copy_system_and_points_logs_into_team_activity(apps, schema_editor):
    """Put on the Team Activity feed what the Points Logs page shows, and the audit entries nobody signed in wrote.

    - Every points-search row with no user (an API-key call) becomes a Test event filed under System. Before this there
      was no event for them at all, so they were missing from the feed.
    - Every other audit entry with no user (written by a management command) becomes an Edit event under System.
    - A signed-in person's points-search rows become Test events too, but only those from before the hook that gives
      such a search its own event shipped (the earliest existing Test event of that kind): later ones already have one.
    None of them knows which URL it came from, so source_path stays empty ("not recorded" on the page).

    Only rows inside the retention window (from the 1st of last month) are copied, and only once each
    (legacy_audit_id is unique), so running this again is harmless.
    """
    AuditLog = apps.get_model("insurance", "AuditLog")
    ActivityEvent = apps.get_model("insurance", "ActivityEvent")
    names = {
        model_name: {str(pk): name for pk, name in apps.get_model("insurance", model_name).objects.values_list("id", "name")}
        for model_name in set(ID_FIELDS.values())
    }

    local_now = timezone.localtime(timezone.now())
    first_of_this_month = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cutoff = (first_of_this_month - timedelta(days=1)).replace(day=1)

    already = set(ActivityEvent.objects.exclude(legacy_audit_id=None).values_list("legacy_audit_id", flat=True))
    first_hook = ActivityEvent.objects.filter(category="TEST", action__in=CHECK_ACTIONS).aggregate(
        first=Min("occurred_at"))["first"]

    rows = (
        AuditLog.objects.filter(timestamp__gte=cutoff).exclude(action__istartswith="sso_ticket")
        .select_related("user").order_by("id")
    )
    batch = []
    for row in rows.iterator(chunk_size=2000):
        if row.pk in already:
            continue
        points = row.action in POINTS_ACTIONS
        if row.user_id is not None and not points:
            continue                      # an entry with a person: migration 0063 copied it
        if points and row.user_id is not None and first_hook is not None and row.timestamp >= first_hook:
            continue                      # a signed-in person's search from after the hook: it has its own event
        if points:
            action, summary, page = POINTS_ACTIONS[row.action]
            parameters = _parameters(row.details, names)
            details = {"page": page, "parameters": parameters} if parameters is not None else {"entry": row.details}
            category = "TEST"
        else:
            action, summary, details, category = row.action or "", row.details or "", {"entry": row.details}, "EDIT"
        user = row.user
        batch.append(ActivityEvent(
            occurred_at=row.timestamp,
            actor=user,
            actor_username=(user.username or "")[:150] if user else SYSTEM_USERNAME,
            actor_name=f"{user.first_name} {user.last_name}".strip()[:150] if user else "System",
            category=category,
            action=action[:60],
            summary=summary[:300],
            details=details,
            legacy_audit_id=row.pk,
            source_path="",
        ))
        if len(batch) >= 1000:
            ActivityEvent.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        ActivityEvent.objects.bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("insurance", "0064_activity_event_source_path"),
    ]

    operations = [
        migrations.RunPython(copy_system_and_points_logs_into_team_activity, migrations.RunPython.noop),
    ]
