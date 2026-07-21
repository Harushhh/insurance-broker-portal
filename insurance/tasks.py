from celery import shared_task


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
