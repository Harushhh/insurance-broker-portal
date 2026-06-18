from pathlib import Path
import os
from dotenv import load_dotenv
import dj_database_url  # <-- ADDED FOR RENDER DEPLOYMENT

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / ".env")

# =========================================================
# SECURITY & CORE SETTINGS
# =========================================================
SECRET_KEY = os.getenv("SECRET_KEY", "fallback-secret-key-for-dev")

# Automatically set DEBUG to False if deployed on Render
DEBUG = 'RENDER' not in os.environ and os.getenv("DEBUG", "False").lower() == "true"

# Allow Render's URL in production, fallback to localhost for dev
if 'RENDER' in os.environ:
    ALLOWED_HOSTS = ['*'] # Allows Render domains automatically
else:
    ALLOWED_HOSTS = [host.strip() for host in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")]
    # Ensure ngrok is always allowed for local tunneling
    if '.ngrok-free.app' not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append('.ngrok-free.app')
    if '.ngrok-free.dev' not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append('.ngrok-free.dev')

# Trust secure POST requests from ngrok domains
CSRF_TRUSTED_ORIGINS = [
    'https://*.ngrok-free.app',
    'https://*.ngrok-free.dev',
]

# Gemini AI API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # --- SWAGGER & API ---
    'rest_framework',
    'drf_spectacular',
    'rest_framework_api_key', 
    
    # --- LOCAL APPS ---
    'insurance',
    'config',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware', # <-- ADDED WHITENOISE HERE
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'project.urls'

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
# DATABASE (Render + Local Postgres Support)
# =========================================================
# This smart config uses Render's DATABASE_URL if it exists. 
# If not, it builds a URL from your local .env Postgres variables.
local_db_url = f"postgres://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME')}" if os.getenv('DB_NAME') else f"sqlite:///{BASE_DIR / 'db.sqlite3'}"

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
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# =========================================================
# INTERNATIONALIZATION
# =========================================================
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# =========================================================
# STATIC FILES (CSS, JavaScript, Images)
# =========================================================
STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')] if os.path.exists(os.path.join(BASE_DIR, 'static')) else []

# Tell WhiteNoise to compress and cache static files in production
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# =========================================================
# MEDIA UPLOADS & SECURITY OVERRIDES (PDF PREVIEWS)
# =========================================================
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# ⚠️ CRITICAL FOR MIS REVIEW: 
# Allows the <object> or <iframe> tag to load PDFs from your own domain 
# without triggering Django's default Clickjacking protection errors.
X_FRAME_OPTIONS = 'SAMEORIGIN'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Auth Redirects
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/home/"  
LOGOUT_REDIRECT_URL = "/login/"

# =========================================================
# EMAIL CONFIGURATION
# =========================================================
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True').lower() == 'true'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')

# =========================================================
# DRF & SWAGGER (SPECTACULAR) SETTINGS
# =========================================================
REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Insurance Portal API',
    'DESCRIPTION': 'API documentation for the Insurance Broker Portal',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SECURITY': [{'ApiKeyAuth': []}],
    'COMPONENTS': {
        'securitySchemes': {
            'ApiKeyAuth': {
                'type': 'apiKey',
                'in': 'header',
                'name': 'Authorization',
                'description': 'Enter your API key in the format: <b>Api-Key &lt;your-key&gt;</b>'
            }
        }
    },
}

# =========================================================
# LARGE DATA UPLOADS CONFIGURATION
# =========================================================
# Increase max payload size for bulk API JSON uploads (20 MB)
# This prevents the 500 error when processing large PapaParse chunks
DATA_UPLOAD_MAX_MEMORY_SIZE = 20971520