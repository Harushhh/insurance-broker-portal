"""Write side of the Team Activity page: `record_activity`.

Call it at the end of something a person did (a run, an upload, a test, a status
change) with a short sentence that carries no personal data (no vehicle number,
no policy-holder name, no PAN or bank detail: counts, file names and statuses
only). Events are read back through services/team_scope.py.

Best effort, by design: recording what someone did must never break what they
were doing, so a failure is logged and swallowed, and the insert runs in its own
savepoint so a database error cannot poison the caller's transaction (on
Postgres one failed statement would otherwise abort the whole transaction).
"""
import json
import logging

from django.db import transaction

from ..models import ActivityEvent

logger = logging.getLogger(__name__)

RUN = ActivityEvent.CATEGORY_RUN
UPLOAD = ActivityEvent.CATEGORY_UPLOAD
TEST = ActivityEvent.CATEGORY_TEST
STATUS = ActivityEvent.CATEGORY_STATUS
EDIT = ActivityEvent.CATEGORY_EDIT

_CATEGORIES = {value for value, _ in ActivityEvent.CATEGORY_CHOICES}


def _clip(value, field_name):
    """Cut a string to the column's length so an over-long value can never fail the insert."""
    limit = ActivityEvent._meta.get_field(field_name).max_length
    return str(value or "")[:limit]


def record_activity(user, category, action, summary, *, target=None, extra=None):
    """Record that `user` did something. Returns the event, or None when nothing was recorded.

    category: one of RUN / UPLOAD / TEST / STATUS / EDIT (the constants above).
    action:   a stable machine code, e.g. "mis.run.finished".
    summary:  one human sentence, without personal data.
    target:   the model instance it was about (stored as a pointer, not a ForeignKey), or None.
    extra:    a small dict (counts, status from -> to); must be JSON-friendly and carry no personal data.

    Nothing is recorded for a missing or anonymous user: an event with no
    person cannot be shown to anyone's manager.
    """
    if user is None or getattr(user, "pk", None) is None:
        return None
    try:
        if category not in _CATEGORIES:
            raise ValueError(f"unknown activity category {category!r}")
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
                # default=str keeps a stray date or Decimal from failing the insert.
                extra=json.loads(json.dumps(extra or {}, default=str)),
            )
    except Exception:
        # Deliberately swallowed (see the module docstring); logged so it is not silent.
        logger.exception("Could not record activity %r for user id %s", action, user.pk)
        return None
