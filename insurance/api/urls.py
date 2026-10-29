"""URL table for the REST API (spliced into insurance/urls.py).

Canonical paths are versioned under ``api/v1/``. The unversioned paths that existed before versioning stay,
pointing at the very same views, so existing callers (the partner portal's SSO ticket request, any
server-to-server consumer holding an API key) keep working. Remove a legacy path only once nothing calls it.

Auth is per view, and deliberately not the page-permission wrapper used by the browser pages:
 * the key-protected endpoints use ``HasAPIKey`` (see ApiKeyGatedRouteTests in insurance/tests.py);
 * ``api-export-rates`` additionally requires a logged-in user with the Dashboard page permission.
"""
from django.urls import path

from ..access import page_access_required
from .views import (
    ExportRatesAPIView,
    HealthPayoutRatesAPIView,
    IssueSSOTicketAPIView,
    MakeModelMasterAPIView,
    PolicyLockCheckerAPIView,
)

urlpatterns = [
    # --- versioned (canonical) ---
    path("api/v1/export-rates/", page_access_required("Can_View_Dashboard")(ExportRatesAPIView.as_view()), name="api-export-rates"),
    path("api/v1/policy-lock-checker/", PolicyLockCheckerAPIView.as_view(), name="api_v1_policy_lock_checker"),
    path("api/v1/health-payout-rates/", HealthPayoutRatesAPIView.as_view(), name="api_v1_health_payout_rates"),
    path("api/v1/make-model-master/", MakeModelMasterAPIView.as_view(), name="api_v1_make_model_master"),
    # Pure API-key auth (no session): called server-to-server, so it skips page_access_required on purpose.
    path("api/v1/sso/issue-ticket/", IssueSSOTicketAPIView.as_view(), name="api_v1_sso_issue_ticket"),

    # --- legacy unversioned paths (same views; names unchanged) ---
    path("api/policy-lock-checker/", PolicyLockCheckerAPIView.as_view(), name="api_policy_lock_checker"),
    path("api/health-payout-rates/", HealthPayoutRatesAPIView.as_view(), name="api_health_payout_rates"),
    path("api/make-model-master/", MakeModelMasterAPIView.as_view(), name="api_make_model_master"),
    path("api/sso/issue-ticket/", IssueSSOTicketAPIView.as_view(), name="sso_issue_ticket"),
]
