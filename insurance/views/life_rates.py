"""Life payout rates: the Life Rate Checker (search only), and the Life Rate Master, where the stored payout rows, the
insurer notes and the uploads (the standard file and the old workbook, with the review of what an upload changes) are
viewed and edited."""
import io
import re
from datetime import date

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_POST

from ..forms import LifeBulkUpdateForm, LifeInsurerNoteForm, LifeRateAddForm, LifeRateForm
from ..models import AuditLog, LifeGridUpload, LifeInsurerNote, LifeRate
from ..services.activity import STATUS, TEST, UPLOAD, record_activity
from ..services.life_filters import CATEGORIES, PAYMENT_MODES, PPT_CHOICES, PT_CHOICES
from ..services.life_dashboard import COLUMN_HELP, dashboard_data, dashboard_workbook
from ..services.life_rates import (
    LIFE_MAX_RESULTS, MASTER_DEFAULT_PAGE_SIZE, MASTER_PAGE_SIZES, PAYOUT_HEADERS, activate_life_upload,
    active_life_upload, add_life_rate, bulk_audit_details, bulk_field_choices, bulk_field_kind, clean_master_filters,
    default_label, delete_life_rate, delete_life_upload, describe_bulk, discard_life_upload, life_export_workbook,
    life_filter_rows,
    life_notes_for, life_rate_snapshot, master_filter_options, master_rows, master_stats, plan_bulk_update,
    save_bulk_changes, save_life_upload, search_life_rates, selected_life_filters, selected_master_filters,
)
from ..services.life_review import KIND_ORDER, insurer_key, review_upload
from ..services.life_standard import LifeFileError, read_standard_file, standard_template_workbook
from ..services.life_workbook import LifeLayoutError, read_life_workbook

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
PROBLEMS_SHOWN = 40                  # problems of a refused standard file listed on the page (the session keeps no more)
REVIEW_PAGE_SIZE = 50
LEFT_OUT_SHOWN = 200                 # plans listed on the Dashboard tab as left out / restated (the sheet itself has all)
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# -------------------------
# LIFE RATE CHECKER (read-only: nothing on this page edits a rate)
# -------------------------
def life_payout_rates(request):
    """Search the active Life upload by insurer, category, product, payment mode, PPT and PT; best first-year payout first."""
    upload = active_life_upload()
    selected = selected_life_filters(request.GET)
    has_searched = bool(request.GET)

    results, total_found, notes, columns = [], 0, {}, []
    filter_rows, insurer_list = [], []
    if upload is not None:
        filter_rows = life_filter_rows(upload)
        insurer_list = sorted({row[0] for row in filter_rows})
        if has_searched:
            results, total_found = search_life_rates(upload, selected)
            shown = list(dict.fromkeys(r.insurer for r in results))
            notes = life_notes_for(upload, shown)
            notes = {insurer: notes[insurer] for insurer in shown if insurer in notes}
            columns = [(attr, header) for attr, header in PAYOUT_HEADERS
                       if any(getattr(r, attr) is not None for r in results)]

    if has_searched:
        # Only that a search happened and how many rows it found: no filter values or search words.
        record_activity(
            request.user, TEST, "life.rates.searched", "Ran a Life payout rate search",
            extra={"results": total_found},
        )

    return render(request, "life_payout_rates.html", {
        "upload": upload,
        "selected": selected,
        "has_searched": has_searched,
        "data": results,
        "total_found": total_found,
        "has_more": total_found > len(results),
        "max_results": LIFE_MAX_RESULTS,
        "columns": columns,
        "notes": notes,
        "filter_rows": filter_rows,
        "insurer_list": insurer_list,
        "category_options": CATEGORIES,
        "payment_mode_options": PAYMENT_MODES,
        "ppt_options": PPT_CHOICES,
        "pt_options": PT_CHOICES,
    })


# -------------------------
# LIFE RATE MASTER (admin): the stored rows, the insurer notes, the uploads
# -------------------------
def _viewed_upload(request, uploads):
    """The upload the Life Rate Master pages show: the one ?upload= names, else the live one, else the newest."""
    try:
        wanted = int(request.GET.get("upload") or request.POST.get("upload"))
    except (TypeError, ValueError):
        wanted = None
    for upload in uploads:
        if upload.id == wanted:
            return upload
    for upload in uploads:
        if upload.is_active:
            return upload
    return uploads[0] if uploads else None


def _upload_query(upload):
    """'?upload=<id>' for an upload that is not the live one (the live one is what the pages show by default)."""
    return f"?upload={upload.id}" if upload is not None and not upload.is_active else ""


def _master_context(request, section):
    """What the header and the sub-navigation of every Life Rate Master page need."""
    uploads = list(LifeGridUpload.objects.select_related("uploaded_by"))
    upload = _viewed_upload(request, uploads)
    return {
        "section": section,
        "uploads": uploads,
        "upload": upload,
        "live_upload": next((u for u in uploads if u.is_active), None),
        "stats": master_stats(upload) if upload else {"rows": 0, "insurers": 0, "edited": 0, "with_mg": 0},
        "upload_query": _upload_query(upload),
    }


def _safe_next(request, default):
    """Where to go after an edit: the page the Edit button was clicked on (?next=), if that is on this site."""
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return default
    return next_url


def life_rate_master(request):
    """Every stored payout row of an upload (the live one unless ?upload= names another): filter, page through, edit."""
    context = _master_context(request, "rows")
    upload = context["upload"]
    if upload is not None:
        options = master_filter_options(upload)
        selected = clean_master_filters(selected_master_filters(request.GET), options)
        page_obj = Paginator(master_rows(upload, selected), selected["page_size"]).get_page(request.GET.get("page"))
        query = request.GET.copy()
        query.pop("page", None)
        add_params = {"next": request.get_full_path()}
        if selected["insurer"]:
            add_params["insurer"] = selected["insurer"]
        if context["upload_query"]:
            add_params["upload"] = upload.id
        context.update({
            "options": options,
            "selected": selected,
            "page_obj": page_obj,
            "page_query": query.urlencode(),
            "payout_columns": PAYOUT_HEADERS,
            "page_sizes": MASTER_PAGE_SIZES,
            "add_url": f"{reverse('life_rate_master_add')}?{urlencode(add_params)}",
            "bulk_groups": [(group, [(value, label, bulk_field_kind(value)) for value, label in choices])
                            for group, choices in bulk_field_choices()],
        })
    return render(request, "life_rate_master.html", context)


def life_rate_master_notes(request):
    """The insurer notes of an upload (the terms and conditions under each insurer's tables), each with an Edit button."""
    context = _master_context(request, "notes")
    upload = context["upload"]
    note_rows = []
    if upload is not None:
        notes = {note.insurer: note for note in LifeInsurerNote.objects.filter(upload=upload)}
        insurers = master_filter_options(upload)["insurers"]
        shown = {name for name, _ in insurers}
        note_rows = [{"insurer": name, "rows": count, "note": notes.get(name)} for name, count in insurers]
        note_rows += [{"insurer": name, "rows": 0, "note": note}
                      for name, note in sorted(notes.items()) if name not in shown]
    context["note_rows"] = note_rows
    return render(request, "life_rate_master_notes.html", context)


def life_rate_master_uploads(request):
    """Upload the standard file (or the old workbook), review what an upload changes, and switch between the uploads
    kept so far."""
    context = _master_context(request, "uploads")
    context["max_upload_mb"] = MAX_UPLOAD_BYTES // (1024 * 1024)
    problems = request.session.pop("life_upload_problems", None)           # shown once, after a refused file
    if problems:
        problems["more"] = max(0, problems["total"] - len(problems["problems"]))
    context["upload_problems"] = problems
    context["pending_uploads"] = [item for item in context["uploads"] if item.is_pending]
    return render(request, "life_rate_master_uploads.html", context)


def _label_for(request, file_name):
    return (request.POST.get("label") or "").strip()[:100] or default_label(file_name)


def _stored_for_review(request, upload, file_name, label):
    """Log an upload that was stored but not made live, tell the user, and open its review."""
    AuditLog.objects.create(
        user=request.user,
        action="LIFE GRID UPLOAD",
        details=(f"Uploaded '{file_name}' as '{label}' for review: {upload.row_count} rows from "
                 f"{upload.insurer_count} insurers. It is not live."),
    )
    record_activity(
        request.user, UPLOAD, "life.grid.uploaded",
        f"Uploaded the Life grid '{label}' for review: {upload.row_count} rows from {upload.insurer_count} insurers (not live)",
        target=upload, extra={"rows": upload.row_count, "insurers": upload.insurer_count, "live": False},
    )
    messages.success(
        request, f"'{label}' was read: {upload.row_count} payout rows from {upload.insurer_count} insurers. "
                 f"It is not live yet: look at what it changes, then make it live.")
    return redirect("life_rate_master_review", pk=upload.pk)


@require_POST
def life_rate_master_upload(request):
    """The old way in: the working workbook, read at fixed positions. It goes live at once unless "show me what changed
    first" is ticked, in which case it waits for review like a standard file."""
    uploaded = request.FILES.get("workbook")
    if uploaded is None:
        messages.error(request, "Choose the Life workbook (.xlsx) to upload.")
        return redirect("life_rate_master_uploads")
    if not uploaded.name.lower().endswith(".xlsx"):
        messages.error(request, "The Life workbook must be an .xlsx file.")
        return redirect("life_rate_master_uploads")
    if uploaded.size > MAX_UPLOAD_BYTES:
        messages.error(request, f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        return redirect("life_rate_master_uploads")

    try:
        result = read_life_workbook(io.BytesIO(uploaded.read()))
    except LifeLayoutError as exc:
        messages.error(request, f"Nothing was uploaded; the Life rates are unchanged. {exc}")
        return redirect("life_rate_master_uploads")

    label = _label_for(request, uploaded.name)
    review_first = request.POST.get("review") == "1"
    upload = save_life_upload(result, label=label, file_name=uploaded.name, user=request.user,
                              make_live=not review_first)
    if review_first:
        return _stored_for_review(request, upload, uploaded.name, label)
    AuditLog.objects.create(
        user=request.user,
        action="LIFE GRID UPLOAD",
        details=f"Uploaded '{uploaded.name}' as '{label}': {upload.row_count} rows from {upload.insurer_count} insurers.",
    )
    record_activity(
        request.user, UPLOAD, "life.grid.uploaded",
        f"Uploaded the Life grid '{label}' and made it live: {upload.row_count} rows from {upload.insurer_count} insurers",
        target=upload, extra={"rows": upload.row_count, "insurers": upload.insurer_count, "live": True},
    )
    messages.success(
        request, f"'{label}' is now live: {upload.row_count} payout rows from {upload.insurer_count} insurers.")
    for line in upload.warnings.splitlines():
        messages.warning(request, line)
    return redirect("life_rate_master")


@require_POST
def life_rate_master_upload_standard(request):
    """The standard file (services/life_standard.py): one flat table for every insurer. The whole file is checked first;
    a refused file leaves everything as it was and its problems are listed on the Uploads page. An accepted one is
    stored but not live: it goes to its review, where Make live is one click."""
    uploaded = request.FILES.get("standard_file")
    if uploaded is None:
        messages.error(request, "Choose the standard file (.xlsx or .csv) to upload.")
        return redirect("life_rate_master_uploads")
    if uploaded.size > MAX_UPLOAD_BYTES:
        messages.error(request, f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        return redirect("life_rate_master_uploads")

    try:
        result = read_standard_file(uploaded.read(), uploaded.name)
    except LifeFileError as exc:
        request.session["life_upload_problems"] = {
            "file": uploaded.name, "problems": exc.problems[:PROBLEMS_SHOWN], "total": exc.total}
        messages.error(request, "Nothing was uploaded; the Life rates are unchanged. The problems are listed below.")
        return redirect("life_rate_master_uploads")

    label = _label_for(request, uploaded.name)
    upload = save_life_upload(result, label=label, file_name=uploaded.name, user=request.user, make_live=False)
    return _stored_for_review(request, upload, uploaded.name, label)


def life_rate_master_template(request):
    """The empty standard file: the Rates and Notes sheets with their headers, and a Read me sheet."""
    response = HttpResponse(content_type=XLSX_TYPE)
    response["Content-Disposition"] = 'attachment; filename="life_standard_template.xlsx"'
    standard_template_workbook().save(response)
    return response


def life_rate_master_review(request, pk):
    """What making an upload live would change from the live grid: the difference row by row, a table by insurer and a
    few checks. Nothing here changes anything; Make live and Discard are POSTs to their own views."""
    upload = get_object_or_404(LifeGridUpload.objects.select_related("uploaded_by"), pk=pk)
    if upload.is_active:
        messages.info(request, f"'{upload.label}' is already the live grid, so there is nothing to review.")
        return redirect("life_rate_master_uploads")

    context = _master_context(request, "uploads")
    context["upload"], context["upload_query"] = upload, _upload_query(upload)
    context["stats"] = master_stats(upload)
    review = review_upload(upload, context["live_upload"])

    kind = request.GET.get("kind") if request.GET.get("kind") in KIND_ORDER else ""
    insurer = (request.GET.get("insurer") or "").strip()
    if insurer not in {row["name"] for row in review.insurers}:
        insurer = ""
    shown = [change for change in review.changes
             if (not kind or change.kind == kind)
             and (not insurer or insurer_key(change.row["insurer"]) == insurer_key(insurer))]
    query = request.GET.copy()
    query.pop("page", None)
    context.update({
        "review": review,
        "page_obj": Paginator(shown, REVIEW_PAGE_SIZE).get_page(request.GET.get("page")),
        "kinds": list(KIND_ORDER),
        "selected_kind": kind,
        "selected_insurer": insurer,
        "page_query": query.urlencode(),
        "notes_from_file": upload.warnings.splitlines(),
    })
    return render(request, "life_rate_master_review.html", context)


@require_POST
def life_rate_master_discard(request, pk):
    """Throw away an upload that is waiting for review (with its rows and notes). The live grid and earlier uploads are
    history and cannot be discarded."""
    upload = get_object_or_404(LifeGridUpload, pk=pk)
    label, file_name, rows = upload.label, upload.file_name, upload.row_count
    try:
        discard_life_upload(upload)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("life_rate_master_uploads")
    AuditLog.objects.create(
        user=request.user,
        action="LIFE GRID UPLOAD",
        details=f"Discarded the upload '{label}' ('{file_name}', {rows} rows) that was waiting for review.",
    )
    record_activity(
        request.user, STATUS, "life.grid.discarded", f"Discarded the Life grid upload '{label}' that was waiting for review",
        extra={"rows": rows},
    )
    messages.success(request, f"'{label}' was discarded. The live grid is unchanged.")
    return redirect("life_rate_master_uploads")


def _count(number, word):
    """'1 row', '3 rows'."""
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


@require_POST
def life_rate_master_delete_upload(request, pk):
    """Delete an earlier upload (or one waiting for review) with its rows and notes, so test uploads can be cleaned up.
    The live grid cannot be deleted: the Rate Checker and the MIS engine read it, so make another upload live first."""
    upload = get_object_or_404(LifeGridUpload, pk=pk)
    label, file_name = upload.label, upload.file_name
    rows, insurers = upload.row_count, upload.insurer_count
    uploaded = timezone.localtime(upload.uploaded_at).strftime("%d %b %Y %H:%M")
    kind = "an upload that was waiting for review" if upload.is_pending else "an earlier upload"
    try:
        delete_life_upload(upload)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("life_rate_master_uploads")
    AuditLog.objects.create(
        user=request.user,
        action="LIFE GRID UPLOAD",
        details=(f"Deleted {kind} '{label}' ('{file_name}', {_count(rows, 'row')} from {_count(insurers, 'insurer')}, "
                 f"uploaded {uploaded}) with its rows and notes."),
    )
    record_activity(
        request.user, STATUS, "life.grid.deleted", f"Deleted {kind} '{label}' (uploaded {uploaded})",
        extra={"rows": rows, "insurers": insurers},
    )
    messages.success(request, f"'{label}' (uploaded {uploaded}) was deleted with its {_count(rows, 'row')}. "
                              f"The live grid is unchanged.")
    return redirect("life_rate_master_uploads")


@require_POST
def life_rate_master_activate(request, pk):
    upload = get_object_or_404(LifeGridUpload, pk=pk)
    activate_life_upload(upload)
    AuditLog.objects.create(user=request.user, action="LIFE GRID UPLOAD", details=f"Made '{upload.label}' the live Life grid.")
    record_activity(
        request.user, STATUS, "life.grid.live", f"Made the Life grid '{upload.label}' live", target=upload,
    )
    messages.success(request, f"'{upload.label}' is now live.")
    return redirect("life_rate_master_uploads")


def _field_groups(form):
    """The fields of the row form in the order a person reads a row: what the row is, its MG part, its BG part, the remark."""
    groups = [
        ("", "", ["insurer", "category", "product", "variant", "ppt", "pt", "condition"]),
        ("Valid", "the dates the row applies between, both included; leave one empty for no limit on that side "
                  "(the MIS engine reads them against the policy's inception date)", ["from_date", "to_date"]),
        ("MG", "what the insurer pays the broker (the workbook's Final MG)", LifeRate.MG_FIELDS),
        ("BG", "the partner payout (the workbook's Final BG): the only part the Rate Checker shows", LifeRate.PAYOUT_FIELDS),
        ("", "", ["remarks"]),
    ]
    return [(title, hint, [form[name] for name in names if name in form.fields]) for title, hint, names in groups]


def _audit_value(value):
    """A value as the audit log shows it: a date as 'YYYY-MM-DD', anything else as its repr."""
    return repr(value.isoformat()) if isinstance(value, date) else repr(value)


def life_rate_master_edit(request, pk):
    """Change one stored payout row on screen. The change lives on that upload's row, so the next upload starts clean."""
    record = get_object_or_404(LifeRate.objects.select_related("upload", "edited_by"), pk=pk)
    next_url = _safe_next(request, reverse("life_rate_master") + _upload_query(record.upload))

    if request.method == "POST":
        before = LifeRate.objects.get(pk=pk)
        form = LifeRateForm(request.POST, instance=record)
        if form.is_valid():
            if form.has_changed():
                changes = "; ".join(
                    f"{name}: {_audit_value(getattr(before, name))} -> {_audit_value(form.cleaned_data[name])}"
                    for name in form.changed_data)
                row = form.save(commit=False)
                row.is_edited = True
                row.edited_at = timezone.now()
                row.edited_by = request.user
                row.save()
                AuditLog.objects.create(
                    user=request.user,
                    action="LIFE RATE EDIT",
                    details=f"Edited Life rate #{pk} ({row.insurer} | {row.product or 'no product'}): {changes}",
                )
                messages.success(request, "The payout row was saved.")
            return redirect(next_url)
    else:
        form = LifeRateForm(instance=record)

    return render(request, "edit_life_rate.html", {
        "form": form, "record": record, "next_url": next_url, "field_groups": _field_groups(form)})


def _preview(text, length=120):
    """A one-line start of a long text, for the audit log."""
    flat = " ".join((text or "").split())
    return repr(flat[:length] + ("..." if len(flat) > length else ""))


@require_POST
def life_rate_master_note_add(request):
    """Start the notes of an insurer that has none in an upload (the workbook had no notes block under its tables),
    then open them for editing."""
    try:
        upload_id = int(request.POST.get("upload", ""))
    except ValueError:
        upload_id = None
    upload = get_object_or_404(LifeGridUpload, pk=upload_id)
    notes_page = reverse("life_rate_master_notes") + _upload_query(upload)
    insurer = (request.POST.get("insurer") or "").strip()
    if not LifeRate.objects.filter(upload=upload, insurer=insurer).exists():
        messages.error(request, "That insurer has no payout rows in this upload.")
        return redirect(notes_page)
    note, _ = LifeInsurerNote.objects.get_or_create(upload=upload, insurer=insurer, defaults={"text": ""})
    return redirect(f"{reverse('life_rate_master_note_edit', args=[note.pk])}?{urlencode({'next': notes_page})}")


def life_rate_master_note_edit(request, pk):
    """Change an insurer's notes on screen. Like a row edit, it lives on that upload only."""
    note = get_object_or_404(LifeInsurerNote.objects.select_related("upload"), pk=pk)
    next_url = _safe_next(request, reverse("life_rate_master_notes") + _upload_query(note.upload))

    if request.method == "POST":
        before_text = note.text                      # read first: validating the form writes the new text onto `note`
        form = LifeInsurerNoteForm(request.POST, instance=note)
        if form.is_valid():
            if form.has_changed():
                saved = form.save()
                AuditLog.objects.create(
                    user=request.user,
                    action="LIFE NOTE EDIT",
                    details=(f"Edited the notes of {saved.insurer} in '{saved.upload.label}': "
                             f"{_preview(before_text)} -> {_preview(saved.text)}"),
                )
                messages.success(request, f"The notes of {saved.insurer} were saved.")
            return redirect(next_url)
    else:
        form = LifeInsurerNoteForm(instance=note)

    return render(request, "edit_life_note.html", {"form": form, "note": note, "next_url": next_url})


# -------------------------
# LIFE RATE MASTER: add a row, delete a row, bulk edit, Excel export
# -------------------------
def _row_url(upload, row):
    """The Life Rate Master page that shows `row` (its insurer's rows, on the page the row falls on), scrolled to it."""
    before = LifeRate.objects.filter(upload=upload, insurer=row.insurer, position__lt=row.position).count()
    params = {"insurer": row.insurer, "page": before // MASTER_DEFAULT_PAGE_SIZE + 1}
    if not upload.is_active:
        params["upload"] = upload.id
    return f"{reverse('life_rate_master')}?{urlencode(params)}#row-{row.id}"


def life_rate_master_add(request):
    """Type in a new payout row for an upload (the live one unless ?upload= names another). It goes after the last row of
    its insurer, is marked as added on screen, and, like every change made on screen, lasts until the next upload."""
    uploads = list(LifeGridUpload.objects.all())
    upload = _viewed_upload(request, uploads)
    if upload is None:
        messages.error(request, "Upload a Life workbook before adding rows.")
        return redirect("life_rate_master_uploads")
    next_url = _safe_next(request, reverse("life_rate_master") + _upload_query(upload))

    if request.method == "POST":
        form = LifeRateAddForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                row = add_life_rate(upload, form.cleaned_data, request.user)
                AuditLog.objects.create(
                    user=request.user,
                    action="LIFE RATE ADD",
                    details=f"Added Life rate #{row.pk} to '{upload.label}': {life_rate_snapshot(row)}",
                )
            messages.success(request, f"Added row #{row.pk}: {row.insurer} | {row.product}.")
            return redirect(_row_url(upload, row))
    else:
        form = LifeRateAddForm(initial={"insurer": (request.GET.get("insurer") or "").strip()})

    return render(request, "edit_life_rate.html", {
        "form": form, "adding": True, "upload": upload, "next_url": next_url, "field_groups": _field_groups(form),
        "insurers": [name for name, _ in master_filter_options(upload)["insurers"]],
    })


@require_POST
def life_rate_master_delete(request, pk):
    """Delete one stored row. The audit log keeps every value it had, so it can be typed in again."""
    row = get_object_or_404(LifeRate.objects.select_related("upload"), pk=pk)
    upload = row.upload
    next_url = _safe_next(request, reverse("life_rate_master") + _upload_query(upload))
    snapshot, label = life_rate_snapshot(row), f"{row.insurer} | {row.product or 'no product'}"
    with transaction.atomic():
        delete_life_rate(row)
        AuditLog.objects.create(
            user=request.user,
            action="LIFE RATE DELETE",
            details=f"Deleted Life rate #{pk} from '{upload.label}': {snapshot}",
        )
    messages.success(request, f"Deleted row #{pk} ({label}).")
    return redirect(next_url)


def _ids(values):
    """The row ids in a list of POSTed values (each may also be a comma-separated list)."""
    ids = []
    for raw in values:
        ids.extend(int(part) for part in str(raw).split(",") if part.strip().isdigit())
    return ids


def _filters_in_words(selected):
    parts = [f"insurer {selected['insurer']!r}" if selected["insurer"] else "",
             f"category {selected['category']!r}" if selected["category"] else "",
             f"search {selected['q']!r}" if selected["q"] else "",
             "edited rows only" if selected["edited"] else ""]
    return ", ".join(part for part in parts if part) or "no filters"


@require_POST
def life_rate_master_bulk_update(request):
    """Change one field on many rows at once: the rows ticked on the page, or every row matching the page's filters.
    Only the fields in services.life_rates.BULK_FIELD_LABELS can be changed. The whole operation is checked first and
    refused as a whole if any row would end outside 0-100 (or too long); otherwise it is saved in one transaction, each
    changed row is stamped as edited on screen, and one audit entry records what was done and the first values."""
    try:
        upload_id = int(request.POST.get("upload", ""))
    except ValueError:
        upload_id = None
    upload = get_object_or_404(LifeGridUpload, pk=upload_id)
    back = _safe_next(request, reverse("life_rate_master") + _upload_query(upload))

    form = LifeBulkUpdateForm(request.POST)
    if not form.is_valid():
        problems = " ".join(error for errors in form.errors.values() for error in errors)
        messages.error(request, f"Bulk edit rejected: {problems} No rows were changed.")
        return redirect(back)
    column, operation, value = form.cleaned_data["field"], form.cleaned_data["operation"], form.cleaned_data["value"]

    if request.POST.get("scope") == "filtered":
        selected = clean_master_filters(selected_master_filters(request.POST), master_filter_options(upload))
        rows = list(master_rows(upload, selected))
        try:
            expected = int(request.POST.get("expected_count", ""))
        except ValueError:
            expected = None
        if expected != len(rows):
            messages.error(
                request,
                f"Bulk edit rejected: the rows matching the filters changed since the page was loaded (it showed "
                f"{expected}, there are {len(rows)} now). Reload the page and try again. No rows were changed.")
            return redirect(back)
        scope = f"all {len(rows)} rows matching the filters ({_filters_in_words(selected)})"
    else:
        rows = list(LifeRate.objects.filter(upload=upload, id__in=_ids(request.POST.getlist("selected_rows"))))
        scope = f"{len(rows)} selected rows"
    if not rows:
        messages.error(request, "Bulk edit rejected: no rows were selected. No rows were changed.")
        return redirect(back)

    plan = plan_bulk_update(rows, column, operation, value)
    what = describe_bulk(column, operation, value)
    if plan.errors:
        more = f" (and {len(plan.errors) - 5} more)" if len(plan.errors) > 5 else ""
        messages.error(request, f"Bulk edit rejected ({what}): {' '.join(plan.errors[:5])}{more} No rows were changed.")
        return redirect(back)
    if not plan.changes:
        messages.info(request, f"Nothing to change ({what}): the {len(rows)} rows already have that value"
                               f"{f' or have no payout to work from ({plan.skipped} skipped)' if plan.skipped else ''}.")
        return redirect(back)

    with transaction.atomic():
        changed = save_bulk_changes(plan, column, request.user)
        AuditLog.objects.create(
            user=request.user,
            action="LIFE BULK UPDATE",
            details=bulk_audit_details(upload, column, operation, value, scope, plan),
        )
    extra = []
    if plan.skipped:
        extra.append(f"{plan.skipped} skipped (blank payout)")
    if plan.unchanged:
        extra.append(f"{plan.unchanged} already had that value")
    messages.success(request, f"Bulk edit done ({what}): {changed} rows changed" + (f"; {', '.join(extra)}" if extra else "") + ".")
    return redirect(back)


def life_rate_master_dashboard(request):
    """The Dashboard tab: the live grid (or ?upload=) as ONE flat sheet for a dashboard, what it holds, and what it had
    to leave out. Nothing is stored: the sheet is built from the upload each time it is downloaded."""
    context = _master_context(request, "dashboard")
    upload = context["upload"]
    if upload is not None:
        data = dashboard_data(upload)
        context.update({
            "data": data,
            "left_out": data.left_out[:LEFT_OUT_SHOWN],
            "left_out_more": max(0, len(data.left_out) - LEFT_OUT_SHOWN),
            "restated": data.restated[:LEFT_OUT_SHOWN],
            "columns": COLUMN_HELP,
        })
    return render(request, "life_rate_master_dashboard.html", context)


def life_rate_master_dashboard_download(request):
    """Download the one-sheet Excel of the viewed upload. ?mg=0 leaves the MG rows out (BG only: safe to share)."""
    uploads = list(LifeGridUpload.objects.all())
    upload = _viewed_upload(request, uploads)
    if upload is None:
        messages.error(request, "There is nothing to export yet: upload a Life grid first.")
        return redirect("life_rate_master_uploads")
    include_mg = request.GET.get("mg") != "0"
    data = dashboard_data(upload, include_mg=include_mg)
    if not data.rows:
        messages.error(request, f"'{upload.label}' has no payout rows the sheet can hold.")
        return redirect("life_rate_master_dashboard")
    name = re.sub(r"[^A-Za-z0-9_-]+", "", upload.label) or "grid"
    response = HttpResponse(content_type=XLSX_TYPE)
    response["Content-Disposition"] = f'attachment; filename="life_dashboard_{name}{"" if include_mg else "_BG"}.xlsx"'
    dashboard_workbook(data).save(response)
    return response


def life_rate_master_export(request):
    """Download the data grid as Excel: every stored row of the upload that matches the page's filters (not only the
    page being shown), with the stored field names as the header."""
    uploads = list(LifeGridUpload.objects.all())
    upload = _viewed_upload(request, uploads)
    if upload is None:
        messages.error(request, "There is nothing to export yet: upload a Life workbook first.")
        return redirect("life_rate_master_uploads")
    selected = clean_master_filters(selected_master_filters(request.GET), master_filter_options(upload))
    workbook = life_export_workbook(master_rows(upload, selected).iterator(chunk_size=2000), upload)
    name = re.sub(r"[^A-Za-z0-9_-]+", "", upload.label) or "grid"
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="life_rates_{name}.xlsx"'
    workbook.save(response)
    return response
