"""AI OCR pipeline pages: upload a policy document, the MIS register, and MIS review."""
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Count, Sum
from ..models import ExtractionField, PolicyDocumentUpload, PolicyMISRecord
from ..tasks import process_policy_document_task
from ..forms import PolicyDocumentUploadForm
import logging


logger = logging.getLogger(__name__)


# -------------------------
# Upload & Extract PDF / Image
# -------------------------
def upload_extract_pdf(request):
    msg = ""
    error = ""
    upload_form = PolicyDocumentUploadForm()

    if request.method == "POST":
        upload_form = PolicyDocumentUploadForm(request.POST, request.FILES)
        if upload_form.is_valid():
            uploaded_file = request.FILES['uploaded_file']
            
            if PolicyDocumentUpload.objects.filter(original_filename=uploaded_file.name).exists():
                error = f"⚠️ Duplicate File: A document named '{uploaded_file.name}' has already been processed."
            else:
                try:
                    doc_obj = upload_form.save(commit=False)
                    doc_obj.original_filename = doc_obj.uploaded_file.name
                    doc_obj.uploaded_by = request.user
                    doc_obj.mime_type = getattr(doc_obj.uploaded_file, "content_type", "") or ""
                    doc_obj.status = PolicyDocumentUpload.STATUS_PENDING
                    doc_obj.save()

                    process_policy_document_task.delay(doc_obj.id)
                    return redirect("upload_extract_pdf")

                except Exception as e:
                    logger.exception("Policy document upload failed")
                    error = f"⚠️ {str(e)}"
        else:
            error = "⚠️ Please correct the upload form."

    documents = PolicyDocumentUpload.objects.order_by("-created_at")[:20]
    latest_document = documents[0] if documents else None

    if latest_document and latest_document.status == PolicyDocumentUpload.STATUS_COMPLETED:
        msg = f"✅ Last processed document: {latest_document.original_filename}"

    return render(request, "upload_extract_pdf.html", {
        "upload_form": upload_form,
        "documents": documents,
        "latest_document": latest_document,
        "msg": msg,
        "error": error,
    })


# -------------------------
# My MIS
# -------------------------
def my_mis(request):
    qs = PolicyMISRecord.objects.select_related("source_document").order_by("-created_at")

    policy_number = (request.GET.get("policy_number") or "").strip()
    insured_name = (request.GET.get("insured_name") or "").strip()
    insurer_name = (request.GET.get("insurer_name") or "").strip()

    if policy_number:
        qs = qs.filter(policy_number__icontains=policy_number)
    if insured_name:
        qs = qs.filter(insured_name__icontains=insured_name)
    if insurer_name:
        qs = qs.filter(insurer_name__icontains=insurer_name)

    stats = qs.aggregate(
        total_processed=Count('id'),
        total_premium=Sum('gross_premium')
    )

    return render(request, "my_mis.html", {
        "records": qs[:200],
        "stats": stats,
        "selected": {
            "policy_number": policy_number,
            "insured_name": insured_name,
            "insurer_name": insurer_name,
        }
    })


# -------------------------
# MIS REVIEW
# -------------------------
def mis_review(request, pk):
    record = get_object_or_404(PolicyMISRecord.objects.select_related('source_document'), pk=pk)
    
    rules = ExtractionField.objects.filter(is_active=True).order_by('category', 'order_index')
    
    if request.method == 'POST':
        updated_json = record.raw_ai_json or {}
        
        for rule in rules:
            field_key = rule.field_name.strip().lower().replace(" ", "_")
            new_val = request.POST.get(field_key)
            if new_val is not None:
                updated_json[field_key] = new_val
                
        record.raw_ai_json = updated_json
        
        record.policy_number = updated_json.get("policy_number", record.policy_number)
        record.insurer_name = updated_json.get("insurance_company", record.insurer_name)
        record.insured_name = updated_json.get("insured_name", record.insured_name)
        record.save()
        
        return redirect('my_mis')

    field_data = []
    parsed = record.raw_ai_json or {}
    
    for rule in rules:
        field_key = rule.field_name.strip().lower().replace(" ", "_")
        val = parsed.get(field_key, "")
        
        is_missing_mandatory = rule.is_mandatory and not val
        
        dropdown_choices = []
        if getattr(rule, 'has_dropdown', False) and getattr(rule, 'dropdown_options', None):
            dropdown_choices = [x.strip() for x in rule.dropdown_options.split(',') if x.strip()]
            
        field_data.append({
            'rule': rule,
            'key': field_key,
            'value': val,
            'is_missing_mandatory': is_missing_mandatory,
            'dropdown_choices': dropdown_choices
        })
        
    return render(request, 'mis_review.html', {
        'record': record,
        'field_data': field_data
    })
