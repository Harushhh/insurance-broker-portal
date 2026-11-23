"""Reads the Life working workbook (``Life_Grid_working-<Mon'YY>.xlsx``) into rows with both parts of each payout.

The workbook always has the same layout: one sheet per insurer, each with a "Final MG" block (what the insurer pays
the broker) and, beside it, a "Final BG" block (what the broker pays the partner). Both are read, as the workbook's own
formulas calculated them (``data_only``), so the portal shows exactly what the workbook shows: the BG payouts
(``y1``, ``d``, ``y2`` ... ``y6``) and, on the same row of the sheet, the Final MG they come from (``m_y1`` ...
``m_y6``). The margin and share columns are never read. MG is for the admin pages only: the Rate Checker never shows it.

Each insurer sheet has a spec below: its tables are found by the header text in a fixed column (so rows added above a
table just move it down), the label columns (category, product, variant, PPT, PT, ...) and the payout columns are
fixed letters; the Final MG block sits a fixed number of columns to the left of the BG block (``mg_offset``), with the
same columns in the same order (a few sheets list their MG columns by hand). A sheet whose table header (the anchor
text in its column) is not where the spec expects it stops the whole read with a message naming the sheet, the column
and the text it expected, and every broken sheet is named at once. That anchor is the only thing checked, though: the
other columns are read by letter, so a column inserted inside a table is read wrongly without a warning. The layout-
independent way in is the standard file (``services/life_standard.py``), whose columns are found by name.

``read_life_workbook`` returns the rows (dicts), the sheet notes and conditions per insurer, and warnings.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

PAYOUT_FIELDS = ("y1", "d", "y2", "y3", "y4", "y5", "y6")
MG_FIELDS = tuple(f"m_{name}" for name in PAYOUT_FIELDS)          # the Final MG of the same year ("m_y1" ...)
FIELD_LABELS = {
    "y1": "Year 1", "d": "At 2nd year", "y2": "Year 2", "y3": "Year 3",
    "y4": "Year 4", "y5": "Year 5", "y6": "Year 6+",
}
LEVELS = ("category", "product", "variant")        # a new higher-level label clears the lower ones
IGNORED_SHEETS = {"Ratemas", "Sheet3"}
GAP_LIMIT = 10                     # this many rows in a row without a payout end a table (a stray cell far below)
NO_VALUE = {"", "-", "–", "—", "na", "n/a", "nil", "none"}
TITLE_RX = re.compile(
    r"^(final (mg|bg)|mg as per insurer.*|insurer mg|ins|product|ppt|pt|fresh|renewals?|year \d.*|%)$", re.I)


class LifeLayoutError(ValueError):
    """The workbook does not match the layout this reader expects."""


@dataclass
class Table:
    anchor: tuple                      # (column, (header texts...)): the row holding this table's header
    labels: dict                       # role -> column letter (category, product, variant, ppt, pt, condition)
    payouts: dict                      # column letter -> (fields "y1,y2", header label)
    skip: int = 1                      # rows from the anchor row to the first data row
    section: str = ""
    unit: float = 100.0                # workbook fraction -> percent
    carry: tuple = LEVELS              # label roles that continue down blank cells
    reset_category_on_product: bool = False
    remark_cols: dict = field(default_factory=dict)    # column -> prefix: text appended to the row's remarks
    rule_cols: dict = field(default_factory=dict)      # column -> prefix: text that makes a row with no number
    overflow: dict = field(default_factory=dict)       # column -> label: renewal years the row has no field for
    headings: dict = None              # TATA: short label-only rows that set category / variant for what follows
    hook: str = ""
    stop: tuple = ()                   # ((column, text start), ...): the first row that says this ends the table
    keep_text_tail: bool = False       # LIC: written rules after the last number are rows, not notes
    mg_offset: int = 0                 # the Final MG block is this many columns left of the BG block (same columns)
    mg: dict = field(default_factory=dict)             # BG payout column -> the Final MG column of the same row

    def __post_init__(self):
        if self.mg_offset and not self.mg:
            self.mg = {col: get_column_letter(column_index_from_string(col) - self.mg_offset) for col in self.payouts}


@dataclass
class SheetSpec:
    name: str
    insurer: str
    bg: tuple                          # first / last column of the Final BG block (where its own notes live)
    tables: list
    notes_side: str = "auto"           # "auto": BG block text if the row has any, else the left block; or "left"
    mg_offset: int = 0                 # its tables' Final MG block sits this many columns left of the BG block

    def __post_init__(self):
        for table in self.tables:
            if self.mg_offset and not table.mg and not table.mg_offset:
                table.mg_offset = self.mg_offset
                table.__post_init__()


@dataclass
class LifeWorkbook:
    rows: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)          # insurer -> text
    warnings: list = field(default_factory=list)
    sheet_counts: dict = field(default_factory=dict)   # insurer -> rows read


def _p(fields, label):
    return (fields, label)


Y1 = _p("y1", "Year 1")
DEF = _p("d", "At 2nd year")
Y2 = _p("y2", "Year 2")
Y3 = _p("y3", "Year 3")
Y4 = _p("y4", "Year 4")
Y5 = _p("y5", "Year 5")
Y6 = _p("y6", "Year 6+")
RENEWALS = _p("y2,y3,y4,y5,y6", "Renewals")
CAT = ("Catogory", "Category")

TATA_HEADINGS = {
    "protection": "category", "ilp": "category", "par": "category", "non-par": "category", "non par": "category",
    "ulip": "category", "pension": "category", "health": "category", "term": "category",
}

SPECS = [
    SheetSpec("TATA", "Tata AIA Life", ("X", "AC"), notes_side="left", tables=[
        # Final MG 1st / 2nd year, offline (P, Q) and digital (S, T): the columns are not a fixed offset from the BG ones
        Table(("X", ("Product",)), {"product": "X"},
              {"Y": Y1, "Z": DEF, "AA": Y1, "AB": DEF}, headings=TATA_HEADINGS, hook="tata",
              remark_cols={"R": "", "AC": ""}, mg={"Y": "P", "Z": "Q", "AA": "S", "AB": "T"}),
    ]),
    SheetSpec("Bajaj", "Bajaj Allianz Life", ("AB", "AM"), mg_offset=14, tables=[
        Table(("AB", ("Category",)), {"category": "AB", "product": "AC", "variant": "AD", "ppt": "AE"},
              {"AF": Y1, "AG": DEF, "AH": Y2, "AI": Y3, "AJ": Y4, "AK": Y5, "AL": Y6}, remark_cols={"AM": ""}),
    ]),
    SheetSpec("Shriram", "Shriram Life", ("X", "AG"), mg_offset=12, tables=[
        Table(("X", ("Ins",)), {"product": "B", "pt": "C", "ppt": "D"},
              {"AB": Y1, "AC": Y2, "AD": Y3, "AE": Y4, "AF": Y5, "AG": Y6}),
    ]),
    SheetSpec("Digit", "Digit Life", ("L", "T"), mg_offset=11, tables=[
        Table(("L", ("Ins",)), {"product": "B", "variant": "C", "ppt": "D"},
              {"P": Y1, "Q": Y2, "R": Y3, "S": Y4, "T": _p("y5,y6", "Year 5+")}, section="Digit ICON / Glow Plus"),
        Table(("L", ("Ins",)), {"product": "B", "variant": "C", "ppt": "D"}, {"P": Y1}, section="Annuity (Sukoon)"),
        Table(("L", ("Ins",)), {"product": "B", "variant": "C", "ppt": "D"},
              {"P": Y1, "Q": Y2, "R": Y3, "S": Y4, "T": Y5}, section="Annuity (Boss)"),
    ]),
    SheetSpec("Axa", "Bharti AXA Life", ("K", "Q"), mg_offset=9, tables=[
        Table(("B", ("Category",)), {"category": "B", "product": "C", "ppt": "D"}, {"N": Y1}, section="First year"),
        Table(("B", ("Category",)), {"category": "B", "product": "C", "ppt": "D"},
              {"N": _p("y2,y3", "Year 2 & 3"), "O": _p("y4,y5,y6", "Year 4 till PPT ends")},
              skip=2, section="Renewal"),
        Table(("B", ("Product Name",)), {"product": "B", "ppt": "C"},
              {"M": Y1, "N": Y2, "O": Y3, "P": Y4, "Q": _p("y5,y6", "Year 5 till PPT ends")},
              carry=("product",), skip=2, section="Insta Gain"),
        Table(("B", ("Product Name",)), {"product": "B", "ppt": "C"},
              {"M": Y1, "N": _p("y2,y3", "Year 2 & 3"), "P": _p("y4,y5,y6", "Year 4 till PPT ends")},
              carry=("product",), skip=2, section="DSP / GSP", stop=(("B", "5."),)),
    ]),
    SheetSpec("HDFC", "HDFC Life", ("S", "Y"), mg_offset=9, tables=[
        Table(("S", CAT), {"category": "S", "product": "T", "ppt": "U", "pt": "V", "condition": "W"},
              {"X": Y1, "Y": RENEWALS}),
    ]),
    SheetSpec("India_First", "IndiaFirst Life", ("G", "AB"), mg_offset=6, tables=[
        Table(("A", ("Products",)), {"product": "A", "ppt": "B", "pt": "C"}, {"J": Y1}, section="First year",
              carry=("product",)),
        # the renewal table has its own Final MG block (A-N, "Renewal MG") 15 columns left of its BG block (P-AB)
        Table(("A", ("Products",)), {"product": "A", "ppt": "B", "pt": "C"},
              {"S": Y2, "T": Y3, "U": Y4, "V": Y5, "W": Y6}, skip=2, section="Renewal", carry=("product",),
              overflow={"X": "Year 7", "Y": "Year 8", "Z": "Year 9", "AA": "Year 10", "AB": "Year 11+"},
              mg_offset=15),
    ]),
    SheetSpec("Kotak", "Kotak Mahindra Life", ("M", "V"), mg_offset=11, tables=[
        Table(("B", ("Product",)), {"category": "A", "product": "B", "ppt": "C", "pt": "D"},
              {"P": Y1, "Q": DEF, "R": Y2, "S": Y3, "T": Y4, "U": Y5, "V": Y6}, reset_category_on_product=True),
    ]),
    SheetSpec("ABLI", "Aditya Birla Sun Life (ABSLI)", ("N", "R"), mg_offset=8, tables=[
        Table(("A", ("Segment",)), {"category": "A", "product": "B", "ppt": "C"}, {"Q": Y1, "R": DEF}),
    ]),
    SheetSpec("Ageas Federal", "Ageas Federal Life", ("L", "O"), mg_offset=5, tables=[
        Table(("A", CAT), {"category": "A", "product": "B", "ppt": "C"}, {"O": Y1}),
    ]),
    SheetSpec("ICICI_Pru", "ICICI Prudential Life", ("R", "Z"), mg_offset=8, tables=[
        Table(("A", CAT), {"category": "A", "product": "B", "ppt": "C"}, {"U": Y1, "V": DEF}, skip=2),
    ]),
    SheetSpec("AxisMax", "Axis Max Life", ("H", "L"), mg_offset=7, tables=[
        Table(("A", CAT), {"category": "A", "product": "B", "ppt": "C", "pt": "D"}, {"L": Y1},
              rule_cols={"J": "Year 1"}),
    ]),
    SheetSpec("Generali(FGI)", "Generali Central (FGI)", ("K", "R"), mg_offset=10, tables=[
        Table(("A", ("Product Name",)), {"product": "A", "category": "B", "ppt": "C", "pt": "D"},
              {"O": Y1, "P": _p("y2,y3", "Year 2 & 3"), "Q": _p("y4,y5", "Year 4 & 5 onwards"), "R": Y6}),
        Table(("A", ("Product Name",)), {"product": "A", "category": "B", "ppt": "C", "pt": "D"},
              {"O": Y1, "P": _p("y2,y3,y4,y5,y6", "Year 2 to 10")}),
        Table(("A", ("Product Name",)), {"product": "A", "category": "B", "ppt": "C", "pt": "D"},
              {"O": Y1, "P": _p("y2,y3,y4,y5", "Year 2 to 5"), "Q": Y6}),
        Table(("A", ("Product Name",)), {"product": "A", "category": "B", "ppt": "C", "pt": "D"},
              {"O": Y1, "P": _p("y2,y3", "Year 2 & 3"), "Q": _p("y4,y5,y6", "Year 4 onwards")}),
        Table(("A", ("Product Name",)), {"product": "A", "category": "B", "ppt": "C", "pt": "D"},
              {"O": Y1, "P": _p("y2,y3,y4,y5,y6", "Year 2 onwards")}),
    ]),
    SheetSpec("PNB", "PNB MetLife", ("O", "Y"), mg_offset=14, tables=[
        Table(("A", ("Product Name",)), {"product": "A", "ppt": "B", "condition": "C"},
              {"T": _p("y1", "Year 1 (commission + reward)"), "W": _p("y2", "Year 2 (commission + reward)")},
              carry=("product",), hook="pnb"),
    ]),
    SheetSpec("SUD", "Star Union Dai-ichi (SUD)", ("L", "T"), mg_offset=11, tables=[
        Table(("A", ("Product",)), {"product": "A", "ppt": "B", "pt": "C"},
              {"O": Y1, "P": Y2, "Q": Y3, "R": Y4, "S": Y5, "T": Y6}, carry=("product",)),
    ]),
    SheetSpec("Pramerica", "Pramerica Life", ("AL", "AZ"), tables=[
        # Final MG lists the 1st-year commission, the reward and their Total (W, X, Y) and the renewal (Z); the BG block
        # keeps only the total and the renewal, so its columns are 17 (not 19) to the right of the MG ones
        Table(("A", ("Product",)), {"product": "A", "category": "B", "ppt": "C", "pt": "D"},
              {"AP": Y1, "AQ": _p("y2,y3,y4,y5,y6", "Year 2 onwards")}, skip=2, section="Traditional",
              carry=("product",), mg={"AP": "Y", "AQ": "Z"}),
        Table(("A", ("Product",)), {"product": "A", "category": "B", "ppt": "C", "pt": "D"},
              {"AP": Y1, "AQ": _p("y2,y3,y4,y5,y6", "Year 2 onwards")}, skip=2, section="ULIP",
              carry=("product",), mg={"AP": "Y", "AQ": "Z"}),
        Table(("A", ("Product",)), {"product": "A", "category": "B", "ppt": "C", "pt": "D"},
              {"AP": Y1, "AQ": Y1}, skip=2, section="Single pay ULIP", hook="pramerica_single",
              mg={"AP": "Y", "AQ": "AB"}),               # total commission, ticket size below / above 50 lakhs
        Table(("A", ("PPT",)), {"ppt": "A", "pt": "B"}, {"AN": Y1}, skip=2, section="Invest Shield",
              hook="pramerica_shield", mg_offset=19),
    ]),
    SheetSpec("Bandhan", "Bandhan Life", ("J", "P"), mg_offset=9, tables=[
        Table(("A", CAT), {"category": "A", "product": "B", "ppt": "C"},
              {"O": _p("y1", "Year 1 (base + IDRE)"), "P": RENEWALS}, skip=2, hook="bandhan"),
    ]),
    SheetSpec("Edelweiss", "Edelweiss Life", ("H", "L"), mg_offset=7, tables=[
        Table(("A", CAT), {"category": "A", "product": "B", "ppt": "C"}, {"K": Y1}),
    ]),
    SheetSpec("LIC", "LIC of India", ("K", "P"), mg_offset=10, tables=[
        Table(("A", ("PLANS",)), {"product": "A", "ppt": "B"},
              {"M": Y1, "N": _p("y2,y3", "Year 2 & 3"), "O": _p("y4,y5", "Year 4 & 5"), "P": Y6},
              skip=2, unit=1.0, carry=("product",), hook="lic", keep_text_tail=True),
    ]),
]
SPEC_BY_NAME = {s.name: s for s in SPECS}


# ----------------------------------------------------------------------------------------------- cells
_COL = {}


def _ci(col):
    if col not in _COL:
        _COL[col] = column_index_from_string(col)
    return _COL[col]


def clean(v):
    """A cell as one-line text ('' for empty, an Excel error or a bool)."""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.strftime("%d/%m/%Y")
    s = re.sub(r"\s+", " ", str(v).replace("\xa0", " ")).strip()
    return "" if s.startswith("#") else s


def label_text(v):
    s = clean(v)
    return "" if s.lower() in NO_VALUE else s


def payout_cell(v):
    """('num', float) for a number, ('text', str) for a written rule, (None, None) for nothing."""
    if v is None or isinstance(v, bool):
        return None, None
    if isinstance(v, (int, float)):
        return "num", float(v)
    s = clean(v)
    if s.lower() in NO_VALUE:
        return None, None
    return "text", s


def pct(v, unit=100.0):
    """A workbook number as a percent (rounded to 6 dp to drop float noise)."""
    return round(v * unit, 6)


def fmt_pct(v):
    return f"{v:.4f}".rstrip("0").rstrip(".") + "%"


class Sheet:
    """A worksheet with merged-cell filling for labels."""

    def __init__(self, ws):
        self.ws = ws
        self.max_row = ws.max_row
        self._merged = {}
        for rng in ws.merged_cells.ranges:
            if rng.min_col != rng.max_col:
                continue                                # a label merged across columns is not repeated sideways
            for r in range(rng.min_row + 1, rng.max_row + 1):
                self._merged[(r, rng.min_col)] = (rng.min_row, rng.min_col)

    def raw(self, col, row):
        return self.ws.cell(row=row, column=_ci(col)).value

    def val(self, col, row):
        top = self._merged.get((row, _ci(col)))
        return self.ws.cell(row=top[0], column=top[1]).value if top else self.raw(col, row)

    def cell(self, c, row):
        return self.ws.cell(row=row, column=c)


# ----------------------------------------------------------------------------------------------- reading
def _find_row(sh, col, texts, start):
    wanted = {t.lower() for t in texts}
    for r in range(start, sh.max_row + 1):
        if clean(sh.raw(col, r)).lower() in wanted:
            return r
    return None


def _locate(sh, spec):
    found, cursor = [], 1
    for t in spec.tables:
        col, texts = t.anchor
        row = _find_row(sh, col, texts, cursor)
        if row is None:
            what = t.section or "payout"
            raise LifeLayoutError(
                f"{spec.name}: expected the header '{texts[0]}' in column {col} (the {what} table) "
                f"below row {cursor - 1 if cursor > 1 else 0}, but it is not there. The sheet layout has changed.")
        found.append((t, row))
        cursor = row + 1
    return found


def _tata_ppt_pt(name):
    ppt = pt = ""
    m = re.search(r"(\d+)\s*(?:-|to)\s*(\d+)\s*Pay", name, re.I)
    if m:
        ppt = f"{m.group(1)}-{m.group(2)}"
    elif re.search(r"(\d+)\s*\+\s*Pay", name, re.I):
        ppt = re.search(r"(\d+)\s*\+\s*Pay", name, re.I).group(1) + "+"
    elif re.search(r"(\d+)\s*Pay\s*(?:&\s*(?:above|>)|>)", name, re.I):
        ppt = re.search(r"(\d+)\s*Pay\s*(?:&\s*(?:above|>)|>)", name, re.I).group(1) + "+"
    elif re.search(r"(\d+)\s*Pay\b", name, re.I):
        ppt = re.search(r"(\d+)\s*Pay\b", name, re.I).group(1)
    elif re.search(r"\bSP\b|Single Pay", name, re.I):
        ppt = "SP"
    elif re.search(r"Reg(?:ular)?\s*Pay", name, re.I):
        ppt = "Regular"
    else:
        m = re.search(r"\b(?:SIP|VIP|SVIP)_(\d+)(?:\s*-\s*(\d+))?", name)
        if m:
            ppt = m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")
    m = re.search(r"([<>]=?\s*\d+)\s*(?:PT|Policy Term)", name, re.I)
    if m:
        pt = re.sub(r"\s+", "", m.group(1))
    else:
        m = re.search(r"(\d+)\s*T[O0]\s*(\d+)\s*Policy Term", name, re.I)
        if m:
            pt = f"{m.group(1)}-{m.group(2)}"
        elif re.search(r"ALL\s*Policy Term", name, re.I):
            pt = "All"
    return ppt, pt


class _Ctx:
    """What a row hook needs: the sheet, the row number, the row's labels and a way to make rows."""

    def __init__(self, sh, spec, table, r, base):
        self.sh, self.spec, self.table, self.r, self.base = sh, spec, table, r, base

    def num(self, col):
        kind, v = payout_cell(self.sh.raw(col, self.r))
        return v if kind == "num" else None

    def txt(self, col):
        return clean(self.sh.val(col, self.r))

    def row(self, **over):
        row = dict(self.base)
        row.update(over)
        row["remarks"] = list(over.get("remarks", row.get("remarks", [])))
        return row

    def ref(self, col):
        return f"{self.spec.name}!{col}{self.r}"


def _set(row, fields, value):
    for f in fields.split(","):
        if row.get(f) is None:
            row[f] = value


def _set_mg(row, fields, value):
    """The Final MG of the same years as `fields` ("y2,y3" -> m_y2, m_y3)."""
    _set(row, ",".join(f"m_{f}" for f in fields.split(",")), value)


def _generic(ctx):
    """One row from the table's payout columns; None when the row holds no payout."""
    t, sh, r = ctx.table, ctx.sh, ctx.r
    row = ctx.row()
    remarks, first_col = [], None
    for col, (fields, label) in t.payouts.items():
        kind, v = payout_cell(sh.raw(col, r))
        if kind == "num":
            _set(row, fields, pct(v, t.unit))
            first_col = first_col or col
        elif kind == "text":
            remarks.append(f"{label}: {v}")
            first_col = first_col or col
        if col in t.mg:                                 # the Final MG this payout was worked out from
            mg_kind, mg_value = payout_cell(sh.raw(t.mg[col], r))
            if mg_kind == "num":
                _set_mg(row, fields, pct(mg_value, t.unit))
    if first_col is None and t.rule_cols:
        for col, label in t.rule_cols.items():
            text = clean(sh.raw(col, r))
            if text and not re.fullmatch(r"[\d.]+", text):
                remarks.append(f"{label}: {text}")
                first_col = first_col or col
    if first_col is None:
        return None
    later = []
    for col, label in t.overflow.items():
        kind, v = payout_cell(sh.raw(col, r))
        if kind == "num" and v:
            later.append((label, pct(v, t.unit)))
    if any(abs(v - (row.get("y6") or 0)) > 1e-9 for _, v in later):
        remarks.extend(f"{label}: {fmt_pct(v)}" for label, v in later)
    for col, prefix in t.remark_cols.items():
        text = clean(sh.val(col, r))
        if text:
            remarks.append(f"{prefix}: {text}" if prefix else text)
    row["remarks"] = remarks
    row["source_ref"] = ctx.ref(first_col)
    return row


def _hook_tata(ctx):
    sh, r = ctx.sh, ctx.r
    name = ctx.base["product"]
    ppt, pt = _tata_ppt_pt(name)
    terms = clean(sh.val("F", r))
    variant = ctx.base["variant"]
    m = re.match(r"(NROP|ROP)\b", terms)
    if m and not variant:
        variant = m.group(1)
    extra = [clean(sh.val(c, r)) for c in ("R", "AC")]
    remarks = [x for x in [terms] + extra if x]
    out = []
    mg = sh.raw("B", r)
    y1, d = ctx.num("Y"), ctx.num("Z")
    final_mg = ctx.table.mg                              # BG column -> the Final MG column it comes from
    if isinstance(mg, (int, float)) or (y1 or d):
        out.append(ctx.row(ppt=ppt, pt=pt, variant=variant, condition="Offline", y1=_pct(y1), d=_pct(d),
                           m_y1=_pct(ctx.num(final_mg["Y"])), m_d=_pct(ctx.num(final_mg["Z"])),
                           remarks=remarks, source_ref=ctx.ref("Y")))
    dig = sh.raw("C", r)
    if isinstance(dig, (int, float)):
        out.append(ctx.row(ppt=ppt, pt=pt, variant=variant, condition="Digital discount",
                           y1=_pct(ctx.num("AA")), d=_pct(ctx.num("AB")),
                           m_y1=_pct(ctx.num(final_mg["AA"])), m_d=_pct(ctx.num(final_mg["AB"])),
                           remarks=remarks, source_ref=ctx.ref("AA")))
    return out


def _pct(v, unit=100.0):
    return None if v is None else pct(v, unit)


def _hook_lic(ctx):
    base = _generic(ctx)
    if base is None:
        return []
    raw = ctx.sh.val("A", ctx.r)
    lines = [clean(x) for x in str(raw or "").split("\n")]
    lines = [x for x in lines if x]
    plans = [x for x in lines if re.match(r"^(?:Plan\s*)?\d{3}\b", x)]
    others = [x for x in lines if x not in plans]
    if not plans:
        return [base]
    out = []
    for plan in plans:
        row = dict(base)
        row["product"] = re.sub(r"^Plan\s*", "", plan)
        row["condition"] = " ".join(others)
        row["remarks"] = list(base["remarks"])
        out.append(row)
    return out


def _hook_pnb(ctx):
    base = _generic(ctx)
    if base is None:
        return []
    k = clean(ctx.sh.val("K", ctx.r))
    y3 = ctx.num("X")
    if y3 is not None:
        if re.search(r"end of PPT|Subsequent", k, re.I):
            fields = "y3,y4,y5,y6"
        else:
            m = re.search(r"Till Yr-(\d)", k, re.I)
            fields = ",".join(f"y{i}" for i in range(3, min(int(m.group(1)), 6) + 1)) if m else "y3"
        _set(base, fields, pct(y3))
        mg_y3 = ctx.num(get_column_letter(_ci("X") - ctx.table.mg_offset))     # Comm. Yr-3 of the Final MG block
        if mg_y3 is not None:
            _set_mg(base, fields, pct(mg_y3))
    parts = []
    for label, comm, rew in (("Year 1", "R", "S"), ("Year 2", "U", "V")):
        c, w = ctx.num(comm), ctx.num(rew)
        if w:
            parts.append(f"{label}: commission {fmt_pct(pct(c or 0))} + reward {fmt_pct(pct(w))}")
    base["remarks"] = parts + base["remarks"]
    return [base]


def _hook_bandhan(ctx):
    base = _generic(ctx)
    if base is None:
        return []
    b, i = ctx.num("M"), ctx.num("N")
    if b is not None and i is not None:
        base["remarks"].insert(0, f"Year 1: base {fmt_pct(pct(b))} + IDRE {fmt_pct(pct(i))}")
    return [base]


def _hook_pramerica_single(ctx):
    out = []
    for col, label in (("AP", "Ticket size below 50 Lakhs"), ("AQ", "Ticket size 50 Lakhs and above")):
        v = ctx.num(col)
        if v is not None:
            out.append(ctx.row(condition=label, y1=pct(v), m_y1=_pct(ctx.num(ctx.table.mg[col])),
                               source_ref=ctx.ref(col)))
    return out


def _hook_pramerica_shield(ctx):
    sh, r, t = ctx.sh, ctx.r, ctx.table
    header = t._anchor_row + 1                           # the variant labels sit one row below the anchor
    out = []
    shift = ctx.table.mg_offset                          # the Final MG block's columns are this far to the left
    shared = ctx.num("AZ")
    mg_shared = ctx.num(get_column_letter(_ci("AZ") - shift))
    for i in range(6):
        y1 = ctx.num(get_column_letter(_ci("AN") + i))
        y2 = ctx.num(get_column_letter(_ci("AT") + i))
        if y1 is None and y2 is None:
            continue
        label = clean(sh.raw(get_column_letter(_ci("C") + i), header))
        row = ctx.row(product="Invest Shield", condition=label, y1=_pct(y1), y2=_pct(y2),
                      m_y1=_pct(ctx.num(get_column_letter(_ci("AN") + i - shift))),
                      m_y2=_pct(ctx.num(get_column_letter(_ci("AT") + i - shift))),
                      source_ref=ctx.ref(get_column_letter(_ci("AN") + i)))
        if shared is not None:
            for f in ("y3", "y4", "y5", "y6"):
                row[f] = pct(shared)
                if mg_shared is not None:
                    row[f"m_{f}"] = pct(mg_shared)
        out.append(row)
    return out


HOOKS = {
    "tata": _hook_tata, "lic": _hook_lic, "pnb": _hook_pnb, "bandhan": _hook_bandhan,
    "pramerica_single": _hook_pramerica_single, "pramerica_shield": _hook_pramerica_shield,
}


def _table_extent(sh, table, start, end_row):
    """(the row where the table really ends, the last row holding a number): stops at a stop marker or after
    GAP_LIMIT rows without any payout, so a note table or a stray cell far below is never read as rates."""
    gap, last_number, stop_at = 0, 0, end_row
    for r in range(start, end_row):
        if any(clean(sh.raw(col, r)).lower().startswith(text) for col, text in table.stop):
            stop_at = r
            break
        kinds = [payout_cell(sh.raw(col, r))[0] for col in table.payouts]
        if "num" in kinds:
            last_number = r
        if any(kinds):
            gap = 0
        else:
            gap += 1
            if gap >= GAP_LIMIT:
                stop_at = r - GAP_LIMIT + 1
                break
    return stop_at, last_number


def _read_table(sh, spec, table, anchor_row, end_row, consumed):
    rows, state = [], {r: "" for r in LEVELS}
    heading_variant, heading_prefix = "", ""
    start = anchor_row + table.skip
    consumed.update(range(anchor_row, start))
    table._anchor_row = anchor_row
    end_row, last_number = _table_extent(sh, table, start, end_row)
    for r in range(start, end_row):
        new = {role: label_text(sh.val(col, r)) for role, col in table.labels.items()}
        has_payout = any(payout_cell(sh.raw(col, r))[0] for col in table.payouts)
        if not has_payout and table.headings is not None and new.get("product") and len(new["product"]) <= 40:
            role = table.headings.get(new["product"].lower())
            if role is None and not clean(sh.raw("B", r)) and not clean(sh.raw("C", r)):
                role = "variant"
            if role:
                if role == "category":
                    state["category"], heading_variant, heading_prefix = new["product"], "", ""
                else:
                    heading_variant = new["product"]
                    heading_prefix = heading_variant.rsplit(" - ", 1)[0].lower()
                consumed.add(r)
                continue
        has_rule = not has_payout and any(
            clean(sh.raw(c, r)) and not re.fullmatch(r"[\d.]+", clean(sh.raw(c, r))) for c in table.rule_cols)
        if not (has_payout or has_rule):
            continue                                    # a note, a title or a spacer: never changes the labels
        if r > last_number and not table.keep_text_tail:
            continue                                    # a written rule after the last number is a note
        if not (new.get("product") or new.get("ppt") or new.get("pt")):
            continue                                    # a row with no label of its own (typed zeros in a spacer row)
        if (table.reset_category_on_product and new.get("product") and new["product"] != state["product"]
                and not new.get("category")):
            state["category"] = ""
        for i, role in enumerate(LEVELS):
            if role not in table.labels:
                continue
            v = new.get(role, "")
            if v:
                if v != state[role]:
                    for lower in LEVELS[i + 1:]:
                        state[lower] = ""
                state[role] = v
            elif role not in table.carry:
                state[role] = ""
        base = {
            "insurer": spec.insurer, "sheet": spec.name, "section": table.section,
            "category": state["category"], "product": state["product"],
            "variant": state["variant"] or (
                heading_variant if state["product"].lower().startswith(heading_prefix) else ""),
            "ppt": new.get("ppt", ""), "pt": new.get("pt", ""), "condition": new.get("condition", ""),
            "y1": None, "d": None, "y2": None, "y3": None, "y4": None, "y5": None, "y6": None,
            "m_y1": None, "m_d": None, "m_y2": None, "m_y3": None, "m_y4": None, "m_y5": None, "m_y6": None,
            "remarks": [], "source_ref": "",
        }
        ctx = _Ctx(sh, spec, table, r, base)
        made = HOOKS[table.hook](ctx) if table.hook else [_generic(ctx)]
        made = [m for m in made if m]
        if made:
            consumed.add(r)
            rows.extend(made)
    return rows


def _note_cell(cell, plain_numbers):
    v = cell.value
    text = clean(v)
    if not text:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "%" in (cell.number_format or ""):
            return fmt_pct(v * 100)
        return text if plain_numbers else ""
    return text


def _note_text(sh, spec, r, first_anchor):
    bg_first, bg_last = _ci(spec.bg[0]), _ci(spec.bg[1])
    bg = [_note_cell(sh.cell(c, r), False) for c in range(bg_first, bg_last + 1)]
    left = [_note_cell(sh.cell(c, r), True) for c in range(1, min(bg_first, 9))]
    bg, left = [x for x in bg if x], [x for x in left if x]
    words = re.compile(r"[A-Za-z]{3,}")
    above = r < first_anchor
    if spec.notes_side != "left" and any(words.search(x) for x in bg):
        parts = bg
    elif any(words.search(x) for x in left) and (not above or len(left) == 1):
        parts = left                                    # above the tables the left block is the insurer's own header
    else:
        return ""
    parts = [p for p in parts if not TITLE_RX.match(p) and not re.search(r"final (mg|bg)", p, re.I)]
    line = " | ".join(dict.fromkeys(parts))
    return line if len(line) >= 6 else ""


def _notes(sh, spec, consumed, first_anchor):
    lines = []
    for r in range(1, sh.max_row + 1):
        if r in consumed:
            continue
        line = _note_text(sh, spec, r, first_anchor)
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


def read_sheet(ws, spec):
    sh = Sheet(ws)
    found = _locate(sh, spec)
    consumed, rows = set(), []
    for i, (table, arow) in enumerate(found):
        end = found[i + 1][1] if i + 1 < len(found) else sh.max_row + 1
        rows.extend(_read_table(sh, spec, table, arow, end, consumed))
    if not rows:
        raise LifeLayoutError(
            f"{spec.name}: no payout rows were found. Was the workbook saved with its calculated values "
            f"(open it in Excel and save it again)?")
    return rows, _notes(sh, spec, consumed, found[0][1])


def read_life_workbook(source):
    """source: a path or a file object of the .xlsx. Raises LifeLayoutError when it is not the Life workbook."""
    try:
        wb = load_workbook(source, data_only=True)
    except Exception as exc:                            # not a zip / not an xlsx
        raise LifeLayoutError(f"This file could not be opened as an Excel workbook ({exc.__class__.__name__}).") from exc
    result, problems = LifeWorkbook(), []
    known = set(SPEC_BY_NAME) | IGNORED_SHEETS
    for spec in SPECS:
        if spec.name not in wb.sheetnames:
            result.warnings.append(f"Sheet '{spec.name}' ({spec.insurer}) is not in the workbook, so it has no rates.")
            continue
        try:
            rows, notes = read_sheet(wb[spec.name], spec)
        except LifeLayoutError as exc:
            problems.append(str(exc))
            continue
        for position, row in enumerate(rows):
            row["remarks"] = " | ".join(dict.fromkeys(row["remarks"]))
        result.rows.extend(rows)
        result.sheet_counts[spec.insurer] = len(rows)
        if notes:
            result.notes[spec.insurer] = notes
    for name in wb.sheetnames:
        if name not in known:
            result.warnings.append(f"Sheet '{name}' is not a known insurer sheet and was not read.")
    if problems:
        raise LifeLayoutError("\n".join(problems))
    if not result.rows:
        raise LifeLayoutError("None of the insurer sheets were found - is this the Life working workbook?")
    for position, row in enumerate(result.rows):
        row["position"] = position
    return result
