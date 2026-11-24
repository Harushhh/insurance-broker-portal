"""Write side of the Team Activity page: `record_activity`, `audit_change` and the audit-log mirror.

Call `record_activity` at the end of something a person did (a run, an upload, a test, a status change, an edit):
- `summary` is one short sentence with no personal data (no vehicle number, no policy-holder name);
- `details` is where the EXACT parameters go: the values a search was run with, what an edit changed from and to, the
  rows a bulk edit touched. It may hold personal data, and is shown and exported under the same team scoping.
Events are read back through services/team_scope.py.

`audit_change` is for changes that are also audit-log entries: one call writes both. Every other AuditLog row is copied
onto the feed by the post_save receiver in signals.py (`mirror_audit_entry`), so the page holds everything the Security
Audit page showed.

Best effort, by design: recording what someone did must never break what they were doing, so a failure is logged and
swallowed, and the insert runs in its own savepoint so a database error cannot poison the caller's transaction (on
Postgres one failed statement would otherwise abort the whole transaction).
"""
import datetime
import json
import logging
from decimal import Decimal

from django.db import models, transaction
from django.db.models import Count
from django.utils import timezone

from ..models import ActivityEvent, AuditLog

logger = logging.getLogger(__name__)

RUN = ActivityEvent.CATEGORY_RUN
UPLOAD = ActivityEvent.CATEGORY_UPLOAD
TEST = ActivityEvent.CATEGORY_TEST
STATUS = ActivityEvent.CATEGORY_STATUS
EDIT = ActivityEvent.CATEGORY_EDIT

_CATEGORIES = {value for value, _ in ActivityEvent.CATEGORY_CHOICES}

MAX_DETAILS_CHARS = 200_000        # a bigger `details` is replaced by a note: the row must stay a sensible size
MAX_LISTED_IDS = 5000              # callers cap the row ids they list at this
# Audit actions that are not copied onto the feed: the points-search rows already have their own Test events (with
# every parameter), and the sso_ticket_* rows are system events with no person behind them.
MIRROR_SKIP_ACTIONS = {"MOTOR_POINTS_SEARCH", "HEALTH_POINTS_SEARCH"}


def _clip(value, field_name):
    """Cut a string to the column's length so an over-long value can never fail the insert."""
    limit = ActivityEvent._meta.get_field(field_name).max_length
    return str(value or "")[:limit]


# -------------------------
# Retention: whole calendar months
# -------------------------
def retention_cutoff(now=None):
    """The moment events start being kept from: midnight (site time zone) at the start of LAST month.

    So the feed always holds last month in full plus the month so far, and daily, weekly and monthly numbers are never
    cut in half the way a rolling 90 days cut them. Events before this are deleted by the nightly job, and the cutoff
    moves forward on the 1st of each month (on 1 Dec it becomes 1 Nov: October's events go that night).
    """
    local = timezone.localtime(now or timezone.now())
    first_of_this_month = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first_of_this_month - datetime.timedelta(days=1)).replace(day=1)


# -------------------------
# Turning values, forms and rows into plain `details`
# -------------------------
def plain(value):
    """A value as plain JSON-friendly data: a model by its name, a date as ISO text, a Decimal as text, a set as a list."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(item) for item in value]
    if isinstance(value, models.Model):
        return str(value)
    if isinstance(value, models.QuerySet):
        return [plain(item) for item in value]
    return str(value)


def _shown(form, name, value):
    """A form value as a person reads it: a ModelChoiceField's id becomes the name of what it points at."""
    queryset = getattr(form.fields.get(name), "queryset", None)
    if queryset is None or value in (None, "", [], ()):
        return plain(value)
    many = isinstance(value, (list, tuple, set, frozenset, models.QuerySet))
    items = list(value) if many else [value]
    pks = [getattr(item, "pk", item) for item in items]
    names = {str(obj.pk): str(obj) for obj in queryset.filter(pk__in=pks)}
    shown = [names.get(str(pk), str(pk)) for pk in pks]
    return shown if many else shown[0]


def form_changes(form):
    """What a ModelForm changed, as {field: {"from": old, "to": new}}, read from the form's own initial and cleaned
    values (so it is right before or after save, and the new values are the ones the form accepted)."""
    return {
        name: {"from": _shown(form, name, form.initial.get(name)), "to": _shown(form, name, form.cleaned_data.get(name))}
        for name in form.changed_data
    }


def snapshot(instance, exclude=()):
    """Every stored field of a row, as plain data (a ForeignKey as its id), for the details of a create or a delete."""
    return {
        field.name: plain(field.value_from_object(instance))
        for field in instance._meta.concrete_fields if field.name not in exclude
    }


def snapshot_changes(before, after):
    """{field: {"from", "to"}} for the fields that differ between two snapshots."""
    return {
        name: {"from": before.get(name), "to": value}
        for name, value in after.items() if before.get(name) != value
    }


def previous_values(queryset, field_name, limit=50):
    """What the rows of `queryset` hold in `field_name` right now, as {value: number of rows}, the most common first (a
    ForeignKey by the name of what it points at, an empty value as "(empty)"). Called before a bulk update, it is the
    'from' side of it: the rows can differ, so there can be several."""
    field = queryset.model._meta.get_field(field_name)
    counted = list(queryset.order_by().values_list(field_name).annotate(n=Count("id")).order_by("-n")[:limit])
    names = {}
    if field.is_relation and field.related_model is not None:
        wanted = [value for value, _ in counted if value is not None]
        names = {obj.pk: str(obj) for obj in field.related_model.objects.filter(pk__in=wanted)}
    return {("(empty)" if value is None else str(names.get(value, value))): n for value, n in counted}


# -------------------------
# Writing events
# -------------------------
def record_activity(user, category, action, summary, *, target=None, extra=None, details=None, legacy_audit_id=None):
    """Record that `user` did something. Returns the event, or None when nothing was recorded.

    category: one of RUN / UPLOAD / TEST / STATUS / EDIT (the constants above).
    action:   a stable machine code, e.g. "mis.run.finished".
    summary:  one human sentence, without personal data.
    target:   the model instance it was about (stored as a pointer, not a ForeignKey), or None.
    extra:    a small dict (counts, status from -> to); must be JSON-friendly and carry no personal data.
    details:  the exact parameters, a JSON-friendly dict (see the module docstring); may carry personal data.
    legacy_audit_id: the AuditLog row this event mirrors, if any.

    Nothing is recorded for a missing or anonymous user: an event with no
    person cannot be shown to anyone's manager.
    """
    if user is None or getattr(user, "pk", None) is None:
        return None
    try:
        if category not in _CATEGORIES:
            raise ValueError(f"unknown activity category {category!r}")
        # default=str keeps a stray date or Decimal from failing the insert.
        details_json = json.dumps(details or {}, default=str)
        if len(details_json) > MAX_DETAILS_CHARS:
            details_json = json.dumps({"too_large": True, "characters": len(details_json)})
        with transaction.atomic():
            return ActivityEvent.objects.create(
                actor=user,
                actor_username=_clip(user.get_username(), "actor_username"),
                actor_name=_clip(user.get_full_name(), "actor_name"),
                category=category,
                action=_clip(action, "action"),
                summary=_clip(summary, "summary"),
                target_type=_clip(target._meta.model_name if target is not None else "", "target_type"),
                target_id=_clip(target.pk if target is not None else "", "target_id"),
                extra=json.loads(json.dumps(extra or {}, default=str)),
                details=json.loads(details_json),
                legacy_audit_id=legacy_audit_id,
            )
    except Exception:
        # Deliberately swallowed (see the module docstring); logged so it is not silent.
        logger.exception("Could not record activity %r for user id %s", action, user.pk)
        return None


def audit_change(user, action, text, *, category=EDIT, code=None, summary=None, details=None, target=None, extra=None):
    """Write an audit log entry AND its Team Activity event from one call (so the receiver does not copy it again).

    action/text: the AuditLog action and its sentence, as before (the Security Audit page keeps showing them).
    category/code/summary: how the event reads on the feed; by default an Edit event under the audit action's own name
        with the audit sentence as its summary.
    details: the exact parameters; the audit sentence is added to them as "entry".
    """
    entry = AuditLog(user=user if getattr(user, "pk", None) else None, action=action, details=text)
    entry._activity_recorded = True
    entry.save()
    record_activity(
        user, category, code or action, summary or text, target=target, extra=extra,
        details={"entry": text, **(details or {})}, legacy_audit_id=entry.pk,
    )
    return entry


def mirror_audit_entry(entry):
    """Copy a new AuditLog row onto the feed as an Edit event whose details are the entry's text. Called by the
    post_save receiver for every audit entry that `audit_change` did not already record. Never raises."""
    try:
        action = entry.action or ""
        if (entry.user_id is None or getattr(entry, "_activity_recorded", False)
                or action in MIRROR_SKIP_ACTIONS or action.lower().startswith("sso_ticket")):
            return None
        if ActivityEvent.objects.filter(legacy_audit_id=entry.pk).exists():
            return None
        return record_activity(
            entry.user, EDIT, action, entry.details, details={"entry": entry.details}, legacy_audit_id=entry.pk,
        )
    except Exception:
        logger.exception("Could not mirror audit log entry %s onto the activity feed", getattr(entry, "pk", None))
        return None


# -------------------------
# Reading `details` back as rows (the page and the Excel export)
# -------------------------
LISTED_ITEMS = 25                  # a long list shows this many items on the page (the export has them all)
LISTED_ROWS = 200                  # and a very long details shows this many rows


def _label(key):
    text = str(key).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _text(value):
    if value is None or value == "":
        return "(empty)"
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return str(value)


def _walk(value, path, rows, max_items):
    if isinstance(value, dict):
        if set(value) == {"from", "to"}:
            rows.append((" › ".join(path), f"{_text(value['from'])} → {_text(value['to'])}"))
            return
        for key, item in value.items():
            _walk(item, path + [_label(key)], rows, max_items)
    elif isinstance(value, (list, tuple)):
        items = list(value)
        shown = items if max_items is None else items[:max_items]
        more = len(items) - len(shown)
        if all(not isinstance(item, (dict, list, tuple)) for item in shown):
            text = ", ".join(_text(item) for item in shown) if shown else "(none)"
            rows.append((" › ".join(path), text + (f" … and {more} more" if more else "")))
        else:
            for number, item in enumerate(shown, 1):
                _walk(item, path + [str(number)], rows, max_items)
            if more:
                rows.append((" › ".join(path), f"… and {more} more"))
    else:
        rows.append((" › ".join(path), _text(value)))


def detail_rows(details, *, full=False):
    """The exact parameters of an event as flat (label, text) rows. Nested values read 'Outer › Inner'; a
    {"from", "to"} pair reads 'old → new'; a long list shows its first items and how many more there are.
    full=True lists everything (for the Excel export)."""
    if not details:
        return []
    rows = []
    _walk(details, [], rows, None if full else LISTED_ITEMS)
    if not full and len(rows) > LISTED_ROWS:
        rows = rows[:LISTED_ROWS] + [("…", f"{len(rows) - LISTED_ROWS} more rows (all of them are in the Excel export)")]
    return rows
