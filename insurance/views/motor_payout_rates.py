"""Motor payout rates: the Motor "Check Eligibility" search page and its load-more endpoint."""
from django.shortcuts import render
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.db.models import Q, F
import json
from datetime import datetime
from ..models import RTOMaster, RateMaster, ProductMaster, SubProductMaster, FuelTypeMaster, MakeModelClassMaster
from ..services.make_model import NA_MAKE_MODEL_MAP, apply_make_model_filter, get_dynamic_make_model_class_list, get_make_mapping_context, resolve_make_model_groups, strict_match_in_cluster
from ..services.motor_payout import _collect_motor_payout_rows
from ..services.points_audit import _log_motor_points_search


# -------------------------
# MOTOR PAYOUT RATES
# -------------------------
MOTOR_PAYOUT_BATCH_SIZE = 50


MOTOR_PAYOUT_FIELD_NAMES = [
    "display_group_id", "status", "insurance_company", "tariff_range", "type",
    "rate", "flat_amount", "add_tnc"
]


def _build_motor_payout_queryset(request, target_date):
    """Filter-building logic shared by motor_payout_rates (first batch) and
    motor_payout_rates_more (subsequent batches), so both stay in sync."""
    qs = RateMaster.objects.select_related(
        "product", "sub_product", "policy_type", "fuel_type",
        "make_model_class", "is_ncb", "is_cpa", "is_zd"
    ).all()

    qs = qs.exclude(is_deleted="YES")
    qs = qs.filter(status__in=["ACTIVE", "INACTIVE"])

    product = (request.GET.get("product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_names = (request.GET.get("make_names") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    cc = (request.GET.get("cc") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sc = (request.GET.get("sc") or "").strip()
    mfg_year = (request.GET.get("mfg_year") or "").strip()
    is_zd = (request.GET.get("is_zd") or "").strip().upper()
    is_cpa = (request.GET.get("is_cpa") or "").strip().upper()
    is_ncb = (request.GET.get("is_ncb") or "").strip().upper()

    if target_date:
        qs = qs.filter(
            (Q(from_date__lte=target_date) | Q(from_date__isnull=True)) &
            (Q(to_date__gte=target_date) | Q(to_date__isnull=True))
        )

    if product:
        if str(product).isdigit():
            qs = qs.filter(product_id=product)
        else:
            qs = qs.filter(product__name__iexact=str(product).strip())

    qs = apply_make_model_filter(qs, product, make_model_class)

    if sub_product:
        if str(sub_product).isdigit():
            qs = qs.filter(sub_product_id=sub_product)
        else:
            qs = qs.filter(sub_product__name__iexact=str(sub_product).strip())

    if fuel:
        if str(fuel).isdigit():
            qs = qs.filter(fuel_type_id=fuel)
        else:
            qs = qs.filter(fuel_type__name__iexact=str(fuel).strip())

    matching_rto_names = []
    if rto_code:
        potential_rtos = RTOMaster.objects.filter(rto_cluster__icontains=rto_code)
        for rto_record in potential_rtos:
            if strict_match_in_cluster(rto_code, rto_record.rto_cluster):
                matching_rto_names.append(rto_record.rto_name.strip().upper())
        if matching_rto_names:
            q_rto = Q()
            for rto_name in matching_rto_names:
                q_rto |= Q(new_rto_list__icontains=rto_name)
            qs = qs.filter(q_rto)
        else:
            qs = qs.filter(new_rto_list__icontains=rto_code.strip())

    matching_make_groups = []
    if make_names:
        matching_make_groups = resolve_make_model_groups(make_names)
        if matching_make_groups:
            q_make = Q()
            for group_name in matching_make_groups:
                if group_name:
                    q_make |= Q(new_vehicle_makes__icontains=group_name.strip())
            qs = qs.filter(q_make)
        else:
            qs = qs.filter(new_vehicle_makes__icontains=make_names.strip())

    if cc and cc.isdigit():
        qs = qs.filter(
            (Q(cc_min__lte=int(cc)) | Q(cc_min__isnull=True)) &
            (Q(cc_max__gte=int(cc)) | Q(cc_max__isnull=True))
        )

    if sc:
        try:
            qs = qs.filter(
                (Q(sc_min__lte=float(sc)) | Q(sc_min__isnull=True)) &
                (Q(sc_max__gte=float(sc)) | Q(sc_max__isnull=True))
            )
        except ValueError:
            pass

    if mfg_year and mfg_year.isdigit() and target_date:
        try:
            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
            mfg_dt = datetime(int(mfg_year), 1, 1)
            exact_age = round((target_dt - mfg_dt).days / 365.25, 2)
            qs = qs.filter(
                (Q(vehicle_age_min__lte=exact_age) | Q(vehicle_age_min__isnull=True)) &
                (Q(vehicle_age_max__gte=exact_age) | Q(vehicle_age_max__isnull=True))
            )
        except ValueError:
            pass

    if is_zd:
        qs = qs.filter(Q(is_zd__code__iexact=is_zd) | Q(is_zd__code__iexact="NA"))
    if is_cpa:
        qs = qs.filter(Q(is_cpa__code__iexact=is_cpa) | Q(is_cpa__code__iexact="NA"))
    if is_ncb:
        qs = qs.filter(Q(is_ncb__code__iexact=is_ncb) | Q(is_ncb__code__iexact="NA"))

    qs = qs.order_by(
        F("pi_net_rate").desc(nulls_last=True),
        F("pi_od_rate").desc(nulls_last=True),
        F("pi_flat_amount").desc(nulls_last=True),
        "-id"
    )

    return qs, matching_rto_names, matching_make_groups, rto_code, make_names


def motor_payout_rates(request):
    has_searched = bool(request.GET)

    if has_searched:
        _log_motor_points_search(request)

    today_str = datetime.today().strftime("%Y-%m-%d")
    target_date = (request.GET.get("target_date") or today_str).strip()

    product = (request.GET.get("product") or "").strip()
    make_model_class = (request.GET.get("make_model_class") or "").strip()
    sub_product = (request.GET.get("sub_product") or "").strip()
    make_names = (request.GET.get("make_names") or "").strip()
    rto_code = (request.GET.get("rto_code") or "").strip()
    cc = (request.GET.get("cc") or "").strip()
    fuel = (request.GET.get("fuel") or "").strip()
    sc = (request.GET.get("sc") or "").strip()
    mfg_year = (request.GET.get("mfg_year") or "").strip()
    is_zd = (request.GET.get("is_zd") or "").strip().upper()
    is_cpa = (request.GET.get("is_cpa") or "").strip().upper()
    is_ncb = (request.GET.get("is_ncb") or "").strip().upper()

    results = []
    has_more = False

    # Nothing is queried until the user actually submits the search form —
    # a bare GET with no params at all (the very first page load) skips the
    # database entirely.
    if has_searched:
        qs, matching_rto_names, matching_make_groups, rto_code, make_names = _build_motor_payout_queryset(request, target_date)
        results, has_more = _collect_motor_payout_rows(
            qs, matching_rto_names, matching_make_groups, rto_code, make_names,
            skip=0, limit=MOTOR_PAYOUT_BATCH_SIZE
        )

    all_makes_json, class_makes_mapping_json, product_makes_mapping_json, all_makes = get_make_mapping_context()

    insurer_list = RateMaster.objects.exclude(is_deleted="YES").exclude(insurance_company="").values_list(
        "insurance_company", flat=True
    ).distinct().order_by("insurance_company")

    return render(request, "motor_payout_rates.html", {
        "has_searched": has_searched,
        "data": results,
        "total_found": len(results),
        "has_more": has_more,
        "insurer_list": insurer_list,
        "next_offset": len(results),
        "field_names": MOTOR_PAYOUT_FIELD_NAMES,
        "product_list": ProductMaster.objects.all().order_by("name"),
        "sub_product_list": SubProductMaster.objects.all().order_by("name"),
        "fuel_list": FuelTypeMaster.objects.all().order_by("name"),
        "make_model_class_list": get_dynamic_make_model_class_list(product),
        "all_makes_json": all_makes_json,
        "class_makes_mapping_json": class_makes_mapping_json,
        "product_makes_mapping_json": product_makes_mapping_json,

        "make_class_mapping_json": json.dumps(NA_MAKE_MODEL_MAP),
        "all_make_classes_json": json.dumps(list(MakeModelClassMaster.objects.exclude(name__iexact="NA").values('id', 'name'))),

        "selected": {
            "target_date": target_date,
            "product": product,
            "make_model_class": make_model_class,
            "sub_product": sub_product,
            "make_names": make_names,
            "rto_code": rto_code,
            "cc": cc,
            "fuel": fuel,
            "sc": sc,
            "mfg_year": mfg_year,
            "is_zd": is_zd,
            "is_cpa": is_cpa,
            "is_ncb": is_ncb,
        }
    })


def motor_payout_rates_more(request):
    """AJAX 'Load More' endpoint for motor_payout_rates — same filters
    (passed again as query params, exactly as the browser already has them
    in the URL), plus a `skip` telling it how many groups have already been
    shown. Returns a JSON payload with a pre-rendered HTML fragment of the
    next rows, rather than the whole page."""
    today_str = datetime.today().strftime("%Y-%m-%d")
    target_date = (request.GET.get("target_date") or today_str).strip()

    try:
        skip = int(request.GET.get("skip", 0))
    except (TypeError, ValueError):
        skip = 0
    skip = max(skip, 0)

    qs, matching_rto_names, matching_make_groups, rto_code, make_names = _build_motor_payout_queryset(request, target_date)
    results, has_more = _collect_motor_payout_rows(
        qs, matching_rto_names, matching_make_groups, rto_code, make_names,
        skip=skip, limit=MOTOR_PAYOUT_BATCH_SIZE
    )

    rows_html = render_to_string(
        "_motor_payout_rates_rows.html",
        {"data": results, "field_names": MOTOR_PAYOUT_FIELD_NAMES},
        request=request,
    )

    return JsonResponse({
        "html": rows_html,
        "has_more": has_more,
        "next_offset": skip + len(results),
    })
