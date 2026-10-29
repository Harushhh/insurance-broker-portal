"""Make/model matching and filtering shared by the dashboards, the payout pages, the policy lock checker and the MIS mapping engine."""
from django.db.models import Q
import re
from collections import defaultdict
from ..models import MakeModelMaster, RateMaster, ProductMaster, MakeModelClassMaster, MakeModelMappingValue
from .common import safe_json_for_script


# =========================================================
# NA CONFIGURATION & HELPERS
# =========================================================
NA_MAKE_MODEL_MAP = {
    "TW": ["Scooter", "Bike"],
    "Private Car": ["Car"],
    "GCV 4W": ["Truck", "Pick Up Van", "Dumper Tipper", "Trailer", "Tanker", "Goods Carrying Tractor"],
    "GCV 3W": ["Goods Carrying Rickshaw"],
    "PCV 3W": ["Passenger Carrying Rickshaw"],
    "PCV 4W": ["Taxi", "School Bus", "Other Bus"],
    "MISCD": ["MISCD-Tractor", "MISCD-Others"],
    "PCV 2W": ["PCV 2W"],
}


# Bulletproof case-insensitive dictionary matcher
def get_translated_make_model(product_name):
    if not product_name:
        return None
    # Standardize map keys to uppercase and strip spaces
    normalized_map = {str(k).strip().upper(): v for k, v in NA_MAKE_MODEL_MAP.items()}
    # Standardize the incoming database string
    clean_product = str(product_name).strip().upper()
    
    if clean_product in normalized_map:
        return ", ".join(normalized_map[clean_product])
    return None


def get_dynamic_make_model_class_list(product_id):
    """
    Returns the appropriate make/model classes for the selected product.
    Instead of just renaming 'NA', it explicitly filters the list to show valid individual classes.
    """
    all_classes = list(MakeModelClassMaster.objects.all().order_by("name"))
    
    if not product_id or not str(product_id).isdigit():
        return all_classes

    prod_obj = ProductMaster.objects.filter(id=int(product_id)).first()
    if not prod_obj:
        return all_classes

    product_name = prod_obj.name.strip()

    # If the product exists in our map, filter the dropdown to only show those classes + NA
    if product_name in NA_MAKE_MODEL_MAP:
        valid_names = [c.lower() for c in NA_MAKE_MODEL_MAP[product_name]]
        valid_names.append("na")  # Always include NA as a fallback

        filtered_classes = []
        for m in all_classes:
            if m.name.strip().lower() in valid_names:
                filtered_classes.append(m)
        return filtered_classes

    return all_classes


# =========================================================
# UTILITY FUNCTIONS
# =========================================================
def strict_match_in_cluster(search_term, cluster_string):
    """
    Word-boundary regex match against comma-separated cluster items, so
    'MH09' won't accidentally match inside 'MH090' or similar.

    The one definition behind RTO/Make matching everywhere: the dashboard,
    motor payout rates, policy lock checker and the MIS mapping engine
    (insurance/mapping_engine.py imports it from here).
    """
    if not cluster_string:
        return False
    term = str(search_term).strip().upper()
    items = [x.strip().upper() for x in str(cluster_string).split(",")]
    if term in items:
        return True
    pattern = rf"(?<![A-Z0-9]){re.escape(term)}(?![A-Z0-9])"
    for item in items:
        if re.search(pattern, item):
            return True
    return False


def resolve_make_model_groups(make_term):
    """
    Upper-cased MakeModelMaster.make_model_name of every group whose cluster
    contains make_term (strict_match_in_cluster) -- or contains any other
    spelling linked to it on the Make/Model Mapping page, so picking "HERO
    SPLENDOR" also finds a group that only lists "HERO MOTOR SPLENDOR".
    Ordered and de-duplicated; with no mapping it's exactly the single-term
    lookup the search pages did before.

    Only the make/model search pages use this. The MIS mapping engine and the
    overlap detector keep their own fuzzy word-overlap matching and must stay
    in sync with each other, not with this.
    """
    term = (make_term or "").strip().upper()
    if not term:
        return []

    terms = [term]
    mapping_id = MakeModelMappingValue.objects.filter(value=term).values_list("mapping_id", flat=True).first()
    if mapping_id is not None:
        terms += [
            v for v in MakeModelMappingValue.objects.filter(mapping_id=mapping_id).values_list("value", flat=True)
            if v != term
        ]

    group_names = []
    for t in terms:
        for make_record in MakeModelMaster.objects.filter(make_model_cluster__icontains=t):
            if strict_match_in_cluster(t, make_record.make_model_cluster):
                name = make_record.make_model_name.strip().upper()
                if name not in group_names:
                    group_names.append(name)
    return group_names


def apply_make_model_filter(qs, product_id: str, make_model_class: str):
    """
    Unified filter for Make Model Classes applied across all rate views.
    Handles fallbacks correctly so that 'NA' databases hits are included when a specific sub-class is searched.
    """
    if not make_model_class:
        return qs

    clean_mmc = str(make_model_class).strip()
    
    if clean_mmc.isdigit():
        # If passed an ID (e.g. for "Scooter"), fetch exact ID OR fallback to 'NA' if mapped
        mmc_obj = MakeModelClassMaster.objects.filter(id=int(clean_mmc)).first()
        if mmc_obj:
            mmc_name = mmc_obj.name.strip().upper()
            if mmc_name == "NA":
                qs = qs.filter(make_model_class__name__iexact="NA")
            else:
                is_na_equivalent = any(mmc_name.lower() in [c.lower() for c in mapped_classes] for mapped_classes in NA_MAKE_MODEL_MAP.values())
                if is_na_equivalent:
                    qs = qs.filter(Q(make_model_class_id=clean_mmc) | Q(make_model_class__name__iexact="NA"))
                else:
                    qs = qs.filter(make_model_class_id=clean_mmc)
        else:
            qs = qs.filter(make_model_class_id=clean_mmc)

    elif clean_mmc.upper() == "NA":
        qs = qs.filter(make_model_class__name__iexact="NA")
    else:
        is_na_equivalent = False
        for mapped_classes in NA_MAKE_MODEL_MAP.values():
            if clean_mmc.lower() in [c.lower() for c in mapped_classes]:
                is_na_equivalent = True
                break
        
        if is_na_equivalent:
            qs = qs.filter(Q(make_model_class__name__iexact=clean_mmc) | Q(make_model_class__name__iexact="NA"))
        else:
            qs = qs.filter(make_model_class__name__iexact=clean_mmc)
            
    return qs


def get_make_mapping_context():
    all_makes_objs = list(MakeModelMaster.objects.all())
    all_individual_makes = set()

    for obj in all_makes_objs:
        if obj.make_model_cluster:
            for item in str(obj.make_model_cluster).split(","):
                item = item.strip()
                if item:
                    all_individual_makes.add(item)

    all_individual_makes = sorted(list(all_individual_makes))
    class_to_makes = defaultdict(set)
    product_to_makes = defaultdict(set)

    # Built once and reused below instead of rescanning the full
    # MakeModelMaster table for every (rate row x make) pair — that was an
    # O(n*m) scan repeated on every request to this and the payout/lock-checker
    # views that call this function.
    # Keyed upper-case: RateMaster.new_vehicle_makes tokens (rg below) don't
    # always match a MakeModelMaster.make_model_name's casing byte-for-byte
    # (different insurers' uploads have spelled the same group differently --
    # e.g. "Private_Car_All" vs. "Private_Car_ALL") -- an exact-case dict
    # lookup silently dropped that group's makes from the mapping entirely.
    makes_by_name = {
        obj.make_model_name.strip().upper(): obj
        for obj in all_makes_objs
        if obj.make_model_name
    }

    # .distinct() matters a lot in practice here: many thousands of RateMaster
    # rows (differing only in numeric rate fields) commonly share the exact
    # same (product, make_model_class, new_vehicle_makes) triple, so without
    # it this loop was repeating identical work per duplicate row. .order_by()
    # with no arguments is required alongside it — RateMaster's default
    # Meta.ordering is "-id", and Postgres requires ORDER BY columns to
    # appear in a SELECT DISTINCT's column list, so without clearing it
    # Django silently pulls the (always-unique) id into the comparison and
    # .distinct() ends up deduplicating nothing at all.
    #
    # product_id is selected alongside make_model_class_id so the Make Name
    # dropdown can be keyed by Product: every product in NA_MAKE_MODEL_MAP
    # stores its rows under the single shared "NA" make_model_class, so a
    # class-keyed mapping alone can't tell one product's makes from another's.
    rate_makes = RateMaster.objects.filter(is_deleted="NO").exclude(
        new_vehicle_makes__isnull=True
    ).exclude(new_vehicle_makes="").order_by().values_list(
        "make_model_class_id", "product_id", "new_vehicle_makes"
    ).distinct()

    for mmc_id, product_id, makes_str in rate_makes:
        rate_groups = [m.strip() for m in makes_str.split(",")]
        for rg in rate_groups:
            if not rg:
                continue
            obj = makes_by_name.get(rg.upper())
            if obj and obj.make_model_cluster:
                for item in str(obj.make_model_cluster).split(","):
                    item = item.strip()
                    if not item:
                        continue
                    if mmc_id is not None:
                        class_to_makes[str(mmc_id)].add(item)
                    if product_id is not None:
                        product_to_makes[str(product_id)].add(item)

    class_makes_mapping = {k: sorted(list(v)) for k, v in class_to_makes.items()}
    product_makes_mapping = {k: sorted(list(v)) for k, v in product_to_makes.items()}
    return (
        safe_json_for_script(all_individual_makes),
        safe_json_for_script(class_makes_mapping),
        safe_json_for_script(product_makes_mapping),
        all_individual_makes,
    )
