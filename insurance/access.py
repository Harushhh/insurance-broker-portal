"""Who may open which page.

The three decorators below are what ``insurance/urls.py`` wraps every route in,
and ``PAGE_GROUPS`` is the list of per-page Django groups they check (and that
``/user-management/`` hands out). ``insurance/context_processors.py`` reads
``PAGE_GROUPS`` to decide which sidebar links to draw, so a change to who can
open a page belongs here and in that processor.

Group names are stable identifiers (migrations seed them and users hold them):
never rename one.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

# =========================================================
# PAGE-LEVEL ACCESS GROUPS
# Enforced by page_access_required() below — each entry here
# is one Django Group, toggled per-user from the Access Control checkboxes
# in user_management() (insurance/views).
# =========================================================
PAGE_GROUPS = [
    "Can_View_Dashboard",
    "Can_View_Analysis",
    "Can_View_Motor_Payout_Rates",
    "Can_Upload_CSV",
    "Can_View_RTO_Dashboard",
    "Can_View_Make_Model_Dashboard",
    "Can_View_Missing_Make_Model",
    "Can_View_Audit_Log",
    "Can_View_Grid_Management",
    "Can_View_Alias_Management",
    "Can_View_Tickets",
    "Can_View_Policy_Locker",
    "Can_View_Locked_Policies",
    "Can_View_Motor_Points_Logs",
    "Can_View_AI_Rulebook",
    "Can_View_OCR_Pipeline",
    "Can_View_MIS_Register",
    "Can_View_MIS_Payout_Engine",
    "Can_View_Rate_Master_Health",
    "Can_View_Health_Rate_Master",
    "Can_View_Pincode_Dashboard",
    "Can_View_Health_Payout_Rates",
    "Can_View_Special_Rates",
    "Can_Review_Special_Rates",
    "Can_View_Life_Payout_Grid",
]

# Rate Checker: the sidebar entry that sends a user to the first of these pages they
# can open (see views.rate_checker_entry and partials/rate_checker_tabs.html).
RATE_CHECKER_TABS = [
    ("Can_View_Policy_Locker", "policy_lock_checker"),
    ("Can_View_Health_Payout_Rates", "health_payout_rates"),
    # Life tab disabled 2026-10-11 along with the Life Payout Grid service.
    # ("Can_View_Life_Payout_Grid", "life_payout_grid_redirect"),
]

# The single "Rate Checker" checkbox in the /user-management/ Page
# Permissions grid stands in for these three real page-access groups (see
# user_management() below). It shows checked only when a user already holds
# all three -- so saving without deliberately re-checking it revokes a
# partial subset rather than silently granting the rest.
RATE_CHECKER_GROUPS = {group_name for group_name, _ in RATE_CHECKER_TABS}


def staff_required(view_func):
    """
    Requires login AND (is_staff or is_superuser or in the ADMIN group).
    A logged-in user who fails the check gets a 403, not a redirect back to
    the login page — user_passes_test's default redirect-on-failure would
    otherwise loop forever, since the login view sends already-authenticated
    users straight back to `next`.
    """
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        u = request.user
        if not (u.is_staff or u.is_superuser or u.groups.filter(name="ADMIN").exists()):
            raise PermissionDenied("Staff access required.")
        return view_func(request, *args, **kwargs)
    return _wrapped


def super_admin_required(view_func):
    """
    Requires login AND membership in SUPER_ADMIN specifically -- unlike
    page_access_required, being in ADMIN (the "Make Admin" bulk-access
    group) does NOT get you in here. Reserved for the single most
    sensitive action in the app: minting a Life Payout Grid admin token,
    which grants the ability to overwrite the live commission grid. (No
    route uses it right now -- that handoff is disabled, see below -- but
    it stays for the next super-admin-only action.)
    SUPER_ADMIN is deliberately not offered as a Page Permissions checkbox
    on /user-management/ -- grant it via Django's own /admin/ site instead.
    """
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.groups.filter(name="SUPER_ADMIN").exists():
            raise PermissionDenied("You don't have access to this page.")
        return view_func(request, *args, **kwargs)
    return _wrapped


def page_access_required(group_name):
    """
    Requires login AND (in the ADMIN group or in the named per-page group).
    This is the enforcement half of the Access Control checkboxes on
    /user-management/ — those already store real Group membership, this just
    checks it instead of letting every logged-in user through regardless of
    what's ticked for them.

    Deliberately does NOT give is_superuser a free pass here — ADMIN
    (assigned via the "Make Admin" button) is this app's one deliberate
    "full admin" flag, and unchecking a box for a superuser account should
    actually take effect. A superuser who isn't in ADMIN and gets locked out
    of a page can't get locked out of Access Control itself, though:
    staff_required (guarding /user-management/) still honors is_staff /
    is_superuser, so there's always a way back in to re-grant access.
    """
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            u = request.user
            if not (u.groups.filter(name="ADMIN").exists()
                    or u.groups.filter(name=group_name).exists()):
                raise PermissionDenied("You don't have access to this page.")
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator
