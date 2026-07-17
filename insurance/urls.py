from django.urls import path
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required, user_passes_test
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect
from . import views


def staff_required(view_func):
    """Requires login AND is_staff=True."""
    return login_required(user_passes_test(lambda u: u.is_staff)(view_func))


urlpatterns = [
    # Root now requires login (previously bypassed auth entirely).
    path("", login_required(views.home_dashboard), name="home"),
    path("home/", login_required(views.home_dashboard), name="home_dashboard"),

    # Core portal pages
    path("upload/", login_required(views.import_data_view), name="upload"),
    path("api/upload-chunk/", login_required(views.api_upload_chunk), name="api_upload_chunk"),
    path("dashboard/", login_required(views.dashboard), name="dashboard"),
    path("edit-rate/<str:group_id>/", login_required(views.edit_rate), name="edit_rate"),
    path("bulk-update/", login_required(views.bulk_update_rates), name="bulk_update_rates"),
    path("motor-payout-rates/", login_required(views.motor_payout_rates), name="motor_payout_rates"),
    path("analysis/", login_required(views.business_analysis), name="business_analysis"),

    # Admin-sensitive — staff only
    path("audit-log/", staff_required(views.audit_logs), name="audit_logs"),
    path("user-management/", staff_required(views.user_management), name="user_management"),
    path("grid-management/", staff_required(views.grid_management), name="grid_management"),

    # Ticketing System
    path('tickets/', login_required(views.ticket_dashboard), name='ticket_dashboard'),
    path('api/create-ticket/', login_required(views.create_ticket_api), name='create_ticket_api'),
    path('api/update-ticket-status/', login_required(views.update_ticket_status), name='update_ticket_status'),

    # AI OCR & PDF extraction Pipeline
    path("upload-extract-pdf/", login_required(views.upload_extract_pdf), name="upload_extract_pdf"),
    path("my-mis/", login_required(views.my_mis), name="my_mis"),
    path('mis-review/<int:pk>/', login_required(views.mis_review), name='mis_review'),
    path('configurator/', staff_required(views.field_configurator), name='field_configurator'),
    path('configurator/edit/<int:pk>/', staff_required(views.edit_field), name='edit_field'),
    path('configurator/delete/<int:pk>/', staff_required(views.delete_field), name='delete_field'),

    # Policy Lock System
    path("policy-lock-checker/", login_required(views.policy_lock_checker), name="policy_lock_checker"),
    path("lock-unlock-policy/<int:rate_id>/", login_required(views.lock_unlock_policy), name="lock_unlock_policy"),
    path("locked-policy-dashboard/", login_required(views.locked_policy_dashboard), name="locked_policy_dashboard"),

    # Export routes
    path("export/", login_required(views.export_rates_xlsx), name="export_rates_xlsx"),
    path("export-rto/", login_required(views.export_rto_xlsx), name="export_rto_xlsx"),
    path("export-make-model/", login_required(views.export_make_model_xlsx), name="export_make_model_xlsx"),

    # Master dashboards
    path("rto-dashboard/", login_required(views.rto_dashboard), name="rto_dashboard"),
    path("make-model-dashboard/", login_required(views.make_model_dashboard), name="make_model_dashboard"),

    # Master edit routes
    path("rto/edit/<int:pk>/", staff_required(views.edit_rto), name="edit_rto"),
    path("make-model/edit/<int:pk>/", staff_required(views.edit_make_model), name="edit_make_model"),

    # Password reset — intentionally public (unauthenticated users need this)
    path("password-reset/", views.direct_password_reset, name="password_reset"),
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

    path('motor-points-logs/', staff_required(views.motor_points_audit_logs), name='motor_points_audit_logs'),

    # ==========================================
    # AUTOMATED MIS PAYOUT CALCULATION ROUTES
    # ==========================================
    path("mis-payout-automation/", login_required(views.mis_payout_automation), name="mis_payout_automation"),
    path("mis-payout/download/<int:file_id>/", login_required(views.download_processed_mis), name="download_processed_mis"),
    path("mis-mapping/", staff_required(views.mis_mapping_dashboard), name="mis_mapping_dashboard"),
    path("mis-mapping/add/", staff_required(views.add_mis_mapping), name="add_mis_mapping"),
    path("mis-mapping/edit/<int:pk>/", staff_required(views.edit_mis_mapping), name="edit_mis_mapping"),
    path("mis-mapping/delete/<int:pk>/", staff_required(views.delete_mis_mapping), name="delete_mis_mapping"),

    # ==========================================
    # REST API ENDPOINTS
    # ==========================================
    path('api/v1/export-rates/', login_required(views.ExportRatesAPIView.as_view()), name='api-export-rates'),

    # Catch-all — must stay last. Any unmatched route redirects to login.
    path("<path:unused>/", lambda request, unused: redirect("login"), name="catch_all"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)