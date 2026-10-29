"""Helpers for the overlap detector page: scan lookup, counts and labels."""
from django.utils import timezone
from datetime import timedelta
from ..models import RateMaster, RateOverlapScan


# =========================================================
# OVERLAP & AMBIGUITY DETECTOR
#
# The MIS Errors table above is reactive - a conflicting pair of rate groups
# only surfaces once a real policy happens to land on it. These cards come at
# the same problem from the Rate Master side: insurance/overlap_utils.py sweeps
# every pair of ACTIVE groups and reports the ones no rule in the engine's
# RULE 1-6 chain can separate, so the conflict is visible before an MIS file
# ever hits it. See that module's docstring for the per-rule semantics.
# =========================================================

# One entry per Overlaps card, same shape as RANGE_ERROR_RULES /
# EQUALITY_ERROR_RULES above. Unlike those two, these four buckets are mutually
# exclusive (classify_pair assigns each pair exactly one), so the card counts
# sum to the scan's total rather than overlapping each other.
OVERLAP_RULES = [
    {
        "key": "DOUBLE_RATE_RISK",
        "label": "Double Rate Risks",
        "sub": "a raw code is duplicated across master clusters",
    },
    {
        "key": "EXACT_DUPLICATE",
        "label": "Exact Duplicates",
        "sub": "same rules and same T&C - one is surplus",
    },
    {
        "key": "CONTAINED",
        "label": "Contained Ranges",
        "sub": "one group sits entirely inside another",
    },
    {
        "key": "OPEN_ENDED",
        "label": "Open-Ended Overlaps",
        "sub": "a blank bound swallows another group",
    },
    {
        "key": "PARTIAL",
        "label": "Partial Overlaps",
        "sub": "ranges cross on at least one axis",
    },
]


OVERLAP_RULES_MAP = {rule["key"]: rule for rule in OVERLAP_RULES}


# A RUNNING scan older than this is treated as abandoned (worker killed
# mid-sweep, redis restarted) rather than blocking every future scan forever.
# Comfortably past run_rate_overlap_scan_task's own 1860s hard time limit, so
# a scan that is merely slow - even one running right up against that limit -
# is never mistaken for a dead one and marked FAILED out from under a task
# that's still legitimately executing.
OVERLAP_SCAN_STALE_AFTER = timedelta(minutes=35)


def _latest_overlap_scan():
    """The newest successful scan - the one whose pairs the dashboard shows."""
    return RateOverlapScan.objects.filter(status=RateOverlapScan.STATUS_COMPLETED).first()


def _active_overlap_scan():
    """
    A scan that is genuinely still running, retiring any that outlived the
    task's time limit so a dead worker can't leave the button wedged.
    """
    running = RateOverlapScan.objects.filter(status=RateOverlapScan.STATUS_RUNNING)
    cutoff = timezone.now() - OVERLAP_SCAN_STALE_AFTER
    running.filter(started_at__lt=cutoff).update(
        status=RateOverlapScan.STATUS_FAILED,
        error_message="Scan did not finish - the worker stopped before reporting a result.",
        finished_at=timezone.now(),
    )
    return running.filter(started_at__gte=cutoff).first()


def _overlap_counts(scan):
    """
    Per-card counts for `scan`, with every card present even at zero.

    Reads the scan's recorded type_counts rather than counting stored
    RateOverlapPair rows: the noisier buckets are truncated at their
    DEFAULT_TYPE_CAPS ceiling when stored, and a card must report how many
    conflicts exist, not how many fitted.
    """
    if scan is None:
        return [dict(rule, count=0, capped=False) for rule in OVERLAP_RULES]
    counts = scan.type_counts or {}
    capped = set(scan.capped_types or [])
    return [
        dict(rule, count=counts.get(rule["key"], 0), capped=rule["key"] in capped)
        for rule in OVERLAP_RULES
    ]


def _rate_group_rows(group_key):
    """
    The RateMaster rows behind one COALESCE(group_id, id) key.

    Same group_id/id ambiguity edit_rate and bulk_update_rates both handle: a
    group id can collide with an unrelated row's primary key, so resolve
    against group_id first and only fall back to a raw id. The fallback is
    additionally restricted to group-less rows, because a row that HAS a group
    is keyed by that group, never by its own id.
    """
    rows = RateMaster.objects.filter(group_id=group_key, is_deleted="NO")
    if rows.exists():
        return rows
    return RateMaster.objects.filter(id=group_key, group__isnull=True, is_deleted="NO")


def _active_rate_master_insurers():
    """Distinct insurers actually present in the ACTIVE Rate Master - the population an overlap scan can run against."""
    return list(
        RateMaster.objects.filter(status="ACTIVE", is_deleted="NO")
        .exclude(insurance_company="")
        .order_by().values_list("insurance_company", flat=True).distinct()
    )


def _active_rate_master_products():
    """Distinct product names actually present in the ACTIVE Rate Master."""
    return list(
        RateMaster.objects.filter(status="ACTIVE", is_deleted="NO", product__isnull=False)
        .order_by().values_list("product__name", flat=True).distinct()
    )


def _scan_scope_label(scan):
    """
    "Acme General, GCV 4W, as of 29 Aug 2026" (or None for an unscoped scan) -
    built once here rather than as inline template conditionals, since with
    three independent optional parts the comma placement is combinatorial
    (any of 7 non-empty combinations), not just a single if/else.
    """
    bits = [
        bit for bit in (
            scan.filter_insurer,
            scan.filter_product,
            scan.filter_as_of_date and f"as of {scan.filter_as_of_date.strftime('%d %b %Y')}",
        )
        if bit
    ]
    return ", ".join(bits) if bits else None


def _fmt_bound(v):
    """None means unbounded — same "any" the drill-down table shows for it."""
    return "any" if v is None else str(v)


def _fmt_axis_side(v):
    """
    One side (a/b/overlap) of a describe_pair() axis, formatted the same way
    the "All fields" drill-down table renders it: a [low, high] pair as
    "low – high", anything else as-is.
    """
    if isinstance(v, list):
        return f"{_fmt_bound(v[0] if v else None)} - {_fmt_bound(v[1] if len(v) > 1 else None)}"
    return v if v not in (None, "") else "(blank)"
