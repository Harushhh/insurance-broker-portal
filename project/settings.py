from pathlib import Path
import os
from dotenv import load_dotenv
import dj_database_url

# =========================================================
# BASE SETUP
# =========================================================
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# =========================================================
# SECURITY & CORE SETTINGS
# =========================================================
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        # Fine for local dev only — never reaches production because of the check below.
        SECRET_KEY = "django-insecure-dev-only-key-do-not-use-in-prod"
    else:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. "
            "Refusing to start in production without it."
        )

# ALLOWED_HOSTS: no wildcard by default. You must set this explicitly in
# your Railway env vars, e.g. ALLOWED_HOSTS=web-production-23c64.up.railway.app
_allowed_hosts_env = os.getenv("ALLOWED_HOSTS", "")
if _allowed_hosts_env:
    ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]
elif DEBUG:
    ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
else:
    raise RuntimeError(
        "ALLOWED_HOSTS environment variable is not set. "
        "Refusing to start in production with an open host list."
    )

# =========================================================
# CSRF
# =========================================================
CSRF_TRUSTED_ORIGINS = [
    "https://web-production-23c64.up.railway.app",
    "https://*.up.railway.app",
]
if DEBUG:
    CSRF_TRUSTED_ORIGINS += ["http://127.0.0.1:8000", "http://localhost:8000"]

# =========================================================
# INSTALLED APPS
# =========================================================
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'whitenoise.runserver_nostatic',
    'django.contrib.staticfiles',

    # API
    'rest_framework',
    'drf_spectacular',
    'rest_framework_api_key',

    # LOCAL APPS
    'insurance',
    'config',
    'dashboard',
]

# =========================================================
# MIDDLEWARE
# =========================================================
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'project.urls'

# =========================================================
# TEMPLATES
# =========================================================
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'insurance', 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'project.wsgi.application'

# =========================================================
# DATABASE
# =========================================================
local_db_url = (
    f"postgres://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST', 'localhost')}:"
    f"{os.getenv('DB_PORT', '5432')}/"
    f"{os.getenv('DB_NAME')}"
    if os.getenv('DB_NAME')
    else f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
)

DATABASES = {
    'default': dj_database_url.config(
        default=os.getenv('DATABASE_URL', local_db_url),
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# =========================================================
# PASSWORD VALIDATION
# =========================================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 10},
    },
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# =========================================================
# INTERNATIONALIZATION
# =========================================================
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# =========================================================
# STATIC FILES
# =========================================================
STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

local_static_dir = os.path.join(BASE_DIR, 'static')
STATICFILES_DIRS = [local_static_dir] if os.path.exists(local_static_dir) else []

STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# =========================================================
# MEDIA FILES
# =========================================================
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# =========================================================
# CACHE (used for login-attempt throttling)
# =========================================================
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}
# NOTE: LocMemCache is per-process. If you ever run multiple Railway
# workers/replicas, switch this to Redis (django-redis) so throttling
# state is shared across all of them.

# =========================================================
# SECURITY HEADERS & COOKIES
# =========================================================
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Session expires after 30 min of inactivity; also expires on browser close.
SESSION_COOKIE_AGE = 1800
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # Railway terminates TLS at its edge proxy and forwards plain HTTP,
    # so Django needs this header to know the original request was HTTPS.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
else:
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_SSL_REDIRECT = False

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# =========================================================
# AUTH REDIRECTS
# =========================================================
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/home/"
LOGOUT_REDIRECT_URL = "/login/"

# =========================================================
# EMAIL CONFIG
# =========================================================
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True').lower() == 'true'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')
# Office365 (and most SMTP relays) reject mail where From doesn't match the
# authenticated account, so this must line up with EMAIL_HOST_USER.
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER)

# =========================================================
# DRF + SWAGGER
# =========================================================
REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Insurance Portal API',
    'DESCRIPTION': 'API documentation for Insurance Broker Portal',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SECURITY': [{'ApiKeyAuth': []}],
    'COMPONENTS': {
        'securitySchemes': {
            'ApiKeyAuth': {
                'type': 'apiKey',
                'in': 'header',
                'name': 'Authorization',
                'description': 'Format: Api-Key <your-key>'
            }
        }
    },
}

# =========================================================
# FILE UPLOAD LIMIT
# =========================================================
DATA_UPLOAD_MAX_MEMORY_SIZE = 20971520  # 20 MB

# =========================================================
# LOGGING (auth events land in stdout -> visible in Railway logs)
# =========================================================
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "security": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": True,
        },
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": True,
        },
    },
}