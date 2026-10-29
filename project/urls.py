from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.conf.urls.static import static

from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from insurance.access import staff_required
from insurance.auth_views import SignupView, ThrottledLoginView

urlpatterns = [
    path("admin/", admin.site.urls),

    # login page at /login/ — now with throttling
    path("login/", ThrottledLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("signup/", SignupView.as_view(), name="signup"),

    # API documentation (OpenAPI schema, Swagger UI, ReDoc) -- staff only. Listed before the app's urls on
    # purpose: insurance.urls ends in a catch-all that redirects every unmatched path to the login page, so
    # these were unreachable when they sat after the include.
    path("api/schema/", staff_required(SpectacularAPIView.as_view()), name="schema"),
    path("api/schema/swagger-ui/", staff_required(SpectacularSwaggerView.as_view(url_name="schema")), name="swagger-ui"),
    path("api/schema/redoc/", staff_required(SpectacularRedocView.as_view(url_name="schema")), name="redoc"),

    # your app
    path("", include("insurance.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)