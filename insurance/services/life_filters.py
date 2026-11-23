"""The Life Rate Checker's fixed filter values, and how each one is read against the workbook's own wording.

The page offers only the values below. The workbook writes categories, PPT and PT as free text ("Non PAR(Trad)",
"7 to 9", "12 & Above", "15/20", ">=20"), so every value is translated here into a test on the text stored on a
row (``category``, ``product``, ``ppt``, ``pt``). Nothing here touches the database; the search runs these tests
over the rows of the live upload (``services/life_rates.py``).

The rules are the user's (2026-11-13); where the workbook does not say, a row falls out of a filter instead of
being guessed into it.

**Category** (seven values; a row can belong to more than one)
  * From the category wording, first match wins: TULIP, ULIP, "Annuity" -> ANNUITY / SAVINGS, "Pension" -> PENSION,
    "Term" or "Protection" -> TERM, "Non Par" -> NON PAR, "Par" -> PAR. "Saving" wording stays inside PAR / NON PAR.
  * A row whose plan name says "Pension" is also PENSION, whatever its category wording says (so a row worded
    "Annuity" with a plan called "... Pension ..." shows under both PENSION and ANNUITY / SAVINGS).
  * A row with no category wording is sorted by the words of its plan name: Term -> TERM, Annuity -> ANNUITY /
    SAVINGS, TULIP -> TULIP, ULIP -> ULIP (and Pension as above).
  * Anything else (the ILP rows, rows that say nothing) belongs to no category: it shows only when Category is blank.

**PPT** (1-13 or ">13") and **Payment Mode** (SP / LP / RP)
  * A PPT pick matches a row whose PPT covers that number: "8", "7 to 9", "5 & 6", "10+", ">=10", "12 & Above",
    "15 years & above". ">13" matches a row that reaches 14 or more.
  * Payment Mode SP returns the rows worded single pay ("SP", "Single Pay", "Single Premium") and the rows with a PPT
    of exactly 1; the PPT pick is ignored. RP returns the rows worded regular pay ("RP", "Regular", "Regular Pay").
    LP returns every row with a PPT number or range of 2 or more years, plus the rows worded "Limited"; the PPT pick
    narrows LP by number. Rows worded RP or Limited carry no number, so they stay for any PPT pick.
  * With a PPT picked and no Payment Mode, rows worded SP, RP or Limited are left out; rows that state no PPT at all
    (blank, "All", a written payout rule) stay.

**PT** (1-100)
  * A PT pick matches a row whose PT covers that number ("20", "15/20", ">=20", "15-19", "5 to 20 Years"). A row
    with no PT, "Any", "All", "RP" or a whole-life PT ("Whole Life", "100 minus entry age") matches any PT.

**A policy in the MIS Payout Engine** (``policy_ppt_matches``; the engine's Life route, services/life_mis.py)
  A MIS row gives the policy's own PPT and PT in years, not a pick from the page, so the rules above are applied to
  that one number. Where the page lets a person choose a Payment Mode, the policy's mode follows from its numbers
  (proposed 2026-11-14, to be confirmed by the user): single pay is a PPT of 1 (or a MIS premium mode that says
  single), limited pay is a PPT below the policy term, regular pay is a PPT equal to the term.
  * Single pay: only the rows worded single pay (the "PPT of exactly 1" rows are worded that way too).
  * Otherwise a row applies when its PPT covers that number, or states no PPT at all (blank, "All", a written
    payout rule); a row worded "Limited" also applies to a limited-pay policy and a row worded "Regular" to a
    regular-pay one. Rows worded single pay never apply.
"""
import re
from dataclasses import dataclass
from functools import lru_cache

CATEGORIES = ("PAR", "NON PAR", "TERM", "ULIP", "TULIP", "ANNUITY / SAVINGS", "PENSION")
PAYMENT_MODES = ("SP", "LP", "RP")
PPT_CHOICES = tuple(str(n) for n in range(1, 14)) + (">13",)
PT_CHOICES = tuple(str(n) for n in range(1, 101))

_NO_STATEMENT = {"all", "any", "na", "n/a", "-", "--"}


# ----------------------------------------------------------------------------------------------- category
def _words(text):
    return re.sub(r"[^a-z]+", " ", (text or "").lower()).split()


@lru_cache(maxsize=None)
def categories_of(category, product):
    """The categories (a frozenset of CATEGORIES values) a row belongs to, from its category wording and plan name."""
    wording, plan = _words(category), _words(product)
    keys = set()
    if wording:
        if "tulip" in wording:
            keys.add("TULIP")
        elif "ulip" in wording:
            keys.add("ULIP")
        elif "annuity" in wording or "annuaity" in wording:
            keys.add("ANNUITY / SAVINGS")
        elif "pension" in wording:
            keys.add("PENSION")
        elif "term" in wording or "protection" in wording:
            keys.add("TERM")
        elif "non" in wording and "par" in wording:
            keys.add("NON PAR")
        elif "par" in wording:
            keys.add("PAR")
    else:
        if "tulip" in plan:
            keys.add("TULIP")
        if "ulip" in plan:
            keys.add("ULIP")
        if "term" in plan:
            keys.add("TERM")
        if "annuity" in plan or "annuaity" in plan:
            keys.add("ANNUITY / SAVINGS")
    if "pension" in plan:
        keys.add("PENSION")
    return frozenset(keys)


# ----------------------------------------------------------------------------------------------- numbers
def _spans(text, floor):
    """The numbers a wording covers, as ((low, high), ...) with high None for "and above". floor is the lowest value a
    "<N" wording starts at (PPT 2: a PPT of 1 is single pay; PT 1)."""
    spans = []

    def take(pattern, make):
        nonlocal text

        def grab(match):
            span = make(match)
            if span[1] is None or span[1] >= span[0]:
                spans.append(span)
            return " "
        text = re.sub(pattern, grab, text)

    take(r"(?:>=|≥)\s*(\d+)", lambda m: (int(m[1]), None))
    take(r">\s*(\d+)", lambda m: (int(m[1]) + 1, None))
    take(r"(?:<=|≤)\s*(\d+)", lambda m: (floor, int(m[1])))
    take(r"<\s*(\d+)", lambda m: (floor, int(m[1]) - 1))
    take(r"(\d+)\s*(?:\+|(?:&|and|or)?\s*(?:above|onwards|plus))", lambda m: (int(m[1]), None))
    take(r"(\d+)\s*(?:to|-|–|—)\s*(\d+)", lambda m: (int(m[1]), int(m[2])))
    spans += [(int(n), int(n)) for n in re.findall(r"\d+", text)]
    return tuple(spans)


def _covers(spans, n):
    return any(low <= n and (high is None or n <= high) for low, high in spans)


def _reaches_beyond_13(spans):
    return any(high is None or high >= 14 for _, high in spans)


# ----------------------------------------------------------------------------------------------- PPT
@dataclass(frozen=True)
class PptSpec:
    modes: frozenset          # the Payment Modes the wording counts as: SP, LP, RP
    spans: tuple              # the PPT numbers it covers
    states_none: bool         # no PPT at all (blank, "All", a written payout rule)


@lru_cache(maxsize=None)
def ppt_spec(text):
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t or t in _NO_STATEMENT or t.startswith("excluding") or "capped" in t:
        return PptSpec(frozenset(), (), True)
    modes = set()
    if re.search(r"\bsingle\b|\bsp\b", t):
        modes.add("SP")
    if re.search(r"\bregular\b|\breg\b|\brp\b", t):
        modes.add("RP")
    if re.search(r"\blimited\b|\bltd\b", t):
        modes.add("LP")
    spans = _spans(re.sub(r"\b(?:years?|yrs?|pay|ppt)\b", " ", t), 2)
    if spans == ((1, 1),):
        modes.add("SP")                                   # a PPT of exactly 1 is single pay
    return PptSpec(frozenset(modes), spans, not modes and not spans)


def _is_limited_pay(spec):
    return "LP" in spec.modes or any(high is None or high >= 2 for _, high in spec.spans)


def _ppt_covers(spec, pick):
    return _reaches_beyond_13(spec.spans) if pick == ">13" else _covers(spec.spans, int(pick))


def ppt_matches(spec, mode, pick):
    """Whether a row's PPT passes the Payment Mode and PPT picks ("" = not picked)."""
    if mode == "SP":
        return "SP" in spec.modes                         # the PPT pick is ignored for single pay
    if mode == "RP":
        return "RP" in spec.modes                         # regular-pay rows state no number: any PPT stays
    if mode == "LP":
        if not _is_limited_pay(spec):
            return False
        return not pick or not spec.spans or _ppt_covers(spec, pick)
    if not pick:
        return True
    return spec.states_none or (bool(spec.spans) and _ppt_covers(spec, pick))


# ----------------------------------------------------------------------------------------------- PT
@dataclass(frozen=True)
class PtSpec:
    any_term: bool            # no PT stated, "Any", "All", regular pay or whole life: matches every PT
    spans: tuple


_PT_OPEN = re.compile(r"whole life|entry age|\bany\b|\ball\b|\brp\b|\bregular\b")     # wordings that match every PT


@lru_cache(maxsize=None)
def pt_spec(text):
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t or t in _NO_STATEMENT or _PT_OPEN.search(t):
        return PtSpec(True, ())
    t = re.sub(r"\([^)]*\)", " ", t)                      # "(PO Period 25 & 30)" is the payout period, not the PT
    spans = _spans(re.sub(r"\b(?:years?|yrs?|pt|policy|term)\b", " ", t), 1)
    return PtSpec(not spans, spans)


def pt_matches(spec, pick):
    return not pick or spec.any_term or _covers(spec.spans, int(pick))


# ----------------------------------------------------------------------------------------------- unreadable wording
# A wording with words but nothing the rules above can read ("Five Pay") is read as "states nothing", which matches
# every PPT / PT. These two say which stored values fall into that, so a new upload can be checked before it goes
# live (services/life_review.py). They use the same constants as ppt_spec / pt_spec, so the two cannot drift apart.
def ppt_unreadable(text):
    """True for a non-blank PPT wording that has neither a number nor a mode word (and is not one of the deliberate
    'states no PPT' wordings), so ppt_spec reads it as 'applies to every PPT'."""
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t or t in _NO_STATEMENT or t.startswith("excluding") or "capped" in t:
        return False
    spec = ppt_spec(text)
    return not spec.modes and not spec.spans


def pt_unreadable(text):
    """True for a non-blank PT wording with no number and none of the 'any term' wordings, so pt_spec reads it as
    'applies to every PT'."""
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t or t in _NO_STATEMENT or _PT_OPEN.search(t):
        return False
    return not pt_spec(text).spans


# ----------------------------------------------------------------------------------------------- a policy
def policy_ppt_matches(spec, ppt, term, single_pay=False):
    """Whether a row's PPT wording (a PptSpec) applies to one policy: `ppt` years of premium payment over a
    `term`-year policy term (either may be None when the MIS does not say). `single_pay` is True for a policy
    that is single pay by its own wording, whatever its PPT cell holds. See the module docstring."""
    if single_pay or (ppt is not None and ppt <= 1):
        return "SP" in spec.modes
    if ppt is None:
        return False
    if spec.spans and _covers(spec.spans, ppt):
        return True
    if spec.states_none:
        return True
    if not spec.spans:
        if "LP" in spec.modes and term is not None and ppt < term:
            return True
        if "RP" in spec.modes and term is not None and ppt == term:
            return True
    return False


# ----------------------------------------------------------------------------------------------- a row
def row_matches(row, selected):
    """Whether a LifeRate (anything with category, product, ppt and pt) passes the mapped filters in `selected`
    (keys category, payment_mode, ppt, pt; "" = not picked). Insurer, product and the search words are SQL filters."""
    if selected["category"] and selected["category"] not in categories_of(row.category, row.product):
        return False
    if (selected["payment_mode"] or selected["ppt"]) and not ppt_matches(
            ppt_spec(row.ppt), selected["payment_mode"], selected["ppt"]):
        return False
    if selected["pt"] and not pt_matches(pt_spec(row.pt), selected["pt"]):
        return False
    return True
