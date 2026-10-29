"""Small shared helpers: tolerant date and yes/no/NA parsing, string joins, JSON-for-`<script>` escaping."""
import json
from datetime import datetime
from ..models import YesNoNAMaster


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%d/%m/%Y").date()
    except Exception:
        return None


def parse_yes_no_na(value):
    def _get_or_create_code(code_str):
        obj, _ = YesNoNAMaster.objects.get_or_create(code=code_str)
        return obj

    if not value:
        return _get_or_create_code("NA")
    
    v_clean = str(value).strip().lower()
    if v_clean in ["yes", "y", "true", "1"]:
        return _get_or_create_code("YES")
    elif v_clean in ["no", "n", "false", "0"]:
        return _get_or_create_code("NO")
    else:
        return _get_or_create_code("NA")


def normalize(v):
    if v is None:
        return ""
    return str(v).strip().lower()


def unique_join(values):
    seen = set()
    out = []
    for v in values:
        if not v:
            continue
        s = str(v).strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return ", ".join(out)


def split_csv_values(values):
    out = []
    for v in values:
        if not v:
            continue
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


# json.dumps() alone does not escape "</script>" — safe if only rendered as
# JSON, unsafe the moment a template marks it |safe to embed straight into an
# inline <script> block (as motor_payout_rates.html does). This is the same
# escaping django.utils.html.json_script applies internally.
_JSON_SCRIPT_ESCAPES = {ord(">"): "\\u003E", ord("<"): "\\u003C", ord("&"): "\\u0026"}


def safe_json_for_script(value):
    return json.dumps(value).translate(_JSON_SCRIPT_ESCAPES)
