"""
Django settings for repo_verdict project.
"""
from io import StringIO
import os
from pathlib import Path
from dotenv import dotenv_values


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv_file(path: Path) -> None:
    """Load .env files written by common macOS/Linux/Windows editors.

    Windows PowerShell's Out-File can create UTF-16 files by default. The stock
    python-dotenv loader expects UTF-8, so a shared project could crash before
    Django starts. Decode with a small set of common encodings and set values
    without overriding real environment variables.
    """
    if not path.exists():
        return

    text = None
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        return

    for key, value in dotenv_values(stream=StringIO(text)).items():
        if key and value is not None and key not in os.environ:
            os.environ[key] = value


_load_dotenv_file(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-change-me-in-production")

DEBUG = os.getenv("DEBUG", "True").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "analyzer",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "repo_verdict.urls"

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

WSGI_APPLICATION = "repo_verdict.wsgi.application"
ASGI_APPLICATION = "repo_verdict.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
WHITENOISE_USE_FINDERS = True
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
}

# RepoVerdict configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Request timeout for GitHub API calls
GITHUB_TIMEOUT = int(os.getenv("GITHUB_TIMEOUT", "30"))

# Maximum paginated pages to fetch per resource (issues/PRs/branches)
GITHUB_MAX_PAGES = int(os.getenv("GITHUB_MAX_PAGES", "3"))

# Related-repo prior-art analysis. Enabled by default so prominent public forks
# can prevent false "new" verdicts when they already contain the implementation.
ANALYZE_RELATED_REPOS = os.getenv("ANALYZE_RELATED_REPOS", "True").lower() in ("true", "1", "yes")
RELATED_REPO_LIMIT = int(os.getenv("RELATED_REPO_LIMIT", "4"))
RELATED_FORK_MIN_STARS = int(os.getenv("RELATED_FORK_MIN_STARS", "100"))
RELATED_SEARCH_TERMS = int(os.getenv("RELATED_SEARCH_TERMS", "8"))
