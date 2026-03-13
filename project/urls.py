from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.conf.urls.static import static

# --- ADDED FOR SWAGGER ---
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
# -------------------------

urlpatterns = [
    path("admin/", admin.site.urls),

    # login page at /login/
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    # your app
    path("", include("insurance.urls")),
    
    # ==========================================
    # SWAGGER & API DOCUMENTATION URLS
    # ==========================================
    # 1. The raw JSON/YAML schema file
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    
    # 2. The interactive Swagger UI
    path('api/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    
    # 3. ReDoc UI (Alternative beautiful layout for API docs)
    path('api/schema/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

# ✅ NEW: This tells Django how to serve the files uploaded in the Grid Management page
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)