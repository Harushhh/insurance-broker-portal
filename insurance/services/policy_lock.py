"""The policy lock checker search shared by the page and its API endpoint."""
from django.db.models import Q, F
from datetime import datetime
from ..models import RTOMaster, RateMaster, MakeModelClassMaster, LockedPolicy
from .make_model import apply_make_model_filter, get_translated_make_model, resolve_make_model_groups, strict_match_in_cluster
from .points_audit import _log_motor_points_search


# -------------------------
# POLICY LOCK CHECKER
# -------------------------
def _run_policy_lock_checker_search(request):
    """
    Core search/filter/dedup logic shared by the policy_lock_checker HTML
    page and PolicyLockCheckerAPIView. Returns (results, has_searched, selected)
    -- everything the two callers need, minus the HTML-only dropdown lists.
    """
    has_searched = bool(request.GET)

    if has_searched:
        _log_motor_points_search(request)

    qs = RateMaster.objects.select_related(
        "product", "sub_product", "policy_type", "fuel_type",
        "make_model_class", "is_ncb", "is_cpa", "is_zd"
    ).all()

    qs = qs.exclude(is_deleted="YES")
    qs = qs.filter(status__in=["ACTIVE", "INACTIVE"])

    today_str = datetime.today().strftime("%Y-%m-%d")
    target_date = (request.GET.get("target_date") or today_str).strip()

    vehicle_no = (request.GET.get("vehicle_no") or "").strip()
    policy_holder_name = (request.GET.get("policy_holder_name") or "").strip()
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

    if cc:
        try:
            cc_val = float(cc)
            qs = qs.filter(
                (Q(cc_min__lte=cc_val) | Q(cc_min__isnull=True)) &
                (Q(cc_max__gte=cc_val) | Q(cc_max__isnull=True))
            )
        except ValueError:
            pass

    if sc:
        try:
            sc_val = float(sc)
            qs = qs.filter(
                (Q(sc_min__lte=sc_val) | Q(sc_min__isnull=True)) &
                (Q(sc_max__gte=sc_val) | Q(sc_max__isnull=True))
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
        F("po_net_rate").desc(nulls_last=True),
        F("po_od_rate").desc(nulls_last=True),
        F("po_flat_amount").desc(nulls_last=True),
        "-id"
    )

    results = []
    seen_groups = set()

    if has_searched:
        for row in qs.iterator(chunk_size=2000):
            if rto_code and matching_rto_names:
                if not row.new_rto_list:
                    continue
                row_rtos = [x.strip().upper() for x in row.new_rto_list.split(",")]
                if not any(x in matching_rto_names for x in row_rtos):
                    continue

            if make_names and matching_make_groups:
                if not row.new_vehicle_makes:
                    continue
                row_makes = [x.strip().upper() for x in row.new_vehicle_makes.split(",")]
                if not any(x in matching_make_groups for x in row_makes):
                    continue

            gid = row.group_id if row.group_id is not None else row.id
            if gid not in seen_groups:
                row.display_group_id = gid

                mmc_name = row.make_model_class.name.strip().upper() if row.make_model_class else "NA"
                
                if mmc_name in ["NA", ""]:
                    prod_name = row.product.name if row.product else ""
                    translated_name = get_translated_make_model(prod_name)
                    if translated_name:
                        row.make_model_class = MakeModelClassMaster(name=translated_name)
                        row.display_make_model_class = translated_name
                    else:
                        row.display_make_model_class = "NA"
                else:
                    row.display_make_model_class = row.make_model_class.name

                if row.po_net_rate and row.po_net_rate > 0:
                    row.po_rate = row.po_net_rate
                elif row.po_od_rate and row.po_od_rate > 0:
                    row.po_rate = row.po_od_rate
                elif row.po_tp_rate and row.po_tp_rate > 0:
                    row.po_rate = row.po_tp_rate
                else:
                    row.po_rate = 0.0

                existing_lock = None
                if vehicle_no and policy_holder_name:
                    existing_lock = LockedPolicy.objects.filter(
                        source_rate=row,
                        vehicle_no__iexact=vehicle_no,
                        policy_holder_name__iexact=policy_holder_name
                    ).order_by("-id").first()

                row.lock_status = existing_lock.status if existing_lock else "UNLOCKED"

                results.append(row)
                seen_groups.add(gid)

            # Capped high (not at the 300 display limit) so the same-insurer
            # collapse below still sees the full candidate set before it's
            # cut down for display.
            if len(results) >= 2000:
                break

    # Different group_ids can still carry what is effectively the same rate
    # card - same insurer, tariff range, PO type, flat amount, and add TNC,
    # differing only in the payout rate (a data entry issue, not two
    # genuinely different offers). Collapse those down to the single
    # lowest-rate row. Group ID and the rate itself are deliberately left out
    # of the matching key - group_id is exactly what differs between these
    # near-duplicates, and rate is the one field allowed to differ. Status is
    # also left out of the key on purpose: the real-world case this is fixing
    # has the lower (correct) rate on the ACTIVE row and a stale, wildly
    # inflated rate sitting on an INACTIVE duplicate - those still need to
    # collapse together, with the lowest rate's own status winning.
    def effective_rate(row):
        if row.po_type == "On OD and TP":
            return (row.po_od_rate or 0) + (row.po_tp_rate or 0)
        return row.po_rate or 0

    best_by_key = {}
    for row in results:
        row.effective_rate = effective_rate(row)
        key = (
            row.insurance_company,
            row.tariff_min, row.tariff_max,
            row.po_type,
            row.po_flat_amount,
            row.add_tnc,
        )
        existing = best_by_key.get(key)
        if existing is None or row.effective_rate < existing.effective_rate:
            best_by_key[key] = row

    results = sorted(best_by_key.values(), key=lambda r: r.effective_rate, reverse=True)[:300]

    selected = {
        "target_date": target_date,
        "vehicle_no": vehicle_no,
        "policy_holder_name": policy_holder_name,
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

    return results, has_searched, selected
