"""Audit-log writers for the Motor and Health "Check Eligibility" searches."""
from ..models import AuditLog
from .activity import TEST, record_activity


# What each checker page is called on the Team Activity feed, by URL name. The
# Motor Payout Rates page, the Policy Lock Checker and the Health Payout Rates
# page all write through the two functions below, so this is how the feed tells
# them apart. Never a vehicle number, policy-holder name or any search value.
_CHECKER_EVENTS = {
    "motor_payout_rates": ("motor.eligibility.checked", "Ran a Motor eligibility check on Motor Payout Rates"),
    "policy_lock_checker": ("policy_lock.checked", "Ran a Policy Lock Checker search"),
    "health_payout_rates": ("health.eligibility.checked", "Ran a Health eligibility check on Health Payout Rates"),
}


def _record_checker_activity(request, default_action, default_summary):
    url_name = getattr(getattr(request, "resolver_match", None), "url_name", None)
    action, summary = _CHECKER_EVENTS.get(url_name, (default_action, default_summary))
    # An API-key request has no signed-in user; record_activity then records nothing.
    record_activity(request.user, TEST, action, summary)


def _log_motor_points_search(request):
    """Record a MOTOR_POINTS_SEARCH audit log entry from the current request's
    GET params. Shared by every "Check Eligibility" search page (Rate Checker,
    Motor Payout Rates Checker, ...) so they all feed the same
    /points-logs/ audit trail in the same format."""
    flat_params = {}
    for key in request.GET.keys():
        val = request.GET.get(key, "").strip()
        if val and key != "csrfmiddlewaretoken":
            flat_params[key] = val

    if flat_params:
        AuditLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            action="MOTOR_POINTS_SEARCH",
            details=str(flat_params)
        )
        _record_checker_activity(request, "motor.eligibility.checked", "Ran a Motor eligibility check")


def _log_health_points_search(request):
    """Record a HEALTH_POINTS_SEARCH audit log entry from the current
    request's GET params, so the Health Points Checker feeds the same
    /points-logs/ audit trail as the Motor search pages. Unlike
    _log_motor_points_search, Insurer (insurance_company) is a multi-select
    field on this form, so it's read with getlist() and joined rather than
    losing every value but the last."""
    flat_params = {}
    for key in request.GET.keys():
        if key == "csrfmiddlewaretoken":
            continue
        values = [v.strip() for v in request.GET.getlist(key) if v and v.strip()]
        if values:
            flat_params[key] = ", ".join(values)

    if flat_params:
        AuditLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            action="HEALTH_POINTS_SEARCH",
            details=str(flat_params)
        )
        _record_checker_activity(request, "health.eligibility.checked", "Ran a Health eligibility check")
