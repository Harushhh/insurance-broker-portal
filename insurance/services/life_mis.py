"""The MIS Payout Engine's Life route: match one MIS policy whose Product is "life" to a row of the live Life grid.

Nothing here writes the engine's output. ``match_life_policy`` returns a LifeMatch and ``mapping_engine`` turns it into
the row's Mapping Status / Failure Reason / Porate / Addtnc, so the wording of a reason lives in the engine with the
others. Nothing is guessed: a spelling nobody has mapped, a missing plan name or a PPT that no grid row covers ends the
row as NO MATCH (or FAILED - BAD DATA) and says exactly what to fix.

The rules (the user's, from the plan agreed 2026-11-14)

  * Policy Type: only a 'Fresh' policy is read for its first-year payout. 'Endorsement Financial' (or anything else,
    or blank) is NO MATCH.
  * Insurer: the MIS spelling is looked up on Life Insurer Aliases first; then the grid's own insurer name; then a
    MIS name that contains every distinctive word of exactly one grid insurer, unless it is a General insurer's name
    ("Bajaj Allianz General ..." on a Life policy is flagged, never tied to "Bajaj Allianz Life"). Otherwise it is
    unmapped.
  * Plan: the MIS plan name is looked up on Life Plan Mapping (the grid products it stands for); failing that, it is
    matched by NAME only: equal to a grid product, or to one of the '/'-separated names a grid product lists
    ("ACE / ACE ADVANTAGE"), once the insurer's own brand words are dropped from the front. Never fuzzy: a name that
    is not equal is unmapped (the reason lists the closest grid products as a hint) and a name that fits several grid
    products is ambiguous. A blank plan name cannot be matched at all (39% of the Sept'26 Life rows).
  * Dates (added 2026-11-14, mirroring Motor's and Health's date window): a grid row applies when the policy's
    'Policy: inception date' is inside its from_date / to_date, both included, blank = no limit on that side. A blank
    or unreadable inception date leaves only the rows that have no dates. A grid with no dates is never narrowed, so
    nothing changes until dates are loaded.
  * PPT and PT: the user's rules in life_filters (numeric coverage; a row that states none applies to every one) applied
    to the policy's own numbers; see life_filters.policy_ppt_matches. A blank, 0 or unreadable PPT or PT, or a PPT longer
    than the term, is bad data (a MIS premium mode that says "single" stands for a PPT of 1).
  * Rate: the grid row's first-year payout (percent, as stored). Several rows left alike in first-year payout, deferred
    part, condition and remarks collapse to the lowest id; rows that differ are MULTIPLE MATCHES, named by condition.
  * Never applied, only printed: the deferred second-year part, and any condition or remark of the row.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd
from django.db.models import Q
from rapidfuzz import fuzz, process

from ..models import LifeInsurerAlias, LifePlanMapping, LifeRate, MISFailedRow
from . import life_filters as lf
from .life_rates import active_life_upload

LIFE_PRODUCT = "life"

# The Potype a matched Life row carries: the first-year payout, as a percent of the premium.
PAYOUT_TYPE = "On Premium - Year 1"

# The labels of the engine's "Failed on: <label> — <detail>" (the coverage-gap panel groups on them).
INSURER_LABEL = "Policy: insurance company"
PLAN_LABEL = "Policy: product name / plan name"
PPT_LABEL = "Policy: premium paying term / ppt"
PT_LABEL = "Policy: policy term / pt"
POLICY_TYPE_LABEL = "Policy Type"
DATE_LABEL = "Policy: inception date"           # the same label Motor's and Health's date window use
GRID_LABEL = "Life grid"

FIRST_YEAR_POLICY_TYPES = frozenset({"fresh"})

# The MIS headers the Life route reads, by role, in the order they are tried. Matched with case, punctuation and
# spacing ignored ("Policy: product name  plan name" has two spaces in the real exports and no slash). The first
# non-blank cell of a role wins: some exports put the Life insurer under 'ins company' or 'insurer name' only.
LIFE_COLUMNS = {
    "insurer": ("Policy: insurance company", "Policy: ins company", "Policy: insurer name"),
    "plan": ("Policy: product name  plan name", "Policy: plan name", "Policy: product name"),
    "ppt": ("Policy: premium paying term  ppt", "Policy: premium paying term", "Policy: ppt"),
    "pt": ("Policy: policy termpt", "Policy: policy term pt", "Policy: policy term"),
    "mode": ("Policy: premium mode",),
    "policy_type": ("Policy Type",),
}

# Words that say nothing about which insurer is meant.
_INSURER_FILLER = frozenset({"insurance", "company", "co", "ltd", "limited", "the", "of", "and"})
_BLANK_TEXT = frozenset({"", "nan", "none", "null", "nil", "na", "n/a", "-"})

# What the screens look for in a stored failure reason (MISFailedRow) to list what still needs mapping. The detail
# sentences below are written to these patterns, and test_life_mis checks the two stay in step.
PLAN_MAPPING_HINT = "add it on Life Plan Mapping"
INSURER_ALIAS_HINT = "add it on Life Insurer Aliases"
UNMAPPED_PLAN_PATTERNS = (
    re.compile(r"Plan '(.*?)' is not mapped to any '(.*?)' Life grid product"),
    re.compile(r"Plan '(.*?)' matches \d+ '(.*?)' Life grid products by name"),
)
UNKNOWN_INSURER_PATTERN = re.compile(
    r"Insurer '(.*?)' is (?:not a known Life insurer|a General insurer's name on a Life policy)"
)


# ----------------------------------------------------------------------------------------------- reading text
def fold(text):
    """Case, punctuation and spacing folded away: 'Go-Digit Life Insurance Ltd.' -> 'go digit life insurance ltd'."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def text_of(value):
    """A cell as clean text: '' for a blank, NaN or null-like cell."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in _BLANK_TEXT else text


def whole_number(value):
    """12, '12', '12.0', '1,200' -> the int; anything that is not a whole number -> None."""
    text = text_of(value).replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")) or number != int(number):
        return None
    return int(number)


def percent(number):
    """15.0 -> '15', 15.5 -> '15.5'."""
    return f"{number:g}" if number is not None else ""


def _header_key(header):
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def find_life_columns(columns):
    """{role: [the real headers present, in LIFE_COLUMNS order]} for a MIS file's columns."""
    present = {}
    for column in columns:
        present.setdefault(_header_key(column), column)
    return {
        role: [present[_header_key(name)] for name in names if _header_key(name) in present]
        for role, names in LIFE_COLUMNS.items()
    }


def life_fields(df):
    """The Life matching inputs of every row of a MIS frame, as clean text: a frame with one column per role."""
    found = find_life_columns(df.columns)
    out = {}
    for role, names in found.items():
        series = None
        for name in names:
            cells = df[name].map(text_of)
            series = cells if series is None else series.where(series != "", cells)
        out[role] = series if series is not None else pd.Series([""] * len(df), index=df.index)
    return pd.DataFrame(out, index=df.index)


# ----------------------------------------------------------------------------------------------- the grid
@dataclass(frozen=True)
class GridRow:
    id: int
    insurer: str
    product: str
    product_key: str
    names: frozenset           # the product folded, and each '/'-separated name in it folded
    variant: str
    category: str
    ppt_text: str
    pt_text: str
    ppt: object                # life_filters.PptSpec
    pt: object                 # life_filters.PtSpec
    condition: str
    remarks: str
    yr1: float | None
    deferred: float | None
    from_date: object = None   # datetime.date: the row applies from this day (included); None = no start
    to_date: object = None     # datetime.date: ... until this day (included); None = no end


@dataclass
class LifeGrid:
    """The live Life upload as the engine reads it, with the two mapping tables, loaded once per run."""
    upload_id: int
    label: str
    rows: dict                 # insurer -> [GridRow], in workbook order
    aliases: dict              # insurer name_key -> grid insurer
    mappings: dict             # (grid insurer, plan_key) -> grid_products text

    @property
    def insurers(self):
        return frozenset(self.rows)


def load_life_grid():
    """The live upload's rows plus the alias and plan mappings; None when no Life grid is live."""
    upload = active_life_upload()
    if upload is None:
        return None
    rows = {}
    for r in LifeRate.objects.filter(upload=upload).order_by("position", "id").values(
            "id", "insurer", "product", "variant", "category", "ppt", "pt", "condition", "remarks",
            "payout_yr1", "payout_deferred", "from_date", "to_date"):
        product = (r["product"] or "").strip()
        names = {fold(product)} | {fold(part) for part in product.split("/")}
        names.discard("")
        rows.setdefault(r["insurer"], []).append(GridRow(
            id=r["id"], insurer=r["insurer"], product=product, product_key=fold(product), names=frozenset(names),
            variant=(r["variant"] or "").strip(), category=(r["category"] or "").strip(),
            ppt_text=(r["ppt"] or "").strip(), pt_text=(r["pt"] or "").strip(),
            ppt=lf.ppt_spec(r["ppt"] or ""), pt=lf.pt_spec(r["pt"] or ""),
            condition=(r["condition"] or "").strip(), remarks=(r["remarks"] or "").strip(),
            yr1=r["payout_yr1"], deferred=r["payout_deferred"], from_date=r["from_date"], to_date=r["to_date"],
        ))
    return LifeGrid(
        upload_id=upload.id, label=upload.label, rows=rows,
        aliases={a.name_key: a.insurer for a in LifeInsurerAlias.objects.all()},
        mappings={(m.insurer, m.plan_key): m.grid_products for m in LifePlanMapping.objects.all()},
    )


# ----------------------------------------------------------------------------------------------- the answer
@dataclass
class LifePolicy:
    insurer: str = ""
    plan: str = ""
    ppt: str = ""
    pt: str = ""
    mode: str = ""
    policy_type: str = ""
    inception: object = None                    # datetime.date of the policy's inception date, None when blank / unreadable


@dataclass
class LifeMatch:
    kind: str                                   # matched | multiple | no_match | bad_data
    stage: str = ""                             # why not matched, for the run summary (see STAGE_LABELS)
    failed_on: list = field(default_factory=list)     # [(label, detail)] every rate-matching failure, in rule order
    data_errors: list = field(default_factory=list)   # [message] problems in the policy's own PPT / PT
    row: GridRow | None = None                  # the grid row the rate is read from (matched)
    rows: list = field(default_factory=list)    # the rows that clashed (multiple)
    tied: int = 0                               # how many alike rows collapsed to the lowest id (matched)
    how: str = ""                               # how the plan was found: 'mapping' or 'name' (matched)


# What each stage of a row that did not match is called in the run summary.
STAGE_LABELS = {
    "policy_type": "not a first-year policy",
    "insurer": "insurer not recognised",
    "plan_blank": "no plan name",
    "plan_unmapped": "plan not mapped",
    "plan_ambiguous": "plan name fits several products",
    "plan_stale": "plan mapping points to nothing in the grid",
    "date": "inception date outside the grid rows' dates",
    "ppt": "no grid row for the PPT",
    "pt": "no grid row for the PT",
    "rate": "grid row has no first-year payout",
    "bad_data": "bad PPT / PT",
    "multiple": "several grid rows differ",
}


def _insurer_words(name):
    return set(fold(name).split()) - _INSURER_FILLER


def _distinct(values):
    seen, kept = set(), []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            kept.append(value)
    return kept


def _listed(values, limit=3):
    values = list(values)
    text = "; ".join(values[:limit])
    return text + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


def resolve_insurer(raw, grid):
    """-> (the grid insurer, None) or (None, the reason it could not be placed)."""
    raw = text_of(raw)
    if not raw:
        return None, "Insurer is blank on this policy, so no Life grid insurer can be chosen."
    key = fold(raw)
    alias = grid.aliases.get(key)
    if alias is not None:
        if alias in grid.insurers:
            return alias, None
        return None, (f"Insurer '{raw}' is mapped to '{alias}', which is not an insurer of the live Life grid "
                      f"('{grid.label}') — fix it on Life Insurer Aliases.")
    for insurer in sorted(grid.insurers):
        if fold(insurer) == key:
            return insurer, None
    words = _insurer_words(raw)
    if "general" in words:
        return None, (f"Insurer '{raw}' is a General insurer's name on a Life policy — correct the MIS, or "
                      f"{INSURER_ALIAS_HINT} if it is the Life company.")
    fits = [g for g in sorted(grid.insurers) if _insurer_words(g) and _insurer_words(g) <= words]
    if len(fits) == 1:
        return fits[0], None
    if len(fits) > 1:
        return None, (f"Insurer '{raw}' is not a known Life insurer (it fits {' and '.join(fits)}) — "
                      f"{INSURER_ALIAS_HINT}.")
    return None, f"Insurer '{raw}' is not a known Life insurer — {INSURER_ALIAS_HINT}."


def parse_grid_products(text):
    """A mapping's grid_products text -> (the exact products, the 'starts with' prefixes), all folded."""
    exact, prefixes = set(), []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith("*"):
            prefix = fold(line[:-1])
            if prefix:
                prefixes.append(prefix)
        elif fold(line):
            exact.add(fold(line))
    return exact, prefixes


def rows_for_mapping(rows, grid_products):
    """The grid rows (of one insurer) that a mapping's grid_products text stands for."""
    exact, prefixes = parse_grid_products(grid_products)
    return [
        row for row in rows
        if row.product_key in exact or any(row.product_key == p or row.product_key.startswith(p + " ") for p in prefixes)
    ]


def _core_plan(key, insurer, mis_insurer):
    """The plan name without the insurer's own brand words at the front ('hdfc life click 2 achieve' -> 'click 2 achieve')."""
    brand = (set(fold(insurer).split()) | set(fold(mis_insurer).split())) - _INSURER_FILLER
    words = key.split()
    while len(words) > 1 and words[0] in brand:
        words.pop(0)
    return " ".join(words)


def closest_products(core, rows, limit=3):
    """The grid products nearest in spelling to a plan name: a hint for the person mapping it, never used to match."""
    products = _distinct(row.product for row in rows)
    if not core or not products:
        return []
    return [choice for choice, _score, _index in process.extract(
        core, products, scorer=fuzz.token_set_ratio, processor=fold, limit=limit, score_cutoff=60)]


def resolve_plan(plan_raw, insurer, mis_insurer, grid):
    """-> (grid rows, how, stage, detail). detail is None when the plan was placed on at least one grid row."""
    plan = text_of(plan_raw)
    if not plan:
        return [], "", "plan_blank", (
            "Plan name is blank on this policy, so the Life product cannot be chosen — fill in 'Policy: product "
            "name / plan name' in the MIS and re-run mapping for this row.")
    key = fold(plan)
    rows = grid.rows.get(insurer, [])

    mapped = grid.mappings.get((insurer, key))
    if mapped is not None:
        hits = rows_for_mapping(rows, mapped)
        if hits:
            return hits, "mapping", "", None
        return [], "", "plan_stale", (
            f"Plan '{plan}' is mapped on Life Plan Mapping, but none of its grid products is in the live Life grid "
            f"('{grid.label}') for '{insurer}' — update the mapping.")

    core = _core_plan(key, insurer, mis_insurer)
    hits = [row for row in rows if core in row.names or key in row.names]
    products = _distinct(row.product for row in hits)
    if len(products) == 1:
        return hits, "name", "", None
    if len(products) > 1:
        return [], "", "plan_ambiguous", (
            f"Plan '{plan}' matches {len(products)} '{insurer}' Life grid products by name ({_listed(products)}) — "
            f"choose which on Life Plan Mapping.")
    close = closest_products(core, rows)
    hint = f" (closest grid products: {'; '.join(close)})" if close else ""
    return [], "", "plan_unmapped", (
        f"Plan '{plan}' is not mapped to any '{insurer}' Life grid product — {PLAN_MAPPING_HINT}{hint}.")


def _read_terms(policy):
    """-> (ppt years or None, term years or None, single pay?, data errors) for one policy."""
    errors = []
    ppt_text, pt_text = text_of(policy.ppt), text_of(policy.pt)
    ppt, term = whole_number(ppt_text), whole_number(pt_text)
    single_pay = bool(set(fold(policy.mode).split()) & {"single", "sp"}) or fold(ppt_text) in {
        "sp", "single", "single pay", "single premium"}

    if single_pay:
        if ppt is not None and ppt >= 2:
            errors.append(f"{PPT_LABEL} is {ppt} but the premium mode says single pay.")
            ppt = None
        else:
            ppt = 1
    elif not ppt_text:
        errors.append(f"{PPT_LABEL} is blank on this policy, so its pay term cannot be matched.")
    elif ppt is None:
        errors.append(f"{PPT_LABEL} '{ppt_text}' is not a whole number of years.")
    elif ppt == 0:
        errors.append(f"{PPT_LABEL} is 0, which cannot be matched to a pay term.")
        ppt = None

    if not pt_text:
        errors.append(f"{PT_LABEL} is blank on this policy, so its policy term cannot be matched.")
    elif term is None:
        errors.append(f"{PT_LABEL} '{pt_text}' is not a whole number of years.")
    elif term == 0:
        errors.append(f"{PT_LABEL} is 0, which cannot be matched to a policy term.")
        term = None

    if not single_pay and ppt is not None and term is not None and ppt > term:
        errors.append(f"{PPT_LABEL} ({ppt} years) is longer than {PT_LABEL} ({term} years).")
    return ppt, term, single_pay, errors


def _narrow(pool, keep, failed_on, stages, stage, label, detail):
    """One rule over the candidate grid rows. If none survive the rule has failed: it is recorded and the candidates
    from before it are kept, so every later rule still runs (the engine's batch error collection)."""
    kept = [row for row in pool if keep(row)]
    if kept:
        return kept
    failed_on.append((label, detail))
    stages.append(stage)
    return pool


def _row_label(row):
    return row.condition or row.variant or f"PPT {row.ppt_text or 'any'}"


def _window_text(row):
    """A grid row's dates in words: '01/11/2026–30/11/2026', 'from 01/12/2026', 'until 30/11/2026' or 'any date'."""
    if row.from_date and row.to_date:
        return f"{row.from_date:%d/%m/%Y}–{row.to_date:%d/%m/%Y}"
    if row.from_date:
        return f"from {row.from_date:%d/%m/%Y}"
    if row.to_date:
        return f"until {row.to_date:%d/%m/%Y}"
    return "any date"


def _narrow_by_date(pool, inception, failed_on, stages, products, insurer):
    """The date window, as Health's RULE H2 does it: a grid row applies when the policy's inception date is inside its
    from_date / to_date (both included, blank = no limit on that side). A blank or unreadable inception date leaves
    only the rows with no dates at all. A grid with no dates is never narrowed."""
    if inception is None:
        return _narrow(
            pool, lambda row: row.from_date is None and row.to_date is None, failed_on, stages, "date", DATE_LABEL,
            f"Inception date is blank/unparseable on this policy, and no remaining Life grid row of '{products}' for "
            f"'{insurer}' has an open (blank) date window.")
    windows = _listed(_distinct(_window_text(row) for row in pool), limit=6)
    return _narrow(
        pool,
        lambda row: ((row.from_date is None or row.from_date <= inception)
                     and (row.to_date is None or row.to_date >= inception)),
        failed_on, stages, "date", DATE_LABEL,
        f"Inception date {inception:%d/%m/%Y} falls outside the date window of every remaining Life grid row of "
        f"'{products}' for '{insurer}' (the grid states: {windows}).")


def match_life_policy(policy, grid):
    """Matches one policy to the live Life grid -> LifeMatch. `grid` is a LifeGrid (never None here)."""
    failed_on, stages = [], []

    def fail(stage, label, detail):
        failed_on.append((label, detail))
        stages.append(stage)

    # Only a first-year policy is read for its first-year payout.
    if fold(policy.policy_type) not in FIRST_YEAR_POLICY_TYPES:
        shown = text_of(policy.policy_type) or "(blank)"
        fail("policy_type", POLICY_TYPE_LABEL,
             f"Policy Type '{shown}' is not a first-year ('Fresh') policy, so no first-year rate applies — the Life "
             f"grid is read for first-year payouts only.")

    ppt, term, single_pay, data_errors = _read_terms(policy)

    insurer, detail = resolve_insurer(policy.insurer, grid)
    pool, how = [], ""
    if insurer is None:
        fail("insurer", INSURER_LABEL, detail)
    else:
        pool, how, stage, detail = resolve_plan(policy.plan, insurer, policy.insurer, grid)
        if detail:
            fail(stage, PLAN_LABEL, detail)

    if pool:
        products = _listed(_distinct(row.product for row in pool))
        pool = _narrow_by_date(pool, policy.inception, failed_on, stages, products, insurer)
        if ppt is not None:
            wordings = _listed(_distinct(row.ppt_text for row in pool), limit=8)
            shown = "single pay" if single_pay else f"{ppt}"
            pool = _narrow(
                pool, lambda row: lf.policy_ppt_matches(row.ppt, ppt, term, single_pay), failed_on, stages, "ppt",
                PPT_LABEL,
                f"PPT '{shown}' is not covered by any grid row of '{products}' for '{insurer}' "
                f"(the grid states PPT: {wordings or 'none'}).")
        if term is not None:
            wordings = _listed(_distinct(row.pt_text for row in pool), limit=8)
            pool = _narrow(
                pool, lambda row: lf.pt_matches(row.pt, str(term)), failed_on, stages, "pt", PT_LABEL,
                f"Policy term '{term}' is not covered by any remaining grid row of '{products}' for '{insurer}' "
                f"(the grid states PT: {wordings or 'none'}).")

    if data_errors:
        return LifeMatch("bad_data", "bad_data", failed_on, data_errors)
    if failed_on:
        return LifeMatch("no_match", stages[0], failed_on)

    # Every check passed: pool holds the rows that apply.
    pool = sorted(pool, key=lambda row: row.id)
    signature = {(row.yr1, row.deferred, row.condition, row.remarks) for row in pool}
    if len(signature) > 1:
        return LifeMatch("multiple", "multiple", rows=pool)
    chosen = pool[0]
    if chosen.yr1 is None:
        return LifeMatch("no_match", "rate", [(
            GRID_LABEL,
            f"Life grid row {chosen.id} ('{chosen.product}') has no first-year payout, so there is no rate to print — "
            f"check it on Life Rate Master.")])
    return LifeMatch("matched", row=chosen, tied=len(pool) if len(pool) > 1 else 0, how=how)


# ----------------------------------------------------------------------------------------------- what is printed
def addtnc_text(row):
    """The grid row's deferred part, condition and remarks as the text for the Addtnc column, or None.

    Printed, never applied: the deferred second-year part is a separate payment, and a condition or remark is a
    written rule the engine cannot evaluate, so it is flagged for a person ('Manual Review Required:')."""
    parts = []
    if row.deferred not in (None, 0):
        parts.append(f"Deferred (paid at 2nd year on persistency): {percent(row.deferred)}%")
    notes = []
    if row.condition:
        notes.append(f"Condition: {row.condition}")
    if row.remarks:
        notes.append(f"Remarks: {row.remarks}")
    if notes:
        parts.append("Manual Review Required: " + " | ".join(notes))
    return " | ".join(parts) or None


def matched_reason(match, grid):
    """The Failure Reason of a matched row: where the rate came from, so it can be traced and re-checked."""
    row = match.row
    where = f"{row.insurer} / {row.product}" + (f" / {row.variant}" if row.variant else "")
    reason = (
        f"Matched Successfully — Life grid {grid.label}: {where}, PPT {row.ppt_text or 'any'}, "
        f"PT {row.pt_text or 'any'} (grid row {row.id}; plan found by {'mapping' if match.how == 'mapping' else 'name'})."
    )
    if row.from_date or row.to_date:
        reason += f" The grid row applies {_window_text(row)}."
    if match.tied:
        reason += f" {match.tied} grid rows had identical payout, condition and remarks; used grid row {row.id} (lowest id)."
    return reason


def multiple_reason(match):
    """The Failure Reason of a MULTIPLE MATCHES row: the clashing rows named by what tells them apart."""
    listed = [f"{row.id} ({_row_label(row)}: {percent(row.yr1) or 'no payout'}%)" for row in match.rows[:6]]
    if len(match.rows) > 6:
        listed.append(f"… (+{len(match.rows) - 6} more)")
    ids = ", ".join(str(row.id) for row in match.rows[:10])
    if len(match.rows) > 10:
        ids += f" … (+{len(match.rows) - 10} more)"
    return (
        f"Multiple Life grid rows matched ({len(match.rows)} rows) and they differ in payout, condition or remarks: "
        f"{', '.join(listed)}. Refine the plan mapping or Life Rate Master so only one row applies. "
        f"Matching Group IDs: {ids}"
    )


# ----------------------------------------------------------------------------------------------- the screens
def grid_product_lines(text):
    """The non-blank lines of a grid_products text, stripped, without repeats."""
    return _distinct(line.strip() for line in str(text or "").splitlines())


def grid_product_problems(rows, text):
    """What is wrong with a mapping's grid_products text against one insurer's grid rows; [] when every line points at
    something. Matching ignores case and punctuation, so a typo would otherwise just never match, silently."""
    lines = grid_product_lines(text)
    if not lines:
        return ["Enter at least one grid product."]
    keys = {row.product_key for row in rows}
    problems = []
    for line in lines:
        if line.endswith("*"):
            prefix = fold(line[:-1])
            if not prefix:
                problems.append("A line that is only * would take every product; type the start of the name before it.")
            elif not any(key == prefix or key.startswith(prefix + " ") for key in keys):
                problems.append(f"No product of this insurer starts with '{line[:-1].strip()}'.")
        elif fold(line) not in keys:
            problems.append(f"'{line}' is not a product of this insurer in the live Life grid.")
    return problems


def suggestions_for(plan, insurer, grid):
    """The grid products nearest in spelling to a MIS plan name, as a hint on the screens."""
    if grid is None:
        return []
    return closest_products(_core_plan(fold(plan), insurer, ""), grid.rows.get(insurer, []))


def picker_products(grid):
    """{insurer: its distinct grid products, sorted} for the product picker of the plan mapping form."""
    if grid is None:
        return {}
    return {
        insurer: sorted(_distinct(row.product for row in rows), key=str.lower)
        for insurer, rows in sorted(grid.rows.items())
    }


def mapping_reach(grid, insurer, grid_products):
    """(grid products, grid rows) a mapping reaches in the live grid -- (0, 0) when it points at nothing."""
    if grid is None:
        return 0, 0
    rows = rows_for_mapping(grid.rows.get(insurer, []), grid_products)
    return len({row.product for row in rows}), len(rows)


def unmapped_from_failed_rows():
    """What the engine could not place, read back from the failed rows of processed MIS files, most policies first.

    -> (plans, insurers): plans = [{insurer, plan, plan_key, count}], insurers = [{name, name_key, count}], both
    without anything that has been mapped since."""
    reasons = MISFailedRow.objects.filter(
        Q(failure_reason__contains="Life Plan Mapping") | Q(failure_reason__contains="Life Insurer Aliases")
    ).order_by("id").values_list("failure_reason", flat=True)

    plan_counts, plan_names, insurer_counts, insurer_names = Counter(), {}, Counter(), {}
    for reason in reasons.iterator():
        for pattern in UNMAPPED_PLAN_PATTERNS:
            found = pattern.search(reason)
            if found:
                key = (found.group(2), fold(found.group(1)))
                plan_counts[key] += 1
                plan_names.setdefault(key, found.group(1))
                break
        found = UNKNOWN_INSURER_PATTERN.search(reason)
        if found:
            key = fold(found.group(1))
            insurer_counts[key] += 1
            insurer_names.setdefault(key, found.group(1))

    mapped_plans = set(LifePlanMapping.objects.values_list("insurer", "plan_key"))
    mapped_names = set(LifeInsurerAlias.objects.values_list("name_key", flat=True))
    plans = [
        {"insurer": insurer, "plan": plan_names[(insurer, plan_key)], "plan_key": plan_key, "count": count}
        for (insurer, plan_key), count in plan_counts.most_common() if (insurer, plan_key) not in mapped_plans
    ]
    insurers = [
        {"name": insurer_names[name_key], "name_key": name_key, "count": count}
        for name_key, count in insurer_counts.most_common() if name_key not in mapped_names
    ]
    return plans, insurers
