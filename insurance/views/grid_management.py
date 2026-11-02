"""Grid Management: upload insurer grid files (saved to storage by a Celery task) and track their status."""
from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
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

# Which date the month filter (and the month dropdown) is scoped to, picked
# with the toggle beside it via the `month_basis` query param. "upload" is the
# default so links that predate the toggle (?month=2026-10) mean what they
# always did.
MONTH_BASIS_CHOICES = (
    ("upload", "Upload Month"),
    ("work_effected", "Work Effected Date Month"),
)
MONTH_BASIS_FIELDS = {"upload": "uploaded_date", "work_effected": "work_effected_date"}
DEFAULT_MONTH_BASIS = "upload"


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
            # The row's status form has no action=, so it posts to the page's
            # own URL and request.GET still holds the filters (month,
            # month_basis, status card): go back to that same view of the list.
            # urlencode() keeps a blank month=, which means "All Months" --
            # dropping it would fall back to the current month. The upload
            # redirect below deliberately lands on the defaults instead, so the
            # grid just uploaded is on screen.
            query = request.GET.urlencode()
            url = reverse("grid_management")
            return redirect(f"{url}?{query}" if query else url)

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

    # Month-year filter, scoped to the date column `month_basis` picks:
    # `uploaded_date` (the "Date & Time" column) or `work_effected_date`.
    # No "month" param at all means a fresh page load (e.g. the sidebar link),
    # which defaults to the current month; an explicit empty value is the
    # "All Months" option clearing the filter. An unrecognized basis falls
    # back to the default, like an unparseable month does.
    month_basis = request.GET.get("month_basis")
    if month_basis not in MONTH_BASIS_FIELDS:
        month_basis = DEFAULT_MONTH_BASIS
    month_field = MONTH_BASIS_FIELDS[month_basis]

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

    # Months that have a grid under this basis (a grid with no work effected
    # date has no month there), plus the current month and the selected one --
    # the latter so the dropdown can always show what is being filtered, even
    # when flipping the toggle leaves that month with no rows.
    available_months = set(GridDocument.objects.dates(month_field, "month"))
    available_months.add(current_month)
    if selected_month:
        available_months.add(selected_month)
    available_months = sorted(available_months, reverse=True)

    base_qs = GridDocument.objects.all()
    if selected_month:
        base_qs = base_qs.filter(**{
            f"{month_field}__year": selected_month.year,
            f"{month_field}__month": selected_month.month,
        })

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
        "month_basis_choices": MONTH_BASIS_CHOICES,
        "selected_month_basis": month_basis,
        "selected_month_basis_label": dict(MONTH_BASIS_CHOICES)[month_basis],
        "available_months": available_months,
        "selected_month_str": selected_month.strftime("%Y-%m") if selected_month else "",
        "selected_month_label": selected_month.strftime("%B %Y") if selected_month else "",
    })
