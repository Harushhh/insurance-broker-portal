"""Executive analysis dashboard (`business_analysis`): rate-card analysis by state and month."""
from django.shortcuts import render
from django.db.models import Q
from collections import defaultdict
from ..models import RateMaster
from ..services.analysis import EVERGREEN, MONTH_NAMES, _build_rto_state_map, _row_states_and_months, _track_min_max, month_label_for


MONTH_ORDER = list(MONTH_NAMES.keys())


# Premium/tariff (PI) fields, not payout (PO) - what the policy is priced
# at, not what the broker earns. pi_tp_2..pi_tp_5 are the year 2-5 legs
# of a multi-year TP schedule; no upload has ever populated them, but
# they're wired up now so they light up the moment that data arrives.
RATE_FIELDS = {
    "net": "pi_net_rate", "od": "pi_od_rate", "tp": "pi_tp_rate",
    "tp2": "pi_tp_2", "tp3": "pi_tp_3", "tp4": "pi_tp_4", "tp5": "pi_tp_5",
}


def _filtered_rate_master_qs(request):
    qs = RateMaster.objects.filter(is_deleted="NO", status__in=["ACTIVE", "INACTIVE"])

    # Same date-range overlap filter used on the Rate Master Matrix
    # (dashboard/edit_rate): keeps only rows whose validity window
    # (from_date/to_date) covers the selected date, not an exact-match on
    # any single date field.
    date_range = (request.GET.get("date_range") or "").strip()
    if date_range:
        dates = date_range.split(" - ")
        if len(dates) == 2:
            d_from, d_to = dates[0].strip(), dates[1].strip()
            if d_from and d_to:
                qs = qs.filter(
                    (Q(from_date__lte=d_to) | Q(from_date__isnull=True)) &
                    (Q(to_date__gte=d_from) | Q(to_date__isnull=True))
                )

    # Engine CC / seating-capacity filters: a row matches if the entered
    # value falls inside its cc_min-cc_max (or sc_min-sc_max) band, treating
    # a null bound as open-ended - same semantics as the quote simulator's
    # CC/SC matching.
    cc = (request.GET.get("cc") or "").strip()
    if cc:
        try:
            cc_val = float(cc)
        except ValueError:
            cc_val = None
        if cc_val is not None:
            qs = qs.filter(
                (Q(cc_min__lte=cc_val) | Q(cc_min__isnull=True)) &
                (Q(cc_max__gte=cc_val) | Q(cc_max__isnull=True))
            )

    sc = (request.GET.get("sc") or "").strip()
    if sc:
        try:
            sc_val = float(sc)
        except ValueError:
            sc_val = None
        if sc_val is not None:
            qs = qs.filter(
                (Q(sc_min__lte=sc_val) | Q(sc_min__isnull=True)) &
                (Q(sc_max__gte=sc_val) | Q(sc_max__isnull=True))
            )

    return qs, date_range


def business_analysis(request):
    rto_state_map = _build_rto_state_map()
    qs, date_range = _filtered_rate_master_qs(request)
    auto_loaded_today = request.GET.get("auto_loaded_today") or ""

    def new_cell():
        cell = {"age_min": None, "age_max": None, "cc_min": None, "cc_max": None,
                "tariff_min": None, "tariff_max": None, "ncb": set(), "cpa": set(), "zd": set()}
        for key in RATE_FIELDS:
            cell[f"{key}_min"] = None
            cell[f"{key}_max"] = None
            cell[f"{key}_n"] = 0
        return cell

    agg = defaultdict(new_cell)
    months_seen = set()

    for row in qs.values(
        "product__name", "sub_product__name", "insurance_company", "new_rto_list",
        *RATE_FIELDS.values(),
        "vehicle_age_min", "vehicle_age_max", "cc_min", "cc_max",
        "tariff_min", "tariff_max",
        "is_ncb__code", "is_cpa__code", "is_zd__code",
    ).iterator(chunk_size=5000):
        rto_list = row["new_rto_list"]
        if not rto_list:
            continue

        states, months = _row_states_and_months(rto_list, rto_state_map)
        if not states:
            continue
        months_seen |= months

        product = row["product__name"] or "Uncategorized"
        sub_product = row["sub_product__name"] or "Uncategorized"
        insurer = row["insurance_company"]

        for state in states:
            for mo in months:
                cell = agg[(product, sub_product, insurer, state, mo)]
                for key, field in RATE_FIELDS.items():
                    val = row[field]
                    if val and val > 0:
                        cell[f"{key}_n"] += 1
                        _track_min_max(cell, key, val)

                _track_min_max(cell, "age", row["vehicle_age_min"])
                _track_min_max(cell, "age", row["vehicle_age_max"])
                _track_min_max(cell, "cc", row["cc_min"])
                _track_min_max(cell, "cc", row["cc_max"])
                _track_min_max(cell, "tariff", row["tariff_min"])
                _track_min_max(cell, "tariff", row["tariff_max"])

                if row["is_ncb__code"]:
                    cell["ncb"].add(row["is_ncb__code"])
                if row["is_cpa__code"]:
                    cell["cpa"].add(row["is_cpa__code"])
                if row["is_zd__code"]:
                    cell["zd"].add(row["is_zd__code"])

    records = []
    product_volume = defaultdict(int)
    sub_product_volume = defaultdict(int)
    insurer_set = set()
    state_set = set()

    for (product, sub_product, insurer, state, month), cell in agg.items():
        total_n = sum(cell[f"{key}_n"] for key in RATE_FIELDS)
        if not total_n:
            continue
        rec = {
            "product": product, "sub_product": sub_product, "insurer": insurer, "state": state, "month": month,
            "age_min": cell["age_min"], "age_max": cell["age_max"],
            "cc_min": cell["cc_min"], "cc_max": cell["cc_max"],
            "tariff_min": cell["tariff_min"], "tariff_max": cell["tariff_max"],
            "ncb": sorted(cell["ncb"]), "cpa": sorted(cell["cpa"]), "zd": sorted(cell["zd"]),
        }
        for key in RATE_FIELDS:
            n = cell[f"{key}_n"]
            rec[key] = {"min": round(cell[f"{key}_min"], 2), "max": round(cell[f"{key}_max"], 2), "n": n} if n else None
        records.append(rec)
        product_volume[product] += total_n
        sub_product_volume[sub_product] += total_n
        insurer_set.add(insurer)
        state_set.add(state)

    def month_sort_key(code):
        if code == EVERGREEN:
            return (9999, 99)
        mon, yy = code[:3], code[3:]
        return (int(yy), MONTH_ORDER.index(mon) if mon in MONTH_ORDER else 99)

    months_list = [
        {"code": m, "label": month_label_for(m)}
        for m in sorted(months_seen, key=month_sort_key) if m != EVERGREEN
    ]

    chart_data = {
        "records": records,
        "products": sorted(product_volume.keys(), key=lambda p: -product_volume[p]),
        "sub_products": sorted(sub_product_volume.keys(), key=lambda p: -sub_product_volume[p]),
        "insurers": sorted(insurer_set),
        "states": sorted(state_set),
        "months": months_list,
    }

    return render(request, "analysis.html", {
        "chart_data": chart_data,
        "date_range": date_range,
        "auto_loaded_today": auto_loaded_today,
        "cc_filter": request.GET.get("cc") or "",
        "sc_filter": request.GET.get("sc") or "",
    })
