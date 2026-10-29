"""Helpers for the executive analysis dashboard: month bucketing, state mapping, min/max tracking."""
import re
from ..models import RTOMaster


# -------------------------
# EXECUTIVE ANALYSIS DASHBOARD
# -------------------------
# Some insurer grids spell the same state's RTO prefix differently, and at
# least one cluster has a stray typo. Fold those onto the standard code so
# they land in the same heatmap bucket instead of splitting a state's data
# across two rows.
STATE_CODE_ALIASES = {
    "TG": "TS",  # Telangana - some grids use TG instead of the official TS
    "OR": "OD",  # Odisha - legacy "OR" alongside the current "OD"
    "GC": "CG",  # Chhattisgarh - one-off typo found in an SBI cluster
}


# Insurers embed the grid's upload month directly in the batch label, e.g.
# "MAGMA_APR26_CG2" or "SBI_MAY26_MH_-_M". Grids that instead use a plain,
# undated label (e.g. SBI's own "AP"/"CG", or "ALLINDIA") don't carry a month
# at all — those are treated as evergreen and stay visible regardless of
# which month is selected, rather than disappearing from the chart.
MONTH_TOKEN_RE = re.compile(r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})", re.IGNORECASE)


MONTH_NAMES = {
    "JAN": "January", "FEB": "February", "MAR": "March", "APR": "April",
    "MAY": "May", "JUN": "June", "JUL": "July", "AUG": "August",
    "SEP": "September", "OCT": "October", "NOV": "November", "DEC": "December",
}


EVERGREEN = "EVERGREEN"


def month_bucket_for(token):
    m = MONTH_TOKEN_RE.search(token)
    if not m:
        return EVERGREEN
    return m.group(1).upper() + m.group(2)


def month_label_for(code):
    if code == EVERGREEN:
        return "No specific month"
    return f"{MONTH_NAMES.get(code[:3], code[:3])} 20{code[3:]}"


def _track_min_max(cell, prefix, value):
    if value is None:
        return
    cur_min = cell.get(f"{prefix}_min")
    cur_max = cell.get(f"{prefix}_max")
    cell[f"{prefix}_min"] = value if cur_min is None else min(cur_min, value)
    cell[f"{prefix}_max"] = value if cur_max is None else max(cur_max, value)


def _build_rto_state_map():
    rto_state_map = {}
    for name, cluster in RTOMaster.objects.values_list("rto_name", "rto_cluster"):
        if not name or not cluster:
            continue
        states = set()
        for item in cluster.split(","):
            item = item.strip()
            if len(item) >= 2 and item[:2].isalpha():
                code = item[:2].upper()
                states.add(STATE_CODE_ALIASES.get(code, code))
        if states:
            rto_state_map[name.strip().upper()] = states
    return rto_state_map


def _row_states_and_months(rto_list, rto_state_map):
    states, months = set(), set()
    for token in (rto_list or "").split(","):
        token = token.strip().upper()
        if not token:
            continue
        if token in rto_state_map:
            states |= rto_state_map[token]
        months.add(month_bucket_for(token))
    return states, months or {EVERGREEN}
