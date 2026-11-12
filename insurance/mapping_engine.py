import pandas as pd
import io
import re
from collections import Counter
from rapidfuzz import fuzz
from django.core.files.base import ContentFile
from django.utils import timezone
from django.db import connection
from .constants import HEALTH_TENURE_RATE_COLUMNS
from .models import (
    MISFile, RateMaster, RTOMaster, MakeModelMaster, MISFailedRow,
    HealthRateMaster, PincodeMaster,
)
from .services.make_model import strict_match_in_cluster
import logging


logger = logging.getLogger(__name__)


# ── Tunable thresholds ───────────────────────────────────────────────────────
INSURANCE_FUZZY_THRESHOLD = 55   # lowered from 60: handles typos in company names
                                  # (e.g. "Limted" instead of "Limited" in Liberty)
CATEGORICAL_FUZZY_THRESHOLD = 75
MAKE_MODEL_MIN_WORD_MATCH = 2     # Rule 5a: min words the MIS "make + model" string
                                  # must share with a MakeModelMaster cluster entry

# The failed_on label and the exact detail sentence RULE 5a emits when a MIS
# "make + model" string matches no MakeModelMaster cluster entry at all.
# Named here rather than inlined at the RULE 5a block because the Missing
# Make/Model page filters MISFailedRow.failure_reason on this exact text — the
# two must never drift, and the text below must stay byte-identical to what
# already-stored failure_reason rows contain (see the constants test).
MAKE_MODEL_RULE_LABEL = 'Policy: vehicle make / model'
MAKE_MODEL_UNRESOLVED_VALUE_PREFIX = "Vehicle make/model '"
MAKE_MODEL_UNRESOLVED_VALUE_SUFFIX = "' did not share at least "


def make_model_unresolved_detail(value):
    """The RULE 5a 'no MakeModelMaster cluster matched at all' detail sentence."""
    return (
        f"{MAKE_MODEL_UNRESOLVED_VALUE_PREFIX}{value}{MAKE_MODEL_UNRESOLVED_VALUE_SUFFIX}"
        f"{MAKE_MODEL_MIN_WORD_MATCH} words with any MakeModelMaster cluster "
        f"entry — add it to MakeModelMaster or check the MIS value."
    )


# Assembled exactly the way process_mis_mapping assembles a NO MATCH reason:
#     reason = f"Failed on: {first_label} — {first_detail}"
# Every RULE block is guarded by `if not current_grid.empty`, and a rule only
# appends when it *just* emptied the grid, so failed_on never holds more than
# one entry. That makes this a true PREFIX of the stored reason rather than
# just a substring, which is what lets the page filter on __startswith.
#
# The prefix alone is NOT enough to identify this variant, though: RULE 5a's
# sibling "resolved to master group(s) [...] but no candidate rate row" detail
# opens with the same "Vehicle make/model '<value>'" text, and only diverges
# after the value. Selecting this variant means prefix AND
# MAKE_MODEL_UNRESOLVED_VALUE_SUFFIX — which is exactly the pair
# MAKE_MODEL_UNRESOLVED_VALUE_PATTERN below anchors between.
MAKE_MODEL_UNRESOLVED_REASON_PREFIX = (
    f"Failed on: {MAKE_MODEL_RULE_LABEL} — {MAKE_MODEL_UNRESOLVED_VALUE_PREFIX}"
)

# Health plan names have no separate master table (unlike RTO/Make-Model) —
# the MIS value is fuzzy-matched directly against the Health Grid row's own
# comma-separated plan_names cluster, after normalizing '+' -> 'plus' and
# stripping punctuation. 80 was picked by measuring real MIS/grid pairs from
# the source config workbook: 'ASPIRE - GOLD +' vs 'Aspire Gold Plus' = 100,
# 'Optima Secure' vs 'My: Optima Secure' = 89.7, 'Arogya Sanjeevani' vs
# 'Arogya Sanjeevani Policy' = 82.9 (all correctly ABOVE 80 and meant to
# match), while the genuinely ambiguous bare 'Optima' vs 'Optima Plus' /
# 'Optima Restore' scored 70.6 / 60.0 (correctly BELOW 80 and meant to fail —
# a bare "Optima" doesn't tell you which Optima variant was actually sold).
HEALTH_PLAN_NAME_FUZZY_THRESHOLD = 80

# Words that appear in almost every insurer name and carry zero discriminative value.
# Stripping these before matching lets the unique identifying tokens (Magma, Liberty,
# SBI, National, etc.) dominate the score, so WRatio can't accidentally map
# "Magma-HDI General Insurance Company Limited" → "SBI General Insurance Company Limited"
# just because both share "General Insurance Company Limited".
_INS_STOP_WORDS = frozenset({
    'general', 'insurance', 'company', 'limited', 'ltd', 'co',
    'the', 'of', 'and', 'an', 'a', 'in'
})


def _key_tokens(name: str) -> set:
    """Return the discriminative tokens from an insurer name (stop-words removed)."""
    tokens = set(re.sub(r'[^a-z0-9\s]', ' ', name.lower()).split())
    return tokens - _INS_STOP_WORDS


def _insurer_score(mis_name: str, rate_name: str) -> float:
    """
    Combined scorer for insurer names.

    Step 1 — token Jaccard on discriminative tokens (unique company identifiers):
      Handles abbreviation vs full-form differences:
        "magma hdi" ↔ "magma-hdi general insurance co ltd"  → Jaccard = 1.0
        "liberty"   ↔ "sbi general insurance company ltd"   → Jaccard = 0.0

    Step 2 — WRatio as a tie-breaker when Jaccard is ambiguous (e.g. scores are equal).

    Returns a combined float 0–100 where Jaccard dominates (weight 80) and
    WRatio is the fine-grained tie-breaker (weight 20).
    """
    mis_keys  = _key_tokens(mis_name)
    rate_keys = _key_tokens(rate_name)
    if not mis_keys or not rate_keys:
        return fuzz.WRatio(mis_name, rate_name)

    intersection = mis_keys & rate_keys
    union        = mis_keys | rate_keys
    jaccard      = len(intersection) / len(union)   # 0.0 – 1.0

    wratio = fuzz.WRatio(mis_name, rate_name)        # 0 – 100

    # Weight: Jaccard × 80 + WRatio × 0.20
    return jaccard * 80 + wratio * 0.20


def get_fuzzy_dict(source_list, target_list, threshold=INSURANCE_FUZZY_THRESHOLD):
    """
    Fuzzy matching dictionary creator for Insurance Companies.

    Uses a two-stage scorer:
      1. Token Jaccard on discriminative words (strips generic stop-words like
         'general', 'insurance', 'company', 'limited') — prevents cross-mapping
         e.g. Magma-HDI → SBI simply because they share common suffix words.
      2. WRatio as a tie-breaker for edge cases where Jaccard alone is equal.

    Threshold is applied to the combined 0-100 score.
    """
    clean_targets = [str(t).strip().lower() for t in target_list if pd.notna(t) and str(t).strip()]

    mapping = {}
    for src in source_list:
        if pd.isna(src) or not str(src).strip():
            mapping[src] = None
            continue
        src_str = str(src).strip().lower()

        if not clean_targets:
            mapping[src] = None
            continue

        best_match = None
        best_score = -1
        for tgt in clean_targets:
            score = _insurer_score(src_str, tgt)
            if score > best_score:
                best_score = score
                best_match = tgt

        mapping[src] = best_match if best_score >= threshold else None

    return mapping


def safe_get_col(df, target_col):
    """Safely extracts a column, ignoring leading/trailing spaces in the header."""
    cols_clean = {str(c).strip(): c for c in df.columns}
    if target_col in cols_clean:
        return df[cols_clean[target_col]]
    return pd.Series([None] * len(df), index=df.index)


_CP_NULL_EQUIVALENTS = frozenset({'null', 'nul', 'nill', 'na'})


def clean_cp_numeric(raw_series):
    """
    Parses a raw MIS field for the cp premium# calculation. Per the explicit
    data-cleansing rule for this field: a blank/missing cell, or a literal
    null-equivalent string ('null'/'nul'/'nill'/'na', case-insensitive), is
    treated as the number 0 — not left blank/NaN. Anything else is parsed as
    a number the normal way (stripping stray non-numeric characters like
    commas); text that still isn't a valid number after that stays blank,
    same as every other numeric MIS field elsewhere in this pipeline.
    This 0-for-blank behavior is specific to cp premium# — it deliberately
    isn't reused for cc/sc/age/tariff etc., where a blank value has its own
    distinct meaning (an open-ended Rate Master range) that 0 would corrupt.
    """
    stripped = raw_series.astype(str).str.strip()
    is_null_like = raw_series.isna() | (stripped == '') | stripped.str.lower().isin(_CP_NULL_EQUIVALENTS)
    numeric = pd.to_numeric(stripped.str.replace(r'[^0-9.\-]', '', regex=True), errors='coerce')
    return numeric.mask(is_null_like, 0)


def consolidated_rate(rate_type, od_rate, tp_rate, net_rate):
    """
    Collapses a RateMaster row's OD/TP/Net rate trio (the same 3-way split
    exists on both the po_* and pi_* sides) into the single rate implied by
    its own type field, instead of exporting all three as separate columns.
    'On OD and TP' sums both rates — same convention already used for match
    tie-breaking (_effective_rate) and Policy Locker's po_rate.
    """
    if rate_type == 'On OD and TP':
        od = od_rate if pd.notna(od_rate) else 0
        tp = tp_rate if pd.notna(tp_rate) else 0
        return od + tp
    if rate_type == 'On TP':
        return tp_rate if pd.notna(tp_rate) else None
    if rate_type == 'On OD':
        return od_rate if pd.notna(od_rate) else None
    if rate_type == 'On Net':
        return net_rate if pd.notna(net_rate) else None
    # Unexpected/blank type — fall back to whichever column is actually
    # populated (Net > OD > TP), so a data-quality gap in the type field
    # doesn't produce a blank rate when a real rate value exists.
    for val in (net_rate, od_rate, tp_rate):
        if pd.notna(val) and val:
            return val
    return None


# RateMaster columns the payout/payin rates and their rate types are read from.
_PAYOUT_COLUMNS = [
    'po_type', 'po_od_rate', 'po_tp_rate', 'po_net_rate',
    'pi_type', 'pi_od_rate', 'pi_tp_rate', 'pi_net_rate',
]


def _rounded_or_none(val):
    return None if pd.isna(val) else round(float(val), 6)


def payout_signature(row):
    """
    The payout rate and payin rate one matched RateMaster row would map, each
    with its rate type, as a hashable tuple — what lands in Potype/Porate and
    Pitype/Pirate. The type is part of the rate: it decides which premium the
    amount is calculated on (Pitype drives cp premium#), so 'On OD' 10 and
    'On Net' 10 are different rates. Nothing else on the row is compared
    (T&C, flat amount, ...): by the time rows reach this point RULE 1-6 have
    already picked them as the right records for the policy.

    The raw OD/TP pair is part of the signature for 'On OD and TP' rates:
    Pay-out/Pay-in Amt for those is computed from the two rates separately
    (against Total OD and TP Premium), not from their consolidated sum, so
    10+5 and 5+10 both read as 15 yet are different rates.
    """
    signature = []
    for side in ('po', 'pi'):
        rate_type = row.get(f'{side}_type')
        rate_type = None if pd.isna(rate_type) else rate_type
        od_rate = row.get(f'{side}_od_rate')
        tp_rate = row.get(f'{side}_tp_rate')
        signature.append(rate_type)
        signature.append(_rounded_or_none(
            consolidated_rate(rate_type, od_rate, tp_rate, row.get(f'{side}_net_rate'))
        ))
        if rate_type == 'On OD and TP':
            signature.append(_rounded_or_none(od_rate) or 0.0)
            signature.append(_rounded_or_none(tp_rate) or 0.0)
    return tuple(signature)


def matched_rows_share_one_payout(matched_grid) -> bool:
    """
    True when every row in matched_grid maps the same payout and payin rate
    (type and value). Collapses to the distinct raw rate columns first (a
    handful of rows at most) so signatures are only built for those, not for
    every physical row of a group exploded across many RTOs.
    """
    distinct_payouts = matched_grid[_PAYOUT_COLUMNS].drop_duplicates()
    return len({payout_signature(row) for _, row in distinct_payouts.iterrows()}) == 1


def health_matched_rows_share_one_rate(matched_grid, rate_col) -> bool:
    """
    Health equivalent of matched_rows_share_one_payout: True when every row in
    matched_grid carries the same payout rate (the tenure column rate_col that
    becomes Porate) and the same payin_rate. Only those two rates are
    compared — not the other tenures' columns, plan names, remarks, etc. Health
    has no rate-type column to compare: Potype/Pitype are fixed for every row.
    """
    distinct_rates = matched_grid[[rate_col, 'payin_rate']].drop_duplicates()
    return len({
        (_rounded_or_none(payout), _rounded_or_none(payin))
        for payout, payin in distinct_rates.itertuples(index=False, name=None)
    }) == 1


def _format_number(n) -> str:
    """12.0 -> '12', 12.5 -> '12.5' — used by format_od_tp_rate so a whole-
    number rate doesn't show a pointless trailing '.0' in the 'OD+TP' string."""
    n = float(n) if pd.notna(n) else 0.0
    return str(int(n)) if n == int(n) else str(n)


def format_od_tp_rate(od_rate, tp_rate) -> str:
    """
    Human-readable 'OD+TP' display for an 'On OD and TP' Porate/Pirate cell,
    e.g. od_rate=13, tp_rate=8 -> '13+8'. Used only to reformat the already-
    computed Porate/Pirate export column after every numeric calculation
    that depends on it (Margin Rate, and Pay-out/Pay-in Amt's split
    calculation) has already run against the real numeric OD/TP rates —
    this never feeds back into arithmetic itself.
    """
    return f"{_format_number(od_rate)}+{_format_number(tp_rate)}"


def check_categorical_match(val, grid_val):
    """
    Smart matching for categorical fields (Fuel, Class, Product) to handle typos safely.
    Uses RapidFuzz instead of difflib for the fuzzy fallback — same logic, faster scorer.
    """
    # If DB is empty, it acts as a wildcard (matches anything)
    if pd.isna(grid_val) or not grid_val or str(grid_val).strip().lower() == 'nan':
        return True

    # If MIS value is missing, it fails the match
    if pd.isna(val) or not val or str(val).strip().lower() == 'nan':
        return False

    v_str = str(val).strip().lower()
    g_str = str(grid_val).strip().lower()

    # 1. Exact Match
    if v_str == g_str:
        return True
    # 2. Substring Match (e.g., "pvt car" inside "private car")
    if v_str in g_str or g_str in v_str:
        return True
    # 3. High-Threshold Fuzzy Match (handles slight typos) — RapidFuzz, 0-100 scale
    if fuzz.ratio(v_str, g_str) > (CATEGORICAL_FUZZY_THRESHOLD):
        return True

    return False


def check_exact_match(val, grid_val):
    """
    RULE 2's product, sub product and fuel check: an EXACT, case-insensitive
    match — no substring and no fuzzy matching, unlike check_categorical_match.
    The fuzzy scorer rates "GCV 3W" / "GCV 4W" (and "PCV 4W" / "GCV 4W",
    "PCV 3W" / "PCV 4W", ...) at 83.3 against a threshold of 75, so one
    product's policy also matched every sibling product's rate rows and
    surfaced as MULTIPLE MATCHES. These are master values ('GCV 3W', '1+1',
    'CNG'), not free text to be forgiven.

    Everything else is unchanged from check_categorical_match: surrounding
    whitespace is ignored, a blank Rate Master value is a wildcard that
    matches any policy, and a blank/missing MIS value matches nothing but a
    wildcard row.
    """
    if pd.isna(grid_val) or not grid_val or str(grid_val).strip().lower() == 'nan':
        return True

    if pd.isna(val) or not val or str(val).strip().lower() == 'nan':
        return False

    return str(val).strip().lower() == str(grid_val).strip().lower()


def build_master_lookup(master_qs, name_field, cluster_field):
    """
    Preloads a master table (RTOMaster or MakeModelMaster) once per mapping
    job and builds a reverse lookup: literal MIS value (upper-cased) -> set
    of master 'name' values whose cluster contains it. This mirrors the
    two-step chain used in views.py:

        MIS 'MH09' -> search master.cluster for 'MH09' -> master.name 'ZYX'
        -> then search RateMaster's cluster column for 'ZYX'

    One MIS value can resolve to multiple master names if more than one
    master row's cluster contains it; all of them are tried downstream.
    No fallback is applied if a value isn't found in any cluster — that
    MIS value simply resolves to an empty set, which fails the rule.
    """
    master_rows = list(master_qs.values_list(name_field, cluster_field))

    # Build the master_name -> cluster_items lookup once
    parsed = []
    all_individual_terms = set()
    for name, cluster in master_rows:
        if not cluster:
            continue
        items = [x.strip().upper() for x in str(cluster).split(",") if x.strip()]
        parsed.append((name, cluster, items))
        all_individual_terms.update(items)

    # Cache resolved lookups per MIS value so repeated values (common in
    # bulk MIS files) don't re-scan the whole master table each time.
    resolution_cache = {}

    def resolve(mis_value):
        """Returns a set of master 'name' values whose cluster contains mis_value."""
        if not mis_value:
            return set()
        key = str(mis_value).strip().upper()
        if key in resolution_cache:
            return resolution_cache[key]

        matched_names = set()
        for name, cluster, items in parsed:
            if strict_match_in_cluster(key, cluster):
                matched_names.add(str(name).strip().lower())

        resolution_cache[key] = matched_names
        return matched_names

    return resolve


def check_resolved_cluster_match(resolved_names, grid_val):
    """
    Step 2 of the two-step chain: given the set of master 'name' values
    resolved from the MIS value (e.g. {'zyx'} or {'private_car_all'}),
    check whether RateMaster's cluster column (new_rto_list /
    new_vehicle_makes) contains ANY of them.
    """
    if not resolved_names:
        return False  # No fallback — unresolved MIS value fails the rule
    if not grid_val:
        return False

    g_str = str(grid_val).strip().lower()
    g_items = [x.strip() for x in g_str.split(',') if x.strip()]

    for resolved_name in resolved_names:
        if resolved_name in g_items:
            return True

    return False


def _normalize_model_words(text: str) -> set:
    """
    Tokenizes a make/model string for Rule 5a's word-overlap match, handling
    two common MIS/master formatting differences that would otherwise hide a
    real word-level match despite the underlying make/model being the same:
      - a hyphenated trim/variant suffix glued to the model:  "CITY-1.5"  -> {"CITY", "1.5"}
      - an edition/engine number glued directly to the model: "ACTIVA6G" -> {"ACTIVA", "6G"}
    Applied identically to both the search term and each cluster item so the
    comparison stays symmetric.
    """
    normalized = text.replace('-', ' ')
    normalized = re.sub(r'(?<=[A-Za-z])(?=\d)', ' ', normalized)
    return set(normalized.split())


def fuzzy_match_make_model(search_term, cluster_string, min_shared_words=MAKE_MODEL_MIN_WORD_MATCH):
    """
    Word-overlap matcher used for vehicle make/model (Rule 5a), replacing a
    strict/substring match. A MakeModelMaster cluster entry counts as a match
    if it shares at least `min_shared_words` words with the search term —
    e.g. search "YAMAHA ALPHA" against cluster entry "YAMAHA ALPHA LX" shares
    {"YAMAHA", "ALPHA"} = 2 words -> match — rather than requiring the whole
    entry to match exactly or the search term to appear as one substring.

    Matching is done per cluster item (not across the whole cluster field),
    so two unrelated items can't accidentally combine to satisfy the word
    count. If the search term itself has fewer than `min_shared_words` words
    (e.g. model is blank), no item can ever match — that's intentional: the
    threshold is on shared words, not a fraction of the search term.
    """
    if not search_term or not cluster_string:
        return False

    search_words = _normalize_model_words(str(search_term).strip().upper())
    if len(search_words) < min_shared_words:
        return False

    items = [x.strip().upper() for x in str(cluster_string).split(",") if x.strip()]
    for item in items:
        item_words = _normalize_model_words(item)
        if len(search_words & item_words) >= min_shared_words:
            return True

    return False


def build_make_model_lookup(master_qs, name_field, cluster_field, min_shared_words=MAKE_MODEL_MIN_WORD_MATCH):
    """
    Preloads MakeModelMaster once per mapping job and builds a lookup from a
    MIS "make + model" search string -> set of master 'name' values whose
    cluster shares at least `min_shared_words` words with it (see
    fuzzy_match_make_model). Mirrors build_master_lookup's shape and caching,
    but is specific to Rule 5a's fuzzy word-overlap matching — RTO (Rule 5b)
    still uses build_master_lookup/strict_match_in_cluster unchanged.

    Answers come from build_make_model_cluster_index: every cluster item is
    tokenised ONCE here, so resolving a new term walks only the items that share
    one of its words. Walking every master row and re-tokenising every cluster
    item for each new term (fuzzy_match_make_model over the whole master) cost
    ~0.4s a term on real data, which is what dominated a mapping run once every
    row reaches this rule (a failed rule no longer ends a row's checks — see
    narrow_candidates). The decision is identical (fuzzy_match_make_model stays
    as the reference implementation, and a test asserts the two agree).
    """
    index = build_make_model_cluster_index(master_qs, name_field, cluster_field)

    resolution_cache = {}

    def resolve(search_term):
        if not search_term:
            return set()
        key = str(search_term).strip().upper()
        if key in resolution_cache:
            return resolution_cache[key]

        matched_names = resolve_make_model_with_index(index, key, min_shared_words)

        resolution_cache[key] = matched_names
        return matched_names

    return resolve


def build_make_model_cluster_index(master_qs, name_field, cluster_field):
    """
    Inverted word index over every MakeModelMaster cluster item, for callers
    that resolve MANY search terms against the SAME master snapshot — the
    Missing Make/Model page re-checks every aggregated failing value against
    the live master on each request.

    build_make_model_lookup caches per search TERM, but for a term it has not
    seen it walks all ~500 master rows and re-tokenizes their entire cluster
    strings — on real data that is ~145k cluster items, i.e. ~30ms per new
    term, which a page resolving hundreds of distinct values cannot afford.

    Inverting it costs the same single pass to build but turns each lookup
    into a walk of only the items that actually contain one of the search
    term's words. The match DECISION is unchanged: because the search side is
    a set of distinct words and each item contributes each of its distinct
    words once, the number of postings an item is hit through is exactly
    |search_words ∩ item_words| — the same quantity fuzzy_match_make_model
    compares against min_shared_words, over the same _normalize_model_words
    tokens, still per individual cluster item so two items can't combine. A
    test asserts the two agree.

    Returns an opaque structure for resolve_make_model_with_index.
    """
    item_names = []                      # item index -> owning master name
    postings = {}                        # word -> [item index, ...]
    for name, cluster in master_qs.values_list(name_field, cluster_field):
        if not cluster:
            continue
        name_lowered = str(name).strip().lower()
        for item in str(cluster).split(","):
            item = item.strip()
            if not item:
                continue
            words = _normalize_model_words(item.upper())
            if not words:
                continue
            item_index = len(item_names)
            item_names.append(name_lowered)
            for word in words:
                postings.setdefault(word, []).append(item_index)
    return (item_names, postings)


def resolve_make_model_with_index(index, search_term, min_shared_words=MAKE_MODEL_MIN_WORD_MATCH):
    """
    Set of master names whose cluster matches search_term — the
    build_make_model_cluster_index counterpart of build_make_model_lookup's
    resolve(). Same semantics, including the "search term with fewer than
    min_shared_words words can never match" rule.
    """
    if not search_term:
        return set()
    search_words = _normalize_model_words(str(search_term).strip().upper())
    if len(search_words) < min_shared_words:
        return set()

    item_names, postings = index
    hits = Counter()
    for word in search_words:
        hits.update(postings.get(word, ()))
    return {
        item_names[item_index]
        for item_index, shared in hits.items()
        if shared >= min_shared_words
    }


# Two numbers jammed together with a slash (e.g. "6702/47500" = CC/GVW for a
# commercial vehicle). The old digit-only regex silently concatenated both
# into one nonsense integer ("670247500") instead of a real CC value — this
# pattern must be caught before any numeric parsing is attempted.
_CC_COMBINED_PATTERN = re.compile(r'\d+\s*/\s*\d+')


def is_combined_cc_gvw(raw_value) -> bool:
    """
    True if a raw MIS 'cc cubic capacity' value looks like two numbers
    combined with a slash (CC/GVW jammed into one cell) rather than a
    single CC figure. These are never parsed — the row is routed straight
    to FAILED - BAD DATA instead of risking a match against a fabricated
    number.
    """
    if pd.isna(raw_value):
        return False
    return bool(_CC_COMBINED_PATTERN.search(str(raw_value)))


# 'Policy: sub product' values that mark a brand-new vehicle, whose age is
# counted from the policy's inception year instead of a registration year.
# Compared against the already stripped + lower-cased _mis_sub_prod column.
NEW_VEHICLE_SUB_PRODUCTS = frozenset({'1+3', '1+5'})

# The failed_on labels RULE 3's vehicle-age check reports under. The age is no
# longer read from the MIS 'Policy: vehage' column, nor calculated from
# 'Policy: manufacturing year' — it comes from the year in one of these two
# columns (see calculate_vehicle_age), and which one a human has to fix when
# this rule fails depends on the row.
VEHICLE_AGE_REGISTRATION_LABEL = 'Policy: date of registration'
VEHICLE_AGE_NEW_VEHICLE_LABEL = 'Policy: inception date'

# A calendar year (1900-2099) standing on its own inside the cell, so "2024",
# "2024.0", "15-03-2024" (dd-mm-yyyy), "15/03/2024" and an Excel
# "2024-03-15 00:00:00" all yield 2024. Day and month order never matters —
# only the 4-digit year is read.
_YEAR_PATTERN = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')


def extract_year(date_series):
    """The 4-digit calendar year in each cell of a raw MIS date column; NaN where there is none."""
    return pd.to_numeric(
        date_series.astype(str).str.extract(_YEAR_PATTERN, expand=False),
        errors='coerce',
    )


def calculate_vehicle_age(registration_series, inception_series, is_new_vehicle, current_year):
    """
    Motor vehicle age for RULE 3, as a whole number of years:

        Calculated_Age = (CURRENT_YEAR + 1) - Year

    Year is the year of 'Policy: date of registration' for a standard/rollover
    policy, and the year of 'Policy: inception date' for a new vehicle
    (is_new_vehicle is True: sub product 1+3 / 1+5). The "+ 1" counts the year
    of registration as age 1 — with a current year of 2026, a vehicle
    registered in 2025 is age 2 and one registered in 2026 is age 1.

    CURRENT_YEAR is the calendar year the mapping run happens in (2026 now,
    2027 once the clock rolls over), passed in by process_mis_mapping from
    timezone.localdate() rather than hard-coded here.

    The result is NaN when the year's source cell is blank or has no 4-digit
    year in it, with no fallback to the other date column or to the
    manufacturing year; RULE 3 then treats it like any other blank value (only
    a rate row with an open, fully blank age range can pass). It is NOT
    clamped: a year later than the current year gives an age of 0 or less,
    which falls below the exclusive lower bound of every band that has one.

    RULE 3 then reads this against each rate row's age band as
    (vehicle_age_min, vehicle_age_max] — lower bound exclusive, upper bound
    inclusive, so the current year's age 1 sits in the '0 to 1' band — see
    the range_rules loop.
    """
    year = extract_year(inception_series).where(is_new_vehicle, extract_year(registration_series))
    return (current_year + 1) - year


# ── Premium split / NCB validation (Motor rows) ──────────────────────────────
# Rules a Motor MIS row must pass before it is allowed anywhere near RULE 1-6.
# Each is keyed on 'Policy: sub product' (matched stripped + lower-cased, like
# _mis_sub_prod) and reads 'Policy: total od', 'Policy: tp premium' and
# 'Policy: no claim bonus'. A cell that is blank, zero or one of the null
# strings clean_cp_numeric knows ('null'/'nul'/'nill'/'na') is ABSENT; any
# other number is PRESENT, negatives included (an endorsement can refund
# premium).
#
#   STP              premium in 'Policy: tp premium' ONLY   (total od absent)
#   SAOD             premium in 'Policy: total od' ONLY     (tp premium absent)
#   1+1 / 1+3 / 1+5  premium in BOTH total od and tp premium — except Private
#                    Car ('Policy: vehproduct'), where total od alone is fine
#                    too: total od must be present, tp premium may or may not be
#   1+3 / 1+5        'Policy: no claim bonus' ABSENT (a brand-new vehicle has
#                    none). Stacks on the premium rule above — both must hold.
#
# Any other sub product (3+3, 5+5, a blank one, ...) isn't covered. The rules
# only ever run for Motor rows (see the PRODUCT ROUTING block in
# process_mis_mapping): STP/SAOD/1+x are Motor concepts and a Health row has no
# OD/TP split.
STP_SUB_PRODUCT = 'stp'
SAOD_SUB_PRODUCT = 'saod'
PACKAGE_SUB_PRODUCTS = frozenset({'1+1', '1+3', '1+5'})
# The same two sub products as NEW_VEHICLE_SUB_PRODUCTS, but a separate rule
# with its own constant: widening one must not silently widen the other.
NO_NCB_SUB_PRODUCTS = frozenset({'1+3', '1+5'})
PREMIUM_VALIDATED_SUB_PRODUCTS = PACKAGE_SUB_PRODUCTS | {STP_SUB_PRODUCT, SAOD_SUB_PRODUCT}
PRIVATE_CAR_PRODUCT = 'private car'

TOTAL_OD_LABEL = 'Policy: total od'
TP_PREMIUM_LABEL = 'Policy: tp premium'
NCB_LABEL = 'Policy: no claim bonus'

VALIDATION_ERROR_PREFIX = 'Validation error — '
VALIDATION_ERROR_SUFFIX = ' Correct the source MIS data and re-run mapping for this row.'


def _format_amount(value):
    """4151.0 -> '4,151', 6725.88 -> '6,725.88': a cleaned amount as it reads in a failure reason."""
    return f"{value:,.2f}".rstrip('0').rstrip('.')


def _raw_cell_text(raw):
    """The cell as the MIS file has it, for quoting an unreadable value back to the user."""
    return '(blank)' if pd.isna(raw) else str(raw).strip()


def _premium_validation_problems(sub, is_private_car, od, tp, ncb, raw_od, raw_tp, raw_ncb):
    """
    The validation sentences one covered row earns, in rule order (an empty
    list means it passes). `od` / `tp` / `ncb` are the cleaned amounts from
    clean_cp_numeric — NaN when the cell isn't a number at all — and the raw_*
    values are the cells as written, only used to quote an unreadable one.
    """
    label = sub.upper()
    od_unreadable, tp_unreadable = pd.isna(od), pd.isna(tp)
    has_od = not od_unreadable and od != 0
    has_tp = not tp_unreadable and tp != 0
    # "Known to be zero/blank" is not the same as "not present": an amount
    # that can't be read is neither, and is reported as such instead of being
    # guessed at (a typo like '1870..00' is not a missing premium).
    od_zero = not od_unreadable and not has_od
    tp_zero = not tp_unreadable and not has_tp

    problems = []
    for unreadable, column, raw in (
        (od_unreadable, TOTAL_OD_LABEL, raw_od),
        (tp_unreadable, TP_PREMIUM_LABEL, raw_tp),
    ):
        if unreadable:
            problems.append(
                f"{column} value '{_raw_cell_text(raw)}' is not a valid number, so the "
                f"premium split of this {label} policy could not be checked."
            )

    if sub == STP_SUB_PRODUCT:
        if tp_zero:
            problems.append(f"{TP_PREMIUM_LABEL} is zero/blank, but STP policies must carry their premium there.")
        if has_od:
            problems.append(
                f"{TOTAL_OD_LABEL} is {_format_amount(od)}, but STP policies must have it zero/blank "
                f"(the premium belongs in {TP_PREMIUM_LABEL} only)."
            )
    elif sub == SAOD_SUB_PRODUCT:
        if od_zero:
            problems.append(f"{TOTAL_OD_LABEL} is zero/blank, but SAOD policies must carry their premium there.")
        if has_tp:
            problems.append(
                f"{TP_PREMIUM_LABEL} is {_format_amount(tp)}, but SAOD policies must have it zero/blank "
                f"(the premium belongs in {TOTAL_OD_LABEL} only)."
            )
    elif is_private_car:
        # 1+1 / 1+3 / 1+5 on a Private Car: total od is mandatory, tp premium isn't.
        if od_zero:
            problems.append(
                f"{TOTAL_OD_LABEL} is zero/blank, but Private Car {label} policies must carry premium "
                f"in {TOTAL_OD_LABEL} ({TP_PREMIUM_LABEL} is optional)."
            )
    else:
        # 1+1 / 1+3 / 1+5 on anything else: premium in both.
        both = f"{label} policies must carry premium in both {TOTAL_OD_LABEL} and {TP_PREMIUM_LABEL}"
        if od_zero and tp_zero:
            problems.append(f"{TOTAL_OD_LABEL} and {TP_PREMIUM_LABEL} are both zero/blank, but {both}.")
        elif od_zero:
            problems.append(f"{TOTAL_OD_LABEL} is zero/blank, but {both}.")
        elif tp_zero:
            problems.append(f"{TP_PREMIUM_LABEL} is zero/blank, but {both}.")

    if sub in NO_NCB_SUB_PRODUCTS:
        if pd.isna(ncb):
            problems.append(
                f"{NCB_LABEL} value '{_raw_cell_text(raw_ncb)}' is not a valid number, so it could "
                f"not be checked against the {label} rule (zero/blank required)."
            )
        elif ncb != 0:
            problems.append(f"{NCB_LABEL} is {_format_amount(ncb)}, but {label} policies must have it zero/blank.")

    return problems


def premium_validation_errors(sub_product, vehicle_product, total_od, tp_premium, no_claim_bonus):
    """
    The premium split / NCB rules above, over whole MIS columns:
    'Policy: sub product', 'Policy: vehproduct', 'Policy: total od',
    'Policy: tp premium' and 'Policy: no claim bonus', as Series sharing one
    index. The two product columns are stripped and lower-cased here, so raw
    and already-normalised values both work; the three amount columns are
    cleaned with clean_cp_numeric, the same cleaning cp premium# uses, so
    '4,151.00' is 4151 and a blank, 0 or 'null' is simply absent.

    Returns a Series of strings on that index: '' for a row that passes or
    whose sub product isn't covered, otherwise the whole failure reason —
    every rule the row breaks, so one run shows everything to correct. It does
    not look at Product: process_mis_mapping applies it to Motor rows only.
    """
    subs = sub_product.astype(str).str.strip().str.lower()
    is_private_car = (
        vehicle_product.astype(str).str.strip().str.lower() == PRIVATE_CAR_PRODUCT
    ).fillna(False).astype(bool)
    od = clean_cp_numeric(total_od)
    tp = clean_cp_numeric(tp_premium)
    ncb = clean_cp_numeric(no_claim_bonus)

    reasons = []
    for sub, private_car, od_val, tp_val, ncb_val, raw_od, raw_tp, raw_ncb in zip(
        subs, is_private_car, od, tp, ncb, total_od, tp_premium, no_claim_bonus
    ):
        problems = (
            _premium_validation_problems(
                sub, private_car, od_val, tp_val, ncb_val, raw_od, raw_tp, raw_ncb
            )
            if sub in PREMIUM_VALIDATED_SUB_PRODUCTS else []
        )
        reasons.append(
            f"{VALIDATION_ERROR_PREFIX}{' '.join(problems)}{VALIDATION_ERROR_SUFFIX}" if problems else ''
        )
    return pd.Series(reasons, index=sub_product.index, dtype=object)


def normalize_rto_code(raw_value):
    """
    Normalizes a raw MIS RTO value before the two-step RTOMaster lookup.
    Handles three real-world MIS formatting issues at once:
      - internal spaces:            "UP 32"      -> "UP32"
      - hyphens:                    "GJ-19"      -> "GJ19"
      - full registration numbers
        instead of just the code:   "MH05FG9876" -> "MH05"
    A clean RTO code is always 2 letters + 2 digits, and a full
    registration number always starts with that code — so stripping
    spaces/hyphens and truncating to 4 characters handles all three.
    """
    if pd.isna(raw_value):
        # Must return a real string, not the raw NA marker: with newer
        # pandas string-dtype semantics, an entirely-blank/missing column
        # can still be genuine NA even after .astype(str) upstream. If this
        # branch returns raw_value unchanged, a column that's blank on
        # every row makes .apply() infer float64 for the whole result
        # (all-NaN), and the caller's chained .str.lower() then crashes
        # with "Can only use .str accessor with string values, not floating".
        return ''
    s = str(raw_value).strip()
    if not s or s.lower() == 'nan':
        return s
    s = re.sub(r'[\s\-]', '', s)
    return s[:4]


def normalize_pincode(raw_value):
    """
    Normalizes a raw MIS 'Customer: pin code' value before the two-step
    PincodeMaster lookup. A pincode column read through pandas/Excel often
    comes back as a float when the column has any blank cells elsewhere
    (400064 -> "400064.0"), which would silently fail an exact match against
    PincodeMaster's plain-digit cluster entries — strip that trailing '.0'.
    """
    if pd.isna(raw_value):
        return ''
    s = str(raw_value).strip()
    if not s or s.lower() == 'nan':
        return ''
    if s.endswith('.0'):
        s = s[:-2]
    return s


_PLAN_NAME_PUNCT_PATTERN = re.compile(r'[^a-z0-9\s]')
_PLAN_NAME_WS_PATTERN = re.compile(r'\s+')


def _normalize_plan_name(text: str) -> str:
    """'+' is a meaningful word in health plan names ('Gold+' == 'Gold Plus'),
    so it's spelled out before punctuation is stripped, not just discarded
    along with hyphens/colons/other formatting noise."""
    s = str(text).strip().lower()
    s = s.replace('+', ' plus')
    s = _PLAN_NAME_PUNCT_PATTERN.sub(' ', s)
    s = _PLAN_NAME_WS_PATTERN.sub(' ', s).strip()
    return s


def fuzzy_match_plan_name(search_term, cluster_string, threshold=HEALTH_PLAN_NAME_FUZZY_THRESHOLD):
    """
    Health equivalent of Rule 5a's fuzzy make/model match, but there's no
    separate PlanMaster table to bridge MIS plan-name spelling against —
    HealthRateMaster.plan_names is a comma-separated list living directly on
    the grid row itself (e.g. 'Activate Booster,Arogya Sanjeevani Policy,...
    Elevate,Family Shield,...'), so this matches the (normalized) MIS plan
    name against each (normalized) item in that list using RapidFuzz's
    token_sort_ratio, which is order-independent and tolerant of the
    punctuation/spacing drift real MIS plan names have (see
    HEALTH_PLAN_NAME_FUZZY_THRESHOLD for measured examples).
    """
    if not search_term or not cluster_string:
        return False
    norm_search = _normalize_plan_name(search_term)
    if not norm_search:
        return False
    for item in str(cluster_string).split(','):
        item = item.strip()
        if not item:
            continue
        norm_item = _normalize_plan_name(item)
        if norm_search == norm_item:
            return True
        if fuzz.token_sort_ratio(norm_search, norm_item) >= threshold:
            return True
    return False


# The MIS and the Health Grid use different vocabulary for the same business
# concept — a policy moved from one insurer to another without a break in
# cover: the MIS calls it 'RollOver' (seen with case/spacing/separator drift
# — 'Rollover', 'roll over', 'Roll-Over', 'Roll_Over'), the Health Grid calls
# it 'Port' (or 'Portability'). Deliberately narrow: only these known
# synonyms fold together. Every other business type ('Fresh', 'Renewal',
# 'New', ...) isn't a key here, so it's returned unchanged by
# _normalize_health_business_type and stays exact-match-strict against the
# grid via check_categorical_match, same as before this rule existed.
_HEALTH_BUSINESS_TYPE_SYNONYMS = {
    'rollover': 'port',
    'roll over': 'port',
    'portability': 'port',
    'port': 'port',
}
_BUSINESS_TYPE_SEPARATOR_PATTERN = re.compile(r'[\s_\-]+')


def _normalize_health_business_type(value):
    """
    Canonicalizes Health business-type / Policy Type synonyms (see
    _HEALTH_BUSINESS_TYPE_SYNONYMS) before the Rule H3 categorical match.
    Separators (spaces/hyphens/underscores) are collapsed first so
    'Roll-Over'/'Roll_Over'/'roll   over' all resolve to the same lookup key
    as 'RollOver' — only then is the synonym map consulted, and only the
    'port' family of spellings is affected. Always returns a string (a
    missing/NaN value stringifies to 'nan', same as every sibling _mis_h_*
    categorical column via .astype(str).str.strip().str.lower()), so this
    is a drop-in replacement for that chain, not a special case.
    """
    key = _BUSINESS_TYPE_SEPARATOR_PATTERN.sub(' ', str(value).strip().lower()).strip()
    return _HEALTH_BUSINESS_TYPE_SYNONYMS.get(key, key)


_QUOTED_VALUE_PATTERN = re.compile(r"'([^']*)'")
_FAILED_ON_PATTERN = re.compile(r"^Failed on:\s*([^—]+)—\s*(.*)$")
_GROUP_IDS_PATTERN = re.compile(r"Matching Group IDs:\s*(.*)$")


# ── Batch error collection ───────────────────────────────────────────────────
# No check stops at the first failure. A row that fails several checks reports
# ALL of them in its one Failure Reason, so fixing a row (or a Rate Master /
# master-data gap) never just uncovers the next error on the next run. The
# rate-matching chains (RULE 1-6, Health H1-H6) collect their failures as
# (label, detail) pairs in rule order, and format_failed_on renders them on ONE
# line:
#
#   Failed on: <label 1> — <detail 1> | Also failed on: <label 2> — <detail 2> | ...
#
# The first segment is exactly the Failure Reason a row that stopped at its
# first failed rule always had, byte for byte: the Missing Make/Model page and
# the coverage-gap panel read it from the start of the text. Everything after
# it is additional. It has to stay one line — the coverage-gap pattern above
# does not cross a newline.
FAILURE_SEGMENT_SEPARATOR = ' | '
ALSO_FAILED_ON_PREFIX = 'Also failed on: '


def format_failed_on(failed_on):
    """
    The NO MATCH Failure Reason for the (label, detail) failures one row
    collected, in rule order: 'Failed on: <first> | Also failed on: <next> | ...'.
    """
    segments = []
    for position, (label, detail) in enumerate(failed_on):
        prefix = 'Failed on: ' if position == 0 else ALSO_FAILED_ON_PREFIX
        segments.append(f"{prefix}{label} — {detail}")
    return FAILURE_SEGMENT_SEPARATOR.join(segments)


def split_failed_on(reason):
    """
    The (label, detail) pairs inside a stored 'Failed on:' Failure Reason —
    format_failed_on in reverse. Empty when the reason isn't of that shape
    (e.g. 'No rates found for criteria'). A reason written before failures were
    collected has just the one pair.
    """
    first, *others = str(reason).split(FAILURE_SEGMENT_SEPARATOR + ALSO_FAILED_ON_PREFIX)
    match = _FAILED_ON_PATTERN.match(first)
    if not match:
        return []
    failures = [(match.group(1).strip(), match.group(2).strip())]
    for segment in others:
        label, _, detail = segment.partition('—')
        failures.append((label.strip(), detail.strip()))
    return failures


def narrow_candidates(current_grid, rule_mask, failed_on, label, detail):
    """
    Applies one rate-matching rule to the candidate Rate Master rows. If rows
    survive, they are the new candidates. If NONE do, the rule has failed: it is
    recorded in failed_on and the candidates from before it are kept, so every
    later rule is still evaluated (and reported) instead of being skipped.

    Every rule is a row-wise test of a Rate Master row against one MIS value, so
    keeping the pre-failure candidates changes nothing about which rows survive
    the rules that PASSED; and a rule that fails still fails exactly as it did
    when it was the last one standing. The catch: current_grid is no longer
    empty after a failure, so a caller must decide NO MATCH from failed_on, not
    from current_grid.empty.
    """
    narrowed = current_grid[rule_mask]
    if narrowed.empty:
        failed_on.append((label, detail))
        return current_grid
    return narrowed

# Pulls the failing make/model back out of a stored failure_reason, for the
# Missing Make/Model page. Deliberately NOT _QUOTED_VALUE_PATTERN: that stops
# at the first apostrophe, so a value like "bmw 3's series" would come back
# truncated to "bmw 3". The non-greedy group instead runs to the first
# occurrence of the suffix marker, which is specific enough that an embedded
# apostrophe is harmless.
MAKE_MODEL_UNRESOLVED_VALUE_PATTERN = re.compile(
    r"^" + re.escape(MAKE_MODEL_UNRESOLVED_REASON_PREFIX) + r"(.*?)"
    + re.escape(MAKE_MODEL_UNRESOLVED_VALUE_SUFFIX)
)


def _extract_gap_key(detail: str) -> str:
    """
    Best-effort extraction of the specific value that failed a rule, straight
    from its already-generated Failure Reason sentence (see the RULE 1-6
    blocks in process_mis_mapping) — e.g. the insurer/product/RTO name inside
    the first pair of quotes. Falls back to a short prefix of the detail
    sentence for rules that don't quote a single value (numeric ranges,
    blank/unparseable fields), so every row still groups into *some* bucket.
    """
    match = _QUOTED_VALUE_PATTERN.search(detail)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return detail[:60].strip()


def build_coverage_gap_summary(df_final, max_entries=25):
    """
    Ranks NO MATCH / MULTIPLE MATCHES rows by (rule, value) so the MIS
    Payout Engine dashboard can show "which Rate Master gap to close next"
    instead of someone reading Failure Reason for every row individually.
    Runs once per processed file, purely over the already-computed output —
    it doesn't touch or re-run any matching logic, so it carries zero risk
    to the match/no-match decisions themselves. A NO MATCH row that failed
    several checks is counted under each of them (see format_failed_on).
    """
    no_match_counts = Counter()
    no_match_reasons = df_final.loc[df_final['Mapping Status'] == '❌ NO MATCH', 'Failure Reason']
    for reason in no_match_reasons:
        reason = str(reason)
        failures = split_failed_on(reason)
        if not failures:
            no_match_counts[('Other', reason[:60].strip())] += 1
            continue
        # Every failed check is a gap to close, so a row that failed several
        # counts once under each of them, not only under the first.
        for rule_label, detail in failures:
            no_match_counts[(rule_label, _extract_gap_key(detail))] += 1

    no_match_ranked = [
        {'rule': rule, 'value': value, 'count': count}
        for (rule, value), count in no_match_counts.most_common(max_entries)
    ]

    multi_counts = Counter()
    multi_reasons = df_final.loc[df_final['Mapping Status'] == '⚠️ MULTIPLE MATCHES', 'Failure Reason']
    for reason in multi_reasons:
        m = _GROUP_IDS_PATTERN.search(str(reason))
        key = m.group(1).strip() if m else str(reason)[:60].strip()
        multi_counts[key] += 1

    multiple_ranked = [
        {'group_ids': key, 'count': count}
        for key, count in multi_counts.most_common(max_entries)
    ]

    return {'no_match': no_match_ranked, 'multiple_matches': multiple_ranked}


# Raw 'Mapping Status' text (as written below) -> the short key MISFailedRow
# and the Rate Master Health dashboard filter/display by.
MIS_STATUS_KEYS = {
    '⚠️ MULTIPLE MATCHES': 'MULTIPLE_MATCHES',
    '❌ NO MATCH': 'NO_MATCH',
    'FAILED - BAD DATA': 'BAD_DATA',
}

# The exact raw MIS columns RULE 1-6 above read from (every safe_get_col(...)
# call feeding a _mis_* column, plus 'Policy: vehicle make' / 'Policy: model'
# which together feed _mis_make_model), and the 'Policy: total od' / 'Policy:
# tp premium' pair the premium split validation reads — kept in sync with that
# logic so a failed row's "policy details" only ever shows fields a human could
# plausibly need to fix a mapping failure, not the ~100 other MIS columns
# (customer address, agent, QC status, etc.) that have no bearing on why it
# didn't map.
MATCHING_RULE_COLUMNS = [
    'Policy: insurance company',       # Rule 1
    'Policy: vehproduct',               # Rule 2
    'Policy: sub product',              # Rule 2
    'Policy: fuel',                      # Rule 2
    'Policy: vehicle class',            # Rule 2b
    'Policy: cc cubic capacity',        # Rule 3 (CC, or GVW below for GCV 3W/4W)
    'Policy: gvw gross vehicle weight', # Rule 3 (GCV 3W/4W only)
    'Policy: seating capacity',         # Rule 3
    'Policy: date of registration',     # Rule 3 (vehicle age = current year + 1 - this year)
    'Policy: tariff rate',              # Rule 3
    'Policy: inception date',           # Rule 4 (and Rule 3's vehicle age for new vehicles)
    'Policy: vehicle make',             # Rule 5a
    'Policy: model',                    # Rule 5a
    'Policy: rto no',                   # Rule 5b
    'Policy: no claim bonus',           # Rule 6 (and the 1+3 / 1+5 NCB validation)
    'Policy: cpa',                      # Rule 6
    'Policy: nil dep',                  # Rule 6
    'Policy: product name',             # Rule H2 (Health)
    'Policy: policy category',          # Rule H2 (Health)
    'Policy Type',                      # Rule H2 (Health) — business_type
    'Policy: plan name',                # Rule H3 (Health)
    'Policy: deductible amount',        # Rule H4 (Health)
    'Policy: sum assured',              # Rule H4 (Health)
    'Customer: age',                    # Rule H4 (Health)
    'Customer: pin code',               # Rule H5 (Health)
    'Policy: expiry date',              # Rule H-tenure (Health)
    # Last, so the order every column above reaches a failed row's details in
    # (and the first-few-entries fallback for rows with no vehicle columns)
    # stays exactly as it was.
    'Policy: total od',                 # Premium split validation (BAD DATA)
    'Policy: tp premium',               # Premium split validation (BAD DATA)
]


def _find_column(columns, target):
    """Case/whitespace-tolerant lookup of a column name, same tolerance as safe_get_col."""
    target_norm = target.strip().lower()
    for c in columns:
        if str(c).strip().lower() == target_norm:
            return c
    return None


def _json_safe(val):
    """numpy scalars (int64/float64/bool_ — what a pandas row actually hands
    back for numeric columns) aren't JSON-serializable; native Python values
    coming through unchanged."""
    if hasattr(val, 'item'):
        try:
            return val.item()
        except Exception:
            pass
    return val


def _extract_failed_rows_from_df(df):
    """
    Pure extraction over an already-loaded DataFrame carrying 'Mapping
    Status'/'Failure Reason' — df_final fresh out of process_mis_mapping, or
    a processed_file re-read by extract_failed_mis_rows. Returns one dict per
    row that isn't a clean ✅ MATCH (⚠️ MULTIPLE MATCHES, ❌ NO MATCH,
    FAILED - BAD DATA — see the RULE 1-6 / BAD DATA blocks above). Never
    touches or re-runs any matching logic.
    """
    if 'Mapping Status' not in df.columns:
        return []

    failed = df[df['Mapping Status'] != '✅ MATCH']
    if failed.empty:
        return []

    insurer_col = _find_column(df.columns, 'Policy: insurance company')
    payload_columns = [c for c in (_find_column(df.columns, target) for target in MATCHING_RULE_COLUMNS) if c]

    rows = []
    for _, row in failed.iterrows():
        reason = str(row.get('Failure Reason') or '').strip()

        # Only ⚠️ MULTIPLE MATCHES rows carry candidate Group IDs (embedded in
        # the reason text itself — see the 'Matching Group IDs: ...' sentence
        # built above). Truncated lists end in ' … (+N more)'; split that off
        # before pulling digits out, or the "N" in "+N more" gets mistaken
        # for a group id.
        group_ids = []
        m = _GROUP_IDS_PATTERN.search(reason)
        if m:
            head = m.group(1).split('…')[0]
            group_ids = [int(tok) for tok in re.findall(r'\d+', head)]

        payload = {}
        for col in payload_columns:
            val = row.get(col)
            if pd.isna(val):
                continue
            # Duck-typed rather than isinstance(val, pd.Timestamp): a date
            # column can come back as plain datetime.datetime/date instead of
            # Timestamp depending on how the source file stored it (mixed-type
            # 'object' columns after an Excel round-trip, in particular).
            if hasattr(val, 'strftime'):
                val = val.strftime('%d %b %Y')
            else:
                val = _json_safe(val)
            payload[col] = val

        insurer = row.get(insurer_col) if insurer_col else None
        if pd.isna(insurer):
            insurer = None
        else:
            insurer = _json_safe(insurer)

        mapping_status = str(row.get('Mapping Status') or '').strip()
        row_id = row.get('Original_Row_ID')

        rows.append({
            'row_id': int(row_id) if pd.notna(row_id) else None,
            'mapping_status': mapping_status,
            'status_key': MIS_STATUS_KEYS.get(mapping_status, 'OTHER'),
            'failure_reason': reason,
            'group_ids': group_ids,
            'insurer': insurer,
            'payload': payload,
        })
    return rows


def extract_failed_mis_rows(mis_obj):
    """
    Re-reads a COMPLETED MISFile's processed_file and runs the same
    extraction _extract_failed_rows_from_df does on df_final at process time.
    Only needed for files that finished processing before MISFailedRow
    existed — see sync_failed_rows_for_file. Purely read-only.
    """
    if not mis_obj.processed_file:
        return []

    file_ext = mis_obj.processed_file.name.split('.')[-1].lower()
    with mis_obj.processed_file.open('rb') as f:
        if file_ext == 'csv':
            df = pd.read_csv(f)
        else:
            df = pd.read_excel(f)

    return _extract_failed_rows_from_df(df)


def _bulk_save_failed_rows(mis_file, rows):
    MISFailedRow.objects.filter(mis_file=mis_file).delete()
    MISFailedRow.objects.bulk_create([
        MISFailedRow(
            mis_file=mis_file,
            # Original_Row_ID should always be a real int (assigned via
            # range(len(df_mis)) at process time) — the -(i+1) fallback only
            # exists for older processed files where it came back blank on
            # re-read. Using the enumerate position (not a shared constant)
            # keeps it unique per row even when several rows in the same
            # file are missing it, so the (mis_file, row_id) constraint
            # never collides.
            row_id=r['row_id'] if r['row_id'] is not None else -(i + 1),
            status_key=r['status_key'],
            mapping_status=r['mapping_status'],
            failure_reason=r['failure_reason'],
            group_ids=r['group_ids'],
            insurer=r['insurer'],
            payload=r['payload'],
        )
        for i, r in enumerate(rows)
    ])


def sync_failed_rows_for_file(mis_file):
    """
    One-time backfill for a COMPLETED MISFile that finished processing
    before MISFailedRow existed — reads its processed_file once and persists
    the same rows process_mis_mapping now writes directly from df_final.
    Idempotent: no-ops once health_synced_at is set.
    """
    if mis_file.health_synced_at:
        return
    rows = extract_failed_mis_rows(mis_file)
    _bulk_save_failed_rows(mis_file, rows)
    mis_file.health_synced_at = timezone.now()
    mis_file.save(update_fields=['health_synced_at'])


def _match_health_row(mis_row, grid_by_insurer, empty_grid, resolve_pincode):
    """
    Health equivalent of the Motor RULE 1-6 chain inside process_mis_mapping,
    matching a single MIS health row against HealthRateMaster instead of
    RateMaster. Same progressive-narrowing-with-failed_on-tracking shape as
    Motor, but over Health's own fields (see the 'mapping fields' sheet in
    the source config workbook): insurer -> date window -> product name /
    policy category / business type (categorical) -> plan name (fuzzy, no
    master table) -> deductible / sum-insured / age (numeric ranges) ->
    pincode zone (two-step PincodeMaster lookup — same mechanism as Motor's
    RTOMaster lookup, just against a single pincode_zone value per grid row
    instead of a comma cluster). Tenure (computed from inception/expiry
    dates) doesn't filter candidate rows — every candidate row already
    carries all 5 tenure columns — it only selects which one becomes Porate.
    Returns a result dict shaped exactly like Motor's per-row result so both
    feed the same df_extracted merge / payout_cols / coverage-gap machinery.
    """
    row_id = mis_row.get('Original_Row_ID')

    def _no_match(reason):
        return {
            'Original_Row_ID': row_id,
            'Mapping Status': '❌ NO MATCH',
            'Failure Reason': reason,
            'Displaygroupid': None,
            'Potype': None, 'Porate': None, 'Poflatamount': None,
            'Pitype': None, 'Pirate': None, 'Piflatamount': None,
            'Addtnc': None,
        }

    # --- TENURE CHECK: must resolve to a supported 1-5 year bucket ---
    # A real match on every other field is still useless without a rate
    # column to read Porate from, so an unsupported tenure makes the row NO
    # MATCH by itself. It is recorded as the row's first failure but no longer
    # ends it: every rule below still runs, so the reason lists them all.
    inception = mis_row['_mis_date']
    expiry = mis_row['_mis_h_expiry']
    tenure_years = None
    if pd.notna(inception) and pd.notna(expiry):
        days = (expiry - inception).days
        if days > 0:
            tenure_years = round(days / 365.25)

    failed_on = []
    if tenure_years in HEALTH_TENURE_RATE_COLUMNS:
        rate_col = HEALTH_TENURE_RATE_COLUMNS[tenure_years]
    else:
        rate_col = None
        computed = f"{tenure_years} year(s)" if tenure_years is not None else "an unparseable tenure"
        failed_on.append((
            'Policy tenure',
            f"computed {computed} from Policy: inception date / "
            f"Policy: expiry date, but the Health Grid only defines payout rates for 1-5 year tenures."
        ))

    # --- RULE H1: Insurance Company ---
    val_ins_raw = mis_row['_mis_ins']
    val_ins = mis_row['_mis_h_ins_mapped']
    if pd.isna(val_ins) or not val_ins:
        raw_display = '(blank)' if pd.isna(val_ins_raw) else val_ins_raw
        current_grid = empty_grid
        failed_on.append((
            'Policy: insurance company',
            f"Insurer '{raw_display}' did not fuzzy-match any active Health Rate Master insurer "
            f"(threshold={INSURANCE_FUZZY_THRESHOLD})."
        ))
    else:
        current_grid = grid_by_insurer.get(val_ins, empty_grid)
        if current_grid.empty:
            failed_on.append((
                'Policy: insurance company',
                f"Insurer resolved to '{val_ins}' but there are 0 active Health Rate Master rows for that insurer."
            ))

    # --- RULE H2: Date window (inception date within grid's From Date/to_date) ---
    if not current_grid.empty:
        if pd.isna(inception):
            rule_mask = current_grid['from_date'].isna() & current_grid['to_date'].isna()
            detail = (
                "Inception date is blank/unparseable on this policy, and no remaining "
                "candidate Health Grid row has an open (blank) date window."
            )
        else:
            min_cond = current_grid['from_date'].isna() | (current_grid['from_date'] <= inception)
            max_cond = current_grid['to_date'].isna() | (current_grid['to_date'] >= inception)
            rule_mask = min_cond & max_cond
            detail = (
                f"Inception date {inception.date()} falls outside the active date window "
                f"on every remaining candidate Health Grid row."
            )
        current_grid = narrow_candidates(current_grid, rule_mask, failed_on, 'Policy: inception date', detail)

    # --- RULE H3: Categorical matches (product name / policy category / business type) ---
    cat_rules = [
        ('_mis_h_prodname', 'product_name', 'Policy: product name'),
        ('_mis_h_category', 'policy_category', 'Policy: policy category'),
        ('_mis_h_biztype', 'business_type', 'Policy Type'),
    ]
    for mis_col, grid_col, label in cat_rules:
        if not current_grid.empty:
            val = mis_row[mis_col]
            rule_mask = current_grid[grid_col].apply(lambda g: check_categorical_match(val, g))
            current_grid = narrow_candidates(
                current_grid, rule_mask, failed_on, label,
                f"Value '{val}' did not match any remaining candidate Health Grid row's "
                f"{grid_col.replace('_', ' ')}."
            )

    # --- RULE H4: Plan name — fuzzy match against the grid row's own plan_names cluster ---
    if not current_grid.empty:
        val_plan = mis_row['_mis_h_plan_raw']
        if not val_plan or str(val_plan).strip().lower() == 'nan':
            rule_mask = current_grid['plan_names'] == ''
            detail = (
                "Plan name is blank on this policy, and no remaining candidate Health "
                "Grid row allows a blank plan list."
            )
        else:
            rule_mask = current_grid['plan_names'].apply(lambda g: fuzzy_match_plan_name(val_plan, g))
            detail = (
                f"Plan name '{val_plan}' did not fuzzy-match any plan listed on the "
                f"remaining candidate Health Grid rows."
            )
        current_grid = narrow_candidates(current_grid, rule_mask, failed_on, 'Policy: plan name', detail)

    # --- RULE H5: Numeric ranges (deductible / sum insured / age) ---
    range_rules = [
        ('_mis_h_deductible', 'min_deductible', 'max_deductible', 'Policy: deductible amount'),
        ('_mis_h_sum_assured', 'min_sum_insured', 'max_sum_insured', 'Policy: sum assured'),
        ('_mis_h_age', 'min_age', 'max_age', 'Customer: age'),
    ]
    for mis_col, min_col, max_col, label in range_rules:
        if not current_grid.empty:
            val = mis_row[mis_col]
            if pd.isna(val):
                rule_mask = current_grid[min_col].isna() & current_grid[max_col].isna()
                detail = (
                    f"{label} is blank/unparseable on this policy, and no remaining "
                    f"candidate Health Grid row has an open (blank) range."
                )
            else:
                min_cond = current_grid[min_col].isna() | (current_grid[min_col] <= val)
                max_cond = current_grid[max_col].isna() | (current_grid[max_col] >= val)
                rule_mask = min_cond & max_cond
                detail = (
                    f"{label} value {val} falls outside the range configured on every "
                    f"remaining candidate Health Grid row."
                )
            current_grid = narrow_candidates(current_grid, rule_mask, failed_on, label, detail)

    # --- RULE H6: Pincode zone — two-step PincodeMaster lookup ---
    # Same resolve-then-check-membership shape as Motor's RTO rule (Rule 5b),
    # except the grid side (HealthRateMaster.pincode_zone) holds one zone
    # value per row rather than a comma cluster — check_resolved_cluster_match
    # handles that identically either way (a 1-item list after split).
    if not current_grid.empty:
        val_pincode = normalize_pincode(mis_row['_mis_h_pincode_raw'])
        if not val_pincode:
            rule_mask = current_grid['pincode_zone'] == ''
            detail = (
                "Customer: pin code is blank on this policy, and no remaining candidate "
                "Health Grid row allows a blank pincode zone."
            )
        else:
            resolved_zone_names = resolve_pincode(val_pincode)
            if not resolved_zone_names:
                detail = (
                    f"Pincode '{val_pincode}' was not found in any PincodeMaster cluster — "
                    f"add it to PincodeMaster or check the MIS value."
                )
            else:
                preview = ", ".join(sorted(resolved_zone_names)[:5])
                detail = (
                    f"Pincode '{val_pincode}' resolved to zone(s) [{preview}], but no remaining "
                    f"candidate Health Grid row lists that zone."
                )
            rule_mask = current_grid['pincode_zone'].apply(
                lambda g: check_resolved_cluster_match(resolved_zone_names, g)
            )
        current_grid = narrow_candidates(current_grid, rule_mask, failed_on, 'Customer: pin code', detail)

    # Any failed check — the tenure included — means NO MATCH, however many
    # candidates are left: narrow_candidates keeps the pre-failure candidates so
    # the later rules still run, which means current_grid is NOT empty here.
    if failed_on:
        return _no_match(format_failed_on(failed_on))

    # --- FINAL RESOLUTION ---
    # No group_id concept on HealthRateMaster — "each imported row already is
    # one rate rule" (see the model docstring), so ambiguity is judged on
    # distinct row ids directly, not a group_id/id fallback like Motor.
    matched_grid = current_grid
    distinct_ids = matched_grid['id'].unique().tolist() if not matched_grid.empty else []

    # Several rows matched, but every one carries the same payout and payin
    # rate: which row wins can't change the rate, so it isn't an ambiguity
    # worth skipping the row for (same rule as Motor's identical-payout
    # resolution in process_mis_mapping). Use the lowest row id and carry on
    # through the single-row path below. Only the two rates are compared.
    tied_row_count = 0
    if len(distinct_ids) > 1 and health_matched_rows_share_one_rate(matched_grid, rate_col):
        tied_row_count = len(distinct_ids)
        distinct_ids = [min(distinct_ids)]

    if len(distinct_ids) == 1:
        best_match = matched_grid[matched_grid['id'] == distinct_ids[0]].iloc[0]
        result = {
            'Original_Row_ID': row_id,
            'Mapping Status': '✅ MATCH',
            'Failure Reason': 'Matched Successfully',
            'Displaygroupid': best_match.get('id'),
            'Potype': 'ON Net 1 Year',
            'Porate': best_match.get(rate_col),
            'Poflatamount': None,
            'Pitype': 'ON Net 1 Year',
            'Pirate': best_match.get('payin_rate'),
            'Piflatamount': None,
            'Addtnc': None,
        }
        if tied_row_count:
            result['Failure Reason'] = (
                f"Matched Successfully — {tied_row_count} Health Rate Master rows matched with "
                f"identical payout and payin rates; used Group {int(best_match.get('id'))} (lowest Group ID)."
            )
            # Private marker for the caller's run-summary count; the caller
            # pops it so it never becomes a column of the results frame.
            result['_tied_rows'] = tied_row_count
        return result

    if len(distinct_ids) > 1:
        sorted_ids = sorted(int(i) for i in distinct_ids)
        id_str = ', '.join(str(i) for i in sorted_ids[:10])
        if len(sorted_ids) > 10:
            id_str += f' … (+{len(sorted_ids) - 10} more)'
        return {
            'Original_Row_ID': row_id,
            'Mapping Status': '⚠️ MULTIPLE MATCHES',
            'Failure Reason': (
                f"Multiple Health Rate Master rows matched ({len(sorted_ids)} rows). Please "
                f"refine Health Rate Master so only one row applies. Matching Group IDs: {id_str}"
            ),
            'Displaygroupid': None,
            'Potype': None, 'Porate': None, 'Poflatamount': None,
            'Pitype': None, 'Pirate': None, 'Piflatamount': None,
            'Addtnc': None,
        }

    return _no_match("No rates found for criteria")


def process_mis_mapping(mis_file_id):
    # Always forcefully wipe the thread's DB connection state to prevent inheritance locks
    connection.close()

    try:
        mis_obj = MISFile.objects.get(id=mis_file_id)
        mis_obj.status = 'PROCESSING'
        mis_obj.save(update_fields=['status'])
    except Exception:
        logger.exception("Failed to initialize processing state for MISFile %s", mis_file_id)
        connection.close()
        return

    try:
        # 1. READ MIS FILE
        file_ext = mis_obj.uploaded_file.name.split('.')[-1].lower()
        # .open() rather than .path — .path isn't available on non-filesystem
        # storage backends (e.g. S3-compatible object storage).
        if file_ext == 'csv':
            with mis_obj.uploaded_file.open('rb') as f:
                df_mis = pd.read_csv(f)
        else:
            with mis_obj.uploaded_file.open('rb') as f:
                df_mis = pd.read_excel(f)

        # Clean up column names internally
        df_mis.columns = [str(col).strip() for col in df_mis.columns]
        df_mis['Original_Row_ID'] = range(len(df_mis))
        mis_cols = df_mis.columns.tolist()

        # 3. PRE-COMPUTE MIS DATA
        # Moved ahead of the Rate Master fetch (Recommendation 5): _mis_ins is
        # needed to know which insurers this file could possibly match before
        # fetching any Rate Master rows. Nothing else in this block depends on
        # df_grid, so hoisting the whole section is a pure reorder.
        df_mis['_mis_ins'] = safe_get_col(df_mis, 'Policy: insurance company')
        df_mis['_mis_prod'] = safe_get_col(df_mis, 'Policy: vehproduct').astype(str).str.strip().str.lower()
        df_mis['_mis_sub_prod'] = safe_get_col(df_mis, 'Policy: sub product').astype(str).str.strip().str.lower()
        df_mis['_mis_fuel'] = safe_get_col(df_mis, 'Policy: fuel').astype(str).str.strip().str.lower()
        df_mis['_mis_class'] = safe_get_col(df_mis, 'Policy: vehicle class').astype(str).str.strip().str.lower()

        _make = safe_get_col(df_mis, 'Policy: vehicle make').fillna('').astype(str)
        _model = safe_get_col(df_mis, 'Policy: model').fillna('').astype(str)
        # Rule 5a matches on make+model concatenated with a single space
        # (e.g. "YAMAHA" + "ALPHA" -> "yamaha alpha"), not make alone —
        # this is the only make/model column it needs.
        df_mis['_mis_make_model'] = (_make + " " + _model).str.strip().str.lower()

        # RTO normalization happens before the two-step master lookup: strip
        # internal spaces and hyphens, then truncate to 4 chars so full
        # registration numbers (e.g. "MH05FG9876") collapse to the RTO code
        # ("MH05"). Raw value is kept alongside for readable failure messages.
        _rto_raw_series = safe_get_col(df_mis, 'Policy: rto no').astype(str).str.strip()
        df_mis['_mis_rto_raw'] = _rto_raw_series
        df_mis['_mis_rto'] = _rto_raw_series.apply(normalize_rto_code).str.lower()

        # Regex strips letters ("1500 CC" -> 1500). Values that jam two
        # numbers together with a slash (CC/GVW combined, e.g. "6702/47500")
        # are flagged up front instead of being silently digit-stripped into
        # one nonsense integer — see is_combined_cc_gvw / FAILED - BAD DATA below.
        #
        # GCV 3W/4W are weight-classified rather than engine-classified —
        # Rate Master's cc_min/cc_max columns hold GVW ranges for these
        # products (shown as "CC / GVW" in the Rate Master UI). So for those
        # rows, source the raw value from 'Policy: gvw gross vehicle weight'
        # instead of 'Policy: cc cubic capacity'; every other product keeps
        # using CC as before.
        _is_gcv = df_mis['_mis_prod'].isin(['gcv 3w', 'gcv 4w'])
        _cc_col_series = safe_get_col(df_mis, 'Policy: cc cubic capacity').astype(str).str.strip()
        _gvw_col_series = safe_get_col(df_mis, 'Policy: gvw gross vehicle weight').astype(str).str.strip()
        _cc_raw_series = _gvw_col_series.where(_is_gcv, _cc_col_series)
        df_mis['_mis_cc_raw'] = _cc_raw_series
        df_mis['_mis_cc_source_label'] = _is_gcv.map({
            True: 'Policy: gvw gross vehicle weight',
            False: 'Policy: cc cubic capacity'
        })
        df_mis['_mis_cc_bad_data'] = _cc_raw_series.apply(is_combined_cc_gvw)
        cc_raw = _cc_raw_series.str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_cc'] = pd.to_numeric(cc_raw, errors='coerce')

        sc_raw = safe_get_col(df_mis, 'Policy: seating capacity').astype(str).str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_sc'] = pd.to_numeric(sc_raw, errors='coerce')

        tariff_raw = safe_get_col(df_mis, 'Policy: tariff rate').astype(str).str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_tariff'] = pd.to_numeric(tariff_raw, errors='coerce')

        df_mis['_mis_date'] = pd.to_datetime(safe_get_col(df_mis, 'Policy: inception date'), errors='coerce', dayfirst=True)

        # Vehicle age (RULE 3) is calculated, not read: the current year (the
        # year this run happens in) + 1, minus the year of the registration
        # date — or of the inception date for a new vehicle (sub product
        # 1+3 / 1+5). The label records which column the year came from, for
        # RULE 3's failure messages.
        _is_new_vehicle = df_mis['_mis_sub_prod'].isin(NEW_VEHICLE_SUB_PRODUCTS)
        df_mis['_mis_age'] = calculate_vehicle_age(
            safe_get_col(df_mis, 'Policy: date of registration'),
            safe_get_col(df_mis, 'Policy: inception date'),
            _is_new_vehicle,
            timezone.localdate().year,
        )
        df_mis['_mis_age_source_label'] = _is_new_vehicle.map({
            True: VEHICLE_AGE_NEW_VEHICLE_LABEL,
            False: VEHICLE_AGE_REGISTRATION_LABEL,
        })

        df_mis['_mis_ncb'] = pd.to_numeric(safe_get_col(df_mis, 'Policy: no claim bonus'), errors='coerce')
        df_mis['_mis_cpa'] = pd.to_numeric(safe_get_col(df_mis, 'Policy: cpa'), errors='coerce')
        df_mis['_mis_zd'] = safe_get_col(df_mis, 'Policy: nil dep').astype(str).str.strip().str.upper()

        # --- PRODUCT ROUTING KEYS ---
        # The Product column decides which Rate Master a row may be compared
        # against: 'motor' -> RateMaster only, 'health' -> HealthRateMaster only,
        # anything else -> neither (see the PRODUCT ROUTING block in the row
        # loop). Defined ahead of every insurer/rate lookup below so nothing is
        # fuzzy-matched or fetched for a row before its table is known.
        df_mis['_mis_product'] = safe_get_col(df_mis, 'Product').astype(str).str.strip().str.lower()
        df_mis['_mis_is_motor'] = df_mis['_mis_product'] == 'motor'

        # --- PREMIUM / NCB VALIDATION (Motor rows) ---
        # STP / SAOD / 1+1 / 1+3 / 1+5 must have their premium split between
        # 'Policy: total od' and 'Policy: tp premium' in one fixed way, and
        # 1+3 / 1+5 must carry no NCB — see premium_validation_errors for the
        # rules. Worked out for the whole frame here; the row loop turns a
        # non-blank value into FAILED - BAD DATA before any rate lookup. Kept
        # to Motor rows (.where) so a Health row is never touched by it.
        df_mis['_mis_validation_error'] = premium_validation_errors(
            df_mis['_mis_sub_prod'],
            df_mis['_mis_prod'],
            safe_get_col(df_mis, 'Policy: total od'),
            safe_get_col(df_mis, 'Policy: tp premium'),
            safe_get_col(df_mis, 'Policy: no claim bonus'),
        ).where(df_mis['_mis_is_motor'], '')

        # --- HEALTH PRE-COMPUTE ---
        # Mirrors the Motor _mis_* block above, but for the fields Health
        # matching needs (see _match_health_row). 'Policy: insurance company'
        # and inception date are shared with Motor via _mis_ins/_mis_date —
        # no need to recompute them under a separate name.
        df_mis['_mis_h_is_health'] = safe_get_col(df_mis, 'Product').astype(str).str.strip().str.lower() == 'health'
        df_mis['_mis_h_prodname'] = safe_get_col(df_mis, 'Policy: product name').astype(str).str.strip().str.lower()
        df_mis['_mis_h_category'] = safe_get_col(df_mis, 'Policy: policy category').astype(str).str.strip().str.lower()
        # .apply(_normalize_health_business_type) instead of the plain
        # .str.strip().str.lower() every other _mis_h_* categorical column
        # uses — folds MIS 'RollOver' onto the Health Grid's 'Port' spelling
        # (see _HEALTH_BUSINESS_TYPE_SYNONYMS) while leaving every other
        # business type untouched.
        df_mis['_mis_h_biztype'] = safe_get_col(df_mis, 'Policy Type').apply(_normalize_health_business_type)
        df_mis['_mis_h_plan_raw'] = safe_get_col(df_mis, 'Policy: plan name').fillna('').astype(str).str.strip()
        df_mis['_mis_h_pincode_raw'] = safe_get_col(df_mis, 'Customer: pin code')
        df_mis['_mis_h_expiry'] = pd.to_datetime(safe_get_col(df_mis, 'Policy: expiry date'), errors='coerce', dayfirst=True)

        h_deductible_raw = safe_get_col(df_mis, 'Policy: deductible amount').astype(str).str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_h_deductible'] = pd.to_numeric(h_deductible_raw, errors='coerce')

        h_sum_assured_raw = safe_get_col(df_mis, 'Policy: sum assured').astype(str).str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_h_sum_assured'] = pd.to_numeric(h_sum_assured_raw, errors='coerce')

        h_age_raw = safe_get_col(df_mis, 'Customer: age').astype(str).str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_h_age'] = pd.to_numeric(h_age_raw, errors='coerce')

        # --- CP PREMIUM# (reconciliation) ---
        # Computed straight from raw MIS fields, independent of Rate Master
        # matching, so it's populated for every row regardless of Mapping
        # Status:
        #   1. Private Car + sub product in {1+1, 1+3, SAOD} -> Policy: total od
        #   2. Else, Product == Non Motor -> gwp net premium - tp terrorism premium
        #   3. Else -> gwp net premium
        # Checked in that order (case 1 wins over case 2 on any overlap),
        # matching the if/elif/else the business logic was specified as.
        _cp_policy_category = safe_get_col(df_mis, 'Policy: policy category').astype(str).str.strip().str.lower()
        _cp_sub_product = safe_get_col(df_mis, 'Policy: sub product').astype(str).str.strip().str.lower()
        # 'Product' values seen in practice: 'motor', 'non_motor', 'health', 'life' —
        # normalize the underscore away so 'non_motor' and 'non motor' are the same check.
        _cp_product = safe_get_col(df_mis, 'Product').astype(str).str.strip().str.lower().str.replace('_', ' ', regex=False)

        # Data cleansing: blank cells and null-equivalent strings ('null',
        # 'nul', 'nill', 'na' — case-insensitive) are treated as 0 for all
        # three fields feeding this calculation.
        _cp_total_od = clean_cp_numeric(safe_get_col(df_mis, 'Policy: total od'))
        _cp_gwp_net_premium = clean_cp_numeric(safe_get_col(df_mis, 'Policy: gwp net premium'))
        _cp_tp_terrorism_premium = clean_cp_numeric(safe_get_col(df_mis, 'Policy: tp terrorism premium'))

        _cp_is_private_car_saod_group = (_cp_policy_category == 'private car') & _cp_sub_product.isin(['1+1', '1+3', 'saod'])
        _cp_is_non_motor = _cp_product == 'non motor'

        df_mis['cp premium#'] = _cp_gwp_net_premium
        df_mis.loc[_cp_is_non_motor, 'cp premium#'] = _cp_gwp_net_premium - _cp_tp_terrorism_premium
        df_mis.loc[_cp_is_private_car_saod_group, 'cp premium#'] = _cp_total_od

        # 2. FETCH RATE MASTER DATA
        # Two-phase (Recommendation 5): first find which insurers actually
        # have active rows at all (cheap — one column, deduplicated), fuzzy-
        # match this file's insurers against just that list, then fetch full
        # rows only for the insurers this file could possibly match. Avoids
        # materializing Rate Master rows for insurers that aren't even
        # present in the upload.
        distinct_raw_insurers = list(
            RateMaster.objects.filter(status="ACTIVE", is_deleted="NO")
            .values_list('insurance_company', flat=True).distinct()
        )
        if not distinct_raw_insurers:
            raise ValueError("RateMaster has no active records configured.")

        # Maps the normalized (strip+lower) name the fuzzy matcher works with
        # back to the exact raw DB value(s) to filter on — get_fuzzy_dict
        # normalizes its target list internally, so this mirrors that.
        raw_by_normalized = {}
        for raw in distinct_raw_insurers:
            if raw and str(raw).strip():
                raw_by_normalized.setdefault(str(raw).strip().lower(), set()).add(raw)

        # Insurance company fuzzy mapping — now powered by RapidFuzz (5-100x
        # faster), and now matched against just the distinct insurer list
        # instead of the full grid.
        # Only Motor rows' insurers are matched against RateMaster's insurer
        # list — a Health row's insurer belongs to HealthRateMaster (matched
        # separately below), never to this one.
        fuzzy_ins_map = get_fuzzy_dict(
            df_mis.loc[df_mis['_mis_is_motor'], '_mis_ins'].unique(),
            distinct_raw_insurers,
            threshold=INSURANCE_FUZZY_THRESHOLD
        )
        df_mis['_mis_ins_mapped'] = df_mis['_mis_ins'].map(fuzzy_ins_map)

        relevant_raw_insurers = set()
        for normalized_name in {v for v in fuzzy_ins_map.values() if v}:
            relevant_raw_insurers.update(raw_by_normalized.get(normalized_name, set()))

        # Explicit columns= keeps df_grid's shape correct even when
        # relevant_raw_insurers is empty (none of this file's insurers matched
        # anything active) — pd.DataFrame.from_records([]) with no columns=
        # would otherwise produce a frame with zero columns and break every
        # df_grid[...] reference below. An empty-but-correctly-shaped grid is
        # the right outcome here, not an error: it's the same result the old
        # full fetch would have reached row-by-row (every row's insurer fails
        # to resolve), just without spending time fetching irrelevant rows.
        grid_columns = [
            'id', 'group_id', 'po_type', 'po_od_rate', 'po_tp_rate', 'po_net_rate',
            'po_flat_amount', 'add_tnc', 'insurance_company',
            'pi_type', 'pi_od_rate', 'pi_tp_rate', 'pi_net_rate', 'pi_flat_amount',
            'product__name', 'sub_product__name', 'fuel_type__name', 'make_model_class__name',
            'is_ncb__code', 'is_cpa__code', 'is_zd__code',
            'new_vehicle_makes', 'new_rto_list',
            'cc_min', 'cc_max', 'sc_min', 'sc_max',
            'vehicle_age_min', 'vehicle_age_max', 'tariff_min', 'tariff_max',
            'from_date', 'to_date',
        ]
        qs = RateMaster.objects.filter(
            status="ACTIVE", is_deleted="NO", insurance_company__in=relevant_raw_insurers
        ).values(*grid_columns)
        df_grid = pd.DataFrame.from_records(qs, columns=grid_columns)

        df_grid = df_grid.rename(columns={
            'product__name': 'product',
            'sub_product__name': 'sub_product',
            'fuel_type__name': 'fuel_type',
            'make_model_class__name': 'make_model_class',
            'is_ncb__code': 'is_ncb',
            'is_cpa__code': 'is_cpa',
            'is_zd__code': 'is_zd',
        })

        # Vectorized equivalent of the old per-object normalization: blank
        # (NULL/empty) content fields fall back to '', NCB/CPA/ZD codes fall
        # back to 'NA' — same rules as before, just applied to a whole
        # column at once instead of per Django object.
        for col in ['insurance_company', 'product', 'sub_product', 'fuel_type',
                    'make_model_class', 'new_vehicle_makes', 'new_rto_list']:
            df_grid[col] = df_grid[col].fillna('').astype(str).str.strip().str.lower()

        for col in ['is_ncb', 'is_cpa', 'is_zd']:
            df_grid[col] = df_grid[col].fillna('NA').astype(str).str.strip().str.upper()

        # Preload RTOMaster and MakeModelMaster once per job — these power
        # the two-step lookup chain for Rules 5a/5b below (MIS value ->
        # master cluster -> master name -> RateMaster cluster).
        resolve_rto = build_master_lookup(
            RTOMaster.objects.all(), 'rto_name', 'rto_cluster'
        )
        # Vehicle make/model uses fuzzy word-overlap matching (Rule 5a), not the
        # strict substring match RTO above still uses — see build_make_model_lookup.
        resolve_make = build_make_model_lookup(
            MakeModelMaster.objects.all(), 'make_model_name', 'make_model_cluster'
        )

        # Cast Grid numeric & date columns safely
        for col in ['cc_min', 'cc_max', 'sc_min', 'sc_max', 'vehicle_age_min', 'vehicle_age_max', 'tariff_min', 'tariff_max']:
            df_grid[col] = pd.to_numeric(df_grid[col], errors='coerce')
        df_grid['from_date'] = pd.to_datetime(df_grid['from_date'], errors='coerce')
        df_grid['to_date'] = pd.to_datetime(df_grid['to_date'], errors='coerce')

        # Rule 1 (insurer) is an exact match and mutually exclusive — every
        # grid row belongs to exactly one insurer bucket. Partitioning once
        # here turns each MIS row's insurer lookup into an O(1) dict access
        # instead of scanning the full grid, and every rule after it then
        # starts from that already-small slice automatically instead of
        # rescanning the whole grid on every row.
        grid_by_insurer = {name: sub_df for name, sub_df in df_grid.groupby('insurance_company')}
        _empty_grid = df_grid.iloc[0:0]

        # 2b. FETCH HEALTH GRID DATA (only if this file actually has Health
        # rows — skips the query entirely for pure-Motor uploads). Same
        # two-phase shape as the Motor fetch above: find which insurers this
        # file could match, fuzzy-map this file's insurers against just that
        # list, then fetch full Health Grid rows only for insurers that could
        # possibly match.
        health_grid_columns = [
            'id', 'insurance_company', 'product_name', 'policy_category', 'plan_names',
            'business_type', 'min_deductible', 'max_deductible', 'min_sum_insured',
            'max_sum_insured', 'min_age', 'max_age', 'pincode_zone', 'payin_rate',
            'one_year_rate', 'multi_year_2_rate', 'multi_year_3_rate',
            'multi_year_4_rate', 'multi_year_5_rate', 'from_date', 'to_date',
        ]
        has_health_rows = bool(df_mis['_mis_h_is_health'].any())

        if has_health_rows:
            distinct_health_insurers = list(
                HealthRateMaster.objects.exclude(is_deleted="YES").filter(status="ACTIVE")
                .values_list('insurance_company', flat=True).distinct()
            )

            raw_health_by_normalized = {}
            for raw in distinct_health_insurers:
                if raw and str(raw).strip():
                    raw_health_by_normalized.setdefault(str(raw).strip().lower(), set()).add(raw)

            fuzzy_health_ins_map = get_fuzzy_dict(
                df_mis.loc[df_mis['_mis_h_is_health'], '_mis_ins'].unique(),
                distinct_health_insurers,
                threshold=INSURANCE_FUZZY_THRESHOLD
            )
            df_mis['_mis_h_ins_mapped'] = df_mis['_mis_ins'].map(fuzzy_health_ins_map)

            relevant_health_insurers = set()
            for normalized_name in {v for v in fuzzy_health_ins_map.values() if v}:
                relevant_health_insurers.update(raw_health_by_normalized.get(normalized_name, set()))

            health_qs = HealthRateMaster.objects.exclude(is_deleted="YES").filter(
                status="ACTIVE", insurance_company__in=relevant_health_insurers
            ).values(*health_grid_columns)
            df_health_grid = pd.DataFrame.from_records(health_qs, columns=health_grid_columns)

            for col in ['insurance_company', 'product_name', 'policy_category', 'pincode_zone']:
                df_health_grid[col] = df_health_grid[col].fillna('').astype(str).str.strip().str.lower()
            # business_type goes through the same RollOver/Port synonym fold as
            # the MIS side (_mis_h_biztype) instead of the plain lowercase the
            # other columns above get — see _normalize_health_business_type.
            df_health_grid['business_type'] = df_health_grid['business_type'].fillna('').apply(_normalize_health_business_type)
            # plan_names keeps its original casing — fuzzy_match_plan_name normalizes internally.
            df_health_grid['plan_names'] = df_health_grid['plan_names'].fillna('').astype(str)

            for col in ['min_deductible', 'max_deductible', 'min_sum_insured', 'max_sum_insured',
                        'min_age', 'max_age', 'payin_rate', 'one_year_rate', 'multi_year_2_rate',
                        'multi_year_3_rate', 'multi_year_4_rate', 'multi_year_5_rate']:
                df_health_grid[col] = pd.to_numeric(df_health_grid[col], errors='coerce')
            df_health_grid['from_date'] = pd.to_datetime(df_health_grid['from_date'], errors='coerce')
            df_health_grid['to_date'] = pd.to_datetime(df_health_grid['to_date'], errors='coerce')

            # PincodeMaster: Health's equivalent of RTOMaster — same
            # build_master_lookup mechanism resolves a raw MIS pincode to the
            # zone name(s) whose cluster contains it.
            resolve_pincode = build_master_lookup(
                PincodeMaster.objects.all(), 'pincode_zone', 'pincode_cluster'
            )

            health_grid_by_insurer = {name: sub_df for name, sub_df in df_health_grid.groupby('insurance_company')}
            _empty_health_grid = df_health_grid.iloc[0:0]
        else:
            df_mis['_mis_h_ins_mapped'] = None
            health_grid_by_insurer = {}
            _empty_health_grid = pd.DataFrame(columns=health_grid_columns)
            resolve_pincode = lambda _v: set()

        results = []
        total_identical_payout_resolved = 0

        # 4. ROW-BY-ROW PROCESSING
        # Iterate a slimmed frame (just the precomputed _mis_* columns + the
        # row id) instead of the full ~100+ column df_mis — cuts per-row
        # Series-boxing overhead without the correctness risk of switching to
        # .itertuples() (which would silently rename every leading-underscore
        # column to a positional _1, _2, ... field, since Python namedtuples
        # don't allow underscore-prefixed names). Access pattern inside the
        # loop (mis_row['_mis_xxx']) is unchanged.
        mis_cols_for_loop = ['Original_Row_ID'] + [c for c in df_mis.columns if c.startswith('_mis_')]
        df_mis_slim = df_mis[mis_cols_for_loop]

        for idx, mis_row in df_mis_slim.iterrows():

            # --- PRODUCT ROUTING (first step for every row) ---
            # The Product column selects which Rate Master this row may be
            # compared against, before any other check, range test, fuzzy
            # match or rate logic runs:
            #   'health' -> HealthRateMaster only (_match_health_row)
            #   'motor'  -> RateMaster only (the BAD DATA guards and RULE 1-6
            #               chain below)
            #   anything else (blank, 'non_motor', 'life', a typo) -> neither
            if mis_row['_mis_h_is_health']:
                health_result = _match_health_row(mis_row, health_grid_by_insurer, _empty_health_grid, resolve_pincode)
                if health_result.pop('_tied_rows', 0):
                    total_identical_payout_resolved += 1
                results.append(health_result)
                continue

            if not mis_row['_mis_is_motor']:
                raw_product = mis_row['_mis_product']
                shown_product = '(blank)' if pd.isna(raw_product) or raw_product in ('', 'nan', 'none') else raw_product
                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': '❌ NO MATCH',
                    'Failure Reason': (
                        f"Failed on: Product — Product '{shown_product}' is neither 'motor' nor 'health', "
                        f"so no Rate Master applies and the row was not matched. Correct the Product "
                        f"column and re-run mapping for this row."
                    ),
                    'Displaygroupid': None,
                    'Potype': None, 'Porate': None, 'Poflatamount': None,
                    'Pitype': None, 'Pirate': None, 'Piflatamount': None,
                    'Addtnc': None
                })
                continue

            # --- DATA VALIDATION: every check runs, every error is kept ---
            # Both are flagged during pre-compute: a combined CC/GVW cell
            # (is_combined_cc_gvw) and the premium split / NCB rules
            # (premium_validation_errors). Neither stops the row: a row with
            # any data error ends as FAILED - BAD DATA (decided after the rate
            # chain below, which still runs so the row's reason can list the
            # rate-matching failures too — see FINAL RESOLUTION), and one with
            # both lists both. Motor rows only: this sits after the Product
            # routing above, so it never fires for (or blocks) a Health row.
            data_errors = []
            if mis_row['_mis_cc_bad_data']:
                data_errors.append(
                    f"{mis_row['_mis_cc_source_label']} value '{mis_row['_mis_cc_raw']}' looks like a "
                    f"combined CC/GVW figure (two numbers separated by '/'). Not auto-parsed — "
                    f"correct the source MIS data and re-run mapping for this row."
                )
            if mis_row['_mis_validation_error']:
                data_errors.append(mis_row['_mis_validation_error'])

            # Motor row (Product == 'motor'): the RULE 1-6 chain below runs
            # against RateMaster only. A failed rule no longer ends the chain —
            # it is recorded in failed_on and the next rule still runs (see
            # narrow_candidates), so a NO MATCH row reports every rule it fails.
            failed_on = []

            # --- RULE 1: Insurance Company ---
            # Instead of masking the full ~100k-row grid with an equality
            # check, jump straight to that insurer's pre-partitioned slice
            # (grid_by_insurer, built once above) — every rule after this one
            # then only ever sees that already-narrowed slice.
            val_ins_raw = mis_row['_mis_ins']
            val_ins = mis_row['_mis_ins_mapped']
            # .map() on a dict silently turns a `None` value (get_fuzzy_dict's
            # "no fuzzy match found above threshold" marker) into float NaN —
            # and bool(nan) is True, so a plain `if not val_ins` falls through
            # to the wrong branch below and reports a misleading "resolved to
            # 'nan'" instead of the real, actionable reason.
            if pd.isna(val_ins) or not val_ins:
                raw_display = '(blank)' if pd.isna(val_ins_raw) else val_ins_raw
                failed_on.append((
                    'Policy: insurance company',
                    f"Insurer '{raw_display}' did not fuzzy-match any active Rate Master insurer "
                    f"(threshold={INSURANCE_FUZZY_THRESHOLD}). Either no active rate is configured "
                    f"for this insurer, or the name on file differs too much to match."
                ))
                current_grid = _empty_grid
            else:
                current_grid = grid_by_insurer.get(val_ins, _empty_grid)
                if current_grid.empty:
                    failed_on.append((
                        'Policy: insurance company',
                        f"Insurer resolved to '{val_ins}' but there are 0 active Rate Master rows for that insurer."
                    ))

            # --- RULE 2: Categorical Matches ---
            # Product, sub product and fuel are all exact, case-insensitive
            # matches (check_exact_match) — no substring or fuzzy matching.
            # Vehicle class is handled separately below (Rule 2b) with its own
            # direct-match + NA-wildcard logic, so it is excluded here.
            cat_rules = [
                ('_mis_prod', 'product', 'Policy: vehproduct'),
                ('_mis_sub_prod', 'sub_product', 'Policy: sub product'),
                ('_mis_fuel', 'fuel_type', 'Policy: fuel'),
            ]
            for mis_col, grid_col, label in cat_rules:
                if not current_grid.empty:
                    val = mis_row[mis_col]
                    rule_mask = current_grid[grid_col].apply(lambda g: check_exact_match(val, g))
                    current_grid = narrow_candidates(
                        current_grid, rule_mask, failed_on, label,
                        f"Value '{val}' did not match any remaining candidate Rate Master row's "
                        f"{grid_col.replace('_', ' ')} (after the previous rules narrowed the field)."
                    )

            # --- RULE 2b: Vehicle Class — direct match + NA wildcard ---
            # Uses case-insensitive exact match only (no fuzzy/substring).
            # RateMaster rows where make_model_class == 'na' are treated as a
            # wildcard — they pass regardless of what vehicle class the MIS row
            # carries. All other RateMaster values must match exactly
            # (e.g. 'bike' == 'bike', 'car' == 'car').
            # If the MIS value itself is blank or 'na', only NA wildcard rows pass.
            if not current_grid.empty:
                val_class = mis_row['_mis_class']  # already .strip().lower()
                mis_class_is_blank = (not val_class or val_class == 'nan')

                def match_vehicle_class(grid_val):
                    g = str(grid_val).strip().lower() if grid_val else ''
                    # NA in RateMaster → always passes (wildcard)
                    if g == 'na' or not g:
                        return True
                    # blank MIS value → only NA wildcard rows pass
                    if mis_class_is_blank:
                        return False
                    # direct case-insensitive exact match
                    return val_class == g

                rule_mask = current_grid['make_model_class'].apply(match_vehicle_class)
                current_grid = narrow_candidates(
                    current_grid, rule_mask, failed_on, 'Policy: vehicle class',
                    f"Vehicle class '{val_class}' has no exact match among remaining candidate "
                    f"rate rows, and none of them has an NA-wildcard class."
                )

            # --- RULE 3: Numeric Ranges ---
            # Last element: is the band's LOWER bound inclusive? True for CC/
            # GVW, seating capacity and tariff, exactly as before — all of them
            # are inclusive on both ends. Vehicle age is a whole number
            # ((current year + 1) - registration year, or inception year for a
            # new vehicle), so a vehicle registered in the current year is age
            # 1 and always belongs to the '0 to 1' slab, last year's is age 2
            # and belongs to '1 to 2', and so on. An age band is therefore
            # (min, max]: the lower bound is EXCLUSIVE and the upper bound
            # inclusive, which puts an age on a seam (age 1 between '0 to 1'
            # and '1 to 5.01') in exactly one band — the lower one. Bands
            # written with an x.01 bound still behave: '0 to 1.01' covers age 1
            # and '1.01 to 5.01' starts at age 2, as no whole-number age
            # equals x.01.
            range_rules = [
                ('_mis_cc', 'cc_min', 'cc_max', mis_row['_mis_cc_source_label'], True),
                ('_mis_sc', 'sc_min', 'sc_max', 'Policy: seating capacity', True),
                ('_mis_age', 'vehicle_age_min', 'vehicle_age_max', mis_row['_mis_age_source_label'], False),
                ('_mis_tariff', 'tariff_min', 'tariff_max', 'Policy: tariff rate', True),
            ]
            for mis_col, min_col, max_col, label, min_inclusive in range_rules:
                # A combined CC/GVW cell has no usable number to range-check; it
                # is already reported as a data error above.
                if mis_col == '_mis_cc' and mis_row['_mis_cc_bad_data']:
                    continue
                if not current_grid.empty:
                    val = mis_row[mis_col]
                    if pd.isna(val):
                        rule_mask = current_grid[min_col].isna() & current_grid[max_col].isna()
                        detail = (
                            f"{label} is blank/unparseable on this policy, and no remaining "
                            f"candidate rate row has an open (blank) range."
                        )
                    else:
                        if min_inclusive:
                            min_cond = current_grid[min_col].isna() | (current_grid[min_col] <= val)
                        else:
                            min_cond = current_grid[min_col].isna() | (current_grid[min_col] < val)
                        max_cond = current_grid[max_col].isna() | (current_grid[max_col] >= val)
                        rule_mask = min_cond & max_cond
                        if mis_col == '_mis_age':
                            detail = (
                                f"Calculated vehicle age '{val:g}' ((current year + 1) - the year in {label}) "
                                f"falls outside the age band (lower bound exclusive, upper bound inclusive) "
                                f"configured on every remaining candidate rate row."
                            )
                        else:
                            detail = (
                                f"{label} value {val} falls outside the range configured on every "
                                f"remaining candidate rate row."
                            )
                    current_grid = narrow_candidates(current_grid, rule_mask, failed_on, label, detail)

            # --- RULE 4: Dates ---
            if not current_grid.empty:
                val = mis_row['_mis_date']
                if pd.isna(val):
                    rule_mask = current_grid['from_date'].isna() & current_grid['to_date'].isna()
                    detail = (
                        "Inception date is blank/unparseable on this policy, and no remaining "
                        "candidate rate row has an open (blank) date window."
                    )
                else:
                    min_cond = current_grid['from_date'].isna() | (current_grid['from_date'] <= val)
                    max_cond = current_grid['to_date'].isna() | (current_grid['to_date'] >= val)
                    rule_mask = min_cond & max_cond
                    detail = (
                        f"Inception date {val.date()} falls outside the active date window "
                        f"on every remaining candidate rate row."
                    )
                current_grid = narrow_candidates(current_grid, rule_mask, failed_on, 'Policy: inception date', detail)

            # --- RULE 5a: Vehicle Make/Model — two-step lookup, fuzzy word-overlap ---
            # Input is "Policy: vehicle make" + "Policy: model" concatenated with a
            # single space (e.g. "YAMAHA" + "ALPHA" -> "yamaha alpha"), not make alone.
            # Step 1: match that string against MakeModelMaster.make_model_cluster via
            #         fuzzy_match_make_model — a cluster entry matches if it shares at
            #         least MAKE_MODEL_MIN_WORD_MATCH words with it (not a strict/substring
            #         match) -> extract the matching row's make_model_name, e.g. {'private_car_all'}
            # Step 2: check RateMaster.new_vehicle_makes for 'private_car_all' (unchanged —
            #         still an exact item match against the Rate Master's own cluster list).
            # No fallback: if the make+model string doesn't share enough words with any
            # MakeModelMaster cluster entry, the row fails this rule. Note: if either the
            # make or the model is blank, the search string may have only 1 word available,
            # so the word-match threshold can never be met for that row by design.
            if not current_grid.empty:
                val_make_model = mis_row['_mis_make_model']
                if val_make_model == 'nan' or not val_make_model:
                    rule_mask = current_grid['new_vehicle_makes'] == ''
                    detail = (
                        "Vehicle make/model is blank on this policy, and no remaining candidate "
                        "rate row allows a blank vehicle-make cluster."
                    )
                else:
                    resolved_make_names = resolve_make(val_make_model)
                    if not resolved_make_names:
                        detail = make_model_unresolved_detail(val_make_model)
                    else:
                        preview = ", ".join(sorted(resolved_make_names)[:5])
                        detail = (
                            f"Vehicle make/model '{val_make_model}' resolved to master group(s) "
                            f"[{preview}], but no remaining candidate rate row lists that group in "
                            f"its vehicle-make cluster."
                        )
                    rule_mask = current_grid['new_vehicle_makes'].apply(
                        lambda g: check_resolved_cluster_match(resolved_make_names, g)
                    )

                current_grid = narrow_candidates(current_grid, rule_mask, failed_on, MAKE_MODEL_RULE_LABEL, detail)

            # --- RULE 5b: RTO — two-step master lookup ---
            # _mis_rto is already normalized (normalize_rto_code): spaces/hyphens
            # stripped, truncated to 4 chars, so full registration numbers like
            # "MH05FG9876" resolve as "MH05" instead of failing outright.
            # Step 1: resolve normalized MIS RTO against RTOMaster.rto_cluster -> e.g. {'zyx'}
            # Step 2: check RateMaster.new_rto_list for 'zyx'
            # No fallback: if the normalized value isn't found in RTOMaster at all,
            # the row fails this rule.
            if not current_grid.empty:
                val_rto = mis_row['_mis_rto']
                val_rto_raw = mis_row['_mis_rto_raw']
                if val_rto == 'nan' or not val_rto:
                    rule_mask = current_grid['new_rto_list'] == ''
                    detail = (
                        "RTO is blank on this policy, and no remaining candidate rate row "
                        "allows a blank RTO cluster."
                    )
                else:
                    resolved_rto_names = resolve_rto(val_rto)
                    if not resolved_rto_names:
                        detail = (
                            f"RTO '{val_rto_raw}' (normalized to '{val_rto.upper()}') was not found in any "
                            f"RTOMaster cluster — add it to RTOMaster or check the MIS value."
                        )
                    else:
                        preview = ", ".join(sorted(resolved_rto_names)[:5])
                        detail = (
                            f"RTO '{val_rto_raw}' (normalized to '{val_rto.upper()}') resolved to master "
                            f"group(s) [{preview}], but no remaining candidate rate row lists that group "
                            f"in its RTO cluster."
                        )
                    rule_mask = current_grid['new_rto_list'].apply(
                        lambda g: check_resolved_cluster_match(resolved_rto_names, g)
                    )
                current_grid = narrow_candidates(current_grid, rule_mask, failed_on, 'Policy: rto no', detail)

            # --- RULE 6: Complex Codes (NCB, CPA, ZD) ---
            if not current_grid.empty:
                val_ncb = mis_row['_mis_ncb']
                m_ncb_yes = (current_grid['is_ncb'] == 'YES') & (val_ncb >= 1) & (val_ncb <= 99)
                m_ncb_no = (current_grid['is_ncb'] == 'NO') & (val_ncb == 0)
                m_ncb_na = (current_grid['is_ncb'] == 'NA') | current_grid['is_ncb'].isna()
                rule_mask = m_ncb_yes | m_ncb_no | m_ncb_na
                current_grid = narrow_candidates(
                    current_grid, rule_mask, failed_on, 'Policy: no claim bonus',
                    f"NCB value {val_ncb} doesn't satisfy the YES/NO/NA requirement on any "
                    f"remaining candidate rate row."
                )

            if not current_grid.empty:
                val_cpa = mis_row['_mis_cpa']
                m_cpa_yes = (current_grid['is_cpa'] == 'YES') & (val_cpa >= 1) & (val_cpa <= 1000)
                m_cpa_no = (current_grid['is_cpa'] == 'NO') & (val_cpa == 0)
                m_cpa_na = (current_grid['is_cpa'] == 'NA') | current_grid['is_cpa'].isna()
                rule_mask = m_cpa_yes | m_cpa_no | m_cpa_na
                current_grid = narrow_candidates(
                    current_grid, rule_mask, failed_on, 'Policy: cpa',
                    f"CPA value {val_cpa} doesn't satisfy the YES/NO/NA requirement on any "
                    f"remaining candidate rate row."
                )

            if not current_grid.empty:
                # FIX: original code computed `val_zd in [...]` once as a Python bool
                # OUTSIDE the per-row grid comparison, so it never varied per grid row
                # correctly when combined with current_grid['is_zd'] comparisons. Made
                # this an explicit boolean column-level mask instead.
                val_zd = mis_row['_mis_zd']
                zd_is_yes_value = val_zd in ['1', 'YES', 'Y', 'TRUE']
                zd_is_no_value = val_zd in ['0', 'NO', 'N', 'FALSE']

                m_zd_yes = (current_grid['is_zd'] == 'YES') & zd_is_yes_value
                m_zd_no = (current_grid['is_zd'] == 'NO') & zd_is_no_value
                m_zd_na = (current_grid['is_zd'] == 'NA') | current_grid['is_zd'].isna()
                rule_mask = m_zd_yes | m_zd_no | m_zd_na
                current_grid = narrow_candidates(
                    current_grid, rule_mask, failed_on, 'Policy: nil dep',
                    f"Nil Dep value '{val_zd}' doesn't satisfy the YES/NO/NA requirement on any "
                    f"remaining candidate rate row."
                )

            # --- FINAL RESOLUTION ---
            if data_errors:
                # A data error makes the row FAILED - BAD DATA whatever the rate
                # chain found — it never gets a payout from a rate card matched
                # on source data that can't be right. The chain still ran so the
                # reason is complete: the data errors first, then every
                # rate-matching failure (each introduced as "Also failed on:").
                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': 'FAILED - BAD DATA',
                    'Failure Reason': FAILURE_SEGMENT_SEPARATOR.join(
                        data_errors + [f"{ALSO_FAILED_ON_PREFIX}{label} — {detail}" for label, detail in failed_on]
                    ),
                    'Displaygroupid': None,
                    'Potype': None, 'Porate': None, 'Poflatamount': None,
                    'Pitype': None, 'Pirate': None, 'Piflatamount': None,
                    'Addtnc': None
                })
                continue

            # Any failed rule means NO MATCH, however many candidates are left:
            # narrow_candidates keeps the pre-failure candidates so the later
            # rules still run, which means current_grid is NOT empty here.
            matched_grid = current_grid.iloc[0:0] if failed_on else current_grid

            # Rows sharing a group_id are the SAME rate card exploded across
            # many physical DB rows (e.g. one per RTO in its cluster) - not
            # separate options to disambiguate between. A row with no
            # group_id is its own standalone offer, keyed by its own id.
            # Ambiguity has to be judged on distinct groups, never on the raw
            # row count, or a single rate card that happens to span several
            # physical rows gets wrongly reported as "multiple groups
            # matched" (and the old code's `.dropna()` on group_id could
            # even hide a group-less row from the message entirely, so it
            # would name only one Group ID while still claiming "multiple").
            if matched_grid.empty:
                distinct_keys = []
            else:
                effective_keys = matched_grid['group_id'].where(matched_grid['group_id'].notna(), matched_grid['id'])
                distinct_keys = effective_keys.unique().tolist()

            def _effective_rate(row):
                if row.get('po_type') == 'On OD and TP':
                    return (row.get('po_od_rate') or 0) + (row.get('po_tp_rate') or 0)
                for field in ('po_net_rate', 'po_od_rate', 'po_tp_rate'):
                    val = row.get(field)
                    if val and val > 0:
                        return val
                return 0

            # Several distinct groups matched, but every matched row carries
            # the same payout and payin rate: which group wins can't change
            # the rate, so it isn't an ambiguity worth skipping the row for.
            # Resolve to the lowest group id (deterministic, not DB order) and
            # carry on through the single-group path below. Only the rates
            # (type and value) are compared; a difference in either the payout
            # or payin rate leaves it as MULTIPLE MATCHES.
            tied_group_count = 0
            if len(distinct_keys) > 1 and matched_rows_share_one_payout(matched_grid):
                tied_group_count = len(distinct_keys)
                distinct_keys = [min(distinct_keys)]

            if len(distinct_keys) == 1:
                # EXACTLY ONE DISTINCT RATE GROUP — safe to apply, even when
                # it spans several physical rows. Those are expected to be
                # identical; if they're not, the lowest rate is the safe
                # tie-break (same principle as the Policy Locker dedupe).
                group_rows = matched_grid[effective_keys == distinct_keys[0]]
                best_match = (
                    min((row for _, row in group_rows.iterrows()), key=_effective_rate)
                    if len(group_rows) > 1 else group_rows.iloc[0]
                )
                chosen_group_id = best_match.get('group_id') if pd.notna(best_match.get('group_id')) else best_match.get('id')
                match_reason = 'Matched Successfully'
                if tied_group_count:
                    total_identical_payout_resolved += 1
                    match_reason = (
                        f"Matched Successfully — {tied_group_count} Rate Master groups matched with "
                        f"identical payout and payin rates; used Group {int(chosen_group_id)} (lowest Group ID)."
                    )
                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': '✅ MATCH',
                    'Failure Reason': match_reason,
                    'Displaygroupid': chosen_group_id,
                    'Potype': best_match.get('po_type'),
                    'Porate': consolidated_rate(
                        best_match.get('po_type'), best_match.get('po_od_rate'),
                        best_match.get('po_tp_rate'), best_match.get('po_net_rate')
                    ),
                    'Poflatamount': best_match.get('po_flat_amount'),
                    'Pitype': best_match.get('pi_type'),
                    'Pirate': consolidated_rate(
                        best_match.get('pi_type'), best_match.get('pi_od_rate'),
                        best_match.get('pi_tp_rate'), best_match.get('pi_net_rate')
                    ),
                    'Piflatamount': best_match.get('pi_flat_amount'),
                    'Addtnc': best_match.get('add_tnc'),
                    # Raw OD/TP rate pair, kept alongside the consolidated
                    # Porate/Pirate above specifically for the 'On OD and TP'
                    # split payout/payin calculation further down (see
                    # _split_rate_cols) — Porate/Pirate collapse OD+TP into
                    # one summed percentage, which is only correct when
                    # applied to a single premium base, not to OD/TP's two
                    # distinct bases (Total OD vs TP Premium).
                    '_po_od_rate': best_match.get('po_od_rate'),
                    '_po_tp_rate': best_match.get('po_tp_rate'),
                    '_pi_od_rate': best_match.get('pi_od_rate'),
                    '_pi_tp_rate': best_match.get('pi_tp_rate'),
                })

            elif len(distinct_keys) > 1:
                # MULTIPLE DISTINCT RATE GROUPS — genuine ambiguity between
                # different configured rate cards; do not guess a payout
                # rate, flag it for a human to narrow the Rate Master.
                sorted_keys = sorted(int(k) for k in distinct_keys)
                group_id_str = ', '.join(str(k) for k in sorted_keys[:10])
                if len(sorted_keys) > 10:
                    group_id_str += f' … (+{len(sorted_keys)-10} more)'

                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': '⚠️ MULTIPLE MATCHES',
                    'Failure Reason': (
                        f"Multiple Rate Master groups matched ({len(sorted_keys)} groups, "
                        f"{len(matched_grid)} rows total). Please refine Rate Master so only "
                        f"one group applies. Matching Group IDs: {group_id_str}"
                    ),
                    'Displaygroupid': None,
                    'Potype': None, 'Porate': None, 'Poflatamount': None,
                    'Pitype': None, 'Pirate': None, 'Piflatamount': None,
                    'Addtnc': None
                })

            else:
                # ZERO MATCHES — every rule that eliminated all candidates, each
                # with the specific value/reason for that rule so it's actionable.
                # The first is what a row that stopped at its first failure always
                # reported; the rest follow it (see format_failed_on).
                if failed_on:
                    reason = format_failed_on(failed_on)
                else:
                    reason = "No rates found for criteria"
                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': '❌ NO MATCH',
                    'Failure Reason': reason,
                    'Displaygroupid': None,
                    'Potype': None, 'Porate': None, 'Poflatamount': None,
                    'Pitype': None, 'Pirate': None, 'Piflatamount': None,
                    'Addtnc': None
                })

        # 5. ASSEMBLE FINAL EXPORT
        df_extracted = pd.DataFrame(results)

        # Calculate process counts
        total_processed = len(df_mis)
        total_matched   = len(df_extracted[df_extracted['Mapping Status'] == '✅ MATCH'])
        total_multiple  = len(df_extracted[df_extracted['Mapping Status'] == '⚠️ MULTIPLE MATCHES'])
        total_bad_data  = len(df_extracted[df_extracted['Mapping Status'] == 'FAILED - BAD DATA'])

        # Clean up temporary columns from df_mis before merging
        cols_to_drop = [c for c in df_mis.columns if c.startswith('_mis_')]
        df_mis = df_mis.drop(columns=cols_to_drop)

        if not df_extracted.empty:
            df_final = df_mis.merge(df_extracted, on='Original_Row_ID', how='left')
        else:
            df_final = df_mis.copy()
            df_final['Mapping Status'] = "❌ NO MATCH"
            df_final['Failure Reason'] = "Failed on: System Error"

        df_final['Mapping Status'] = df_final['Mapping Status'].fillna("❌ NO MATCH")

        # Ensure output columns exist
        payout_cols = ['Displaygroupid', 'Potype', 'Porate', 'Poflatamount', 'Pitype', 'Pirate', 'Piflatamount', 'Addtnc']
        for p_col in payout_cols:
            if p_col not in df_final.columns:
                df_final[p_col] = None

        # Internal-only (never added to generated_cols, so never reaches the
        # exported file) — the raw OD/TP rate pair set alongside Porate/Pirate
        # above, for the 'On OD and TP' split Pay-out/Pay-in Amt calculation.
        split_rate_cols = ['_po_od_rate', '_po_tp_rate', '_pi_od_rate', '_pi_tp_rate']
        for s_col in split_rate_cols:
            if s_col not in df_final.columns:
                df_final[s_col] = None

        df_final.loc[df_final['Mapping Status'] != "✅ MATCH", payout_cols + split_rate_cols] = None

        # cp premium# is computed from raw MIS fields only (see pre-compute
        # section above) and applies to every row regardless of Mapping
        # Status, so it's listed separately from payout_cols rather than
        # blanked out alongside them.
        if 'cp premium#' not in df_final.columns:
            df_final['cp premium#'] = None

        # --- CP PREMIUM# OVERWRITE — Motor rows, keyed by Pitype ---
        # For matched Motor rows, cp premium# must reflect the specific
        # premium component implied by Pitype instead of the general-purpose
        # calculation above. This runs on df_final (post-match, post-blank)
        # rather than reusing the pre-compute section's variables, both
        # because Pitype only exists after matching and so the already-
        # blanked-to-None Pitype on non-MATCH rows naturally excludes them
        # here with no extra Mapping Status check needed, and because
        # re-deriving straight from df_final's own columns avoids relying on
        # index alignment surviving the df_mis/df_extracted merge above.
        # Non-motor rows, and motor rows with any other Pitype (e.g. 'On OD
        # and TP'), fall through untouched — Condition 4's fallback is simply
        # "don't touch cp premium#" for every row none of these masks hit.
        _pi_ov_product = safe_get_col(df_final, 'Product').astype(str).str.strip().str.lower()
        _pi_ov_is_motor = _pi_ov_product == 'motor'

        _pi_ov_total_od = clean_cp_numeric(safe_get_col(df_final, 'Policy: total od'))
        _pi_ov_tp_premium = clean_cp_numeric(safe_get_col(df_final, 'Policy: tp premium'))
        _pi_ov_gwp_net_premium = clean_cp_numeric(safe_get_col(df_final, 'Policy: gwp net premium'))

        _pi_ov_on_od_mask = _pi_ov_is_motor & (df_final['Pitype'] == 'On OD')
        _pi_ov_on_tp_mask = _pi_ov_is_motor & (df_final['Pitype'] == 'On TP')
        _pi_ov_on_net_mask = _pi_ov_is_motor & (df_final['Pitype'] == 'On Net')

        df_final.loc[_pi_ov_on_od_mask, 'cp premium#'] = _pi_ov_total_od
        df_final.loc[_pi_ov_on_tp_mask, 'cp premium#'] = _pi_ov_tp_premium
        df_final.loc[_pi_ov_on_net_mask, 'cp premium#'] = _pi_ov_gwp_net_premium

        # Pay-in/Pay-out/Margin — derived straight from Porate/Pirate/cp
        # premium#, computed after those have already been blanked to None
        # on non-MATCH rows, so a blank Porate/Pirate naturally makes these
        # blank too (arithmetic on None propagates to NaN) rather than
        # needing separate blanking logic here.
        reconciliation_cols = ['Pay-in Amt', 'Pay-out Amt', 'Margin Rate', 'Margin Amt']
        df_final['Pay-in Amt'] = (df_final['Pirate'] / 100) * df_final['cp premium#']
        df_final['Pay-out Amt'] = (df_final['Porate'] / 100) * df_final['cp premium#']

        # --- SPLIT OD/TP OVERRIDE — Potype/Pitype == 'On OD and TP' ---
        # Porate/Pirate collapse the OD+TP rate pair into one summed number
        # (consolidated_rate) and the general path above multiplies that one
        # number against one premium base (cp premium#). That's wrong for
        # this type: OD and TP are genuinely two separate calculations, each
        # against its own premium base — Total OD for the OD leg, TP Premium
        # for the TP leg — not one combined rate times one base. Potype and
        # Pitype are independent columns (a rate group's payout and payin
        # sides can carry different types), so the override is applied to
        # Pay-out Amt and Pay-in Amt independently, each keyed off its own
        # type column and its own OD/TP rate pair (_po_od_rate/_po_tp_rate
        # and _pi_od_rate/_pi_tp_rate, set alongside Porate/Pirate above).
        # Missing OD/TP rate defaults to 0, mirroring consolidated_rate's own
        # convention for this same rate_type.
        _po_split_mask = df_final['Potype'] == 'On OD and TP'
        _pi_split_mask = df_final['Pitype'] == 'On OD and TP'

        _split_po_amt = (
            _pi_ov_total_od * (df_final['_po_od_rate'].fillna(0) / 100)
            + _pi_ov_tp_premium * (df_final['_po_tp_rate'].fillna(0) / 100)
        )
        _split_pi_amt = (
            _pi_ov_total_od * (df_final['_pi_od_rate'].fillna(0) / 100)
            + _pi_ov_tp_premium * (df_final['_pi_tp_rate'].fillna(0) / 100)
        )
        df_final.loc[_po_split_mask, 'Pay-out Amt'] = _split_po_amt[_po_split_mask]
        df_final.loc[_pi_split_mask, 'Pay-in Amt'] = _split_pi_amt[_pi_split_mask]

        df_final['Margin Rate'] = df_final['Pirate'] - df_final['Porate']
        df_final['Margin Amt'] = df_final['Pay-in Amt'] - df_final['Pay-out Amt']

        # --- DISPLAY FORMAT — Porate/Pirate as 'OD+TP' for On OD and TP rows ---
        # Runs last, after Margin Rate/Margin Amt above have already used the
        # real numeric OD+TP sum — this only reformats the exported Porate/
        # Pirate cell so a reviewer sees the two rates that were combined
        # (e.g. '13+8') instead of just their opaque sum ('21'). Porate/Pirate
        # are float64 columns (every non-split row is a plain number) — cast
        # to object first, or pandas raises ("Invalid value ... for dtype
        # 'float64'") instead of upcasting when a string is set via .loc.
        df_final['Porate'] = df_final['Porate'].astype(object)
        df_final['Pirate'] = df_final['Pirate'].astype(object)

        df_final.loc[_po_split_mask, 'Porate'] = df_final.loc[_po_split_mask].apply(
            lambda r: format_od_tp_rate(r['_po_od_rate'], r['_po_tp_rate']), axis=1
        )
        df_final.loc[_pi_split_mask, 'Pirate'] = df_final.loc[_pi_split_mask].apply(
            lambda r: format_od_tp_rate(r['_pi_od_rate'], r['_pi_tp_rate']), axis=1
        )

        generated_cols = ['Mapping Status', 'Failure Reason'] + payout_cols + ['cp premium#'] + reconciliation_cols
        original_cols = [c for c in mis_cols]
        df_final = df_final[generated_cols + original_cols]

        # 6. SAVE
        output = io.BytesIO()
        if file_ext == 'csv':
            df_final.to_csv(output, index=False)
        else:
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_final.to_excel(writer, index=False)

        new_filename = f"mapped_{mis_obj.uploaded_file.name.split('/')[-1]}"
        mis_obj.processed_file.save(new_filename, ContentFile(output.getvalue()))
        mis_obj.status = 'COMPLETED'
        mis_obj.processed_at = timezone.now()

        identical_payout_note = (
            f"{total_identical_payout_resolved} of them matched several Rate Master groups/rows with "
            f"identical payout and payin rates and used the lowest Group ID. "
            if total_identical_payout_resolved else ""
        )
        mis_obj.error_message = (
            f"Processed {total_processed} rows successfully. "
            f"Mapped {total_matched} rates. "
            f"{identical_payout_note}"
            f"{total_multiple} rows skipped — multiple Rate Master groups matched "
            f"(refine Rate Master to get a single match). "
            f"{total_bad_data} rows flagged FAILED - BAD DATA (invalid source values — see Failure Reason)."
        )

        # Coverage-gap breakdown is a reporting nicety, not core output — a
        # bug here must never turn an otherwise-successful run into FAILED
        # and discard a perfectly good processed_file.
        try:
            mis_obj.coverage_gaps = build_coverage_gap_summary(df_final)
        except Exception:
            logger.exception("Coverage gap summary failed to build (non-fatal) for MISFile %s", mis_file_id)
            mis_obj.coverage_gaps = None

        # Persist failed rows straight from the in-memory df_final — this is
        # what lets the Rate Master Health dashboard be a normal indexed DB
        # query instead of re-opening and re-parsing this file's Excel/CSV
        # output on every page view. Non-fatal: a bug here must never turn an
        # otherwise-successful run into FAILED and discard a good processed_file
        # (sync_failed_rows_for_file can always backfill this later).
        try:
            failed_rows = _extract_failed_rows_from_df(df_final)
            _bulk_save_failed_rows(mis_obj, failed_rows)
            mis_obj.health_synced_at = timezone.now()
        except Exception:
            logger.exception("Failed to persist MIS failure rows (non-fatal) for MISFile %s", mis_file_id)

        mis_obj.save()

    except Exception as e:
        # FIX: was `except BaseException`, which also catches KeyboardInterrupt /
        # SystemExit and can block graceful container shutdown. Narrowed to Exception.
        logger.exception("MIS mapping failed for MISFile %s", mis_file_id)

        try:
            connection.close()
            mis_obj = MISFile.objects.get(id=mis_file_id)
            mis_obj.status = 'FAILED'
            mis_obj.error_message = str(e)[:1000]
            mis_obj.processed_at = timezone.now()
            mis_obj.save()
        except Exception as recovery_err:
            logger.critical("Could not write the FAILED state for MISFile %s to the DB: %s", mis_file_id, recovery_err, exc_info=True)

    finally:
        connection.close()