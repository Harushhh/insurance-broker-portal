from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # ✅ when user opens http://127.0.0.1:8000/ -> show login
    path("", auth_views.LoginView.as_view(template_name="registration/login.html"), name="home"),

    path("upload/", views.upload_csv, name="upload"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("user-management/", views.user_management, name="user_management"),
    path("export/", views.export_rates_xlsx, name="export_rates_xlsx"),
    path("rto-dashboard/", views.rto_dashboard, name="rto_dashboard"),
    path("make-model-dashboard/", views.make_model_dashboard, name="make_model_dashboard"),
]
