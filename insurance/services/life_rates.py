"""Life payout rates shared by the Life Rate Checker, the admin pages and the tests.

Saving an upload (read from the workbook by ``services/life_workbook.py`` or from the standard file by
``services/life_standard.py``), switching the active upload, and the search the Rate Checker runs over the active
upload's rows.
"""
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Count, F, Max, Min, Q
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from ..health_grid_utils import parse_grid_date
from ..models import LifeGridUpload, LifeInsurerNote, LifeRate
from . import life_filters
from .life_standard import add_notes_sheet

LIFE_MAX_RESULTS = 300

# The Life Rate Master pages through every stored row of an upload in these page sizes.
MASTER_PAGE_SIZES = (50, 100, 200)
MASTER_DEFAULT_PAGE_SIZE = 50
BLANK_CATEGORY = "__blank__"     # the Category filter's value for rows that have no category

# workbook field (services/life_workbook.PAYOUT_FIELDS) -> LifeRate column
PAYOUT_COLUMNS = {
    "y1": "payout_yr1", "d": "payout_deferred", "y2": "payout_yr2", "y3": "payout_yr3",
    "y4": "payout_yr4", "y5": "payout_yr5", "y6": "payout_yr6_plus",
}
# workbook field (services/life_workbook.MG_FIELDS) -> LifeRate column of the Final MG beside that BG payout
MG_COLUMNS = {
    "m_y1": "mg_yr1", "m_d": "mg_deferred", "m_y2": "mg_yr2", "m_y3": "mg_yr3",
    "m_y4": "mg_yr4", "m_y5": "mg_yr5", "m_y6": "mg_yr6_plus",
}
# column -> the header the Rate Checker shows
PAYOUT_HEADERS = [
    ("payout_yr1", "Year 1"), ("payout_deferred", "At 2nd year"), ("payout_yr2", "Year 2"),
    ("payout_yr3", "Year 3"), ("payout_yr4", "Year 4"), ("payout_yr5", "Year 5"), ("payout_yr6_plus", "Year 6+"),
]
# the same years for the MG columns (the Life Rate Master shows both)
MG_HEADERS = [(mg, header) for mg, (_, header) in zip(LifeRate.MG_FIELDS, PAYOUT_HEADERS)]

# A previous insurer that lost this share of its rows is worth a second look before the new upload goes live.
DROP_WARNING_RATIO = 0.75


def default_label(file_name):
    """'Life_Grid_working-Oct'26.xlsx' -> "Oct'26"; any other name -> the file name without its extension."""
    stem = re.sub(r"\.(?:xlsx?|csv)$", "", file_name or "", flags=re.I)
    m = re.search(r"([A-Za-z]{3,9})[\s'’_-]*(\d{2,4})$", stem)
    if m:
        return f"{m.group(1).title()[:3]}'{m.group(2)[-2:]}"
    return stem[:100] or "Life grid"


def _compare_with_previous(sheet_counts, previous):
    """Warnings for insurers whose rows disappeared or shrank a lot since the previous upload."""
    if previous is None:
        return []
    before = dict(previous.rates.values_list("insurer").annotate(n=Count("id")).values_list("insurer", "n"))
    notes = []
    for insurer, count in sorted(before.items()):
        now = sheet_counts.get(insurer, 0)
        if now == 0:
            notes.append(f"{insurer} had {count} rows in '{previous.label}' and has none in this upload.")
        elif now < count * DROP_WARNING_RATIO:
            notes.append(f"{insurer} went from {count} rows in '{previous.label}' to {now}.")
    for insurer in sorted(set(sheet_counts) - set(before)):
        notes.append(f"{insurer} is new: {sheet_counts[insurer]} rows.")
    return notes


@transaction.atomic
def save_life_upload(result, *, label, file_name, user, make_live=True):
    """Store a read file (services.life_workbook.LifeWorkbook) as an upload; returns it.

    By default it becomes the live grid at once. With make_live=False it is stored as a pending upload (waiting for
    review) and the live grid is left exactly as it was."""
    previous = LifeGridUpload.objects.filter(is_active=True).first()
    warnings = list(result.warnings) + _compare_with_previous(result.sheet_counts, previous)
    if make_live:
        LifeGridUpload.objects.filter(is_active=True).update(is_active=False)
    upload = LifeGridUpload.objects.create(
        label=label, file_name=file_name[:255], uploaded_by=user, is_active=make_live, is_pending=not make_live,
        row_count=len(result.rows), insurer_count=len(result.sheet_counts), warnings="\n".join(warnings),
    )
    rates = []
    for row in result.rows:
        values = {column: row.get(field) for field, column in {**PAYOUT_COLUMNS, **MG_COLUMNS}.items()}
        rates.append(LifeRate(
            upload=upload, insurer=row["insurer"], sheet=row["sheet"], section=row["section"],
            category=row["category"], product=row["product"], variant=row["variant"], ppt=row["ppt"],
            pt=row["pt"], condition=row["condition"], remarks=row["remarks"], source_ref=row["source_ref"],
            from_date=row.get("from_date"), to_date=row.get("to_date"),        # only the standard file has dates
            position=row["position"], **values,
        ))
    LifeRate.objects.bulk_create(rates, batch_size=500)
    LifeInsurerNote.objects.bulk_create(
        [LifeInsurerNote(upload=upload, insurer=insurer, text=text) for insurer, text in result.notes.items()])
    return upload


@transaction.atomic
def activate_life_upload(upload):
    LifeGridUpload.objects.exclude(pk=upload.pk).filter(is_active=True).update(is_active=False)
    # Written straight to the database: the instance passed in may be stale (another upload can have
    # switched it off since it was loaded), so its own is_active says nothing reliable.
    LifeGridUpload.objects.filter(pk=upload.pk).update(is_active=True, is_pending=False)
    upload.is_active, upload.is_pending = True, False
    return upload


def discard_life_upload(upload):
    """Delete an upload that was never made live (one waiting for review), with its rows and notes. Anything else
    (the live grid, an earlier upload) is history and is refused."""
    if upload.is_active or not upload.is_pending:
        raise ValueError("Only an upload that is waiting for review can be discarded.")
    upload.delete()


@transaction.atomic
def delete_life_upload(upload):
    """Delete an upload with its rows and notes: an earlier upload, or one waiting for review (so test uploads can be
    cleaned up). The live grid is refused, because the Rate Checker and the MIS engine read it: make another upload live
    first. The upload is read again under a lock, since the instance passed in may be stale (another tab can have made
    it live, or deleted it, since the page was loaded)."""
    try:
        current = LifeGridUpload.objects.select_for_update().get(pk=upload.pk)
    except LifeGridUpload.DoesNotExist:
        raise ValueError("That upload has already been deleted.")
    if current.is_active:
        raise ValueError("This is the live grid, so it cannot be deleted. Make another upload live first, then delete this one.")
    current.delete()


def active_life_upload():
    return LifeGridUpload.objects.filter(is_active=True).first()


def _pick(params, name, allowed, upper=False):
    """A filter value if it is one of the fixed values the page offers, else "" (an unknown value is ignored)."""
    value = (params.get(name) or "").strip()
    value = value.upper() if upper else value
    return value if value in allowed else ""


def selected_life_filters(params):
    """The Rate Checker's filters as the page and the search read them from the query string. Category, Payment Mode,
    PPT and PT take only the fixed values of services/life_filters.py; anything else counts as not picked. The date is
    the day the rate is wanted for (the page's date box, YYYY-MM-DD or DD/MM/YYYY); one that is not a date is ignored."""
    return {
        "insurers": [v for v in params.getlist("insurer") if v],
        "category": _pick(params, "category", life_filters.CATEGORIES, upper=True),
        "payment_mode": _pick(params, "payment_mode", life_filters.PAYMENT_MODES, upper=True),
        "ppt": _pick(params, "ppt", life_filters.PPT_CHOICES),
        "pt": _pick(params, "pt", life_filters.PT_CHOICES),
        "product": (params.get("product") or "").strip(),
        "q": (params.get("q") or "").strip(),
        "target_date": parse_grid_date((params.get("target_date") or "").strip()),
    }


def _rank(row):
    """Best first-year payout first, blanks last."""
    return (row.payout_yr1 is None, -(row.payout_yr1 or 0.0), row.insurer, row.product, row.position, row.id)


def search_life_rates(upload, selected):
    """(rows, total matches) of an upload, ranked by the first-year payout, best first. Rows are capped at
    LIFE_MAX_RESULTS; blank payouts sort last. Insurer, product and the search words are SQL filters; Category,
    Payment Mode, PPT and PT are read against the rows' own wording by services/life_filters.py (an upload is a
    few thousand rows, so that runs over the rows the SQL filters leave). The Final MG is never loaded: the Rate
    Checker shows the partner payout (BG) only."""
    qs = LifeRate.objects.filter(upload=upload).defer(*LifeRate.MG_FIELDS)
    target = selected.get("target_date")
    if target:                      # the same inclusive, open-ended window check Health Payout Rates uses
        qs = qs.filter((Q(from_date__lte=target) | Q(from_date__isnull=True))
                       & (Q(to_date__gte=target) | Q(to_date__isnull=True)))
    if selected["insurers"]:
        qs = qs.filter(insurer__in=selected["insurers"])
    if selected["product"]:
        qs = qs.filter(product=selected["product"])
    for term in selected["q"].split():
        qs = qs.filter(
            Q(insurer__icontains=term) | Q(category__icontains=term) | Q(product__icontains=term)
            | Q(variant__icontains=term) | Q(ppt__icontains=term) | Q(pt__icontains=term)
            | Q(condition__icontains=term) | Q(remarks__icontains=term) | Q(section__icontains=term)
        )
    if not any(selected[key] for key in ("category", "payment_mode", "ppt", "pt")):
        qs = qs.order_by(F("payout_yr1").desc(nulls_last=True), "insurer", "product", "position", "id")
        return list(qs[:LIFE_MAX_RESULTS]), qs.count()
    rows = sorted((row for row in qs if life_filters.row_matches(row, selected)), key=_rank)
    return rows[:LIFE_MAX_RESULTS], len(rows)


def life_filter_rows(upload):
    """[insurer, "CATEGORY|CATEGORY", product] for every distinct insurer + product of an upload: the page narrows
    its Product dropdown from this when an insurer or category is picked."""
    keys = {}
    for insurer, category, product in LifeRate.objects.filter(upload=upload).values_list(
            "insurer", "category", "product").distinct():
        keys.setdefault((insurer, product), set()).update(life_filters.categories_of(category, product))
    return [[insurer, "|".join(sorted(found)), product] for (insurer, product), found in sorted(keys.items())]


def life_notes_for(upload, insurers):
    """{insurer: text} of the notes for the insurers shown (an insurer whose notes were emptied on screen has none)."""
    found = LifeInsurerNote.objects.filter(upload=upload, insurer__in=insurers).values_list("insurer", "text")
    return {insurer: text for insurer, text in found if text.strip()}


# -------------------------
# Life Rate Master: every stored row of an upload, for viewing and editing
# -------------------------
def selected_master_filters(params):
    """The Life Rate Master's filters as read from the query string. Insurer and Category are the stored values (the
    page checks them against the upload), q needs every word, edited limits the list to rows changed on screen."""
    try:
        page_size = int(params.get("page_size") or MASTER_DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        page_size = MASTER_DEFAULT_PAGE_SIZE
    return {
        "insurer": (params.get("insurer") or "").strip(),
        "category": (params.get("category") or "").strip(),
        "q": (params.get("q") or "").strip(),
        "edited": params.get("edited") == "1",
        "page_size": page_size if page_size in MASTER_PAGE_SIZES else MASTER_DEFAULT_PAGE_SIZE,
    }


def master_filter_options(upload):
    """What the Life Rate Master's dropdowns offer for an upload: the insurers in workbook order with their row
    counts, the distinct stored categories, and whether some rows have no category."""
    rows = LifeRate.objects.filter(upload=upload)
    insurers = list(
        rows.values("insurer").annotate(n=Count("id"), first=Min("position")).order_by("first").values_list("insurer", "n"))
    categories = sorted(rows.exclude(category="").values_list("category", flat=True).distinct())
    return {"insurers": insurers, "categories": categories, "has_blank_category": rows.filter(category="").exists()}


def clean_master_filters(selected, options):
    """The filters with an insurer or category the upload does not have counted as not picked."""
    cleaned = dict(selected)
    if cleaned["insurer"] not in {name for name, _ in options["insurers"]}:
        cleaned["insurer"] = ""
    allowed = set(options["categories"]) | ({BLANK_CATEGORY} if options["has_blank_category"] else set())
    if cleaned["category"] not in allowed:
        cleaned["category"] = ""
    return cleaned


def master_rows(upload, selected):
    """Every stored payout row of an upload in workbook order, narrowed by the filters (selected_master_filters)."""
    qs = LifeRate.objects.filter(upload=upload).select_related("edited_by")
    if selected["insurer"]:
        qs = qs.filter(insurer=selected["insurer"])
    if selected["category"] == BLANK_CATEGORY:
        qs = qs.filter(category="")
    elif selected["category"]:
        qs = qs.filter(category=selected["category"])
    if selected["edited"]:
        qs = qs.filter(is_edited=True)
    for term in selected["q"].split():
        qs = qs.filter(
            Q(insurer__icontains=term) | Q(category__icontains=term) | Q(product__icontains=term)
            | Q(variant__icontains=term) | Q(ppt__icontains=term) | Q(pt__icontains=term)
            | Q(condition__icontains=term) | Q(remarks__icontains=term) | Q(section__icontains=term)
            | Q(source_ref__icontains=term)
        )
    return qs.order_by("position", "id")


def master_stats(upload):
    """{rows, insurers, edited, with_mg} of an upload, counted from its stored rows (with_mg: the rows that have any
    Final MG; none for an upload made before MG was kept)."""
    has_mg = Q()
    for name in LifeRate.MG_FIELDS:
        has_mg |= Q(**{f"{name}__isnull": False})
    return LifeRate.objects.filter(upload=upload).aggregate(
        rows=Count("id"), insurers=Count("insurer", distinct=True), edited=Count("id", filter=Q(is_edited=True)),
        with_mg=Count("id", filter=has_mg))


# -------------------------
# Life Rate Master: add a row, delete a row
# -------------------------
def refresh_upload_counts(upload):
    """Bring an upload's stored row and insurer counts (shown on the Uploads page and the Rate Checker) back in line
    with its rows after one was added or deleted."""
    stats = master_stats(upload)
    LifeGridUpload.objects.filter(pk=upload.pk).update(row_count=stats["rows"], insurer_count=stats["insurers"])
    upload.row_count, upload.insurer_count = stats["rows"], stats["insurers"]


def _position_after_insurer(upload, insurer):
    """The workbook position for a new row of `insurer`: right after that insurer's last row (the rows below move
    down by one), or the very end for an insurer the upload does not have yet."""
    rows = LifeRate.objects.filter(upload=upload)
    last = rows.filter(insurer=insurer).aggregate(last=Max("position"))["last"]
    if last is None:
        top = rows.aggregate(top=Max("position"))["top"]
        return 0 if top is None else top + 1
    rows.filter(position__gt=last).update(position=F("position") + 1)
    return last + 1


@transaction.atomic
def add_life_rate(upload, values, user):
    """Store a row typed in on screen (values: the cleaned LifeRateAddForm data) in an upload. It is marked as added
    and edited on screen, takes the sheet of its insurer's other rows and goes after the last of them."""
    sheet = LifeRate.objects.filter(upload=upload, insurer=values["insurer"]).values_list("sheet", flat=True).first() or ""
    position = _position_after_insurer(upload, values["insurer"])
    row = LifeRate.objects.create(
        upload=upload, sheet=sheet, position=position, source_ref=LifeRate.MANUAL_SOURCE_REF,
        is_edited=True, edited_at=timezone.now(), edited_by=user, **values)
    refresh_upload_counts(upload)
    return row


@transaction.atomic
def delete_life_rate(row):
    """Delete a stored row and keep its upload's counts right."""
    upload = row.upload
    row.delete()
    refresh_upload_counts(upload)


SNAPSHOT_FIELDS = ("insurer", "sheet", "section", "category", "product", "variant", "ppt", "pt", "condition",
                   "from_date", "to_date", *LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS, "remarks", "source_ref")


def life_rate_snapshot(row):
    """Every filled value of a row on one line, for the audit log (a deleted row can be typed in again from it)."""
    def shown(value):
        return value.isoformat() if isinstance(value, date) else value
    return "; ".join(f"{name}={shown(getattr(row, name))!r}" for name in SNAPSHOT_FIELDS
                     if getattr(row, name) not in (None, ""))


# -------------------------
# Life Rate Master: bulk edit
# -------------------------
# The fields a bulk edit may change. Everything else is refused: a row's identity (id, upload, insurer, sheet, section,
# workbook cell, position) and the edit stamps (is_edited, edited_at, edited_by) are set by the system, never by hand.
# The seven MG and the seven BG percentages are stored values read from the workbook (its Final MG and Final BG); they
# are independent: changing one never recomputes the other.
BULK_PAYOUT_FIELDS = {**dict(MG_HEADERS), **dict(PAYOUT_HEADERS)}      # "payout" kind: a percent between 0 and 100
BULK_TEXT_FIELDS = {
    "category": "Category", "product": "Product", "variant": "Variant", "ppt": "PPT", "pt": "PT",
    "condition": "Condition", "remarks": "Remarks",
}
BULK_FIELD_LABELS = {
    **{name: f"{header} MG" for name, header in MG_HEADERS},
    **{name: f"{header} BG payout" for name, header in PAYOUT_HEADERS},
    **BULK_TEXT_FIELDS,
}
BULK_OPERATIONS = {
    "payout": ("set", "add", "multiply", "clear"),
    "text": ("set", "clear"),
    "remarks": ("set", "append", "clear"),
}
PAYOUT_PRECISION = Decimal("0.0001")           # the workbook's own precision (a renewal such as 0.4237 %)


def bulk_field_kind(column):
    """'payout', 'remarks' or 'text' for a field a bulk edit may change; '' for anything else."""
    if column in BULK_PAYOUT_FIELDS:
        return "payout"
    if column == "remarks":
        return "remarks"
    return "text" if column in BULK_TEXT_FIELDS else ""


def bulk_field_choices():
    """The Field dropdown of the bulk edit, in three groups."""
    return [
        ("MG (%)", [(name, f"{header} MG") for name, header in MG_HEADERS]),
        ("BG payouts (%)", [(name, f"{header} BG payout") for name, header in PAYOUT_HEADERS]),
        ("Details", list(BULK_TEXT_FIELDS.items())),
    ]


@dataclass
class BulkPlan:
    """What a bulk operation would do, worked out before anything is saved."""
    changes: list = dataclass_field(default_factory=list)   # (row, old value, new value) of every row that changes
    unchanged: int = 0                                      # rows that already have the value the operation gives
    skipped: int = 0                                        # add / multiply on a blank payout: nothing to work from
    errors: list = dataclass_field(default_factory=list)    # why it cannot be applied; with any, nothing is saved


def _rounded(number):
    return float(number.quantize(PAYOUT_PRECISION, rounding=ROUND_HALF_UP))


def _payout_after(old, operation, value):
    """(new payout, skipped) after a bulk operation; `value` is a Decimal (the amount, or the factor for multiply)."""
    if operation == "clear":
        return None, False
    if operation == "set":
        return _rounded(value), False
    if old is None:
        return None, True
    base = Decimal(str(old))
    return _rounded(base + value if operation == "add" else base * value), False


def _text_after(old, operation, value):
    old = old or ""
    if operation == "clear":
        return ""
    if operation == "append":
        return f"{old} {value}".strip()
    return value


def plan_bulk_update(rows, column, operation, value):
    """Work out what a bulk operation does to each row, without saving anything. `value` is a Decimal for the payout
    operations (the factor for multiply), the new text for set / append on a text field, None for clear.

    Each new payout is rounded to 4 decimals and must stay between 0 and 100 (the rule of the single-row edit); a row
    that would leave that range makes the whole operation refuse, so a bulk edit never half-applies."""
    kind = bulk_field_kind(column)
    label = BULK_FIELD_LABELS[column]
    max_length = None if kind == "payout" else LifeRate._meta.get_field(column).max_length
    plan = BulkPlan()
    for row in rows:
        old = getattr(row, column)
        if kind == "payout":
            new, skipped = _payout_after(old, operation, value)
            if skipped:
                plan.skipped += 1
                continue
            if new is not None and not 0 <= new <= 100:
                plan.errors.append(
                    f"#{row.id} ({row.insurer} | {row.product or 'no product'}): {label} would become {new:g}, "
                    f"outside 0-100.")
                continue
        else:
            new = _text_after(old, operation, value)
            if max_length and len(new) > max_length:
                plan.errors.append(f"#{row.id}: {label} would be {len(new)} characters long; the limit is {max_length}.")
                continue
        if new == old:
            plan.unchanged += 1
        else:
            plan.changes.append((row, old, new))
    return plan


@transaction.atomic
def save_bulk_changes(plan, column, user):
    """Write a plan's changes in one go; every changed row is stamped as edited on screen. Returns how many changed."""
    now = timezone.now()
    rows = []
    for row, _old, new in plan.changes:
        setattr(row, column, new)
        row.is_edited, row.edited_at, row.edited_by = True, now, user
        rows.append(row)
    LifeRate.objects.bulk_update(rows, [column, "is_edited", "edited_at", "edited_by"], batch_size=500)
    return len(rows)


def describe_bulk(column, operation, value):
    """'add 0.5 percentage points to Year 1 payout': the operation in words, for messages and the audit log."""
    label = BULK_FIELD_LABELS[column]
    if operation == "clear":
        return f"clear {label}"
    if bulk_field_kind(column) == "payout":
        number = format(value.normalize(), "f")
        return {"set": f"set {label} to {number}%", "add": f"add {number} percentage points to {label}",
                "multiply": f"multiply {label} by {number}"}[operation]
    return f"append {value!r} to {label}" if operation == "append" else f"set {label} to {value!r}"


def bulk_audit_details(upload, column, operation, value, scope, plan, limit=100):
    """The audit log entry of one bulk edit: what was done, to which rows, and the first `limit` before -> after values."""
    parts = [
        f"Bulk update on '{upload.label}': {describe_bulk(column, operation, value)}.",
        f"Rows: {scope}; {len(plan.changes)} changed, {plan.unchanged} already had that value, "
        f"{plan.skipped} skipped (blank payout).",
    ]
    shown = [f"#{row.id}: {old!r} -> {new!r}" for row, old, new in plan.changes[:limit]]
    if shown:
        more = f"; ... and {len(plan.changes) - limit} more" if len(plan.changes) > limit else ""
        parts.append("Changes (id: before -> after): " + "; ".join(shown) + more)
    return " ".join(parts)


# -------------------------
# Life Rate Master: Excel export of the data grid
# -------------------------
# The header is the stored field names, exactly (the same convention as the Motor and Health exports).
EXPORT_COLUMNS = (
    "id", "upload", "insurer", "sheet", "section", "category", "product", "variant", "ppt", "pt", "condition",
    "from_date", "to_date", *LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS, "remarks", "source_ref", "position", "is_edited", "edited_by",
    "edited_at",
)


def _export_value(row, upload, column):
    if column == "upload":
        return upload.label
    if column == "is_edited":
        return "YES" if row.is_edited else "NO"
    if column == "edited_by":
        return row.edited_by.username if row.edited_by else ""
    if column == "edited_at":
        return timezone.localtime(row.edited_at).strftime("%Y-%m-%d %H:%M:%S") if row.edited_at else ""
    value = getattr(row, column)
    return "" if value is None else value


def life_export_workbook(rows, upload):
    """An Excel workbook of the given rows (the data grid with its filters applied): one line per stored row with every
    stored value, the payouts as plain percent numbers (47.5 means 47.5 %)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Life rates"
    ws.append(list(EXPORT_COLUMNS))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    payout_columns = {EXPORT_COLUMNS.index(name) for name in (*LifeRate.MG_FIELDS, *LifeRate.PAYOUT_FIELDS)}
    date_columns = {EXPORT_COLUMNS.index(name) for name in ("from_date", "to_date")}
    insurers = {}                                         # the insurers in the export, in the order they first appear
    for row in rows:
        insurers.setdefault(row.insurer)
        ws.append([_export_value(row, upload, column) for column in EXPORT_COLUMNS])
        for index, cell in enumerate(ws[ws.max_row]):
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"                      # remark text that starts with "=" stays text, not a formula
            elif index in payout_columns:
                cell.number_format = "0.####"
            elif index in date_columns:
                cell.number_format = "DD/MM/YYYY"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for index, column in enumerate(EXPORT_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(index)].width = {
            "product": 34, "variant": 24, "remarks": 40, "condition": 22, "insurer": 24, "category": 18,
            "source_ref": 14, "edited_at": 19, "upload": 12,
        }.get(column, 12)
    # The insurer notes go in a second sheet (services/life_standard.py), so the export is a complete standard file.
    notes = dict(LifeInsurerNote.objects.filter(upload=upload, insurer__in=list(insurers)).values_list("insurer", "text"))
    add_notes_sheet(wb, [(name, notes[name]) for name in insurers if name in notes])
    return wb
