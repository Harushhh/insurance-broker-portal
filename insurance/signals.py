"""Signal receivers for the insurance app (connected in apps.py)."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AuditLog
from .services.activity import mirror_audit_entry


@receiver(post_save, sender=AuditLog, dispatch_uid="mirror_audit_log_into_team_activity")
def mirror_new_audit_log_entry(sender, instance, created, **kwargs):
    """Every new audit log entry also appears on the Team Activity page, with its full text as the details. Best effort:
    mirror_audit_entry never raises, so a failure here cannot break the write that was being audited."""
    if created:
        mirror_audit_entry(instance)
