"""
Finds pairs of Rate Master groups that the MIS Payout Engine can never tell
apart - the root cause of every "MULTIPLE MATCHES" row the Rate Master Health
dashboard reports after the fact.

The dashboard's MIS Errors table is reactive: a conflicting pair only shows up
once a real policy happens to land on it, and the same pair keeps resurfacing
file after file because nothing points at the two rate rows underneath. This
module works the other way round - it asks, directly of the Rate Master, "do
two groups exist that ANY policy could match simultaneously?", so the conflict
is visible before an MIS file ever hits it.

WHAT COUNTS AS A CONFLICT
-------------------------
Faithfulness to process_mis_mapping's RULE 1-6 chain is the whole game here: a
predicate looser than the engine's reports pairs the engine can actually tell
apart (false alarms nobody trusts), and a tighter one misses real ambiguity.
So every axis below mirrors one rule, using that rule's own semantics:

  RULE 1  insurance_company  exact match on the strip+lower-cased value -
                             mapping_engine lower-cases the column before
                             partitioning by it, so two spellings that differ
                             only in case land in the SAME bucket and CAN
                             collide.
  RULE 2  product /          check_categorical_match treats a blank grid value
          sub_product /      as a wildcard that matches anything.
          fuel_type
  RULE 2b make_model_class   blank OR 'na' is the wildcard.
  RULE 3  cc / sc /          a NULL bound is open (see the min_col.isna()
          vehicle_age /      branches); 0 is a real bound, NOT "unset".
          tariff
  RULE 4  from_date/to_date  same open-bound convention as RULE 3.
  RULE 5a new_vehicle_makes  comma-separated cluster list, matched by exact
  RULE 5b new_rto_list       item membership. A blank list is NOT a wildcard -
                             check_resolved_cluster_match returns False for it
                             - it only ever matches a policy whose own value is
                             blank, so two blank lists still collide. See the
                             caveat below: this axis is treated as a LOWER
                             bound on ambiguity, not an exact one.

THE CLUSTER AXES ARE A LOWER BOUND
----------------------------------
Two groups whose cluster lists share no item are treated here as separable,
but the engine can still match both. RULE 5a resolves one MIS make+model
string to a SET of master names (resolve_make on a real policy - "TATA MOTORS
LTD TATA SIGNA 5530.S BSVI 4X2" - returns 145 of the 522 MakeModelMaster
names, because fuzzy_match_make_model only needs two shared words), and
check_resolved_cluster_match passes if ANY of them appears in the group's
list. So disjoint lists routinely co-resolve, and a strict item-intersection
test under-reports.

Modelling that faithfully was measured against the live Rate Master and
deliberately not adopted: it takes the conflict count from ~36k to ~800k pairs
and the sweep from 23s to 57s, while leaving the two buckets anyone can
actually act on - EXACT_DUPLICATE (45) and CONTAINED (784) - bit-for-bit
identical. Those two are invariant by construction: identical groups have
identical lists, and containment already requires a superset, so neither can
be hidden by the strict test. Only PARTIAL, which is a health metric rather
than a work queue at either size, moves. If that trade is ever revisited, the
grounded way to do it is a co-resolution index over MakeModelMaster's cluster
entries keyed by shared word-pairs, not a blanket "any two non-empty lists
collide".
  RULE 6  is_ncb / is_cpa /  'NA' (and NULL, which mapping_engine fills to
          is_zd              'NA') is the wildcard; YES and NO are disjoint.

Two things the engine does NOT look at are deliberately absent: policy_type and
veh_use. Neither appears anywhere in RULE 1-6, so two groups differing only in
policy_type are genuinely ambiguous to the engine, and leaving them out here is
what makes this match reality rather than the schema.

The axes combine with AND, not OR: a pair is only reported when EVERY axis
fails to separate the two groups simultaneously. That is the same shape as the
engine, where each rule is a successive filter on the surviving candidates - a
single discriminating axis anywhere in the chain is enough to resolve a policy
to one group, so it is enough to clear the pair here too.

GROUPS, NOT ROWS
----------------
process_mis_mapping judges ambiguity on distinct COALESCE(group_id, id) keys,
never on raw row counts, because one rate card explodes across many physical
rows (one per RTO in its cluster). This module aggregates to exactly that key
for exactly that reason - comparing rows would report every multi-row grid as
hundreds of self-conflicts. A group's footprint on each axis is the union
across its rows, which only matters for new_rto_list (the one axis
views.GROUP_FIELDS leaves out of the hash, so it can legitimately vary within
a group).
"""
import logging
import time
from collections import defaultdict

from django.db.models import Count
from django.db.models.functions import Coalesce

logger = logging.getLogger(__name__)


# --- Axis definitions -------------------------------------------------------
# (field, label) for the RULE 2 categoricals, where blank == wildcard.
CATEGORICAL_AXES = [
    ("product", "Product"),
    ("sub_product", "Sub Product"),
    ("fuel_type", "Fuel Type"),
]

# RULE 6's YES/NO/NA codes, where 'NA' == wildcard and YES/NO are disjoint.
YNN_AXES = [
    ("is_ncb", "NCB"),
    ("is_cpa", "CPA"),
    ("is_zd", "Nil Dep"),
]

# (min_field, max_field, label) for RULE 3, where a NULL bound is open-ended.
NUMERIC_AXES = [
    ("cc_min", "cc_max", "CC"),
    ("sc_min", "sc_max", "Seating Capacity"),
    ("vehicle_age_min", "vehicle_age_max", "Vehicle Age"),
    ("tariff_min", "tariff_max", "Tariff"),
]

# RULE 4. Same open-bound convention, kept separate from NUMERIC_AXES only so
# the drill-down can render dates as dates.
DATE_AXES = [
    ("from_date", "to_date", "Validity"),
]

# (field, label) for the RULE 5a/5b comma-separated cluster lists.
LIST_AXES = [
    ("new_vehicle_makes", "Vehicle Make Cluster"),
    ("new_rto_list", "RTO Cluster"),
]

INTERVAL_AXES = NUMERIC_AXES + DATE_AXES

# Fields the engine never reads. They must NEVER enter groups_conflict or
# _group_contains: the matcher cannot discriminate on them, so treating one as
# an axis would clear pairs it really does resolve to both.
#
# They DO steer classification, though. add_tnc is usually the only thing that
# says why two otherwise-identical rate cards both exist, and the answer
# changes the fix completely:
#
#   identical T&C -> one group is genuinely surplus; deactivate it.
#   differing T&C -> two real, different offers ("Garbage Van" vs
#                    "Construction Eq") that the matcher happens to be blind
#                    to. Deactivating either one throws away a rate the
#                    brokerage actually needs. The grid needs a field the
#                    engine does read - a class, a make cluster, a CC or age
#                    split - so the two stop colliding.
#
# Calling that second case a duplicate would be wrong and would invite exactly
# the destructive fix, which is why classify_pair splits them.
CONTEXT_AXES = [
    ("add_tnc", "Add T&C"),
]

# make_model_class (RULE 2b) has two wildcard spellings, not one.
CLASS_WILDCARDS = {"", "na"}

CONFLICT_DOUBLE_RATE_RISK = "DOUBLE_RATE_RISK"
CONFLICT_EXACT_DUPLICATE = "EXACT_DUPLICATE"
CONFLICT_CONTAINED = "CONTAINED"
CONFLICT_OPEN_ENDED = "OPEN_ENDED"
CONFLICT_PARTIAL = "PARTIAL"

# Lower rank == more actionable. The scan writes pairs in this order so a
# capped result still leads with the pairs worth fixing first. Double Rate
# Risk ranks above Exact Duplicate: unlike every other bucket, its fix is a
# one-line edit to RTOMaster/MakeModelMaster that the brokerage owns outright
# (not the insurer's grid), and one such fix can resolve several pairs at once
# since the same colliding raw code can span many RateMaster groups.
CONFLICT_SEVERITY = {
    CONFLICT_DOUBLE_RATE_RISK: 1,
    CONFLICT_EXACT_DUPLICATE: 2,
    CONFLICT_CONTAINED: 3,
    CONFLICT_OPEN_ENDED: 4,
    CONFLICT_PARTIAL: 5,
}


# --- Normalization (mirrors mapping_engine's df_grid preparation) -----------
def normalize_text(value):
    """Blank-safe strip+lower, matching df_grid's fillna('').str.strip().str.lower()."""
    if value is None:
        return ""
    return str(value).strip().lower()


def normalize_ynn(value):
    """NULL -> 'NA', matching df_grid's fillna('NA').str.strip().str.upper()."""
    if value is None:
        return "NA"
    text = str(value).strip().upper()
    return text or "NA"


def squash_whitespace(value):
    """
    Collapses runs of whitespace so two T&C blocks that differ only in line
    breaks or indentation still compare as the same text.
    """
    if not value:
        return ""
    return " ".join(str(value).split())


def split_cluster(value):
    """
    'ZYX, abc' -> frozenset of {'zyx', 'abc'} - the same comma-split-and-strip
    check_resolved_cluster_match applies to the grid side of RULE 5a/5b.
    """
    if not value:
        return frozenset()
    return frozenset(
        part.strip() for part in str(value).strip().lower().split(",") if part.strip()
    )


# --- Per-axis predicates ----------------------------------------------------
def _wildcard_compatible(a_value, b_value, wildcards):
    return a_value in wildcards or b_value in wildcards or a_value == b_value


def _wildcard_contains(outer, inner, wildcards):
    """Does `outer` accept every policy value `inner` accepts on this axis?"""
    return outer in wildcards or outer == inner


def intervals_overlap(a_min, a_max, b_min, b_max, adjacent_bands_are_separate=False):
    """
    Do [a_min, a_max] and [b_min, b_max] share at least one value, with a NULL
    bound meaning unbounded on that side (RULE 3/4's isna() branches)?

    adjacent_bands_are_separate is for the numeric axes, where a shared bound
    between consecutive bands is how the grids write a boundary, not an
    overlap. "0 - 1000" followed by "1000 - 1500", or "0 - 2500.01" followed by
    "2500.01 - 3500.01", both mean one band stops where the next begins; the
    boundary value belongs to exactly one of them. RULE 3 compares inclusively
    on both sides, so it would technically match a policy sitting exactly on
    that bound to both bands - but that is an artefact of the comparison, not a
    conflict anyone can act on, and reporting it buried the real findings
    (23,163 partial overlaps, of which 556 survive this rule).

    Only a bare touch between two bands approaching from opposite sides is
    excused. Two bands that genuinely pin the same single value - both written
    as "1000 - 1000" - still collide, because neither is stopping where the
    other starts. Any intersection with real width is untouched, so the
    fractional CC and tariff values the MIS carries (1.5, 16.08, 109.2) are
    unaffected.

    Dates don't take this rule: grids give a validity window an inclusive end
    date and start the next window the following day, so two windows sharing a
    day really do both cover a policy incepted on it. That is why DATE_AXES
    stays separate from NUMERIC_AXES.
    """
    if a_min is not None and b_max is not None and a_min > b_max:
        return False
    if b_min is not None and a_max is not None and b_min > a_max:
        return False

    if adjacent_bands_are_separate:
        lows = [v for v in (a_min, b_min) if v is not None]
        highs = [v for v in (a_max, b_max) if v is not None]
        if lows and highs:
            low, high = max(lows), min(highs)
            if low == high and _bands_merely_touch(a_min, a_max, b_min, b_max, low):
                return False

    return True


def _bands_merely_touch(a_min, a_max, b_min, b_max, value):
    """
    Is `value` just the seam between two consecutive bands - one ending there
    while the other starts there - rather than a value both bands really cover?

    A None bound is unbounded, so it always extends past the seam.
    """
    def ends_at(low, high):
        return high == value and (low is None or low < value)

    def starts_at(low, high):
        return low == value and (high is None or high > value)

    return (
        (ends_at(a_min, a_max) and starts_at(b_min, b_max))
        or (ends_at(b_min, b_max) and starts_at(a_min, a_max))
    )


def interval_contains(outer_min, outer_max, inner_min, inner_max):
    """Is every value the inner interval accepts also accepted by the outer one?"""
    if outer_min is not None and (inner_min is None or inner_min < outer_min):
        return False
    if outer_max is not None and (inner_max is None or inner_max > outer_max):
        return False
    return True


def _lists_compatible(a_items, b_items):
    """
    Blank is not a wildcard (check_resolved_cluster_match returns False for an
    empty grid value) - it only collides with another blank, which happens on a
    policy whose own make/RTO is blank.
    """
    if not a_items and not b_items:
        return True
    if not a_items or not b_items:
        return False
    return bool(a_items & b_items)


def _list_contains(outer_items, inner_items):
    if not outer_items and not inner_items:
        return True
    if not outer_items or not inner_items:
        return False
    return inner_items <= outer_items


def _non_list_axes_compatible(a, b):
    """
    Every RULE 1-6 axis except the RTO/Make cluster lists (RULE 5a/5b) - split
    out of groups_conflict so the Double Rate Risk sweep
    (detect_double_rate_risk_pairs) can reuse it with its own, different check
    on those two list axes: a raw-code collision through the master tables
    rather than a shared cluster NAME.

    Ordered cheapest-and-most-selective first so both callers bail out on the
    first discriminating axis.
    """
    if a["insurance_company"] != b["insurance_company"]:
        return False

    for field, _label in CATEGORICAL_AXES:
        if not _wildcard_compatible(a[field], b[field], {""}):
            return False

    if not _wildcard_compatible(a["make_model_class"], b["make_model_class"], CLASS_WILDCARDS):
        return False

    for field, _label in YNN_AXES:
        if not _wildcard_compatible(a[field], b[field], {"NA"}):
            return False

    # Numeric and date axes are checked separately because only the numeric
    # ones treat a shared bound as a band boundary - see intervals_overlap.
    for min_field, max_field, _label in NUMERIC_AXES:
        if not intervals_overlap(
            a[min_field], a[max_field], b[min_field], b[max_field],
            adjacent_bands_are_separate=True,
        ):
            return False

    for min_field, max_field, _label in DATE_AXES:
        if not intervals_overlap(a[min_field], a[max_field], b[min_field], b[max_field]):
            return False

    return True


def groups_conflict(a, b):
    """
    True when no rule in the RULE 1-6 chain can separate these two groups -
    i.e. some policy exists that would match both, which is precisely what
    makes the engine emit MULTIPLE MATCHES.
    """
    if not _non_list_axes_compatible(a, b):
        return False

    for field, _label in LIST_AXES:
        if not _lists_compatible(a[field], b[field]):
            return False

    return True


def _group_contains(outer, inner):
    """Does every policy that matches `inner` also match `outer`?"""
    for field, _label in CATEGORICAL_AXES:
        if not _wildcard_contains(outer[field], inner[field], {""}):
            return False
    if not _wildcard_contains(outer["make_model_class"], inner["make_model_class"], CLASS_WILDCARDS):
        return False
    for field, _label in YNN_AXES:
        if not _wildcard_contains(outer[field], inner[field], {"NA"}):
            return False
    for min_field, max_field, _label in INTERVAL_AXES:
        if not interval_contains(
            outer[min_field], outer[max_field], inner[min_field], inner[max_field]
        ):
            return False
    for field, _label in LIST_AXES:
        if not _list_contains(outer[field], inner[field]):
            return False
    return True


def _groups_identical(a, b):
    for field, _label in CATEGORICAL_AXES + YNN_AXES + LIST_AXES:
        if a[field] != b[field]:
            return False
    if a["make_model_class"] != b["make_model_class"]:
        return False
    for min_field, max_field, _label in INTERVAL_AXES:
        if a[min_field] != b[min_field] or a[max_field] != b[max_field]:
            return False
    return True


def _has_open_bound_mismatch(a, b):
    """One side leaves a bound blank where the other pins it down."""
    for min_field, max_field, _label in INTERVAL_AXES:
        for field in (min_field, max_field):
            if (a[field] is None) != (b[field] is None):
                return True
    return False


def context_differs(a, b):
    """
    True when the two groups disagree on any CONTEXT_AXES field - i.e. they are
    indistinguishable to the matcher but were written as different offers.
    Whitespace-only differences don't count, so a reformatted T&C block doesn't
    read as a change.
    """
    for field, _label in CONTEXT_AXES:
        a_value = squash_whitespace(a.get(field)).lower()
        b_value = squash_whitespace(b.get(field)).lower()
        if a_value != b_value:
            return True
    return False


def classify_pair(a, b):
    """
    Buckets a conflicting pair into exactly one card, or returns None for the
    pairs the dashboard deliberately does not report.

    The types are mutually exclusive and checked most-specific first, so their
    counts sum to the reported total - unlike the Equality cards, which are
    deliberately a narrower lens on rows the Range cards already count.

    DIFFERING T&Cs MEAN IT IS NOT A DUPLICATE, whatever else the two groups
    share. "Garbage Van" and "Construction Eq" are two offers the brokerage
    deliberately sells; the matcher is simply blind to what separates them.
    Neither side can be deactivated without dropping a live rate, and the only
    other fix - re-cutting the ranges or adding a field the engine reads -
    means editing the insurer's own rate sheet, which is not the brokerage's to
    change. That applies just as much to a contained or partial range overlap
    as to two identical rate cards: if the T&Cs say they are different offers,
    there is nothing here anyone can act on. So the sweep counts them and moves
    on rather than parking an unfixable queue beside the fixable ones.

    This one rule takes Contained Ranges from 784 to 9 on the live Rate Master.
    See CONTEXT_AXES.
    """
    if context_differs(a, b):
        return None
    if _groups_identical(a, b):
        return CONFLICT_EXACT_DUPLICATE
    if _group_contains(a, b) or _group_contains(b, a):
        return CONFLICT_CONTAINED
    if _has_open_bound_mismatch(a, b):
        return CONFLICT_OPEN_ENDED
    return CONFLICT_PARTIAL


# --- Drill-down detail ------------------------------------------------------
def _fmt(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


# A group's RTO cluster can carry dozens of codes. Rendering all of them makes
# the drill-down unreadable and bloats the stored detail, while telling nobody
# anything - the Shared column is where the actual collision is. Show enough to
# recognise the cluster, then say how many more there are.
#
# Two limits, because item length varies wildly: 'mh01' next to
# 'liberty_gcv_4w_<7.5t_apr26_andhra_pradesh_-_1_kv'. A count alone would render
# eight short codes (a tidy line) or eight long ones (a 370-character wall), so
# whichever limit bites first wins.
CLUSTER_PREVIEW_LIMIT = 8
CLUSTER_PREVIEW_CHARS = 80


def format_cluster(items, blank="(blank)"):
    """'a, b, c +14 more' - a readable stand-in for a long cluster list."""
    ordered = sorted(items)
    if not ordered:
        return blank

    shown = []
    length = 0
    for item in ordered[:CLUSTER_PREVIEW_LIMIT]:
        # Always keep the first item, however long, so the cell is never just
        # a bare "+N more" with nothing to identify the cluster by.
        if shown and length + len(item) + 2 > CLUSTER_PREVIEW_CHARS:
            break
        shown.append(item)
        length += len(item) + 2

    remaining = len(ordered) - len(shown)
    text = ", ".join(shown)
    return f"{text} +{remaining} more" if remaining else text


def _overlap_span(a_min, a_max, b_min, b_max):
    """The values both intervals accept, as (low, high); None means unbounded."""
    lows = [v for v in (a_min, b_min) if v is not None]
    highs = [v for v in (a_max, b_max) if v is not None]
    return (max(lows) if lows else None, min(highs) if highs else None)


def describe_pair(a, b, hidden_axis_codes=None):
    """
    Per-axis breakdown for the drill-down table: what each side carries, and -
    for the range axes - the exact span where they collide, which is the bit
    that tells someone which boundary to move.

    hidden_axis_codes: {field: sorted raw codes}, for Double Rate Risk pairs
    only. a[field] and b[field] there are DIFFERENT cluster names with no
    shared item - describe_pair's default "both blank"/shared-name text would
    read as "no collision here", which is wrong: the two names resolve to
    overlapping raw codes through RTOMaster/MakeModelMaster, just not through
    a name either group directly references. is_hidden_risk marks that row so
    the template can call out the real cause instead of the absence of one.
    """
    hidden_axis_codes = hidden_axis_codes or {}
    axes = []
    for field, label in CATEGORICAL_AXES:
        axes.append({
            "label": label,
            "a": a[field] or "(any)",
            "b": b[field] or "(any)",
            "overlap": "wildcard" if not a[field] or not b[field] else "identical",
        })
    axes.append({
        "label": "Vehicle Class",
        "a": a["make_model_class"] or "(any)",
        "b": b["make_model_class"] or "(any)",
        "overlap": (
            "wildcard"
            if a["make_model_class"] in CLASS_WILDCARDS or b["make_model_class"] in CLASS_WILDCARDS
            else "identical"
        ),
    })
    for min_field, max_field, label in INTERVAL_AXES:
        low, high = _overlap_span(a[min_field], a[max_field], b[min_field], b[max_field])
        axes.append({
            "label": label,
            "a": [_fmt(a[min_field]), _fmt(a[max_field])],
            "b": [_fmt(b[min_field]), _fmt(b[max_field])],
            "overlap": [_fmt(low), _fmt(high)],
            "is_range": True,
        })
    for field, label in YNN_AXES:
        axes.append({
            "label": label,
            "a": a[field],
            "b": b[field],
            "overlap": "wildcard" if a[field] == "NA" or b[field] == "NA" else "identical",
        })
    for field, label in LIST_AXES:
        hidden_codes = hidden_axis_codes.get(field)
        if hidden_codes:
            axes.append({
                "label": label,
                "a": format_cluster(a[field]),
                "b": format_cluster(b[field]),
                "overlap": f"shares raw code(s): {format_cluster(hidden_codes)}",
                "is_hidden_risk": True,
            })
        else:
            axes.append({
                "label": label,
                "a": format_cluster(a[field]),
                "b": format_cluster(b[field]),
                "overlap": format_cluster(a[field] & b[field], blank="both blank"),
            })
    # Context last, flagged so the drill-down can separate it from the fields
    # that actually drove the conflict.
    for field, label in CONTEXT_AXES:
        a_value = (a.get(field) or "").strip()
        b_value = (b.get(field) or "").strip()
        same = squash_whitespace(a_value).lower() == squash_whitespace(b_value).lower()
        axes.append({
            "label": label,
            "a": a_value or "(blank)",
            "b": b_value or "(blank)",
            "overlap": "identical" if same else "differs",
            "is_context": True,
        })
    return axes


def overlap_summary(axes):
    """
    The one-line version of describe_pair, for the drill-down's summary column:
    the range axes that actually pin the collision down somewhere (an axis
    where both sides are wide open says nothing about where to look), plus any
    hidden-risk axis, since for a Double Rate Risk pair that IS where the
    collision is - a range chip alone would show every axis lining up with
    nothing pointing at the actual cause.
    """
    summary = []
    for axis in axes:
        if axis.get("is_hidden_risk"):
            summary.append({"label": axis["label"], "text": axis["overlap"]})
            continue
        if not axis.get("is_range"):
            continue
        low, high = axis["overlap"]
        if low is None and high is None:
            continue
        summary.append({
            "label": axis["label"],
            "low": low,
            "high": high,
        })
    return summary


# --- Loading ----------------------------------------------------------------
GROUP_VALUE_FIELDS = [
    "grid_key",
    "insurance_company",
    "product__name",
    "sub_product__name",
    "fuel_type__name",
    "make_model_class__name",
    "is_ncb__code",
    "is_cpa__code",
    "is_zd__code",
    "new_vehicle_makes",
    "new_rto_list",
    # Context only - see CONTEXT_AXES. Safe to add to the GROUP BY because
    # add_tnc is part of views.GROUP_FIELDS, so it is constant within a group
    # and cannot split one group into extra rows here.
    "add_tnc",
    "cc_min", "cc_max",
    "sc_min", "sc_max",
    "vehicle_age_min", "vehicle_age_max",
    "tariff_min", "tariff_max",
    "from_date", "to_date",
]


def load_active_groups():
    """
    Every ACTIVE, non-deleted rate group, in the shape groups_conflict expects.

    Aggregated in the DB rather than pulling ~100k rows into Python: rows inside
    one group agree on everything in views.GROUP_FIELDS, so .values() + Count
    collapses each group to a handful of rows (one per distinct new_rto_list,
    the only axis the group hash leaves out), which are merged below.

    Note COALESCE(group_id, id) can theoretically collide - a group_id equal to
    some group-less row's primary key - exactly as it can in
    process_mis_mapping's own final resolution and in the Grid Summary pivot.
    Reproducing that is correct here: this predicts the engine's behaviour, so
    it has to share the engine's key.
    """
    from .models import RateMaster

    rows = (
        RateMaster.objects
        .filter(status="ACTIVE", is_deleted="NO")
        .annotate(grid_key=Coalesce("group_id", "id"))
        .values(*GROUP_VALUE_FIELDS)
        .annotate(row_count=Count("id"))
    )

    groups = {}
    for row in rows:
        key = row["grid_key"]
        group = groups.get(key)
        if group is None:
            group = {
                "grid_key": key,
                "insurance_company": normalize_text(row["insurance_company"]),
                "insurer_display": (row["insurance_company"] or "").strip(),
                "product": normalize_text(row["product__name"]),
                "sub_product": normalize_text(row["sub_product__name"]),
                "fuel_type": normalize_text(row["fuel_type__name"]),
                "make_model_class": normalize_text(row["make_model_class__name"]),
                "is_ncb": normalize_ynn(row["is_ncb__code"]),
                "is_cpa": normalize_ynn(row["is_cpa__code"]),
                "is_zd": normalize_ynn(row["is_zd__code"]),
                "new_vehicle_makes": split_cluster(row["new_vehicle_makes"]),
                "new_rto_list": split_cluster(row["new_rto_list"]),
                "add_tnc": row["add_tnc"],
                "row_count": 0,
            }
            for min_field, max_field, _label in INTERVAL_AXES:
                group[min_field] = row[min_field]
                group[max_field] = row[max_field]
            groups[key] = group
        else:
            # Only new_rto_list can differ between rows of the same group; take
            # the union, since the group as a whole is reachable through any of
            # its rows' clusters.
            group["new_rto_list"] = group["new_rto_list"] | split_cluster(row["new_rto_list"])
        group["row_count"] += row["row_count"]

    return list(groups.values())


# --- Double Rate Risk: master-table cluster collisions ----------------------
#
# Everything above compares RateMaster groups by the cluster NAMES each one
# references (RULE 5a/5b's own comparison: does new_rto_list/new_vehicle_makes
# share an item). That misses a real class of collision one layer further
# upstream: two DIFFERENT cluster names can still resolve to the same MIS raw
# code if that code was pasted into more than one RTOMaster.rto_cluster or
# MakeModelMaster.make_model_cluster list. mapping_engine.build_master_lookup
# resolves a raw MIS value to EVERY name whose cluster contains it and tries
# all of them (see check_resolved_cluster_match) - so a policy carrying that
# code can match both RateMaster groups even though their cluster NAMES never
# intersect and every group-vs-group comparison above reports them as fine.
#
# This is deliberately not folded into groups_conflict/classify_pair: it needs
# the master tables preloaded (build_cluster_code_index), and by construction
# it only ever fires on pairs the primary sweep has ALREADY decided don't
# conflict (their list-axis names don't match) - so it can never double-count
# a pair the cards above already show, and doesn't touch classify_pair's
# existing, tested behaviour at all.


# Literal cluster items confirmed, by inspecting the real RTOMaster data, to
# be padding rather than real codes - see build_cluster_code_index.
PLACEHOLDER_CODES = {"0"}


def build_cluster_code_index(name_field, cluster_field, model):
    """
    {cluster name (lower, stripped) -> set of raw codes (upper, stripped)} for
    RTOMaster or MakeModelMaster, loaded once per scan rather than per pair.

    Keys are normalized to match load_active_groups' new_rto_list/
    new_vehicle_makes frozensets (split_cluster lower-cases); values are
    normalized to match build_master_lookup's own upper-cased comparison, so a
    collision found here is one the mapping engine would actually hit.

    Excludes PLACEHOLDER_CODES: several RTOMaster rows carry a bare "0" as a
    literal item (e.g. ROYAL_MAY26_REST_OF__ANDHRA_PRADESH:
    "...,AP30,0,0,0,0,0,0,0,0,0,0,0,0,0") - almost certainly padding from
    however the cluster strings were generated, not a real RTO/make-model
    code. Left in, it alone connected ~1,450 otherwise-unrelated pairs across
    a dozen Andhra Pradesh/Telangana clusters that share nothing real - a
    single data artifact swamping every genuine finding underneath it.
    """
    index = {}
    for name, cluster in model.objects.values_list(name_field, cluster_field):
        if not cluster:
            continue
        codes = {
            code.strip().upper() for code in str(cluster).split(",") if code.strip()
        } - PLACEHOLDER_CODES
        if codes:
            index[str(name).strip().lower()] = codes
    return index


def _codes_for_names(names, code_index):
    codes = set()
    for name in names:
        codes |= code_index.get(name, set())
    return codes


def _axis_conflict_status(a_names, b_names, code_index):
    """
    Does this list axis (RTO or Make cluster) fail to separate two groups, and
    if so, is that visible through the cluster names themselves or only
    through the raw codes underneath?

    Returns (compatible, hidden_codes). compatible=False means the axis
    genuinely discriminates the two groups - no collision here at all, by name
    or by code. hidden_codes is non-empty only when the names DON'T overlap
    (the ordinary case _lists_compatible already covers) but the master
    tables' raw codes do - that's the collision this sweep exists to find.
    """
    if _lists_compatible(a_names, b_names):
        return True, frozenset()
    hidden = _codes_for_names(a_names, code_index) & _codes_for_names(b_names, code_index)
    return bool(hidden), hidden


def detect_double_rate_risk_pairs(groups, rto_code_index, make_model_code_index, cap=None):
    """
    Pairs where every RULE 1-6 axis except RTO/Make lines up, AND at least one
    of those two list axes only conflicts through a raw code shared between
    two differently-named clusters - never through the primary sweep's
    name-based check, since a pair that clears BOTH axes by name is already
    reported (or not) by classify_pair up in detect_overlap_pairs.

    Unlike every other conflict type, this one does NOT exclude pairs whose
    Add T&C differs. Everywhere else, differing T&Cs mean the fix would be
    re-cutting the INSURER's own grid, which isn't the brokerage's to touch -
    that's why classify_pair drops those pairs entirely. Here the object that
    needs fixing is RTOMaster/MakeModelMaster: the brokerage's own cluster
    definitions, always editable, regardless of whether the two RateMaster
    rows happen to be genuinely different offers.

    Returns (pairs, true_count, was_capped) - same shape as detect_overlap_pairs
    (pairs vs counts_by_type vs capped_types) so run_overlap_scan can report
    the true total even when the stored subset was capped.
    """
    by_insurer = defaultdict(list)
    for group in groups:
        by_insurer[group["insurance_company"]].append(group)

    found = []
    for members in by_insurer.values():
        for first, second in _candidate_pairs(members):
            if not _non_list_axes_compatible(first, second):
                continue

            rto_ok, rto_hidden = _axis_conflict_status(
                first["new_rto_list"], second["new_rto_list"], rto_code_index
            )
            if not rto_ok:
                continue
            make_ok, make_hidden = _axis_conflict_status(
                first["new_vehicle_makes"], second["new_vehicle_makes"], make_model_code_index
            )
            if not make_ok:
                continue
            if not rto_hidden and not make_hidden:
                # Both axes line up by cluster name - already the primary
                # sweep's territory, not a hidden collision.
                continue

            if first["grid_key"] <= second["grid_key"]:
                low, high = first, second
            else:
                low, high = second, first

            hidden_axis_codes = {}
            if rto_hidden:
                hidden_axis_codes["new_rto_list"] = rto_hidden
            if make_hidden:
                hidden_axis_codes["new_vehicle_makes"] = make_hidden

            axes = describe_pair(low, high, hidden_axis_codes=hidden_axis_codes)
            found.append({
                "insurance_company": low["insurer_display"],
                "group_key_a": low["grid_key"],
                "group_key_b": high["grid_key"],
                "conflict_type": CONFLICT_DOUBLE_RATE_RISK,
                "severity_rank": CONFLICT_SEVERITY[CONFLICT_DOUBLE_RATE_RISK],
                "row_count_a": low["row_count"],
                "row_count_b": high["row_count"],
                "detail": {"axes": axes, "summary": overlap_summary(axes)},
            })

    found.sort(key=lambda p: (p["insurance_company"], p["group_key_a"], p["group_key_b"]))
    true_count = len(found)
    was_capped = cap is not None and true_count > cap
    if was_capped:
        found = found[:cap]
    return found, true_count, was_capped


# --- Pair sweep -------------------------------------------------------------
# Every axis _candidate_pairs pre-filters on before the expensive numeric/date/
# list checks - each is (field, its wildcard value set), matching exactly what
# _wildcard_compatible already treats as "matches anything" inside
# _non_list_axes_compatible/groups_conflict. Bucketing on all of them (not
# product alone) is what keeps one insurer with many distinct sub_product/
# fuel_type/class/NCB-CPA-ZD combinations from forcing an O(n^2) scan over
# every one of its rows - a single huge insurer/product bucket was measured
# timing out a full scan in production (15+ minutes, never finishing) despite
# taking under a minute against a smaller local copy of the same Rate Master.
WILDCARD_BUCKET_AXES = (
    [(field, frozenset({""})) for field, _label in CATEGORICAL_AXES]
    + [("make_model_class", frozenset(CLASS_WILDCARDS))]
    + [(field, frozenset({"NA"})) for field, _label in YNN_AXES]
)


def _bucket_key(group):
    return tuple(group[field] for field, _wildcards in WILDCARD_BUCKET_AXES)


def _bucket_keys_compatible(key_a, key_b):
    """
    Could ANY pair drawn from these two buckets still pass every
    WILDCARD_BUCKET_AXES check? True unless some axis has two different,
    both-non-wildcard values - exactly _wildcard_compatible's own rule,
    applied once per bucket-pair instead of once per group-pair.
    """
    for (_field, wildcards), value_a, value_b in zip(WILDCARD_BUCKET_AXES, key_a, key_b):
        if value_a in wildcards or value_b in wildcards:
            continue
        if value_a != value_b:
            return False
    return True


def _candidate_pairs(members):
    """
    Every unordered pair from one insurer's groups, exactly once, skipping the
    ones WILDCARD_BUCKET_AXES already separates.

    Pure optimisation - groups_conflict re-checks every one of these axes
    itself, so this can only ever drop pairs it would have rejected anyway.
    Groups are bucketed by their exact (product, sub_product, fuel_type,
    class, ncb, cpa, zd) tuple; two buckets are only compared at all when
    _bucket_keys_compatible says every axis could still match (same value, or
    either side wildcard on that axis). The number of distinct buckets is
    normally far smaller than the number of groups in them, so the expensive
    part - the actual pairwise scan - only ever runs within one bucket or
    across a pair of compatible buckets, never across the insurer's full
    group list at once.
    """
    buckets = defaultdict(list)
    for group in members:
        buckets[_bucket_key(group)].append(group)

    keys = list(buckets.keys())
    for i, key_a in enumerate(keys):
        bucket_a = buckets[key_a]
        for first_idx, first in enumerate(bucket_a):
            for second in bucket_a[first_idx + 1:]:
                yield first, second

        for key_b in keys[i + 1:]:
            if not _bucket_keys_compatible(key_a, key_b):
                continue
            for first in bucket_a:
                for second in buckets[key_b]:
                    yield first, second


# How many pairs of each type one scan stores. The three actionable buckets get
# a ceiling high enough to be irrelevant in practice (45, 784 and 2 on the live
# Rate Master) but still bounded, so a pathological grid can't write millions of
# rows. PARTIAL is capped far lower on purpose: it runs to tens of thousands of
# pairs, which is a health metric rather than a queue anyone works through, and
# storing a browsable sample says everything the full list would. The card
# always shows the TRUE count from counts_by_type, never the stored subset, so
# capping never understates the problem.
DEFAULT_TYPE_CAPS = {
    CONFLICT_DOUBLE_RATE_RISK: 20000,
    CONFLICT_EXACT_DUPLICATE: 20000,
    CONFLICT_CONTAINED: 20000,
    CONFLICT_OPEN_ENDED: 20000,
    CONFLICT_PARTIAL: 2000,
}


def detect_overlap_pairs(groups, type_caps=None):
    """
    Returns (pairs, counts_by_type, capped_types, tnc_differing_skipped):
      pairs           - the stored subset, most-actionable first
      counts_by_type  - the TRUE count per conflict type, before any capping
      capped_types    - the types whose stored subset was truncated
      tnc_differing_skipped- real conflicts classify_pair deliberately drops
                        (identical rules, different T&C - see classify_pair).
                        Returned rather than silently swallowed so the
                        dashboard can say the sweep saw them and chose not to
                        list them.

    The sweep keeps only group references while it runs and builds the per-axis
    detail afterwards, for the pairs actually being kept - on the live Rate
    Master that is ~2,800 detail dicts instead of ~36,000.
    """
    if type_caps is None:
        type_caps = DEFAULT_TYPE_CAPS

    by_insurer = defaultdict(list)
    for group in groups:
        by_insurer[group["insurance_company"]].append(group)

    found = []
    tnc_differing_skipped = 0
    counts_by_type = {conflict_type: 0 for conflict_type in CONFLICT_SEVERITY}
    for members in by_insurer.values():
        for first, second in _candidate_pairs(members):
            if not groups_conflict(first, second):
                continue
            # Stable a<b ordering keeps one pair from being stored twice under
            # swapped keys across scans.
            if first["grid_key"] <= second["grid_key"]:
                low, high = first, second
            else:
                low, high = second, first
            conflict_type = classify_pair(low, high)
            if conflict_type is None:
                tnc_differing_skipped += 1
                continue
            counts_by_type[conflict_type] += 1
            found.append((
                CONFLICT_SEVERITY[conflict_type],
                low["insurer_display"],
                low["grid_key"],
                high["grid_key"],
                conflict_type,
                low,
                high,
            ))

    found.sort(key=lambda item: item[:4])

    kept = []
    capped_types = []
    stored_by_type = defaultdict(int)
    for severity, insurer, key_a, key_b, conflict_type, low, high in found:
        cap = type_caps.get(conflict_type)
        if cap is not None and stored_by_type[conflict_type] >= cap:
            if conflict_type not in capped_types:
                capped_types.append(conflict_type)
            continue
        stored_by_type[conflict_type] += 1
        axes = describe_pair(low, high)
        kept.append({
            "insurance_company": insurer,
            "group_key_a": key_a,
            "group_key_b": key_b,
            "conflict_type": conflict_type,
            "severity_rank": severity,
            "row_count_a": low["row_count"],
            "row_count_b": high["row_count"],
            # No tnc_differs flag: classify_pair has already dropped every pair
            # whose T&Cs disagree, so a listed pair's two groups are always
            # interchangeable. The Add T&C row in `axes` still shows the text
            # itself, which now always reads "identical".
            "detail": {
                "axes": axes,
                "summary": overlap_summary(axes),
            },
        })

    return kept, counts_by_type, capped_types, tnc_differing_skipped


def _largest_bucket_sizes(groups):
    """
    Diagnostic only - not used by detection itself. Reports the single
    largest insurer group count, and the single largest WILDCARD_BUCKET_AXES
    bucket within any insurer (i.e. the biggest set of groups still identical
    on every axis _candidate_pairs pre-filters on). Logged once per scan so
    that if a future scan is ever slow again, the logs say WHERE the size is
    concentrated instead of just a bare timeout traceback - a large insurer
    total with a small largest sub-bucket means the multi-axis bucketing is
    doing its job; a large sub-bucket means the remaining O(n^2) numeric/date
    scan within it is the next thing worth optimizing.
    """
    by_insurer = defaultdict(list)
    for group in groups:
        by_insurer[group["insurance_company"]].append(group)

    largest_insurer = max((len(members) for members in by_insurer.values()), default=0)

    largest_bucket = 0
    for members in by_insurer.values():
        bucket_sizes = defaultdict(int)
        for group in members:
            bucket_sizes[_bucket_key(group)] += 1
        if bucket_sizes:
            largest_bucket = max(largest_bucket, max(bucket_sizes.values()))

    return largest_insurer, largest_bucket


def run_overlap_scan(scan_id, type_caps=None):
    """
    Fills one RateOverlapScan with its pairs and marks it COMPLETED.

    Each scan owns its own pairs (FK + cascade), so a run in progress never
    disturbs the results already on screen, and older scans are pruned only
    once this one has succeeded.
    """
    from django.utils import timezone
    from .models import MakeModelMaster, RateOverlapPair, RateOverlapScan, RTOMaster

    scan = RateOverlapScan.objects.get(id=scan_id)
    try:
        started = time.monotonic()
        groups = load_active_groups()
        largest_insurer, largest_bucket = _largest_bucket_sizes(groups)
        logger.info(
            "Overlap scan %s: loaded %d active groups in %.1fs "
            "(largest insurer=%d groups, largest same-axis bucket=%d groups)",
            scan_id, len(groups), time.monotonic() - started, largest_insurer, largest_bucket,
        )

        step = time.monotonic()
        pairs, counts_by_type, capped_types, tnc_differing_skipped = detect_overlap_pairs(
            groups, type_caps=type_caps
        )
        logger.info(
            "Overlap scan %s: primary sweep found %d pair(s) in %.1fs",
            scan_id, sum(counts_by_type.values()), time.monotonic() - step,
        )

        caps = type_caps if type_caps is not None else DEFAULT_TYPE_CAPS
        step = time.monotonic()
        rto_code_index = build_cluster_code_index("rto_name", "rto_cluster", RTOMaster)
        make_model_code_index = build_cluster_code_index(
            "make_model_name", "make_model_cluster", MakeModelMaster
        )
        double_rate_pairs, double_rate_count, double_rate_capped = detect_double_rate_risk_pairs(
            groups, rto_code_index, make_model_code_index,
            cap=caps.get(CONFLICT_DOUBLE_RATE_RISK),
        )
        logger.info(
            "Overlap scan %s: double rate risk sweep found %d pair(s) in %.1fs",
            scan_id, double_rate_count, time.monotonic() - step,
        )
        counts_by_type[CONFLICT_DOUBLE_RATE_RISK] = double_rate_count
        pairs = pairs + double_rate_pairs
        if double_rate_capped:
            capped_types = capped_types + [CONFLICT_DOUBLE_RATE_RISK]

        RateOverlapPair.objects.bulk_create(
            [RateOverlapPair(scan=scan, **pair) for pair in pairs],
            batch_size=1000,
        )
        logger.info(
            "Overlap scan %s: completed in %.1fs total",
            scan_id, time.monotonic() - started,
        )

        scan.groups_scanned = len(groups)
        # The true total, not len(pairs) - the cards must report what exists,
        # not what fitted.
        scan.pairs_found = sum(counts_by_type.values())
        scan.type_counts = counts_by_type
        scan.capped_types = capped_types
        scan.was_capped = bool(capped_types)
        scan.tnc_differing_skipped = tnc_differing_skipped
        scan.status = RateOverlapScan.STATUS_COMPLETED
        scan.finished_at = timezone.now()
        scan.save(update_fields=[
            "groups_scanned", "pairs_found", "type_counts", "capped_types",
            "was_capped", "tnc_differing_skipped", "status", "finished_at",
        ])

        # Keep only the newest completed scan's pairs; the cascade drops the
        # superseded rows with their scan.
        RateOverlapScan.objects.filter(
            status=RateOverlapScan.STATUS_COMPLETED
        ).exclude(id=scan.id).delete()
    except Exception as exc:
        scan.status = RateOverlapScan.STATUS_FAILED
        scan.error_message = str(exc)
        scan.finished_at = timezone.now()
        scan.save(update_fields=["status", "error_message", "finished_at"])
        raise
    return scan
