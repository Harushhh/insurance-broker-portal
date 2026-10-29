"""MIS payout automation: upload MIS files, track / cancel / download mapping runs, edit the mapping configuration."""
from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
import json
import logging
import mimetypes
from ..models import MISFile, MappingConfiguration
from ..forms import MISUploadForm, MappingConfigurationForm
from ..tasks import process_mis_mapping_task
from ..services.mis_payout import get_rule_schema_context


logger = logging.getLogger("security")


# =========================================================
# AUTOMATED MIS PAYOUT CALCULATION VIEWS
# =========================================================

def mis_payout_automation(request):
    msg = ""
    error = ""
    if request.method == 'POST':
        form = MISUploadForm(request.POST, request.FILES)
        if form.is_valid():
            mis_obj = form.save(commit=False)
            mis_obj.uploaded_by = request.user
            mis_obj.save()

            # Handed off to Celery so the UI doesn't block while Pandas does the heavy lifting.
            # Task id is saved so a later Cancel action can revoke this exact job.
            async_result = process_mis_mapping_task.delay(mis_obj.id)
            mis_obj.celery_task_id = async_result.id
            mis_obj.save(update_fields=['celery_task_id'])

            msg = "File uploaded successfully. The mapping engine has started processing in the background."
            return redirect('mis_payout_automation')
        else:
            error = "Please upload a valid CSV or Excel file."
    else:
        form = MISUploadForm()
    
    files = MISFile.objects.all().order_by('-created_at')[:50]
    
    return render(request, 'insurance/mis_payout_automation.html', {
        'form': form,
        'files': files,
        'msg': msg,
        'error': error
    })


def download_processed_mis(request, file_id):
    mis_obj = get_object_or_404(MISFile, id=file_id)
    if not mis_obj.processed_file:
        return HttpResponse("Processed file is not available yet. Please check back when status is COMPLETED.", status=404)

    try:
        content_type, _ = mimetypes.guess_type(mis_obj.processed_file.name)
        response = HttpResponse(mis_obj.processed_file, content_type=content_type or 'application/octet-stream')
        response['Content-Disposition'] = f'attachment; filename="{mis_obj.processed_file.name.split("/")[-1]}"'
        return response
    except Exception:
        # The DB row can outlive the actual object in storage (deleted/moved
        # out from under it, a storage outage, etc.) - surface a clean
        # message instead of a raw 500 from whatever the storage backend
        # raised trying to open it.
        logger.exception("Could not read processed MIS file %s (id=%s) from storage", mis_obj.processed_file.name, mis_obj.id)
        return HttpResponse("The processed file could not be retrieved from storage. Please contact support.", status=404)


def cancel_mis_processing(request, file_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Invalid request."})

    mis_obj = get_object_or_404(MISFile, id=file_id)

    if mis_obj.status not in ('PENDING', 'PROCESSING'):
        return JsonResponse({
            "success": False,
            "message": f"File #{file_id} is already {mis_obj.status.title()} — nothing to cancel."
        })

    # Best-effort OS-level termination — not the source of truth for the UI.
    # A missing task id or an unreachable broker must never block the DB
    # update below, which is what the dashboard actually reflects.
    if mis_obj.celery_task_id:
        try:
            from project.celery import app as celery_app
            celery_app.control.revoke(mis_obj.celery_task_id, terminate=True, signal='SIGTERM')
        except Exception:
            logger.exception("Failed to revoke Celery task %s for MISFile %s", mis_obj.celery_task_id, file_id)

    # Conditional update guards the race where the task completes/fails right
    # as Cancel is clicked — only flip status if it's still PENDING/PROCESSING,
    # never overwrite a real COMPLETED/FAILED result.
    updated = MISFile.objects.filter(id=file_id, status__in=['PENDING', 'PROCESSING']).update(
        status='CANCELLED',
        processed_at=timezone.now(),
        error_message=f"Cancelled by {request.user.username}.",
    )

    if not updated:
        mis_obj.refresh_from_db()
        return JsonResponse({
            "success": False,
            "message": f"File #{file_id} finished as {mis_obj.status.title()} before it could be cancelled."
        })

    return JsonResponse({"success": True, "message": f"File #{file_id} cancelled."})


def mis_mapping_dashboard(request):
    mappings = MappingConfiguration.objects.all()
    return render(request, 'insurance/mis_mapping_dashboard.html', {'mappings': mappings})


def add_mis_mapping(request):
    if request.method == 'POST':
        form = MappingConfigurationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('mis_mapping_dashboard')
    else:
        form = MappingConfigurationForm()
        
    return render(request, 'insurance/mis_mapping_form.html', {
        'form': form, 
        'title': 'Add Multi-Table Mapping Rule',
        'schema_json': json.dumps(get_rule_schema_context())
    })


def edit_mis_mapping(request, pk):
    mapping = get_object_or_404(MappingConfiguration, pk=pk)
    if request.method == 'POST':
        form = MappingConfigurationForm(request.POST, instance=mapping)
        if form.is_valid():
            form.save()
            return redirect('mis_mapping_dashboard')
    else:
        form = MappingConfigurationForm(instance=mapping)
        
    return render(request, 'insurance/mis_mapping_form.html', {
        'form': form, 
        'title': 'Edit Multi-Table Mapping Rule',
        'schema_json': json.dumps(get_rule_schema_context())
    })


def delete_mis_mapping(request, pk):
    mapping = get_object_or_404(MappingConfiguration, pk=pk)
    mapping.delete()
    return redirect('mis_mapping_dashboard')
