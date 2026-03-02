from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # ✅ when user opens http://127.0.0.1:8000/ -> show login
    path("", auth_views.LoginView.as_view(template_name="registration/login.html"), name="home"),

    path("upload/", views.upload_csv, name="upload"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("edit-rate/<str:group_id>/", views.edit_rate, name="edit_rate"),
    path("bulk-update/", views.bulk_update_rates, name="bulk_update_rates"),
    path("motor-payout-rates/", views.motor_payout_rates, name="motor_payout_rates"),
    path('export-rto/', views.export_rto_xlsx, name='export_rto_xlsx'),
    path('export-make-model/', views.export_make_model_xlsx, name='export_make_model_xlsx'),
    path('rto/edit/<int:pk>/', views.edit_rto, name='edit_rto'),
    path('make-model/edit/<int:pk>/', views.edit_make_model, name='edit_make_model'),
    path("user-management/", views.user_management, name="user_management"),
    path("export/", views.export_rates_xlsx, name="export_rates_xlsx"),
    path("rto-dashboard/", views.rto_dashboard, name="rto_dashboard"),
    path("make-model-dashboard/", views.make_model_dashboard, name="make_model_dashboard"),
]
