"""Audit-log writers for the Motor and Health "Check Eligibility" searches."""
from ..models import AuditLog, FuelTypeMaster, MakeModelClassMaster, ProductMaster, SubProductMaster
from .activity import TEST, record_activity, record_system_activity


# What each checker page is called on the Team Activity feed, by URL name. The
# Motor Payout Rates page, the Policy Lock Checker, the Health Payout Rates page
# and the two API endpoints behind them all write through the two functions
# below, so this is how the feed tells them apart. (action code, sentence without
# any search value, page name)
_CHECKER_EVENTS = {
    "motor_payout_rates": ("motor.eligibility.checked", "Ran a Motor eligibility check on Motor Payout Rates", "Motor Payout Rates"),
    "policy_lock_checker": ("policy_lock.checked", "Ran a Policy Lock Checker search", "Policy Lock Checker"),
    "health_payout_rates": ("health.eligibility.checked", "Ran a Health eligibility check on Health Payout Rates", "Health Payout Rates"),
    # The same searches made through the API with an API key. There is no signed-in person, so they are filed under
    # System; the feed's Source path says which URL (with or without /v1/) the caller used.
    "api_v1_policy_lock_checker": ("policy_lock.checked", "Ran a Policy Lock Checker search through the API", "Policy Lock Checker (API)"),
    "api_policy_lock_checker": ("policy_lock.checked", "Ran a Policy Lock Checker search through the API", "Policy Lock Checker (API)"),
    "api_v1_health_payout_rates": ("health.eligibility.checked", "Ran a Health eligibility check through the API", "Health Payout Rates (API)"),
    "api_health_payout_rates": ("health.eligibility.checked", "Ran a Health eligibility check through the API", "Health Payout Rates (API)"),
}

# The Motor search forms submit these as numeric ids; the feed shows the names.
_ID_FIELDS = {
    "product": ProductMaster, "sub_product": SubProductMaster,
    "make_model_class": MakeModelClassMaster, "fuel": FuelTypeMaster,
}


def search_parameters(params):
    """The values a search was run with, for the Team Activity details: the ids of Product, Sub Product, Class and Fuel
    replaced by their names (what the page showed the person), every other value exactly as submitted."""
    shown = dict(params)
    for key, model in _ID_FIELDS.items():
        value = shown.get(key)
        if value and str(value).isdigit():
            name = model.objects.filter(pk=int(value)).values_list("name", flat=True).first()
            if name:
                shown[key] = name
    return shown


def _record_checker_activity(request, default_action, default_summary, default_page, flat_params, audit_entry):
    """The Team Activity event for a search. It points at the audit-log row written for the same search
    (legacy_audit_id), which is how the Points Logs page's rows and the feed's events are matched up."""
    url_name = getattr(getattr(request, "resolver_match", None), "url_name", None)
    action, summary, page = _CHECKER_EVENTS.get(url_name, (default_action, default_summary, default_page))
    fields = {
        "details": {"page": page, "parameters": search_parameters(flat_params)},
        "legacy_audit_id": audit_entry.pk,
    }
    if getattr(request.user, "pk", None) is None:
        # An API-key call has no signed-in person: it is filed under System rather than dropped.
        record_system_activity(TEST, action, summary, **fields)
    else:
        record_activity(request.user, TEST, action, summary, **fields)


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
        entry = AuditLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            action="MOTOR_POINTS_SEARCH",
            details=str(flat_params)
        )
        _record_checker_activity(request, "motor.eligibility.checked", "Ran a Motor eligibility check", "Motor eligibility check", flat_params, entry)


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
        entry = AuditLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            action="HEALTH_POINTS_SEARCH",
            details=str(flat_params)
        )
        _record_checker_activity(request, "health.eligibility.checked", "Ran a Health eligibility check", "Health eligibility check", flat_params, entry)
