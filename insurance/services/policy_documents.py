"""Policy-document OCR processing: Gemini extraction into PolicyMISRecord rows (run by `process_policy_document_task`)."""
from django.utils import timezone
import os
import re
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from ..models import PolicyDocumentUpload, PolicyMISRecord
from ..utils import extract_data_with_gemini
import logging


logger = logging.getLogger(__name__)


# =========================================================
# AI EXTRACTION HELPERS
# =========================================================
def safe_decimal(value):
    if value in [None, ""]:
        return None
    cleaned = str(value).strip().replace(",", "")
    cleaned = re.sub(r"[^\d.\-]", "", cleaned)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def safe_date_multi(value):
    if not value:
        return None
    value = str(value).strip()
    formats = [
        "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
        "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except Exception:
            pass
    return None


def process_policy_document(document_obj):
    try:
        document_obj.status = PolicyDocumentUpload.STATUS_PROCESSING
        document_obj.error_message = ""
        document_obj.save(update_fields=["status", "error_message"])

        # .path isn't available on non-filesystem storage backends, and the
        # Gemini SDK wants a real local path — so pull the file down to a
        # temp file first rather than handing it a storage-backed stream.
        suffix = os.path.splitext(document_obj.uploaded_file.name)[1]
        with document_obj.uploaded_file.open("rb") as src, \
                tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(src.read())
            tmp_path = tmp.name
        try:
            mapped_data = extract_data_with_gemini(tmp_path)
        finally:
            os.remove(tmp_path)

        document_obj.extracted_text = "Extracted directly via Gemini Multimodal API."
        document_obj.extraction_method = "Gemini 2.5 Flash"
        document_obj.parsed_json = mapped_data
        document_obj.status = PolicyDocumentUpload.STATUS_COMPLETED
        document_obj.processed_at = timezone.now()
        document_obj.save()

        PolicyMISRecord.objects.update_or_create(
            source_document=document_obj,
            defaults={
                "insurer_name": mapped_data.get("insurance_company") or mapped_data.get("insurer_name"),
                "policy_number": mapped_data.get("policy_number"),
                "insured_name": mapped_data.get("insured_name"),
                "vehicle_registration_number": mapped_data.get("vehicle_registration_number"),
                "vehicle_make": mapped_data.get("vehicle_make"),
                "vehicle_model": mapped_data.get("vehicle_model"),
                "vehicle_make_model": mapped_data.get("vehicle_make_model"),
                "engine_number": mapped_data.get("engine_number"),
                "chassis_number": mapped_data.get("chassis_number"),
                "fuel_type": mapped_data.get("fuel_type"),
                "cubic_capacity_cc": mapped_data.get("cubic_capacity_cc"),
                "gross_premium": safe_decimal(mapped_data.get("gross_premium")),
                "net_premium": safe_decimal(mapped_data.get("net_premium")),
                "tax_amount": safe_decimal(mapped_data.get("tax_amount")),
                "policy_start_date": safe_date_multi(mapped_data.get("policy_start_date")),
                "policy_end_date": safe_date_multi(mapped_data.get("policy_end_date")),
                "issue_date": safe_date_multi(mapped_data.get("issue_date")),
                "rto_location": mapped_data.get("rto_location"),
                "raw_ai_json": mapped_data,
                "confidence_notes": "Extracted via Gemini 2.5 Flash with Synonyms injection.",
                "ai_model_name": "gemini-2.5-flash",
            }
        )

    except Exception as e:
        logger.exception("AI extraction failed for policy document %s", document_obj.pk)
        document_obj.status = PolicyDocumentUpload.STATUS_FAILED
        document_obj.error_message = str(e)
        document_obj.processed_at = timezone.now()
        document_obj.save(update_fields=["status", "error_message", "processed_at"])
