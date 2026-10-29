"""Health payout rates checker: the Health "Check Eligibility" search page."""
from django.shortcuts import render
from ..models import HealthRateMaster
from ..services.health_payout import HEALTH_PAYOUT_MAX_RESULTS, _run_health_payout_search


# Standard Sum Insured ladder (in rupees) offered on the "Insurance Cover"
# dropdown. These are conventional Indian health-insurance cover tiers, not
# values read from the data — a customer picks from a known ladder, and the
# query checks which rate rules' [min_sum_insured, max_sum_insured] band
# contains that amount.
HEALTH_SUM_INSURED_OPTIONS = [
    (500000, "5 Lakhs"), (750000, "7.5 Lakhs"), (1000000, "10 Lakhs"),
    (1500000, "15 Lakhs"), (2000000, "20 Lakhs"), (2500000, "25 Lakhs"),
    ("above_25L", "Above 25 Lakhs"),
]


def health_payout_rates(request):
    results, total_found, has_more, has_searched, selected = _run_health_payout_search(request)

    product_list = HealthRateMaster.objects.exclude(is_deleted="YES").exclude(product_name__isnull=True).exclude(
        product_name=""
    ).values_list("product_name", flat=True).distinct().order_by("product_name")
    business_type_list = HealthRateMaster.objects.exclude(is_deleted="YES").exclude(business_type__isnull=True).exclude(
        business_type=""
    ).values_list("business_type", flat=True).distinct().order_by("business_type")
    category_list = HealthRateMaster.objects.exclude(is_deleted="YES").exclude(policy_category__isnull=True).exclude(
        policy_category=""
    ).values_list("policy_category", flat=True).distinct().order_by("policy_category")
    insurer_list = HealthRateMaster.objects.exclude(is_deleted="YES").exclude(insurance_company="").values_list(
        "insurance_company", flat=True
    ).distinct().order_by("insurance_company")

    return render(request, "health_payout_rates.html", {
        "has_searched": has_searched,
        "data": results,
        "total_found": total_found,
        "has_more": has_more,
        "max_results": HEALTH_PAYOUT_MAX_RESULTS,
        "product_list": product_list,
        "business_type_list": business_type_list,
        "category_list": category_list,
        "insurer_list": insurer_list,
        "sum_insured_options": HEALTH_SUM_INSURED_OPTIONS,
        "selected": selected,
    })
