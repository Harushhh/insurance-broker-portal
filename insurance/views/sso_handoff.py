"""Landing page of the inbound partner-portal SSO handoff. The ticket request itself is `api/views.py::IssueSSOTicketAPIView`."""
from django.contrib.auth import login as auth_login
from django.shortcuts import redirect
import logging
from .. import sso
from ..models import AuditLog


logger = logging.getLogger("security")


# landing_page value the partner may request -> URL name to redirect to.
# Deliberately a fixed allow-list, never a raw URL/path taken from the
# request -- an open redirect here would defeat the point of scoping access.
SSO_ALLOWED_LANDING_PAGES = {
    "rate_checker": "rate_checker",
    "policy_lock_checker": "policy_lock_checker",
    "health_payout_rates": "health_payout_rates",
}


def sso_consume_view(request):
    """
    Public landing endpoint for the partner-portal redirect. Deliberately
    NOT wrapped in login_required -- that's the entire point of this route.
    A missing/expired/replayed/invalid ticket just falls back to the normal
    login page, same as any other unauthenticated visitor.
    """
    result = sso.verify_and_consume_ticket(request.GET.get("ticket", ""))
    if not result:
        logger.warning(
            "Rejected SSO ticket from %s",
            request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "unknown")),
        )
        return redirect("login")

    user, landing_page = result
    auth_login(request, user)
    # Rotate the session key, same as a normal password login
    # (ThrottledLoginView.form_valid in auth_views.py), to prevent session
    # fixation.
    request.session.cycle_key()
    AuditLog.objects.create(user=user, action="sso_ticket_consumed", details=f"landing={landing_page}")
    logger.info("SSO login consumed for %s", user.username)

    return redirect(SSO_ALLOWED_LANDING_PAGES.get(landing_page, "home"))
