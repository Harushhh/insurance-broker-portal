"""Grid Management: upload insurer grid files (saved to storage by a Celery task) and track their status."""
from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Count
import base64
import threading
from datetime import datetime
from ..constants import GRID_INSURER_CHOICES
from ..models import GridDocument
from ..tasks import save_grid_document_file_task
from ..services.email import send_email_background


# -------------------------
# GRID MANAGEMENT
# -------------------------

def grid_management(request):
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "update_status":
            doc_id = request.POST.get("doc_id")
            new_status = request.POST.get("status")
            if doc_id and new_status:
                doc = get_object_or_404(GridDocument, id=doc_id)
                doc.status = new_status
                doc.save()
            return redirect("grid_management")

        insurer_name = request.POST.get("insurer_name")
        remarks = request.POST.get("remarks")
        if remarks:
            remarks_words = remarks.split()
            if len(remarks_words) > 250:
                remarks = " ".join(remarks_words[:250])
        work_effected_date = request.POST.get("work_effected_date") or None
        uploaded_file = request.FILES.get("uploaded_file")

        if insurer_name and uploaded_file:
            file_bytes = uploaded_file.read()
            file_name = uploaded_file.name

            doc = GridDocument.objects.create(
                insurer_name=insurer_name,
                remarks=remarks,
                work_effected_date=work_effected_date,
                uploaded_file="",
                uploaded_by=request.user,
                status="PENDING"
            )
            # Writing uploaded_file to storage (Cloudflare R2) here used to
            # block this request until the write finished -- when R2 was slow
            # to respond, that pinned a gunicorn worker for up to the full
            # request timeout, and with only a couple of worker/thread slots
            # (see Procfile) that alone was enough to starve every other page
            # on the site. Deferring the actual storage write to Celery keeps
            # a slow/stuck upload from taking down unrelated requests.
            save_grid_document_file_task.delay(
                doc.id, file_name, base64.b64encode(file_bytes).decode("ascii")
            )

            uploader_name = "System User"
            subject = f"🔔 New Grid Uploaded: {insurer_name}"
            message = (
                f"Hello,\n\n"
                f"A new provider grid has just been uploaded to the Insurance Portal.\n\n"
                f"• Insurer: {insurer_name}\n"
                f"• Uploaded By: {uploader_name}\n"
                f"• Work Effected Date: {work_effected_date or 'Not specified.'}\n"
                f"• Remarks: {remarks or 'No remarks provided.'}\n\n"
                f"Please log in to the portal to download it."
            )

            email_thread = threading.Thread(
                target=send_email_background,
                args=(subject, message, ["harsh.t@arhamsecure.com"])
            )
            email_thread.start()

            return redirect("grid_management")

    # Month-year upload filter, scoped to `uploaded_date` (the "Date & Time"
    # column). No "month" param at all means a fresh page load (e.g. the
    # sidebar link), which defaults to the current month; an explicit empty
    # value is the "All Months" option clearing the filter.
    current_month = timezone.localdate().replace(day=1)
    month_param = request.GET.get("month")
    if month_param is None:
        selected_month = current_month
    elif month_param == "":
        selected_month = None
    else:
        try:
            selected_month = datetime.strptime(month_param, "%Y-%m").date().replace(day=1)
        except ValueError:
            selected_month = current_month

    available_months = list(GridDocument.objects.dates("uploaded_date", "month", order="DESC"))
    if current_month not in available_months:
        available_months.insert(0, current_month)
        available_months.sort(reverse=True)

    base_qs = GridDocument.objects.all()
    if selected_month:
        base_qs = base_qs.filter(
            uploaded_date__year=selected_month.year, uploaded_date__month=selected_month.month
        )

    total_all = base_qs.count()
    status_counts = dict(
        base_qs.values("status").annotate(n=Count("id")).values_list("status", "n")
    )

    # "all" (or anything unrecognized) clears the filter, matching the Clear
    # Filters convention used elsewhere — only a real STATUS_CHOICES code
    # narrows the queryset and highlights its card.
    status_filter = (request.GET.get("status") or "").strip()
    valid_status_codes = {code for code, _ in GridDocument.STATUS_CHOICES}
    selected_status = status_filter if status_filter in valid_status_codes else ""

    documents = base_qs.order_by("-uploaded_date")
    if selected_status:
        documents = documents.filter(status=selected_status)

    return render(request, "grid_management.html", {
        "documents": documents,
        "status_choices": GridDocument.STATUS_CHOICES,
        "status_counts": status_counts,
        "total_all": total_all,
        "selected_status": selected_status,
        "insurer_choices": GRID_INSURER_CHOICES,
        "available_months": available_months,
        "selected_month_str": selected_month.strftime("%Y-%m") if selected_month else "",
        "selected_month_label": selected_month.strftime("%B %Y") if selected_month else "",
    })
