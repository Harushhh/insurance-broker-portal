"""Rate Master validity rules (Pi/Po type and margin, ranges, equality, tariff) and the grid summary."""
from django.db.models import Q, F, Count, Value, FloatField
from django.db.models.functions import Coalesce, Greatest, Round
import logging
from ..models import RateMaster, MISFile


logger = logging.getLogger("security")


# Fields shown in the Policy Details column's collapsed summary before "View
# Details" is clicked. payload's keys are whatever column headers the
# uploaded MIS file actually had (see mapping_engine._extract_failed_rows_from_df
# — it keeps the source file's own header text), so these are matched as
# case-insensitive substrings rather than exact keys.
POLICY_DETAILS_SUMMARY_TARGETS = ["vehicle make", "model", "policy type", "sub product"]


POLICY_DETAILS_SUMMARY_LIMIT = 3


def _split_payload_summary(payload):
    """
    Pick up to POLICY_DETAILS_SUMMARY_LIMIT of a MISFailedRow's most
    identifying payload fields for the table row's collapsed view; everything
    else is returned separately for the "View Details" expansion. Falls back
    to the first few payload entries if none of the target fields are present
    (e.g. a Health row with no vehicle columns), so the summary is never blank
    while payload itself has data.

    Substring matching is deliberate here and is why this can't share code with
    _normalized_payload (used by the Missing Make/Model page), which needs an
    exact normalized-key lookup of known columns.
    """
    items = [(k, v) for k, v in (payload or {}).items() if v not in (None, "")]

    summary = []
    used_keys = set()
    for target in POLICY_DETAILS_SUMMARY_TARGETS:
        if len(summary) >= POLICY_DETAILS_SUMMARY_LIMIT:
            break
        for k, v in items:
            if k in used_keys:
                continue
            if target in k.strip().lower():
                summary.append((k, v))
                used_keys.add(k)
                break

    if not summary:
        summary = items[:POLICY_DETAILS_SUMMARY_LIMIT]
        used_keys = {k for k, _ in summary}

    rest = [(k, v) for k, v in items if k not in used_keys]
    return summary, rest


def _sync_unsynced_mis_files():
    """
    Backfill for MISFile rows that completed before MISFailedRow existed (or
    whose direct write at process time failed) — cheap indexed no-op query
    in steady state once every completed file has been synced once.
    """
    unsynced = (
        MISFile.objects.filter(status="COMPLETED", health_synced_at__isnull=True)
        .exclude(processed_file="")
        .exclude(processed_file__isnull=True)
    )
    if not unsynced.exists():
        return
    from ..mapping_engine import sync_failed_rows_for_file
    for mis_file in unsynced:
        try:
            sync_failed_rows_for_file(mis_file)
        except Exception:
            logger.exception("Could not sync MIS failure rows for MISFile %s", mis_file.id)


# The full set of PI (premium/tariff) rate fields on RateMaster — same
# grouping used elsewhere for PI-side rate handling (see RATE_FIELDS et al).
# pi_type says which of these should carry the rate; the rest must be 0/blank.
PI_RATE_FIELDS = [
    "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
    "pi_net_rate", "pi_flat_amount", "pi_vli",
]


PI_TYPE_RULES = [
    ("On Net", ["pi_net_rate"]),
    ("On OD", ["pi_od_rate"]),
    ("On TP", ["pi_tp_rate"]),
    ("On OD and TP", ["pi_od_rate", "pi_tp_rate"]),
]


PI_TYPE_RULES_MAP = dict(PI_TYPE_RULES)


def _rate_master_pi_type_violations_qs(label):
    """
    RateMaster rows whose pi_type is `label` but that have a nonzero value in
    a PI rate field other than the one(s) that pi_type says should hold the
    rate. NULL/0 fields are "unset" and never violate — only a genuinely
    populated field outside the allowed set counts as an error.
    """
    allowed_fields = PI_TYPE_RULES_MAP.get(label)
    if allowed_fields is None:
        return RateMaster.objects.none()
    other_fields_q = Q()
    for field in PI_RATE_FIELDS:
        if field in allowed_fields:
            continue
        other_fields_q |= ~Q(**{field: 0})
    return RateMaster.objects.filter(is_deleted="NO", pi_type=label).filter(other_fields_q)


def _rate_master_pi_type_error_counts():
    return [
        {"label": label, "count": _rate_master_pi_type_violations_qs(label).count()}
        for label, _ in PI_TYPE_RULES
    ]


# Range-validity rules — a RateMaster row is only valid when each "min" bound
# is strictly less than its paired "max" bound. A bound that's NULL can't be
# compared, so — same "unset never violates" convention as the pi_type rules
# above — a row is only flagged once both bounds in the pair are populated.

# STP (Standard Third Party) tariff_max is statutorily fixed rather than a
# free-form upper bound, so its range rule is checked against this constant
# *in addition to* the general tariff_min < tariff_max rule, not instead of it.
STP_SUB_PRODUCT = "STP"


STP_TARIFF_MAX = 100.01


def _rate_master_min_max_violations_qs(min_field, max_field):
    """RateMaster rows where min_field/max_field are both set but min isn't strictly less than max."""
    lookup = {
        f"{min_field}__isnull": False,
        f"{max_field}__isnull": False,
        f"{min_field}__gte": F(max_field),
    }
    return RateMaster.objects.filter(is_deleted="NO", **lookup)


def _rate_master_tariff_violations_qs():
    """
    RateMaster rows that break the tariff_min < tariff_max rule, or — for STP
    sub-products — don't have tariff_max pinned to exactly STP_TARIFF_MAX.
    """
    general_bad = Q(tariff_min__isnull=False, tariff_max__isnull=False, tariff_min__gte=F("tariff_max"))
    stp_bad = (
        Q(sub_product__name__iexact=STP_SUB_PRODUCT, tariff_max__isnull=False)
        & ~Q(tariff_max=STP_TARIFF_MAX)
    )
    return RateMaster.objects.filter(is_deleted="NO").filter(general_bad | stp_bad)


# One entry per Range Validation Errors card. "columns" are the extra
# (field_path, header) pairs the Affected Rows drill-down shows beyond the
# common Group ID / Insurer columns, so the user can see exactly which values
# broke the rule — field_path is anything .values() accepts, including a
# relation lookup like sub_product__name.
RANGE_ERROR_RULES = [
    {
        "label": "Invalid Vehicle Age Range",
        "violations_fn": lambda: _rate_master_min_max_violations_qs("vehicle_age_min", "vehicle_age_max"),
        "columns": [("vehicle_age_min", "Vehicle Age Min"), ("vehicle_age_max", "Vehicle Age Max")],
    },
    {
        "label": "Invalid CC Range",
        "violations_fn": lambda: _rate_master_min_max_violations_qs("cc_min", "cc_max"),
        "columns": [("cc_min", "CC Min"), ("cc_max", "CC Max")],
    },
    {
        "label": "Invalid Seating Capacity Range",
        "violations_fn": lambda: _rate_master_min_max_violations_qs("sc_min", "sc_max"),
        "columns": [("sc_min", "SC Min"), ("sc_max", "SC Max")],
    },
    {
        "label": "Invalid Tariff Range",
        "violations_fn": _rate_master_tariff_violations_qs,
        "columns": [
            ("sub_product__name", "Sub Product"),
            ("tariff_min", "Tariff Min"),
            ("tariff_max", "Tariff Max"),
        ],
    },
]


RANGE_ERROR_RULES_MAP = {rule["label"]: rule for rule in RANGE_ERROR_RULES}


def _rate_master_range_error_counts():
    return [{"label": rule["label"], "count": rule["violations_fn"]().count()} for rule in RANGE_ERROR_RULES]


def _rate_master_equality_violations_qs(min_field, max_field):
    """RateMaster rows where min_field/max_field are both set and exactly equal."""
    lookup = {
        f"{min_field}__isnull": False,
        f"{max_field}__isnull": False,
        min_field: F(max_field),
    }
    return RateMaster.objects.filter(is_deleted="NO", **lookup)


# One entry per Equality Errors card — same shape as RANGE_ERROR_RULES, but
# flagging the narrower "min was typed in as exactly equal to max" mistake
# rather than "min isn't strictly less than max". Every row these catch is
# therefore already counted by the matching Range Validation Errors card too
# (equality is a subset of "not strictly less than") — this is a deliberately
# more specific lens on the same data, not a separate class of bad row.
EQUALITY_ERROR_RULES = [
    {
        "label": "Vehicle Age Equality",
        "violations_fn": lambda: _rate_master_equality_violations_qs("vehicle_age_min", "vehicle_age_max"),
        "columns": [("vehicle_age_min", "Vehicle Age Min"), ("vehicle_age_max", "Vehicle Age Max")],
    },
    {
        "label": "CC Equality",
        "violations_fn": lambda: _rate_master_equality_violations_qs("cc_min", "cc_max"),
        "columns": [("cc_min", "CC Min"), ("cc_max", "CC Max")],
    },
    {
        "label": "SC Equality",
        "violations_fn": lambda: _rate_master_equality_violations_qs("sc_min", "sc_max"),
        "columns": [("sc_min", "SC Min"), ("sc_max", "SC Max")],
    },
    {
        "label": "Tariff Equality",
        "violations_fn": lambda: _rate_master_equality_violations_qs("tariff_min", "tariff_max"),
        "columns": [("tariff_min", "Tariff Min"), ("tariff_max", "Tariff Max")],
    },
    {
        "label": "Validity Date Equality",
        "violations_fn": lambda: _rate_master_equality_violations_qs("from_date", "to_date"),
        "columns": [("from_date", "From Date"), ("to_date", "To Date")],
    },
]


EQUALITY_ERROR_RULES_MAP = {rule["label"]: rule for rule in EQUALITY_ERROR_RULES}


def _rate_master_equality_error_counts():
    return [{"label": rule["label"], "count": rule["violations_fn"]().count()} for rule in EQUALITY_ERROR_RULES]


# Pi vs Po rate rules — a RateMaster row is only valid when its Pi (incoming)
# rate exceeds its paired Po (payout) rate by exactly RATE_MASTER_PI_PO_MARGIN
# (the broker's kept margin), and pi_type exactly matches po_type. Same
# "unset never violates" convention as the range/equality rules above — a
# row is only flagged once both sides of the pair are populated.
#
# A Pi rate at or below the required margin (e.g. <= 7) is a deliberate
# special case rather than a violation: there's no way to subtract a full
# margin from a Pi that small without landing on a negative Po, so the rule
# flips to "Po must be exactly 0" instead of "Pi - Po == margin" whenever
# Pi <= required_margin.
#
# A raw negative gap (Po > Pi) is clamped to 0 before comparing against the
# required margin, so it reads as "no margin" rather than a negative number.
RATE_MASTER_PI_PO_MARGIN = 7


def _rate_master_pi_po_rate_violations_qs(pi_field, po_field, required_margin=RATE_MASTER_PI_PO_MARGIN):
    """
    RateMaster rows where pi_field/po_field are both set and break the margin
    rule: Greatest(pi_field - po_field, 0) isn't exactly `required_margin`,
    EXCEPT when pi_field <= required_margin -- there the rule flips to
    po_field must be exactly 0 (can't subtract a full margin off a Pi that
    small without going negative).
    """
    both_set = Q(**{f"{pi_field}__isnull": False, f"{po_field}__isnull": False})
    pi_le_margin_exempt = Q(**{f"{pi_field}__lte": required_margin, po_field: 0})
    qs = (
        RateMaster.objects.filter(is_deleted="NO")
        .filter(both_set)
        .exclude(pi_le_margin_exempt)
        .annotate(
            # Round to 2dp before comparing: pi_field/po_field are plain
            # FloatFields (IEEE-754 doubles), so e.g. 19.65 - 12.65 lands on
            # 6.999999999999998 rather than 7.0 in raw floating point --
            # rounding avoids flagging a mathematically-exact margin as a
            # violation purely from binary float representation error. Rates
            # are limited to 2 decimal places on entry (see RATE_DECIMAL_FIELDS),
            # so rounding the margin to the same precision never masks a real
            # violation.
            margin=Round(
                Greatest(F(pi_field) - F(po_field), Value(0.0), output_field=FloatField()),
                2,
            )
        )
    )
    return qs.exclude(margin=required_margin)


def _rate_master_pi_po_type_violations_qs():
    """RateMaster rows where pi_type/po_type are both set but don't exactly match."""
    return (
        RateMaster.objects.filter(is_deleted="NO", pi_type__isnull=False, po_type__isnull=False)
        .exclude(pi_type=F("po_type"))
    )


# One entry per Pi vs Po Validation Errors card, same shape as
# RANGE_ERROR_RULES / EQUALITY_ERROR_RULES above.
PI_PO_ERROR_RULES = [
    {
        "label": "Invalid OD Rate",
        "violations_fn": lambda: _rate_master_pi_po_rate_violations_qs("pi_od_rate", "po_od_rate"),
        "columns": [("pi_od_rate", "Pi OD Rate"), ("po_od_rate", "Po OD Rate"), ("margin", "Margin")],
    },
    {
        "label": "Invalid TP Rate",
        "violations_fn": lambda: _rate_master_pi_po_rate_violations_qs("pi_tp_rate", "po_tp_rate"),
        "columns": [("pi_tp_rate", "Pi TP Rate"), ("po_tp_rate", "Po TP Rate"), ("margin", "Margin")],
    },
    {
        "label": "Invalid NET Rate",
        "violations_fn": lambda: _rate_master_pi_po_rate_violations_qs("pi_net_rate", "po_net_rate"),
        "columns": [("pi_net_rate", "Pi NET Rate"), ("po_net_rate", "Po NET Rate"), ("margin", "Margin")],
    },
    {
        "label": "Pi/Po Type Mismatch",
        "violations_fn": _rate_master_pi_po_type_violations_qs,
        "columns": [("pi_type", "Pi Type"), ("po_type", "Po Type")],
    },
]


PI_PO_ERROR_RULES_MAP = {rule["label"]: rule for rule in PI_PO_ERROR_RULES}


def _rate_master_pi_po_error_counts():
    return [{"label": rule["label"], "count": rule["violations_fn"]().count()} for rule in PI_PO_ERROR_RULES]


def _rate_master_grid_summary_qs(as_of_date=None):
    """
    Pivot of uploaded RateMaster grids across the same six fields the
    dashboard uses to describe a grid: product, sub_product,
    insurance_company, from_date, to_date, status. grid_count is a DISTINCT
    count of group_id (falling back to id for the rare row with no group
    assigned), not a row count, so one grid that exploded into hundreds of
    make/RTO/CC rate lines still only counts once.

    Excludes is_deleted="YES" rows - the Rate Master dashboard's own "Is
    Deleted?" flag - so a soft-deleted grid doesn't still count here as
    "uploaded". status itself (ACTIVE/INACTIVE) is deliberately left
    unfiltered and shown as its own pivot column: unlike a deletion, an
    insurer's grid going INACTIVE is a real, current fact about that grid
    worth seeing, not stale bookkeeping to hide.

    as_of_date optionally restricts the pivot to grids valid on that date -
    same inclusive from_date/to_date check used everywhere else in this
    codebase a payout rate's validity is tested (motor/health payout lookups,
    the Overlap scan's own "Valid As Of" filter), with a NULL bound treated as
    open-ended rather than "never valid".
    """
    qs = RateMaster.objects.filter(is_deleted="NO")
    if as_of_date:
        qs = qs.filter(
            (Q(from_date__lte=as_of_date) | Q(from_date__isnull=True))
            & (Q(to_date__gte=as_of_date) | Q(to_date__isnull=True))
        )
    return (
        qs
        .annotate(grid_key=Coalesce("group_id", "id"))
        .values("product__name", "sub_product__name", "insurance_company", "from_date", "to_date", "status")
        .annotate(grid_count=Count("grid_key", distinct=True))
        .order_by("-grid_count", "insurance_company", "product__name", "sub_product__name")
    )
