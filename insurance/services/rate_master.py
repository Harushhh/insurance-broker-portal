"""Rate Master helpers: group fields and key hash, range filtering."""
import hashlib
from .common import normalize


GROUP_FIELDS = [
    "new_vehicle_makes", "insurer_vertical", "insurance_company", "product", "sub_product",
    "policy_type", "vehicle_age_min", "vehicle_age_max", "make_model_class", "fuel_type",
    "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
    "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type",
    "tariff_min", "tariff_max", "is_ncb", "is_cpa", "cc_min", "cc_max",
    "is_zd", "from_date", "to_date", "sc_min", "sc_max", "add_tnc",
    "status", "is_deleted", "upload_batch",
]


def build_key_hash(cleaned_dict):
    parts = []
    for f in GROUP_FIELDS:
        v = cleaned_dict.get(f)
        if hasattr(v, "pk"):
            v = v.pk
        if hasattr(v, "strftime"):
            v = v.strftime("%Y-%m-%d")
        parts.append(normalize(v))
    key_text = "|".join(parts)
    key_hash = hashlib.sha256(key_text.encode("utf-8")).hexdigest()
    return key_hash, key_text


def _is_valid_range_number(value):
    # field_min/field_max are FloatFields -- Django coerces the filter value
    # via float() while building the query, which raises an uncaught
    # ValueError (-> 500) for stray characters like "/" or "\". Validate
    # here so an unparseable value is just ignored, like an empty one.
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def apply_range_filter(qs, field_min, field_max, range_val):
    if not range_val:
        return qs
    if "-" in range_val:
        parts = range_val.split("-")
        val_min = parts[0].strip()
        val_max = parts[1].strip()
        if val_min and _is_valid_range_number(val_min):
            qs = qs.filter(**{field_min: val_min})
        if val_max and _is_valid_range_number(val_max):
            qs = qs.filter(**{field_max: val_max})
    else:
        # A bare value (no "-") means "records whose min-max span contains
        # this value", not an exact match on the min column.
        if _is_valid_range_number(range_val):
            qs = qs.filter(**{f"{field_min}__lte": range_val, f"{field_max}__gte": range_val})
    return qs
