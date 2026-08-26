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

# URL of the standalone Life Payout Grid app (life-payout-grid/), linked from
# the sidebar. Defaults to the local Next.js dev server; set this in Railway
# env vars once that service has a real URL, e.g.
# LIFE_PAYOUT_GRID_URL=https://life-payout-grid-production.up.railway.app
LIFE_PAYOUT_GRID_URL = os.getenv("LIFE_PAYOUT_GRID_URL", "http://localhost:3000")

# Shared secret used to sign the handoff token when a permitted user opens
# the Life Payout Grid's admin page from this portal's sidebar (see
# views.life_payout_grid_admin_redirect). Must be set to the *same* value
# as LIFE_PAYOUT_GRID_AUTH_SECRET on the life-payout-grid Railway service --
# that app trusts a token signed with this secret instead of a password of
# its own. Set in this repo's .env for local dev (must match
# life-payout-grid/.env.local); required as a real Railway env var in
# production.
LIFE_PAYOUT_GRID_AUTH_SECRET = os.getenv("LIFE_PAYOUT_GRID_AUTH_SECRET")
if not LIFE_PAYOUT_GRID_AUTH_SECRET and not DEBUG:
    raise RuntimeError(
        "LIFE_PAYOUT_GRID_AUTH_SECRET environment variable is not set. "
        "Refusing to start in production without it."
    )

# Signs the short-lived ticket used to hand an already-logged-in user off
# from an external partner portal (e.g. ArhamSecure's partner portal) into
# this app without a second login -- see insurance/sso.py and
# views.IssueSSOTicketAPIView / views.sso_consume_view. Unlike
# LIFE_PAYOUT_GRID_AUTH_SECRET this is not shared with another app's env --
# only this app ever signs or verifies it.
PARTNER_SSO_TICKET_SECRET = os.getenv("PARTNER_SSO_TICKET_SECRET")
if not PARTNER_SSO_TICKET_SECRET and not DEBUG:
    raise RuntimeError(
        "PARTNER_SSO_TICKET_SECRET environment variable is not set. "
        "Refusing to start in production without it."
    )

# =========================================================
# CSRF
# =========================================================
# up.railway.app is a shared, multi-tenant domain — anyone can deploy their
# own app to some-other-name.up.railway.app. A wildcard entry here would
# trust that app's Origin/Referer for CSRF purposes on this one too, so list
# only the exact production hostname(s) actually in use.
CSRF_TRUSTED_ORIGINS = [
    "https://web-production-23c64.up.railway.app",
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
                'insurance.context_processors.sidebar_access',
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

# =========================================================
# FILE STORAGE (media -> Cloudflare R2, static -> WhiteNoise, unchanged)
# =========================================================
# Falls back to local disk when R2 credentials aren't configured (e.g. local
# dev) - without this, any access to a FileField's .url crashes with boto3's
# "Required parameter name not set" the moment AWS_STORAGE_BUCKET_NAME is
# empty, since S3Storage has no other way to build a bucket reference.
USE_S3_STORAGE = bool(
    os.getenv("AWS_STORAGE_BUCKET_NAME")
    and os.getenv("AWS_ACCESS_KEY_ID")
    and os.getenv("AWS_SECRET_ACCESS_KEY")
)

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage" if USE_S3_STORAGE else "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME")
AWS_S3_ENDPOINT_URL = os.getenv("AWS_S3_ENDPOINT_URL")  # https://<ACCOUNT_ID>.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_S3_REGION_NAME = "auto"          # R2 ignores region but boto3 requires a value; Cloudflare's own docs use "auto"
AWS_S3_SIGNATURE_VERSION = "s3v4"    # R2 requires SigV4
AWS_S3_ADDRESSING_STYLE = "virtual"

# R2 has no ACL support — sending an ACL header errors outright, not just no-ops.
# None tells django-storages to omit it entirely. Objects stay private regardless
# (AWS_QUERYSTRING_AUTH defaults to True, so .url generates signed, expiring links).
AWS_DEFAULT_ACL = None

# The S3 backend's own default is True (silently overwrites a same-named key).
# FileSystemStorage's default behavior is to auto-suffix instead — this restores
# that, since nothing else guards against two unrelated uploads sharing a filename
# except one specific duplicate-name check in upload_extract_pdf.
AWS_S3_FILE_OVERWRITE = False

# =========================================================
# MEDIA FILES
# =========================================================
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# =========================================================
# CELERY (background jobs: MIS mapping, Gemini OCR extraction)
# =========================================================
CELERY_BROKER_URL = os.getenv("REDIS_URL")
CELERY_RESULT_BACKEND = os.getenv("REDIS_URL")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

# A worker crash/restart mid-job shouldn't silently lose it — only ack once
# the task actually finishes, and don't let one worker process hoard more
# than one long-running job at a time.
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# Periodic jobs, run by a `celery -A project beat` process (see Procfile's
# `beat` entry). Requires that process to actually be deployed/running.
from celery.schedules import crontab  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    "cleanup-motor-points-search-logs": {
        "task": "insurance.tasks.cleanup_motor_points_search_logs",
        "schedule": crontab(hour=2, minute=0),
    },
    "cleanup-security-audit-logs": {
        "task": "insurance.tasks.cleanup_security_audit_logs",
        "schedule": crontab(hour=2, minute=15),
    },
}

# =========================================================
# CACHE (used for login-attempt throttling)
# =========================================================
# DB-backed rather than LocMemCache: LocMemCache is per-process, so with
# multiple Railway workers each one tracked its own separate attempt count,
# making the lockout inconsistent. The cache table is created by
# `manage.py createcachetable` (run automatically via the Procfile release
# step on deploy).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache_table",
    }
}

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
# Two mutually exclusive security modes depending on the provider/port:
# STARTTLS (port 587, EMAIL_USE_TLS) or implicit SSL (port 465, EMAIL_USE_SSL).
# Default keeps the previous STARTTLS-on-587 behavior; providers/accounts
# that require port 465 (e.g. Zoho's regional servers) set EMAIL_USE_SSL=True
# and EMAIL_USE_TLS=False instead.
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True').lower() == 'true'
EMAIL_USE_SSL = os.getenv('EMAIL_USE_SSL', 'False').lower() == 'true'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')
# Office365 (and most SMTP relays) reject mail where From doesn't match the
# authenticated account, so this must line up with EMAIL_HOST_USER.
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER)

# =========================================================
# GEMINI (AI OCR extraction)
# =========================================================
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

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
        # Django's own default logging only sends unhandled 500s to the
        # console when DEBUG=True, and to ADMINS by email otherwise — with
        # no ADMINS configured, that means production 500s were being
        # dropped with no record anywhere. Force them to stdout unconditionally.
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}