from datetime import timedelta

from django.db import migrations
from django.utils import timezone

# Frozen copies, not imports from app code (migrations must not depend on code that can change later).
# The points-search rows have their own Test events and the sso_ticket_* rows are system events with no person.
SKIP_ACTIONS = ["MOTOR_POINTS_SEARCH", "HEALTH_POINTS_SEARCH"]


def copy_audit_log_into_team_activity(apps, schema_editor):
    """Give the Team Activity page the audit log entries that already exist (the Security Audit page keeps 7 days of
    them), so that page can be retired without losing them. Only rows inside the retention window (from the 1st of
    last month) are copied, and only once each (legacy_audit_id is unique), so running this again is harmless."""
    AuditLog = apps.get_model("insurance", "AuditLog")
    ActivityEvent = apps.get_model("insurance", "ActivityEvent")

    local_now = timezone.localtime(timezone.now())
    first_of_this_month = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cutoff = (first_of_this_month - timedelta(days=1)).replace(day=1)

    already = set(ActivityEvent.objects.exclude(legacy_audit_id=None).values_list("legacy_audit_id", flat=True))
    rows = (
        AuditLog.objects.filter(timestamp__gte=cutoff, user__isnull=False)
        .exclude(action__in=SKIP_ACTIONS).exclude(action__istartswith="sso_ticket")
        .select_related("user").order_by("id")
    )
    batch = []
    for row in rows.iterator(chunk_size=2000):
        if row.pk in already:
            continue
        user = row.user
        batch.append(ActivityEvent(
            occurred_at=row.timestamp,
            actor=user,
            actor_username=(user.username or "")[:150],
            actor_name=f"{user.first_name} {user.last_name}".strip()[:150],
            category="EDIT",
            action=(row.action or "")[:60],
            summary=(row.details or "")[:300],
            details={"entry": row.details},
            legacy_audit_id=row.pk,
        ))
        if len(batch) >= 1000:
            ActivityEvent.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        ActivityEvent.objects.bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("insurance", "0062_activity_event_details"),
    ]

    operations = [
        migrations.RunPython(copy_audit_log_into_team_activity, migrations.RunPython.noop),
    ]
