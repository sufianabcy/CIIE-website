"""Settings for the KL-CIIE event portal.

Every value that differs between machines comes from an environment variable
(or a local .env file). See .env.example for the full list.
"""

from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env.bool("DEBUG", default=False)
TESTING = env.bool("TESTING", default=False)

SECRET_KEY = env("SECRET_KEY", default="")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "dev-only-insecure-key"
    else:
        raise ImproperlyConfigured("Set SECRET_KEY in the environment.")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
SITE_URL = env("SITE_URL", default="http://localhost:8000").rstrip("/")
# Render tells the app its own address; use it so a first deploy needs fewer settings.
RENDER_EXTERNAL_HOSTNAME = env("RENDER_EXTERNAL_HOSTNAME", default="")
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_EXTERNAL_HOSTNAME}")
    if "SITE_URL" not in env.ENVIRON:
        SITE_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.microsoft",
    "pgtrigger",
    "pghistory",
    "pghistory.admin",
    "people",
    "ops",
    "mailer",
    "events",
    "registrations",
    "checkin",
    "results",
    "judging",
]

MIDDLEWARE = [
    "ops.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "pghistory.middleware.HistoryMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "ops.context_processors.portal",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": env.db(
        "DATABASE_URL", default="postgres://ciie:ciie@localhost:5432/ciie"
    )
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

# Shared through the database, so rate limits hold across processes without Redis.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
    }
}
if TESTING:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "people.Person"
# Person.email is unique through a case-insensitive constraint, not unique=True,
# because walk-ins may have no email at all.
SILENCED_SYSTEM_CHECKS = ["auth.W004"]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG or TESTING
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# --- Sessions and security -------------------------------------------------
SESSION_COOKIE_AGE = 60 * 60 * 24 * 60  # 60 days: people sign in once a season
SESSION_COOKIE_SECURE = env.bool("SECURE_COOKIES", default=not DEBUG)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
SECURE_REDIRECT_EXEMPT = [r"^ops/healthz/$"]
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# --- Sign-in (django-allauth) ----------------------------------------------
LOGIN_URL = "signin"
LOGIN_REDIRECT_URL = "my_registrations"
ACCOUNT_LOGOUT_REDIRECT_URL = "event_list"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]  # no passwords anywhere
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED = True
ACCOUNT_LOGIN_BY_CODE_ENABLED = True
ACCOUNT_LOGIN_BY_CODE_TIMEOUT = 15 * 60  # codes can be slow to arrive in a rush
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_PREVENT_ENUMERATION = False
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[KL-CIIE] "
ACCOUNT_ADAPTER = "people.adapters.AccountAdapter"
SOCIALACCOUNT_ADAPTER = "people.adapters.SocialAccountAdapter"
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
SOCIALACCOUNT_ONLY = False

GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", default="")
MICROSOFT_CLIENT_ID = env("MICROSOFT_CLIENT_ID", default="")
KL_MICROSOFT_TENANT_ID = env("KL_MICROSOFT_TENANT_ID", default="")

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APPS": (
            [
                {
                    "client_id": GOOGLE_CLIENT_ID,
                    "secret": env("GOOGLE_CLIENT_SECRET", default=""),
                    "key": "",
                }
            ]
            if GOOGLE_CLIENT_ID
            else []
        ),
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"prompt": "select_account"},
        # Google confirms the address, so a Google login may join an existing
        # person with the same verified email (brief B6).
        "EMAIL_AUTHENTICATION": True,
        "EMAIL_AUTHENTICATION_AUTO_CONNECT": True,
    },
    "microsoft": {
        "APPS": (
            [
                {
                    "client_id": MICROSOFT_CLIENT_ID,
                    "secret": env("MICROSOFT_CLIENT_SECRET", default=""),
                    "key": "",
                    # A specific tenant ID lets only KL accounts sign in.
                    "settings": {"tenant": KL_MICROSOFT_TENANT_ID or "organizations"},
                }
            ]
            if MICROSOFT_CLIENT_ID
            else []
        ),
        "SCOPE": ["User.Read"],
    },
}

# --- KL-specific rules -------------------------------------------------------
KL_EMAIL_DOMAINS = [d.lower() for d in env.list("KL_EMAIL_DOMAINS", default=["kluniversity.in"])]
# KL addresses appear to be the student ID plus the KL domain (brief B6, to verify).
KL_STUDENT_ID_PATTERN = env("KL_STUDENT_ID_PATTERN", default=r"^\d{8,12}$")
# How a KL ID typed at the walk-in desk becomes an email (to verify with KL).
KL_STUDENT_EMAIL_FORMAT = env("KL_STUDENT_EMAIL_FORMAT", default="{id}@kluniversity.in")

CLUB_NAME = env("CLUB_NAME", default="KL-CIIE")
CLUB_CONTACT_EMAIL = env("CLUB_CONTACT_EMAIL", default="klciie@kluniversity.in")

# --- Email -------------------------------------------------------------------
# EMAIL_URL examples: consolemail://  or  smtp+tls://user:app-password@smtp.gmail.com:587
_email = env.email_url("EMAIL_URL", default="consolemail://")
EMAIL_BACKEND = _email["EMAIL_BACKEND"]
for _key in ("EMAIL_HOST", "EMAIL_PORT", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD", "EMAIL_USE_TLS", "EMAIL_USE_SSL"):
    if _key in _email:
        globals()[_key] = _email[_key]
# Free hosts often block SMTP ports; an HTTPS sending API avoids that.
ANYMAIL_ESP = env("ANYMAIL_ESP", default="")  # e.g. brevo, resend, postmark
if ANYMAIL_ESP:
    EMAIL_BACKEND = f"anymail.backends.{ANYMAIL_ESP}.EmailBackend"
    ANYMAIL = {f"{ANYMAIL_ESP.upper()}_API_KEY": env("ANYMAIL_API_KEY", default="")}
if TESTING:
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default=f"{CLUB_NAME} <noreply@localhost>")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

MAIL_SEND_INLINE = env.bool("MAIL_SEND_INLINE", default=True)
MAIL_DAILY_CAP = env.int("MAIL_DAILY_CAP", default=450)
MAIL_CODE_RESERVE = env.int("MAIL_CODE_RESERVE", default=100)
MAIL_RATE_PER_MINUTE = env.int("MAIL_RATE_PER_MINUTE", default=20)
# Secret for /ops/tick/, which free schedulers can call to send queued mail.
OPS_TICK_TOKEN = env("OPS_TICK_TOKEN", default="")

# --- Backups -----------------------------------------------------------------
# Comma-separated rclone remotes, e.g. "gdrive:klciie-backups,onedrive:klciie-backups"
BACKUP_RCLONE_REMOTES = env.list("BACKUP_RCLONE_REMOTES", default=[])
BACKUP_AGE_RECIPIENT = env("BACKUP_AGE_RECIPIENT", default="")  # public key; private key stays in the password manager
BACKUP_INTERVAL_MINUTES = env.int("BACKUP_INTERVAL_MINUTES", default=60)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
}
