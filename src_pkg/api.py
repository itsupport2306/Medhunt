"""
Medhunt Sourcing Assistant — FastAPI backend.

Flow: add candidate names you're entitled to work with (manual / CSV / paste) →
enrich via Enformion → rank vs a job → draft outreach (human-approved) → pipeline.

Run:  uvicorn api:app --port 8091     then open http://localhost:8091
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import sys
import time
import uuid
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from sourcing import (
    store, intake, ranking, outreach, config, storage,
    resume_enrichment, contact_access,
    person_name, phone_policy, quick_sourcer_client,
    nexus_delivery, ceipal_delivery, watcher_notifications, healthboard_auth,
    profile_resume, analytics_delivery, twilio_sms, zoom_sms, sms_notifications,
    verification,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Database schema preparation and provider clients are already lazy inside
    # their respective stores. Do not contact any remote dependency before the
    # localhost API becomes ready: a slow pooled database connection or
    # provider probe must not make every browser-extension action unavailable.
    if config.NEXUS_SYNC_REQUESTED and not config.NEXUS_SYNC_ENABLED:
        logging.getLogger("medhunt.nexus").error(
            "Candidate synchronization was requested but is disabled (%s).",
            config.NEXUS_DISABLED_REASON,
        )
    nexus_delivery.start()
    ceipal_delivery.start()
    analytics_delivery.start()
    try:
        yield
    finally:
        analytics_delivery.stop()
        ceipal_delivery.stop()
        nexus_delivery.stop()


APP_VERSION = "3.26.27"

app = FastAPI(
    title="Medhunt Sourcing Assistant",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.EXTENSION_ALLOWED_ORIGINS),
    allow_origin_regex=(
        r"^(?:chrome-extension://[a-p]{32}|moz-extension://[A-Za-z0-9-]+)$"
        if config.EXTENSION_ALLOW_UNLISTED_ORIGINS else None
    ),
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=[
        "Content-Type", "X-Medhunt-Token", "X-HealthBoard-Extension-Token",
        "X-Medhunt-Device-ID",
    ],
)


_PUBLIC_LOCAL_PATHS = frozenset({
    '/integrations/twilio/webhook',
    '/integrations/zoom/webhook',
    "/", "/app.js", "/styles.css", "/privacy", "/health", "/auth/config",
    "/auth/request-code", "/auth/verify-code", "/auth/device-status",
})


def _is_same_loopback_origin(request: Request, origin: str) -> bool:
    """Return whether ``origin`` is the exact loopback origin serving the UI."""
    try:
        parsed = urlsplit(origin)
        origin_host = (parsed.hostname or "").casefold().rstrip(".")
        request_host = (request.url.hostname or "").casefold().rstrip(".")
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or origin_host not in loopback_hosts
            or request_host not in loopback_hosts
            or origin_host != request_host
            or parsed.scheme != request.url.scheme
        ):
            return False
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
        return origin_port == request_port
    except ValueError:
        return False


@app.middleware("http")
async def authenticate_local_api_requests(request: Request, call_next):
    """Authenticate extension API calls with HealthBoard or the local token."""
    origin = request.headers.get("origin", "").strip()
    protected_cross_origin = bool(
        origin and not _is_same_loopback_origin(request, origin)
    )
    if origin.startswith(("chrome-extension://", "moz-extension://")):
        permitted_origin = (
            origin.rstrip("/") in config.EXTENSION_ALLOWED_ORIGINS
            or (
                config.EXTENSION_ALLOW_UNLISTED_ORIGINS
                and re.fullmatch(
                    r"(?:chrome-extension://[a-p]{32}|moz-extension://[A-Za-z0-9-]+)",
                    origin.rstrip("/"),
                )
            )
        )
        if not permitted_origin:
            return JSONResponse(
                {"detail": "This extension installation is not authorized."},
                status_code=403,
            )
    request.state.user = None
    request.state.healthboard_extension_token = ""
    if request.url.path.startswith("/internal/halo/"):
        supplied = request.headers.get("x-medhunt-service-token", "")
        if not config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN or not hmac.compare_digest(
            supplied, config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN
        ):
            return JSONResponse({"detail": "Invalid Halo service token."}, status_code=401)
        request.state.halo_service = True
        return await call_next(request)
    if (
        healthboard_auth.enabled()
        and request.method.upper() != "OPTIONS"
        and request.url.path not in _PUBLIC_LOCAL_PATHS
    ):
        supplied = request.headers.get("x-healthboard-extension-token", "").strip()
        if not supplied:
            return JSONResponse({"detail": "Healthcareboard sign-in is required."}, status_code=401)
        try:
            identity = healthboard_auth.verify_extension_token(supplied)
            subject = str(identity.get("user_id") or "")
        except Exception:
            return JSONResponse({"detail": "Invalid Healthcareboard extension session."}, status_code=401)
        installation_id = request.headers.get("x-medhunt-device-id", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", installation_id):
            return JSONResponse(
                {"detail": "This Medhunt installation is not registered. Update the extension and sign in again."},
                status_code=403,
            )
        device = store.authorize_extension_device(subject, installation_id)
        if not device:
            return JSONResponse(
                {"detail": "This Medhunt installation is not registered. Sign in to request access."},
                status_code=403,
            )
        if device.get("status") != "approved":
            messages = {
                "pending": "This device is waiting for approval from a Healthcareboard administrator.",
                "expired": "This device registration expired after extended inactivity. Sign in again to restore access.",
                "revoked": "Access for this device was revoked. Contact your administrator if this was unexpected.",
            }
            return JSONResponse(
                {"detail": messages.get(str(device.get("status")), "This device is not approved.")},
                status_code=403,
            )
        try:
            request.state.user = {
                "sub": subject,
                "email": str(identity.get("email") or ""),
                "role": str(identity.get("role") or ""),
                "delivery_targets": dict(identity.get("delivery_targets") or {}),
            }
            request.state.healthboard_extension_token = supplied
        except Exception:
            return JSONResponse({"detail": "Invalid Healthcareboard extension session."}, status_code=401)
    if (
        config.LOCAL_API_TOKEN
        and request.method.upper() != "OPTIONS"
        and request.url.path not in _PUBLIC_LOCAL_PATHS
        and protected_cross_origin
    ):
        supplied = request.headers.get("x-medhunt-token", "")
        if not hmac.compare_digest(supplied, config.LOCAL_API_TOKEN):
            return JSONResponse({"detail": "Unauthorized local API request."}, status_code=401)
        request.state.user = request.state.user or {"sub": "local"}
    return await call_next(request)


def _request_user(request: Request | None) -> dict:
    """Return the verified Healthcareboard identity, or a local-dev identity."""
    user = getattr(getattr(request, "state", None), "user", None)
    if user:
        return user
    if healthboard_auth.enabled():
        raise HTTPException(401, "Authentication required.")
    return {"sub": "local", "name": "Local Medhunt", "email": ""}

_RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
_FRONTEND_DIR = _RESOURCE_ROOT / "frontend"


class JobIn(BaseModel):
    title: str
    location: str = ""
    description: str = ""

class IntakeIn(BaseModel):
    text: str
    job_id: int | None = None


class ProfileImportIn(BaseModel):
    name: str
    location: str = ""
    # Facebook exposes current city ("Lives in") and hometown ("From") as
    # separate facts.  The database keeps the current city as the candidate's
    # location; hometown is retained as source context for a controlled
    # second-pass contact lookup only.
    hometown: str = ""
    headline: str = ""
    # Specialty labels are captured when a source platform exposes them. They
    # are persisted as bounded, explicit evidence lines so the Nexus delivery
    # worker can resolve the exact tenant specialty master record later.
    specialties: list[str] = Field(default_factory=list, max_length=20)
    # Adapters may send structured, source-visible context in addition to the
    # raw notes.  The server serializes these into canonical evidence tags so
    # every provider/verification path consumes the same bounded facts.
    roles: list[str] = Field(default_factory=list, max_length=20)
    employers: list[str] = Field(default_factory=list, max_length=20)
    schools: list[str] = Field(default_factory=list, max_length=20)
    alternate_names: list[str] = Field(default_factory=list, max_length=10)
    notes: str = ""
    source: str = "indeed"
    source_url: str = ""
    source_id: str = ""
    job_id: int | None = None


class ProfileBatchImportIn(BaseModel):
    profiles: list[ProfileImportIn]
    job_id: int | None = None
    search_url: str = ""


class ResumeDownloadIn(BaseModel):
    path: str
    filename: str = ""


class ResumeCaptureIn(BaseModel):
    content_base64: str
    filename: str = "resume.pdf"


class ProfessionalProfileResumeIn(BaseModel):
    """Bounded public-directory facts used to generate an attributed PDF."""
    kind: str = Field(pattern=r"^public_professional_profile$")
    source_label: str = Field(default="U.S. News Doctor Finder", max_length=100)
    source_url: str = Field(default="", max_length=2000)
    headline: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=4000)
    credentials: list[str] = Field(default_factory=list, max_length=12)
    specialties: list[str] = Field(default_factory=list, max_length=20)
    subspecialties: list[str] = Field(default_factory=list, max_length=20)
    hospitals: list[str] = Field(default_factory=list, max_length=40)
    education: list[str] = Field(default_factory=list, max_length=40)
    certifications: list[str] = Field(default_factory=list, max_length=40)
    licenses: list[str] = Field(default_factory=list, max_length=75)
    languages: list[str] = Field(default_factory=list, max_length=20)
    years_experience: str = Field(default="", max_length=60)
    npi: str = Field(default="", max_length=30)
    address: str = Field(default="", max_length=500)
    location: str = Field(default="", max_length=300)


class HealthBoardCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class HealthBoardCodeVerify(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")
    challenge: str = Field(min_length=40, max_length=4096)
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{40,64}$")
    device_name: str = Field(default="Medhunt browser", max_length=200)


class DeviceRevokeIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class PdlBatchEnrichIn(BaseModel):
    candidate_ids: list[int] = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=8, max_length=80)
    # Fresh paid fallback is opt-in for every request. Cached Enformion results
    # remain available without this consent because they consume no credit.
    allow_enformion: bool = False
    max_enformion_calls: int | None = Field(default=None, ge=0, le=100)


class ContactLookupBatchIn(BaseModel):
    """Small vendor-neutral contract used by the browser extension."""
    candidate_ids: list[int] = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=8, max_length=80)
    confirmed: bool = False


class QuickSourcerFindIn(BaseModel):
    """One person lookup requested from a captured or stored profile."""
    name: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=200)
    candidate_id: int | None = None
    refresh: bool = False


class IdentityReviewIn(BaseModel):
    decision: str
    canonical_name: str = ""
    note: str = ""


class OutreachIn(BaseModel):
    candidate_id: int
    job_id: int | None = None
    channel: str | None = None

class StageIn(BaseModel):
    stage: str


class PoolAssignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    candidate_ids: list[int] = Field(min_length=1, max_length=100)


class CampaignCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    candidate_ids: list[int] = Field(min_length=1, max_length=100)
    job_id: int | None = None


class OutreachBatchIn(BaseModel):
    candidate_ids: list[int] = Field(min_length=1, max_length=100)
    job_id: int | None = None
    channel: str = "email"


class SmsSendIn(BaseModel):
    candidate_id: int
    phone: str = Field(min_length=7, max_length=50)
    message: str = Field(min_length=1, max_length=1600)
    request_id: str = Field(default='', max_length=100)
    provider: str = Field(default='twilio', pattern=r'^(twilio|zoom)$')


class DncIn(BaseModel):
    value: str
    reason: str = ""


class WatcherResumeProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location: str = Field(default="", max_length=500)
    headline: str = Field(default="", max_length=500)
    source_url: str = Field(default="", max_length=2000)
    source_id: str = Field(default="", max_length=500)
    resume_marker: str = Field(default="", max_length=500)
    change: str = Field(pattern=r"^(new|updated)$")


class WatcherResumeNotificationIn(BaseModel):
    event_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    watch_key: str = Field(default="", max_length=128)
    query: str = Field(default="", max_length=2000)
    location: str = Field(default="", max_length=300)
    detected_at: str = Field(default="", max_length=64)
    profiles: list[WatcherResumeProfileIn] = Field(min_length=1, max_length=100)


def _public_lookup_result(result: dict) -> dict:
    """Return the intentionally small, vendor-neutral lookup contract.

    Provider responses, matching evidence, scores, policy decisions, costs, and
    diagnostics stay inside the backend.  Contact values cross this boundary
    only after the internal orchestration has explicitly marked them trusted.
    """
    source = dict(result or {})
    verification_record = dict(source.get("verification") or {})
    verification_evidence = dict(verification_record.get("evidence") or {})
    contact = dict(
        source.get("contact_verification")
        or verification_evidence.get("contact_verification")
        or {}
    )
    internal_status = str(
        source.get("status") or source.get("enrich_status") or "error"
    ).strip().lower()
    trusted = bool(
        internal_status == "success"
        and (
            source.get("contacts_trusted") is True
            or contact.get("automatic_use_allowed") is True
        )
    )
    emails = list(source.get("emails") or [])[:20] if trusted else []
    # Trust booleans authorize the record, not arbitrary values inside it.
    # Reapply per-number provenance at the final browser boundary so stale or
    # inconsistent cached landlines fail closed instead of reaching a card.
    phone_details = phone_policy.preferred_phone_details(source) if trusted else []
    latest_phone = phone_policy.latest_phone_detail(source) if trusted else None
    latest_wireless = (
        latest_phone.get("value")
        if latest_phone and latest_phone.get("kind") == "mobile" else ""
    )
    if latest_wireless:
        phone_details = sorted(
            phone_details,
            key=lambda item: item.get("value") != latest_wireless,
        )
    phones = [item["value"] for item in phone_details]
    found = bool(trusted and (emails or phones))
    failed = internal_status in {"error", "disabled", "budget_exhausted"}
    internal_location_match = source.get("matched_location_context") or {}
    location_match = None
    if (
        found
        and isinstance(internal_location_match, dict)
        and internal_location_match.get("type") == "hometown"
    ):
        matched_location = " ".join(
            str(internal_location_match.get("value") or "").split()
        )[:160]
        if matched_location:
            location_match = {"type": "hometown", "value": matched_location}
    return {
        "status": "found" if found else ("failed" if failed else "not_found"),
        "emails": emails if found else [],
        "phones": phones if found else [],
        "phone_contacts": [
            {"value": item["value"], "kind": item["kind"]}
            for item in phone_details
        ] if found else [],
        "latest_phone": latest_wireless if found else "",
        # One approved usable contact channel is enough to retain the matched
        # Indeed resume. Masked provider text is not exposed as an email, but a
        # valid phone beside that text still makes the result eligible.
        "resume_required": bool(found and (emails or phones)),
        # This is deliberately vendor-neutral. It is emitted only when a
        # trusted contact was accepted on the Facebook "From" retry.
        "location_match": location_match,
    }


def _public_candidate(candidate: dict | None) -> dict:
    projected = contact_access.project_candidate(candidate)
    latest_phone = phone_policy.latest_phone_detail(projected)
    latest_wireless = (
        latest_phone.get("value")
        if latest_phone and latest_phone.get("kind") == "mobile" else ""
    )
    phone_details = phone_policy.preferred_phone_details(projected)
    if latest_wireless:
        phone_details.sort(key=lambda item: item.get("value") != latest_wireless)
        projected["phones"] = [item["value"] for item in phone_details]
        projected["phone_contacts"] = [
            {"value": item["value"], "kind": item["kind"]}
            for item in phone_details
        ]
    allowed = (
        "id", "name", "location", "job_id", "stage", "fit_score",
        "emails", "phones", "phone_contacts", "addresses", "enrich_status",
        "latest_phone", "notes", "created", "updated", "identity_status",
    )
    public = {key: projected.get(key) for key in allowed if key in projected}
    public["latest_phone"] = latest_wireless
    public["records_available"] = bool(
        str(projected.get("contact_source") or "") == "quick_sourcer"
    )
    return public


def _public_resume(resume: dict | None) -> dict:
    """Expose only fields the side panel needs to retrieve a stored resume."""
    source = resume or {}
    allowed = (
        "id", "candidate_id", "filename", "mime_type", "size", "created",
        "deduplicated", "contact_sheet_embedded", "contacts_saved",
    )
    return {key: source.get(key) for key in allowed if key in source}


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse(config.MEDHUNT_MAIN_WEBSITE_URL, status_code=302)


@app.get("/styles.css", include_in_schema=False)
def styles():
    return FileResponse(str(_FRONTEND_DIR / "styles.css"), media_type="text/css")


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_notice():
    return FileResponse(str(_FRONTEND_DIR / "privacy.html"), media_type="text/html")


@app.get("/app.js", include_in_schema=False)
def frontend_script():
    return FileResponse(
        str(_FRONTEND_DIR / "app.js"),
        media_type="application/javascript",
    )


@app.get("/health")
def health():
    record_status = quick_sourcer_client.status()
    return {
        "status": "ok",
        "service": "medhunt-api",
        "version": APP_VERSION,
        # Browser-facing capability flags remain vendor-neutral. Endpoints,
        # credentials, and orchestration names stay inside the backend.
        "records_lookup": {
            "enabled": bool(record_status.get("enabled")),
            "typical_seconds": list(record_status.get("typical_seconds") or []),
        },
        "lookup_behavior": (
            "sequential" if _quick_sourcer_selected() else "bulk"
        ),
    }


@app.get("/auth/config")
def auth_config():
    """Tell the extension whether Healthcareboard email-code login is enabled."""
    return {
        "enabled": healthboard_auth.enabled(),
        "provider": "healthboard",
    }


def _request_client_ip(request: Request) -> str:
    """Preserve the original browser IP for HealthBoard's auth rate limit."""
    forwarded = request.headers.get("x-forwarded-for", "")
    value = forwarded.split(",")[0].strip() if forwarded else ""
    if not value and request.client:
        value = request.client.host
    return value[:64]


@app.post("/auth/request-code")
def request_healthboard_code(body: HealthBoardCodeRequest, request: Request):
    if not healthboard_auth.enabled():
        raise HTTPException(503, "Healthcareboard login is not configured.")
    try:
        return healthboard_auth.request_code(
            body.email.strip().lower(), client_ip=_request_client_ip(request),
        )
    except Exception as exc:
        logging.getLogger("medhunt.auth").warning(
            "Healthcareboard code request failed (%s).", type(exc).__name__,
        )
        raise HTTPException(503, "The sign-in code service is temporarily unavailable.") from exc


@app.post("/auth/verify-code")
def verify_healthboard_code(body: HealthBoardCodeVerify, request: Request):
    if not healthboard_auth.enabled():
        raise HTTPException(503, "Healthcareboard login is not configured.")
    try:
        verified = healthboard_auth.verify_code(
            body.email.strip().lower(), body.code, body.challenge,
            client_ip=_request_client_ip(request),
        )
    except Exception as exc:
        logging.getLogger("medhunt.auth").info(
            "Healthcareboard code verification was rejected (%s).", type(exc).__name__,
        )
        raise HTTPException(401, "Invalid or expired sign-in code.") from exc
    identity = verified.get("user") or {}
    subject = str(identity.get("user_id") or "")
    if not subject:
        raise HTTPException(502, "Healthcareboard did not return a valid user identity.")
    store.upsert_user(
        subject,
        email=str(identity.get("email") or ""),
        name=str(identity.get("name") or ""),
    )
    device = store.register_extension_device(
        subject,
        body.device_id,
        device_name=body.device_name,
        user_agent=request.headers.get("user-agent", ""),
    )
    verified = dict(verified)
    verified["device"] = device
    verified["device_approval_required"] = device.get("status") != "approved"
    return verified


def _admin_identity(user: dict) -> bool:
    role = re.sub(r"[\s-]+", "_", str(user.get("role") or "").strip().casefold())
    email = str(user.get("email") or "").strip().casefold()
    return role in {
        "admin", "administrator", "super_admin", "owner",
        "org_admin", "orgadmin", "org_administrator",
        "organization_admin", "organizationadmin", "organization_administrator",
        "medhunt_admin", "medhuntadmin", "medhunt_administrator",
        "platform_admin", "platformadmin", "platform_administrator",
    } or email in config.MEDHUNT_ADMIN_EMAILS


@app.get("/auth/device-status")
def auth_device_status(request: Request):
    """Allow a signed-in pending installation to finish auto-approval."""
    token = request.headers.get("x-healthboard-extension-token", "").strip()
    installation_id = request.headers.get("x-medhunt-device-id", "").strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", installation_id):
        raise HTTPException(401, "A valid Medhunt device session is required.")
    try:
        identity = healthboard_auth.verify_extension_token(token)
    except Exception as exc:
        raise HTTPException(401, "Invalid Healthcareboard extension session.") from exc
    subject = str(identity.get("user_id") or "")
    device = store.auto_approve_pending_extension_device(subject, installation_id)
    if not device:
        raise HTTPException(404, "This device request no longer exists.")
    return {
        "device": device,
        "approved": device.get("status") == "approved",
        "user": {
            "user_id": subject,
            "email": str(identity.get("email") or ""),
            "name": str(identity.get("name") or ""),
            "role": str(identity.get("role") or ""),
        },
    }


@app.get("/auth/devices")
def auth_devices(request: Request):
    user = _request_user(request)
    subject = str(user.get("sub") or "")
    current = request.headers.get("x-medhunt-device-id", "").strip()
    devices = store.list_extension_devices(
        subject,
        current_installation_id=current,
        include_all_users=False,
    )
    multi_device_accounts = []
    if _admin_identity(user):
        owners = {}
        for device in devices:
            owner_id = str(device.get("user_id") or "")
            entry = owners.setdefault(owner_id, {
                "user_id": owner_id,
                "email": str(device.get("user_email") or ""),
                "name": str(device.get("user_name") or ""),
                "device_count": int(device.get("approved_device_count") or 0),
            })
            entry["device_count"] = int(
                device.get("approved_device_count") or entry["device_count"]
            )
        multi_device_accounts = [
            account for account in owners.values()
            if account["device_count"] > 1
        ]
    return {
        "items": devices,
        "multi_device_accounts": multi_device_accounts,
        "is_admin": _admin_identity(user),
    }


@app.post("/auth/devices/{device_id}/approve")
def approve_auth_device(device_id: int, request: Request):
    _request_user(request)
    raise HTTPException(410, "Approve extension devices from the Halo admin website.")


@app.post("/auth/devices/{device_id}/revoke")
def revoke_auth_device(device_id: int, body: DeviceRevokeIn, request: Request):
    user = _request_user(request)
    try:
        device = store.revoke_extension_device(
            device_id,
            actor_user_id=str(user.get("sub") or ""),
            actor_is_admin=_admin_identity(user),
            reason=body.reason,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"device": device}


class HaloScopeIn(BaseModel):
    user_ids: list[str] = Field(default_factory=list, max_length=1000)
    all_users: bool = False


class HaloDeviceActionIn(HaloScopeIn):
    actor_user_id: str = Field(min_length=1, max_length=80)


class HaloConversationAssignmentIn(HaloScopeIn):
    actor_user_id: str = Field(min_length=1, max_length=80)
    recruiter_user_id: str = Field(min_length=1, max_length=80)
    recruiter_email: str = Field(default='', max_length=320)
    recruiter_name: str = Field(default='', max_length=320)


class HaloConversationReplyIn(HaloScopeIn):
    actor_user_id: str = Field(min_length=1, max_length=80)
    actor_name: str = Field(default='', max_length=320)
    message: str = Field(min_length=1, max_length=500)
    sender_number: str = Field(min_length=7, max_length=40)
    zoom_user_id: str = Field(min_length=1, max_length=80)
    request_id: str = Field(default='', max_length=120)


def _halo_scoped(record: dict, scope: HaloScopeIn) -> bool:
    return scope.all_users or str(record.get("user_id") or "") in set(scope.user_ids)


@app.post("/internal/halo/devices")
def halo_devices(scope: HaloScopeIn):
    devices = store.list_extension_devices("", include_all_users=True)
    items = [device for device in devices if _halo_scoped(device, scope)]
    multiple = {}
    for device in items:
        if device.get("approved_device_count", 0) > 1:
            multiple[device["user_id"]] = {
                "user_id": device["user_id"],
                "email": device.get("user_email"),
                "device_count": device["approved_device_count"],
            }
    return {"items": items, "multi_device_accounts": list(multiple.values())}


@app.post("/internal/halo/devices/{device_id}/approve")
def halo_approve_device(device_id: int, body: HaloDeviceActionIn):
    devices = store.list_extension_devices("", include_all_users=True)
    target = next((item for item in devices if item["id"] == device_id), None)
    if not target:
        raise HTTPException(404, "Device request not found.")
    if not _halo_scoped(target, body):
        raise HTTPException(403, "Device is outside your organization.")
    try:
        return {"device": store.approve_extension_device(
            device_id, actor_user_id=body.actor_user_id, actor_is_admin=True,
        )}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _halo_conversation_in_scope(conversation: dict, scope: HaloScopeIn) -> bool:
    if scope.all_users:
        return True
    users = {str(value) for value in scope.user_ids}
    return bool(users & {
        str(conversation.get('initiated_by') or ''),
        str(conversation.get('assigned_recruiter_id') or ''),
    })


def _halo_conversation_public(conversation: dict, *, include_messages=False) -> dict:
    if not conversation:
        return {}
    fields = (
        'id', 'candidate_id', 'nexus_candidate_id', 'candidate_name',
        'candidate_phone', 'initiated_by', 'assigned_recruiter_id',
        'assigned_recruiter_email', 'assigned_recruiter_name', 'status',
        'created', 'updated', 'last_message_at', 'has_reply', 'last_reply_at',
        'last_outbound_at', 'last_outbound_user_id',
    )
    result = {key: conversation.get(key) for key in fields if key in conversation}
    if include_messages:
        result['messages'] = [{
            key: message.get(key) for key in (
                'id', 'direction', 'body', 'status', 'sender_user_id',
                'sender_name', 'sender_number', 'created', 'updated',
            ) if key in message
        } for message in conversation.get('messages') or []]
    return result


@app.post('/internal/halo/conversations')
def halo_conversations(scope: HaloScopeIn):
    items = store.list_sms_conversations(
        user_ids=scope.user_ids,
        include_all=scope.all_users,
    )
    return {'items': [_halo_conversation_public(item) for item in items]}


@app.post('/internal/halo/conversations/{conversation_id}')
def halo_conversation(conversation_id: int, scope: HaloScopeIn):
    conversation = store.get_sms_conversation(conversation_id)
    if not conversation:
        raise HTTPException(404, 'SMS conversation not found.')
    if not _halo_conversation_in_scope(conversation, scope):
        raise HTTPException(403, 'Conversation is outside the requested organization scope.')
    return _halo_conversation_public(conversation, include_messages=True)


@app.post('/internal/halo/conversations/{conversation_id}/assign')
def halo_assign_conversation(conversation_id: int, body: HaloConversationAssignmentIn):
    conversation = store.get_sms_conversation(conversation_id)
    if not conversation:
        raise HTTPException(404, 'SMS conversation not found.')
    if not _halo_conversation_in_scope(conversation, body):
        raise HTTPException(403, 'Conversation is outside the requested organization scope.')
    if conversation.get('status') == 'opted_out':
        raise HTTPException(409, 'An opted-out candidate cannot be reassigned for outreach.')
    messages = conversation.get('messages') or []
    last_reply = max((float(message.get('created') or 0) for message in messages
                      if message.get('direction') == 'inbound'), default=0)
    last_outbound = max((float(message.get('created') or 0) for message in messages
                         if message.get('direction') == 'outbound'
                         and message.get('status') in {'accepted', 'sent', 'delivered'}),
                        default=0)
    if not last_reply:
        raise HTTPException(409, 'Wait for a candidate reply before assigning.')
    if last_outbound > last_reply:
        raise HTTPException(409, 'This conversation has already been answered.')
    conversation = store.update_sms_conversation(
        conversation_id,
        assigned_recruiter_id=body.recruiter_user_id,
        assigned_recruiter_email=body.recruiter_email,
        assigned_recruiter_name=body.recruiter_name,
    )
    return {'conversation': _halo_conversation_public(conversation, include_messages=True)}


@app.post('/internal/halo/conversations/{conversation_id}/reply')
def halo_reply_to_conversation(conversation_id: int, body: HaloConversationReplyIn):
    conversation = store.get_sms_conversation(conversation_id)
    if not conversation:
        raise HTTPException(404, 'SMS conversation not found.')
    if not _halo_conversation_in_scope(conversation, body):
        raise HTTPException(403, 'Conversation is outside the requested organization scope.')
    if conversation.get('status') == 'opted_out':
        raise HTTPException(409, 'This candidate opted out of SMS.')
    messages = conversation.get('messages') or []
    last_reply = max((float(message.get('created') or 0) for message in messages
                      if message.get('direction') == 'inbound'), default=0)
    last_outbound = max((float(message.get('created') or 0) for message in messages
                         if message.get('direction') == 'outbound'
                         and message.get('status') in {'accepted', 'sent', 'delivered'}),
                        default=0)
    if not last_reply:
        raise HTTPException(409, 'Wait for a candidate reply before replying.')
    if last_outbound > last_reply:
        raise HTTPException(409, 'This candidate has already received a reply to their latest message.')
    text = body.message.strip()
    if not text:
        raise HTTPException(422, 'Reply cannot be blank.')
    request_id = body.request_id.strip() or uuid.uuid4().hex
    message, created = store.create_sms_message(
        conversation_id, 'outbound', text, request_id=request_id,
        sender_user_id=body.actor_user_id, sender_name=body.actor_name,
        sender_number=body.sender_number, zoom_user_id=body.zoom_user_id,
    )
    if created:
        try:
            result = zoom_sms.send_sms(
                conversation['candidate_phone'], text,
                sender_number=body.sender_number,
                sender_user_id=body.zoom_user_id,
            )
            sid = str(result.get('sid') or result.get('message_id') or result.get('id') or '')
            store.update_sms_message(message['id'], status='accepted', zoom_message_id=sid)
        except zoom_sms.ZoomSmsError as exc:
            store.update_sms_message(
                message['id'], status='failed', failure_reason=str(exc),
            )
            raise HTTPException(502, str(exc)) from exc
    conversation = store.get_sms_conversation(conversation_id)
    return _halo_conversation_public(conversation, include_messages=True)


@app.get("/auth/me")
def auth_me(request: Request):
    claims = _request_user(request)
    subject = str(claims.get("sub") or "")
    user = store.upsert_user(
        subject,
        email=str(claims.get("email") or ""),
        name=str(claims.get("name") or ""),
    )
    return {
        "user": {
            "user_id": subject,
            "email": claims.get("email", ""),
            "name": claims.get("name", ""),
            "role": claims.get("role", ""),
        },
    }


@app.get("/analytics/me")
def analytics_me(request: Request):
    claims = _request_user(request)
    subject = str(claims.get("sub") or "")
    store.upsert_user(subject, email=str(claims.get("email") or ""), name=str(claims.get("name") or ""))
    return store.user_enrichment_stats(subject)


@app.get("/analytics/users")
def analytics_users(request: Request):
    """Local aggregate retained for administrators and diagnostics."""
    claims = _request_user(request)
    if healthboard_auth.enabled() and claims.get("role") != "admin":
        raise HTTPException(403, "Healthcareboard administrator access is required.")
    return store.all_user_enrichment_stats()


@app.post("/watcher/resume-notifications")
def watcher_resume_notifications(body: WatcherResumeNotificationIn):
    return watcher_notifications.send_new_resume_alert(
        event_id=body.event_id,
        query=body.query,
        location=body.location,
        detected_at=body.detected_at,
        profiles=[profile.model_dump() for profile in body.profiles],
    )


@app.get("/stats")
def stats():
    return store.stats()

# ---- jobs ----
@app.post("/jobs")
def create_job(j: JobIn):
    title = j.title.strip()
    if not title:
        raise HTTPException(400, "Job title is required.")
    return {"id": store.create_job(title, j.location, j.description)}

@app.get("/jobs")
def jobs():
    return store.list_jobs()

# ---- candidates ----
@app.post("/candidates/intake")
def intake_candidates(body: IntakeIn):
    rows = intake.parse(body.text)
    if not rows:
        raise HTTPException(400, "No candidate names found. Paste one per line or upload a CSV with a 'name' column.")
    ids = store.add_candidates_bulk(rows, job_id=body.job_id)
    return {"added": len(ids), "ids": ids, "parsed": rows}


def _profile_row(body: ProfileImportIn, default_job_id: int | None = None):
    captured_name = body.name.strip()
    parsed_name = person_name.parse_person_name(captured_name)
    name = parsed_name.name
    if not name:
        raise HTTPException(400, "Candidate name is required.")
    job_id = body.job_id if body.job_id is not None else default_job_id
    source = body.source.strip().lower()[:50] or "indeed"
    location = body.location.strip()[:500]
    notes = body.notes.strip()
    hometown = " ".join(body.hometown.strip().split())[:500]

    def source_values(values, *, limit: int, max_chars: int = 240):
        output, seen = [], set()
        for value in values or []:
            cleaned = " ".join(str(value or "").split())[:max_chars]
            key = cleaned.casefold()
            if not cleaned or key in seen:
                continue
            seen.add(key)
            output.append(cleaned)
            if len(output) >= limit:
                break
        return output

    def add_evidence_line(lines: list[str], label: str, value: str):
        cleaned = " ".join(str(value or "").split())
        if not cleaned:
            return
        line = f"{label}: {cleaned}"
        if line.casefold() not in {item.casefold() for item in lines}:
            lines.append(line)

    evidence_lines: list[str] = []
    headline = " ".join(body.headline.strip().split())[:500]
    if headline:
        add_evidence_line(evidence_lines, "Headline", headline)
        headline_parts = re.split(r"\s+at\s+", headline, maxsplit=1, flags=re.IGNORECASE)
        add_evidence_line(evidence_lines, "Role", headline_parts[0])
        if len(headline_parts) == 2:
            add_evidence_line(evidence_lines, "Employer", headline_parts[1])
    for value in source_values(body.roles, limit=12):
        add_evidence_line(evidence_lines, "Role", value)
    for value in source_values(body.employers, limit=12):
        add_evidence_line(evidence_lines, "Employer", value)
    for value in source_values(body.schools, limit=10):
        add_evidence_line(evidence_lines, "School", value)
    for value in source_values(body.specialties, limit=20):
        add_evidence_line(evidence_lines, "Specialty", value)
    for value in source_values(body.alternate_names, limit=8, max_chars=160):
        alternate = person_name.normalize_person_name(value)
        if len(person_name.identity_tokens(alternate)) >= 2:
            add_evidence_line(evidence_lines, "Source alternate name", alternate)

    if source == "facebook" and hometown:
        hometown_line = f"Facebook hometown: {hometown}"
        if hometown_line.casefold() not in notes.casefold():
            notes = "\n".join(filter(None, (notes, hometown_line)))
    identity_context = []
    if captured_name and captured_name.casefold() != name.casefold():
        identity_context.append(f"Source display name: {captured_name}")
    identity_context.extend(
        f"Source alternate name: {value}" for value in parsed_name.alternate_names
    )
    identity_context.extend(
        f"Source professional descriptor: {value}" for value in parsed_name.descriptors
    )
    if identity_context:
        evidence_lines.extend(identity_context)
    existing_note_lines = {
        " ".join(line.split()).casefold()
        for line in notes.splitlines() if line.strip()
    }
    canonical_evidence = [
        line for line in evidence_lines
        if " ".join(line.split()).casefold() not in existing_note_lines
    ]
    if canonical_evidence:
        notes = "\n".join([*canonical_evidence, notes]).strip()
    return {
        "name": name,
        "location": location,
        "hometown": hometown if source == "facebook" else "",
        "job_id": job_id,
        "notes": notes[:20000],
        "source": source,
        "source_url": body.source_url.strip()[:2000],
        "source_id": body.source_id.strip()[:500],
    }


def _import_profile(body: ProfileImportIn):
    row = _profile_row(body)
    if row["job_id"] is not None and not store.get_job(row["job_id"]):
        raise HTTPException(404, "job not found")
    result = store.upsert_candidate_profiles([row])[0]
    return {**result, "candidate": _public_candidate(result["candidate"])}


@app.post("/candidates/import")
def import_candidate(body: ProfileImportIn):
    return _import_profile(body)


@app.post("/candidates/import/batch")
def import_candidate_batch(body: ProfileBatchImportIn):
    if not body.profiles:
        raise HTTPException(400, "At least one profile is required.")
    if len(body.profiles) > 100:
        raise HTTPException(400, "A maximum of 100 displayed profiles can be saved at once.")
    if body.job_id is not None and not store.get_job(body.job_id):
        raise HTTPException(404, "job not found")

    rows = [_profile_row(profile, default_job_id=body.job_id) for profile in body.profiles]
    upserted = store.upsert_candidate_profiles(rows, default_job_id=body.job_id)
    imported = sum(int(result["imported"]) for result in upserted)
    existing = len(upserted) - imported
    results = [
        {
            "id": result["id"],
            "imported": result["imported"],
            "candidate": _public_candidate(result["candidate"]),
        }
        for result in upserted
    ]
    return {
        "saved": len(results),
        "imported": imported,
        "existing": existing,
        "results": results,
        "search_url": body.search_url[:2000],
        "database": store.backend_name(),
    }


@app.get("/candidates")
def candidates(job_id: int | None = None, stage: str | None = None):
    return [
        _public_candidate(row)
        for row in store.list_candidates(job_id=job_id, stage=stage)
    ]

@app.get("/candidates/{cid}")
def candidate(cid: int):
    c = store.get_candidate(cid)
    if not c:
        raise HTTPException(404, "not found")
    projected = _public_candidate(c)
    projected["outreach"] = store.list_outreach(cid)
    projected["resumes"] = [
        _public_resume(resume) for resume in store.list_resumes(cid)
    ]
    return projected

@app.patch("/candidates/{cid}/stage")
def set_stage(cid: int, s: StageIn):
    if not store.get_candidate(cid):
        raise HTTPException(404, "candidate not found")
    try:
        store.set_stage(cid, s.stage)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---- talent pools / campaigns ----
@app.get("/pools")
def talent_pools():
    return store.list_talent_pools()


@app.post("/pools/assign")
def assign_pool(body: PoolAssignIn):
    try:
        return store.add_candidates_to_pool(body.name, body.candidate_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/campaigns")
def campaigns():
    return store.list_campaigns()


@app.post("/campaigns")
def create_campaign(body: CampaignCreateIn):
    if body.job_id is not None and not store.get_job(body.job_id):
        raise HTTPException(404, "job not found")
    try:
        return store.create_campaign(body.name, body.candidate_ids, body.job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _store_resume_pdf(cid: int, filename: str, data: bytes):
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    contactable = contact_access.project_candidate(candidate)
    # Strip any previous Medhunt cover before reprocessing, then attach one
    # cover for this candidate. Resume contents are supplied by the source site.
    try:
        data, extraction, _resume_name = resume_enrichment.prepare_candidate_resume(
            data, candidate,
        )
    except resume_enrichment.ContactSheetRefreshError as exc:
        raise HTTPException(409, str(exc)) from exc
    accepted_resume_fields = extraction.get("accepted") or {}
    contact_sheet_candidate = dict(contactable)
    if isinstance(accepted_resume_fields, dict):
        if not str(contact_sheet_candidate.get("name") or "").strip():
            contact_sheet_candidate["name"] = accepted_resume_fields.get("full_name") or ""
        if not str(contact_sheet_candidate.get("location") or "").strip():
            contact_sheet_candidate["location"] = accepted_resume_fields.get("location") or ""
    delivery_route = store.get_candidate_delivery_route(cid) or {}
    nexus_contact_ready = bool(
        config.NEXUS_SYNC_ENABLED
        and delivery_route.get("nexus_enabled")
        and contactable.get("contacts_trusted") is True
        and (contactable.get("phones") or contactable.get("emails"))
    )
    data, contact_sheet_embedded = resume_enrichment.add_contact_sheet(
        data, contact_sheet_candidate,
    )
    if contact_sheet_embedded:
        filename = resume_enrichment.enriched_filename(filename)
    size = len(data)
    queue_nexus = bool(
        nexus_contact_ready and size <= config.NEXUS_MAX_RESUME_BYTES
    )
    nexus_skip_status = (
        "skipped_resume_too_large"
        if nexus_contact_ready and not queue_nexus
        else "disabled"
    )
    checksum = hashlib.sha256(data).hexdigest()
    existing = store.get_resume_by_checksum(cid, checksum)
    if existing:
        store.save_resume_extraction(existing["id"], cid, extraction)
        nexus_delivery = (
            store.enqueue_nexus_delivery(cid, existing["id"], checksum)
            if queue_nexus else None
        )
        return {
            **existing,
            "contact_sheet_embedded": contact_sheet_embedded,
            "contacts_saved": bool(contactable.get("phones") or contactable.get("emails")),
            "deduplicated": True,
            "nexus_sync_status": (
                nexus_delivery.get("status") if nexus_delivery else nexus_skip_status
            ),
        }
    if storage.enabled():
        try:
            uploaded = storage.upload_resume(cid, filename, data)
        except Exception as exc:
            raise HTTPException(502, f"Resume upload to private object storage failed: {exc}")
        resume = store.attach_resume(
            cid,
            filename,
            b"",
            size=size,
            queue_nexus=queue_nexus,
            extraction=extraction,
            **uploaded,
        )
    else:
        resume = store.attach_resume(
            cid, filename, data, checksum_sha256=checksum,
            queue_nexus=queue_nexus,
            extraction=extraction,
        )
    result = {
        **resume,
        "contact_sheet_embedded": contact_sheet_embedded,
        "contacts_saved": bool(contactable.get("phones") or contactable.get("emails")),
    }
    if not queue_nexus:
        result["nexus_sync_status"] = nexus_skip_status
    return result


@app.get("/session")
def authenticated_session():
    """Confirm that this extension copy shares the backend's local token."""
    return {"status": "ok"}


@app.post("/candidates/{cid}/resume/from-download")
def attach_downloaded_resume(cid: int, body: ResumeDownloadIn):
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    contactable = contact_access.project_candidate(candidate)
    if not (
        contactable.get("contacts_trusted") is True
        and (contactable.get("phones") or contactable.get("emails"))
    ):
        raise HTTPException(409, "A verified candidate phone or email is required before saving a downloaded resume.")
    try:
        path = Path(body.path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(400, "Downloaded resume file was not found.")
    try:
        path.relative_to(config.RESUME_DOWNLOAD_DIR)
    except ValueError:
        raise HTTPException(400, "Resume must be inside the configured Downloads directory.")
    if path.suffix.lower() != ".pdf":
        raise HTTPException(400, "Only PDF resumes can be attached automatically.")
    size = path.stat().st_size
    if size <= 0 or size > config.RESUME_MAX_BYTES:
        raise HTTPException(400, "Resume PDF is empty or exceeds the configured size limit.")
    data = path.read_bytes()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "The downloaded file is not a valid PDF.")
    filename = Path(body.filename or path.name).name[:255] or "resume.pdf"
    resume = _store_resume_pdf(cid, filename, data)
    return {"attached": True, "resume": _public_resume(resume)}


@app.post("/candidates/{cid}/resume/from-browser")
def attach_captured_resume(cid: int, body: ResumeCaptureIn):
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    encoded = body.content_base64.strip()
    if encoded.lower().startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    if not encoded or len(encoded) > (config.RESUME_MAX_BYTES * 2):
        raise HTTPException(400, "Captured resume is empty or exceeds the configured size limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Captured resume data is not valid base64.")
    pdf_start = data.find(b"%PDF-")
    if pdf_start < 0:
        raise HTTPException(400, "Captured resume is not a valid PDF.")
    if pdf_start:
        data = data[pdf_start:]
    if not data or len(data) > config.RESUME_MAX_BYTES:
        raise HTTPException(400, "Captured resume is empty or exceeds the configured size limit.")
    filename = Path(body.filename or "resume.pdf").name[:255] or "resume.pdf"
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    resume = _store_resume_pdf(cid, filename, data)
    refreshed = contact_access.project_candidate(store.get_candidate(cid))
    return {
        "attached": True,
        "resume": _public_resume(resume),
        "candidate": {
            "id": cid,
            "phones": refreshed.get("phones", []),
            "emails": refreshed.get("emails", []),
            "database_saved": bool(refreshed.get("phones") or refreshed.get("emails")),
        },
    }


@app.get("/candidates/{cid}/resumes/{resume_id}")
def get_resume(cid: int, resume_id: int):
    resume = store.get_resume(cid, resume_id)
    if not resume:
        raise HTTPException(404, "resume not found")
    safe_name = Path(resume["filename"]).name.replace('"', "")
    data = bytes(resume.get("data") or b"")
    if not data and resume.get("storage_provider") == "r2":
        try:
            data = storage.download_resume(resume.get("object_key") or "")
        except Exception as exc:
            raise HTTPException(502, f"Stored resume could not be retrieved: {exc}")
    if not data:
        raise HTTPException(404, "resume file is unavailable")
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    extraction_row = store.get_resume_extraction(resume_id, cid)
    try:
        _original, extraction, _resume_name = resume_enrichment.prepare_candidate_resume(
            data, candidate,
            extraction_row.get("extraction") if extraction_row else None,
        )
    except resume_enrichment.ContactSheetRefreshError as exc:
        raise HTTPException(409, f"Stored resume could not be prepared: {exc}") from exc
    if not extraction_row or extraction_row.get("extraction") != extraction:
        store.save_resume_extraction(resume_id, cid, extraction)
    current = contact_access.project_candidate(candidate)
    try:
        data, _ = resume_enrichment.refresh_contact_sheet(data, current)
    except resume_enrichment.ContactSheetRefreshError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(
        content=data,
        media_type=resume["mime_type"] or "application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )

# ---- enrichment ----
def _consume_extension_enrichment_credits(
    request: Request | None, candidate_ids: list[int], run_id: str = "",
) -> dict | None:
    if not candidate_ids or not healthboard_auth.enabled():
        return None
    token = str(getattr(
        getattr(request, "state", None), "healthboard_extension_token", "",
    ) or "")
    if not token:
        raise HTTPException(503, "Extension credit service is unavailable. Please sign in again.")
    try:
        return healthboard_auth.consume_medhunt_enrichment_credits(
            token, candidate_ids=candidate_ids, run_id=run_id,
        )
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", "Extension credit request failed.")
        except (ValueError, AttributeError):
            detail = "Extension credit request failed."
        raise HTTPException(exc.response.status_code, detail) from exc
    except Exception as exc:
        logging.getLogger("medhunt.credits").warning(
            "Extension credit request failed (%s).", type(exc).__name__,
        )
        raise HTTPException(503, "Extension credits could not be checked. Try again shortly.") from exc


@app.get("/credits/enrichment")
def enrichment_credit_balance(request: Request):
    actor = _request_user(request)
    token = str(getattr(request.state, "healthboard_extension_token", "") or "")
    if not healthboard_auth.enabled() or not token:
        return {"enabled": False, "balance": None}
    try:
        return {"enabled": True, **healthboard_auth.medhunt_enrichment_credits(token)}
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", "Extension credits could not be loaded.")
        except (ValueError, AttributeError):
            detail = "Extension credits could not be loaded."
        raise HTTPException(exc.response.status_code, detail) from exc
    except Exception as exc:
        logging.getLogger("medhunt.credits").warning(
            "Extension credit balance request failed (%s).", type(exc).__name__,
        )
        raise HTTPException(503, "Extension credits could not be loaded. Try again shortly.") from exc


def _quick_sourcer_selected() -> bool:
    """The public Medhunt product has one backend contact-lookup path."""
    return True


def _record_enrichment(request: Request | None, candidate_id: int, status: str,
                       *, provider: str, run_id: str = "") -> None:
    actor = _request_user(request)
    subject = str(actor.get("sub") or "local")
    normalized_status = str(status or "unknown")
    token = str(getattr(
        getattr(request, "state", None), "healthboard_extension_token", "",
    ) or "")
    targets = actor.get("delivery_targets") or {}
    store.set_candidate_delivery_route(
        candidate_id, subject, str(actor.get("email") or ""),
        ceipal=bool(targets.get("ceipal")), nexus=bool(targets.get("nexus")),
    )
    if normalized_status.casefold() in {"found", "success", "enriched"}:
        ceipal_delivery.queue_candidate(candidate_id)
    event = store.record_enrichment_event(
        subject, candidate_id, normalized_status,
        provider=provider, run_id=run_id,
        halo_pending=bool(token and healthboard_auth.enabled()),
    )
    if not token or not healthboard_auth.enabled():
        return
    try:
        analytics_delivery.deliver(event, token=token)
    except Exception as exc:
        # The paid/contact result is already complete. Analytics delivery must
        # never discard it; the local event remains available for reconciliation.
        logging.getLogger("medhunt.analytics").warning(
            "Healthcareboard enrichment event delivery failed (%s).",
            type(exc).__name__,
        )


@app.post("/candidates/{cid}/contact-lookup")
def enrich_one(cid: int, request: Request = None):
    _request_user(request)
    if not store.get_candidate(cid):
        raise HTTPException(404, "candidate not found")
    credit_result = None
    if quick_sourcer_client.needs_provider_lookup(cid):
        credit_result = _consume_extension_enrichment_credits(request, [cid])
    result = quick_sourcer_client.lookup_candidate(cid)
    if credit_result is not None:
        result["credits_remaining"] = credit_result.get("balance")
    _record_enrichment(
        request, cid,
        result.get("status") or result.get("enrich_status") or "unknown",
        provider="quick_sourcer",
    )
    nexus_delivery.queue_latest_resume_if_ready(cid)
    return result


@app.post("/candidates/{cid}/identity/review")
def review_candidate_identity(cid: int, body: IdentityReviewIn):
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    decision = body.decision.strip().lower()
    if decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be approved or rejected")
    evidence = {
        **(candidate.get("identity_evidence") or {}),
        "review_note": body.note.strip()[:1000], "review_decision": decision,
    }
    if decision == "approved":
        canonical = (
            body.canonical_name.strip() or candidate.get("canonical_name")
            or candidate["name"]
        )
        canonical = person_name.normalize_person_name(canonical)
        if not canonical:
            raise HTTPException(400, "A valid candidate name is required.")
        store.update_candidate(
            cid, canonical_name=canonical, identity_status="recruiter_confirmed",
            identity_score=1.0, identity_provider="recruiter_review",
            identity_evidence=evidence, identity_verified_at=time.time(),
        )
    else:
        store.update_candidate(
            cid, identity_status="rejected", identity_score=0,
            identity_provider="recruiter_review", identity_evidence=evidence,
            identity_verified_at=0,
            emails=[], phones=[], addresses=[], enrich_status="no_match",
        )
    return _public_candidate(store.get_candidate(cid))


def _internal_contact_lookup_batch(
    body: PdlBatchEnrichIn, *, location_overrides: dict[int, str] | None = None,
):
    """Resolve a whole selection in one bulk PDL call and one database write.

    The side panel uses this instead of one request per candidate; the run
    credit limit, required usable-contact policy, and provider evidence rules
    are applied identically to each result.
    """
    try:
        batch = pdl_client.enrich_candidates(
            body.candidate_ids, body.run_id,
            location_overrides=location_overrides,
        )
    except Exception as exc:
        # A PDL transport/processing failure is itself a fallback condition;
        # keep the per-candidate errors and let the cost-aware waterfall try
        # Enformion for identities with an exact captured location.
        batch = {
            "provider": "people_data_labs",
            "credits_spent": 0,
            "run_credits_spent": 0,
            "results": {
                str(cid): {
                    "status": "error",
                    "provider": "people_data_labs",
                    "credits_spent": 0,
                    "error": f"PDL backend processing failed ({type(exc).__name__}).",
                }
                for cid in body.candidate_ids
            },
        }
    corroborated = multi_provider.verify_batch(
        body.candidate_ids,
        batch.get("results") or {},
        body.run_id,
        allow_enformion=body.allow_enformion,
        max_enformion_calls=body.max_enformion_calls,
        location_overrides=location_overrides,
    )
    return {
        **batch,
        "provider": "people_data_labs_with_enformion_fallback",
        "contact_verification": corroborated["summary"],
        "results": {str(cid): result for cid, result in corroborated["results"].items()},
    }


def _enformion_run_call_ceiling(candidate_count: int) -> int:
    """Allow one Person Search and at most one owner-bound Tahoe follow-up."""
    return min(100, max(0, int(candidate_count or 0)) * 2)


def _facebook_hometown(candidate: dict | None) -> str:
    """Read the separately captured Facebook ``From`` fact from source notes."""
    if not candidate or str(candidate.get("source") or "").casefold() != "facebook":
        return ""
    structured = " ".join(str(candidate.get("hometown") or "").split())
    if structured:
        return structured[:500]
    notes = str(candidate.get("notes") or "")
    prefixes = (
        "facebook hometown:",
        # Compatibility with profiles captured before the structured field was
        # added. New captures always use the reserved first prefix.
        "visible detail: from ",
        "hometown:",
    )
    for raw_line in notes.splitlines():
        line = " ".join(raw_line.split())
        folded = line.casefold()
        for prefix in prefixes:
            if folded.startswith(prefix):
                value = line[len(prefix):].strip(" :-")
                return value[:500]
    return ""


def _location_identity(value: str) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _hometown_retry_allowed(result: dict) -> bool:
    """Retry only a definitive no-contact outcome, never ambiguity/failure."""
    source = dict(result or {})
    status = str(source.get("status") or "").strip().casefold()
    if status not in {"no_match", "skipped"}:
        return False
    verification_record = dict(source.get("verification") or {})
    verification_evidence = dict(verification_record.get("evidence") or {})
    contact = dict(
        source.get("contact_verification")
        or verification_evidence.get("contact_verification")
        or {}
    )
    contact_status = str(contact.get("status") or "").strip().casefold()
    if contact_status in {
        "fallback_budget_exhausted", "fallback_not_authorized",
        "fallback_unavailable", "enformion_identity_review",
    }:
        return False
    if str(contact.get("enformion_status") or "").strip().casefold() == "error":
        return False
    selection = dict(contact.get("enformion_selection") or {})
    if selection.get("selection") == "ambiguous_exact_matches":
        return False
    if contact.get("enformion_conflicts"):
        return False
    reason_code = str(source.get("reason_code") or "").strip().casefold()
    if source.get("error") and not reason_code:
        return False
    if reason_code == "local_validation_rejected":
        # This is a discovery retry, not acceptance of the rejected record.
        # A provider/current-city mismatch is exactly when Facebook's distinct
        # source-captured hometown can find a different owner record. The
        # second response still has to pass every normal name, location,
        # ambiguity, contact and DNC gate before anything is displayed.
        pass
    elif reason_code and reason_code not in {
        "pdl_required_contact_no_match",
        # Compatibility with cached results created before email-bearing PDL
        # identities became eligible for the normal contact policy.
        "pdl_required_mobile_no_match",
    }:
        return False
    return _public_lookup_result(source)["status"] == "not_found"


def _annotate_hometown_match(candidate_id: int, result: dict, hometown: str) -> dict:
    """Attach auditable internal context only after the retry is trusted."""
    if _public_lookup_result(result).get("status") != "found":
        return result
    location_context = {"type": "hometown", "value": hometown[:500]}
    annotated = {**result, "matched_location_context": location_context}
    candidate = store.get_candidate(candidate_id)
    if candidate:
        verification_record = dict(
            annotated.get("verification") or candidate.get("verification") or {}
        )
        evidence = dict(verification_record.get("evidence") or {})
        evidence["matched_location_context"] = location_context
        verification_record["evidence"] = evidence
        store.update_candidate(candidate_id, verification=verification_record)
        annotated["verification"] = verification_record
    return annotated


def _quick_sourcer_lookup_batch(body: ContactLookupBatchIn) -> dict:
    """Look each selected candidate up through Quick Sourcer, one at a time.

    The external API drives a real browser per person, so these cannot be
    parallelised; the side panel therefore sends small chunks and shows
    per-candidate progress. Every answer is returned exactly as the API
    delivered it — a miss is a miss, not a policy hold.
    """
    results = {}
    for candidate_id in dict.fromkeys(body.candidate_ids):
        if not store.get_candidate(candidate_id):
            results[str(candidate_id)] = {
                "status": "failed", "emails": [], "phones": [],
                "phone_contacts": [], "resume_required": False, "location_match": None,
            }
            continue
        try:
            results[str(candidate_id)] = quick_sourcer_client.lookup_candidate(candidate_id)
        except Exception as exc:
            # A malformed provider response or one database row must never turn
            # the entire selection into an HTTP 500. The panel can retry this
            # candidate independently while completed rows remain visible.
            logging.getLogger("medhunt.lookup").warning(
                "Candidate lookup failed for id %s (%s).",
                candidate_id,
                type(exc).__name__,
            )
            results[str(candidate_id)] = {
                "status": "failed", "emails": [], "phones": [],
                "phone_contacts": [], "resume_required": False,
                "location_match": None,
            }
        try:
            nexus_delivery.queue_latest_resume_if_ready(candidate_id)
        except Exception as exc:
            # Nexus delivery is asynchronous follow-up. It must not invalidate
            # a contact result that was already found and stored successfully.
            logging.getLogger("medhunt.nexus").warning(
                "Nexus queueing was deferred for candidate %s (%s).",
                candidate_id,
                type(exc).__name__,
            )
    return {
        "status": "ok",
        "results": results,
        "processed": len(results),
        "matched": sum(
            1 for result in results.values() if result.get("status") == "found"
        ),
    }


@app.post("/contact-lookup/batch")
def contact_lookup_batch(body: ContactLookupBatchIn, request: Request = None):
    _request_user(request)
    """Vendor-neutral browser endpoint with a deliberately minimal response."""
    billable_ids = [
        candidate_id for candidate_id in dict.fromkeys(body.candidate_ids)
        if store.get_candidate(candidate_id)
        and quick_sourcer_client.needs_provider_lookup(candidate_id)
    ]
    credit_result = _consume_extension_enrichment_credits(
        request, billable_ids, body.run_id,
    )
    result = _quick_sourcer_lookup_batch(body)
    if credit_result is not None:
        result["credits_remaining"] = credit_result.get("balance")
    for candidate_id, item in (result.get("results") or {}).items():
        _record_enrichment(
            request, int(candidate_id), item.get("status") or "unknown",
            provider="quick_sourcer", run_id=body.run_id,
        )
    return result


@app.post("/enrich/batch")
def enrich_batch(job_id: int | None = None):
    raise HTTPException(410, "Use the candidate contact lookup endpoint.")


def _candidate_sms_phone(candidate: dict, supplied: str) -> str:
    requested = store.contact_key(supplied)
    projected = contact_access.project_candidate(candidate)
    selected = next((
        str(item.get('value') or '').strip()
        for item in projected.get('phone_contacts') or []
        if str(item.get('kind') or '').casefold() == 'mobile'
        and store.contact_key(item.get('value')) == requested
    ), '')
    if not selected:
        raise HTTPException(400, 'Select a verified mobile number for this candidate.')
    try:
        return twilio_sms.normalize_phone(selected)
    except twilio_sms.TwilioSmsError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get('/messaging/status')
def messaging_status(request: Request):
    user = _request_user(request)
    zoom_sender = _resolve_zoom_sms_sender(request, strict=False) if zoom_sms.enabled() else None
    providers = {
        'twilio': {
            'enabled': twilio_sms.enabled(),
            'sender_configured': bool(config.TWILIO_PHONE_NUMBER),
        },
        'zoom': {
            'enabled': zoom_sms.enabled(),
            'sender_configured': bool(zoom_sender),
        },
    }
    return {
        'enabled': any(item['enabled'] and item['sender_configured'] for item in providers.values()),
        # Keep these fields for already-installed Twilio-only extension builds.
        'provider': 'twilio',
        'sender_configured': providers['twilio']['sender_configured'],
        'providers': providers,
        'zoom_sender_required': bool(zoom_sms.enabled() and not zoom_sender),
        'reply_notifications_configured': sms_notifications.configured(
            str(user.get('email') or ''),
        ),
    }


@app.get('/candidates/{candidate_id}/sms-preview')
def sms_preview(candidate_id: int, phone: str, request: Request):
    _request_user(request)
    candidate = store.get_candidate(candidate_id)
    if not candidate:
        raise HTTPException(404, 'Candidate not found.')
    verified_phone = _candidate_sms_phone(candidate, phone)
    return {
        'phone': verified_phone,
        'opted_out': bool(
            store.is_dnc(verified_phone)
            or store.candidate_sms_opted_out(candidate_id)
        ),
        'already_contacted': bool(
            store.get_sms_outreach_claim(candidate_id, verified_phone)
            or store.sms_candidate_contacted(candidate_id, verified_phone)
        ),
    }


def _resolve_zoom_sms_sender(request: Request, *, strict: bool) -> dict | None:
    if healthboard_auth.enabled():
        token = str(getattr(request.state, 'healthboard_extension_token', '') or '')
        try:
            return healthboard_auth.medhunt_zoom_sms_sender(token)
        except (httpx.HTTPError, ValueError) as exc:
            logging.getLogger('medhunt.zoom_sms').warning(
                'Could not load the recruiter Zoom sender assignment (%s).',
                type(exc).__name__,
            )
            if strict:
                raise HTTPException(
                    503,
                    'Could not verify your Zoom sender assignment. Try again or contact your Halo administrator.',
                ) from exc
            return None
    # Standalone local deployments retain their environment-configured sender.
    if config.ZOOM_SMS_SENDER_NUMBER and config.ZOOM_SMS_SENDER_USER_ID:
        return {
            'sender_number': config.ZOOM_SMS_SENDER_NUMBER,
            'zoom_user_id': config.ZOOM_SMS_SENDER_USER_ID,
        }
    return None


@app.post('/messaging/sms')
def send_sms(body: SmsSendIn, request: Request):
    user = _request_user(request)
    provider = body.provider.strip().lower()
    if provider not in {'twilio', 'zoom'}:
        raise HTTPException(422, 'Choose Twilio or Zoom Phone as the SMS provider.')
    provider_client = twilio_sms if provider == 'twilio' else zoom_sms
    if not provider_client.enabled():
        raise HTTPException(503, f'{provider.title()} SMS is not configured.')
    zoom_sender = (
        _resolve_zoom_sms_sender(request, strict=True)
        if provider == 'zoom' else None
    )
    if provider == 'zoom' and not zoom_sender:
        raise HTTPException(
            409,
            'Ask your Halo administrator to assign your Zoom Phone number before sending.',
        )
    candidate = store.get_candidate(body.candidate_id)
    if not candidate:
        raise HTTPException(404, 'Candidate not found.')
    phone = _candidate_sms_phone(candidate, body.phone)
    message_text = body.message.strip()
    if not message_text:
        raise HTTPException(400, 'Write a message before sending.')
    if store.is_dnc(phone) or store.candidate_sms_opted_out(body.candidate_id):
        raise HTTPException(409, 'This candidate opted out and cannot be messaged.')
    if store.sms_candidate_contacted(body.candidate_id, phone):
        raise HTTPException(409, 'This candidate has already been sent SMS outreach.')
    request_id = body.request_id.strip() or uuid.uuid4().hex
    claim = store.claim_sms_outreach(body.candidate_id, phone, request_id)
    if not claim['created']:
        raise HTTPException(409, 'This candidate has already been sent SMS outreach.')
    sender_number = (
        config.TWILIO_PHONE_NUMBER if provider == 'twilio'
        else str(zoom_sender['sender_number'])
    )
    sender_provider_id = (
        'twilio' if provider == 'twilio' else str(zoom_sender['zoom_user_id'])
    )
    conversation = store.get_or_create_sms_conversation(
        body.candidate_id, phone,
        candidate_name=str(candidate.get('name') or ''),
        initiated_by=str(user.get('sub') or ''),
        sender_number=sender_number, sender_user_id=sender_provider_id,
    )
    message, created = store.create_sms_message(
        conversation['id'], 'outbound', message_text, request_id=request_id,
        sender_user_id=str(user.get('sub') or ''),
        sender_name=str(user.get('email') or ''),
        sender_number=sender_number, zoom_user_id=sender_provider_id,
    )
    if not created:
        raise HTTPException(409, 'This candidate has already been sent SMS outreach.')
    try:
        if provider == 'zoom':
            result = provider_client.send_sms(
                phone, message_text,
                sender_number=sender_number,
                sender_user_id=sender_provider_id,
            )
        else:
            result = provider_client.send_sms(phone, message_text)
        sid = str(result.get('sid') or result.get('message_id') or result.get('id') or '')
        message = store.update_sms_message(
            message['id'], status='accepted', zoom_message_id=sid,
        )
        store.update_sms_outreach_claim(
            body.candidate_id, status='accepted', message_id=message['id'],
        )
        store.set_stage(body.candidate_id, 'contacted')
    except (twilio_sms.TwilioSmsError, zoom_sms.ZoomSmsError) as exc:
        store.update_sms_message(message['id'], status='failed', failure_reason=str(exc))
        store.update_sms_outreach_claim(
            body.candidate_id, status='failed', message_id=message['id'],
        )
        raise HTTPException(502, str(exc)) from exc
    return {'provider': provider, 'message': message}


@app.post('/integrations/twilio/webhook')
async def twilio_webhook(request: Request):
    raw = await request.body()
    try:
        decoded = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise HTTPException(400, 'Invalid Twilio webhook body.') from exc
    form = parse_qs(decoded, keep_blank_values=True)
    webhook_url = config.TWILIO_WEBHOOK_URL or str(request.url)
    if not twilio_sms.validate_webhook(
        webhook_url, form, request.headers.get('x-twilio-signature', ''),
    ):
        raise HTTPException(403, 'Invalid Twilio webhook signature.')
    sender_phone = str((form.get('From') or [''])[0])
    text = str((form.get('Body') or [''])[0])
    message_sid = str((form.get('MessageSid') or form.get('SmsSid') or [''])[0])
    event_key = f'twilio:received:{message_sid or hashlib.sha256(raw).hexdigest()}'
    if not store.claim_sms_webhook(event_key, 'twilio.sms_received'):
        return Response(content='<Response/>', media_type='application/xml')
    current = store.find_sms_conversation(phone=sender_phone)
    if not current:
        store.complete_sms_webhook(event_key)
        return Response(content='<Response/>', media_type='application/xml')
    store.create_sms_message(
        current['id'], 'inbound', text, request_id=event_key, status='received',
    )
    tokens = re.findall(r'[a-z]+', text.casefold())
    stop_reply = any(
        token in {'stop', 'stopall', 'unsubscribe', 'cancel', 'end', 'quit'}
        for token in tokens
    )
    current = store.update_sms_conversation(
        current['id'], status='opted_out' if stop_reply else 'replied',
    )
    store.set_stage(int(current['candidate_id']), 'replied')
    if stop_reply:
        store.record_sms_consent(
            int(current['candidate_id']), current['candidate_phone'], 'opted_out',
            'inbound_sms', f'Twilio webhook {event_key}', captured_by='twilio',
        )
        current = store.update_sms_conversation(current['id'], status='opted_out')
    saved = store.get_sms_conversation(current['id'])
    outbound = [
        item for item in saved.get('messages', [])
        if item.get('direction') == 'outbound'
    ]
    original = str(outbound[-1].get('body') or '') if outbound else ''
    recruiter_email = next((
        str(item.get('sender_name') or '') for item in reversed(outbound)
        if str(item.get('sender_name') or '').strip()
    ), '')
    sms_notifications.send_reply_email(
        conversation={**saved, 'initiated_by_email': recruiter_email},
        reply=text, original=original,
    )
    store.complete_sms_webhook(event_key)
    return Response(content='<Response/>', media_type='application/xml')


@app.post('/integrations/zoom/webhook')
async def zoom_webhook(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, 'Invalid Zoom webhook payload.') from exc
    if payload.get('event') == 'endpoint.url_validation':
        plain = str((payload.get('payload') or {}).get('plainToken') or '')
        if not config.ZOOM_WEBHOOK_SECRET_TOKEN or not plain:
            raise HTTPException(403, 'Zoom webhook validation is not configured.')
        return {'plainToken': plain, 'encryptedToken': zoom_sms.validation_token(plain)}
    if not zoom_sms.validate_webhook(
        request.headers.get('x-zm-request-timestamp', ''), raw,
        request.headers.get('x-zm-signature', ''),
    ):
        raise HTTPException(403, 'Invalid Zoom webhook signature.')
    event_type = str(payload.get('event') or '')
    obj = ((payload.get('payload') or {}).get('object') or {})
    message_id = str(obj.get('message_id') or '')
    session_id = str(obj.get('session_id') or '')
    event_key = (
        f'zoom:{event_type}:{message_id or payload.get("event_ts") or hashlib.sha256(raw).hexdigest()}'
    )
    if not store.claim_sms_webhook(event_key, event_type):
        return {'received': True, 'duplicate': True}
    sender_phone = str((obj.get('sender') or {}).get('phone_number') or '')
    recipients = obj.get('to_members') or []
    recipient_phone = str((recipients[0] if recipients else {}).get('phone_number') or '')
    lookup_phone = sender_phone if event_type == 'phone.sms_received' else recipient_phone
    current = store.find_sms_conversation(session_id=session_id, phone=lookup_phone)
    if not current:
        store.complete_sms_webhook(event_key)
        return {'received': True, 'matched': False}
    if session_id and not current.get('zoom_session_id'):
        current = store.update_sms_conversation(current['id'], zoom_session_id=session_id)
    if event_type == 'phone.sms_received':
        reply = str(obj.get('message') or '')
        store.create_sms_message(
            current['id'], 'inbound', reply, request_id=event_key, status='received',
        )
        stop_reply = any(
            token in {'stop', 'stopall', 'unsubscribe', 'cancel', 'end', 'quit'}
            for token in re.findall(r'[a-z]+', reply.casefold())
        )
        current = store.update_sms_conversation(
            current['id'], status='opted_out' if stop_reply else 'replied',
        )
        store.set_stage(int(current['candidate_id']), 'replied')
        if stop_reply:
            store.record_sms_consent(
                int(current['candidate_id']), current['candidate_phone'], 'opted_out',
                'inbound_sms', f'Zoom webhook {event_key}', captured_by='zoom',
            )
        saved = store.get_sms_conversation(current['id'])
        outbound = [
            item for item in saved.get('messages', [])
            if item.get('direction') == 'outbound'
        ]
        original = str(outbound[-1].get('body') or '') if outbound else ''
        recruiter_email = next((
            str(item.get('sender_name') or '') for item in reversed(outbound)
            if str(item.get('sender_name') or '').strip()
        ), '')
        sms_notifications.send_reply_email(
            conversation={**saved, 'initiated_by_email': recruiter_email},
            reply=reply, original=original,
        )
    elif event_type in {'phone.sms_sent', 'phone.sms_sent_failed'}:
        failed = bool(obj.get('failure_reason')) or event_type.endswith('failed')
        store.reconcile_outbound_sms(
            current['id'], zoom_message_id=message_id,
            status='failed' if failed else 'sent',
            failure_reason=str(obj.get('failure_reason') or ''),
        )
    for opt in obj.get('phone_number_campaign_opt_statuses') or []:
        if (
            str(opt.get('opt_status') or '').casefold() == 'opt_out'
            or int(opt.get('opt_in_status') or 0) == 4
        ):
            store.record_sms_consent(
                int(current['candidate_id']), current['candidate_phone'], 'opted_out',
                'inbound_sms', f'Zoom campaign opt-out {event_key}', captured_by='zoom',
            )
            store.update_sms_conversation(current['id'], status='opted_out')
    store.complete_sms_webhook(event_key)
    return {'received': True, 'matched': True}


# ---- ranking ----
@app.post("/jobs/{job_id}/rank")
def rank(job_id: int):
    r = ranking.rank_job(job_id)
    if r.get("error"):
        raise HTTPException(404, r["error"])
    return r

# ---- outreach ----
@app.post("/outreach/draft")
def draft(o: OutreachIn):
    job = store.get_job(o.job_id) if o.job_id else None
    if o.job_id and not job:
        raise HTTPException(404, "job not found")
    r = outreach.draft_for_candidate(o.candidate_id, job=job, channel=o.channel)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    return r


@app.post("/outreach/draft/batch")
def draft_batch(body: OutreachBatchIn):
    channel = body.channel.strip().lower()
    if channel != "email":
        raise HTTPException(400, "Batch outreach currently supports email only.")
    job = store.get_job(body.job_id) if body.job_id else None
    if body.job_id and not job:
        raise HTTPException(404, "job not found")

    drafts, errors = [], []
    for candidate_id in dict.fromkeys(body.candidate_ids):
        result = outreach.draft_for_candidate(candidate_id, job=job, channel=channel)
        if result.get("error"):
            errors.append({"candidate_id": candidate_id, "error": result["error"]})
        else:
            candidate = store.get_candidate(candidate_id) or {}
            drafts.append({**result, "candidate_name": candidate.get("name", "")})
    return {
        "drafts": drafts,
        "errors": errors,
        "created": len(drafts),
        "failed": len(errors),
    }


@app.post("/outreach/{oid}/approve")
def approve(oid: int):
    r = outreach.approve_and_mark_sent(oid)
    if r.get("error"):
        raise HTTPException(404, r["error"])
    return r

# ---- Backend-owned public-record lookup ----
@app.get("/records/status")
def records_status():
    status = quick_sourcer_client.status()
    return {
        "enabled": bool(status.get("enabled")),
        "typical_seconds": list(status.get("typical_seconds") or []),
    }


@app.post("/candidates/{cid}/professional-profile-resume")
def build_professional_profile_resume(cid: int, body: ProfessionalProfileResumeIn):
    """Render and store an explicitly labeled public-profile résumé.

    Storage and Nexus delivery intentionally pass through the same audited
    resume path as captured PDFs. Public appointment phone numbers are not
    accepted by this endpoint and therefore cannot become candidate contacts.
    """
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    candidate_source = str(candidate.get("source") or "").strip().casefold()
    source_rules = {
        "usnews": (
            lambda host, path: host == "health.usnews.com"
            and bool(re.match(r"^/(?:doctors|nurse-practitioners)/", path, re.IGNORECASE)),
            "U.S. News",
        ),
        "medifind": (
            lambda host, path: (host == "medifind.com" or host.endswith(".medifind.com"))
            and bool(re.match(r"^/doctors/[^/]+/\d+/?$", path, re.IGNORECASE)),
            "MediFind",
        ),
        "commonspirit": (
            lambda host, path: (host == "commonspirit.org" or host.endswith(".commonspirit.org"))
            and bool(re.match(r"^/find-a-doctor/[^/]+-\d+/?$", path, re.IGNORECASE)),
            "CommonSpirit Health",
        ),
        "sharecare": (
            lambda host, path: host == "providers.sharecare.com"
            and bool(re.match(r"^/doctor/[^/]+/?$", path, re.IGNORECASE)),
            "Sharecare",
        ),
        "webmd": (
            lambda host, path: host == "doctor.webmd.com"
            and bool(re.match(r"^/doctor/[^/]+-overview/?$", path, re.IGNORECASE)),
            "WebMD",
        ),
    }
    rule = source_rules.get(candidate_source)
    if not rule:
        raise HTTPException(400, "Professional profile resumes are unavailable for this source.")
    try:
        source = urlsplit(body.source_url)
    except ValueError:
        source = None
    if (
        not source
        or source.scheme not in {"http", "https"}
        or not rule[0]((source.hostname or "").casefold(), source.path)
    ):
        raise HTTPException(400, f"A valid {rule[1]} provider profile URL is required.")
    contactable = contact_access.project_candidate(candidate)
    if not (
        contactable.get("contacts_trusted") is True
        and (contactable.get("phones") or contactable.get("emails"))
    ):
        raise HTTPException(409, "A verified candidate phone or email is required before generating a resume.")
    profile = body.model_dump()
    if not any((
        profile["education"], profile["certifications"], profile["licenses"],
        profile["specialties"], profile["hospitals"], profile["summary"],
    )):
        raise HTTPException(400, "The public profile has not finished loading professional details.")
    try:
        rendered = profile_resume.render(candidate, profile)
    except Exception as exc:
        logging.getLogger("medhunt.profile_resume").exception(
            "Professional profile PDF rendering failed for candidate %s", cid,
        )
        raise HTTPException(500, "Professional profile PDF could not be generated.") from exc
    resume = _store_resume_pdf(
        cid,
        profile_resume.filename(candidate, profile),
        rendered,
    )
    return {
        "attached": True,
        "document_type": "public_professional_profile",
        "resume": _public_resume(resume),
    }


@app.post("/records/find")
def records_find(body: QuickSourcerFindIn):
    name = person_name.normalize_person_name(body.name) or body.name.strip()
    if not name:
        raise HTTPException(400, "A candidate name is required.")
    location = body.location.strip()
    candidate_id = int(body.candidate_id or 0)
    if candidate_id:
        candidate = store.get_candidate(candidate_id)
        if not candidate:
            raise HTTPException(404, "candidate not found")
        location = location or str(candidate.get("location") or "")
    result = quick_sourcer_client.find(
        name, location, candidate_id=candidate_id, refresh=body.refresh,
    )
    return {**result, "candidate_id": candidate_id or None, "searched": {"name": name, "location": location}}


@app.get("/records/candidates/{external_id}")
def records_candidate(external_id: int, refresh: bool = False):
    result = quick_sourcer_client.fetch(external_id, refresh=refresh)
    if result.get("status") == "not_found":
        raise HTTPException(404, "No record exists with that id.")
    return result


# ---- do-not-contact ----
@app.post("/dnc")
def add_dnc(d: DncIn):
    value = d.value.strip()
    if not value:
        raise HTTPException(400, "Email or phone is required.")
    store.add_dnc(value, d.reason)
    return {"ok": True}

@app.get("/dnc")
def dnc():
    return store.list_dnc()

@app.get("/compliance")
def compliance():
    return {"notice": config.COMPLIANCE_NOTICE,
            "human_approval_required": config.REQUIRE_HUMAN_APPROVAL,
            "default_channel": config.DEFAULT_CHANNEL}
