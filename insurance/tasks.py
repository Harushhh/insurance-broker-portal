from celery import shared_task


# Keep in sync with POINTS_SEARCH_ACTIONS in insurance/views.py - the set of
# action types shown on the unified Points Search Logs page.
POINTS_SEARCH_ACTIONS = ["MOTOR_POINTS_SEARCH", "HEALTH_POINTS_SEARCH"]


@shared_task
def cleanup_points_search_logs():
    """Delete Motor/Health Points Search audit logs older than the 7-day retention window."""
    from datetime import timedelta
    from django.utils import timezone
    from .models import AuditLog

    cutoff = timezone.now() - timedelta(days=7)
    deleted_count, _ = AuditLog.objects.filter(
        action__in=POINTS_SEARCH_ACTIONS, timestamp__lt=cutoff
    ).delete()
    return deleted_count


# Keep in sync with SECURITY_AUDIT_LOG_ACTIONS in insurance/views.py - the
# set of action types shown on the Security Audit & History Log page.
SECURITY_AUDIT_LOG_ACTIONS = ["MANUAL EDIT", "BULK UPDATE", "HEALTH RATE EDIT", "HEALTH BULK UPDATE"]


@shared_task
def cleanup_security_audit_logs():
    """Delete Security Audit Log entries older than the 7-day retention window."""
    from datetime import timedelta
    from django.utils import timezone
    from .models import AuditLog

    cutoff = timezone.now() - timedelta(days=7)
    deleted_count, _ = AuditLog.objects.filter(
        action__in=SECURITY_AUDIT_LOG_ACTIONS, timestamp__lt=cutoff
    ).delete()
    return deleted_count


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=600,
    time_limit=660,
)
def process_mis_mapping_task(self, mis_file_id):
    # Deferred import: keeps this module free of any import-time dependency
    # on mapping_engine beyond what's actually needed to run the task.
    from .mapping_engine import process_mis_mapping
    process_mis_mapping(mis_file_id)


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=180,
    time_limit=240,
)
def process_policy_document_task(self, document_id):
    # Deferred import: views.py imports this module at module level to call
    # .delay(), so importing views.process_policy_document up here at module
    # level would create a circular import. Importing inside the task body
    # instead breaks the cycle without needing to relocate that function.
    from .models import PolicyDocumentUpload
    from .views import process_policy_document
    document_obj = PolicyDocumentUpload.objects.get(id=document_id)
    process_policy_document(document_obj)
