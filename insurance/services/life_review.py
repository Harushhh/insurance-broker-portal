"""The review of a stored Life upload against the live grid: what pressing Make live on it would change.

An upload made from the standard file (``services/life_standard.py``) is stored but not live
(``LifeGridUpload.is_pending``). The standard file proves the columns are right (they are found by name) but not that
the numbers are: a value pasted into the wrong year column still passes. So before it goes live a person sees what is
different from the live grid, and a few checks that need no knowledge of any insurer's layout:

  * **The difference**: rows added, removed and changed (which payout, from what to what). A row's identity is its
    insurer, section, category, product, variant, PPT, PT and condition (case and spacing ignored); rows that are
    identical on both sides are matched first, so a reordered grid shows no changes, and several rows with one
    identity are paired in order.
  * **By insurer**: rows before and after, and what looks wrong: an insurer the upload no longer has, one that lost a
    quarter of its rows, a new one, or one spelled differently from the live grid (the insurer aliases and plan
    mappings use the live spelling).
  * **Checks**: BG payouts above their own MG, BG payouts that moved by 10 points or more, PPT / PT wordings the
    Rate Checker's rules cannot read (they would apply to every PPT / PT), plan mappings and insurer aliases that
    would stop reaching the grid, date windows of one plan that overlap with different payouts, rows already past
    their end date or not started yet, rows with no first-year payout, and rows that are only a written rule.

Nothing here writes: ``review_upload`` only reads the two uploads. Pressing Make live is the existing
``activate_life_upload``.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.utils import timezone

from ..models import LifeInsurerAlias, LifePlanMapping, LifeRate
from . import life_filters
from .life_mis import fold, parse_grid_products
from .life_rates import DROP_WARNING_RATIO, MG_HEADERS, PAYOUT_HEADERS

BIG_MOVE_POINTS = 10          # a BG payout that moves by this many percentage points (or more) is worth a second look
EXAMPLE_LIMIT = 8             # examples listed under a check
BG_FIELDS = tuple(LifeRate.PAYOUT_FIELDS)
MG_FIELDS = tuple(LifeRate.MG_FIELDS)
FIELD_LABELS = {**{column: f"{header} BG" for column, header in PAYOUT_HEADERS},
                **{column: f"{header} MG" for column, header in MG_HEADERS}}
KEY_FIELDS = ("insurer", "section", "category", "product", "variant", "ppt", "pt", "condition")
DATE_FIELDS = ("from_date", "to_date")          # not part of a row's identity: a changed window shows as a change
DATE_LABELS = {"from_date": "Valid from", "to_date": "Valid to"}
ROW_FIELDS = ("id", "position", "remarks", *KEY_FIELDS, *DATE_FIELDS, *BG_FIELDS, *MG_FIELDS)
KIND_ORDER = {"changed": 0, "added": 1, "removed": 2}


@dataclass
class Change:
    kind: str                  # "changed", "added" or "removed"
    row: dict                  # the row as it is in this upload (a removed row: as it is in the live grid)
    details: list = field(default_factory=list)      # changed: [(what, was, now)]
    big: bool = False          # a BG payout moved by BIG_MOVE_POINTS or more
    old: dict = None           # changed: the row as it is in the live grid

    @property
    def summary(self):
        """An added or removed row in a few words: its year-1 BG (and MG, when it has one)."""
        parts = [f"year 1 BG {_fmt(self.row['payout_yr1'])}"]
        if self.row["mg_yr1"] is not None:
            parts.append(f"year 1 MG {_fmt(self.row['mg_yr1'])}")
        if self.row["from_date"] is not None or self.row["to_date"] is not None:
            parts.append(f"valid {_window_words(self.row)}")
        return ", ".join(parts)


@dataclass
class Check:
    level: str                 # "warn" (look before going live) or "info"
    title: str
    summary: str
    examples: list = field(default_factory=list)
    count: int = 0


@dataclass
class Review:
    upload: object
    live: object               # the live LifeGridUpload, None when there is none yet
    insurers: list            # [{name, before, after, added, removed, changed, unchanged, flags: [(level, text)]}]
    changes: list              # [Change], by insurer, then in the upload's row order
    totals: dict
    checks: list

    @property
    def attention(self):
        """How many things deserve a look: warn-level checks and insurers with a warn-level flag."""
        flagged = sum(1 for row in self.insurers if any(level == "warn" for level, _ in row["flags"]))
        return sum(1 for check in self.checks if check.level == "warn") + flagged


# ----------------------------------------------------------------------------------------------- the difference
def load_rows(upload):
    """Every stored row of an upload as a dict, in workbook order."""
    return list(LifeRate.objects.filter(upload=upload).order_by("position", "id").values(*ROW_FIELDS))


def _norm(text):
    return " ".join(str(text or "").lower().split())


insurer_key = _norm            # an insurer's name with case and spacing ignored (how this module tells insurers apart)


def _row_key(row):
    return tuple(_norm(row[name]) for name in KEY_FIELDS)


def _same_number(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1e-9


def _signature(row):
    """What must be equal for two rows with the same identity to count as unchanged."""
    return (*(None if row[name] is None else round(row[name], 6) for name in (*BG_FIELDS, *MG_FIELDS)),
            _norm(row["remarks"]), row["from_date"], row["to_date"])


def _fmt(value):
    return "blank" if value is None else f"{value:g}"


def _fmt_date(value):
    return "blank" if value is None else f"{value:%d/%m/%Y}"


def _details(old, new):
    out = [(FIELD_LABELS[name], _fmt(old[name]), _fmt(new[name]))
           for name in (*BG_FIELDS, *MG_FIELDS) if not _same_number(old[name], new[name])]
    out += [(DATE_LABELS[name], _fmt_date(old[name]), _fmt_date(new[name]))
            for name in DATE_FIELDS if old[name] != new[name]]
    if _norm(old["remarks"]) != _norm(new["remarks"]):
        out.append(("Remarks", old["remarks"] or "blank", new["remarks"] or "blank"))
    return out


def _moved_a_lot(old, new):
    return any(old[name] is not None and new[name] is not None and abs(new[name] - old[name]) >= BIG_MOVE_POINTS
               for name in BG_FIELDS)


def diff_rows(live_rows, new_rows):
    """(changes, unchanged) between the live rows and the rows of an upload (both as load_rows gives them)."""
    old_by, new_by = defaultdict(list), defaultdict(list)
    for row in live_rows:
        old_by[_row_key(row)].append(row)
    for row in new_rows:
        new_by[_row_key(row)].append(row)
    changes, unchanged = [], 0
    for key in [*new_by, *(k for k in old_by if k not in new_by)]:
        olds, news = old_by.get(key, []), new_by.get(key, [])
        pool = defaultdict(list)
        for old in olds:
            pool[_signature(old)].append(old)
        left_new = []
        for new in news:
            bucket = pool.get(_signature(new))
            if bucket:
                bucket.pop(0)                                  # identical on both sides: nothing changed
                unchanged += 1
            else:
                left_new.append(new)
        left_old = sorted((old for bucket in pool.values() for old in bucket), key=lambda old: old["position"])
        paired = min(len(left_old), len(left_new))
        for old, new in zip(left_old, left_new):
            changes.append(Change("changed", new, _details(old, new), _moved_a_lot(old, new), old))
        changes.extend(Change("added", new) for new in left_new[paired:])
        changes.extend(Change("removed", old) for old in left_old[paired:])
    return changes, unchanged


# ----------------------------------------------------------------------------------------------- by insurer
def _insurer_table(live_rows, new_rows, changes, have_live):
    """One line per insurer (in the upload's order, then the insurers only the live grid has)."""
    spelled_new, spelled_live = {}, {}
    for row in new_rows:
        spelled_new.setdefault(_norm(row["insurer"]), row["insurer"])
    for row in live_rows:
        spelled_live.setdefault(_norm(row["insurer"]), row["insurer"])
    order = [*spelled_new, *(key for key in spelled_live if key not in spelled_new)]
    before = Counter(_norm(row["insurer"]) for row in live_rows)
    after = Counter(_norm(row["insurer"]) for row in new_rows)
    counts = {key: Counter() for key in order}
    for change in changes:
        counts[_norm(change.row["insurer"])][change.kind] += 1

    only_new = [key for key in spelled_new if key not in spelled_live]
    only_live = [key for key in spelled_live if key not in spelled_new]
    twin_of_new = {new: live for new in only_new for live in only_live if fold(new) == fold(live)}
    twin_of_live = {live: new for new, live in twin_of_new.items()}

    table = []
    for key in order:
        name = spelled_new.get(key) or spelled_live[key]
        flags = []
        if have_live:
            if key in twin_of_new:
                flags.append(("warn", f"Looks like '{spelled_live[twin_of_new[key]]}' in the live grid, spelled "
                                      f"differently: the insurer aliases and plan mappings use that spelling"))
            elif key in twin_of_live:
                flags.append(("warn", f"Not in this upload, but '{spelled_new[twin_of_live[key]]}' looks like the "
                                      f"same insurer"))
            elif not after[key]:
                flags.append(("warn", f"Not in this upload ({before[key]} rows in the live grid)"))
            elif not before[key]:
                flags.append(("info", "New insurer"))
            elif after[key] < before[key] * DROP_WARNING_RATIO:
                flags.append(("warn", f"Down from {before[key]} to {after[key]} rows"))
            if key in spelled_new and key in spelled_live and spelled_new[key] != spelled_live[key]:
                flags.append(("warn", f"Spelled '{spelled_new[key]}' here and '{spelled_live[key]}' in the live "
                                      f"grid: the insurer aliases and plan mappings use the live spelling"))
        row = {"name": name, "before": before[key], "after": after[key],
               "added": counts[key]["added"], "removed": counts[key]["removed"], "changed": counts[key]["changed"],
               "flags": flags}
        row["unchanged"] = after[key] - row["added"] - row["changed"]
        table.append(row)
    return table, order


# ----------------------------------------------------------------------------------------------- the checks
def _describe(row):
    parts = [row["insurer"], row["product"] or "no product"]
    for label, name in (("", "variant"), ("PPT ", "ppt"), ("PT ", "pt"), ("", "condition")):
        if row[name]:
            parts.append(f"{label}{row[name]}")
    return " | ".join(parts)


def _check_bg_above_mg(new_rows):
    found = []
    for row in new_rows:
        for bg, mg in zip(BG_FIELDS, MG_FIELDS):
            if row[bg] is not None and row[mg] is not None and row[bg] > row[mg] + 1e-9:
                found.append(f"{_describe(row)}: {FIELD_LABELS[bg]} {_fmt(row[bg])} is above {FIELD_LABELS[mg]} "
                             f"{_fmt(row[mg])}")
                break
    if not found:
        return None
    return Check("warn", "BG payout above its own MG",
                 f"{len(found)} rows pay the partner more than the insurer pays the broker. That is usually a value in "
                 f"the wrong column.", found[:EXAMPLE_LIMIT], len(found))


def _check_big_moves(changes):
    moved = [change for change in changes if change.big]
    if not moved:
        return None
    examples = []
    for change in moved[:EXAMPLE_LIMIT]:
        moves = [f"{what} {was} -> {now}" for what, was, now in change.details if what.endswith("BG")]
        examples.append(f"{_describe(change.row)}: {'; '.join(moves)}")
    return Check("warn", f"BG payout moved by {BIG_MOVE_POINTS} points or more",
                 f"{len(moved)} changed rows have a BG payout that moved by {BIG_MOVE_POINTS} percentage points or more.",
                 examples, len(moved))


def _check_unreadable_wording(new_rows):
    ppt, pt = Counter(), Counter()
    for row in new_rows:
        if life_filters.ppt_unreadable(row["ppt"]):
            ppt[(row["insurer"], row["ppt"])] += 1
        if life_filters.pt_unreadable(row["pt"]):
            pt[(row["insurer"], row["pt"])] += 1
    if not ppt and not pt:
        return None
    examples = [f"{insurer}: PPT '{text}' ({n} rows)" for (insurer, text), n in ppt.most_common(EXAMPLE_LIMIT)]
    examples += [f"{insurer}: PT '{text}' ({n} rows)" for (insurer, text), n in pt.most_common(EXAMPLE_LIMIT)]
    count = sum(ppt.values()) + sum(pt.values())
    return Check("warn", "PPT or PT wording the Rate Checker cannot read",
                 "These have no number and none of the words the rules know, so they would apply to every PPT / PT: "
                 "the Rate Checker and the MIS engine could show or use them for a policy they are not meant for.",
                 examples[:EXAMPLE_LIMIT], count)


def _product_keys(rows):
    keys = defaultdict(set)
    for row in rows:
        keys[row["insurer"]].add(fold(row["product"]))
    return keys


def _reach(keys, grid_products):
    exact, prefixes = parse_grid_products(grid_products)
    return sum(1 for key in keys if key in exact or any(key == p or key.startswith(p + " ") for p in prefixes))


def _check_mappings(live_rows, new_rows):
    live_keys, new_keys = _product_keys(live_rows), _product_keys(new_rows)
    broken, shrunk = [], []
    for mapping in LifePlanMapping.objects.order_by("insurer", "plan_name"):
        before = _reach(live_keys.get(mapping.insurer, ()), mapping.grid_products)
        after = _reach(new_keys.get(mapping.insurer, ()), mapping.grid_products)
        if before and not after:
            broken.append(f"{mapping.insurer}: the plan '{mapping.plan_name}' reached {before} product(s) in the live "
                          f"grid and reaches none here")
        elif after < before:
            shrunk.append(f"{mapping.insurer}: the plan '{mapping.plan_name}' reached {before} products and now reaches "
                          f"{after}")
    checks = []
    if broken:
        checks.append(Check("warn", "Plan mappings that would stop working",
                            f"{len(broken)} mapped MIS plans point at grid products this upload does not have. Their "
                            f"policies would end as NO MATCH until the mapping is changed.",
                            broken[:EXAMPLE_LIMIT], len(broken)))
    if shrunk:
        checks.append(Check("info", "Plan mappings that reach fewer products",
                            f"{len(shrunk)} mapped MIS plans lose some of the products they reach.",
                            shrunk[:EXAMPLE_LIMIT], len(shrunk)))
    return checks


def _check_aliases(live_rows, new_rows):
    live_insurers, new_insurers = {row["insurer"] for row in live_rows}, {row["insurer"] for row in new_rows}
    lost = defaultdict(list)
    for alias in LifeInsurerAlias.objects.order_by("insurer", "mis_name"):
        if alias.insurer in live_insurers and alias.insurer not in new_insurers:
            lost[alias.insurer].append(alias.mis_name)
    if not lost:
        return None
    examples = [f"{insurer}: {len(names)} MIS spelling(s), for example '{names[0]}'" for insurer, names in lost.items()]
    return Check("warn", "Insurer aliases that would stop working",
                 f"{len(lost)} insurers that have MIS spellings mapped to them are not in this upload, so the "
                 f"policies of these insurers would end as NO MATCH.", examples[:EXAMPLE_LIMIT], len(lost))


def _check_no_year1(new_rows):
    found = [row for row in new_rows
             if row["payout_yr1"] is None and any(row[name] is not None for name in BG_FIELDS)]
    if not found:
        return None
    return Check("info", "Rows with no first-year BG payout",
                 f"{len(found)} rows have a payout for a later year but none for year 1. The MIS engine prints the "
                 f"first-year payout, so a policy that matches one of them ends as NO MATCH.",
                 [_describe(row) for row in found[:EXAMPLE_LIMIT]], len(found))


def _window_words(row):
    """A row's dates in words: '01/11/2026-30/11/2026', 'from 01/12/2026', 'until 30/11/2026' or 'any date'."""
    start, end = row["from_date"], row["to_date"]
    if start and end:
        return f"{start:%d/%m/%Y}-{end:%d/%m/%Y}"
    if start:
        return f"from {start:%d/%m/%Y}"
    if end:
        return f"until {end:%d/%m/%Y}"
    return "any date"


def _windows_overlap(a, b):
    """Whether two rows' date windows share a day (both ends included, None = no limit on that side)."""
    return ((a["from_date"] is None or b["to_date"] is None or a["from_date"] <= b["to_date"])
            and (b["from_date"] is None or a["to_date"] is None or b["from_date"] <= a["to_date"]))


def _check_overlapping_windows(new_rows):
    """Rows of one plan (same identity) whose date windows overlap while their payouts differ: a policy dated in the
    overlap matches both. Only pairs where a date is involved are looked at, so rows that were already duplicates
    without any date are not reported here."""
    groups = defaultdict(list)
    for row in new_rows:
        groups[_row_key(row)].append(row)
    found = []
    for rows in groups.values():
        for index, a in enumerate(rows):
            for b in rows[index + 1:]:
                dated = any(row[name] is not None for row in (a, b) for name in DATE_FIELDS)
                if (dated and _windows_overlap(a, b)
                        and tuple(a[name] for name in BG_FIELDS) != tuple(b[name] for name in BG_FIELDS)):
                    found.append(f"{_describe(a)}: {_window_words(a)} and {_window_words(b)} overlap, and the payouts "
                                 f"differ")
    if not found:
        return None
    return Check("warn", "Date windows of one plan that overlap",
                 f"{len(found)} pairs of rows for the same plan apply on a common day with different payouts: a policy "
                 f"dated in the overlap matches both, and the MIS engine ends it as MULTIPLE MATCHES. Give the old row "
                 f"an end date before the new row's start date.", found[:EXAMPLE_LIMIT], len(found))


def _check_dates_vs_today(new_rows, today):
    checks = []
    ended = [row for row in new_rows if row["to_date"] is not None and row["to_date"] < today]
    if ended:
        checks.append(Check(
            "info", "Rows already past their end date",
            f"{len(ended)} rows ended before today ({today:%d/%m/%Y}). The MIS engine will not use them for a policy "
            f"incepted after their end date, and the Rate Checker shows them only when no date is typed or the date "
            f"typed is inside their window.",
            [f"{_describe(row)}: {_window_words(row)}" for row in ended[:EXAMPLE_LIMIT]], len(ended)))
    ahead = [row for row in new_rows if row["from_date"] is not None and row["from_date"] > today]
    if ahead:
        checks.append(Check(
            "info", "Rows that start in the future",
            f"{len(ahead)} rows start after today ({today:%d/%m/%Y}), so a policy incepted today does not match them yet.",
            [f"{_describe(row)}: {_window_words(row)}" for row in ahead[:EXAMPLE_LIMIT]], len(ahead)))
    return checks


def _check_rule_only(new_rows):
    found = [row for row in new_rows
             if all(row[name] is None for name in BG_FIELDS) and (row["remarks"] or "").strip()]
    if not found:
        return None
    return Check("info", "Rows that are a written rule, with no payout number",
                 f"{len(found)} rows hold a rule in remarks and no BG payout. The Rate Checker shows the rule; the MIS "
                 f"engine cannot use such a row, so a policy that matches one ends as NO MATCH.",
                 [f"{_describe(row)}: {row['remarks'][:80]}" for row in found[:EXAMPLE_LIMIT]], len(found))


# ----------------------------------------------------------------------------------------------- the review
def review_upload(upload, live, today=None):
    """What Make live on `upload` would change from `live` (the live LifeGridUpload, or None when nothing is live).
    `today` is the date the rows' dates are read against (the current date unless a test says otherwise)."""
    today = today or timezone.localdate()
    new_rows = load_rows(upload)
    live_rows = load_rows(live) if live is not None else []
    have_live = live is not None
    changes, unchanged = diff_rows(live_rows, new_rows) if have_live else ([], 0)

    insurers, order = _insurer_table(live_rows, new_rows, changes, have_live)
    position = {key: index for index, key in enumerate(order)}
    changes.sort(key=lambda c: (position[_norm(c.row["insurer"])], c.kind == "removed", c.row["position"]))

    up = down = 0                                          # year-1 BG payouts that rose / fell among the changed rows
    for change in changes:
        if change.kind == "changed":
            was, now = change.old["payout_yr1"], change.row["payout_yr1"]
            if was is not None and now is not None and not _same_number(was, now):
                up, down = (up + 1, down) if now > was else (up, down + 1)
    kinds = Counter(change.kind for change in changes)
    totals = {"before": len(live_rows), "after": len(new_rows), "unchanged": unchanged,
              "changed": kinds["changed"], "added": kinds["added"], "removed": kinds["removed"],
              "up": up, "down": down, "big": sum(1 for change in changes if change.big)}

    checks = [check for check in (
        _check_big_moves(changes) if have_live else None,
        _check_bg_above_mg(new_rows),
        _check_unreadable_wording(new_rows),
        *(_check_mappings(live_rows, new_rows) if have_live else ()),
        _check_aliases(live_rows, new_rows) if have_live else None,
        _check_overlapping_windows(new_rows),
        *_check_dates_vs_today(new_rows, today),
        _check_no_year1(new_rows),
        _check_rule_only(new_rows),
    ) if check is not None]
    return Review(upload=upload, live=live, insurers=insurers, changes=changes, totals=totals, checks=checks)
