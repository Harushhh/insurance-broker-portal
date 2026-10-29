"""Audit-log writers for the Motor and Health "Check Eligibility" searches."""
from ..models import AuditLog


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
