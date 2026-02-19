from django.urls import path
from . import views

urlpatterns = [
    path("", views.upload_csv, name="upload"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/export/", views.export_groups_xlsx, name="export_groups_xlsx"),
]
