"""Django-настройки WEB UI. Приложение локальное, один пользователь."""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent

for p in (ROOT_DIR / "src", ROOT_DIR / "vendor", BASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

SECRET_KEY = "md9600-webui-local-only-key-not-a-secret"

DEBUG = True

ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]", "testserver"]

if os.environ.get("MD9600_PUBLIC") == "1":
    ALLOWED_HOSTS.append("*")

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "editor",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

LANGUAGE_CODE = "ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
