from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # Home / Login
    path("", auth_views.LoginView.as_view(template_name="registration/login.html"), name="home"),

    # Core portal pages
    path("upload/", views.upload_csv, name="upload"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("edit-rate/<str:group_id>/", views.edit_rate, name="edit_rate"),
    path("bulk-update/", views.bulk_update_rates, name="bulk_update_rates"),
    path("motor-payout-rates/", views.motor_payout_rates, name="motor_payout_rates"),
    path("analysis/", views.business_analysis, name="business_analysis"),
    path("audit-log/", views.audit_logs, name="audit_logs"),
    path("user-management/", views.user_management, name="user_management"),
    path("grid-management/", views.grid_management, name="grid_management"),

    # PDF extraction + MIS pages
    path("upload-extract-pdf/", views.upload_extract_pdf, name="upload_extract_pdf"),
    path("my-mis/", views.my_mis, name="my_mis"),

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

    # Alias Management
    path("alias-management/", views.alias_management, name="alias_management"),

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
]