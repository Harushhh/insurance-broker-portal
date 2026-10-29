"""Health Rate Master grid: list, edit, bulk update and export of HealthRateMaster rows."""
from django.contrib import messages
from django.utils import timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import HttpResponse
from django.db.models import Q
from django.core.paginator import Paginator
from urllib.parse import urlencode
from openpyxl import Workbook
from ..models import PincodeMaster, AuditLog, HealthRateMaster
from ..forms import HealthRateForm
from ..services.make_model import strict_match_in_cluster
from ..services.rate_master import apply_range_filter


# -------------------------
# HEALTH RATE MASTER
# -------------------------
# HealthRateMaster rows aren't exploded/grouped behind a separate group
# table like RateMaster is — one imported grid row is one editable rate
# rule, so list/edit/bulk-update all operate directly on HealthRateMaster ids.
HEALTH_RATE_BATCH_SIZE = 50


HEALTH_RATE_FIELD_NAMES = [
    "insurance_company", "product_name", "policy_category", "business_type",
    "plan_names", "min_sum_insured", "max_sum_insured", "min_deductible", "max_deductible",
    "min_age", "max_age", "pincode_zone", "payin_rate",
    "one_year_rate", "multi_year_2_rate", "multi_year_3_rate", "multi_year_4_rate", "multi_year_5_rate",
    "from_date", "to_date", "remarks", "status", "is_deleted",
]


# Only these HealthRateMaster fields may be touched via bulk update — same
# safety rule as RateMaster's ALLOWED_BULK_UPDATE_FIELDS: update_field comes
# straight from the POST body, so anything not on this list (id, source_row_hash,
# created_at, updated_at) must be rejected rather than passed to .update().
ALLOWED_HEALTH_BULK_UPDATE_FIELDS = {
    "insurance_company", "product_name", "policy_category", "business_type", "plan_names",
    "min_deductible", "max_deductible", "min_sum_insured", "max_sum_insured",
    "min_age", "max_age", "pincode_zone",
    "payin_rate", "one_year_rate", "multi_year_2_rate", "multi_year_3_rate",
    "multi_year_4_rate", "multi_year_5_rate",
    "from_date", "to_date", "status", "is_deleted", "remarks",
}


HEALTH_FLOAT_FIELDS = {
    "min_deductible", "max_deductible", "min_sum_insured", "max_sum_insured",
    "min_age", "max_age", "payin_rate", "one_year_rate",
    "multi_year_2_rate", "multi_year_3_rate", "multi_year_4_rate", "multi_year_5_rate",
}


def _build_health_rate_queryset(request):
    """Filter-building logic shared by health_rate_master (list) and export_health_rates_xlsx."""
    qs = HealthRateMaster.objects.all()

    q = (request.GET.get("q") or "").strip()
    insurance_company = (request.GET.get("insurance_company") or "").strip()
    product_name = (request.GET.get("product_name") or "").strip()
    policy_category = (request.GET.get("policy_category") or "").strip()
    business_type = (request.GET.get("business_type") or "").strip()
    pincode_zone = (request.GET.get("pincode_zone") or "").strip()
    pincode = (request.GET.get("pincode") or "").strip()
    status_filter = (request.GET.get("status") or "").strip()
    is_deleted_filter = (request.GET.get("is_deleted") or "").strip().upper()
    date_range = (request.GET.get("date_range") or "").strip()
    sum_insured_range = (request.GET.get("sum_insured_range") or "").strip()
    age_range = (request.GET.get("age_range") or "").strip()
    deductible_range = (request.GET.get("deductible_range") or "").strip()

    if q:
        # Supports plain text search (insurer/plan name) plus an ID lookup --
        # the table displays each row's id as "h-<id>" (see health_rate_master.html),
        # so accept that same "h-" prefix here (and a comma-separated list of
        # them), alongside a bare numeric id. Only switches into ID-list mode
        # when EVERY comma-separated token looks like an id -- otherwise q may
        # be a genuine text search that happens to contain a comma, so it
        # falls through to the plain insurer/plan-name match untouched.
        id_tokens = []
        for part in q.split(","):
            part = part.strip()
            if not part:
                continue
            if part[:2].lower() == "h-":
                part = part[2:]
            if part.isdigit():
                id_tokens.append(int(part))
            else:
                id_tokens = None
                break

        text_match = Q(insurance_company__icontains=q) | Q(plan_names__icontains=q)
        if id_tokens:
            text_match |= Q(pk__in=id_tokens)
        qs = qs.filter(text_match)
    if insurance_company:
        qs = qs.filter(insurance_company=insurance_company)
    if product_name:
        qs = qs.filter(product_name=product_name)
    if policy_category:
        qs = qs.filter(policy_category=policy_category)
    if business_type:
        qs = qs.filter(business_type=business_type)
    if pincode_zone:
        qs = qs.filter(pincode_zone=pincode_zone)

    # Raw pincode search: resolve through PincodeMaster's cluster to the
    # zone(s) it belongs to, same pattern as rto_code resolving through
    # RTOMaster.rto_cluster to a group name on Motor's dashboard(). A pincode
    # with no known zone mapping yet matches nothing — it means the zone's
    # real pincode list hasn't been filled in on the Pincode Dashboard.
    if pincode:
        matching_zone_names = []
        potential_zones = PincodeMaster.objects.filter(pincode_cluster__icontains=pincode)
        for zone_record in potential_zones:
            if strict_match_in_cluster(pincode, zone_record.pincode_cluster):
                matching_zone_names.append(zone_record.pincode_zone.strip())

        if matching_zone_names:
            qs = qs.filter(pincode_zone__in=matching_zone_names)
        else:
            qs = qs.none()

    if status_filter:
        qs = qs.filter(status=status_filter)
    if is_deleted_filter:
        qs = qs.filter(is_deleted=is_deleted_filter)

    if date_range:
        dates = date_range.split(" - ")
        if len(dates) == 2:
            d_from = dates[0].strip()
            d_to = dates[1].strip()
            if d_from and d_to:
                qs = qs.filter(
                    (Q(from_date__lte=d_to) | Q(from_date__isnull=True)) &
                    (Q(to_date__gte=d_from) | Q(to_date__isnull=True))
                )

    qs = apply_range_filter(qs, "min_sum_insured", "max_sum_insured", sum_insured_range)
    qs = apply_range_filter(qs, "min_age", "max_age", age_range)
    qs = apply_range_filter(qs, "min_deductible", "max_deductible", deductible_range)

    selected = {
        "q": q,
        "insurance_company": insurance_company,
        "product_name": product_name,
        "policy_category": policy_category,
        "business_type": business_type,
        "pincode_zone": pincode_zone,
        "pincode": pincode,
        "status": status_filter,
        "is_deleted": is_deleted_filter,
        "date_range": date_range,
        "sum_insured_range": sum_insured_range,
        "age_range": age_range,
        "deductible_range": deductible_range,
    }
    return qs, selected


def health_rate_master(request):
    # Fresh visit (no filters submitted yet, e.g. first load or the Reset
    # link) defaults the Date Range filter to today so the grid opens on
    # currently-valid rates instead of every historical row. Redirecting
    # (rather than defaulting silently in the queryset) puts date_range in
    # the URL itself, so pagination links and Export Excel -- which both
    # just echo request.GET -- carry the same default forward.
    if not request.GET:
        today_str = timezone.localdate().isoformat()
        return redirect(f"{reverse('health_rate_master')}?{urlencode({'date_range': f'{today_str} - {today_str}'})}")

    qs, selected = _build_health_rate_queryset(request)

    active_count = qs.filter(status="ACTIVE").count()
    inactive_count = qs.filter(status="INACTIVE").count()
    total_count = qs.count()

    qs = qs.order_by("-id")

    try:
        page_number = int(request.GET.get("page") or 1)
    except ValueError:
        page_number = 1
    paginator = Paginator(qs, HEALTH_RATE_BATCH_SIZE)
    page_obj = paginator.get_page(page_number)

    insurer_list = HealthRateMaster.objects.exclude(insurance_company="").values_list(
        "insurance_company", flat=True
    ).distinct().order_by("insurance_company")
    product_list = HealthRateMaster.objects.exclude(product_name__isnull=True).exclude(
        product_name=""
    ).values_list("product_name", flat=True).distinct().order_by("product_name")
    category_list = HealthRateMaster.objects.exclude(policy_category__isnull=True).exclude(
        policy_category=""
    ).values_list("policy_category", flat=True).distinct().order_by("policy_category")
    business_type_list = HealthRateMaster.objects.exclude(business_type__isnull=True).exclude(
        business_type=""
    ).values_list("business_type", flat=True).distinct().order_by("business_type")
    zone_list = HealthRateMaster.objects.exclude(pincode_zone__isnull=True).exclude(
        pincode_zone=""
    ).values_list("pincode_zone", flat=True).distinct().order_by("pincode_zone")

    return render(request, "health_rate_master.html", {
        "page_obj": page_obj,
        "field_names": HEALTH_RATE_FIELD_NAMES,
        "total": total_count,
        "active_count": active_count,
        "inactive_count": inactive_count,
        "insurer_list": insurer_list,
        "product_list": product_list,
        "category_list": category_list,
        "business_type_list": business_type_list,
        "zone_list": zone_list,
        "selected": selected,
    })


def health_rate_master_edit(request, pk):
    record = get_object_or_404(HealthRateMaster, pk=pk)

    if request.method == "POST":
        form = HealthRateForm(request.POST, instance=record)
        if form.is_valid():
            form.save()
            AuditLog.objects.create(
                user=request.user,
                action="HEALTH RATE EDIT",
                details=f"Edited HealthRateMaster #{record.pk} via form."
            )
            return redirect("health_rate_master")
    else:
        form = HealthRateForm(instance=record)

    return render(request, "edit_health_rate.html", {
        "form": form,
        "record": record,
    })


def health_rate_master_bulk_update(request):
    if request.method == "POST":
        raw_ids = request.POST.getlist("selected_rows")
        if not raw_ids:
            raw_ids = [request.POST.get("selected_rows", "")]

        ids = []
        for raw in raw_ids:
            for part in str(raw).split(","):
                part = part.strip()
                if part.isdigit():
                    ids.append(int(part))

        field_name = request.POST.get("update_field")
        new_value = request.POST.get("update_value", "").strip()

        if not ids or not field_name:
            return redirect("health_rate_master")

        if field_name not in ALLOWED_HEALTH_BULK_UPDATE_FIELDS:
            messages.error(
                request,
                f"Bulk update rejected: '{field_name}' is not an editable field. No rows were changed."
            )
            return redirect("health_rate_master")

        records = HealthRateMaster.objects.filter(id__in=ids)
        record_count = records.count()

        parsed_value = new_value
        if field_name == "status":
            parsed_value = new_value.strip().upper()
            valid_statuses = dict(HealthRateMaster.STATUS_CHOICES)
            if parsed_value not in valid_statuses:
                messages.error(
                    request,
                    f"Bulk update rejected: '{new_value}' is not a valid Status "
                    f"(must be one of: {', '.join(valid_statuses)}). No rows were changed."
                )
                return redirect("health_rate_master")
        elif field_name == "is_deleted":
            parsed_value = new_value.strip().upper()
            valid_deleted = dict(HealthRateMaster.IS_DELETED_CHOICES)
            if parsed_value not in valid_deleted:
                messages.error(
                    request,
                    f"Bulk update rejected: '{new_value}' is not a valid Is Deleted value "
                    f"(must be one of: {', '.join(valid_deleted)}). No rows were changed."
                )
                return redirect("health_rate_master")
        elif field_name in HEALTH_FLOAT_FIELDS:
            try:
                parsed_value = float(new_value) if new_value else None
            except ValueError:
                messages.error(
                    request,
                    f"Bulk update rejected: '{new_value}' is not a valid number for '{field_name}'. No rows were changed."
                )
                return redirect("health_rate_master")

        records.update(**{field_name: parsed_value})

        AuditLog.objects.create(
            user=request.user,
            action="HEALTH BULK UPDATE",
            details=f"Updated {record_count} Health rate rows. Changed '{field_name}' to '{new_value}'."
        )

    return redirect("health_rate_master")


def export_health_rates_xlsx(request):
    qs, _ = _build_health_rate_queryset(request)
    qs = qs.order_by("-id")

    wb = Workbook()
    ws = wb.active
    ws.title = "Health Rate Master"

    # Header row matches HealthRateMaster field names exactly (not friendly
    # Title Case) — same convention as Motor's export_rates_xlsx, and it means
    # this export can be edited and re-uploaded via Import Data's "Health
    # Rate Master (Rates CSV)" option without renaming any columns.
    ws.append([
        "id", "insurance_company", "product_name", "policy_category", "business_type",
        "plan_names", "min_deductible", "max_deductible", "min_sum_insured", "max_sum_insured",
        "min_age", "max_age", "pincode_zone", "payin_rate", "one_year_rate",
        "multi_year_2_rate", "multi_year_3_rate", "multi_year_4_rate", "multi_year_5_rate",
        "from_date", "to_date", "status", "is_deleted", "remarks",
    ])

    for r in qs.iterator(chunk_size=2000):
        ws.append([
            r.id, r.insurance_company, r.product_name, r.policy_category, r.business_type,
            r.plan_names, r.min_deductible, r.max_deductible, r.min_sum_insured, r.max_sum_insured,
            r.min_age, r.max_age, r.pincode_zone, r.payin_rate, r.one_year_rate,
            r.multi_year_2_rate, r.multi_year_3_rate, r.multi_year_4_rate, r.multi_year_5_rate,
            r.from_date.strftime("%Y-%m-%d") if r.from_date else "",
            r.to_date.strftime("%Y-%m-%d") if r.to_date else "",
            r.status, r.is_deleted, r.remarks or "",
        ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="health_rates_export.xlsx"'
    wb.save(response)
    return response
