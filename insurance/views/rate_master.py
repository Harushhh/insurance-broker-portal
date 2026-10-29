"""Rate Master: the grouped dashboard, cluster detail / update, Excel export, edit form and bulk update."""
from django.contrib import messages
from django.utils import timezone
from django.shortcuts import render, redirect
from django.http import HttpResponse, JsonResponse
from django.db.models import Q
from django.core.paginator import Paginator
import csv
import json
from collections import defaultdict
from ..models import RTOMaster, MakeModelMaster, RateMaster, YesNoNAMaster, ProductMaster, SubProductMaster, PolicyTypeMaster, FuelTypeMaster, MakeModelClassMaster, AuditLog
from ..forms import RATE_DECIMAL_FIELDS, RateForm
from ..services.common import parse_yes_no_na, split_csv_values, unique_join
from ..services.make_model import NA_MAKE_MODEL_MAP, apply_make_model_filter, get_dynamic_make_model_class_list, get_translated_make_model, resolve_make_model_groups, strict_match_in_cluster
from ..services.rate_master import apply_range_filter


# -------------------------
# Dashboard (GROUPED view)
# -------------------------
# Choices for the "rows per page" selector on dashboard.html -- default is
# first, and any page_size not in this list (missing, malformed, or a
# tampered query param past the 200 cap) falls back to it rather than
# serving an unbounded page.
DASHBOARD_PAGE_SIZE_OPTIONS = (50, 100, 200)


DASHBOARD_BATCH_SIZE = DASHBOARD_PAGE_SIZE_OPTIONS[0]


# The Motor Rate Master filter/table show "UNLOCKED"/"LOCKED" (see
# dashboard.html), but RateMaster.status only ever stores "ACTIVE"/"INACTIVE"
# (RateMaster.STATUS_CHOICES) -- translate the UI term to the stored one
# before filtering.
RATE_STATUS_UI_TO_DB = {"UNLOCKED": "ACTIVE", "LOCKED": "INACTIVE"}


def dashboard(request):
    qs = RateMaster.objects.select_related(
        "group", "product", "sub_product", "policy_type", "fuel_type",
        "make_model_class", "is_ncb", "is_cpa", "is_zd"
    ).all()

    q = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip()
    is_deleted_filter = (request.GET.get("is_deleted") or "").strip().upper()
    created_date = (request.GET.get("created_date") or "").strip()
    updated_date = (request.GET.get("updated_date") or "").strip()
    date_range = (request.GET.get("date_range") or "").strip()

    insurance_company = (request.GET.get("insurance_company") or "").strip()
    product = (request.GET.get("product") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    make_model_code = (request.GET.get("make_model_code") or "").strip()
    # Named *_filter to avoid clashing with the `for rto_name in matching_rto_names`
    # / `for make_name in matching_make_names` loop variables further below —
    # Python for-loops don't scope their variable, so reusing these names here
    # would get silently overwritten by the last value of those loops.
    rto_name_filter = (request.GET.get("rto_name") or "").strip()
    make_model_name_filter = (request.GET.get("make_model_name") or "").strip()
    age_range = (request.GET.get("age_range") or "").strip()
    cc_range = (request.GET.get("cc_range") or "").strip()
    sc_range = (request.GET.get("sc_range") or "").strip()
    is_zd = (request.GET.get("is_zd") or "").strip()
    is_ncb = (request.GET.get("is_ncb") or "").strip()
    is_cpa = (request.GET.get("is_cpa") or "").strip()

    try:
        page_size = int(request.GET.get("page_size") or DASHBOARD_BATCH_SIZE)
    except ValueError:
        page_size = DASHBOARD_BATCH_SIZE
    if page_size not in DASHBOARD_PAGE_SIZE_OPTIONS:
        page_size = DASHBOARD_BATCH_SIZE

    filter_count = sum(1 for v in [
        q, status_filter, is_deleted_filter, created_date, updated_date, date_range,
        insurance_company, product, fuel, sub_product, make_model_class,
        rto_code, make_model_code, rto_name_filter, make_model_name_filter, age_range, cc_range, sc_range,
        is_zd, is_ncb, is_cpa,
    ] if v)

    if filter_count < 2:
        # No filter selected yet -- skip the expensive full-table query/scan
        # entirely and just render the filter form with an empty result set.
        return render(request, "dashboard.html", {
            "data": [],
            "page_obj": None,
            "elided_page_range": [],
            "field_names": [],
            "bulk_update_fields": ALLOWED_BULK_UPDATE_FIELDS,
            "total": 0,
            "active_count": 0,
            "inactive_count": 0,
            "insurance_company_list": RateMaster.objects.exclude(insurance_company="").values_list(
                "insurance_company", flat=True
            ).distinct().order_by("insurance_company"),
            "product_list": ProductMaster.objects.all().order_by("name"),
            "fuel_list": FuelTypeMaster.objects.all().order_by("name"),
            "sub_product_list": SubProductMaster.objects.all().order_by("name"),
            "make_model_class_list": get_dynamic_make_model_class_list(product),
            "yes_no_na_list": YesNoNAMaster.objects.all().order_by("code"),
            "make_model_name_list": MakeModelMaster.objects.values_list(
                "make_model_name", flat=True
            ).distinct().order_by("make_model_name"),
            "rto_name_list": RTOMaster.objects.values_list(
                "rto_name", flat=True
            ).distinct().order_by("rto_name"),
            "make_class_mapping_json": json.dumps(NA_MAKE_MODEL_MAP),
            "all_make_classes_json": json.dumps(list(MakeModelClassMaster.objects.exclude(name__iexact="NA").values('id', 'name'))),
            "selected": {
                "q": q,
                "status": status_filter,
                "is_deleted": is_deleted_filter,
                "created_date": created_date,
                "updated_date": updated_date,
                "insurance_company": insurance_company,
                "product": product,
                "fuel": fuel,
                "sub_product": sub_product,
                "make_model_class": make_model_class,
                "date_range": date_range,
                "rto_code": rto_code,
                "make_model_code": make_model_code,
                "rto_name": rto_name_filter,
                "make_model_name": make_model_name_filter,
                "age_range": age_range,
                "cc_range": cc_range,
                "sc_range": sc_range,
                "is_zd": is_zd,
                "is_ncb": is_ncb,
                "is_cpa": is_cpa,
                "page_size": page_size,
            },
            "page_size_options": DASHBOARD_PAGE_SIZE_OPTIONS,
            "no_filter_selected": True,
        })

    if q:
        group_ids = [int(gid.strip()) for gid in q.split(",") if gid.strip().isdigit()]
        if group_ids:
            qs = qs.filter(Q(group_id__in=group_ids) | Q(group_id__isnull=True, id__in=group_ids))
        else:
            qs = qs.none()

    if status_filter:
        qs = qs.filter(status=RATE_STATUS_UI_TO_DB.get(status_filter, status_filter))

    if is_deleted_filter:
        qs = qs.filter(is_deleted=is_deleted_filter)
    if created_date:
        qs = qs.filter(created_at__date=created_date)
    if updated_date:
        qs = qs.filter(updated_at__date=updated_date)

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

    if insurance_company:
        qs = qs.filter(insurance_company=insurance_company)
    if product:
        qs = qs.filter(product_id=product)
    if fuel:
        qs = qs.filter(fuel_type_id=fuel)
    if sub_product:
        qs = qs.filter(sub_product_id=sub_product)

    matching_rto_names = []
    if rto_code:
        for rto_record in RTOMaster.objects.filter(rto_cluster__icontains=rto_code):
            if strict_match_in_cluster(rto_code, rto_record.rto_cluster):
                matching_rto_names.append(rto_record.rto_name.strip().upper())
        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names:
                q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else:
            qs = qs.none()

    matching_make_names = []
    if make_model_code:
        matching_make_names = resolve_make_model_groups(make_model_code)
        if matching_make_names:
            q_make = Q()
            for make_name in matching_make_names:
                q_make |= Q(new_vehicle_makes__icontains=make_name)
            qs = qs.filter(q_make)
        else:
            qs = qs.none()

    # Direct substring filters on the raw display columns themselves, distinct
    # from rto_code/make_model_code above which resolve a code through the
    # RTOMaster/MakeModelMaster cluster tables first.
    if rto_name_filter:
        qs = qs.filter(new_rto_list__icontains=rto_name_filter)
    if make_model_name_filter:
        qs = qs.filter(new_vehicle_makes__icontains=make_model_name_filter)

    qs = apply_make_model_filter(qs, product, make_model_class)
    qs = apply_range_filter(qs, "vehicle_age_min", "vehicle_age_max", age_range)
    qs = apply_range_filter(qs, "cc_min", "cc_max", cc_range)
    qs = apply_range_filter(qs, "sc_min", "sc_max", sc_range)

    if is_zd:
        qs = qs.filter(is_zd__code=is_zd)
    if is_ncb:
        qs = qs.filter(is_ncb__code=is_ncb)
    if is_cpa:
        qs = qs.filter(is_cpa__code=is_cpa)

    active_count = qs.filter(status="ACTIVE").count()
    inactive_count = qs.filter(status="INACTIVE").count()

    qs = qs.order_by("-id")

    matching_gids_set = set()
    ordered_gids = []

    for row in qs.iterator(chunk_size=2000):
        if rto_code and matching_rto_names:
            if not row.new_rto_list:
                continue
            row_rtos = [x.strip().upper() for x in row.new_rto_list.split(",")]
            if not any(x in matching_rto_names for x in row_rtos):
                continue

        if make_model_code and matching_make_names:
            if not row.new_vehicle_makes:
                continue
            row_makes = [x.strip().upper() for x in row.new_vehicle_makes.split(",")]
            if not any(x in matching_make_names for x in row_makes):
                continue

        gid = row.group_id if row.group_id is not None else row.id
        if gid not in matching_gids_set:
            matching_gids_set.add(gid)
            ordered_gids.append(gid)

    paginator = Paginator(ordered_gids, page_size)
    try:
        page_number = int(request.GET.get("page") or 1)
    except ValueError:
        page_number = 1
    page_obj = paginator.get_page(page_number)
    page_gids = list(page_obj.object_list)
    elided_page_range = list(paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1))

    buckets = defaultdict(list)
    if page_gids:
        full_group_qs = RateMaster.objects.select_related(
            "group", "product", "sub_product", "policy_type", "fuel_type",
            "make_model_class", "is_ncb", "is_cpa", "is_zd"
        ).filter(Q(group_id__in=page_gids) | Q(id__in=page_gids))

        for row in full_group_qs.iterator(chunk_size=2000):
            gid = row.group_id if row.group_id is not None else row.id
            buckets[gid].append(row)

    grouped_rows = []
    for gid in page_gids:
        rows = buckets.get(gid, [])
        if not rows:
            continue

        first = rows[0]
        all_rtos = split_csv_values([r.new_rto_list for r in rows])
        all_fuels = [r.fuel_type.name for r in rows if r.fuel_type]

        first.display_group_id = gid
        first.display_rto_list = unique_join(all_rtos)
        first.display_fuel_types = unique_join(all_fuels)

        mmc_name = first.make_model_class.name.strip().upper() if first.make_model_class else "NA"
        
        if mmc_name in ["NA", ""]:
            prod_name = first.product.name if first.product else ""
            translated_name = get_translated_make_model(prod_name)
            if translated_name:
                first.make_model_class = MakeModelClassMaster(name=translated_name)
                first.display_make_model_class = translated_name
            else:
                first.display_make_model_class = "NA"
        else:
            first.display_make_model_class = first.make_model_class.name

        age_min_str = f"{first.vehicle_age_min:.2f}" if first.vehicle_age_min is not None else ""
        age_max_str = f"{first.vehicle_age_max:.2f}" if first.vehicle_age_max is not None else ""
        first.age_range = f"{age_min_str} - {age_max_str}" if (age_min_str or age_max_str) else ""

        cc_min_str = f"{first.cc_min:.2f}" if first.cc_min is not None else ""
        cc_max_str = f"{first.cc_max:.2f}" if first.cc_max is not None else ""
        first.cc_range = f"{cc_min_str} - {cc_max_str}" if (cc_min_str or cc_max_str) else ""

        sc_min_str = f"{first.sc_min:.2f}" if first.sc_min is not None else ""
        sc_max_str = f"{first.sc_max:.2f}" if first.sc_max is not None else ""
        first.sc_range = f"{sc_min_str} - {sc_max_str}" if (sc_min_str or sc_max_str) else ""

        grouped_rows.append(first)

    field_names = [
        "display_group_id", "new_vehicle_makes", "display_rto_list",
        "insurer_vertical", "insurance_company", "product", "sub_product", "policy_type",
        "display_fuel_types", "display_make_model_class", "age_range",
        "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
        "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type", "tariff_min", "tariff_max",
        "is_ncb", "is_cpa", "is_zd", "cc_range", "from_date", "to_date", "sc_range",
        "user_id", "veh_use", "remarks", "add_tnc",
        "po_type", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount",
        "status", "is_deleted", "updated_at"
    ]

    insurance_company_list = RateMaster.objects.exclude(insurance_company="").values_list(
        "insurance_company", flat=True
    ).distinct().order_by("insurance_company")
    product_list = ProductMaster.objects.all().order_by("name")
    fuel_list = FuelTypeMaster.objects.all().order_by("name")
    sub_product_list = SubProductMaster.objects.all().order_by("name")
    yes_no_na_list = YesNoNAMaster.objects.all().order_by("code")

    return render(request, "dashboard.html", {
        "data": grouped_rows,
        "page_obj": page_obj,
        "elided_page_range": elided_page_range,
        "field_names": field_names,
        "bulk_update_fields": ALLOWED_BULK_UPDATE_FIELDS,
        "total": paginator.count,
        "active_count": active_count,
        "inactive_count": inactive_count,
        "insurance_company_list": insurance_company_list,
        "product_list": product_list,
        "fuel_list": fuel_list,
        "sub_product_list": sub_product_list,
        "make_model_class_list": get_dynamic_make_model_class_list(product),
        "yes_no_na_list": yes_no_na_list,
        "make_model_name_list": MakeModelMaster.objects.values_list(
            "make_model_name", flat=True
        ).distinct().order_by("make_model_name"),
        "rto_name_list": RTOMaster.objects.values_list(
            "rto_name", flat=True
        ).distinct().order_by("rto_name"),

        "make_class_mapping_json": json.dumps(NA_MAKE_MODEL_MAP),
        "all_make_classes_json": json.dumps(list(MakeModelClassMaster.objects.exclude(name__iexact="NA").values('id', 'name'))),

        "selected": {
            "q": q,
            "status": status_filter,
            "is_deleted": is_deleted_filter,
            "created_date": created_date,
            "updated_date": updated_date,
            "insurance_company": insurance_company,
            "product": product,
            "fuel": fuel,
            "sub_product": sub_product,
            "make_model_class": make_model_class,
            "date_range": date_range,
            "rto_code": rto_code,
            "make_model_code": make_model_code,
            "rto_name": rto_name_filter,
            "make_model_name": make_model_name_filter,
            "age_range": age_range,
            "cc_range": cc_range,
            "sc_range": sc_range,
            "is_zd": is_zd,
            "is_ncb": is_ncb,
            "is_cpa": is_cpa,
            "page_size": page_size,
        },
        "page_size_options": DASHBOARD_PAGE_SIZE_OPTIONS,
    })


def get_cluster_details(request):
    """Returns the expanded cluster (individual RTO names or vehicle makes)
    behind one group name shown in the Rate Master dashboard's Display RTO
    List / New Vehicle Makes columns — powers the click-to-expand popup."""
    cluster_type = (request.GET.get("type") or "").strip().lower()
    name = (request.GET.get("name") or "").strip()

    if not name or cluster_type not in ("rto", "make"):
        return JsonResponse({"success": False, "message": "Invalid request."})

    # __iexact: RateMaster's own new_rto_list/new_vehicle_makes tokens have
    # drifted in casing across different insurers' imports over time (e.g.
    # "Private_Car_All" vs. the master row's "Private_Car_ALL"), and the
    # mapping engine already resolves these names case-insensitively
    # (see check_resolved_cluster_match) -- an exact-case lookup here just
    # meant the popup disagreed with what the engine actually matches on.
    if cluster_type == "rto":
        obj = RTOMaster.objects.filter(rto_name__iexact=name).first()
        cluster = obj.rto_cluster if obj else None
    else:
        obj = MakeModelMaster.objects.filter(make_model_name__iexact=name).first()
        cluster = obj.make_model_cluster if obj else None

    if not obj:
        return JsonResponse({"success": False, "message": f'No cluster found for "{name}".'})

    items = [x.strip() for x in (cluster or "").split(",") if x.strip()]
    return JsonResponse({"success": True, "type": cluster_type, "name": name, "items": items})


def update_cluster_details(request):
    """Saves an edited cluster (from the Rate Master dashboard's popup) back
    onto the matching RTOMaster/MakeModelMaster row. Note this is master
    reference data shared by every page that resolves an RTO/make code
    through it (motor_payout_rates, policy_lock_checker, the dashboard's own
    rto_code/make_model_code filters, ...) — editing it here changes matching
    everywhere, not just this dashboard."""
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Invalid request method."})

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"success": False, "message": "Invalid request body."})

    cluster_type = (data.get("type") or "").strip().lower()
    name = (data.get("name") or "").strip()
    raw_items = data.get("items")

    if not name or cluster_type not in ("rto", "make") or not isinstance(raw_items, list):
        return JsonResponse({"success": False, "message": "Invalid request."})

    cleaned_items = []
    seen = set()
    for item in raw_items:
        item = str(item).strip()
        if not item or item.upper() in seen:
            continue
        seen.add(item.upper())
        cleaned_items.append(item)

    cluster_value = ", ".join(cleaned_items)

    if cluster_type == "rto":
        obj = RTOMaster.objects.filter(rto_name__iexact=name).first()
        if not obj:
            return JsonResponse({"success": False, "message": f'No RTO group found for "{name}".'})
        obj.rto_cluster = cluster_value
        obj.save(update_fields=["rto_cluster", "updated_at"])
    else:
        obj = MakeModelMaster.objects.filter(make_model_name__iexact=name).first()
        if not obj:
            return JsonResponse({"success": False, "message": f'No vehicle make group found for "{name}".'})
        obj.make_model_cluster = cluster_value
        obj.save(update_fields=["make_model_cluster", "updated_at"])

    return JsonResponse({"success": True, "type": cluster_type, "name": name, "items": cleaned_items})


# -------------------------
# Export UNGROUPED to CSV (Perfect Match 1:1 Schema Update)
# -------------------------
def export_rates_xlsx(request):
    qs = RateMaster.objects.select_related(
        "product", "sub_product", "policy_type", "fuel_type", "make_model_class", "is_ncb", "is_cpa", "is_zd"
    ).all()

    q = (request.GET.get("q") or "").strip()
    status_filter = (request.GET.get("status") or "").strip()
    is_deleted_filter = (request.GET.get("is_deleted") or "").strip().upper()
    created_date = (request.GET.get("created_date") or "").strip()
    updated_date = (request.GET.get("updated_date") or "").strip()
    date_range = (request.GET.get("date_range") or "").strip()

    insurance_company = (request.GET.get("insurance_company") or "").strip()
    product = (request.GET.get("product") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    make_model_code = (request.GET.get("make_model_code") or "").strip()
    age_range = (request.GET.get("age_range") or "").strip()
    cc_range = (request.GET.get("cc_range") or "").strip()
    sc_range = (request.GET.get("sc_range") or "").strip()
    is_zd = (request.GET.get("is_zd") or "").strip()
    is_ncb = (request.GET.get("is_ncb") or "").strip()
    is_cpa = (request.GET.get("is_cpa") or "").strip()

    if q:
        group_ids = [int(gid.strip()) for gid in q.split(",") if gid.strip().isdigit()]
        if group_ids:
            qs = qs.filter(Q(group_id__in=group_ids) | Q(group_id__isnull=True, id__in=group_ids))
        else:
            qs = qs.none()

    if status_filter:
        qs = qs.filter(status=RATE_STATUS_UI_TO_DB.get(status_filter, status_filter))
    if is_deleted_filter:
        qs = qs.filter(is_deleted=is_deleted_filter)
    if created_date:
        qs = qs.filter(created_at__date=created_date)
    if updated_date:
        qs = qs.filter(updated_at__date=updated_date)

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

    if insurance_company:
        qs = qs.filter(insurance_company=insurance_company)
    if product:
        qs = qs.filter(product_id=product)
    if fuel:
        qs = qs.filter(fuel_type_id=fuel)
    if sub_product:
        qs = qs.filter(sub_product_id=sub_product)

    matching_rto_names = []
    if rto_code:
        for rto_record in RTOMaster.objects.filter(rto_cluster__icontains=rto_code):
            if strict_match_in_cluster(rto_code, rto_record.rto_cluster):
                matching_rto_names.append(rto_record.rto_name.strip().upper())
        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names:
                q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else:
            qs = qs.none()

    matching_make_names = []
    if make_model_code:
        matching_make_names = resolve_make_model_groups(make_model_code)
        if matching_make_names:
            q_make = Q()
            for make_name in matching_make_names:
                q_make |= Q(new_vehicle_makes__icontains=make_name)
            qs = qs.filter(q_make)
        else:
            qs = qs.none()

    qs = apply_make_model_filter(qs, product, make_model_class)
    qs = apply_range_filter(qs, "vehicle_age_min", "vehicle_age_max", age_range)
    qs = apply_range_filter(qs, "cc_min", "cc_max", cc_range)
    qs = apply_range_filter(qs, "sc_min", "sc_max", sc_range)

    if is_zd:
        qs = qs.filter(is_zd__code=is_zd)
    if is_ncb:
        qs = qs.filter(is_ncb__code=is_ncb)
    if is_cpa:
        qs = qs.filter(is_cpa__code=is_cpa)

    qs = qs.order_by("-id")

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="rates_export.csv"'

    writer = csv.writer(response)
    
    # Header aligned perfectly with the keys processed during `api_upload_chunk`
    writer.writerow([
        "id", "group_id", "status", "is_deleted", "insurance_company", "insurer_vertical", 
        "product", "sub_product", "policy_type", "fuel_type", "make_model_class",
        "new_vehicle_makes", "new_rto_list", "vehicle_age_min", "vehicle_age_max", 
        "cc_min", "cc_max", "sc_min", "sc_max", "pi_od_rate", "pi_tp_rate", 
        "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5", "pi_net_rate", "pi_flat_amount", 
        "pi_vli", "pi_type", "tariff_min", "tariff_max", "is_ncb", "is_cpa", "is_zd", 
        "from_date", "to_date", "user_id", "veh_use", "add_tnc", "remarks",
        "po_type", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount", "created_at"
    ])

    for r in qs.iterator(chunk_size=2000):
        writer.writerow([
            r.id,
            r.group_id,
            r.status,
            r.is_deleted,
            r.insurance_company,
            r.insurer_vertical,
            r.product.name if r.product else "",
            r.sub_product.name if r.sub_product else "",
            r.policy_type.name if r.policy_type else "",
            r.fuel_type.name if r.fuel_type else "",
            r.make_model_class.name if r.make_model_class else "",
            r.new_vehicle_makes,
            r.new_rto_list,
            r.vehicle_age_min,
            r.vehicle_age_max,
            r.cc_min,
            r.cc_max,
            r.sc_min,
            r.sc_max,
            r.pi_od_rate,
            r.pi_tp_rate,
            r.pi_tp_2,
            r.pi_tp_3,
            r.pi_tp_4,
            r.pi_tp_5,
            r.pi_net_rate,
            r.pi_flat_amount,
            r.pi_vli,
            r.pi_type,
            r.tariff_min,
            r.tariff_max,
            r.is_ncb.code if r.is_ncb else "",
            r.is_cpa.code if r.is_cpa else "",
            r.is_zd.code if r.is_zd else "",
            r.from_date,
            r.to_date,
            r.user_id,
            r.veh_use,
            r.add_tnc,
            r.remarks,
            r.po_type,
            r.po_od_rate,
            r.po_tp_rate,
            r.po_net_rate,
            r.po_flat_amount,
            r.created_at
        ])

    return response


# -------------------------
# Edit Rate Form
# -------------------------
def edit_rate(request, group_id):
    # group_id here is overloaded: it's either a real RateGroup id (edit the
    # whole group), or — when a caller couldn't resolve a group (e.g. some
    # "Fix Mapping" links on Rate Master Health) — a raw RateMaster.id for a
    # single ungrouped row. These two id spaces are independent auto-increment
    # sequences, so they collide: as of this writing 1,898 RateGroup ids also
    # happen to equal the primary key of some unrelated RateMaster row
    # belonging to a *different* group. Q(group_id=X) | Q(id=X) would match
    # BOTH in that case and silently fold a stranger row into this edit/bulk
    # update. Only fall back to the raw-id interpretation when no real group
    # matches, so the two interpretations can never mix.
    records = RateMaster.objects.filter(group_id=group_id)
    if not records.exists():
        records = RateMaster.objects.filter(id=group_id)
    if not records.exists():
        return HttpResponse("Record not found.", status=404)

    first_record = records.first()
    record_count = records.count()

    all_rtos = split_csv_values([r.new_rto_list for r in records])
    unique_rto_list = list(set([r for r in all_rtos if r]))
    rto_display = unique_join(all_rtos)

    all_makes = split_csv_values([r.new_vehicle_makes for r in records])
    unique_makes_list = list(set([m for m in all_makes if m]))

    initial_data = {
        "new_rto_list": unique_rto_list,
        "new_vehicle_makes": unique_makes_list,
        "insurance_company": first_record.insurance_company,
        "insurer_vertical": first_record.insurer_vertical,
        "status": first_record.status,
        "is_deleted": first_record.is_deleted,
        "product": first_record.product_id,
        "sub_product": first_record.sub_product_id,
        "policy_type": first_record.policy_type_id,
        "fuel_type": first_record.fuel_type_id,
        "make_model_class": first_record.make_model_class_id,
        "is_ncb": first_record.is_ncb_id,
        "is_cpa": first_record.is_cpa_id,
        "is_zd": first_record.is_zd_id,
    }

    if request.method == "POST":
        # initial_data must be passed here too, not just on GET — RateForm.__init__
        # builds the new_rto_list/new_vehicle_makes MultipleChoiceField choices from
        # kwargs["initial"] plus the single `instance` record. Without it, POST-time
        # choices only reflect first_record's own values, so submitting the full
        # group-wide selection the page actually shows fails validation on every
        # value that isn't also on first_record specifically — silently discarding
        # the whole edit.
        form = RateForm(request.POST, instance=first_record, initial=initial_data)
        if form.is_valid():
            # Only write fields the user actually changed from what the form
            # showed them — not the full cleaned_data for every field on the
            # model. This matters most for new_rto_list/new_vehicle_makes:
            # their *displayed* value is the union of every record's own
            # value across the whole group (see unique_rto_list/unique_makes_list
            # above), which is correct for showing "what this group currently
            # covers" but is NOT a value that's safe to blast onto every
            # record — new_rto_list in particular is deliberately excluded
            # from GROUP_FIELDS because individual records in a group
            # routinely carry different single RTOs. Writing the full
            # cleaned_data unconditionally (the old behavior) meant editing
            # any single field — e.g. just a rate — silently overwrote every
            # record's own RTO/vehicle-makes with that group-wide union,
            # destroying the per-record distinction. form.changed_data is
            # Django's built-in "differs from what was shown" check, so a
            # field is only written when the user genuinely touched it.
            update_data = {field: form.cleaned_data[field] for field in form.changed_data}
            if update_data:
                update_data["updated_at"] = timezone.now()
                records.update(**update_data)
                AuditLog.objects.create(
                    user=request.user,
                    action="MANUAL EDIT",
                    details=(
                        f"Edited Group/Record ID {group_id} via form. Updated {record_count} rows. "
                        f"Changed fields: {', '.join(sorted(update_data.keys()))}."
                    )
                )
            return redirect("dashboard")
    else:
        form = RateForm(instance=first_record, initial=initial_data)

    return render(request, "edit_rate.html", {
        "form": form,
        "count": records.count(),
        "group_id": group_id,
        "rto_display": rto_display
    })


# -------------------------
# BULK UPDATE RATES
# -------------------------
# Only these RateMaster fields may be touched via bulk update — update_field
# comes straight from the POST body, so anything not on this list (id,
# group_id, created_at, ...) must be rejected rather than passed through to
# records.update(**{field_name: ...}). Ordered (not a set) because this same
# tuple also drives the "Field to Change" dropdown on the dashboard, so its
# order is the order options appear in — keep the two in sync by construction
# rather than maintaining a second field list in the view/template.
ALLOWED_BULK_UPDATE_FIELDS = (
    "new_vehicle_makes", "product", "sub_product", "policy_type", "fuel_type",
    "make_model_class", "is_ncb", "is_cpa", "is_zd", "status", "is_deleted",
    "vehicle_age_min", "vehicle_age_max", "cc_min", "cc_max", "user_id",
    "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
    "pi_net_rate", "pi_flat_amount", "pi_vli", "tariff_min", "tariff_max",
    "sc_min", "sc_max", "from_date", "to_date",
    "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount",
    "insurance_company", "insurer_vertical", "pi_type", "po_type", "veh_use",
    "add_tnc", "remarks",
)


def bulk_update_rates(request):
    if request.method == "POST":
        # 1. Safely extract group IDs regardless of how JS structured the payload
        raw_groups = request.POST.getlist("selected_groups")
        if not raw_groups:
            raw_groups = request.POST.getlist("selected_groups[]")
        if not raw_groups:
            raw_groups = [request.POST.get("selected_groups", "")]

        group_ids = []
        for g in raw_groups:
            for part in str(g).split(","):
                part = part.strip()
                if part.isdigit():
                    group_ids.append(int(part))

        field_name = request.POST.get("update_field")
        new_value = request.POST.get("update_value", "").strip()

        if not group_ids or not field_name:
            return redirect("dashboard")

        if field_name not in ALLOWED_BULK_UPDATE_FIELDS:
            messages.error(
                request,
                f"Bulk update rejected: '{field_name}' is not an editable field. No rows were changed."
            )
            return redirect("dashboard")

        # 2. Fetch all exact records mapped to the selected rows.
        # Same group_id/id ambiguity as edit_rate (see its comment): resolve
        # each selected id against group_id first, and only treat it as a raw
        # RateMaster.id if no group has that id, so a group id that happens to
        # collide with an unrelated row's primary key can't pull that row in.
        ids_matching_a_group = set(
            RateMaster.objects.filter(group_id__in=group_ids).values_list("group_id", flat=True).distinct()
        )
        leftover_ids = [gid for gid in group_ids if gid not in ids_matching_a_group]
        records = RateMaster.objects.filter(Q(group_id__in=group_ids) | Q(id__in=leftover_ids))
        record_count = records.count()

        # 3. Safely cast the incoming text value into the correct Python/Django Database Type
        parsed_value = new_value
        if field_name == "product":
            parsed_value, _ = ProductMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "sub_product":
            parsed_value, _ = SubProductMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "policy_type":
            parsed_value, _ = PolicyTypeMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "fuel_type":
            parsed_value, _ = FuelTypeMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name == "make_model_class":
            parsed_value, _ = MakeModelClassMaster.objects.get_or_create(name=new_value) if new_value else (None, False)
        elif field_name in ["is_ncb", "is_cpa", "is_zd"]:
            parsed_value = parse_yes_no_na(new_value)
        elif field_name == "status":
            parsed_value = str(new_value).strip().upper()
            valid_statuses = dict(RateMaster.STATUS_CHOICES)
            if parsed_value not in valid_statuses:
                messages.error(
                    request,
                    f"Bulk update rejected: '{new_value}' is not a valid Status "
                    f"(must be one of: {', '.join(valid_statuses)}). No rows were changed."
                )
                return redirect("dashboard")
        elif field_name == "is_deleted":
            parsed_value = str(new_value).strip().upper()
            valid_deleted = dict(RateMaster.IS_DELETED_CHOICES)
            if parsed_value not in valid_deleted:
                messages.error(
                    request,
                    f"Bulk update rejected: '{new_value}' is not a valid Is Deleted value "
                    f"(must be one of: {', '.join(valid_deleted)}). No rows were changed."
                )
                return redirect("dashboard")
        elif field_name == "user_id":
            parsed_value = int(float(new_value)) if new_value else None
        elif field_name in [
            "vehicle_age_min", "vehicle_age_max", "cc_min", "cc_max",
            "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
            "pi_net_rate", "pi_flat_amount", "pi_vli", "tariff_min", "tariff_max",
            "sc_min", "sc_max", "po_od_rate", "po_tp_rate", "po_net_rate", "po_flat_amount"
        ]:
            parsed_value = float(new_value) if new_value else None
            if parsed_value is not None and field_name in RATE_DECIMAL_FIELDS:
                parsed_value = round(parsed_value, 2)

        # 4. Perform ultra-fast vectorized update to the DB
        records.update(**{field_name: parsed_value, "updated_at": timezone.now()})

        # 5. Log the action
        AuditLog.objects.create(
            user=request.user,
            action="BULK UPDATE",
            details=f"Updated {record_count} rows. Changed '{field_name}' to '{new_value}'."
        )

    return redirect("dashboard")
