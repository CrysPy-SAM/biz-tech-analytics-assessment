"""
Django settings for biz-tech-analytics-assessment project.
"""

from pathlib import Path
import os
import sys
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file
load_dotenv(BASE_DIR / ".env")

# Ensure sub-application directories are accessible on sys.path
sys.path.insert(0, str(BASE_DIR / "section-1-django-debugging"))
sys.path.insert(0, str(BASE_DIR / "section-2-job-queue"))

# Quick-start development settings - unsuitable for production
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "django-insecure-assessment-biz-tech-analytics-secret-key-prod-change-me",
)

DEBUG = os.getenv("DEBUG", "True").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0,testserver").split(",")
    if host.strip()
]
if "testserver" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("testserver")

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party applications
    "rest_framework",
    "silk",
    # Local applications
    "orders.apps.OrdersConfig",
    "email_queue.apps.EmailQueueConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "silk.middleware.SilkyMiddleware",  # Django Silk profiler middleware
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# In test runs, bypass profiler interception so internal Silk logging does not skew query assertions
IS_TESTING = "test" in sys.argv or any("pytest" in arg for arg in sys.argv)
if IS_TESTING:
    MIDDLEWARE = [m for m in MIDDLEWARE if m != "silk.middleware.SilkyMiddleware"]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database configuration
# Support PostgreSQL via env var or SQLite local fallback
DB_ENGINE = os.getenv("DB_ENGINE", "django.db.backends.sqlite3")

if "postgresql" in DB_ENGINE:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", "biztech_db"),
            "USER": os.getenv("DB_USER", "postgres"),
            "PASSWORD": os.getenv("DB_PASSWORD", "postgres"),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / os.getenv("DB_NAME", "db.sqlite3"),
        }
    }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Django REST Framework configuration
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
}

# Django Silk Profiler Settings
IS_TESTING = "test" in sys.argv or any("pytest" in arg for arg in sys.argv)
SILKY_PYTHON_PROFILER = True
SILKY_PYTHON_PROFILER_BINARY = False
SILKY_AUTHENTICATION = False  # Set to True in production
SILKY_AUTHORISATION = False   # Set to True in production
SILKY_META = True
SILKY_INTERCEPT_PERCENT = 0 if IS_TESTING else 100
SILKY_MAX_RECORDED_REQUESTS = 1000
SILKY_IGNORE_PATHS = ["/silk/"]

# Celery / Redis Settings
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
CELERY_MAX_RETRIES = int(os.getenv("CELERY_MAX_RETRIES", "5"))
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "False").lower() in ("true", "1")
CELERY_TASK_EAGER_PROPAGATES = True
