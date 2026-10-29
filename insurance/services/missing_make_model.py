"""Grouping, resolution and filtering behind the Missing Make/Model page."""
import json
from ..models import MakeModelMaster, RateMaster, MISFailedRow, MissingMakeModelManualResolution


# Payload keys the aggregation reads, already normalized the way
# _normalized_payload normalizes MISFailedRow.payload's own keys.
MISSING_MM_KEY_MAKE = "policy: vehicle make"


MISSING_MM_KEY_MODEL = "policy: model"


MISSING_MM_KEY_PRODUCT = "policy: vehproduct"


MISSING_MM_KEY_SUB_PRODUCT = "policy: sub product"


MISSING_MM_KEY_INSURER = "policy: insurance company"


MISSING_MM_KEY_VEHICLE_CLASS = "policy: vehicle class"


# How many matching master group names a Resolved row lists before collapsing
# to "+N more" — same preview length RULE 5a uses in its own reason text.
MISSING_MM_RESOLVED_PREVIEW = 5


def _normalized_payload(payload):
    """
    {stripped-lowercased header: stringified value} for one MISFailedRow.

    MISFailedRow.payload's keys are the uploaded file's OWN header text --
    mapping_engine._extract_failed_rows_from_df locates each column with
    _find_column (case/whitespace-insensitive) but then keys the payload by the
    original header. So a DB-level JSON lookup (payload__"Policy: vehproduct")
    would silently miss every file whose header read "Policy: VehProduct", and
    the grouping below has to happen in Python instead.

    Matching is strip+lower EXACT compare, mirroring _find_column -- deliberately
    not substring, or 'Policy: model' would collide with 'Policy: product name'
    and 'Policy: plan name'. That is also why this can't share code with
    _split_payload_summary, which matches key *fragments* on purpose so it can
    pick "the most identifying field present" out of an unknown header set.
    """
    out = {}
    for k, v in (payload or {}).items():
        if v is None:
            continue
        out[str(k).strip().lower()] = str(v).strip()
    return out


def _missing_make_model_groups(date_from=None, date_to=None):
    """
    Every MIS policy row that failed RULE 5a because its "make + model" string
    matched no MakeModelMaster cluster entry, grouped into one entry per
    distinct (failing value, product, sub product, insurer, vehicle class).

    Mirrors _rate_master_grid_summary_qs's role for the Grid Summary tab, but
    the grouping can't be done in the database: product/sub-product live inside
    MISFailedRow.payload under the source file's own header casing (see
    _normalized_payload). Only the date filter is pushed down to SQL, since that
    genuinely reduces how much JSON is dragged into Python; insurer / product /
    search / status are group-level attributes applied by the caller, so doing
    them here would mean a second pass just to build the filter dropdowns.

    Returns a list of dicts sorted by policy_count desc, then value.
    """
    from ..mapping_engine import (
        MAKE_MODEL_UNRESOLVED_REASON_PREFIX,
        MAKE_MODEL_UNRESOLVED_VALUE_PATTERN,
        MAKE_MODEL_UNRESOLVED_VALUE_SUFFIX,
    )

    # Both halves are needed. The prefix anchors the rule (RULE 5a) and rules
    # out the blank-make/model variant; the suffix is what separates this from
    # the "resolved to master group(s) [...] but no candidate rate row" sibling,
    # which opens with the same text and only diverges after the value. The
    # regex below re-checks the pair per row, so this filter is a narrowing, not
    # the correctness boundary.
    qs = MISFailedRow.objects.filter(
        status_key="NO_MATCH",
        failure_reason__startswith=MAKE_MODEL_UNRESOLVED_REASON_PREFIX,
        failure_reason__contains=MAKE_MODEL_UNRESOLVED_VALUE_SUFFIX,
    )
    if date_from:
        qs = qs.filter(mis_file__created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(mis_file__created_at__date__lte=date_to)

    # order_by("id") only so the "first non-blank wins" display rule below is
    # deterministic across requests -- a group's displayed casing must not flip
    # around between page loads. .values_list + .iterator keeps this to plain
    # tuples and one chunk of rows in memory rather than 20k model instances.
    rows = (
        qs.order_by("id")
        .values_list("failure_reason", "payload", "insurer",
                     "mis_file__created_at", "mis_file_id")
        .iterator(chunk_size=2000)
    )

    groups = {}
    for failure_reason, payload, insurer_col, uploaded_at, mis_file_id in rows:
        m = MAKE_MODEL_UNRESOLVED_VALUE_PATTERN.match(failure_reason or "")
        if not m:
            continue                      # defensive: the reason text drifted
        value = " ".join((m.group(1) or "").split())
        if not value:
            continue                      # nothing actionable to add

        p = _normalized_payload(payload)
        make = p.get(MISSING_MM_KEY_MAKE, "")
        model = p.get(MISSING_MM_KEY_MODEL, "")
        product = p.get(MISSING_MM_KEY_PRODUCT, "")
        sub_product = p.get(MISSING_MM_KEY_SUB_PRODUCT, "")
        insurer = (insurer_col or p.get(MISSING_MM_KEY_INSURER, "") or "").strip()
        vehicle_class = p.get(MISSING_MM_KEY_VEHICLE_CLASS, "")

        # Keyed on `value` (parsed from the reason), not on the payload's make
        # and model. `value` IS lower(make + " " + model) -- it's literally the
        # string resolve_make was called with and failed on, and the string that
        # has to go into the cluster to fix it. Rebuilding it from payload is
        # lossy: _extract_failed_rows_from_df skips NaN cells entirely, so a
        # policy with a blank model has no 'Policy: model' key at all while the
        # engine still saw the make. Payload make/model are display-only.
        #
        # vehicle_class is in the key alongside product/sub_product for the same
        # reason sub_product is: RULE 2b (vehicle class) narrows current_grid
        # before RULE 5a runs, so whether a given MakeModelMaster group actually
        # fixes this failure depends on it too -- see
        # _active_vehicle_make_scope.
        key = (value, product.lower(), sub_product.lower(), insurer.lower(), vehicle_class.lower())
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "value": value,
                "make": "", "model": "",
                "product": "", "sub_product": "", "insurer": "", "vehicle_class": "",
                "policy_count": 0,
                "file_ids": set(),
                "first_seen": uploaded_at,
                "last_seen": uploaded_at,
            }
        g["policy_count"] += 1
        g["file_ids"].add(mis_file_id)
        # First non-blank original-cased value wins for every display field:
        # rows in a group can disagree on casing, and on whether a field was
        # present at all, so "first non-blank, oldest row first" is the stable
        # choice.
        for field, raw in (("make", make), ("model", model),
                           ("product", product), ("sub_product", sub_product),
                           ("insurer", insurer), ("vehicle_class", vehicle_class)):
            if not g[field] and raw:
                g[field] = raw
        if uploaded_at and (not g["first_seen"] or uploaded_at < g["first_seen"]):
            g["first_seen"] = uploaded_at
        if uploaded_at and (not g["last_seen"] or uploaded_at > g["last_seen"]):
            g["last_seen"] = uploaded_at

    out = []
    for g in groups.values():
        g["file_count"] = len(g.pop("file_ids"))
        # Every display field can legitimately be blank (the MIS cell was NaN,
        # so the key was never written). Fall back to the parsed value for make
        # so a row is never entirely unlabelled; the rest render as an em-dash.
        if not g["make"] and not g["model"]:
            g["make"] = g["value"]
        out.append(g)

    out.sort(key=lambda g: (-g["policy_count"], g["value"]))
    return out


def _active_vehicle_make_scope():
    """
    (insurer, product, sub product) lowercased -> {
        "wildcard": lowercased new_vehicle_makes items from ACTIVE rows whose
                    make_model_class is blank/NA,
        "by_class": {vehicle class lowercased: lowercased new_vehicle_makes
                     items} for rows with a specific class,
    }

    MakeModelMaster carries no insurer or vehicle-class of its own -- the same
    cluster name can be wired into one insurer/product's grids and not
    another's, and RULE 2b narrows current_grid by vehicle class before RULE
    5a runs. This is Steps 2b+2 of that chain (mirrors match_vehicle_class's
    direct-match + NA-wildcard rule, then check_resolved_cluster_match),
    re-run here so _annotate_make_model_resolution below can tell a global
    Step-1 word-overlap match apart from one this specific insurer / product /
    vehicle class actually uses.
    """
    scope = {}
    rows = (
        RateMaster.objects.filter(status="ACTIVE", is_deleted="NO")
        .exclude(new_vehicle_makes__isnull=True)
        .exclude(new_vehicle_makes="")
        .values_list("insurance_company", "product__name", "sub_product__name",
                     "make_model_class__name", "new_vehicle_makes")
        .iterator(chunk_size=2000)
    )
    for insurer, product, sub_product, vehicle_class, cluster in rows:
        key = (
            (insurer or "").strip().lower(),
            (product or "").strip().lower(),
            (sub_product or "").strip().lower(),
        )
        bucket = scope.setdefault(key, {"wildcard": set(), "by_class": {}})
        items = {item.strip().lower() for item in cluster.split(",") if item.strip()}
        if not items:
            continue
        class_norm = (vehicle_class or "").strip().lower()
        if not class_norm or class_norm == "na":
            bucket["wildcard"].update(items)
        else:
            bucket["by_class"].setdefault(class_norm, set()).update(items)
    return scope


def _annotate_make_model_resolution(groups):
    """
    Mark which aggregated values now match a MakeModelMaster cluster that the
    group's OWN insurer + product + sub product actually uses.

    MISFailedRow is historical -- rows are only rewritten when their MIS file is
    reprocessed -- so adding a value to the master today changes nothing about
    yesterday's failed rows. Without this re-check against the LIVE master the
    page would be a write-only log that never shrinks as work gets done.

    Scoped, not global: MakeModelMaster has no insurer or vehicle-class column,
    so a value can share 2+ words with SOME cluster entry that belongs to a
    master group no Rate Master row for THIS insurer/product/sub
    product/vehicle class ever references (real example: a Liberty / Private
    Car / SAOD failure resolving, word-overlap only, to a master group used
    solely by a different insurer's grid). Marking that "Resolved" would be a
    false positive -- reprocessing would not map the policy, it would just
    trade this failure for the sibling "resolved to master group(s) [...] but
    no candidate rate row" one. So resolution here is Step 1
    (build_make_model_cluster_index / resolve_make_model_with_index, global)
    AND-ed with Steps 2b+2 (_active_vehicle_make_scope, scoped) -- the same
    chain RULE 5a itself runs, just re-run live against today's data.

    Uses build_make_model_cluster_index rather than build_make_model_lookup:
    same match rule, but the master side is tokenized once for the whole request
    instead of once per distinct search term.

    Also OR's in MissingMakeModelManualResolution: a value can be fixed by
    editing MakeModelMaster somewhere other than this page's own Add to
    Master action (the Make/Model Master dashboard directly, or the bulk
    Rate Master importer), and the live re-check above might not pick that up
    -- e.g. the edit used different wording than the MIS value shares 2+
    words with. A manual mark covers that gap without requiring a cluster
    write through here. g["resolved_manual"]/g["resolved_live"] let the
    template tell the two apart (an "Unresolve" action only makes sense on a
    purely-manual mark).
    """
    from ..mapping_engine import build_make_model_cluster_index, resolve_make_model_with_index

    index = build_make_model_cluster_index(
        MakeModelMaster.objects.all(), "make_model_name", "make_model_cluster"
    )
    scope = _active_vehicle_make_scope()
    manual = set(
        MissingMakeModelManualResolution.objects.values_list(
            "value", "product", "sub_product", "insurer", "vehicle_class"
        )
    )
    for g in groups:
        global_names = resolve_make_model_with_index(index, g["value"])
        bucket = scope.get(
            (g["insurer"].strip().lower(), g["product"].strip().lower(), g["sub_product"].strip().lower())
        )
        if bucket is None:
            scoped_makes = set()
        else:
            class_norm = g["vehicle_class"].strip().lower()
            if not class_norm:
                # Blank MIS vehicle class -- match_vehicle_class's
                # mis_class_is_blank branch only lets NA-wildcard rows pass in
                # this case, so only wildcard makes count here too.
                scoped_makes = bucket["wildcard"]
            else:
                scoped_makes = bucket["wildcard"] | bucket["by_class"].get(class_norm, set())
        names = sorted(name for name in global_names if name in scoped_makes)

        group_key = (
            g["value"], g["product"].strip().lower(), g["sub_product"].strip().lower(),
            g["insurer"].strip().lower(), g["vehicle_class"].strip().lower(),
        )
        g["resolved_live"] = bool(names)
        g["resolved_manual"] = group_key in manual
        g["resolved"] = g["resolved_live"] or g["resolved_manual"]
        g["resolved_names"] = names
        # A generic make/model legitimately matches dozens of clusters (a real
        # "yamaha alpha" hits 45+), which would swamp the table cell. Same 5-name
        # preview RULE 5a's own "resolved to master group(s) [...]" message uses;
        # the export still carries the full list.
        g["resolved_preview"] = names[:MISSING_MM_RESOLVED_PREVIEW]
        g["resolved_extra"] = max(0, len(names) - MISSING_MM_RESOLVED_PREVIEW)
        # Carried on each row's bulk-select checkbox so the bulk actions know
        # exactly which group a checked box refers to -- see
        # bulk_add_missing_make_model_to_master / bulk_mark_missing_make_model_resolved.
        g["group_key_json"] = json.dumps({
            "value": g["value"], "product": g["product"], "sub_product": g["sub_product"],
            "insurer": g["insurer"], "vehicle_class": g["vehicle_class"],
        })
    return groups


def _filter_missing_make_model(groups, insurer="", product="", status="missing", q=""):
    """
    Group-level filters for the Missing Make/Model page. Pure function over the
    _missing_make_model_groups output so it can be tested without HTTP, and it
    preserves the incoming sort order (policy_count desc) rather than re-sorting.
    """
    q_norm = q.strip().lower()
    out = []
    for g in groups:
        if insurer and g["insurer"] != insurer:
            continue
        if product and g["product"] != product:
            continue
        if status == "missing" and g["resolved"]:
            continue
        if status == "resolved" and not g["resolved"]:
            continue
        if q_norm and q_norm not in f'{g["value"]} {g["make"]} {g["model"]}'.lower():
            continue
        out.append(g)
    return out


def _append_make_model_cluster_item(master, value):
    """
    Append one item to MakeModelMaster.make_model_cluster.

    The cluster is a comma-separated list that fuzzy_match_make_model splits and
    .strip().upper()s item by item, so separator whitespace and stored casing
    are both cosmetic to matching. The existing text is therefore preserved
    verbatim -- a minimal, reviewable diff on the Make/Model Master dashboard --
    and only ", " + the new item is appended.

    Returns True if the cluster changed, False if an equivalent item was already
    present.
    """
    existing = master.make_model_cluster or ""
    items = [x.strip() for x in existing.split(",") if x.strip()]
    if any(item.upper() == value.upper() for item in items):
        return False
    trimmed = existing.rstrip().rstrip(",").rstrip()
    master.make_model_cluster = f"{trimmed}, {value}" if trimmed else value
    return True


def _missing_make_model_manual_key_from_post(data, prefix=""):
    """
    (value, product, sub_product, insurer, vehicle_class) parsed off a dict,
    lowercased/stripped to match _missing_make_model_groups' own group key
    exactly -- so equality lookups against MissingMakeModelManualResolution
    and against a live group's key both just work.

    `prefix` is needed for the single-row mark/unmark forms: they share the
    page's main <form>, which already carries top-level "insurer" / "product"
    fields for filter preservation on redirect (see MISSING_MM_FILTER_KEYS),
    so the per-row fields these actions read are named mm_mark_* in the
    template to avoid colliding with those. The bulk action instead reads a
    self-contained JSON blob per selected row (no collision risk), so it
    calls this with prefix="" against the parsed dict's own plain keys.
    """
    def get(key):
        return data.get(f"{prefix}{key}") or ""

    return (
        " ".join(get("value").split()).lower(),
        get("product").strip().lower(),
        get("sub_product").strip().lower(),
        get("insurer").strip().lower(),
        get("vehicle_class").strip().lower(),
    )
