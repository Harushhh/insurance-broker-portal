"""Team Activity: what the people who report to you have been doing (runs, uploads, tests, status changes).

Every query starts from `services.team_scope.visible_events(request.user)` and only narrows it, so a manager
cannot reach another team's rows by editing the URL: a forged `?person=` for someone outside their team just
returns nothing. Do not query `ActivityEvent` directly here (`test_team_activity.StructureTests` checks).
"""
import math
from collections import namedtuple
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from ..models import ActivityEvent
from ..services.activity import SYSTEM_NAME, SYSTEM_USERNAME, detail_rows, retention_cutoff
from ..services.common import safe_cell as _safe_cell     # Excel formula defusing, shared with the other exports
from ..services.team_scope import is_admin, managers_with_teams, team_user_ids, visible_events, visible_people

CATEGORIES = ActivityEvent.CATEGORY_CHOICES                    # [(code, label)], in display order
CATEGORY_CODES = {code for code, _ in CATEGORIES}
DEFAULT_RANGE_DAYS = 7                                         # no date_range in the URL = the last 7 days
PAGE_SIZE_OPTIONS = (50, 100, 200)
DEFAULT_PAGE_SIZE = 50
EXPORT_LIMIT = 20000
MAX_PIVOT_DAYS = 366                                           # most day columns the Pivot by day sheet gets
EVENTS_SHEET = "Team Activity"
CATEGORY_SHEET = "Pivot by category"
DAY_SHEET = "Pivot by day"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DONUT_RADIUS = 44                                              # in the 120 x 120 box the template draws the donut in
DONUT_GAP = 2.0                                                # the surface gap between neighbouring slices, same units
MAX_PEOPLE_BARS = 10                                           # more people than this fold into "+ N more"
MAX_QUIET_NAMES = 8                                            # names listed under "No activity"
EXCEL_CELL_LIMIT = 32000                                       # Excel refuses a cell over 32,767 characters
SYSTEM_PERSON = "system"                                       # the ?person= value that picks the System pseudo-person

# feed: what the list shows; base: what the charts count; scope: everyone the viewer may see in the chosen period
# (before the Search and Action filters), which is where the Action dropdown's choices come from.
Filtered = namedtuple("Filtered", "feed base scope selected")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_range(request):
    """(text for the box, first day or None, last day or None).

    No `date_range` at all means the last 7 days (today included); an empty one means the box was cleared,
    so no date limit. Anything unreadable is treated as cleared rather than guessed.
    """
    raw = request.GET.get("date_range")
    if raw is None:
        last = timezone.localdate()
        first = last - timedelta(days=DEFAULT_RANGE_DAYS - 1)
        return f"{first} - {last}", first, last
    parts = [part.strip() for part in raw.split(" - ")]
    if len(parts) != 2:
        return "", None, None
    try:
        first, last = (date.fromisoformat(part) for part in parts)
    except ValueError:
        return "", None, None
    if first > last:
        first, last = last, first
    return f"{first} - {last}", first, last


def _start_of(day):
    """Midnight at the start of `day` in the site's time zone (IST), as an aware datetime."""
    return timezone.make_aware(datetime.combine(day, time.min))


def _for_person(events, person):
    """`events` narrowed to one person: a user id, or SYSTEM_PERSON for the events nobody signed in made (an API-key
    call, a management command). Only ADMIN's scope holds any System events, so for a manager this finds nothing."""
    if person == SYSTEM_PERSON:
        return events.filter(actor__isnull=True, actor_username=SYSTEM_USERNAME)
    return events.filter(actor_id=person)


def _filtered(request):
    """A `Filtered`: the events for the feed, the events the charts count from, the events in scope, the selected filters.

    The charts count from everything except the Person and Category filters, so each chart keeps showing every
    choice of its own kind (the donut applies Person, the people bars apply Category) and a click can be undone.
    """
    user = request.user
    # Team is an admin-only narrowing; for anyone else it is not even read.
    team = User.objects.filter(pk=_int(request.GET.get("team")) or 0).first() if is_admin(user) else None

    base = visible_events(user)
    if team is not None:
        base = base.filter(actor_id__in=team_user_ids(team))
    range_text, first_day, last_day = _date_range(request)
    if first_day:
        base = base.filter(occurred_at__gte=_start_of(first_day))
    if last_day:
        base = base.filter(occurred_at__lt=_start_of(last_day + timedelta(days=1)))
    scope = base
    q = (request.GET.get("q") or "").strip()[:100]
    if q:
        # The details are searched too (a vehicle number, a holder name, a field name), and so is where the event came
        # from (a URL path), inside the same scope.
        base = base.filter(
            Q(summary__icontains=q) | Q(action__icontains=q)
            | Q(actor_username__icontains=q) | Q(actor_name__icontains=q)
            | Q(details__icontains=q) | Q(source_path__icontains=q)
        )
    action = (request.GET.get("action") or "").strip()[:60]
    if action:
        base = base.filter(action=action)

    feed = base
    category = request.GET.get("category", "")
    if category not in CATEGORY_CODES:
        category = ""
    if category:
        feed = feed.filter(category=category)
    person_text = (request.GET.get("person") or "").strip()
    person = SYSTEM_PERSON if person_text.lower() == SYSTEM_PERSON else _int(person_text)
    if person:
        feed = _for_person(feed, person)

    selected = {
        "team": team, "date_range": range_text, "category": category, "q": q, "person": person, "action": action,
        "first_day": first_day, "last_day": last_day,
    }
    return Filtered(feed, base, scope, selected)


def _chart_link(selected):
    """link(**changes): this page's URL with the filters in force, changed as asked. A value of None drops that
    filter; `page` is never carried, so a click lands on page 1."""
    def link(**changes):
        params = {"date_range": selected["date_range"]}
        if selected["team"] is not None:
            params["team"] = selected["team"].pk
        for key in ("category", "q", "person", "action"):
            if selected[key]:
                params[key] = selected[key]
        params.update(changes)
        return "?" + urlencode({key: value for key, value in params.items() if value is not None})
    return link


def _percent_text(n, total):
    if not n or not total:
        return "0%"
    percent = round(100 * n / total)
    return f"{percent}%" if percent else "<1%"


def _donut_slices(parts, total):
    """Arc geometry for the donut. `parts` is [(code, label, n)] in drawing order, clockwise from 12 o'clock.

    Each slice is one dash of a circle: its share of the circumference less DONUT_GAP, so neighbours are separated
    by the card's own surface rather than by a border. A lone slice is the whole ring, with no gap.
    """
    circumference = 2 * math.pi * DONUT_RADIUS
    drawn_count = sum(1 for _, _, n in parts if n)
    slices, start = [], 0.0
    for code, label, n in parts:
        length = circumference * n / total if total else 0.0
        if n:
            gap = 0.0 if drawn_count == 1 else DONUT_GAP
            drawn = max(length - gap, 1.0)
            slices.append({
                "code": code, "label": label, "n": n,
                "dash": f"{drawn:.2f} {circumference - drawn:.2f}",
                "offset": f"{-(start + gap / 2) or 0.0:.2f}",          # `or 0.0`: never print "-0.00"
            })
        start += length
    return slices


def _category_chart(base, selected, link, names):
    """The donut: events by category. It counts the same events as the feed except for the Category filter (so every
    category stays on screen and a slice can be switched off again), but WITH the Person filter, so a click on a
    person shows what that person did. Slices keep a fixed order and a fixed colour per category."""
    events = _for_person(base, selected["person"]) if selected["person"] else base
    counts = dict(events.order_by().values_list("category").annotate(n=Count("id")))
    # Edit (record edits and the audit-log entries) only gets a slice and a legend row when the events being counted
    # include one, so a quiet period keeps the four-category legend.
    columns = [(code, label) for code, label in CATEGORIES if code != ActivityEvent.CATEGORY_EDIT or counts.get(code)]
    parts = [(code, label, counts.get(code, 0)) for code, label in columns]
    total = sum(n for _, _, n in parts)

    legend = []
    for code, label, n in parts:
        active = code == selected["category"]
        legend.append({
            "code": code, "label": label, "n": n, "pct": _percent_text(n, total), "active": active,
            "url": link(category=None if active else code) if (n or active) else "",
        })
    slices = _donut_slices(parts, total)
    for item in slices:
        item["pct"] = _percent_text(item["n"], total)
        item["active"] = item["code"] == selected["category"]
        item["url"] = link(category=None if item["active"] else item["code"])

    spoken = ", ".join(f"{label} {n}" for _, label, n in parts if n) or "no events"
    # A name is only ever shown for someone the viewer may see: a forged ?person= must not reveal who an id belongs to.
    scope = names.get(selected["person"], "Selected person") if selected["person"] else "Everyone"
    return {
        "radius": DONUT_RADIUS, "total": total, "long_total": total >= 10000, "slices": slices, "legend": legend,
        "aria": f"Events by category, {total} in all: {spoken}", "scope": scope,
    }


def _people_chart(request, base, selected, link, people):
    """The ranked bars: events per person. They count the same events as the feed except for the Person filter (so
    everyone stays on screen and a bar can be switched off again), but WITH the Category filter. One hue, biggest
    first, the value at the tip of the bar; people with nothing are listed underneath instead of drawn as empty bars."""
    events = base.filter(category=selected["category"]) if selected["category"] else base
    counts = {actor: n for actor, n in events.order_by().values_list("actor_id").annotate(n=Count("id")) if actor}

    pool = list(people)
    if is_admin(request.user) and selected["team"] is None:
        # Everyone who ever did anything would be the whole organisation: show who acted in this slice.
        pool = [person for person in pool if person.pk in counts]
    rows = []
    for person in pool:
        profile = getattr(person, "profile", None)
        rows.append({
            "id": person.pk, "username": person.username, "name": person.get_full_name() or person.username,
            "designation": (profile.designation if profile else "") or "", "n": counts.get(person.pk, 0),
        })
    if is_admin(request.user):
        # System (nobody signed in: API-key calls, management commands) gets a bar of its own, for the one role that can
        # read it. With a Team picked the base has no System events, so it never shows then.
        system_n = events.order_by().filter(actor__isnull=True, actor_username=SYSTEM_USERNAME).count()
        if system_n:
            rows.append({
                "id": SYSTEM_PERSON, "username": SYSTEM_USERNAME, "name": SYSTEM_NAME,
                "designation": "no signed-in user", "n": system_n,
            })
    rows.sort(key=lambda row: (-row["n"], row["name"].lower()))
    active_rows = [row for row in rows if row["n"]]
    quiet = [row for row in rows if not row["n"]]

    shown, rest = active_rows[:MAX_PEOPLE_BARS], active_rows[MAX_PEOPLE_BARS:]
    picked = next((row for row in rest if row["id"] == selected["person"]), None)
    if picked:                                   # whoever is being looked at always keeps a bar
        rest.remove(picked)
        rest.insert(0, shown.pop())
        shown.append(picked)

    total = sum(row["n"] for row in active_rows)
    top = active_rows[0]["n"] if active_rows else 1
    for row in shown:
        is_selected = row["id"] == selected["person"]
        row.update({
            "ratio": f"{row['n'] / top:.4f}", "share": _percent_text(row["n"], total), "active": is_selected,
            "url": link(person=None if is_selected else row["id"]),
        })
    category_label = dict(CATEGORIES).get(selected["category"])
    return {
        "rows": shown, "total": total,
        "more_people": len(rest), "more_events": sum(row["n"] for row in rest),
        "quiet": [row["name"] for row in quiet[:MAX_QUIET_NAMES]], "quiet_more": max(0, len(quiet) - MAX_QUIET_NAMES),
        "scope": f"{category_label} only" if category_label else "All categories",
    }


def _filter_params(selected):
    """The selected filters as a query string, for the Export link (an explicit empty date_range means no limit)."""
    params = {"date_range": selected["date_range"]}
    for key in ("category", "q", "person", "action"):
        if selected[key]:
            params[key] = selected[key]
    if selected["team"] is not None:
        params["team"] = selected["team"].pk
    return urlencode(params)


def team_activity(request):
    filtered = _filtered(request)
    feed, base, selected = filtered.feed, filtered.base, filtered.selected
    admin = is_admin(request.user)

    page_size = _int(request.GET.get("page_size"))
    if page_size not in PAGE_SIZE_OPTIONS:
        page_size = DEFAULT_PAGE_SIZE
    paginator = Paginator(feed.select_related("actor__profile"), page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    events = list(page_obj)
    for event in events:
        event.detail_rows = detail_rows(event.details)        # the exact parameters, as label / value rows

    # Every kind of thing that happened in this period, for the Action filter (the audit log's action names included).
    action_choices = sorted(filtered.scope.order_by().values_list("action", flat=True).distinct(), key=str.lower)
    if selected["action"] and selected["action"] not in action_choices:
        action_choices.append(selected["action"])

    people = sorted(
        visible_people(request.user, team_of=selected["team"]),
        key=lambda person: (person.get_full_name() or person.username).lower(),
    )
    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)
    link = _chart_link(selected)
    names = {person.pk: person.get_full_name() or person.username for person in people}
    names[SYSTEM_PERSON] = SYSTEM_NAME

    return render(request, "team_activity.html", {
        "page_obj": page_obj,
        "events": events,
        "total": paginator.count,
        "action_choices": action_choices,
        "category_chart": _category_chart(base, selected, link, names),
        "people_chart": _people_chart(request, base, selected, link, people),
        "selected": selected,
        "categories": CATEGORIES,
        "people": people,
        "managers": list(managers_with_teams()) if admin else [],
        "is_admin_view": admin,
        "has_team": bool(people) or admin,
        "page_size": page_size,
        "page_size_options": PAGE_SIZE_OPTIONS,
        "page_query": query_without_page.urlencode(),
        "export_query": _filter_params(selected),
        # Events are kept from the 1st of last month (see services.activity.retention_cutoff).
        "retention_from": timezone.localtime(retention_cutoff()).date(),
    })


def _pivot_counts(request, events, selected):
    """(columns, people, by_day): the counts behind the two Pivot sheets.

    Counted from the same filtered events as the Team Activity sheet, so the manager's scope and every filter apply
    and the numbers always agree with it. One grouped query for person x category, one for person x day.

    people: [{"name", "username", "designation", "counts": {category: n}, "total", "last"}], busiest first.
    by_day: {username: {date: n}}, days in IST.
    """
    people = {}
    grouped = events.order_by().values("actor_username", "category").annotate(
        n=Count("id"), last=Max("occurred_at"), name=Max("actor_name"))
    for row in grouped:
        entry = people.setdefault(row["actor_username"], {"counts": {}, "total": 0, "last": None, "name": ""})
        entry["name"] = entry["name"] or row["name"] or ""
        entry["counts"][row["category"]] = row["n"]
        entry["total"] += row["n"]
        if entry["last"] is None or row["last"] > entry["last"]:
            entry["last"] = row["last"]

    # A quiet teammate is worth a row of zeros, but only when nothing narrows the file by content (Person, Category,
    # Action, Search) and the viewer is not an admin looking at everyone (that would list the whole organisation).
    narrowed = bool(selected["person"] or selected["category"] or selected["action"] or selected["q"])
    quiet = []
    if not narrowed and not (is_admin(request.user) and selected["team"] is None):
        quiet = list(visible_people(request.user, team_of=selected["team"]))
    for person in quiet:
        people.setdefault(person.username, {"counts": {}, "total": 0, "last": None, "name": ""})

    live = {person.username: person for person in quiet}
    missing = [username for username in people if username not in live]
    if missing:
        live.update({u.username: u for u in User.objects.filter(username__in=missing).select_related("profile")})

    rows = []
    for username, entry in people.items():
        user = live.get(username)
        profile = getattr(user, "profile", None) if user else None
        rows.append({
            "username": username,
            # The live name when the account still exists, else the name the event was written with.
            "name": (user.get_full_name() if user else "") or entry["name"] or username,
            "designation": (profile.designation if profile else "") or "",
            "counts": entry["counts"], "total": entry["total"], "last": entry["last"],
        })
    rows.sort(key=lambda row: (-row["total"], row["name"].lower()))

    used = {code for entry in people.values() for code in entry["counts"]}
    columns = [(code, label) for code, label in CATEGORIES if code != ActivityEvent.CATEGORY_EDIT or code in used]

    by_day = {}
    daily = events.order_by().annotate(day=TruncDate("occurred_at")).values("actor_username", "day").annotate(n=Count("id"))
    for row in daily:
        by_day.setdefault(row["actor_username"], {})[row["day"]] = row["n"]
    return columns, rows, by_day


def _pivot_days(selected, by_day):
    """(days, clipped): the day columns of the Pivot by day sheet. The chosen date range, or with no limit the span
    of the days that have events; at most MAX_PIVOT_DAYS, the latest ones."""
    first, last = selected["first_day"], selected["last_day"]
    if first is None or last is None:
        seen = {day for counts in by_day.values() for day in counts}
        if not seen:
            return [], False
        first, last = min(seen), max(seen)
    days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    return days[-MAX_PIVOT_DAYS:], len(days) > MAX_PIVOT_DAYS


def _filters_note(selected):
    """One line saying which filters the file was made with, for whoever it is forwarded to."""
    parts = [f"Dates {selected['date_range']} (IST)" if selected["date_range"] else "All dates"]
    if selected["team"] is not None:
        parts.append(f"Team of {selected['team'].get_full_name() or selected['team'].username}")
    if selected["person"]:
        parts.append("System events" if selected["person"] == SYSTEM_PERSON else "One person")
    if selected["category"]:
        parts.append(f"Category {dict(CATEGORIES)[selected['category']]}")
    if selected["action"]:
        parts.append(f"Action {selected['action']}")
    if selected["q"]:
        parts.append(f'Search "{selected["q"]}"')
    return "Same filters as the Team Activity sheet: " + " · ".join(parts)


def _start_pivot_sheet(wb, name, title, notes):
    """A new sheet with a title, the notes and a blank row; returns (sheet, the row the header goes on)."""
    ws = wb.create_sheet(name)
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    for note in notes:
        ws.append([note])
    ws.append([])
    return ws, len(notes) + 3


def _style_header(ws, row):
    for cell in ws[row]:
        cell.fill = PatternFill("solid", fgColor="0F172A")
        cell.font = Font(bold=True, color="FFFFFF")


def _bold(ws, row):
    for cell in ws[row]:
        cell.font = Font(bold=True)


def _write_category_pivot(wb, columns, rows, notes):
    ws, header_row = _start_pivot_sheet(wb, CATEGORY_SHEET, "Events by person and category", notes)
    ws.append(["Person", "Username", "Designation"] + [label for _, label in columns] + ["Total", "Last active (IST)"])
    _style_header(ws, header_row)
    for row in rows:
        ws.append(
            [_safe_cell(row["name"]), _safe_cell(row["username"]), _safe_cell(row["designation"])]
            + [row["counts"].get(code, 0) for code, _ in columns]
            + [row["total"], timezone.localtime(row["last"]).strftime("%Y-%m-%d %H:%M") if row["last"] else ""]
        )
    ws.append(
        ["Total", "", ""]
        + [sum(row["counts"].get(code, 0) for row in rows) for code, _ in columns]
        + [sum(row["total"] for row in rows), ""]
    )
    _bold(ws, ws.max_row)
    for column, width in zip("ABC", (28, 18, 24)):
        ws.column_dimensions[column].width = width
    ws.column_dimensions[ws.cell(row=header_row, column=len(columns) + 5).column_letter].width = 18
    ws.freeze_panes = f"C{header_row + 1}"


def _write_day_pivot(wb, rows, by_day, days, notes):
    ws, header_row = _start_pivot_sheet(wb, DAY_SHEET, "Events by person and day (IST)", notes)
    ws.append(["Person", "Username"] + days + ["Total"])
    _style_header(ws, header_row)
    for cell in ws[header_row][2:2 + len(days)]:
        cell.number_format = "DD MMM"
        cell.alignment = Alignment(horizontal="center")
    for row in rows:
        counts = by_day.get(row["username"], {})
        ws.append(
            [_safe_cell(row["name"]), _safe_cell(row["username"])]
            + [counts.get(day) or None for day in days]          # blank, not 0, when nothing happened that day
            + [sum(counts.get(day, 0) for day in days)]
        )
    per_day = [sum(by_day.get(row["username"], {}).get(day, 0) for row in rows) for day in days]
    ws.append(["Total", ""] + per_day + [sum(per_day)])
    _bold(ws, ws.max_row)
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 18
    ws.freeze_panes = f"C{header_row + 1}"


def export_team_activity_xlsx(request):
    """The feed as an Excel file: same scope and filters as the page, up to EXPORT_LIMIT rows, times in IST.

    Two more sheets count the same events: Pivot by category (person x category) and Pivot by day (person x day).
    """
    filtered = _filtered(request)
    feed, selected = filtered.feed, filtered.selected
    labels = dict(CATEGORIES)

    wb = Workbook()
    ws = wb.active
    ws.title = EVENTS_SHEET
    ws.append(["Time (IST)", "Username", "Name", "Category", "Action", "Summary", "About", "About id", "Source path",
               "Details"])
    written = 0
    for event in feed[:EXPORT_LIMIT].iterator(chunk_size=2000):
        # Every parameter, one "label: value" line each, in full (the page shortens long lists).
        details = "\n".join(f"{label}: {text}" for label, text in detail_rows(event.details, full=True))
        if len(details) > EXCEL_CELL_LIMIT:
            details = details[:EXCEL_CELL_LIMIT] + "\n… (cut here: Excel cells hold at most 32,767 characters)"
        ws.append([
            timezone.localtime(event.occurred_at).strftime("%Y-%m-%d %H:%M:%S"),
            _safe_cell(event.actor_username),
            _safe_cell(event.actor_name or event.actor_username),
            labels.get(event.category, event.category),
            _safe_cell(event.action),
            _safe_cell(event.summary),
            _safe_cell(event.target_type),
            _safe_cell(event.target_id),
            _safe_cell(event.source_path),
            _safe_cell(details),
        ])
        # Wrapped, so the one-line-per-parameter Details read as lines in Excel (header is row 1, this row is written + 2).
        for column in (6, 10):
            ws.cell(row=written + 2, column=column).alignment = Alignment(wrap_text=True, vertical="top")
        written += 1
    _style_header(ws, 1)
    ws.freeze_panes = "A2"
    for column, width in zip("ABCDEFGHIJ", (20, 14, 22, 10, 28, 60, 14, 10, 34, 90)):
        ws.column_dimensions[column].width = width

    notes = [_filters_note(selected)]
    if written == EXPORT_LIMIT:
        total = feed.count()
        if total > written:
            notes.append(f"The Team Activity sheet holds the newest {written} of {total} matching events; "
                         f"the counts here cover all {total}.")
    columns, rows, by_day = _pivot_counts(request, feed, selected)
    _write_category_pivot(wb, columns, rows, notes)
    days, clipped = _pivot_days(selected, by_day)
    day_notes = notes + ["Blank means no events that day."]
    if clipped:
        day_notes.append(f"Showing the most recent {MAX_PIVOT_DAYS} days only.")
    _write_day_pivot(wb, rows, by_day, days, day_notes)

    response = HttpResponse(content_type=XLSX_TYPE)
    response["Content-Disposition"] = 'attachment; filename="team_activity.xlsx"'
    wb.save(response)
    return response
