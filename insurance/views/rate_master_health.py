"""Rate Master Health and the overlap detector: validity errors, grid summary, overlap scans, and the deactivate / export actions."""
from django.contrib import messages
from django.utils import timezone
from django.shortcuts import render, redirect
from django.urls import reverse
from django.http import HttpResponse
from django.db.models import Count
from django.core.paginator import Paginator
from urllib.parse import urlencode
import logging
from datetime import datetime
from openpyxl import Workbook
from ..models import AuditLog, MISFailedRow, RateOverlapScan, RateOverlapPair
from ..tasks import run_rate_overlap_scan_task
from ..services.overlap import OVERLAP_RULES_MAP, _active_overlap_scan, _active_rate_master_insurers, _active_rate_master_products, _fmt_axis_side, _fmt_bound, _latest_overlap_scan, _overlap_counts, _rate_group_rows, _scan_scope_label
from ..services.rate_master_checks import EQUALITY_ERROR_RULES_MAP, PI_PO_ERROR_RULES_MAP, PI_TYPE_RULES_MAP, RANGE_ERROR_RULES_MAP, RATE_MASTER_PI_PO_MARGIN, _rate_master_equality_error_counts, _rate_master_grid_summary_qs, _rate_master_pi_po_error_counts, _rate_master_pi_type_error_counts, _rate_master_pi_type_violations_qs, _rate_master_range_error_counts, _split_payload_summary, _sync_unsynced_mis_files


logger = logging.getLogger("security")


# -------------------------
# RATE MASTER HEALTH
# -------------------------
# Surfaces individual MIS policy rows the mapping engine
# (mapping_engine.process_mis_mapping) could not resolve to a single payout
# rate — ⚠️ MULTIPLE MATCHES, ❌ NO MATCH, and FAILED - BAD DATA. These are
# stored directly in MISFailedRow (written straight from df_final when a
# file finishes processing — see process_mis_mapping), so this view is a
# normal indexed queryset + select_related + Paginator, not something that
# re-parses any Excel/CSV output on every page view.

MIS_HEALTH_BATCH_SIZE = 50


def _overlap_redirect(overlap_type=""):
    params = {"view": "overlap"}
    if overlap_type:
        params["overlap_type"] = overlap_type
    return redirect(f"{reverse('rate_master_health')}?{urlencode(params)}")


def start_overlap_scan(request):
    """
    Queues a fresh sweep, optionally scoped to one insurer, one product,
    and/or one validity date. Results replace the previous scan's only once
    it succeeds - a scoped scan supersedes a full one and vice versa, same as
    any other re-run, since only the latest COMPLETED scan's pairs are ever
    shown.
    """
    if request.method != "POST":
        return _overlap_redirect()

    if _active_overlap_scan():
        messages.info(request, "An overlap scan is already running - reload in a moment for its results.")
        return _overlap_redirect()

    insurer = (request.POST.get("insurer") or "").strip()
    if insurer and insurer not in _active_rate_master_insurers():
        messages.error(
            request,
            f"Could not start the scan: '{insurer}' has no active Rate Master rows.",
        )
        return _overlap_redirect()

    product = (request.POST.get("product") or "").strip()
    if product and product not in _active_rate_master_products():
        messages.error(
            request,
            f"Could not start the scan: '{product}' has no active Rate Master rows.",
        )
        return _overlap_redirect()

    as_of_date_raw = (request.POST.get("as_of_date") or "").strip()
    as_of_date = None
    if as_of_date_raw:
        try:
            as_of_date = datetime.strptime(as_of_date_raw, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, f"Could not start the scan: '{as_of_date_raw}' is not a valid date.")
            return _overlap_redirect()

    scan = RateOverlapScan.objects.create(
        triggered_by=request.user,
        filter_insurer=insurer or None,
        filter_product=product or None,
        filter_as_of_date=as_of_date,
    )
    try:
        run_rate_overlap_scan_task.delay(scan.id)
    except Exception as exc:
        # Broker/worker unreachable. Close the scan out here rather than
        # leaving it RUNNING, which would block every later scan until it
        # aged past OVERLAP_SCAN_STALE_AFTER.
        logger.exception("Could not queue rate overlap scan %s", scan.id)
        scan.status = RateOverlapScan.STATUS_FAILED
        scan.error_message = f"Could not queue the scan: {exc}"
        scan.finished_at = timezone.now()
        scan.save(update_fields=["status", "error_message", "finished_at"])
        messages.error(
            request,
            "Could not start the overlap scan - the background worker is unreachable. "
            "Check that REDIS_URL is set and the Celery worker is running, or run the "
            "scan directly with: python manage.py run_overlap_scan",
        )
        return _overlap_redirect()

    scope_label = _scan_scope_label(scan)
    scope_note = f" ({scope_label})" if scope_label else ""
    messages.success(
        request, f"Overlap scan{scope_note} started. Reload this page in a moment to see the results."
    )
    return _overlap_redirect()


def deactivate_rate_group(request, group_key):
    """
    Takes one side of a conflicting pair out of the running by marking its rows
    INACTIVE - the fix for an exact duplicate, where the two groups are
    interchangeable and one is simply surplus.

    Deliberately not a delete: the rows stay queryable, keep their history, and
    can be switched back on from the Rate Master dashboard. Narrowing a range
    instead (the fix for a partial overlap) is left to Edit Rate, which already
    validates every field - the drill-down links straight to it.
    """
    if request.method != "POST":
        return _overlap_redirect()

    overlap_type = (request.POST.get("overlap_type") or "").strip()

    # Deactivating is only ever right when the two groups are interchangeable,
    # which is why the pair has to be one this scan actually listed. Pairs whose
    # T&Cs differ are separate offers rather than duplicates and never reach the
    # dashboard at all (overlap_utils.classify_pair drops them), so requiring a
    # live pair id is also what keeps a stale page or a hand-made POST from
    # deactivating one of those behind the scan's back.
    pair = RateOverlapPair.objects.filter(
        id=request.POST.get("pair_id") or 0, scan__status=RateOverlapScan.STATUS_COMPLETED
    ).first()
    if pair is None or group_key not in (pair.group_key_a, pair.group_key_b):
        messages.error(
            request,
            "Could not deactivate: that conflict is no longer in the latest scan. Re-run the scan and try again.",
        )
        return _overlap_redirect(overlap_type)

    # Double Rate Risk pairs conflict through a raw code duplicated across two
    # RTOMaster/MakeModelMaster clusters, not through the two RateMaster groups
    # themselves - deactivating either group leaves that master-table entry
    # untouched, so the exact same collision resurfaces against the next group
    # that references either cluster. The template already hides this button
    # for that type; refuse it here too so a hand-made POST can't reach past it.
    if pair.conflict_type == "DOUBLE_RATE_RISK":
        messages.error(
            request,
            f"Group {group_key} was not deactivated - this conflict is a Double Rate Risk, caused by "
            f"a raw code duplicated across master-table clusters, not by either RateMaster group. Fix "
            f"the RTOMaster/MakeModelMaster entry instead.",
        )
        return _overlap_redirect(overlap_type)

    rows = _rate_group_rows(group_key)
    active_rows = rows.filter(status="ACTIVE")
    record_count = active_rows.count()

    if not record_count:
        messages.warning(request, f"Group {group_key} has no active rows to deactivate.")
        return _overlap_redirect(overlap_type)

    active_rows.update(status="INACTIVE", updated_at=timezone.now())
    AuditLog.objects.create(
        user=request.user,
        action="OVERLAP DEACTIVATE",
        details=(
            f"Deactivated Group/Record ID {group_key} ({record_count} rows) from the "
            f"Rate Master Health overlap detector."
        ),
    )
    messages.success(
        request,
        f"Group {group_key} deactivated ({record_count} rows). Re-run the scan to refresh the conflict list.",
    )
    return _overlap_redirect(overlap_type)


def export_overlap_pairs_xlsx(request):
    """
    Exports every conflicting pair of the given overlap_type from the latest
    scan — the full bucket, not just whatever page the on-screen list happens
    to be showing, same "export ignores pagination but honours real filters"
    rule export_grid_summary_xlsx follows. With no overlap_type (or no
    completed scan), exports an empty sheet with just the headers.

    Mirrors everything the on-screen drill-down card shows for a pair,
    including the "All fields" table that's collapsed by default on the page:
    one column trio (Group A / Group B / Shared) per axis describe_pair()
    compared them on. The axis list itself isn't hardcoded here — it's read
    off the stored pairs so the export can't drift out of sync with whatever
    overlap_utils.describe_pair happens to compare.
    """
    scan = _latest_overlap_scan()
    overlap_type = (request.GET.get("overlap_type") or "").strip()
    rule = OVERLAP_RULES_MAP.get(overlap_type)

    pairs = list(RateOverlapPair.objects.filter(scan=scan, conflict_type=rule["key"])) if (rule and scan) else []

    axis_labels = []
    seen_labels = set()
    for pair in pairs:
        for axis in (pair.detail or {}).get("axes") or []:
            label = axis.get("label")
            if label and label not in seen_labels:
                seen_labels.add(label)
                axis_labels.append(label)

    base_headers = [
        "insurance_company", "conflict_type",
        "group_key_a", "row_count_a", "group_key_b", "row_count_b",
        "collides_on", "note",
    ]
    axis_headers = [
        f"{label} ({side})" for label in axis_labels for side in ("Group A", "Group B", "Shared")
    ]

    wb = Workbook()
    ws = wb.active
    ws.title = "Overlaps"
    ws.append(base_headers + axis_headers)

    for pair in pairs:
        detail = pair.detail or {}
        collides_on = "; ".join(
            f"{span.get('label')}: {span.get('text') or (_fmt_bound(span.get('low')) + ' - ' + _fmt_bound(span.get('high')))}"
            for span in detail.get("summary") or []
        )
        note = (
            "These reference different clusters, but a raw code is duplicated across both clusters' "
            "entries in RTOMaster / MakeModelMaster — fix the master table entry (or point one group "
            "at a cluster that doesn't share the code), rather than deactivating either group."
            if pair.conflict_type == "DOUBLE_RATE_RISK" else ""
        )

        row = [
            pair.insurance_company or "",
            pair.get_conflict_type_display(),
            pair.group_key_a,
            pair.row_count_a,
            pair.group_key_b,
            pair.row_count_b,
            collides_on,
            note,
        ]

        axes_by_label = {axis.get("label"): axis for axis in detail.get("axes") or []}
        for label in axis_labels:
            axis = axes_by_label.get(label)
            if axis is None:
                row += ["", "", ""]
                continue
            row += [
                _fmt_axis_side(axis.get("a")),
                _fmt_axis_side(axis.get("b")),
                _fmt_axis_side(axis.get("overlap")),
            ]
        ws.append(row)

    filename = f"overlap_{rule['key'].lower()}_export.xlsx" if rule else "overlap_export.xlsx"
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


def rate_master_health(request):
    _sync_unsynced_mis_files()

    status_key = (request.GET.get("failure_reason") or "").strip()
    insurer = (request.GET.get("insurer") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    qs = MISFailedRow.objects.select_related("mis_file").order_by("-mis_file__created_at", "-id")

    if status_key:
        qs = qs.filter(status_key=status_key)
    if insurer:
        qs = qs.filter(insurer=insurer)
    if date_from:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            qs = qs.filter(mis_file__created_at__date__gte=from_date)
        except ValueError:
            pass
    if date_to:
        try:
            to_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            qs = qs.filter(mis_file__created_at__date__lte=to_date)
        except ValueError:
            pass

    total_all = MISFailedRow.objects.count()
    status_counts = dict(
        MISFailedRow.objects.values("status_key").annotate(n=Count("id")).values_list("status_key", "n")
    )

    paginator = Paginator(qs, MIS_HEALTH_BATCH_SIZE)
    try:
        page_number = int(request.GET.get("page") or 1)
    except ValueError:
        page_number = 1
    page_obj = paginator.get_page(page_number)
    elided_page_range = list(paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1))

    # select_related already joined mis_file — this only touches the current
    # page's rows (<= MIS_HEALTH_BATCH_SIZE), not the full filtered set.
    for r in page_obj:
        r.file_name = (
            r.mis_file.uploaded_file.name.rsplit("/", 1)[-1]
            if r.mis_file.uploaded_file else f"File #{r.mis_file_id}"
        )
        r.payload_summary, r.payload_rest = _split_payload_summary(r.payload)

    insurer_list = list(
        MISFailedRow.objects.exclude(insurer__isnull=True).exclude(insurer="")
        .order_by().values_list("insurer", flat=True).distinct()
    )
    insurer_list.sort()

    status_types = [c for c in MISFailedRow.STATUS_CHOICES if c[0] != "OTHER"]

    pi_type_error_counts = _rate_master_pi_type_error_counts()
    range_error_counts = _rate_master_range_error_counts()
    equality_error_counts = _rate_master_equality_error_counts()
    pi_po_error_counts = _rate_master_pi_po_error_counts()

    # Grid Summary pivot — materialized once so the paginator doesn't re-run
    # the aggregate query, same pattern dashboard() uses for ordered_gids.
    grid_summary_as_of_date_raw = (request.GET.get("grid_as_of_date") or "").strip()
    grid_summary_as_of_date = None
    if grid_summary_as_of_date_raw:
        try:
            grid_summary_as_of_date = datetime.strptime(grid_summary_as_of_date_raw, "%Y-%m-%d").date()
        except ValueError:
            grid_summary_as_of_date_raw = ""

    grid_summary_rows = list(_rate_master_grid_summary_qs(as_of_date=grid_summary_as_of_date))
    grid_summary_total_combinations = len(grid_summary_rows)
    grid_summary_total_grids = sum(r["grid_count"] for r in grid_summary_rows)

    grid_summary_paginator = Paginator(grid_summary_rows, MIS_HEALTH_BATCH_SIZE)
    try:
        grid_summary_page_number = int(request.GET.get("grid_page") or 1)
    except ValueError:
        grid_summary_page_number = 1
    grid_summary_page_obj = grid_summary_paginator.get_page(grid_summary_page_number)
    grid_summary_elided_range = list(
        grid_summary_paginator.get_elided_page_range(grid_summary_page_obj.number, on_each_side=1, on_ends=1)
    )

    # Drill-down: clicking an Error Dashboard card lists the distinct Rate
    # Master groups behind that rule's violating rows, so the user can jump
    # straight to Fix Mapping instead of hunting through the full table.
    pi_type_error = (request.GET.get("pi_type_error") or "").strip()
    error_group_page_obj = None
    error_group_elided_range = None
    error_group_ungrouped_count = 0
    if pi_type_error in PI_TYPE_RULES_MAP:
        violations_qs = _rate_master_pi_type_violations_qs(pi_type_error)
        error_group_ungrouped_count = violations_qs.filter(group_id__isnull=True).count()
        grouped_qs = (
            violations_qs.exclude(group_id__isnull=True)
            .values("group_id", "insurance_company")
            .annotate(row_count=Count("id"))
            .order_by("-row_count", "group_id")
        )
        error_paginator = Paginator(grouped_qs, MIS_HEALTH_BATCH_SIZE)
        try:
            error_page_number = int(request.GET.get("err_page") or 1)
        except ValueError:
            error_page_number = 1
        error_group_page_obj = error_paginator.get_page(error_page_number)
        error_group_elided_range = list(
            error_paginator.get_elided_page_range(error_group_page_obj.number, on_each_side=1, on_ends=1)
        )

    # Drill-down: clicking a Range Validation Errors card lists the individual
    # violating rows (not grouped counts, unlike the pi_type drill-down above)
    # so the user can see the actual min/max values that broke the rule.
    range_error = (request.GET.get("range_error") or "").strip()
    selected_range_rule = RANGE_ERROR_RULES_MAP.get(range_error)
    range_rows_page_obj = None
    range_rows_elided_range = None
    if selected_range_rule:
        value_fields = ["id", "group_id", "insurance_company"] + [f for f, _ in selected_range_rule["columns"]]
        range_rows_qs = selected_range_rule["violations_fn"]().values(*value_fields).order_by("-id")
        range_paginator = Paginator(range_rows_qs, MIS_HEALTH_BATCH_SIZE)
        try:
            range_page_number = int(request.GET.get("range_page") or 1)
        except ValueError:
            range_page_number = 1
        range_rows_page_obj = range_paginator.get_page(range_page_number)
        range_rows_elided_range = list(
            range_paginator.get_elided_page_range(range_rows_page_obj.number, on_each_side=1, on_ends=1)
        )

    # Drill-down: clicking an Equality Errors card lists the individual
    # violating rows, the same row-level shape as the Range Validation
    # drill-down above — equality is just a narrower rule, checked separately.
    equality_error = (request.GET.get("equality_error") or "").strip()
    selected_equality_rule = EQUALITY_ERROR_RULES_MAP.get(equality_error)
    equality_rows_page_obj = None
    equality_rows_elided_range = None
    if selected_equality_rule:
        value_fields = ["id", "group_id", "insurance_company"] + [f for f, _ in selected_equality_rule["columns"]]
        equality_rows_qs = selected_equality_rule["violations_fn"]().values(*value_fields).order_by("-id")
        equality_paginator = Paginator(equality_rows_qs, MIS_HEALTH_BATCH_SIZE)
        try:
            equality_page_number = int(request.GET.get("equality_page") or 1)
        except ValueError:
            equality_page_number = 1
        equality_rows_page_obj = equality_paginator.get_page(equality_page_number)
        equality_rows_elided_range = list(
            equality_paginator.get_elided_page_range(equality_rows_page_obj.number, on_each_side=1, on_ends=1)
        )

    # Drill-down: clicking a Pi vs Po Validation Errors card lists the
    # individual violating rows, the same row-level shape as the Range/
    # Equality drill-downs above.
    pi_po_error = (request.GET.get("pi_po_error") or "").strip()
    selected_pi_po_rule = PI_PO_ERROR_RULES_MAP.get(pi_po_error)
    pi_po_rows_page_obj = None
    pi_po_rows_elided_range = None
    if selected_pi_po_rule:
        value_fields = ["id", "group_id", "insurance_company"] + [f for f, _ in selected_pi_po_rule["columns"]]
        pi_po_rows_qs = selected_pi_po_rule["violations_fn"]().values(*value_fields).order_by("-id")
        pi_po_paginator = Paginator(pi_po_rows_qs, MIS_HEALTH_BATCH_SIZE)
        try:
            pi_po_page_number = int(request.GET.get("pi_po_page") or 1)
        except ValueError:
            pi_po_page_number = 1
        pi_po_rows_page_obj = pi_po_paginator.get_page(pi_po_page_number)
        pi_po_rows_elided_range = list(
            pi_po_paginator.get_elided_page_range(pi_po_rows_page_obj.number, on_each_side=1, on_ends=1)
        )

    # Overlaps tab. Reads only the newest COMPLETED scan's stored pairs — the
    # sweep itself runs in a Celery task (start_overlap_scan), never on a page
    # view, for the same reason MISFailedRow exists.
    overlap_scan = _latest_overlap_scan()
    overlap_scan_scope = _scan_scope_label(overlap_scan) if overlap_scan else None
    overlap_counts = _overlap_counts(overlap_scan)
    overlap_type = (request.GET.get("overlap_type") or "").strip()
    selected_overlap_rule = OVERLAP_RULES_MAP.get(overlap_type)
    overlap_page_obj = None
    overlap_elided_range = None
    if selected_overlap_rule and overlap_scan:
        overlap_qs = RateOverlapPair.objects.filter(
            scan=overlap_scan, conflict_type=selected_overlap_rule["key"]
        )
        overlap_paginator = Paginator(overlap_qs, MIS_HEALTH_BATCH_SIZE)
        try:
            overlap_page_number = int(request.GET.get("overlap_page") or 1)
        except ValueError:
            overlap_page_number = 1
        overlap_page_obj = overlap_paginator.get_page(overlap_page_number)
        overlap_elided_range = list(
            overlap_paginator.get_elided_page_range(overlap_page_obj.number, on_each_side=1, on_ends=1)
        )

    return render(request, "rate_master_health.html", {
        "page_obj": page_obj,
        "elided_page_range": elided_page_range,
        "total": paginator.count,
        "total_all": total_all,
        "status_types": status_types,
        "status_counts": status_counts,
        "insurer_list": insurer_list,
        "pi_type_error_counts": pi_type_error_counts,
        "pi_type_error_total": sum(r["count"] for r in pi_type_error_counts),
        "selected_pi_type_error": pi_type_error if pi_type_error in PI_TYPE_RULES_MAP else "",
        "range_error_counts": range_error_counts,
        "range_error_total": sum(r["count"] for r in range_error_counts),
        "selected_range_error": range_error if selected_range_rule else "",
        "selected_range_rule": selected_range_rule,
        "range_rows_page_obj": range_rows_page_obj,
        "range_rows_elided_range": range_rows_elided_range,
        "equality_error_counts": equality_error_counts,
        "equality_error_total": sum(r["count"] for r in equality_error_counts),
        "selected_equality_error": equality_error if selected_equality_rule else "",
        "selected_equality_rule": selected_equality_rule,
        "equality_rows_page_obj": equality_rows_page_obj,
        "equality_rows_elided_range": equality_rows_elided_range,
        "pi_po_error_counts": pi_po_error_counts,
        "pi_po_error_total": sum(r["count"] for r in pi_po_error_counts),
        "pi_po_margin": RATE_MASTER_PI_PO_MARGIN,
        "selected_pi_po_error": pi_po_error if selected_pi_po_rule else "",
        "selected_pi_po_rule": selected_pi_po_rule,
        "pi_po_rows_page_obj": pi_po_rows_page_obj,
        "pi_po_rows_elided_range": pi_po_rows_elided_range,
        "error_group_page_obj": error_group_page_obj,
        "error_group_elided_range": error_group_elided_range,
        "error_group_ungrouped_count": error_group_ungrouped_count,
        "grid_summary_page_obj": grid_summary_page_obj,
        "grid_summary_elided_range": grid_summary_elided_range,
        "grid_summary_total_combinations": grid_summary_total_combinations,
        "grid_summary_total_grids": grid_summary_total_grids,
        "grid_summary_as_of_date": grid_summary_as_of_date_raw,
        "overlap_scan": overlap_scan,
        "overlap_scan_running": _active_overlap_scan(),
        "overlap_insurer_list": _active_rate_master_insurers(),
        "overlap_product_list": _active_rate_master_products(),
        "overlap_scan_scope": overlap_scan_scope,
        "overlap_counts": overlap_counts,
        "overlap_total": sum(r["count"] for r in overlap_counts),
        "selected_overlap_type": overlap_type if selected_overlap_rule else "",
        "selected_overlap_rule": selected_overlap_rule,
        "selected_overlap_capped": bool(
            selected_overlap_rule and overlap_scan
            and selected_overlap_rule["key"] in (overlap_scan.capped_types or [])
        ),
        "overlap_page_obj": overlap_page_obj,
        "overlap_elided_range": overlap_elided_range,
        "selected": {
            "failure_reason": status_key,
            "insurer": insurer,
            "date_from": date_from,
            "date_to": date_to,
        },
    })


def export_grid_summary_xlsx(request):
    """
    Same pivot as the Grid Summary tab above -- always the full aggregate
    (all combinations), regardless of which page the on-screen table happens
    to be showing, since grid_page only controls that table's pagination.

    Does honour grid_as_of_date, though: that's a real content filter, not
    pagination, so a Download Excel click exports whatever the on-screen
    table is currently filtered to, same as every other export on this site
    respects its page's own filters.
    """
    as_of_date = None
    as_of_date_raw = (request.GET.get("grid_as_of_date") or "").strip()
    if as_of_date_raw:
        try:
            as_of_date = datetime.strptime(as_of_date_raw, "%Y-%m-%d").date()
        except ValueError:
            pass

    wb = Workbook()
    ws = wb.active
    ws.title = "Grid Summary"
    ws.append(["product", "sub_product", "insurance_company", "from_date", "to_date", "status", "grid_count"])

    for r in _rate_master_grid_summary_qs(as_of_date=as_of_date):
        ws.append([
            r["product__name"] or "",
            r["sub_product__name"] or "",
            r["insurance_company"],
            r["from_date"].strftime("%Y-%m-%d") if r["from_date"] else "",
            r["to_date"].strftime("%Y-%m-%d") if r["to_date"] else "",
            r["status"],
            r["grid_count"],
        ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="grid_summary_export.xlsx"'
    wb.save(response)
    return response
