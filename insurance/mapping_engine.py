import pandas as pd
import io
import re
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

    search_words = set(str(search_term).strip().upper().split())
    if len(search_words) < min_shared_words:
        return False

    items = [x.strip().upper() for x in str(cluster_string).split(",") if x.strip()]
    for item in items:
        item_words = set(item.split())
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
        return raw_value
    s = str(raw_value).strip()
    if not s or s.lower() == 'nan':
        return s
    s = re.sub(r'[\s\-]', '', s)
    return s[:4]


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

        # 2. FETCH RATE MASTER DATA
        qs = RateMaster.objects.filter(status="ACTIVE", is_deleted="NO").select_related(
            'product', 'sub_product', 'fuel_type', 'make_model_class', 'is_ncb', 'is_cpa', 'is_zd'
        )

        grid_data = []
        for r in qs:
            grid_data.append({
                'id': r.id,
                'group_id': r.group_id,
                'po_type': r.po_type,
                'po_od_rate': r.po_od_rate,
                'po_tp_rate': r.po_tp_rate,
                'po_net_rate': r.po_net_rate,
                'po_flat_amount': r.po_flat_amount,
                'add_tnc': r.add_tnc,

                # Relational strings
                'insurance_company': str(r.insurance_company).strip().lower() if r.insurance_company else '',
                'product': str(r.product.name).strip().lower() if r.product else '',
                'sub_product': str(r.sub_product.name).strip().lower() if r.sub_product else '',
                'fuel_type': str(r.fuel_type.name).strip().lower() if r.fuel_type else '',
                'make_model_class': str(r.make_model_class.name).strip().lower() if r.make_model_class else '',

                # Bools/Codes
                'is_ncb': str(r.is_ncb.code).strip().upper() if r.is_ncb else 'NA',
                'is_cpa': str(r.is_cpa.code).strip().upper() if r.is_cpa else 'NA',
                'is_zd': str(r.is_zd.code).strip().upper() if r.is_zd else 'NA',

                # Clusters
                'new_vehicle_makes': str(r.new_vehicle_makes).strip().lower() if r.new_vehicle_makes else '',
                'new_rto_list': str(r.new_rto_list).strip().lower() if r.new_rto_list else '',

                # Numerics & Dates
                'cc_min': r.cc_min, 'cc_max': r.cc_max,
                'sc_min': r.sc_min, 'sc_max': r.sc_max,
                'vehicle_age_min': r.vehicle_age_min, 'vehicle_age_max': r.vehicle_age_max,
                'from_date': r.from_date, 'to_date': r.to_date,
            })

        df_grid = pd.DataFrame(grid_data)
        if df_grid.empty:
            raise ValueError("RateMaster has no active records configured.")

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
        for col in ['cc_min', 'cc_max', 'sc_min', 'sc_max', 'vehicle_age_min', 'vehicle_age_max']:
            df_grid[col] = pd.to_numeric(df_grid[col], errors='coerce')
        df_grid['from_date'] = pd.to_datetime(df_grid['from_date'], errors='coerce')
        df_grid['to_date'] = pd.to_datetime(df_grid['to_date'], errors='coerce')

        # 3. PRE-COMPUTE MIS DATA
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
        _cc_raw_series = safe_get_col(df_mis, 'Policy: cc cubic capacity').astype(str).str.strip()
        df_mis['_mis_cc_raw'] = _cc_raw_series
        df_mis['_mis_cc_bad_data'] = _cc_raw_series.apply(is_combined_cc_gvw)
        cc_raw = _cc_raw_series.str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_cc'] = pd.to_numeric(cc_raw, errors='coerce')

        sc_raw = safe_get_col(df_mis, 'Policy: seating capacity').astype(str).str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_sc'] = pd.to_numeric(sc_raw, errors='coerce')

        age_raw = safe_get_col(df_mis, 'Policy: vehage').astype(str).str.replace(r'[^0-9.]', '', regex=True)
        df_mis['_mis_age'] = pd.to_numeric(age_raw, errors='coerce')

        df_mis['_mis_date'] = pd.to_datetime(safe_get_col(df_mis, 'Policy: inception date'), errors='coerce', dayfirst=True)

        df_mis['_mis_ncb'] = pd.to_numeric(safe_get_col(df_mis, 'Policy: no claim bonus'), errors='coerce')
        df_mis['_mis_cpa'] = pd.to_numeric(safe_get_col(df_mis, 'Policy: cpa'), errors='coerce')
        df_mis['_mis_zd'] = safe_get_col(df_mis, 'Policy: nil dep').astype(str).str.strip().str.upper()

        # Insurance company fuzzy mapping — now powered by RapidFuzz (5-100x faster)
        fuzzy_ins_map = get_fuzzy_dict(
            df_mis['_mis_ins'].unique(),
            df_grid['insurance_company'].unique(),
            threshold=INSURANCE_FUZZY_THRESHOLD
        )
        df_mis['_mis_ins_mapped'] = df_mis['_mis_ins'].map(fuzzy_ins_map)

        results = []

        # 4. ROW-BY-ROW PROCESSING
        for idx, mis_row in df_mis.iterrows():

            # --- BAD DATA GUARD: combined CC/GVW value ---
            # Flagged during pre-compute (is_combined_cc_gvw). Never enters the
            # elimination rules — a fabricated CC number could otherwise cause
            # a false NO MATCH, or worse, a false MATCH against the wrong rate.
            if mis_row['_mis_cc_bad_data']:
                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': 'FAILED - BAD DATA',
                    'Failure Reason': (
                        f"Policy: cc cubic capacity value '{mis_row['_mis_cc_raw']}' looks like a "
                        f"combined CC/GVW figure (two numbers separated by '/'). Not auto-parsed — "
                        f"correct the source MIS data and re-run mapping for this row."
                    ),
                    'Displaygroupid': None,
                    'Potype': None, 'Poodrate': None, 'Potprate': None,
                    'Ponetrate': None, 'Poflatamount': None, 'Addtnc': None
                })
                continue

            valid_mask = pd.Series([True] * len(df_grid), index=df_grid.index)
            failed_on = []

            # --- RULE 1: Insurance Company ---
            val_ins_raw = mis_row['_mis_ins']
            val_ins = mis_row['_mis_ins_mapped']
            if not val_ins:
                failed_on.append((
                    'Policy: insurance company',
                    f"Insurer '{val_ins_raw}' did not fuzzy-match any active Rate Master insurer "
                    f"(threshold={INSURANCE_FUZZY_THRESHOLD}). Either no active rate is configured "
                    f"for this insurer, or the name on file differs too much to match."
                ))
                valid_mask = valid_mask & False
            else:
                rule_mask = (df_grid['insurance_company'] == val_ins)
                valid_mask = valid_mask & rule_mask
                if not valid_mask.any():
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
                if valid_mask.any():
                    val = mis_row[mis_col]
                    rule_mask = df_grid[grid_col].apply(lambda g: check_categorical_match(val, g))
                    valid_mask = valid_mask & rule_mask
                    if not valid_mask.any():
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
            if valid_mask.any():
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

                rule_mask = df_grid['make_model_class'].apply(match_vehicle_class)
                valid_mask = valid_mask & rule_mask
                if not valid_mask.any():
                    failed_on.append((
                        'Policy: vehicle class',
                        f"Vehicle class '{val_class}' has no exact match among remaining candidate "
                        f"rate rows, and none of them has an NA-wildcard class."
                    ))

            # --- RULE 3: Numeric Ranges ---
            range_rules = [
                ('_mis_cc', 'cc_min', 'cc_max', 'Policy: cc cubic capacity'),
                ('_mis_sc', 'sc_min', 'sc_max', 'Policy: seating capacity'),
                ('_mis_age', 'vehicle_age_min', 'vehicle_age_max', 'Policy: vehage')
            ]
            for mis_col, min_col, max_col, label in range_rules:
                if valid_mask.any():
                    val = mis_row[mis_col]
                    if pd.isna(val):
                        rule_mask = df_grid[min_col].isna() & df_grid[max_col].isna()
                        detail = (
                            f"{label} is blank/unparseable on this policy, and no remaining "
                            f"candidate rate row has an open (blank) range."
                        )
                    else:
                        min_cond = df_grid[min_col].isna() | (df_grid[min_col] <= val)
                        max_cond = df_grid[max_col].isna() | (df_grid[max_col] >= val)
                        rule_mask = min_cond & max_cond
                        detail = (
                            f"{label} value {val} falls outside the range configured on every "
                            f"remaining candidate rate row."
                        )
                    valid_mask = valid_mask & rule_mask
                    if not valid_mask.any():
                        failed_on.append((label, detail))

            # --- RULE 4: Dates ---
            if valid_mask.any():
                val = mis_row['_mis_date']
                if pd.isna(val):
                    rule_mask = df_grid['from_date'].isna() & df_grid['to_date'].isna()
                    detail = (
                        "Inception date is blank/unparseable on this policy, and no remaining "
                        "candidate rate row has an open (blank) date window."
                    )
                else:
                    min_cond = df_grid['from_date'].isna() | (df_grid['from_date'] <= val)
                    max_cond = df_grid['to_date'].isna() | (df_grid['to_date'] >= val)
                    rule_mask = min_cond & max_cond
                    detail = (
                        f"Inception date {val.date()} falls outside the active date window "
                        f"on every remaining candidate rate row."
                    )
                valid_mask = valid_mask & rule_mask
                if not valid_mask.any():
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
            if valid_mask.any():
                val_make_model = mis_row['_mis_make_model']
                if val_make_model == 'nan' or not val_make_model:
                    rule_mask = df_grid['new_vehicle_makes'] == ''
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
                    rule_mask = df_grid['new_vehicle_makes'].apply(
                        lambda g: check_resolved_cluster_match(resolved_make_names, g)
                    )

                valid_mask = valid_mask & rule_mask
                if not valid_mask.any():
                    failed_on.append(('Policy: vehicle make / model', detail))

            # --- RULE 5b: RTO — two-step master lookup ---
            # _mis_rto is already normalized (normalize_rto_code): spaces/hyphens
            # stripped, truncated to 4 chars, so full registration numbers like
            # "MH05FG9876" resolve as "MH05" instead of failing outright.
            # Step 1: resolve normalized MIS RTO against RTOMaster.rto_cluster -> e.g. {'zyx'}
            # Step 2: check RateMaster.new_rto_list for 'zyx'
            # No fallback: if the normalized value isn't found in RTOMaster at all,
            # the row fails this rule.
            if valid_mask.any():
                val_rto = mis_row['_mis_rto']
                val_rto_raw = mis_row['_mis_rto_raw']
                if val_rto == 'nan' or not val_rto:
                    rule_mask = df_grid['new_rto_list'] == ''
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
                    rule_mask = df_grid['new_rto_list'].apply(
                        lambda g: check_resolved_cluster_match(resolved_rto_names, g)
                    )
                valid_mask = valid_mask & rule_mask
                if not valid_mask.any():
                    failed_on.append(('Policy: rto no', detail))

            # --- RULE 6: Complex Codes (NCB, CPA, ZD) ---
            if valid_mask.any():
                val_ncb = mis_row['_mis_ncb']
                m_ncb_yes = (df_grid['is_ncb'] == 'YES') & (val_ncb >= 1) & (val_ncb <= 99)
                m_ncb_no = (df_grid['is_ncb'] == 'NO') & (val_ncb == 0)
                m_ncb_na = (df_grid['is_ncb'] == 'NA') | df_grid['is_ncb'].isna()
                rule_mask = m_ncb_yes | m_ncb_no | m_ncb_na
                valid_mask = valid_mask & rule_mask
                if not valid_mask.any():
                    failed_on.append((
                        'Policy: no claim bonus',
                        f"NCB value {val_ncb} doesn't satisfy the YES/NO/NA requirement on any "
                        f"remaining candidate rate row."
                    ))

            if valid_mask.any():
                val_cpa = mis_row['_mis_cpa']
                m_cpa_yes = (df_grid['is_cpa'] == 'YES') & (val_cpa >= 1) & (val_cpa <= 1000)
                m_cpa_no = (df_grid['is_cpa'] == 'NO') & (val_cpa == 0)
                m_cpa_na = (df_grid['is_cpa'] == 'NA') | df_grid['is_cpa'].isna()
                rule_mask = m_cpa_yes | m_cpa_no | m_cpa_na
                valid_mask = valid_mask & rule_mask
                if not valid_mask.any():
                    failed_on.append((
                        'Policy: cpa',
                        f"CPA value {val_cpa} doesn't satisfy the YES/NO/NA requirement on any "
                        f"remaining candidate rate row."
                    ))

            if valid_mask.any():
                # FIX: original code computed `val_zd in [...]` once as a Python bool
                # OUTSIDE the per-row grid comparison, so it never varied per grid row
                # correctly when combined with df_grid['is_zd'] comparisons. Made this
                # an explicit boolean column-level mask instead.
                val_zd = mis_row['_mis_zd']
                zd_is_yes_value = val_zd in ['1', 'YES', 'Y', 'TRUE']
                zd_is_no_value = val_zd in ['0', 'NO', 'N', 'FALSE']

                m_zd_yes = (df_grid['is_zd'] == 'YES') & zd_is_yes_value
                m_zd_no = (df_grid['is_zd'] == 'NO') & zd_is_no_value
                m_zd_na = (df_grid['is_zd'] == 'NA') | df_grid['is_zd'].isna()
                rule_mask = m_zd_yes | m_zd_no | m_zd_na
                valid_mask = valid_mask & rule_mask
                if not valid_mask.any():
                    failed_on.append((
                        'Policy: nil dep',
                        f"Nil Dep value '{val_zd}' doesn't satisfy the YES/NO/NA requirement on any "
                        f"remaining candidate rate row."
                    ))

            # --- FINAL RESOLUTION ---
            matched_grid = df_grid[valid_mask]
            matched_count = len(matched_grid)

            if matched_count == 1:
                # EXACT SINGLE MATCH — safe to apply rate
                best_match = matched_grid.iloc[0]
                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': '✅ MATCH',
                    'Failure Reason': 'Matched Successfully',
                    'Displaygroupid': best_match.get('group_id') if pd.notna(best_match.get('group_id')) else best_match.get('id'),
                    'Potype': best_match.get('po_type'),
                    'Poodrate': best_match.get('po_od_rate'),
                    'Potprate': best_match.get('po_tp_rate'),
                    'Ponetrate': best_match.get('po_net_rate'),
                    'Poflatamount': best_match.get('po_flat_amount'),
                    'Addtnc': best_match.get('add_tnc')
                })

            elif matched_count > 1:
                # MULTIPLE MATCHES — do not apply any rate.
                # The team must narrow the Rate Master so only one group
                # matches this combination of fields.
                matched_group_ids = sorted(
                    matched_grid['group_id'].dropna().astype(int).unique().tolist()
                )
                group_id_str = ', '.join(str(g) for g in matched_group_ids[:10])
                if len(matched_group_ids) > 10:
                    group_id_str += f' … (+{len(matched_group_ids)-10} more)'

                results.append({
                    'Original_Row_ID': mis_row.get('Original_Row_ID', idx),
                    'Mapping Status': '⚠️ MULTIPLE MATCHES',
                    'Failure Reason': (
                        f"Multiple Rate Master groups matched ({matched_count} rows). "
                        f"Please refine Rate Master so only one group applies. "
                        f"Matching Group IDs: {group_id_str}"
                    ),
                    'Displaygroupid': None,
                    'Potype': None, 'Poodrate': None, 'Potprate': None,
                    'Ponetrate': None, 'Poflatamount': None, 'Addtnc': None
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
                    'Potype': None, 'Poodrate': None, 'Potprate': None,
                    'Ponetrate': None, 'Poflatamount': None, 'Addtnc': None
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
        payout_cols = ['Displaygroupid', 'Potype', 'Poodrate', 'Potprate', 'Ponetrate', 'Poflatamount', 'Addtnc']
        for p_col in payout_cols:
            if p_col not in df_final.columns:
                df_final[p_col] = None

        df_final.loc[df_final['Mapping Status'] != "✅ MATCH", payout_cols] = None

        generated_cols = ['Mapping Status', 'Failure Reason'] + payout_cols
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
        mis_obj.save()

    except Exception as e:
        # FIX: was `except BaseException`, which also catches KeyboardInterrupt /
        # SystemExit and can block graceful container shutdown. Narrowed to Exception.
        import traceback
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