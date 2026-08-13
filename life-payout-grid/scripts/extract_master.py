"""
Lossless master extraction for the Life Grid workbook.

This is the sole parser for the project: it feeds the live web app's
data/payout_data.json (via the admin upload route and manual runs alike)
and can also produce an audit-grade CSV. Its only goal is: nothing gets
dropped. Every rate tier, every renewal year, every note/remark/condition
anywhere on any of the 16 sheets ends up in the output -- either in a
clean column, or in one of the raw/remarks/notes columns if it doesn't fit
the clean schema.

Usage:
    python extract_master.py <input.xlsx> <output.json> [--source-name "Aug'26"]
                              [--csv <path>] [--audit-log <path>]

<output.json> is what the web app reads. --csv and --audit-log are
optional extras for a manual, audit-grade run (see main()'s docstring).

Design (why this differs from a fixed per-sheet row/column map):
Header rows and column names are located dynamically per sheet by scanning
for a small vocabulary of known labels (Product / PPT / PT / Fresh /
Share at Issuance / Renewal / Comm. Yr-2 / ...), the same approach as
parse_grid.py. This file extends that with:

  - Renewal years are no longer collapsed into a single "next year"
    column. Every column that looks like a renewal-year column (by label
    pattern, e.g. "Year 3", "At 2nd Year", "Comm. Yr-2", "Year 4 & 5
    Onwards") is detected, its policy year(s) are parsed from the label,
    and it is slotted into Renewal_Yr2..Renewal_Yr6_Plus. A combined-range
    label like "Year 4 & 5 Onwards" fills every year it covers (4, 5, and
    6+) with the same value, because that's what the source is saying.
    When two different columns both resolve to the same year slot (this
    happens -- see Bajaj, which has both "At 2nd Year" and "Year 2"), the
    clean slot keeps one (preferring a "Total" column, else the rightmost)
    and *every* candidate, with its original header text, is preserved
    verbatim in All_Renewal_Years_Raw so nothing is silently discarded.

  - Any row that isn't a header, a data row, or a category/title label but
    still has content anywhere across its cells is captured as a sheet
    note instead of being skipped. This picks up header-area notes
    (SUD's "EPI for FY 26-27"), footer disclaimers, and inline sub-tables
    (Shriram's clawback schedule) alike. Only fully blank rows are
    dropped.

  - A dedicated "Remarks"-style column, plus any non-numeric text found
    inside what would otherwise be a base/renewal cell (Kotak's "Only 7
    Pay -1.5%"), is captured per row in Row_Specific_Remarks.
"""

import csv
import json
import re
import sys
from datetime import datetime, timezone

import pandas as pd

# ---------------------------------------------------------------------------
# Sheet name -> display insurer name (Insurer column uses the raw sheet name
# per the required schema; the display name is kept only for the audit log).
# ---------------------------------------------------------------------------

KNOWN_SHEETS = [
    "TATA_AIA", "Bajaj", "Shriram", "Digit", "HDFC", "Kotak", "India_First",
    "Bharti_Axa", "ABLI", "ICICI_Pru", "AxisMax", "Ageas Federal",
    "Generali(FGI)", "PNB", "SUD", "Pramerica",
]

# sheet name -> (display name, short chip label). The CSV/audit deliverable
# always keeps Insurer as the raw sheet name (matching the schema exactly
# as specified); these are added as extra fields for the web app to show
# instead of raw sheet codes.
INSURER_DISPLAY = {
    "TATA_AIA": ("Tata AIA Life", "Tata AIA"),
    "Bajaj": ("Bajaj Allianz Life", "Bajaj"),
    "Shriram": ("Shriram Life", "Shriram"),
    "Digit": ("Digit Life", "Digit"),
    "HDFC": ("HDFC Life", "HDFC"),
    "Kotak": ("Kotak Mahindra Life", "Kotak"),
    "India_First": ("IndiaFirst Life", "IndiaFirst"),
    "Bharti_Axa": ("Bharti AXA Life", "Bharti AXA"),
    "ABLI": ("Aditya Birla Sun Life (ABSLI)", "ABSLI"),
    "ICICI_Pru": ("ICICI Prudential Life", "ICICI Pru"),
    "AxisMax": ("Axis Max Life", "Axis Max"),
    "Ageas Federal": ("Ageas Federal Life", "Ageas"),
    "Generali(FGI)": ("Generali Central (FGI)", "Generali"),
    "PNB": ("PNB MetLife", "PNB MetLife"),
    "SUD": ("Star Union Dai-ichi (SUD)", "SUD"),
    "Pramerica": ("Pramerica Life", "Pramerica"),
}

# ---------------------------------------------------------------------------
# Column-role keyword vocabulary for the SINGLE-valued roles. Order within a
# list = match priority. Substring match against the lowercased,
# whitespace-normalized cell text.
# ---------------------------------------------------------------------------

ROLE_KEYWORDS = {
    "remarks": ["remarks", "remark"],
    "ppt": [
        "premium payment term", "premium paying term", "ppt (yrs)", "ppt",
    ],
    "pt": [
        "policy term (pt)", "policy term", "product term", "pt (yrs)",
    ],
    "product": [
        "product name", "products", "product",
    ],
    "category": [
        "category", "catogory", "segment", "product type",
    ],
    "variant": [
        "varient", "variant",
    ],
    "base": [
        "total yr-1", "total yr 1", "base payout", "share at issuance",
        "1st year payout", "1st yr", "1st year", "fyc", "fresh",
        "commission & reward", "comm. yr-1",
    ],
}

ROLE_ORDER = ["remarks", "ppt", "pt", "product", "category", "variant", "base"]

NULLISH = {"", "-", "—", "–", "na", "n/a", "nil", "none"}

RENEWAL_COL_PATTERN = re.compile(
    r"(renewal|at\s*\d+\w*\s*year|year\s*-?\s*\d|yr\.?\s*-?\s*\d|"
    r"comm\.?\s*yr-?\s*\d|\d+\s*(st|nd|rd|th)\s*year)",
    re.I,
)

YEAR_NUM_PATTERNS = [
    re.compile(r"(?:year|yr)\.?\s*-?\s*(\d+)", re.I),
    re.compile(r"at\s*(\d+)(?:st|nd|rd|th)?\s*year", re.I),
    re.compile(r"yr-(\d+)", re.I),
    re.compile(r"\b(\d+)\s*(?:st|nd|rd|th)\s*year", re.I),
]
OPEN_ENDED_PATTERN = re.compile(r"onwards|above|beyond|\+", re.I)
# "Year 2 & 3", "Year 4 & 5 Onwards": once an initial "year N" anchor is
# found, pick up any further "& M" / "and M" / ", M" numbers riding on it.
CONJUNCTION_YEAR_PATTERN = re.compile(r"(?:&|and|,)\s*(\d+)", re.I)
# "Year 2 TO 5": a continuous range, every year in between gets the value
# (unlike "&", which only names the specific years listed).
RANGE_TO_PATTERN = re.compile(r"(?:year|yr)\.?\s*-?\s*(\d+)\s*to\s*(\d+)", re.I)

RENEWAL_SLOTS = ["Renewal_Yr2", "Renewal_Yr3", "Renewal_Yr4", "Renewal_Yr5", "Renewal_Yr6_Plus"]


def norm(text):
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def clean_text(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = re.sub(r"\s+", " ", str(val)).strip()
    if norm(s) in NULLISH:
        return None
    return s


def is_numeric_cell(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    if isinstance(val, (int, float)):
        return True
    s = str(val).strip().replace("%", "").replace(",", "")
    try:
        float(s)
        return True
    except ValueError:
        return False


def to_number_or_text(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, (int, float)):
        return round(float(val), 6)
    s = str(val).strip()
    if norm(s) in NULLISH:
        return None
    try:
        return round(float(s.replace(",", "")), 6)
    except ValueError:
        return s


def format_value_for_raw(val):
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{val * 100:.3g}%" if abs(val) < 3 else str(val)
    return str(val)


def cell_matches_any_role(text):
    t = norm(text)
    if not t:
        return 0
    hits = 0
    for keywords in ROLE_KEYWORDS.values():
        for kw in keywords:
            if kw in t:
                hits += 1
                break
    if RENEWAL_COL_PATTERN.search(t):
        hits += 1
    return hits


def build_single_roles(labels):
    """Keyword-priority-first assignment for the singular roles (ppt, pt,
    product, category, variant, base, remarks): for each role, its keyword
    list is tried in priority order across *all* columns before falling
    back to the next keyword -- so e.g. "total yr-1" (specific, listed
    first) wins over "comm. yr-1" (a component, listed last) regardless of
    which column happens to sit further left. Returns (roles, used_cols)."""
    roles = {}
    used_cols = set()
    for role in ROLE_ORDER:
        for kw in ROLE_KEYWORDS[role]:
            found = False
            for col_idx, label in enumerate(labels):
                if col_idx in used_cols or not label:
                    continue
                if kw in label:
                    roles[role] = col_idx
                    used_cols.add(col_idx)
                    found = True
                    break
            if found:
                break
    if "base" not in roles:
        # Some sub-tables label the first-year column literally "Year 1"
        # instead of any of the usual synonyms -- that reads as a
        # year-1-only renewal column, not a keyword match, so it's picked
        # up here as a fallback rather than being left unclaimed (which
        # would otherwise sink the whole first-year figure into remarks).
        for col_idx, label in enumerate(labels):
            if col_idx in used_cols or not label:
                continue
            years, open_ended = parse_renewal_years(label)
            if years == "EXCLUDE":
                roles["base"] = col_idx
                used_cols.add(col_idx)
                break
    if "ppt" not in roles and "base" in roles:
        candidate = roles["base"] - 1
        if candidate >= 0 and candidate not in used_cols and labels[candidate]:
            roles["ppt"] = candidate
            used_cols.add(candidate)
    return roles, used_cols


def parse_renewal_years(label):
    """Returns (years:set[int], open_ended:bool) parsed from a column label,
    (None, False) if no year number could be determined (positional
    fallback needed), or ("EXCLUDE", False) if the label explicitly names
    only policy year 1 (e.g. "Reward Yr-1", "Comm. Yr-1") -- those are
    components of the base/first-year figure, never a renewal year, even
    though they can share a "yr-1"-shaped label with real renewal columns."""
    t = norm(label)
    years = set()
    for pat in YEAR_NUM_PATTERNS:
        for m in pat.finditer(t):
            years.add(int(m.group(1)))
    range_match = RANGE_TO_PATTERN.search(t)
    if range_match:
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        if lo <= hi and hi - lo <= 20:
            years.update(range(lo, hi + 1))
    if years:
        for m in CONJUNCTION_YEAR_PATTERN.finditer(t):
            years.add(int(m.group(1)))
    open_ended = bool(OPEN_ENDED_PATTERN.search(t))
    if years and years == {1} and not open_ended:
        return "EXCLUDE", False
    years.discard(1)
    if not years and not open_ended:
        return None, False
    return years, open_ended


def years_to_slots(years, open_ended):
    """Map a set of policy-year numbers (+ optional open-ended flag) to the
    RENEWAL_SLOTS index list (0=Yr2 .. 4=Yr6_Plus)."""
    slots = set()
    if not years and open_ended:
        return {4}  # unknown start, but "onwards" -> at least the 6+ bucket
    for y in years:
        if y <= 1:
            continue
        idx = min(y - 2, 4)  # year 2->0, year3->1, ... year>=6 -> 4 (6_Plus)
        slots.add(idx)
    if open_ended and years:
        start = min(years)
        idx = min(start - 2, 4)
        for i in range(max(idx, 0), 5):
            slots.add(i)
    return slots


def find_renewal_columns(labels, used_cols, base_col):
    """Every not-yet-claimed column is a renewal candidate: first by label
    pattern, then (safety net for zero data loss) any remaining unclaimed
    column positioned after the base column, on the assumption that in this
    workbook nothing after the base column is decorative. Columns that
    parse as year-1-only (Comm./Reward Yr-1 alongside a Total Yr-1 that won
    the base slot) are pulled out separately -- they still get captured,
    just via Row_Specific_Remarks rather than the renewal slots.
    Returns (candidates, year1_extra_cols)."""
    candidates = []
    year1_extras = []
    for idx, label in enumerate(labels):
        if idx in used_cols or not label:
            continue
        years, _ = parse_renewal_years(label)
        if years == "EXCLUDE":
            year1_extras.append(idx)
            used_cols.add(idx)
            continue
        if RENEWAL_COL_PATTERN.search(label):
            candidates.append(idx)
    if base_col is not None:
        for idx, label in enumerate(labels):
            if idx in used_cols or idx in candidates or idx <= base_col:
                continue
            years, _ = parse_renewal_years(label)
            if years == "EXCLUDE":
                year1_extras.append(idx)
                continue
            if label:
                candidates.append(idx)
    candidates.sort()
    return candidates, year1_extras


def assign_renewal_slots(renewal_col_idxs, combined_labels):
    """Returns (slot_value_col: dict[int,int] slot_idx->col_idx picked for
    the clean schema, all_entries: list[(col_idx,label)] every candidate in
    order, for the raw/audit column)."""
    slot_candidates = {i: [] for i in range(5)}
    unresolved = []
    for col_idx in renewal_col_idxs:
        label = combined_labels[col_idx]
        years, open_ended = parse_renewal_years(label)
        if years is None and not open_ended:
            unresolved.append(col_idx)
            continue
        slots = years_to_slots(years, open_ended)
        if not slots:
            unresolved.append(col_idx)
            continue
        for s in slots:
            slot_candidates[s].append(col_idx)

    # Positional fallback for columns with no discernible year: fill the
    # earliest still-empty slot in order.
    for col_idx in unresolved:
        placed = False
        for s in range(5):
            if not slot_candidates[s]:
                slot_candidates[s].append(col_idx)
                placed = True
                break
        if not placed:
            slot_candidates[4].append(col_idx)

    slot_value_col = {}
    for s, cols in slot_candidates.items():
        if not cols:
            continue
        total_match = [c for c in cols if "total" in combined_labels[c]]
        slot_value_col[s] = total_match[-1] if total_match else cols[-1]

    all_entries = [(c, combined_labels[c]) for c in renewal_col_idxs]
    return slot_value_col, all_entries


def split_column_zones(df, scan_rows=25):
    n = min(scan_rows, len(df))
    blanks = set()
    for c in df.columns:
        if df[c].iloc[:n].isna().all():
            blanks.add(c)
    cols = list(df.columns)
    zones, current = [], []
    for c in cols:
        if c in blanks:
            if current:
                zones.append(current)
                current = []
        else:
            current.append(c)
    if current:
        zones.append(current)
    real_zones = []
    for z in zones:
        sub = df[z]
        signal = False
        for _, row in sub.head(15).iterrows():
            text = " ".join(str(v) for v in row.values if pd.notna(v))
            if cell_matches_any_role(text) >= 1:
                signal = True
                break
        if signal:
            real_zones.append(z)
    return real_zones if real_zones else [cols]


PPT_IN_NAME_RE = re.compile(
    r"(\d+\s*-\s*\d+|\d+\s*&\s*\d+|\d+\s*to\s*\d+|\d+\+?|SP|RP)\s*Pay\s*>?", re.I
)


def ppt_from_product_name(name):
    """Some sheets (TATA_AIA) have no PPT column at all -- it's baked into
    the product name instead ("SRP 2 Pay - Life Option"). Extract it and
    strip it out of the display name, so "SRP 2 Pay - X" / "SRP 3 Pay - X"
    collapse back into one product ("SRP - X") with PPT as its own field,
    instead of 100+ near-duplicate product names."""
    if not name:
        return None, name
    m = PPT_IN_NAME_RE.search(name)
    if not m:
        return None, name
    ppt = re.sub(r"\s*Pay\s*>?$", "", m.group(0), flags=re.I).strip()
    cleaned = (name[: m.start()] + name[m.end():]).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"^-\s*|\s*-\s*$", "", cleaned).strip()
    cleaned = re.sub(r"\s*-\s*-\s*", " - ", cleaned).strip()
    return ppt, (cleaned or name)


def parse_zone(df_zone, sheet_name, audit):
    """Row-scans one column-zone. Returns (records, sheet_notes)."""
    cols = list(df_zone.columns)
    records = []
    notes = []

    roles = {}
    renewal_slot_cols = {}
    renewal_all_entries = []
    renewal_year1_extra_cols = []
    combined_labels_current = []
    current_category = None
    current_product = None
    current_variant = None
    current_pt = None
    pending_title = None

    rows = df_zone.reset_index(drop=True)
    n = len(rows)
    i = 0
    while i < n:
        row = rows.iloc[i]
        raw_cells = [row[c] for c in cols]
        label_cells = [norm(v) for v in raw_cells]
        match_count = sum(1 for v in raw_cells if cell_matches_any_role(v) > 0)
        # Real header rows are pure text -- a data row that happens to
        # contain descriptive text like "For Renewal Only" or "(Variant 3)"
        # can rack up keyword matches too, but it will always also carry a
        # real numeric payout value somewhere. Excluding rows with any
        # numeric cell keeps those false positives from being mistaken for
        # a new header block.
        has_any_numeric = any(is_numeric_cell(v) for v in raw_cells)

        if match_count >= 2 and not has_any_numeric:
            next_row_is_pure_subheader = False
            next_raw = None
            if i + 1 < n:
                next_row = rows.iloc[i + 1]
                next_raw = [next_row[c] for c in cols]
                next_non_null = [v for v in next_raw if clean_text(v) is not None]
                if next_non_null and all(cell_matches_any_role(v) > 0 for v in next_non_null):
                    next_row_is_pure_subheader = True

            # Only merge in the next row's text when it's a genuine
            # sub-header (e.g. ICICI_Pru's "Share at Issuance" / "At 2nd
            # Year" sitting below "Fresh" / "Renewals"). Never merge actual
            # data into the label -- that would contaminate the raw column
            # labels used for audit traceability.
            combined = list(label_cells)
            if next_row_is_pure_subheader and next_raw is not None:
                for idx, val in enumerate(next_raw):
                    nxt = norm(val)
                    if nxt:
                        combined[idx] = (combined[idx] + " " + nxt).strip()

            single_roles, used_cols = build_single_roles(combined)
            renewal_col_idxs, year1_extra_idxs = find_renewal_columns(
                combined, used_cols, single_roles.get("base")
            )
            slot_cols, all_entries = assign_renewal_slots(renewal_col_idxs, combined)

            roles = single_roles
            renewal_slot_cols = slot_cols
            renewal_all_entries = all_entries
            renewal_year1_extra_cols = year1_extra_idxs
            combined_labels_current = combined
            current_category = None
            current_product = None
            current_variant = None
            current_pt = None
            pending_title = None
            i += 2 if next_row_is_pure_subheader else 1
            continue

        if not roles:
            non_null = [clean_text(v) for v in raw_cells if clean_text(v) is not None]
            if non_null:
                notes.append(" ".join(non_null))
            i += 1
            continue

        def get(role):
            idx = roles.get(role)
            return raw_cells[idx] if idx is not None else None

        ppt_val = clean_text(get("ppt"))
        base_val = get("base")
        product_val = clean_text(get("product"))
        category_val = clean_text(get("category"))
        variant_val = clean_text(get("variant"))
        pt_val = clean_text(get("pt"))
        remarks_val = clean_text(get("remarks"))

        renewal_raw_vals = {
            idx: raw_cells[idx] for idx in [c for c, _ in renewal_all_entries]
        }

        has_base = is_numeric_cell(base_val) or clean_text(base_val) is not None
        has_renewal = any(
            is_numeric_cell(v) or clean_text(v) is not None for v in renewal_raw_vals.values()
        )

        has_own_or_context_product = product_val is not None or current_product is not None
        # A footnote list item ("a. Protection deferment...", "b. ULIP
        # commercials...") can land with its marker in the product column
        # and its sentence spilling into the PPT column -- which would
        # otherwise look exactly like a real "ppt_val is present" data row.
        # Guard against that specific shape: a fresh (non-blank) product
        # cell that's just a bullet marker is never a real product name.
        product_is_bullet_marker = product_val is not None and is_bullet_marker(product_val)
        if ppt_val is not None and not product_is_bullet_marker:
            is_data_row = True
        elif (has_base or has_renewal) and has_own_or_context_product:
            # PPT cell is blank on this row (e.g. Digit's "Glow Plus" variants
            # have no numeric PPT) but there's real rate data plus a product
            # to attach it to -- don't drop it for lack of a PPT value.
            is_data_row = True
        else:
            is_data_row = False

        if not is_data_row:
            if product_val and not has_base and not has_renewal:
                if looks_like_category_label(product_val):
                    current_category = product_val
                    i += 1
                    continue
                # A footnote sentence landed in the product-name column --
                # capture it as a note, not as a category, and don't let a
                # stale product name leak into whatever table follows.
                notes.append(product_val)
                current_product = None
                current_variant = None
                i += 1
                continue
            non_null = [v for v in raw_cells if clean_text(v) is not None]
            if roles.get("product") is None and len(non_null) == 1:
                text = clean_text(non_null[0])
                if text and len(text) <= 40:
                    pending_title = text
                    i += 1
                    continue
            texts = [clean_text(v) for v in raw_cells if clean_text(v) is not None]
            if texts:
                notes.append(" ".join(texts))
            current_product = None
            current_variant = None
            i += 1
            continue

        if product_val and product_val != current_product:
            # A genuinely new product started -- Variant/PT belong to
            # whichever product they were written under, so a forward-fill
            # from the previous product must not leak into this one.
            current_variant = None
            current_pt = None
        if product_val:
            current_product = product_val
        if pt_val:
            current_pt = pt_val
        if category_val:
            current_category = category_val
        if variant_val:
            current_variant = variant_val

        product_name = current_product or pending_title
        if not product_name:
            # Flagged as a data row but there's no product context to attach
            # it to (e.g. a stray sub-table after the real product block
            # ended) -- never drop silently, capture it as a note instead.
            texts = [clean_text(v) for v in raw_cells if clean_text(v) is not None]
            if texts:
                notes.append(" ".join(texts))
            i += 1
            continue

        ppt_final = ppt_val
        if not ppt_final:
            extracted_ppt, cleaned_name = ppt_from_product_name(product_name)
            if extracted_ppt:
                ppt_final = extracted_ppt
                product_name = cleaned_name
            else:
                ppt_final = "-"

        row_remarks = []
        if remarks_val:
            row_remarks.append(remarks_val)

        # Year-1 component columns (Comm./Reward Yr-1 alongside a Total
        # Yr-1 that won the Base_Payout_Yr1 slot) aren't renewal data, but
        # their values still have to end up *somewhere* for zero loss.
        for col_idx in renewal_year1_extra_cols:
            val = to_number_or_text(raw_cells[col_idx])
            if val is not None:
                row_remarks.append(f"{combined_labels_current[col_idx]}: {format_value_for_raw(val)}")

        base_number = to_number_or_text(base_val)
        if isinstance(base_number, str):
            base_label = combined_labels_current[roles["base"]] if roles.get("base") is not None else "Base"
            row_remarks.append(f"{base_label}: {base_number}")

        renewal_out = {slot: None for slot in RENEWAL_SLOTS}
        for slot_idx, col_idx in renewal_slot_cols.items():
            val = to_number_or_text(raw_cells[col_idx])
            renewal_out[RENEWAL_SLOTS[slot_idx]] = val
            if isinstance(val, str):
                row_remarks.append(f"{combined_labels_current[col_idx]}: {val}")

        raw_parts = []
        for col_idx, label in renewal_all_entries:
            val = to_number_or_text(raw_cells[col_idx])
            raw_parts.append(f"{label or f'col{col_idx}'}: {format_value_for_raw(val)}")
        all_renewal_raw = "; ".join(raw_parts)

        record = {
            "Insurer": sheet_name,
            "Category": current_category,
            "Product_Name": product_name,
            "Variant": current_variant,
            "PPT": ppt_final,
            "PT": current_pt or "-",
            "Base_Payout_Yr1": base_number,
            **renewal_out,
            "Row_Specific_Remarks": " | ".join(row_remarks) if row_remarks else None,
            "Sheet_Level_Notes_And_Conditions": None,  # filled in after all zones parsed
            "All_Renewal_Years_Raw": all_renewal_raw or None,
        }
        records.append(record)
        i += 1

    return records, notes


BULLET_MARKER_RE = re.compile(r"^[a-zA-Z0-9]{1,2}[\.\)]$")


def is_bullet_marker(text):
    """"a.", "b)", "1." -- a footnote list marker, never a real product
    name or category label even though it's short text sitting in that
    column."""
    return bool(BULLET_MARKER_RE.match(text.strip()))


def looks_like_category_label(text):
    """Distinguishes a genuine short section/category label ("Protection",
    "PAR(Trad)") from a footnote sentence that happens to land in the
    product-name column ("*5 Pay total payout to be capped at...") or a
    footnote list marker ("a.", "b.")."""
    if len(text) > 45:
        return False
    if text.startswith(("*", "#")):
        return False
    if re.match(r"^\d+[\.\)]", text):
        return False
    if is_bullet_marker(text):
        return False
    if ". " in text:
        return False
    return True


def dedupe_preserve_order(items):
    seen = set()
    out = []
    for it in items:
        key = it.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def parse_sheet(path, sheet_name, audit):
    df = pd.read_excel(path, sheet_name=sheet_name, header=None)
    zones = split_column_zones(df)
    all_records, all_notes = [], []
    for zone_cols in zones:
        records, notes = parse_zone(df[zone_cols], sheet_name, audit)
        all_records.extend(records)
        all_notes.extend(notes)

    notes_deduped = dedupe_preserve_order(all_notes)
    notes_str = " | ".join(notes_deduped) if notes_deduped else None
    display_name, chip_code = INSURER_DISPLAY.get(sheet_name, (sheet_name, sheet_name))
    for r in all_records:
        r["Sheet_Level_Notes_And_Conditions"] = notes_str
        r["InsurerDisplay"] = display_name
        r["InsurerCode"] = chip_code

    return all_records, notes_deduped, df.shape[0]


MASTER_COLUMNS = [
    "Insurer", "Category", "Product_Name", "Variant", "PPT", "PT",
    "Base_Payout_Yr1", "Renewal_Yr2", "Renewal_Yr3", "Renewal_Yr4",
    "Renewal_Yr5", "Renewal_Yr6_Plus", "Row_Specific_Remarks",
    "Sheet_Level_Notes_And_Conditions", "All_Renewal_Years_Raw",
]

# Extra columns (beyond the required master schema) included in the JSON
# used by the web app, but left out of the audit CSV to keep that file
# matching the schema exactly as specified.
APP_ONLY_COLUMNS = ["InsurerDisplay", "InsurerCode"]

# Commission-scaling multipliers applied to the portal-facing JSON only.
# The CSV/audit-log deliverable stays unscaled -- it exists to be checked
# against the original Excel, so it has to keep matching that exactly.
# This is what makes the JSON differ from the CSV in *values*, not just
# shape; that's intentional, see apply_portal_scaling()'s docstring.
BASE_YR1_MULTIPLIER = 0.75
RENEWAL_MULTIPLIER = 0.50


def apply_portal_scaling(records):
    """Base_Payout_Yr1 x0.75, every Renewal_Yr* slot x0.50 -- e.g. a 64.0%
    base extracted from the source sheet becomes 48.0% on the portal. Only
    numeric values are scaled; text values (e.g. Kotak's "Only 7 Pay
    -1.5%") pass through unchanged since there's no number to scale.
    Row_Specific_Remarks and All_Renewal_Years_Raw are deliberately left
    as the true source figures -- they're the audit trail, not the quoted
    rate -- so a figure quoted from the clean columns and one read out of
    the expanded "Full schedule" detail will legitimately differ; that's
    expected, not a bug."""
    scaled = []
    for r in records:
        r2 = dict(r)
        base = r2.get("Base_Payout_Yr1")
        if isinstance(base, (int, float)):
            r2["Base_Payout_Yr1"] = round(base * BASE_YR1_MULTIPLIER, 6)
        for slot in RENEWAL_SLOTS:
            val = r2.get(slot)
            if isinstance(val, (int, float)):
                r2[slot] = round(val * RENEWAL_MULTIPLIER, 6)
        scaled.append(r2)
    return scaled


def fail(error, warnings=None, sheet_errors=None):
    print(json.dumps({
        "ok": False, "error": error,
        "warnings": warnings or [], "sheetErrors": sheet_errors or [],
    }))
    sys.exit(1)


def main():
    """
    Usage:
        extract_master.py <input.xlsx> <output.json> [--source-name X]
                           [--csv <path>] [--audit-log <path>]

    <output.json> is the file the web app reads (data/payout_data.json).
    A single-line JSON summary is printed to stdout so callers (the admin
    upload API route) can parse it directly; the full human-readable audit
    log always goes to stderr, and optionally to --audit-log as well.
    --csv is optional and only written when explicitly requested (manual
    audits) -- it matches the master schema exactly, with Insurer as the
    raw sheet name.
    """
    if len(sys.argv) < 3:
        fail("Usage: extract_master.py <input.xlsx> <output.json> [--source-name X] [--csv PATH] [--audit-log PATH]")

    input_path = sys.argv[1]
    output_json_path = sys.argv[2]
    source_label = None
    csv_path = None
    audit_log_path = None
    args = sys.argv[3:]
    i = 0
    while i < len(args):
        if args[i] == "--source-name" and i + 1 < len(args):
            source_label = args[i + 1]
            i += 2
        elif args[i] == "--csv" and i + 1 < len(args):
            csv_path = args[i + 1]
            i += 2
        elif args[i] == "--audit-log" and i + 1 < len(args):
            audit_log_path = args[i + 1]
            i += 2
        else:
            i += 1

    try:
        xl = pd.ExcelFile(input_path)
    except Exception as e:
        fail(f"Could not open workbook: {e}")

    known_sheets = set(KNOWN_SHEETS)
    found_known = [s for s in xl.sheet_names if s in known_sheets]
    if not found_known:
        fail(
            "This workbook doesn't look like a Life Grid file -- none of the expected "
            f"insurer sheet names were found. Sheets present: {xl.sheet_names}"
        )

    audit_rows = []
    all_records = []
    anomalies = []
    sheet_errors = []
    insurer_summary = []

    for sheet_name in KNOWN_SHEETS:
        if sheet_name not in xl.sheet_names:
            audit_rows.append({
                "sheet": sheet_name, "raw_rows": 0, "processed_rows": 0,
                "notes_count": 0, "remarks_rows": 0, "status": "MISSING FROM WORKBOOK",
            })
            anomalies.append(f"Sheet '{sheet_name}' was not found in the workbook.")
            continue

        try:
            records, notes, raw_row_count = parse_sheet(input_path, sheet_name, audit_rows)
        except Exception as e:
            audit_rows.append({
                "sheet": sheet_name, "raw_rows": "?", "processed_rows": 0,
                "notes_count": 0, "remarks_rows": 0, "status": f"ERROR: {e}",
            })
            msg = f"Sheet '{sheet_name}' raised an exception: {e}"
            anomalies.append(msg)
            sheet_errors.append(msg)
            continue

        remarks_rows = sum(1 for r in records if r["Row_Specific_Remarks"])
        overflow_rows = sum(
            1 for r in records
            if r["All_Renewal_Years_Raw"] and r["All_Renewal_Years_Raw"].count(";") >= 5
        )
        status = "OK"
        if len(records) == 0:
            status = "ZERO ROWS EXTRACTED"
            anomalies.append(f"Sheet '{sheet_name}': 0 payout rows extracted from {raw_row_count} raw rows.")
        if overflow_rows > 0:
            anomalies.append(
                f"Sheet '{sheet_name}': {overflow_rows} row(s) have more than 5 distinct "
                f"renewal-year columns in source -- all preserved in All_Renewal_Years_Raw, "
                f"but only a representative value fits the fixed Renewal_Yr2..Yr6_Plus slots."
            )

        audit_rows.append({
            "sheet": sheet_name, "raw_rows": raw_row_count, "processed_rows": len(records),
            "notes_count": len(notes), "remarks_rows": remarks_rows, "status": status,
        })
        display_name, chip_code = INSURER_DISPLAY.get(sheet_name, (sheet_name, sheet_name))
        products = set((r["Product_Name"], r["Variant"]) for r in records)
        insurer_summary.append({
            "code": chip_code, "name": display_name, "sheet": sheet_name,
            "rowCount": len(records), "productCount": len(products),
        })
        all_records.extend(records)

    if len(all_records) < 50:
        fail(
            f"Only {len(all_records)} payout rows were extracted in total -- that's too few "
            "for a real grid, the file format has likely changed in a way the parser doesn't "
            "understand.",
            warnings=anomalies, sheet_errors=sheet_errors,
        )

    for idx, r in enumerate(all_records):
        r["Row_ID"] = idx + 1

    resolved_source = source_label or input_path.split("\\")[-1].split("/")[-1]
    ordered_columns = ["Row_ID"] + MASTER_COLUMNS + APP_ONLY_COLUMNS
    portal_records = apply_portal_scaling(all_records)

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "meta": {
                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                    "sourceFile": resolved_source,
                    "totalRows": len(portal_records),
                    "totalProducts": sum(s["productCount"] for s in insurer_summary),
                    "insurers": sorted(insurer_summary, key=lambda s: -s["rowCount"]),
                    "warnings": anomalies,
                    "sheetErrors": sheet_errors,
                    "rateScaling": {
                        "baseYr1Multiplier": BASE_YR1_MULTIPLIER,
                        "renewalMultiplier": RENEWAL_MULTIPLIER,
                    },
                },
                "rows": [{k: r.get(k) for k in ordered_columns} for r in portal_records],
            },
            f, indent=2, ensure_ascii=False,
        )

    if csv_path:
        csv_columns = ["Row_ID"] + MASTER_COLUMNS
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=csv_columns)
            writer.writeheader()
            for r in all_records:
                writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in csv_columns})

    # --- Human-readable audit log (stderr always; file when requested) ---
    lines = []
    lines.append("LIFE GRID -- LOSSLESS EXTRACTION AUDIT LOG")
    lines.append(f"Source: {input_path}")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    header = f"{'Sheet':<16}{'Raw Rows':>10}{'Processed Rows':>16}{'Notes':>8}{'Remarks Rows':>14}  Status"
    lines.append(header)
    lines.append("-" * len(header))
    for a in audit_rows:
        lines.append(
            f"{a['sheet']:<16}{str(a['raw_rows']):>10}{str(a['processed_rows']):>16}"
            f"{str(a['notes_count']):>8}{str(a['remarks_rows']):>14}  {a['status']}"
        )
    lines.append("")
    lines.append(f"TOTAL payout rows extracted: {len(all_records)}")
    lines.append(f"TOTAL sheets processed: {len(audit_rows)}")
    lines.append("")
    if anomalies:
        lines.append(f"ANOMALIES / FLAGS FOR REVIEW ({len(anomalies)}):")
        for a in anomalies:
            lines.append(f"  - {a}")
    else:
        lines.append("No anomalies flagged.")
    log_text = "\n".join(lines)

    print(log_text, file=sys.stderr)
    if audit_log_path:
        with open(audit_log_path, "w", encoding="utf-8") as f:
            f.write(log_text)

    print(json.dumps({
        "ok": True,
        "rows": len(all_records),
        "insurers": len(insurer_summary),
        "warnings": anomalies,
        "sheetErrors": sheet_errors,
    }))


if __name__ == "__main__":
    main()
