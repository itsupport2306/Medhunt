"""
Configuration for the Radixsol Sourcing Assistant.

Provider credentials, LLM settings, and compliance defaults live here. The
extension uses server-side People Data Labs enrichment for selected candidates.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from dotenv import dotenv_values, load_dotenv

from .nexus_reference import load_reference as load_nexus_reference

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
IS_FROZEN = bool(getattr(sys, "frozen", False))
_APP_HOME_OVERRIDE = os.getenv("RADIXSOL_HOME", "").strip()

if _APP_HOME_OVERRIDE:
    APP_HOME = Path(_APP_HOME_OVERRIDE).expanduser().resolve()
elif IS_FROZEN:
    _local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    APP_HOME = (
        Path(_local_app_data).resolve()
        if _local_app_data
        else (Path.home() / "AppData" / "Local").resolve()
    ) / "Radixsol"
else:
    APP_HOME = _PROJECT_ROOT

_USES_APP_HOME_LAYOUT = bool(IS_FROZEN or _APP_HOME_OVERRIDE)
CONFIG_DIR = APP_HOME / "config" if _USES_APP_HOME_LAYOUT else APP_HOME
DATA_DIR = APP_HOME / "data" if _USES_APP_HOME_LAYOUT else APP_HOME
LOG_DIR = APP_HOME / "logs" if _USES_APP_HOME_LAYOUT else APP_HOME / ".runtime"
for _directory in (CONFIG_DIR, DATA_DIR, LOG_DIR):
    _directory.mkdir(parents=True, exist_ok=True)

_ENV_FILE = CONFIG_DIR / ".env"
_ENV_LOCAL_FILE = CONFIG_DIR / ".env.local"
# Explicit process variables are the highest-precedence configuration source.
# Keep a snapshot so a developer ``.env.local`` can override the shared
# ``.env`` file without overriding test/launcher/container isolation.  The old
# unconditional ``override=True`` made pytest's temporary SOURCING_DB point at
# a real benchmark database and allowed a test reset to erase it.
_PROCESS_ENVIRONMENT = dict(os.environ)
load_dotenv(_ENV_FILE, override=False)
load_dotenv(_ENV_LOCAL_FILE, override=True)
# Development/administrator installations may point at the existing Nexus app
# rather than duplicating its private credential file. Only allowlisted Nexus
# settings are read, and the referenced Python file is parsed rather than
# imported. Explicit Medhunt settings always win.
_NEXUS_REFERENCE_ENV = (
    _PROCESS_ENVIRONMENT.get(
        "NEXUS_REFERENCE_ENV", os.getenv("NEXUS_REFERENCE_ENV", "")
    )
).strip()
_NEXUS_REFERENCE_CONFIG = (
    _PROCESS_ENVIRONMENT.get(
        "NEXUS_REFERENCE_CONFIG", os.getenv("NEXUS_REFERENCE_CONFIG", "")
    )
).strip()
if _NEXUS_REFERENCE_ENV or _NEXUS_REFERENCE_CONFIG:
    _nexus_reference_values = load_nexus_reference(
        _NEXUS_REFERENCE_ENV,
        _NEXUS_REFERENCE_CONFIG,
    )
    for _key, _value in _nexus_reference_values.items():
        if not str(os.getenv(_key, "")).strip():
            os.environ[_key] = _value
for _key, _value in _PROCESS_ENVIRONMENT.items():
    os.environ[_key] = _value

# Per-install browser-to-loopback authentication. Setup generates this value
# independently on every workstation and injects it only into that local copy
# of the unpacked extension. In an installed/frozen build the installer-owned
# file is authoritative: an ambient process variable must not silently disable
# or desynchronise the backend and extension tokens.
if _USES_APP_HOME_LAYOUT:
    _managed_local_values = dotenv_values(_ENV_LOCAL_FILE)
    LOCAL_API_TOKEN = str(
        _managed_local_values.get("MEDHUNT_LOCAL_API_TOKEN") or ""
    ).strip()
else:
    LOCAL_API_TOKEN = os.getenv("MEDHUNT_LOCAL_API_TOKEN", "").strip()
if IS_FROZEN and not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", LOCAL_API_TOKEN):
    raise RuntimeError(
        "The installed Medhunt local API token is missing or invalid. Run Setup again."
    )

# Public Medhunt accounts belong to HealthBoard. The extension asks only for
# an email and verification code; this backend validates HealthBoard's opaque,
# limited extension token and reports enrichment activity to its analytics.
HEALTHBOARD_BASE_URL = os.getenv("HEALTHBOARD_BASE_URL", "").strip().rstrip("/")
MEDHUNT_MAIN_WEBSITE_URL = os.getenv(
    "MEDHUNT_MAIN_WEBSITE_URL", "https://medhunt.ai/"
).strip()
HEALTHBOARD_AUTH_TIMEOUT = max(
    2.0, min(30.0, float(os.getenv("HEALTHBOARD_AUTH_TIMEOUT", "8")))
)
HEALTHBOARD_AUTH_CACHE_SECONDS = max(
    0, min(300, int(os.getenv("HEALTHBOARD_AUTH_CACHE_SECONDS", "60")))
)
# Shared service credential for Halo analytics delivery and internal service routes.
MEDHUNT_HEALTHBOARD_SERVICE_TOKEN = os.getenv(
    "MEDHUNT_HEALTHBOARD_SERVICE_TOKEN", ""
).strip()
# Retained for deployments that still set this variable; device registration
# no longer blocks sign-in based on a device count.
MEDHUNT_MAX_REGISTERED_DEVICES = max(
    1, min(5, int(os.getenv("MEDHUNT_MAX_REGISTERED_DEVICES", "2")))
)
MEDHUNT_ADMIN_EMAILS = frozenset(
    email.strip().casefold()
    for email in re.split(r"[,;]", os.getenv("MEDHUNT_ADMIN_EMAILS", ""))
    if email.strip()
)
MEDHUNT_DEVICE_IDLE_DAYS = max(
    7, min(365, int(os.getenv("MEDHUNT_DEVICE_IDLE_DAYS", "90")))
)

# Zoom Phone remains configured for compatibility, while SMS_PROVIDER selects
# Twilio for current outbound delivery.
ZOOM_SMS_ENABLED_REQUESTED = os.getenv("ZOOM_SMS_ENABLED", "0").strip().lower() in (
    "1", "true", "yes",
)
ZOOM_ACCOUNT_ID = os.getenv("ZOOM_ACCOUNT_ID", "").strip()
ZOOM_CLIENT_ID = os.getenv("ZOOM_CLIENT_ID", "").strip()
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET", "").strip()
ZOOM_SMS_SENDER_USER_ID = os.getenv("ZOOM_SMS_SENDER_USER_ID", "").strip()
ZOOM_SMS_SENDER_NUMBER = os.getenv("ZOOM_SMS_SENDER_NUMBER", "").strip()
ZOOM_WEBHOOK_SECRET_TOKEN = os.getenv("ZOOM_WEBHOOK_SECRET_TOKEN", "").strip()
ZOOM_API_BASE_URL = os.getenv("ZOOM_API_BASE_URL", "https://api.zoom.us/v2").strip().rstrip("/")
ZOOM_OAUTH_URL = os.getenv("ZOOM_OAUTH_URL", "https://zoom.us/oauth/token").strip()
ZOOM_SMS_TIMEOUT = max(3.0, min(60.0, float(os.getenv("ZOOM_SMS_TIMEOUT", "20"))))
ZOOM_SMS_ENABLED = bool(
    ZOOM_SMS_ENABLED_REQUESTED and ZOOM_ACCOUNT_ID and ZOOM_CLIENT_ID
    and ZOOM_CLIENT_SECRET
)
ZOOM_SMS_TEST_MODE = os.getenv("ZOOM_SMS_TEST_MODE", "0").strip().lower() in (
    "1", "true", "yes",
)
ZOOM_SMS_TEST_NUMBERS = tuple(dict.fromkeys(
    value.strip()
    for value in os.getenv("ZOOM_SMS_TEST_NUMBERS", "").split(",")
    if value.strip()
))

# Hosted browser clients must be explicitly allowlisted once Chrome assigns
# the production extension ID. Local development keeps the broad extension
# origin rule unless the operator opts into strict mode.
_EXTENSION_ORIGIN_PATTERN = re.compile(
    r"^(?:chrome-extension://[a-p]{32}|moz-extension://[A-Za-z0-9-]+)$"
)
_extension_origins_raw = os.getenv("MEDHUNT_EXTENSION_ORIGINS", "").strip()
EXTENSION_ALLOWED_ORIGINS = tuple(dict.fromkeys(
    value.strip().rstrip("/")
    for value in _extension_origins_raw.split(",")
    if value.strip()
))
_invalid_extension_origins = [
    value for value in EXTENSION_ALLOWED_ORIGINS
    if not _EXTENSION_ORIGIN_PATTERN.fullmatch(value)
]
if _invalid_extension_origins:
    raise RuntimeError(
        "MEDHUNT_EXTENSION_ORIGINS contains an invalid browser extension origin."
    )
_hosted_runtime = os.getenv("RENDER", "").strip().lower() in ("1", "true", "yes")
EXTENSION_ALLOW_UNLISTED_ORIGINS = os.getenv(
    "MEDHUNT_ALLOW_UNLISTED_EXTENSION_ORIGINS",
    "0" if _hosted_runtime else "1",
).strip().lower() in ("1", "true", "yes")

# ---- Enformion / Endato API ----
ENFORMION_URL = os.getenv(
    "ENFORMION_URL", "https://devapi.enformion.com/PersonSearch"
).strip()
ENFORMION_AP_NAME = (
    os.getenv("ENFORMION_AP_NAME", "").strip()
    or os.getenv("ENFORMION_PROFILE_NAME", "").strip()
    or os.getenv("Profile_name", "").strip()
)
ENFORMION_AP_PASSWORD = (
    os.getenv("ENFORMION_AP_PASSWORD", "").strip()
    or os.getenv("ENFORMION_PASSWORD", "").strip()
    or os.getenv("Password", "").strip()
)
ENFORMION_SEARCH_TYPE = os.getenv("ENFORMION_SEARCH_TYPE", "Person").strip()
HTTP_TIMEOUT = float(os.getenv("ENFORMION_HTTP_TIMEOUT", "20"))
ENFORMION_ENABLED = bool(ENFORMION_AP_NAME and ENFORMION_AP_PASSWORD) and os.getenv(
    "ENFORMION_ENABLED", "1"
).strip().lower() in ("1", "true", "yes")
ENFORMION_VERIFY_PDL = os.getenv(
    "ENFORMION_VERIFY_PDL", "1"
).strip().lower() in ("1", "true", "yes")
ENFORMION_FALLBACK_ONLY = os.getenv(
    "ENFORMION_FALLBACK_ONLY", "1"
).strip().lower() in ("1", "true", "yes")
ENFORMION_CACHE_TTL_SECONDS = max(
    0, int(os.getenv("ENFORMION_CACHE_TTL_SECONDS", str(90 * 24 * 60 * 60)))
)
ENFORMION_MAX_WORKERS = max(
    1, min(10, int(os.getenv("ENFORMION_MAX_WORKERS", "1")))
)
# Person Search can apply a per-account request rate independently of the
# account's remaining credits.  Keep retries bounded and conservative: a
# Retry-After value inside the wait ceiling is honored, while a longer value is
# surfaced to the caller without sending an early retry.
ENFORMION_RATE_LIMIT_RETRIES = max(
    0, min(5, int(os.getenv("ENFORMION_RATE_LIMIT_RETRIES", "2")))
)
ENFORMION_RATE_LIMIT_BACKOFF_SECONDS = max(
    0.0, float(os.getenv("ENFORMION_RATE_LIMIT_BACKOFF_SECONDS", "1.0"))
)
ENFORMION_RATE_LIMIT_MAX_WAIT_SECONDS = max(
    0.0, float(os.getenv("ENFORMION_RATE_LIMIT_MAX_WAIT_SECONDS", "30.0"))
)
# Hard server-side ceiling for fresh Enformion Person Search calls sharing one
# lookup run id.  Unlike PDL's legacy setting, zero is deliberately fail-closed:
# cached results may still be reused, but no fresh paid Enformion call is made.
ENFORMION_RUN_CREDIT_LIMIT = max(
    # One confirmed 50-card Facebook lookup can require a current-city pass
    # plus controlled hometown retries. Both passes share this cumulative
    # ceiling; deployments can lower it explicitly for a smaller spend cap.
    0, min(100, int(os.getenv("ENFORMION_RUN_CREDIT_LIMIT", "100")))
)

# ---- People Data Labs person enrichment ----
# The key is intentionally read only by the backend.  The browser extension
# calls a local endpoint and never receives or stores this credential.
PDL_API_KEY = os.getenv("PDL_API_KEY", "").strip()
PDL_BASE_URL = os.getenv(
    "PDL_BASE_URL", "https://api.peopledatalabs.com/v5/person/enrich"
).strip()
PDL_IDENTIFY_URL = os.getenv(
    "PDL_IDENTIFY_URL", "https://api.peopledatalabs.com/v5/person/identify"
).strip()
PDL_SEARCH_URL = os.getenv(
    "PDL_SEARCH_URL", "https://api.peopledatalabs.com/v5/person/search"
).strip()
PDL_TIMEOUT = float(os.getenv("PDL_TIMEOUT", "20"))
# One bulk request replaces up to 100 sequential person-enrich calls, so it is
# allowed a longer read budget than a single lookup.
PDL_BULK_TIMEOUT = float(os.getenv("PDL_BULK_TIMEOUT", "90"))
PDL_BULK_MAX = max(1, min(100, int(os.getenv("PDL_BULK_MAX", "100"))))
# Return PDL's lowest-confidence matches as requested; the UI displays the
# provider likelihood so recruiters can judge the result themselves.
PDL_MIN_LIKELIHOOD = max(1, min(10, int(os.getenv("PDL_MIN_LIKELIHOOD", "2"))))
# A low-likelihood response can be routed to the fallback provider, but it is
# not persisted automatically as trusted contact data. Exact social-profile
# matching is stronger than name/location matching, so it uses a lower floor.
PDL_AUTO_ACCEPT_MIN_LIKELIHOOD = max(
    1, min(10, int(os.getenv("PDL_AUTO_ACCEPT_MIN_LIKELIHOOD", "6")))
)
PDL_PROFILE_ACCEPT_MIN_LIKELIHOOD = max(
    1, min(
        PDL_AUTO_ACCEPT_MIN_LIKELIHOOD,
        int(os.getenv("PDL_PROFILE_ACCEPT_MIN_LIKELIHOOD", "4")),
    )
)
# Compatibility settings for older administrator configuration files. PDL
# enrichment now always sends every available identity field in one request;
# separate per-field retries are intentionally disabled.
PDL_STAGED_RETRY_ENABLED = os.getenv(
    "PDL_STAGED_RETRY_ENABLED", "0"
).strip().lower() in ("1", "true", "yes")
PDL_STAGED_RETRY_MAX = max(
    1, min(6, int(os.getenv("PDL_STAGED_RETRY_MAX", "1")))
)
# Retained for compatibility with older .env files. It is intentionally not
# consulted: provider trust can never bypass deterministic identity validation.
PDL_TRUST_PROVIDER_MATCH = False
# Zero disables the application-level per-run cap. Provider/account billing and
# limits still apply, and cached identities continue to cost no credits.
PDL_RUN_CREDIT_LIMIT = max(0, int(os.getenv("PDL_RUN_CREDIT_LIMIT", "0")))
PDL_CACHE_TTL_SECONDS = max(
    0, int(os.getenv("PDL_CACHE_TTL_SECONDS", str(30 * 24 * 60 * 60)))
)
PDL_ENABLED = bool(PDL_API_KEY) and os.getenv(
    "PDL_ENABLED", "1"
).strip().lower() in ("1", "true", "yes")

# Which provider answers the extension's candidate contact lookups.
# ``quick_sourcer`` returns exactly what the external API delivers, with no
# identity threshold and no provider-trust gate in front of it.
# ``people_data_labs`` restores the PDL -> Enformion waterfall and its
# verification policy.
CONTACT_LOOKUP_PROVIDER = "quick_sourcer"

# ---- Quick Sourcer external contact lookup ----
# Quick Sourcer drives a real browser against public people-search sites, so an
# uncached search takes 30-90 seconds and the client timeout is generous.  The
# key is read only by the backend; the extension calls the local endpoints.
QUICK_SOURCER_BASE_URL = os.getenv(
    "QUICK_SOURCER_BASE_URL", "https://radixsol.net/api/quick-sourcer/external"
).strip().rstrip("/")
QUICK_SOURCER_API_KEY = os.getenv("QUICK_SOURCER_API_KEY", "").strip()
QUICK_SOURCER_TIMEOUT = max(10.0, float(os.getenv("QUICK_SOURCER_TIMEOUT", "150")))
QUICK_SOURCER_ENABLED = bool(
    QUICK_SOURCER_BASE_URL and QUICK_SOURCER_API_KEY
) and os.getenv("QUICK_SOURCER_ENABLED", "1").strip().lower() in ("1", "true", "yes")
# The Hub uses its shared search pool when this is disabled. Enable it only
# after at least one Hub Search Endpoint is marked Dedicated.
QUICK_SOURCER_DEDICATED_IP = os.getenv(
    "QUICK_SOURCER_DEDICATED_IP", "0"
).strip().lower() in ("1", "true", "yes")
# A repeated search costs another 30-90 second browser run, so a found record is
# reused from the local cache until it expires.
QUICK_SOURCER_CACHE_TTL_SECONDS = max(
    0, int(os.getenv("QUICK_SOURCER_CACHE_TTL_SECONDS", str(30 * 24 * 60 * 60)))
)
# Quick Sourcer records remain subject to DNC suppression. This private,
# administrator-owned switch records the separate business decision that the
# configured Quick Sourcer feed is verified and may be persisted/synced.
QUICK_SOURCER_TRUSTED_FOR_SYNC = os.getenv(
    "QUICK_SOURCER_TRUSTED_FOR_SYNC", "0"
).strip().lower() in ("1", "true", "yes")

# Demo mode returns deterministic mock enrichment when no key is set OR when
# ENFORMION_DEMO=1 — lets you run the whole product before wiring the real key.
DEMO_MODE = os.getenv("ENFORMION_DEMO", "").strip() in ("1", "true", "yes") or not ENFORMION_ENABLED

# ---- LLM (Gemini) for outreach drafting ----
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
AI_MATCH_ENABLED = os.getenv("AI_MATCH_ENABLED", "").strip().lower() in (
    "1", "true", "yes",
)
IDENTITY_MATCH_THRESHOLD = max(
    0.0, min(1.0, float(os.getenv("IDENTITY_MATCH_THRESHOLD", "0.78")))
)
IDENTITY_REVIEW_THRESHOLD = max(
    0.0, min(IDENTITY_MATCH_THRESHOLD, float(os.getenv("IDENTITY_REVIEW_THRESHOLD", "0.62")))
)
IDENTITY_AMBIGUITY_MARGIN = max(
    0.0, min(0.5, float(os.getenv("IDENTITY_AMBIGUITY_MARGIN", "0.08")))
)
IDENTITY_RESOLUTION_ENABLED = os.getenv(
    "IDENTITY_RESOLUTION_ENABLED", "1"
).strip().lower() in ("1", "true", "yes")
CONTACT_FRESHNESS_SECONDS = max(
    3600, int(os.getenv("CONTACT_FRESHNESS_SECONDS", str(90 * 24 * 60 * 60)))
)

# NPPES is public corroborating evidence for healthcare profiles. It is never
# treated as contact data and does not prove an active state license.
NPI_ENABLED = os.getenv("NPI_ENABLED", "1").strip().lower() in ("1", "true", "yes")
NPI_BASE_URL = os.getenv(
    "NPI_BASE_URL", "https://npiregistry.cms.hhs.gov/api/"
).strip()
NPI_TIMEOUT = max(3.0, float(os.getenv("NPI_TIMEOUT", "12")))

# ---- Optional deliverability / phone-line validation ----
VERIFY_EMAILS = os.getenv("VERIFY_EMAILS", "").strip().lower() in ("1", "true", "yes")
NEVERBOUNCE_API_KEY = os.getenv("NEVERBOUNCE_API_KEY", "")
VERIFY_PHONES = os.getenv("VERIFY_PHONES", "").strip().lower() in ("1", "true", "yes")
# ---- Candidate SMS (Twilio) ----
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "twilio").strip().casefold() or "twilio"
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_API_KEY = os.getenv("TWILIO_API_KEY", "").strip()
TWILIO_API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET", "").strip()
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
TWILIO_WEBHOOK_URL = os.getenv("TWILIO_WEBHOOK_URL", "").strip()
TWILIO_SMS_TIMEOUT = max(3.0, min(60.0, float(os.getenv("TWILIO_SMS_TIMEOUT", "20"))))
TWILIO_SMS_ENABLED = bool(
    os.getenv("TWILIO_SMS_ENABLED", "0").strip().lower() in ("1", "true", "yes")
    and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER
)

# CEIPAL Candidate Pass API. Halo controls which recruiters route enriched
# candidates here; credentials and tenant endpoint stay server-side.
CEIPAL_SYNC_ENABLED = os.getenv("CEIPAL_SYNC_ENABLED", "0").strip().lower() in (
    "1", "true", "yes",
)
CEIPAL_AUTH_URL = os.getenv(
    "CEIPAL_AUTH_URL", "https://api.ceipal.com/v1/createAuthtoken/",
).strip()
CEIPAL_EMAIL = os.getenv("CEIPAL_EMAIL", "").strip()
CEIPAL_PASSWORD = os.getenv("CEIPAL_PASSWORD", "").strip()
CEIPAL_API_KEY = os.getenv("CEIPAL_API_KEY", "").strip()
CEIPAL_CANDIDATE_URL = os.getenv("CEIPAL_CANDIDATE_URL", "").strip()
CEIPAL_TIMEOUT = max(5.0, min(120.0, float(os.getenv("CEIPAL_TIMEOUT", "30"))))
CEIPAL_WORKER_INTERVAL_SECONDS = max(
    0.5, float(os.getenv("CEIPAL_WORKER_INTERVAL_SECONDS", "2"))
)
CEIPAL_MAX_ATTEMPTS = max(1, min(20, int(os.getenv("CEIPAL_MAX_ATTEMPTS", "8"))))
CEIPAL_CONFIGURED = bool(
    CEIPAL_SYNC_ENABLED and CEIPAL_AUTH_URL and CEIPAL_EMAIL and CEIPAL_PASSWORD
    and CEIPAL_API_KEY and CEIPAL_CANDIDATE_URL
)
DEFAULT_PHONE_COUNTRY = os.getenv("DEFAULT_PHONE_COUNTRY", "US").strip() or "US"
_DEFAULT_SMS_REPLY_NOTIFICATION_EMAILS = (
    "ricky.singh@radixsol.com",
    "sonali.singh@radixsol.com",
    "ashwin.b@radixsol.com",
)
SMS_REPLY_NOTIFICATION_EMAILS = tuple(dict.fromkeys(
    email.strip().casefold()
    for email in re.split(
        r"[,;]",
        os.getenv("SMS_REPLY_NOTIFICATION_EMAILS", "").strip()
        or ",".join(_DEFAULT_SMS_REPLY_NOTIFICATION_EMAILS),
    )
    if email.strip()
))

# ---- Watcher email notifications (SendGrid) ----
# Recipients are administrator-controlled and never supplied by the browser.
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "").strip()
EMAIL_FROM = os.getenv("EMAIL_FROM", "").strip()
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Medhunt").strip() or "Medhunt"
SENDGRID_TIMEOUT = max(3.0, min(60.0, float(os.getenv("SENDGRID_TIMEOUT", "15"))))
WATCHER_NOTIFICATION_EMAILS = tuple(dict.fromkeys(
    email.strip().casefold()
    for email in re.split(r"[,;]", os.getenv("WATCHER_NOTIFICATION_EMAILS", ""))
    if email.strip()
))
WATCHER_EMAIL_NOTIFICATIONS_ENABLED = os.getenv(
    "WATCHER_EMAIL_NOTIFICATIONS_ENABLED", "0"
).strip().lower() in ("1", "true", "yes")

# ---- Storage ----
DATABASE_BACKEND = os.getenv("DATABASE_BACKEND", "").strip().lower()
DATABASE_NAME = os.getenv("DATABASE_NAME", "").strip()
DATABASE_URL = (
    ""
    if DATABASE_BACKEND == "sqlite"
    else os.getenv("DATABASE_URL", "").strip()
)
if DATABASE_URL and DATABASE_NAME:
    if not re.fullmatch(r"[A-Za-z0-9_]+", DATABASE_NAME):
        raise RuntimeError("DATABASE_NAME may contain only letters, numbers, and underscores.")
    parsed_database_url = urlsplit(DATABASE_URL)
    DATABASE_URL = urlunsplit(parsed_database_url._replace(path=f"/{quote(DATABASE_NAME)}"))
DATABASE_SCHEMA = os.getenv("MEDHUNT_DATABASE_SCHEMA", "medhunt").strip() or "medhunt"
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", DATABASE_SCHEMA):
    raise RuntimeError(
        "MEDHUNT_DATABASE_SCHEMA must be a valid PostgreSQL identifier."
    )
_db_path = Path(os.getenv("SOURCING_DB", "sourcing.db")).expanduser()
if _USES_APP_HOME_LAYOUT and not _db_path.is_absolute():
    _db_path = DATA_DIR / _db_path
DB_PATH = str(_db_path.resolve()) if _db_path.is_absolute() else str(_db_path)
LOG_PATH = str((LOG_DIR / "backend.log").resolve())
DATABASE_CONNECT_TIMEOUT = max(3, int(os.getenv("DATABASE_CONNECT_TIMEOUT", "10")))
RESUME_DOWNLOAD_DIR = Path(
    os.getenv("RESUME_DOWNLOAD_DIR", str(Path.home() / "Downloads"))
).resolve()
RESUME_MAX_BYTES = int(os.getenv("RESUME_MAX_BYTES", str(15 * 1024 * 1024)))
RESUME_OCR_ENABLED = os.getenv("RESUME_OCR_ENABLED", "1").strip().lower() in (
    "1", "true", "yes",
)
RESUME_OCR_TESSERACT_CMD = os.getenv("RESUME_OCR_TESSERACT_CMD", "").strip()
RESUME_OCR_LANGUAGE = os.getenv("RESUME_OCR_LANGUAGE", "eng").strip() or "eng"
RESUME_OCR_MAX_PAGES = max(
    1, min(50, int(os.getenv("RESUME_OCR_MAX_PAGES", "20")))
)
RESUME_OCR_MIN_PAGE_CHARS = max(
    10, min(500, int(os.getenv("RESUME_OCR_MIN_PAGE_CHARS", "60")))
)
RESUME_OCR_DPI = max(120, min(300, int(os.getenv("RESUME_OCR_DPI", "200"))))
RESUME_OCR_PAGE_TIMEOUT = max(
    3.0, min(60.0, float(os.getenv("RESUME_OCR_PAGE_TIMEOUT", "20")))
)
RESUME_OCR_TOTAL_TIMEOUT = max(
    5.0, min(120.0, float(os.getenv("RESUME_OCR_TOTAL_TIMEOUT", "35")))
)
RESUME_OCR_ACCEPT_CONFIDENCE = max(
    0.75, min(1.0, float(os.getenv("RESUME_OCR_ACCEPT_CONFIDENCE", "0.84")))
)
RESUME_OCR_ROLE_CONFIDENCE = max(
    0.70, min(1.0, float(os.getenv("RESUME_OCR_ROLE_CONFIDENCE", "0.80")))
)

# Resume object storage. R2 exposes an S3-compatible API; keep this disabled
# until S3_BUCKET points at a bucket dedicated to this application.
STORAGE_ENABLED = os.getenv("STORAGE_ENABLED", "").strip().lower() in (
    "1", "true", "yes",
)
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").strip()
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "").strip()
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "").strip()
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_REGION = os.getenv("S3_REGION", "auto").strip() or "auto"
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "").strip().rstrip("/")

# ---- Optional LaborEdge Nexus synchronization ----
# Nexus is invoked only by the local backend after a trusted contact and its
# resume have been committed. No credential or provider workflow is exposed to
# the browser extension. Tenant-specific routing belongs in DEFAULT_PROFILE.
NEXUS_BASE_URL = os.getenv(
    "NEXUS_BASE_URL", "https://api-nexus.laboredge.com"
).strip().rstrip("/")
NEXUS_AUTH_METHOD = os.getenv("NEXUS_AUTH_METHOD", "static").strip().lower()
NEXUS_TOKEN_URL = os.getenv("NEXUS_TOKEN_URL", "").strip()
NEXUS_TOKEN_PAYLOAD_STYLE = os.getenv(
    "NEXUS_TOKEN_PAYLOAD_STYLE", "form"
).strip().lower()
NEXUS_CLIENT_ID = os.getenv("NEXUS_CLIENT_ID", "").strip()
NEXUS_CLIENT_SECRET = os.getenv("NEXUS_CLIENT_SECRET", "").strip()
NEXUS_USERNAME = os.getenv("NEXUS_USERNAME", "").strip()
NEXUS_PASSWORD = os.getenv("NEXUS_PASSWORD", "").strip()
NEXUS_STATIC_TOKEN = os.getenv("NEXUS_STATIC_TOKEN", "").strip()
NEXUS_TOKEN_BASIC = os.getenv("NEXUS_TOKEN_BASIC", "").strip()
NEXUS_ORG_CODE = os.getenv("NEXUS_ORG_CODE", "").strip()
NEXUS_RESUME_DOC_TYPE_ID = os.getenv("NEXUS_RESUME_DOC_TYPE_ID", "").strip()
NEXUS_TIMEOUT = max(5.0, float(os.getenv("NEXUS_TIMEOUT", "30")))
NEXUS_CONNECT_TIMEOUT = max(1.0, float(os.getenv("NEXUS_CONNECT_TIMEOUT", "15")))
NEXUS_MASTER_CACHE_SECONDS = max(
    0, int(os.getenv("NEXUS_MASTER_CACHE_SECONDS", "600"))
)
NEXUS_MAX_RESUME_BYTES = max(
    1, int(os.getenv("NEXUS_MAX_RESUME_BYTES", str(10 * 1024 * 1024)))
)
NEXUS_WORKER_INTERVAL_SECONDS = max(
    0.5, float(os.getenv("NEXUS_WORKER_INTERVAL_SECONDS", "2"))
)
NEXUS_MAX_ATTEMPTS = max(1, min(20, int(os.getenv("NEXUS_MAX_ATTEMPTS", "8"))))
NEXUS_LEASE_SECONDS = max(
    120.0,
    float(
        os.getenv(
            "NEXUS_LEASE_SECONDS",
            str(max(600.0, NEXUS_TIMEOUT * 12)),
        )
    ),
)
_NEXUS_DEFAULT_PROFILE_ERROR = ""
try:
    NEXUS_DEFAULT_PROFILE = json.loads(os.getenv("NEXUS_DEFAULT_PROFILE", "") or "{}")
    if not isinstance(NEXUS_DEFAULT_PROFILE, dict):
        _NEXUS_DEFAULT_PROFILE_ERROR = "invalid_default_profile"
        NEXUS_DEFAULT_PROFILE = {}
except (TypeError, ValueError):
    _NEXUS_DEFAULT_PROFILE_ERROR = "invalid_default_profile"
    NEXUS_DEFAULT_PROFILE = {}

_NEXUS_AUTH_CONFIGURED = bool(
    (NEXUS_AUTH_METHOD == "static" and NEXUS_STATIC_TOKEN)
    or (
        NEXUS_AUTH_METHOD == "password" and NEXUS_TOKEN_URL
        and NEXUS_USERNAME and NEXUS_PASSWORD
        and (
            (NEXUS_CLIENT_ID and NEXUS_CLIENT_SECRET)
            or NEXUS_TOKEN_BASIC
        )
    )
    or (
        NEXUS_AUTH_METHOD == "client_credentials" and NEXUS_TOKEN_URL
        and (
            (NEXUS_CLIENT_ID and NEXUS_CLIENT_SECRET)
            or NEXUS_TOKEN_BASIC
        )
    )
)
NEXUS_SYNC_REQUESTED = (
    os.getenv("NEXUS_SYNC_ENABLED", "0").strip().lower() in ("1", "true", "yes")
)
if not NEXUS_SYNC_REQUESTED:
    NEXUS_DISABLED_REASON = "not_requested"
elif _NEXUS_DEFAULT_PROFILE_ERROR:
    NEXUS_DISABLED_REASON = _NEXUS_DEFAULT_PROFILE_ERROR
elif not NEXUS_BASE_URL:
    NEXUS_DISABLED_REASON = "api_address_missing"
elif not _NEXUS_AUTH_CONFIGURED:
    NEXUS_DISABLED_REASON = "authentication_not_configured"
elif not NEXUS_DEFAULT_PROFILE.get("jobTypeIds"):
    NEXUS_DISABLED_REASON = "tenant_job_types_missing"
elif not (
    NEXUS_DEFAULT_PROFILE.get("referralSourceId")
    or NEXUS_DEFAULT_PROFILE.get("referralSourceName")
):
    NEXUS_DISABLED_REASON = "tenant_referral_source_missing"
else:
    NEXUS_DISABLED_REASON = ""
NEXUS_SYNC_ENABLED = bool(NEXUS_SYNC_REQUESTED and not NEXUS_DISABLED_REASON)

# ---- Compliance defaults (baked into the workflow) ----
# Default outreach channel; Medhunt only drafts email outreach.
DEFAULT_CHANNEL = "email"
REQUIRE_HUMAN_APPROVAL = True   # nothing sends automatically
HONOR_DNC = True                # do-not-contact / opt-out list is always enforced

COMPLIANCE_NOTICE = (
    "Contact data may come from licensed enrichment providers. Use for legitimate "
    "recruiting outreach only. Email sends must comply with CAN-SPAM (identify sender, "
    "honor opt-outs). This tool requires human approval before sending and enforces a "
    "do-not-contact list."
)
