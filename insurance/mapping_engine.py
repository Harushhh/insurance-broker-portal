import pandas as pd
import io
import re
import traceback
from collections import Counter
from rapidfuzz import fuzz, process as rf_process
from django.core.files.base import ContentFile
from django.utils import timezone
from django.db import connection
from .models import MISFile, RateMaster, RTOMaster, MakeModelMaster


# ── Tunable thresholds ───────────────────────────────────────────────────────
INSURANCE_FUZZY_THRESHOLD = 55   # lowered from 60: handles typos in company names
                                  # (e.g. "Limted" instead of "Limited" in Liberty)
CATEGORICAL_FUZZY_THRESHOLD = 75
MAKE_MODEL_MIN_WORD_MATCH = 2     # Rule 5a: min words the MIS "make + model" string
                                  # must share with a MakeModelMaster cluster entry

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


def strict_match_in_cluster(search_term, cluster_string):
    """
    Identical to the function of the same name in views.py — kept in sync
    deliberately so RTO/Make matching behaves the same way here as it does
    in the dashboard, motor payout rates, and policy lock checker views.
    Word-boundary regex match against comma-separated cluster items, so
    'MH09' won't accidentally match inside 'MH090' or similar.
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
    """
    master_rows = list(master_qs.values_list(name_field, cluster_field))
    parsed = [(name, cluster) for name, cluster in master_rows if cluster]

    resolution_cache = {}

    def resolve(search_term):
        if not search_term:
            return set()
        key = str(search_term).strip().upper()
        if key in resolution_cache:
            return resolution_cache[key]

        matched_names = set()
        for name, cluster in parsed:
            if fuzzy_match_make_model(key, cluster, min_shared_words):
                matched_names.add(str(name).strip().lower())

        resolution_cache[key] = matched_names
        return matched_names

    return resolve


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


_QUOTED_VALUE_PATTERN = re.compile(r"'([^']*)'")
_FAILED_ON_PATTERN = re.compile(r"^Failed on:\s*([^—]+)—\s*(.*)$")
_GROUP_IDS_PATTERN = re.compile(r"Matching Group IDs:\s*(.*)$")


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
    to the match/no-match decisions themselves.
    """
    no_match_counts = Counter()
    no_match_reasons = df_final.loc[df_final['Mapping Status'] == '❌ NO MATCH', 'Failure Reason']
    for reason in no_match_reasons:
        reason = str(reason)
        m = _FAILED_ON_PATTERN.match(reason)
        if not m:
            no_match_counts[('Other', reason[:60].strip())] += 1
            continue
        rule_label = m.group(1).strip()
        detail = m.group(2).strip()
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


def process_mis_mapping(mis_file_id):
    # Always forcefully wipe the thread's DB connection state to prevent inheritance locks
    connection.close()

    try:
        mis_obj = MISFile.objects.get(id=mis_file_id)
        mis_obj.status = 'PROCESSING'
        mis_obj.save(update_fields=['status'])
    except Exception as init_err:
        print(f"Failed to initialize processing state: {init_err}")
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

        age_raw = safe_get_col(df_mis, 'Policy: vehage').astype(str).str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_age'] = pd.to_numeric(age_raw, errors='coerce')

        tariff_raw = safe_get_col(df_mis, 'Policy: tariff rate').astype(str).str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_tariff'] = pd.to_numeric(tariff_raw, errors='coerce')

        df_mis['_mis_date'] = pd.to_datetime(safe_get_col(df_mis, 'Policy: inception date'), errors='coerce', dayfirst=True)

        df_mis['_mis_ncb'] = pd.to_numeric(safe_get_col(df_mis, 'Policy: no claim bonus'), errors='coerce')
        df_mis['_mis_cpa'] = pd.to_numeric(safe_get_col(df_mis, 'Policy: cpa'), errors='coerce')
        df_mis['_mis_zd'] = safe_get_col(df_mis, 'Policy: nil dep').astype(str).str.strip().str.upper()

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
        fuzzy_ins_map = get_fuzzy_dict(
            df_mis['_mis_ins'].unique(),
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

        results = []

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

            # --- BAD DATA GUARD: combined CC/GVW value ---
            # Flagged during pre-compute (is_combined_cc_gvw). Never enters the
            # elimination rules — a fabricated CC number could otherwise cause
            # a false NO MATCH, or worse, a false MATCH against the wrong rate.
            if mis_row['_mis_cc_bad_data']:
                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': 'FAILED - BAD DATA',
                    'Failure Reason': (
                        f"{mis_row['_mis_cc_source_label']} value '{mis_row['_mis_cc_raw']}' looks like a "
                        f"combined CC/GVW figure (two numbers separated by '/'). Not auto-parsed — "
                        f"correct the source MIS data and re-run mapping for this row."
                    ),
                    'Displaygroupid': None,
                    'Potype': None, 'Porate': None, 'Poflatamount': None,
                    'Pitype': None, 'Pirate': None, 'Piflatamount': None,
                    'Addtnc': None
                })
                continue

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

            # --- RULE 2: Categorical Matches (RapidFuzz-backed) ---
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
                    rule_mask = current_grid[grid_col].apply(lambda g: check_categorical_match(val, g))
                    current_grid = current_grid[rule_mask]
                    if current_grid.empty:
                        failed_on.append((
                            label,
                            f"Value '{val}' did not match any remaining candidate Rate Master row's "
                            f"{grid_col.replace('_', ' ')} (after the previous rules narrowed the field)."
                        ))

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
                current_grid = current_grid[rule_mask]
                if current_grid.empty:
                    failed_on.append((
                        'Policy: vehicle class',
                        f"Vehicle class '{val_class}' has no exact match among remaining candidate "
                        f"rate rows, and none of them has an NA-wildcard class."
                    ))

            # --- RULE 3: Numeric Ranges ---
            range_rules = [
                ('_mis_cc', 'cc_min', 'cc_max', mis_row['_mis_cc_source_label']),
                ('_mis_sc', 'sc_min', 'sc_max', 'Policy: seating capacity'),
                ('_mis_age', 'vehicle_age_min', 'vehicle_age_max', 'Policy: vehage'),
                ('_mis_tariff', 'tariff_min', 'tariff_max', 'Policy: tariff rate'),
            ]
            for mis_col, min_col, max_col, label in range_rules:
                if not current_grid.empty:
                    val = mis_row[mis_col]
                    if pd.isna(val):
                        rule_mask = current_grid[min_col].isna() & current_grid[max_col].isna()
                        detail = (
                            f"{label} is blank/unparseable on this policy, and no remaining "
                            f"candidate rate row has an open (blank) range."
                        )
                    else:
                        min_cond = current_grid[min_col].isna() | (current_grid[min_col] <= val)
                        max_cond = current_grid[max_col].isna() | (current_grid[max_col] >= val)
                        rule_mask = min_cond & max_cond
                        detail = (
                            f"{label} value {val} falls outside the range configured on every "
                            f"remaining candidate rate row."
                        )
                    current_grid = current_grid[rule_mask]
                    if current_grid.empty:
                        failed_on.append((label, detail))

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
                current_grid = current_grid[rule_mask]
                if current_grid.empty:
                    failed_on.append(('Policy: inception date', detail))

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
                        detail = (
                            f"Vehicle make/model '{val_make_model}' did not share at least "
                            f"{MAKE_MODEL_MIN_WORD_MATCH} words with any MakeModelMaster cluster "
                            f"entry — add it to MakeModelMaster or check the MIS value."
                        )
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

                current_grid = current_grid[rule_mask]
                if current_grid.empty:
                    failed_on.append(('Policy: vehicle make / model', detail))

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
                current_grid = current_grid[rule_mask]
                if current_grid.empty:
                    failed_on.append(('Policy: rto no', detail))

            # --- RULE 6: Complex Codes (NCB, CPA, ZD) ---
            if not current_grid.empty:
                val_ncb = mis_row['_mis_ncb']
                m_ncb_yes = (current_grid['is_ncb'] == 'YES') & (val_ncb >= 1) & (val_ncb <= 99)
                m_ncb_no = (current_grid['is_ncb'] == 'NO') & (val_ncb == 0)
                m_ncb_na = (current_grid['is_ncb'] == 'NA') | current_grid['is_ncb'].isna()
                rule_mask = m_ncb_yes | m_ncb_no | m_ncb_na
                current_grid = current_grid[rule_mask]
                if current_grid.empty:
                    failed_on.append((
                        'Policy: no claim bonus',
                        f"NCB value {val_ncb} doesn't satisfy the YES/NO/NA requirement on any "
                        f"remaining candidate rate row."
                    ))

            if not current_grid.empty:
                val_cpa = mis_row['_mis_cpa']
                m_cpa_yes = (current_grid['is_cpa'] == 'YES') & (val_cpa >= 1) & (val_cpa <= 1000)
                m_cpa_no = (current_grid['is_cpa'] == 'NO') & (val_cpa == 0)
                m_cpa_na = (current_grid['is_cpa'] == 'NA') | current_grid['is_cpa'].isna()
                rule_mask = m_cpa_yes | m_cpa_no | m_cpa_na
                current_grid = current_grid[rule_mask]
                if current_grid.empty:
                    failed_on.append((
                        'Policy: cpa',
                        f"CPA value {val_cpa} doesn't satisfy the YES/NO/NA requirement on any "
                        f"remaining candidate rate row."
                    ))

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
                current_grid = current_grid[rule_mask]
                if current_grid.empty:
                    failed_on.append((
                        'Policy: nil dep',
                        f"Nil Dep value '{val_zd}' doesn't satisfy the YES/NO/NA requirement on any "
                        f"remaining candidate rate row."
                    ))

            # --- FINAL RESOLUTION ---
            matched_grid = current_grid

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
                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': '✅ MATCH',
                    'Failure Reason': 'Matched Successfully',
                    'Displaygroupid': best_match.get('group_id') if pd.notna(best_match.get('group_id')) else best_match.get('id'),
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
                    'Addtnc': best_match.get('add_tnc')
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
                # ZERO MATCHES — record the first rule that eliminated candidates,
                # with the specific value/reason for that rule so it's actionable.
                if failed_on:
                    first_label, first_detail = failed_on[0]
                    reason = f"Failed on: {first_label} — {first_detail}"
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

        df_final.loc[df_final['Mapping Status'] != "✅ MATCH", payout_cols] = None

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
        df_final['Margin Rate'] = df_final['Pirate'] - df_final['Porate']
        df_final['Margin Amt'] = df_final['Pay-in Amt'] - df_final['Pay-out Amt']

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

        mis_obj.error_message = (
            f"Processed {total_processed} rows successfully. "
            f"Mapped {total_matched} rates. "
            f"{total_multiple} rows skipped — multiple Rate Master groups matched "
            f"(refine Rate Master to get a single match). "
            f"{total_bad_data} rows flagged FAILED - BAD DATA (malformed source values — see Failure Reason)."
        )

        # Coverage-gap breakdown is a reporting nicety, not core output — a
        # bug here must never turn an otherwise-successful run into FAILED
        # and discard a perfectly good processed_file.
        try:
            mis_obj.coverage_gaps = build_coverage_gap_summary(df_final)
        except Exception:
            print("Coverage gap summary failed to build (non-fatal):")
            print(traceback.format_exc())
            mis_obj.coverage_gaps = None

        mis_obj.save()

    except Exception as e:
        # FIX: was `except BaseException`, which also catches KeyboardInterrupt /
        # SystemExit and can block graceful container shutdown. Narrowed to Exception.
        error_trace = traceback.format_exc()
        print(f"\n❌ MIS Mapping Error: {str(e)}")
        print(error_trace)

        try:
            connection.close()
            mis_obj = MISFile.objects.get(id=mis_file_id)
            mis_obj.status = 'FAILED'
            mis_obj.error_message = str(e)[:1000]
            mis_obj.processed_at = timezone.now()
            mis_obj.save()
        except Exception as recovery_err:
            print(f"CRITICAL FAULT: Could not write Failure state to DB: {recovery_err}")

    finally:
        connection.close()