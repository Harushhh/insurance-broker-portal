"""The Life grid as ONE flat sheet for a dashboard: every payout of every plan is a row, and no cell is empty.

The Life Rate Master keeps one row per plan with 14 payout columns (seven BG years, seven MG years), most of them
empty, and several optional text columns (variant, condition, section, PT, category, remarks). That is the right shape
for editing and for the standard file you upload every month, and the wrong one for a dashboard: every empty cell
shows up as "(blank)" in a slicer or a pivot. This module turns an upload into the shape a dashboard wants, and the
Life Rate Master's Dashboard tab downloads it. Nothing is stored; it is read from the upload every time, so next
month's grid needs no extra work: upload it, make it live, download the sheet again.

One row = one plan in one year, as BG or as MG (``COLUMNS``):

  grid, plan_id, insurer, product, plan, ppt, payout_type, payout_year, year_order, payout_pct

How the gaps are handled, without a made-up value anywhere (the rules the user set on 2026-11-14):

  * **A year the grid gives no payout for has no row.** That removes the 14 mostly-empty payout columns.
  * **What tells two rows of one product apart is merged into the plan name**: variant, condition, section and the
    PT (``New Assured Wealth | PT 12``), so insurer + plan + ppt names exactly one plan (two that still collide get
    their row number at the end). ``product`` stays the plain product name for grouping.
  * **Columns that only some plans have are not in the sheet** (category, PT as a column, remarks): the grid states
    them for a minority, and a column with gaps is what the dashboard cannot take. They stay on the Life Rate Master
    and in its Export Excel.
  * **A plan the sheet cannot hold is left out and listed**, never filled in: a plan with no payout number (its row is
    a written rule such as "Year 1: 30% of 1st year commission"), and a plan whose PPT the grid does not state
    (its PPT cell is "-" or empty), because it cannot sit on a PPT axis.
  * **Two restatements of what the grid itself says, listed as restated**: a blank PPT whose Variant reads as a pay
    term by the Rate Checker's own rules (Digit Glow Plus: the workbook merges the Variant and PPT cells, so "Ltd pay
    5" is the PPT), and a blank PPT whose product name states the range ("ILP 7 - 9 ...", as its Tata siblings do).

``include_mg=False`` leaves the MG rows out, for a dashboard that partners or non-admins can open (MG is what the
insurer pays the broker: admin only).
"""
import re
from dataclasses import dataclass, field

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from ..models import LifeRate
from . import life_filters
from .life_rates import MG_HEADERS, PAYOUT_HEADERS

SHEET_NAME = "life_grid"
COLUMNS = ("grid", "plan_id", "insurer", "product", "plan", "ppt", "payout_type", "payout_year", "year_order",
           "payout_pct")
COLUMN_HELP = (
    ("grid", "The grid month: the upload's name", "Oct'26"),
    ("plan_id", "The Life Rate Master row ID (it changes with every upload)", "4191"),
    ("insurer", "The insurer", "Generali Central (FGI)"),
    ("product", "The plain product name, to group by", "New Assured Wealth"),
    ("plan", "The product plus what tells its rows apart (variant, condition, section, PT)", "New Assured Wealth | PT 12"),
    ("ppt", "The premium paying term as the grid writes it", "6"),
    ("payout_type", "BG = the partner payout, MG = what the insurer pays the broker (admin only)", "BG"),
    ("payout_year", "Year 1, At 2nd year, Year 2 to Year 5, Year 6+", "Year 1"),
    ("year_order", "1 to 7, to sort the years", "1"),
    ("payout_pct", "The payout in percent: 35.5 means 35.5%", "41"),
)

# (BG column, MG column, the year as the grid calls it, its order): the Rate Checker's own headers, so they never drift
YEARS = [(bg, mg, label, order) for order, ((bg, label), (mg, _)) in enumerate(zip(PAYOUT_HEADERS, MG_HEADERS), start=1)]
_TEXT_FIELDS = ("insurer", "product", "variant", "condition", "section", "ppt", "pt", "remarks")
_RANGE_IN_NAME = re.compile(r"(?<![\d.])(\d{1,2})\s*-\s*(\d{1,2})(?![\d.])")


@dataclass
class DashboardData:
    grid: str
    rows: list = field(default_factory=list)          # tuples in COLUMNS order
    plans: int = 0
    insurers: int = 0
    bg_rows: int = 0
    mg_rows: int = 0
    left_out: list = field(default_factory=list)       # [{plan_id, insurer, plan, reason, detail}]
    restated: list = field(default_factory=list)       # [{plan_id, insurer, plan, detail}]
    renamed: int = 0                                   # plans whose name was already taken and got their row number


def _text(value):
    """A cell's text on one line, None when there is none."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def plan_label(row, with_term=True):
    """The product with whatever tells its rows apart: variant, condition, section and the PT, joined with ' | '."""
    parts = [row["product"] or "-"]
    for name in ("variant", "condition", "section"):
        if row[name] is not None:
            parts.append(row[name])
    if with_term and row["pt"] is not None:
        parts.append(f"PT {row['pt']}")
    return " | ".join(parts)


def _restate_ppt(row):
    """For a row with no PPT: (the PPT the grid itself gives elsewhere, the variant to keep, what was done) or
    (None, variant, None) when it gives none."""
    variant = row["variant"]
    if variant is not None:
        spec = life_filters.ppt_spec(variant)
        if spec.modes or spec.spans:                    # the Variant reads as a pay term (Digit Glow Plus)
            return variant, None, f"'{variant}' was in the Variant column; it is now the PPT."
    found = _RANGE_IN_NAME.search(row["product"] or "")
    if found:                                          # "ILP 7 - 9 Param Raksha Life Pro"
        ppt = f"{found.group(1)}-{found.group(2)}"
        return ppt, variant, f"The product name states the pay term ({found.group(1)} - {found.group(2)}); it is now the PPT '{ppt}'."
    return None, variant, None


def _figures(row, columns):
    return "; ".join(f"{kind} {label} {value:g}%" for kind, cols in columns for col, label in cols
                     if (value := row[col]) is not None)


def dashboard_data(upload, include_mg=True):
    """Everything the one-sheet export holds for an upload (and what it leaves out), read from its stored rows."""
    fields = ("id", "position", *_TEXT_FIELDS, *LifeRate.PAYOUT_FIELDS, *LifeRate.MG_FIELDS)
    data = DashboardData(grid=upload.label)
    kept = []
    bg_cols = [(bg, label) for bg, _, label, _ in YEARS]
    mg_cols = [(mg, label) for _, mg, label, _ in YEARS]

    for record in LifeRate.objects.filter(upload=upload).order_by("position", "id").values(*fields):
        row = {name: (_text(value) if name in _TEXT_FIELDS else value) for name, value in record.items()}
        has_bg = any(row[bg] is not None for bg, _, _, _ in YEARS)
        any_mg = any(row[mg] is not None for _, mg, _, _ in YEARS)
        has_mg = include_mg and any_mg
        if row["product"] is None:                      # nothing to name the plan by, and no name is made up
            data.left_out.append({
                "plan_id": row["id"], "insurer": row["insurer"], "plan": plan_label(row), "reason": "No product name",
                "detail": "The row has no product name, so no plan name can be built for it."})
            continue
        if not has_bg and not has_mg:
            data.left_out.append({
                "plan_id": row["id"], "insurer": row["insurer"], "plan": plan_label(row),
                "reason": "No BG payout" if any_mg else "No payout number",
                "detail": ("The grid gives only an MG payout for this plan, which this export leaves out."
                           if any_mg else f"The grid gives a written rule and no number: {row['remarks'] or 'no text either'}")})
            continue
        if row["ppt"] is None:
            ppt, variant, note = _restate_ppt(row)
            if ppt is None:
                data.left_out.append({
                    "plan_id": row["id"], "insurer": row["insurer"], "plan": plan_label(row), "reason": "No PPT stated",
                    "detail": "The grid states no PPT for this plan (its PPT cell is '-' or empty), so it cannot sit on a "
                              "PPT axis. Its payouts: " + _figures(row, [("BG", bg_cols)] + ([("MG", mg_cols)] if include_mg else []))
                              + "."})
                continue
            row["ppt"], row["variant"] = ppt, variant
            data.restated.append({"plan_id": row["id"], "insurer": row["insurer"], "plan": plan_label(row), "detail": note})
        row["plan"] = plan_label(row)
        kept.append(row)

    taken = {}
    for row in kept:
        taken.setdefault((row["insurer"], row["plan"], row["ppt"]), []).append(row)
    for group in taken.values():
        if len(group) > 1:                              # two plans with one name would be averaged together on a chart
            for row in group:
                row["plan"] = f"{row['plan']} | row {row['id']}"
            data.renamed += len(group)

    for row in kept:
        for bg, mg, label, order in YEARS:
            for kind, column in (("BG", bg), ("MG", mg)):
                if kind == "MG" and not include_mg:
                    continue
                value = row[column]
                if value is None:
                    continue
                data.rows.append((upload.label, row["id"], row["insurer"], row["product"], row["plan"], row["ppt"], kind,
                                  label, order, float(value)))
                if kind == "BG":
                    data.bg_rows += 1
                else:
                    data.mg_rows += 1
    data.plans = len({r[1] for r in data.rows})
    data.insurers = len({r[2] for r in data.rows})
    return data


def dashboard_workbook(data):
    """The one-sheet workbook: a single Excel table with a header row, nothing above it and no empty cell."""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(list(COLUMNS))
    for row in data.rows:
        ws.append(list(row))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    if data.rows:                                      # a table needs at least one data row
        last = f"{get_column_letter(len(COLUMNS))}{len(data.rows) + 1}"
        table = Table(displayName=f"tbl_{SHEET_NAME}", ref=f"A1:{last}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(table)
    ws.freeze_panes = "A2"
    widths = {"grid": 10, "plan_id": 9, "insurer": 28, "product": 36, "plan": 54, "ppt": 16, "payout_type": 12,
              "payout_year": 13, "year_order": 11, "payout_pct": 11}
    for index, name in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(index)].width = widths[name]
    return wb
