"""
Shared parsing + identity-hash helpers for loading HealthRateMaster rows.
Used by both the one-time Excel import command
(management/commands/import_health_rate_master.py) and the web bulk-CSV-
upload endpoint (api_upload_chunk's 'health_rate_master' branch in views.py),
so both entry points agree on what makes two rows "the same rate rule".
"""
import hashlib
import re
from datetime import date, datetime

# HealthRateMaster fields that make up a row's identity — everything except
# the rate columns and bookkeeping (status/is_deleted/remarks/timestamps).
# Validity dates are part of the identity: the same insurer/product/.../plan
# list combination legitimately recurs across different date windows with
# different rates (e.g. a 2026 rate and a distinct 2027 rate for the same
# rule), so two rows differing only in dates are genuinely different rules,
# not the same one being renewed.
HASH_FIELDS = [
    "insurance_company", "product_name", "policy_category", "plan_names", "business_type",
    "min_deductible", "max_deductible", "min_sum_insured", "max_sum_insured",
    "min_age", "max_age", "pincode_zone", "from_date", "to_date",
]


def parse_number(value):
    """Handles real numbers, '1,234', ' 999,999 ', ' -   ' (-> 0), '' (-> None)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"-+", s):
        return 0.0
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_percent(value):
    """'33.90%' -> 33.90, or a bare '33.90' -> 33.90. Stored as the bare
    percentage number, matching how RateMaster's own payout rate fields are
    stored (no % sign, no /100)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().rstrip("%").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_grid_date(value):
    """Accepts the source grid's DD/MM/YYYY, or a plain ISO YYYY-MM-DD (what
    a re-exported or hand-built CSV is most likely to contain)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def build_row_hash(cleaned):
    """cleaned must have HASH_FIELDS keys already parsed to their final types
    (numbers as float/None, dates as date/None, text as stripped str/None)."""
    parts = [str(cleaned.get(f, "")).strip().lower() for f in HASH_FIELDS]
    key_text = "|".join(parts)
    return hashlib.sha256(key_text.encode("utf-8")).hexdigest()


def upsert_health_rate_row(cleaned):
    """cleaned holds every HealthRateMaster field this row provides — the
    identity fields (required) plus whichever of rates/status/is_deleted/
    remarks the caller chose to include. Matches by identity hash so
    re-importing the same rule updates it in place instead of duplicating
    it; status/is_deleted only get their ACTIVE/NO default on first
    creation (via create_defaults) — if the row didn't set them, don't put
    them in `cleaned` at all, so manual edits made afterwards in the UI
    survive a later re-import that doesn't mention them.
    """
    from .models import HealthRateMaster  # local import avoids a circular import at module load time

    row_hash = build_row_hash(cleaned)
    create_defaults = dict(cleaned)
    create_defaults.setdefault("status", "ACTIVE")
    create_defaults.setdefault("is_deleted", "NO")

    return HealthRateMaster.objects.update_or_create(
        source_row_hash=row_hash,
        defaults=cleaned,
        create_defaults=create_defaults,
    )
