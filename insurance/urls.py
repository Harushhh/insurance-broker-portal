from functools import wraps

from django.urls import path, reverse_lazy
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.conf.urls.static import static
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from . import views


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
    which grants the ability to overwrite the live commission grid.
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


urlpatterns = [
    # Root now requires login (previously bypassed auth entirely).
    path("", login_required(views.home_dashboard), name="home"),
    path("home/", login_required(views.home_dashboard), name="home_dashboard"),

    # Core portal pages
    path("upload/", page_access_required("Can_Upload_CSV")(views.import_data_view), name="upload"),
    path("api/upload-chunk/", page_access_required("Can_Upload_CSV")(views.api_upload_chunk), name="api_upload_chunk"),
    path("dashboard/", page_access_required("Can_View_Dashboard")(views.dashboard), name="dashboard"),
    path("edit-rate/<str:group_id>/", page_access_required("Can_View_Dashboard")(views.edit_rate), name="edit_rate"),
    path("bulk-update/", page_access_required("Can_View_Dashboard")(views.bulk_update_rates), name="bulk_update_rates"),
    path("rate-master-health/", page_access_required("Can_View_Rate_Master_Health")(views.rate_master_health), name="rate_master_health"),

    # Health Rate Master
    path("health-rate-master/", page_access_required("Can_View_Health_Rate_Master")(views.health_rate_master), name="health_rate_master"),
    path("health-rate-master/edit/<int:pk>/", page_access_required("Can_View_Health_Rate_Master")(views.health_rate_master_edit), name="health_rate_master_edit"),
    path("health-rate-master/bulk-update/", page_access_required("Can_View_Health_Rate_Master")(views.health_rate_master_bulk_update), name="health_rate_master_bulk_update"),
    path("health-rate-master/export/", page_access_required("Can_View_Health_Rate_Master")(views.export_health_rates_xlsx), name="export_health_rates_xlsx"),
    path("health-payout-rates/", page_access_required("Can_View_Health_Payout_Rates")(views.health_payout_rates), name="health_payout_rates"),

    path("motor-payout-rates/", page_access_required("Can_View_Motor_Payout_Rates")(views.motor_payout_rates), name="motor_payout_rates"),
    path("motor-payout-rates/more/", page_access_required("Can_View_Motor_Payout_Rates")(views.motor_payout_rates_more), name="motor_payout_rates_more"),
    path("analysis/", page_access_required("Can_View_Analysis")(views.business_analysis), name="business_analysis"),
    path("analysis/pivot-data/", page_access_required("Can_View_Analysis")(views.analysis_pivot_data), name="analysis_pivot_data"),
    path("analysis/payout-data/", page_access_required("Can_View_Analysis")(views.analysis_payout_data), name="analysis_payout_data"),

    # Admin-sensitive — staff only (or granted per-page below)
    path("audit-log/", page_access_required("Can_View_Audit_Log")(views.audit_logs), name="audit_logs"),
    path("audit-log/export/", page_access_required("Can_View_Audit_Log")(views.export_audit_log_xlsx), name="export_audit_log_xlsx"),
    path("user-management/", staff_required(views.user_management), name="user_management"),
    path("grid-management/", page_access_required("Can_View_Grid_Management")(views.grid_management), name="grid_management"),
    path("life-payout-grid/", page_access_required("Can_View_Life_Payout_Grid")(views.life_payout_grid_redirect), name="life_payout_grid_redirect"),
    path("life-payout-grid/admin/", super_admin_required(views.life_payout_grid_admin_redirect), name="life_payout_grid_admin_redirect"),

    # Special Rate Requests
    path("special-rates/", page_access_required("Can_View_Special_Rates")(views.special_rate_requests), name="special_rate_requests"),
    path("special-rates/review/", page_access_required("Can_Review_Special_Rates")(views.special_rate_requests_review), name="special_rate_requests_review"),
    path("api/update-special-rate-status/", page_access_required("Can_Review_Special_Rates")(views.update_special_rate_request_status), name="update_special_rate_request_status"),

    # Ticketing System
    path('tickets/', page_access_required("Can_View_Tickets")(views.ticket_dashboard), name='ticket_dashboard'),
    # Left on plain login_required (not gated by Can_View_Tickets): also called
    # from policy_lock_checker.html as a standalone "raise an issue" action.
    path('api/create-ticket/', login_required(views.create_ticket_api), name='create_ticket_api'),
    path('api/update-ticket-status/', page_access_required("Can_View_Tickets")(views.update_ticket_status), name='update_ticket_status'),

    # AI OCR & PDF extraction Pipeline
    path("upload-extract-pdf/", page_access_required("Can_View_OCR_Pipeline")(views.upload_extract_pdf), name="upload_extract_pdf"),
    path("my-mis/", page_access_required("Can_View_MIS_Register")(views.my_mis), name="my_mis"),
    path('mis-review/<int:pk>/', page_access_required("Can_View_MIS_Register")(views.mis_review), name='mis_review'),
    path('configurator/', page_access_required("Can_View_AI_Rulebook")(views.field_configurator), name='field_configurator'),
    path('configurator/edit/<int:pk>/', page_access_required("Can_View_AI_Rulebook")(views.edit_field), name='edit_field'),
    path('configurator/delete/<int:pk>/', page_access_required("Can_View_AI_Rulebook")(views.delete_field), name='delete_field'),

    # Rate Checker -- unified Motor/Health/Life entry point (see
    # views.RATE_CHECKER_TABS). Any-of-three check, so it's plain
    # login_required rather than page_access_required (single group only).
    path("rate-checker/", login_required(views.rate_checker_entry), name="rate_checker"),

    # Policy Lock System
    path("policy-lock-checker/", page_access_required("Can_View_Policy_Locker")(views.policy_lock_checker), name="policy_lock_checker"),
    path("lock-unlock-policy/<int:rate_id>/", page_access_required("Can_View_Policy_Locker")(views.lock_unlock_policy), name="lock_unlock_policy"),
    path("locked-policy-dashboard/", page_access_required("Can_View_Locked_Policies")(views.locked_policy_dashboard), name="locked_policy_dashboard"),

    # Export routes
    path("export/", page_access_required("Can_View_Dashboard")(views.export_rates_xlsx), name="export_rates_xlsx"),
    path("export-rto/", page_access_required("Can_View_RTO_Dashboard")(views.export_rto_xlsx), name="export_rto_xlsx"),
    path("export-make-model/", page_access_required("Can_View_Make_Model_Dashboard")(views.export_make_model_xlsx), name="export_make_model_xlsx"),
    path("export-pincode/", page_access_required("Can_View_Pincode_Dashboard")(views.export_pincode_xlsx), name="export_pincode_xlsx"),

    # Master dashboards
    path("rto-dashboard/", page_access_required("Can_View_RTO_Dashboard")(views.rto_dashboard), name="rto_dashboard"),
    path("make-model-dashboard/", page_access_required("Can_View_Make_Model_Dashboard")(views.make_model_dashboard), name="make_model_dashboard"),
    path("pincode-dashboard/", page_access_required("Can_View_Pincode_Dashboard")(views.pincode_dashboard), name="pincode_dashboard"),

    # Master edit routes
    path("rto/edit/<int:pk>/", page_access_required("Can_View_RTO_Dashboard")(views.edit_rto), name="edit_rto"),
    path("make-model/edit/<int:pk>/", page_access_required("Can_View_Make_Model_Dashboard")(views.edit_make_model), name="edit_make_model"),
    path("pincode/edit/<int:pk>/", page_access_required("Can_View_Pincode_Dashboard")(views.edit_pincode), name="edit_pincode"),

    # Password reset — intentionally public (unauthenticated users need this).
    # Uses Django's stock PasswordResetView: the token is emailed to the
    # account's registered address (registration/password_reset_email.html)
    # rather than handed back in the response, so knowing/guessing an email
    # address alone is no longer enough to take over the account.
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="password_reset.html",
            email_template_name="registration/password_reset_email.html",
            success_url=reverse_lazy("password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(template_name="password_reset_done.html"),
        name="password_reset_done"
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(template_name="password_reset_confirm.html"),
        name="password_reset_confirm"
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(template_name="password_reset_complete.html"),
        name="password_reset_complete"
    ),

    path('motor-points-logs/', page_access_required("Can_View_Motor_Points_Logs")(views.motor_points_audit_logs), name='motor_points_audit_logs'),

    # ==========================================
    # AUTOMATED MIS PAYOUT CALCULATION ROUTES
    # ==========================================
    path("mis-payout-automation/", page_access_required("Can_View_MIS_Payout_Engine")(views.mis_payout_automation), name="mis_payout_automation"),
    path("mis-payout/download/<int:file_id>/", page_access_required("Can_View_MIS_Payout_Engine")(views.download_processed_mis), name="download_processed_mis"),
    path("mis-payout/cancel/<int:file_id>/", page_access_required("Can_View_MIS_Payout_Engine")(views.cancel_mis_processing), name="cancel_mis_processing"),
    path("mis-mapping/", page_access_required("Can_View_MIS_Payout_Engine")(views.mis_mapping_dashboard), name="mis_mapping_dashboard"),
    path("mis-mapping/add/", page_access_required("Can_View_MIS_Payout_Engine")(views.add_mis_mapping), name="add_mis_mapping"),
    path("mis-mapping/edit/<int:pk>/", page_access_required("Can_View_MIS_Payout_Engine")(views.edit_mis_mapping), name="edit_mis_mapping"),
    path("mis-mapping/delete/<int:pk>/", page_access_required("Can_View_MIS_Payout_Engine")(views.delete_mis_mapping), name="delete_mis_mapping"),

    # ==========================================
    # REST API ENDPOINTS
    # ==========================================
    path('api/v1/export-rates/', page_access_required("Can_View_Dashboard")(views.ExportRatesAPIView.as_view()), name='api-export-rates'),

    # Inbound partner-portal SSO handoff (see insurance/sso.py). Both routes
    # are intentionally public/unauthenticated -- issue-ticket is gated by
    # its own HasAPIKey permission instead of a session, and consume is the
    # landing page a not-yet-logged-in browser is redirected to.
    path("api/sso/issue-ticket/", views.IssueSSOTicketAPIView.as_view(), name="sso_issue_ticket"),
    path("sso/consume/", views.sso_consume_view, name="sso_consume"),

    # Catch-all — must stay last. Any unmatched route redirects to login.
    path("<path:unused>/", lambda request, unused: redirect("login"), name="catch_all"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)