from django.urls import path
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.conf.urls.static import static
from . import views

urlpatterns = [
    # Changed root URL to load the Home Dashboard directly without a login screen
    path("", views.home_dashboard, name="home"),

    # Unified Home Page Dashboard
    path("home/", views.home_dashboard, name="home_dashboard"),

    # Core portal pages
    path("upload/", views.import_data_view, name="upload"),
    path("api/upload-chunk/", views.api_upload_chunk, name="api_upload_chunk"), # Streaming CSV Upload API
    path("dashboard/", views.dashboard, name="dashboard"),
    path("edit-rate/<str:group_id>/", views.edit_rate, name="edit_rate"),
    path("bulk-update/", views.bulk_update_rates, name="bulk_update_rates"),
    path("motor-payout-rates/", views.motor_payout_rates, name="motor_payout_rates"),
    path("analysis/", views.business_analysis, name="business_analysis"),
    path("audit-log/", views.audit_logs, name="audit_logs"),
    path("user-management/", views.user_management, name="user_management"),
    path("grid-management/", views.grid_management, name="grid_management"),

    # Ticketing System
    path('tickets/', views.ticket_dashboard, name='ticket_dashboard'),
    path('api/create-ticket/', views.create_ticket_api, name='create_ticket_api'),
    path('api/update-ticket-status/', views.update_ticket_status, name='update_ticket_status'),

    # AI OCR & PDF extraction Pipeline
    path("upload-extract-pdf/", views.upload_extract_pdf, name="upload_extract_pdf"),
    path("my-mis/", views.my_mis, name="my_mis"),
    path('mis-review/<int:pk>/', views.mis_review, name='mis_review'),
    path('configurator/', views.field_configurator, name='field_configurator'),
    path('configurator/edit/<int:pk>/', views.edit_field, name='edit_field'),
    path('configurator/delete/<int:pk>/', views.delete_field, name='delete_field'),

    # Policy Lock System
    path("policy-lock-checker/", views.policy_lock_checker, name="policy_lock_checker"),
    path("lock-unlock-policy/<int:rate_id>/", views.lock_unlock_policy, name="lock_unlock_policy"),
    path("locked-policy-dashboard/", views.locked_policy_dashboard, name="locked_policy_dashboard"),

    # Export routes
    path("export/", views.export_rates_xlsx, name="export_rates_xlsx"),
    path("export-rto/", views.export_rto_xlsx, name="export_rto_xlsx"),
    path("export-make-model/", views.export_make_model_xlsx, name="export_make_model_xlsx"),

    # Master dashboards
    path("rto-dashboard/", views.rto_dashboard, name="rto_dashboard"),
    path("make-model-dashboard/", views.make_model_dashboard, name="make_model_dashboard"),

    # Master edit routes
    path("rto/edit/<int:pk>/", views.edit_rto, name="edit_rto"),
    path("make-model/edit/<int:pk>/", views.edit_make_model, name="edit_make_model"),

    # Password reset
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

    path('motor-points-logs/', views.motor_points_audit_logs, name='motor_points_audit_logs'),

    # ==========================================
    # AUTOMATED MIS PAYOUT CALCULATION ROUTES
    # ==========================================
    path("mis-payout-automation/", views.mis_payout_automation, name="mis_payout_automation"),
    path("mis-payout/download/<int:file_id>/", views.download_processed_mis, name="download_processed_mis"),
    path("mis-mapping/", views.mis_mapping_dashboard, name="mis_mapping_dashboard"),
    path("mis-mapping/add/", views.add_mis_mapping, name="add_mis_mapping"),
    path("mis-mapping/edit/<int:pk>/", views.edit_mis_mapping, name="edit_mis_mapping"),
    path("mis-mapping/delete/<int:pk>/", views.delete_mis_mapping, name="delete_mis_mapping"),

    # ==========================================
    # REST API ENDPOINTS
    # ==========================================
    path('api/v1/export-rates/', views.ExportRatesAPIView.as_view(), name='api-export-rates'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)