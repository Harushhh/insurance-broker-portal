from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.conf.urls.static import static

from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from insurance.auth_views import SignupView, ThrottledLoginView

urlpatterns = [
    path("admin/", admin.site.urls),

    # login page at /login/ — now with throttling
    path("login/", ThrottledLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("signup/", SignupView.as_view(), name="signup"),

    # your app
    path("", include("insurance.urls")),

    # ==========================================
    # SWAGGER & API DOCUMENTATION URLS
    # ==========================================
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/schema/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)