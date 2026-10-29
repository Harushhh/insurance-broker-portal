"""Rate Checker: the sidebar entry that sends the user to the first Motor / Health tool they may open. Also holds the commented-out Life Payout Grid handoff, kept so the integration can be revived."""
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from ..access import RATE_CHECKER_TABS


# DISABLED 2026-10-11: the Life Payout Grid Railway service was decommissioned,
# so the outbound handoff into it (and the two views below) are switched off.
# The routes in urls.py, the sidebar / Rate Checker links and the
# LIFE_PAYOUT_GRID_* settings are disabled with it. To bring it back, restore
# all of those together (git history has the originals) and redeploy the
# life-payout-grid/ folder as its own Railway service.
#
# def _life_payout_grid_handoff(request, role):
#     """
#     There's no password on the Life Payout Grid app -- instead we sign a
#     short-lived token (this user's username + role + a 2-minute expiry)
#     with a secret shared between the two apps, and hand it off in the URL.
#     The Next.js app verifies the signature and, if valid, trusts the
#     embedded role, since this portal already checked the relevant
#     permission before minting it (see the two callers below).
#     """
#     secret = settings.LIFE_PAYOUT_GRID_AUTH_SECRET
#     if not secret:
#         return HttpResponse(
#             "LIFE_PAYOUT_GRID_AUTH_SECRET is not configured on this portal -- "
#             "the Life Payout Grid app can't be reached until it is.",
#             status=500,
#         )
#     expiry = int(time.time()) + 120
#     payload = f"{request.user.username}:{expiry}:{role}"
#     signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
#     token = f"{payload}.{signature}"
#     url = f"{settings.LIFE_PAYOUT_GRID_URL}/api/admin/portal-login?{urlencode({'token': token})}"
#     return redirect(url)
#
#
# def life_payout_grid_redirect(request):
#     """Entry point for the sidebar's "Life Payout Grid" link -- requires Can_View_Life_Payout_Grid (page_access_required in urls.py, so ADMIN also gets in)."""
#     return _life_payout_grid_handoff(request, "viewer")
#
#
# def life_payout_grid_admin_redirect(request):
#     """Entry point for the sidebar's "Update Payout Rates" link -- requires SUPER_ADMIN specifically (super_admin_required in urls.py), not just ADMIN."""
#     return _life_payout_grid_handoff(request, "admin")


# =========================================================
# RATE CHECKER (unified Motor / Health / Life entry point)
# Sidebar link consolidating what used to be three separate items --
# Motor Policy Locker, Health Quote Simulator, Life Payout Grid -- behind
# one "Rate Checker" entry. The underlying pages, URLs, and permissions
# (Can_View_Policy_Locker / Can_View_Health_Payout_Rates /
# Can_View_Life_Payout_Grid) are unchanged; this is just a landing view
# that sends the user to whichever one they can access, plus a shared tab
# bar (insurance/templates/partials/rate_checker_tabs.html) included at the
# top of policy_lock_checker.html / health_payout_rates.html for switching
# between them. Life stays a plain link to the existing cross-origin
# handoff (_life_payout_grid_handoff above) -- it's a separate app on a
# different domain, so it can't be rendered inline the way Motor/Health can.
# =========================================================

def rate_checker_entry(request):
    """
    Entry point for the sidebar's "Rate Checker" link -- redirects to
    whichever of Motor/Health/Life the user can access, in that priority
    order. Wrapped with plain login_required in urls.py (like home_dashboard);
    not page_access_required, since that only checks a single group and this
    is an any-of-three check, so it raises PermissionDenied itself instead --
    same style as staff_required/super_admin_required.
    """
    is_admin = request.user.groups.filter(name="ADMIN").exists()
    for group_name, url_name in RATE_CHECKER_TABS:
        if is_admin or request.user.groups.filter(name=group_name).exists():
            return redirect(url_name)
    raise PermissionDenied("You don't have access to any Rate Checker tool.")
