"""The standard Life file: one flat table that loads every Life insurer, whatever their own grid looks like.

The old way in is the working workbook (``services/life_workbook.py``): a fixed map of every insurer sheet, which has
to be edited whenever an insurer moves a column. This is the other way in, the one the Motor and Health uploads
already use: the file's header row names its columns (the stored field names, nothing renamed), the portal finds
each column **by name**, and so nothing can shift or be read from the wrong place. Whoever prepares a grid (by hand
or with a converter script) turns it into this shape; the portal does not care how the insurer laid it out.

One row is one payout row of an insurer's grid. Columns (order does not matter, case and punctuation in a name are
ignored):

  * ``insurer`` and ``product`` are required; ``category``, ``variant``, ``ppt``, ``pt`` and ``condition`` are the
    grid's own wording (the Rate Checker reads it, ``services/life_filters.py``).
  * ``from_date`` and ``to_date``: the window the row applies in, as on Rate Master and Health Rate Master (both ends
    included, an empty one means no limit on that side, a row with neither applies to any date). An Excel date cell,
    ``DD/MM/YYYY`` or ``YYYY-MM-DD``; ``valid_from`` / ``valid_to`` / ``valid_till`` are accepted spellings. The MIS
    engine reads the window against the policy's inception date, the Rate Checker against the date typed on its page.
  * ``payout_yr1``, ``payout_deferred``, ``payout_yr2`` ... ``payout_yr5``, ``payout_yr6_plus``: the BG payout (the
    partner payout the Rate Checker shows). At least one is needed, unless the row is a written rule that is not a
    number ("Year 1: 30% of 1st year commission"): then the rule goes in ``remarks`` and the payouts stay empty (the
    live grid has a few such rows, and an export of it has to load back).
  * ``mg_yr1`` ... ``mg_yr6_plus``: the MG of the same years (what the insurer pays the broker). Optional; admin
    pages only.
  * ``remarks`` and the optional trace columns ``sheet``, ``section`` and ``source_ref`` (an empty ``source_ref``
    becomes "Row N" of the file).
  * Every payout is a percent number: ``35.5`` means 35.5 %, an empty cell means no payout, ``0`` means a payout of
    zero. A cell Excel shows as ``35.5%`` is read as 35.5; the text ``"35.5%"`` too.

A second sheet named ``Notes`` (columns ``insurer`` and ``note``) carries each insurer's terms and conditions. The
columns an Export Excel adds (``id``, ``upload``, ``position``, ``is_edited``, ``edited_by``, ``edited_at``) are
ignored, so an export is itself a valid file: export the live grid, edit it, upload it back.

Like the rest of Life, nothing is guessed and nothing is half-saved: the whole file is checked, every problem is
listed with its row number (``LifeFileError``), and a single bad row stops the upload.

``read_standard_file`` returns the same ``LifeWorkbook`` the workbook reader does, so ``save_life_upload`` stores it
the same way.
"""
import csv
import io
import re
from collections import Counter, namedtuple
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..health_grid_utils import parse_grid_date
from ..models import LifeRate
from .life_workbook import MG_FIELDS as _WORKBOOK_MG_FIELDS
from .life_workbook import PAYOUT_FIELDS as _WORKBOOK_BG_FIELDS
from .life_workbook import LifeWorkbook

BG_COLUMNS = tuple(LifeRate.PAYOUT_FIELDS)
MG_COLUMNS = tuple(LifeRate.MG_FIELDS)
PAYOUT_COLUMNS = (*BG_COLUMNS, *MG_COLUMNS)
LABEL_COLUMNS = ("insurer", "category", "product", "variant", "ppt", "pt", "condition")
DATE_COLUMNS = ("from_date", "to_date")
OPTIONAL_COLUMNS = ("remarks", "sheet", "section", "source_ref")
TEXT_COLUMNS = (*LABEL_COLUMNS, *OPTIONAL_COLUMNS)
STANDARD_COLUMNS = (*LABEL_COLUMNS, *DATE_COLUMNS, *BG_COLUMNS, *MG_COLUMNS, *OPTIONAL_COLUMNS)
EXPORT_ONLY_COLUMNS = ("id", "upload", "position", "is_edited", "edited_by", "edited_at")
NOTES_COLUMNS = ("insurer", "note")

RATES_SHEET, NOTES_SHEET, README_SHEET = "Rates", "Notes", "Read me"

MAX_ROWS = 50_000
MAX_PROBLEMS = 200            # problems kept for the message; the rest are only counted
HEADER_SCAN_ROWS = 10         # the header row has to be within the first rows of the sheet
BLANK_RUN_LIMIT = 500         # this many empty rows in a row end the data (a sheet formatted far below its last row)

# the field a standard column fills in the rows services.life_workbook produces (and save_life_upload stores)
_ROW_FIELD = {**dict(zip(BG_COLUMNS, _WORKBOOK_BG_FIELDS)), **dict(zip(MG_COLUMNS, _WORKBOOK_MG_FIELDS))}
_LIMITS = {name: LifeRate._meta.get_field(name).max_length for name in TEXT_COLUMNS
           if LifeRate._meta.get_field(name).max_length}

_NO_VALUE = {"", "-", "–", "—", "na", "n/a", "nil", "none", "null", "nan"}
_EXCEL_ERRORS = {"#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!"}
_ROW_REF = re.compile(r"^row \d+$", re.I)

_Cell = namedtuple("_Cell", "value percent error")


class LifeFileError(ValueError):
    """The standard file cannot be loaded. ``problems`` lists the reasons (at most MAX_PROBLEMS), ``total`` how many
    there are in all."""

    def __init__(self, problems, total=None):
        self.problems = list(problems)
        self.total = len(self.problems) if total is None else total
        super().__init__("\n".join(self.problems))


class _Problems:
    def __init__(self):
        self.items, self.total = [], 0

    def add(self, text):
        self.total += 1
        if len(self.items) < MAX_PROBLEMS:
            self.items.append(text)

    def raise_if_any(self):
        if self.total:
            raise LifeFileError(self.items, self.total)


# ----------------------------------------------------------------------------------------------- headers
def _key(text):
    """A header with case, spacing and punctuation ignored ('Payout Yr 1' -> 'payoutyr1')."""
    return re.sub(r"[^a-z0-9]", "", ("" if text is None else str(text)).lower())


def _fold(text):
    return _key(text)


# other ways a person writes the validity columns; without these a "Valid till" column would be ignored as unknown
_DATE_ALIASES = {"validfrom": "from_date", "validto": "to_date", "validtill": "to_date", "validuntil": "to_date"}
_STANDARD_KEYS = {**{_key(name): name for name in STANDARD_COLUMNS}, **_DATE_ALIASES}
_EXPORT_ONLY_KEYS = {_key(name) for name in EXPORT_ONLY_COLUMNS}
_NOTE_KEYS = {"insurer": "insurer", "note": "note", "notes": "note", "text": "note"}
_NOTES_SHEET_KEYS = {"notes", "insurernotes"}
_SKIPPED_SHEET_KEYS = {"readme", "instructions", "guide", "help"}


def _header_candidate(values, names):
    """({standard name: column index}, [unrecognised headers], {name: [indexes]}) for one row of header cells."""
    columns, unknown, seen = {}, [], {}
    for index, value in enumerate(values):
        key = _key(value)
        if not key:
            continue
        name = names.get(key)
        if name:
            seen.setdefault(name, []).append(index)
            columns.setdefault(name, index)
        elif key not in _EXPORT_ONLY_KEYS:
            unknown.append(str(value).strip())
    return columns, unknown, seen


def _find_header(records, names, need=2):
    """The header among the first rows: the one carrying the most known column names (at least `need`). `records` is
    [(row number, [header cell values])]. -> (row number, columns, unknown, seen) or None."""
    best = None
    for number, values in records[:HEADER_SCAN_ROWS]:
        columns, unknown, seen = _header_candidate(values, names)
        if len(columns) >= need and (best is None or len(columns) > len(best[1])):
            best = (number, columns, unknown, seen)
    return best


def _column_names(indexes):
    return ", ".join(get_column_letter(i + 1) for i in indexes)


# ----------------------------------------------------------------------------------------------- cells
def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _g(number):
    return f"{number:g}"


def _text(cell, name, problems):
    """One text cell as clean one-line text; a problem (Excel error, a date where a PPT / PT should be) is added to
    `problems` and '' returned."""
    value = cell.value
    if cell.error:
        problems.append(f"{name}: the cell holds the Excel error {value}.")
        return ""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime, date)):
        if name in ("ppt", "pt"):
            problems.append(
                f"{name}: Excel turned the entry into the date {value:%d/%m/%Y}. Format the column as Text and type "
                f"it again (for example 5-10).")
            return ""
        return value.strftime("%d/%m/%Y")
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    text = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    if text in _EXCEL_ERRORS:
        problems.append(f"{name}: the cell holds the Excel error {text}.")
        return ""
    return text


def _percent(cell, name, problems):
    """One payout cell as a percent number (None for an empty cell); a problem is added to `problems`."""
    value = cell.value
    if cell.error:
        problems.append(f"{name}: the cell holds the Excel error {value}.")
        return None
    if value is None:
        return None
    if isinstance(value, bool):
        problems.append(f"{name}: TRUE / FALSE is not a number.")
        return None
    if isinstance(value, (datetime, date)):
        problems.append(f"{name}: a date is not a number between 0 and 100.")
        return None
    if isinstance(value, (int, float)):
        number = float(value) * (100 if cell.percent else 1)
    else:
        text = str(value).replace("\xa0", " ").strip()
        if text.lower() in _NO_VALUE:
            return None
        if text in _EXCEL_ERRORS:
            problems.append(f"{name}: the cell holds the Excel error {text}.")
            return None
        try:
            number = float(text.rstrip("%").strip())
        except ValueError:
            if re.fullmatch(r"\d+,\d+%?", text):
                problems.append(f"{name}: '{text}' uses a comma as the decimal mark; write it with a point "
                                f"({text.replace(',', '.')}).")
            else:
                problems.append(f"{name}: '{text[:40]}' is not a number. Put a written rule in remarks instead.")
            return None
    if number != number or number in (float("inf"), float("-inf")):
        problems.append(f"{name}: not a number.")
        return None
    if not 0 <= number <= 100:
        problems.append(f"{name}: {_g(number)} is outside 0-100 (a payout is a percent: 35.5 means 35.5%).")
        return None
    return round(number, 6)


# formats beyond the two the Health upload takes (DD/MM/YYYY and YYYY-MM-DD, health_grid_utils.parse_grid_date): what a
# date looks like after a trip through pandas or a Windows locale. A two-digit year is not guessed at.
_MORE_DATE_FORMATS = ("%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S")


def _date(cell, name, problems):
    """One validity cell as a date (None for an empty cell); a problem is added to `problems`."""
    value = cell.value
    if cell.error:
        problems.append(f"{name}: the cell holds the Excel error {value}.")
        return None
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        problems.append(f"{name}: TRUE / FALSE is not a date.")
        return None
    if isinstance(value, (int, float)):
        problems.append(f"{name}: {value:g} is a number, not a date. Format the cell as a date (DD/MM/YYYY).")
        return None
    text = str(value).replace("\xa0", " ").strip()
    if text.lower() in _NO_VALUE:
        return None
    if text in _EXCEL_ERRORS:
        problems.append(f"{name}: the cell holds the Excel error {text}.")
        return None
    parsed = parse_grid_date(text)
    for fmt in _MORE_DATE_FORMATS:
        if parsed is not None:
            break
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if parsed is None:
        problems.append(f"{name}: '{text[:40]}' is not a date. Write it as DD/MM/YYYY (or YYYY-MM-DD).")
    return parsed


# ----------------------------------------------------------------------------------------------- loading a file
def _csv_records(raw):
    """[(line number, [cells])] of a CSV (UTF-8, with or without a BOM, else Windows-1252; comma, semicolon or tab)."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1")
    first = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max((",", ";", "\t"), key=first.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    return [(reader.line_num, record) for record in reader]


def _xlsx_workbook(raw):
    try:
        return load_workbook(io.BytesIO(raw), data_only=True)
    except Exception as exc:                          # not a zip / not an xlsx
        raise LifeFileError([f"This file could not be opened as an Excel workbook ({exc.__class__.__name__})."]) from exc


def _sheet_records(ws, limit=None):
    """[(row number, [cell values])] of the first `limit` rows of a worksheet (all when None)."""
    last = ws.max_row if limit is None else min(ws.max_row, limit)
    return [(number, [cell.value for cell in row])
            for number, row in enumerate(ws.iter_rows(min_row=1, max_row=last), start=1)]


def _data_rows_xlsx(ws, header_row, columns):
    """[(row number, {name: _Cell})] below the header; stops after BLANK_RUN_LIMIT empty rows in a row."""
    out, blank_run = [], 0
    for number, row in enumerate(ws.iter_rows(min_row=header_row + 1), start=header_row + 1):
        cells = {}
        for name, index in columns.items():
            if index < len(row):
                cell = row[index]
                cells[name] = _Cell(cell.value, name in PAYOUT_COLUMNS and "%" in (cell.number_format or ""),
                                    cell.data_type == "e")
        if all(_is_blank(c.value) for c in cells.values()):
            blank_run += 1
            if blank_run >= BLANK_RUN_LIMIT:
                break
            continue
        blank_run = 0
        out.append((number, cells))
    return out


def _data_rows_csv(records, header_row, columns):
    out = []
    for number, record in records:
        if number <= header_row:
            continue
        cells = {name: _Cell(record[index], False, False) for name, index in columns.items() if index < len(record)}
        if all(_is_blank(c.value) for c in cells.values()):
            continue
        out.append((number, cells))
    return out


# ----------------------------------------------------------------------------------------------- reading the rows
def _parse_row(number, cells):
    """(row dict or None, [problems]) for one data row."""
    problems, texts, payouts = [], {}, {}
    for name in TEXT_COLUMNS:
        cell = cells.get(name)
        texts[name] = _text(cell, name, problems) if cell is not None else ""
        limit = _LIMITS.get(name)
        if limit and len(texts[name]) > limit:
            problems.append(f"{name}: {len(texts[name])} characters, the most allowed is {limit}.")
    dates = {}
    for name in DATE_COLUMNS:
        cell = cells.get(name)
        if cell is not None:
            dates[name] = _date(cell, name, problems)
    if dates.get("from_date") and dates.get("to_date") and dates["from_date"] > dates["to_date"]:
        problems.append(f"from_date ({dates['from_date']:%d/%m/%Y}) is after to_date ({dates['to_date']:%d/%m/%Y}).")
    bad_payout = False
    for name in PAYOUT_COLUMNS:
        cell = cells.get(name)
        if cell is None:
            continue
        before = len(problems)
        payouts[name] = _percent(cell, name, problems)
        bad_payout = bad_payout or len(problems) > before
    if not texts["insurer"] and not any("insurer:" in p for p in problems):
        problems.append("insurer is empty.")
    if not texts["product"] and not any("product:" in p for p in problems):
        problems.append("product is empty.")
    if not bad_payout and all(payouts.get(name) is None for name in BG_COLUMNS) and not texts["remarks"]:
        # a row may carry only a written rule instead of a number (the real grid has some: "Year 1: 30% of 1st year
        # commission"), but then the rule has to be there: a row with neither says nothing
        problems.append("needs at least one BG payout (payout_yr1 ... payout_yr6_plus) or, for a rule that is not a "
                        "number, the rule in remarks.")
    if problems:
        return None, [f"Row {number}: {text}" for text in problems]

    source = texts["source_ref"]
    if not source or _ROW_REF.match(source) or source == LifeRate.MANUAL_SOURCE_REF:
        source = f"Row {number}"                      # where the row sits in this file, not in an earlier one
    row = {name: texts[name] for name in TEXT_COLUMNS}
    row["source_ref"] = source[:_LIMITS["source_ref"]]
    row["from_date"], row["to_date"] = dates.get("from_date"), dates.get("to_date")
    for name in PAYOUT_COLUMNS:
        row[_ROW_FIELD[name]] = payouts.get(name)
    return row, []


def _read_notes(ws, problems, rate_insurers):
    """{insurer: text} from the Notes sheet."""
    records = _sheet_records(ws)
    found = _find_header(records, _NOTE_KEYS)
    if found is None or not {"insurer", "note"} <= set(found[1]):
        problems.add("Notes: the first row of the Notes sheet needs the column names insurer and note.")
        return {}
    header_row, columns = found[0], found[1]
    notes, spelled = {}, {_fold(name): name for name in rate_insurers}
    for number, values in records:
        if number <= header_row:
            continue
        insurer = re.sub(r"\s+", " ", str(values[columns["insurer"]] or "").replace("\xa0", " ")).strip() \
            if columns["insurer"] < len(values) else ""
        raw_note = values[columns["note"]] if columns["note"] < len(values) else None
        note = "\n".join(" ".join(line.split()) for line in str(raw_note or "").splitlines() if line.strip())
        if not insurer and not note:
            continue
        if not insurer:
            problems.add(f"Notes row {number}: insurer is empty.")
            continue
        if not note:
            continue                                  # an insurer with no notes: nothing to keep
        match = spelled.get(_fold(insurer))
        if match and match != insurer:
            problems.add(f"Notes row {number}: '{insurer}' is spelled differently from '{match}' on the rates sheet.")
            continue
        if insurer in notes:
            problems.add(f"Notes row {number}: the notes of '{insurer}' are given twice.")
            continue
        notes[insurer] = note
    return notes


def read_standard_file(raw, file_name):
    """The rows (and notes) of a standard file as a ``LifeWorkbook``. `raw` is the file's bytes; its name decides
    between .xlsx and .csv. Raises LifeFileError listing every problem found."""
    lower = (file_name or "").lower()
    warnings, problems = [], _Problems()
    notes_ws, skipped = None, []
    if lower.endswith(".csv"):
        records = _csv_records(raw)
        found = _find_header(records, _STANDARD_KEYS)
        sheet_label = "The file"
    elif lower.endswith(".xlsx"):
        wb = _xlsx_workbook(raw)
        found, rates_ws = None, None
        for ws in wb.worksheets:
            title = _key(ws.title)
            if title in _NOTES_SHEET_KEYS:
                notes_ws = notes_ws or ws
            elif title in _SKIPPED_SHEET_KEYS:
                continue
            elif rates_ws is None:
                head = _find_header(_sheet_records(ws, HEADER_SCAN_ROWS), _STANDARD_KEYS)
                if head is not None:
                    found, rates_ws = head, ws
                else:
                    skipped.append(ws.title)
            else:
                skipped.append(ws.title)
        sheet_label = f"Sheet '{rates_ws.title}'" if rates_ws is not None else "The workbook"
    else:
        raise LifeFileError(["The standard file must be an .xlsx or a .csv file."])

    if found is None:
        raise LifeFileError([
            f"{sheet_label} has no header row: the first row needs the column names (insurer, product, "
            f"payout_yr1 ...). Download the template for the full list."])
    header_row, columns, unknown, seen = found
    missing = [f"has no '{name}' column" for name in ("insurer", "product") if name not in columns]
    if not any(name in columns for name in BG_COLUMNS):
        missing.append("has no payout column (payout_yr1, payout_yr2 ...)")
    if missing:
        raise LifeFileError([f"{sheet_label}: the header row {text}." for text in missing])
    for name, indexes in seen.items():
        if len(indexes) > 1:
            problems.add(f"The column '{name}' appears more than once (columns {_column_names(indexes)}).")

    if lower.endswith(".csv"):
        data = _data_rows_csv(records, header_row, columns)
    else:
        data = _data_rows_xlsx(rates_ws, header_row, columns)
    if not data:
        raise LifeFileError([f"{sheet_label} has no data rows below its header."])
    if len(data) > MAX_ROWS:
        raise LifeFileError([f"The file has {len(data)} rows; the most one upload takes is {MAX_ROWS}."])

    rows, any_payout_cell = [], False
    for number, cells in data:
        any_payout_cell = any_payout_cell or any(
            not _is_blank(cells[name].value) for name in BG_COLUMNS if name in cells)
        row, row_problems = _parse_row(number, cells)
        for text in row_problems:
            problems.add(text)
        if row is not None:
            rows.append(row)
    if not any_payout_cell:
        problems.add("No row has any payout. If the payouts are formulas, open the file in Excel and save it again "
                     "so their values are stored.")

    spellings = {}
    for row in rows:
        spellings.setdefault(_fold(row["insurer"]), Counter())[row["insurer"]] += 1
    for counter in spellings.values():
        if len(counter) > 1:
            listed = ", ".join(f"'{name}' ({count} rows)" for name, count in counter.most_common())
            problems.add(f"An insurer is spelled more than one way: {listed}. Use one spelling on every row.")

    insurers = list(dict.fromkeys(row["insurer"] for row in rows))
    notes = _read_notes(notes_ws, problems, insurers) if notes_ws is not None else {}
    problems.raise_if_any()

    if unknown:
        warnings.append(f"Columns not recognised and ignored: {', '.join(dict.fromkeys(unknown))}.")
    for title in skipped:
        warnings.append(f"Sheet '{title}' was not read (only the first sheet with the column names, and a sheet named "
                        f"Notes, are read).")
    same = Counter(tuple(sorted((k, v) for k, v in row.items() if k != "source_ref")) for row in rows)
    repeated = sum(count - 1 for count in same.values() if count > 1)
    if repeated:
        warnings.append(f"{repeated} row{'s are exact copies' if repeated != 1 else ' is an exact copy'} of an earlier "
                        f"row (same labels, payouts and remarks); {'they were' if repeated != 1 else 'it was'} kept.")
    for insurer in notes:
        if insurer not in insurers:
            warnings.append(f"Notes were given for '{insurer}', which has no rows in the file.")

    counts = {}
    for position, row in enumerate(rows):
        row["position"] = position
        counts[row["insurer"]] = counts.get(row["insurer"], 0) + 1       # in the order the insurers first appear
    return LifeWorkbook(rows=rows, notes=notes, warnings=warnings, sheet_counts=counts)


# ----------------------------------------------------------------------------------------------- writing a file
_HEADER_FILL = PatternFill("solid", start_color="E2E8F0")
_COLUMN_GUIDE = (
    ("insurer", "Required", "The insurer, spelled the same on every row and the same as in the live grid (for example "
                            "Bajaj Allianz Life). The insurer aliases and plan mappings use this name."),
    ("category", "", "The grid's own wording (PAR(Trad), Non PAR, Term, ULIP, Pension, Annuity ...). The Rate Checker "
                     "reads it into its seven categories."),
    ("product", "Required", "The plan name as the grid writes it."),
    ("variant", "", "The plan's variant or option, if the grid has one."),
    ("ppt", "", "Premium paying term as the grid writes it: 5, 5-10, >=10, 10+, Single Pay, Regular Pay."),
    ("pt", "", "Policy term as the grid writes it: 20, 15/20, >=20, Whole Life."),
    ("condition", "", "A condition that selects the row (sum assured band, channel, ticket size ...)."),
    ("from_date, to_date", "", "The dates the row applies between, both included, as DD/MM/YYYY or an Excel date. An "
                               "empty one means no limit on that side; a row with neither applies to any date. The MIS "
                               "engine reads them against the policy's inception date, the Rate Checker against the "
                               "date typed on its page. A note such as Valid till 30th Nov belongs here, not in remarks."),
    ("payout_yr1", "One BG payout (or a rule in remarks)", "Year 1 BG payout, in percent (35.5 means 35.5%). The "
                                                           "partner payout the Rate Checker shows."),
    ("payout_deferred", "", "The part of the BG payout paid at the 2nd year on persistency (%)."),
    ("payout_yr2 ... payout_yr5", "", "Year 2 to year 5 BG payout (%)."),
    ("payout_yr6_plus", "", "Year 6 and later BG payout (%)."),
    ("mg_yr1 ... mg_yr6_plus", "", "The same years as the insurer pays the broker (MG, %). Optional; shown on the Life "
                                   "Rate Master only, never on the Rate Checker."),
    ("remarks", "", "A written rule that is not a number (for example Only 7 Pay -1.5%)."),
    ("sheet, section, source_ref", "", "Optional: where the row came from. An empty source_ref becomes Row N of the "
                                       "file."),
)
_RULES = (
    "One row is one payout row of an insurer's grid: a plan, its PPT / PT / condition, and the payout of each year.",
    "Fill the sheet Rates. The first row holds the column names (the order does not matter; the names do, so do not "
    "rename them). The sheet Notes (insurer, note) is optional and carries each insurer's terms and conditions.",
    "Payouts are percent numbers: 35.5 means 35.5%. A cell Excel shows as 35.5% is read as 35.5. Leave a cell empty "
    "when there is no payout; 0 means a payout of zero.",
    "A row needs an insurer, a product and at least one BG payout. MG is optional. A row that is a written rule and not "
    "a number (for example Year 1: 30% of 1st year commission) has no payout: write the rule in remarks.",
    "Spell each insurer the same way on every row, and the same way as in the live grid.",
    "Dates (from_date, to_date) are optional and both ends are included: a row valid till 30/11/2026 still applies on "
    "30/11/2026. Leave both empty for a row that always applies. When a rate changes on a date, give the old row a "
    "to_date and the new row a from_date (the review warns when two windows of one plan overlap).",
    "Type PPT and PT as text (5, 5-10, >=10, 15/20). The template formats these columns as Text so that Excel does "
    "not turn 5-10 into a date.",
    "The whole file is checked before anything is saved. One bad row stops the upload, and every problem is listed "
    "with its row number.",
    "An upload is stored but not live: you review what changed against the live grid, then press Make live.",
    "To change a few insurers, use Export Excel on the Life Rate Master (it is a valid standard file), edit the rows "
    "and upload it back.",
)
_EXAMPLE = (
    ("insurer", "category", "product", "ppt", "pt", "from_date", "to_date", "payout_yr1", "payout_yr2", "mg_yr1",
     "mg_yr2", "remarks"),
    ("Example Life", "PAR(Trad)", "Example Plan", ">=10", "", "01/11/2026", "30/11/2026", 50, 2.5, 66.7, 5,
     "Campaign rate"),
    ("Example Life", "TERM", "Example Term Plan", "Single Pay", "20", "", "", 30, "", 40, "", ""),
)


def _style_header(ws, width_of=None):
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    ws.freeze_panes = "A2"
    for index, cell in enumerate(ws[1], start=1):
        ws.column_dimensions[get_column_letter(index)].width = (width_of or {}).get(cell.value, 14)


def standard_template_workbook():
    """The empty standard file: the sheets Rates and Notes with their header rows only (so an unfilled template is
    refused instead of loading examples), and a Read me sheet that explains every column."""
    wb = Workbook()
    rates = wb.active
    rates.title = RATES_SHEET
    rates.append(list(STANDARD_COLUMNS))
    widths = {"insurer": 24, "category": 18, "product": 34, "variant": 22, "ppt": 12, "pt": 12, "condition": 22,
              "from_date": 12, "to_date": 12, "remarks": 40, "sheet": 14, "section": 18, "source_ref": 14}
    _style_header(rates, widths)
    for index, name in enumerate(STANDARD_COLUMNS, start=1):
        # ppt / pt (and the other text columns) as Text, so Excel keeps "5-10" as typed instead of making it a date;
        # the two date columns as real dates, shown the way they are written in India
        if name in PAYOUT_COLUMNS:
            number_format = "0.####"
        elif name in DATE_COLUMNS:
            number_format = "DD/MM/YYYY"
        else:
            number_format = "@"
        rates.column_dimensions[get_column_letter(index)].number_format = number_format

    notes = wb.create_sheet(NOTES_SHEET)
    notes.append(list(NOTES_COLUMNS))
    _style_header(notes, {"insurer": 24, "note": 90})
    notes.column_dimensions["B"].number_format = "@"

    guide = wb.create_sheet(README_SHEET)
    guide.append(["Life payout rates: the standard file"])
    guide["A1"].font = Font(bold=True, size=14)
    guide.append([])
    for rule in _RULES:
        guide.append([f"- {rule}"])
    guide.append([])
    guide.append(["Columns of the sheet Rates"])
    guide[f"A{guide.max_row}"].font = Font(bold=True)
    guide.append(["Column", "Needed", "What it holds"])
    for cell in guide[guide.max_row]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    for row in _COLUMN_GUIDE:
        guide.append(list(row))
    guide.append([])
    guide.append(["An example (these rows are not in the Rates sheet)"])
    guide[f"A{guide.max_row}"].font = Font(bold=True)
    for index, row in enumerate(_EXAMPLE):
        guide.append(list(row))
        if index == 0:
            for cell in guide[guide.max_row]:
                cell.font = Font(bold=True)
                cell.fill = _HEADER_FILL
    guide.column_dimensions["A"].width = 30
    guide.column_dimensions["B"].width = 16
    guide.column_dimensions["C"].width = 70
    for row in guide.iter_rows(min_row=3):
        for cell in row:
            cell.alignment = Alignment(wrap_text=cell.column == 3, vertical="top")
    return wb


def add_notes_sheet(wb, notes):
    """Add the Notes sheet to a workbook: `notes` is [(insurer, text)]. Does nothing when there are none."""
    notes = [(insurer, text) for insurer, text in notes if (text or "").strip()]
    if not notes:
        return
    ws = wb.create_sheet(NOTES_SHEET)
    ws.append(list(NOTES_COLUMNS))
    _style_header(ws, {"insurer": 24, "note": 90})
    for insurer, text in notes:
        ws.append([insurer, text])
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"                  # text that starts with "=" stays text, not a formula
        ws.cell(row=ws.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
