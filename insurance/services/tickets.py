"""Helpers that deep-link a ticket to the Motor payout page."""
from django.urls import reverse
from urllib.parse import urlencode
from ..models import ProductMaster, SubProductMaster, FuelTypeMaster, MakeModelClassMaster
from .make_model import NA_MAKE_MODEL_MAP


# =========================================================
# TICKETING SYSTEM VIEWS
# =========================================================
# Maps the human-readable labels the ticket-raising JS saves (see
# formatKey() in policy_lock_checker.html / motor_payout_rates.html) back to
# the GET param names /motor-payout-rates/ actually reads. Fields with no
# equivalent there (Vehicle No., Policy Holder Name) are simply dropped.
TICKET_TO_MOTOR_PAYOUT_PARAMS = {
    "Target Date": "target_date",
    "Product": "product",
    "Sub Product": "sub_product",
    "Make Model Class": "make_model_class",
    "Make Name (Code)": "make_names",
    "RTO Code": "rto_code",
    "CC / GVW": "cc",
    "Fuel": "fuel",
    "Exact SC": "sc",
    "Manufacturing Year": "mfg_year",
    "Zero Dep (ZD)": "is_zd",
    "CPA": "is_cpa",
    "NCB": "is_ncb",
}


MOTOR_PAYOUT_PARAM_NAMES = set(TICKET_TO_MOTOR_PAYOUT_PARAMS.values())


def _build_motor_payout_rates_url(ticket):
    """_build_motor_payout_queryset (the search/filter backend) accepts either
    a numeric id or the plain text name for product/sub_product/fuel/
    make_model_class, so it finds the right rows either way. But the
    motor_payout_rates.html <select> dropdowns pick their "selected" option
    by comparing against option value="{{ id }}" — a ticket's saved
    human-readable name (e.g. "Private Car") never matches that, so the
    dropdown silently falls back to "Select..." even though the search
    itself worked. Resolve name -> id below so the destination page's
    dropdowns actually show the right option, not just the right results.

    Only MOTOR tickets: Health/Life payloads reuse labels like "Product" and
    "Sub Product" for their own (non-vehicle) fields, so applying this
    mapping to them would build a nonsense motor-rates search."""
    if ticket.category != "MOTOR" or not ticket.form_payload:
        return None

    params = {}
    for label, value in ticket.form_payload.items():
        if not value:
            continue
        param_name = TICKET_TO_MOTOR_PAYOUT_PARAMS.get(label)
        if not param_name and label in MOTOR_PAYOUT_PARAM_NAMES:
            # Older tickets saved the raw field name instead of the
            # human-readable label — already a valid param, use as-is.
            param_name = label
        if param_name:
            params[param_name] = value

    if not params:
        return None

    # Resolve the product's real name regardless of whether the ticket saved
    # an id or a name for it — NA_MAKE_MODEL_MAP below is keyed by name, and
    # older tickets may have already saved a raw id here (see the "already a
    # valid param" branch above).
    product_param = str(params.get("product") or "").strip()
    if product_param.isdigit():
        prod = ProductMaster.objects.filter(id=product_param).first()
        resolved_product_name = prod.name if prod else ""
    else:
        resolved_product_name = product_param
        if product_param:
            prod = ProductMaster.objects.filter(name__iexact=product_param).first()
            if prod:
                params["product"] = prod.id

    if params.get("sub_product") and not str(params["sub_product"]).isdigit():
        sub_prod = SubProductMaster.objects.filter(name__iexact=str(params["sub_product"]).strip()).first()
        if sub_prod:
            params["sub_product"] = sub_prod.id

    if params.get("fuel") and not str(params["fuel"]).isdigit():
        fuel_obj = FuelTypeMaster.objects.filter(name__iexact=str(params["fuel"]).strip()).first()
        if fuel_obj:
            params["fuel"] = fuel_obj.id

    # Make Model Class is the one exception: motor_payout_rates.html's JS
    # swaps in string-valued options (value == class name, e.g. "Car")
    # for products in NA_MAKE_MODEL_MAP instead of the usual database-id
    # options, so for those products the saved name is already the correct
    # value and must NOT be converted to an id.
    if params.get("make_model_class") and not str(params["make_model_class"]).isdigit():
        if resolved_product_name not in NA_MAKE_MODEL_MAP:
            mmc = MakeModelClassMaster.objects.filter(name__iexact=str(params["make_model_class"]).strip()).first()
            if mmc:
                params["make_model_class"] = mmc.id

    return reverse("motor_payout_rates") + "?" + urlencode(params)
