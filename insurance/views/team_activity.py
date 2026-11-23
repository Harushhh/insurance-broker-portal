"""Team Activity: what the people who report to you have been doing (runs, uploads, tests, status changes).

Every query starts from `services.team_scope.visible_events(request.user)` and only narrows it, so a manager
cannot reach another team's rows by editing the URL: a forged `?person=` for someone outside their team just
returns nothing. Do not query `ActivityEvent` directly here (`test_team_activity.StructureTests` checks).
"""
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.conf import settings
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


def _filtered(request):
    """(events for the feed, events behind the people strip, the selected filters).

    The strip ignores Person, Category and the search box, so picking a person never empties it.
    """
    user = request.user
    # Team is an admin-only narrowing; for anyone else it is not even read.
    team = User.objects.filter(pk=_int(request.GET.get("team")) or 0).first() if is_admin(user) else None

    strip_events = visible_events(user)
    if team is not None:
        strip_events = strip_events.filter(actor_id__in=team_user_ids(team))
    range_text, first_day, last_day = _date_range(request)
    if first_day:
        strip_events = strip_events.filter(occurred_at__gte=_start_of(first_day))
    if last_day:
        strip_events = strip_events.filter(occurred_at__lt=_start_of(last_day + timedelta(days=1)))

    feed = strip_events
    category = request.GET.get("category", "")
    if category not in CATEGORY_CODES:
        category = ""
    if category:
        feed = feed.filter(category=category)
    q = (request.GET.get("q") or "").strip()[:100]
    if q:
        feed = feed.filter(
            Q(summary__icontains=q) | Q(action__icontains=q)
            | Q(actor_username__icontains=q) | Q(actor_name__icontains=q)
        )
    person = _int(request.GET.get("person"))
    if person:
        feed = feed.filter(actor_id=person)

    selected = {
        "team": team, "date_range": range_text, "category": category, "q": q, "person": person,
        "first_day": first_day, "last_day": last_day,
    }
    return feed, strip_events, selected


def _people_strip(request, strip_events, selected):
    """(columns, rows): one row per person with what they did in the chosen period, by category, and links
    that filter the feed. The Edit column only appears once something has been recorded under it."""
    totals = {}
    counted = strip_events.order_by().values("actor_id", "category").annotate(n=Count("id"), last=Max("occurred_at"))
    for row in counted:
        entry = totals.setdefault(row["actor_id"], {"counts": {}, "total": 0, "last": None})
        entry["counts"][row["category"]] = row["n"]
        entry["total"] += row["n"]
        if entry["last"] is None or row["last"] > entry["last"]:
            entry["last"] = row["last"]

    people = visible_people(request.user, team_of=selected["team"])
    if is_admin(request.user) and selected["team"] is None:
        # Everyone who ever did anything would list the whole organisation: show who was active in this period.
        people = people.filter(pk__in=[pk for pk in totals if pk is not None])

    def link(**overrides):
        params = {"date_range": selected["date_range"]}
        if selected["team"] is not None:
            params["team"] = selected["team"].pk
        params.update(overrides)
        return "?" + urlencode(params)

    used = {code for entry in totals.values() for code in entry["counts"]}
    columns = [(code, label) for code, label in CATEGORIES if code != ActivityEvent.CATEGORY_EDIT or code in used]

    rows = []
    for person in people:
        entry = totals.get(person.pk, {"counts": {}, "total": 0, "last": None})
        profile = getattr(person, "profile", None)
        rows.append({
            "id": person.pk,
            "name": person.get_full_name() or person.username,
            "username": person.username,
            "designation": (profile.designation if profile else "") or "",
            "total": entry["total"],
            "last": entry["last"],
            "url": link(person=person.pk),
            "active": selected["person"] == person.pk,
            "cells": [
                {"code": code, "label": label, "n": entry["counts"].get(code, 0),
                 "url": link(person=person.pk, category=code)}
                for code, label in columns
            ],
        })
    rows.sort(key=lambda row: (-row["total"], row["name"].lower()))
    return columns, rows


def _filter_params(selected):
    """The selected filters as a query string, for the Export link (an explicit empty date_range means no limit)."""
    params = {"date_range": selected["date_range"]}
    for key in ("category", "q", "person"):
        if selected[key]:
            params[key] = selected[key]
    if selected["team"] is not None:
        params["team"] = selected["team"].pk
    return urlencode(params)


def team_activity(request):
    feed, strip_events, selected = _filtered(request)
    admin = is_admin(request.user)

    page_size = _int(request.GET.get("page_size"))
    if page_size not in PAGE_SIZE_OPTIONS:
        page_size = DEFAULT_PAGE_SIZE
    paginator = Paginator(feed.select_related("actor__profile"), page_size)
    page_obj = paginator.get_page(request.GET.get("page"))

    people = sorted(
        visible_people(request.user, team_of=selected["team"]),
        key=lambda person: (person.get_full_name() or person.username).lower(),
    )
    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)
    strip_columns, strip_rows = _people_strip(request, strip_events, selected)

    return render(request, "team_activity.html", {
        "page_obj": page_obj,
        "total": paginator.count,
        "strip_columns": strip_columns,
        "strip": strip_rows,
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
        "retention_days": settings.TEAM_ACTIVITY_RETENTION_DAYS,
    })


def _safe_cell(value):
    """Text for a spreadsheet cell. A value starting with = + - @ would be read as a formula by Excel, and these
    summaries can contain a file name someone chose, so it is defused with a leading apostrophe."""
    text = str(value or "")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


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
    # Search) and the viewer is not an admin looking at everyone (that would list the whole organisation).
    narrowed = bool(selected["person"] or selected["category"] or selected["q"])
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
        parts.append("One person")
    if selected["category"]:
        parts.append(f"Category {dict(CATEGORIES)[selected['category']]}")
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
    feed, _, selected = _filtered(request)
    labels = dict(CATEGORIES)

    wb = Workbook()
    ws = wb.active
    ws.title = EVENTS_SHEET
    ws.append(["Time (IST)", "Username", "Name", "Category", "Action", "Summary", "About", "About id"])
    written = 0
    for event in feed[:EXPORT_LIMIT].iterator(chunk_size=2000):
        ws.append([
            timezone.localtime(event.occurred_at).strftime("%Y-%m-%d %H:%M:%S"),
            _safe_cell(event.actor_username),
            _safe_cell(event.actor_name or event.actor_username),
            labels.get(event.category, event.category),
            _safe_cell(event.action),
            _safe_cell(event.summary),
            _safe_cell(event.target_type),
            _safe_cell(event.target_id),
        ])
        written += 1

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
