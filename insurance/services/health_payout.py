"""Health payout search shared by the Health Payout Rates page and its API endpoint."""
from django.db.models import Q, F
from datetime import datetime
from ..constants import HEALTH_POLICY_TERM_FIELD
from ..models import PincodeMaster, HealthRateMaster
from .make_model import strict_match_in_cluster
from .points_audit import _log_health_points_search


# -------------------------
# HEALTH PAYOUT RATES CHECKER (Health's equivalent of Motor's Quote Simulator)
# -------------------------
HEALTH_PAYOUT_MAX_RESULTS = 200


HEALTH_ABOVE_25L_THRESHOLD = 2500000


def _build_health_payout_queryset(request, target_date, rate_field):
    """Eligibility-style matching (containment on age/sum-insured/date,
    same as Motor's cc/sc/vehicle-age matching in _build_motor_payout_queryset)
    — deliberately different from Health Rate Master's admin filters, which
    match a rule's own band exactly rather than checking whether it covers
    a given customer's numbers."""
    qs = HealthRateMaster.objects.exclude(is_deleted="YES").filter(status="ACTIVE")

    product_name = (request.GET.get("product_name") or "").strip()
    business_type = (request.GET.get("business_type") or "").strip()
    policy_category = (request.GET.get("policy_category") or "").strip()
    sum_insured = (request.GET.get("sum_insured") or "").strip()
    age = (request.GET.get("age") or "").strip()
    pincode = (request.GET.get("pincode") or "").strip()
    insurance_companies = [c for c in request.GET.getlist("insurance_company") if c]

    if target_date:
        qs = qs.filter(
            (Q(from_date__lte=target_date) | Q(from_date__isnull=True)) &
            (Q(to_date__gte=target_date) | Q(to_date__isnull=True))
        )

    if product_name:
        qs = qs.filter(product_name=product_name)
    if business_type:
        qs = qs.filter(business_type=business_type)
    if policy_category:
        qs = qs.filter(policy_category=policy_category)
    if insurance_companies:
        qs = qs.filter(insurance_company__in=insurance_companies)

    if sum_insured == "above_25L":
        # Open-ended option — not a single point to contain, but "does this
        # rule's cover extend past the threshold at all" (a range-overlap
        # check, not point-containment like the fixed Lakh amounts below).
        qs = qs.filter(
            Q(max_sum_insured__gt=HEALTH_ABOVE_25L_THRESHOLD) | Q(max_sum_insured__isnull=True)
        )
    elif sum_insured:
        try:
            si_val = float(sum_insured)
            qs = qs.filter(
                (Q(min_sum_insured__lte=si_val) | Q(min_sum_insured__isnull=True)) &
                (Q(max_sum_insured__gte=si_val) | Q(max_sum_insured__isnull=True))
            )
        except ValueError:
            pass

    if age:
        try:
            age_val = float(age)
            qs = qs.filter(
                (Q(min_age__lte=age_val) | Q(min_age__isnull=True)) &
                (Q(max_age__gte=age_val) | Q(max_age__isnull=True))
            )
        except ValueError:
            pass

    if pincode:
        matching_zone_names = []
        potential_zones = PincodeMaster.objects.filter(pincode_cluster__icontains=pincode)
        for zone_record in potential_zones:
            if strict_match_in_cluster(pincode, zone_record.pincode_cluster):
                matching_zone_names.append(zone_record.pincode_zone.strip())
        if matching_zone_names:
            qs = qs.filter(pincode_zone__in=matching_zone_names)
        else:
            # Lenient fallback, same pattern as rto_code in Motor's quote
            # checker: maybe the user typed the zone name (or "All") directly
            # instead of an actual pincode, or that zone's cluster hasn't been
            # populated yet on the Pincode Dashboard.
            qs = qs.filter(pincode_zone__icontains=pincode)

    qs = qs.order_by(F(rate_field).desc(nulls_last=True), F("payin_rate").desc(nulls_last=True), "-id")

    return qs


def _run_health_payout_search(request):
    """
    Core eligibility search shared by the health_payout_rates HTML page
    and HealthPayoutRatesAPIView. Returns (results, total_found, has_more,
    has_searched, selected).
    """
    has_searched = bool(request.GET)

    if has_searched:
        _log_health_points_search(request)

    today_str = datetime.today().strftime("%Y-%m-%d")
    target_date = (request.GET.get("target_date") or today_str).strip()
    policy_term = (request.GET.get("policy_term") or "1").strip()
    if policy_term not in HEALTH_POLICY_TERM_FIELD:
        policy_term = "1"
    rate_field = HEALTH_POLICY_TERM_FIELD[policy_term]

    results = []
    total_found = 0
    has_more = False

    if has_searched:
        qs = _build_health_payout_queryset(request, target_date, rate_field)
        total_found = qs.count()
        has_more = total_found > HEALTH_PAYOUT_MAX_RESULTS

        for row in qs[:HEALTH_PAYOUT_MAX_RESULTS]:
            term_rate = getattr(row, rate_field)
            row.applicable_rate = term_rate if term_rate is not None else row.payin_rate
            results.append(row)

    selected = {
        "product_name": (request.GET.get("product_name") or "").strip(),
        "business_type": (request.GET.get("business_type") or "").strip(),
        "policy_category": (request.GET.get("policy_category") or "").strip(),
        "sum_insured": (request.GET.get("sum_insured") or "").strip(),
        "age": (request.GET.get("age") or "").strip(),
        "pincode": (request.GET.get("pincode") or "").strip(),
        "insurance_companies": [c for c in request.GET.getlist("insurance_company") if c],
        "target_date": target_date,
        "policy_term": policy_term,
    }

    return results, total_found, has_more, has_searched, selected
