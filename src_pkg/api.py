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
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from sourcing import (
    store, intake, ranking, outreach, config, storage,
    resume_enrichment, contact_access,
    person_name, phone_policy, quick_sourcer_client,
    nexus_delivery, resume_extraction, watcher_notifications, healthboard_auth,
    profile_resume, zoom_sms,
    contact_lookup_queue, ats_routing, ceipal_delivery,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Database schema preparation and provider clients are already lazy inside
    # their respective stores. Do not contact any remote dependency before the
    # localhost API becomes ready: a slow pooled database connection or
    # provider probe must not make every browser-extension action unavailable.
    if (config.NEXUS_SYNC_REQUESTED and not config.NEXUS_SYNC_ENABLED
            and not nexus_delivery.worker_enabled()):
        logging.getLogger("medhunt.nexus").error(
            "Candidate synchronization was requested but is disabled (%s).",
            config.NEXUS_DISABLED_REASON,
        )
    nexus_delivery.start()
    ceipal_delivery.start()
    contact_lookup_queue.start()
    try:
        yield
    finally:
        contact_lookup_queue.stop()
        ceipal_delivery.stop()
        nexus_delivery.stop()


APP_VERSION = "3.37.4"

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
    ],
)


_PUBLIC_LOCAL_PATHS = frozenset({
    "/", "/app.js", "/styles.css", "/privacy", "/health", "/auth/config",
    "/auth/request-code", "/auth/verify-code",
    "/integrations/zoom/webhook",
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
        expected = config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN
        supplied = request.headers.get("x-medhunt-service-token", "").strip()
        if not expected:
            return JSONResponse({"detail": "Halo service access is not configured."}, status_code=503)
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Invalid Halo service credential."}, status_code=401)
        request.state.user = {"sub": "halo-service", "role": "service"}
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
            request.state.user = {
                "sub": str(identity.get("user_id") or ""),
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
_FRONTEND = _FRONTEND_DIR / "index.html"


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
    npi: str = Field(default="", max_length=30)
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


class ContactLookupControlIn(BaseModel):
    paused: bool


class HaloBackfillProfileIn(BaseModel):
    profile_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=3, max_length=200)
    location: str = Field(min_length=2, max_length=200)


class HaloBackfillBatchIn(BaseModel):
    run_id: str = Field(min_length=8, max_length=80)
    profiles: list[HaloBackfillProfileIn] = Field(min_length=1, max_length=100)


class NexusDeliverySummaryIn(BaseModel):
    candidate_ids: list[int] = Field(min_length=1, max_length=100)


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
    message: str = Field(min_length=1, max_length=420)
    request_id: str = Field(default="", max_length=100)


class SmsAssignIn(BaseModel):
    recruiter_user_id: str = Field(min_length=1, max_length=200)


class HaloScopeIn(BaseModel):
    user_ids: list[str] = Field(default_factory=list, max_length=1000)
    all_users: bool = False


class HaloDeviceActionIn(HaloScopeIn):
    actor_user_id: str = Field(min_length=1, max_length=80)


class HaloConversationAssignmentIn(HaloScopeIn):
    actor_user_id: str = Field(min_length=1, max_length=80)
    recruiter_user_id: str = Field(min_length=1, max_length=80)
    recruiter_email: str = Field(default="", max_length=320)
    recruiter_name: str = Field(default="", max_length=320)


class HaloConversationReplyIn(HaloScopeIn):
    actor_user_id: str = Field(min_length=1, max_length=80)
    actor_name: str = Field(default="", max_length=320)
    message: str = Field(min_length=1, max_length=500)
    sender_number: str = Field(min_length=7, max_length=40)
    zoom_user_id: str = Field(min_length=1, max_length=80)
    request_id: str = Field(default="", max_length=120)


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
        # One approved usable contact channel is enough to retain the matched
        # Indeed resume. Masked provider text is not exposed as an email, but a
        # valid phone beside that text still makes the result eligible.
        "resume_required": bool(found and (emails or phones)),
        # This is deliberately vendor-neutral. It is emitted only when a
        # trusted contact was accepted on the Facebook "From" retry.
        "location_match": location_match,
    }


def _public_candidate(candidate: dict | None, actor: dict | None = None) -> dict:
    projected = contact_access.project_candidate(candidate)
    identity = actor or {"sub": "local", "delivery_targets": {}}
    ats = ats_routing.stored(
        candidate,
        str(identity.get("sub") or "local"),
        ats_routing.destination_for(identity),
    )
    allowed = (
        "id", "name", "location", "job_id", "stage", "fit_score",
        "emails", "phones", "phone_contacts", "addresses", "enrich_status",
        "notes", "created", "updated", "identity_status",
    )
    public = {key: projected.get(key) for key in allowed if key in projected}
    public["records_available"] = bool(
        str(projected.get("contact_source") or "") == "quick_sourcer"
    )
    public["ats_destination"] = ats.get("destination") or "nexus"
    # Keep fields for older extension builds, but no longer expose external ATS
    # duplicate checks or block contact visibility based on their results.
    public["ats_eligibility"] = {}
    public["nexus_eligibility"] = {}
    return public


def _public_resume(resume: dict | None) -> dict:
    """Expose only fields the side panel needs to retrieve a stored resume."""
    source = resume or {}
    allowed = (
        "id", "candidate_id", "filename", "mime_type", "size", "created",
        "deduplicated", "contact_sheet_embedded", "contacts_saved",
        "ceipal_sync_status",
    )
    return {key: source.get(key) for key in allowed if key in source}


@app.get("/", response_class=HTMLResponse)
def home():
    if _FRONTEND.exists():
        return FileResponse(str(_FRONTEND))
    return HTMLResponse("<h1>Medhunt Sourcing Assistant API</h1>")


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


@app.post("/internal/halo/api-monitor")
def halo_api_monitor():
    status = quick_sourcer_client.status()
    queue = store.contact_lookup_queue_counts()
    current_request_id = (
        "LQ-" + hashlib.sha256(queue["processing_job_key"].encode()).hexdigest()[:12].upper()
        if queue.get("processing_job_key") else ""
    )
    return {
        "service": "medhunt1",
        "lookup_provider": {
            "enabled": bool(status.get("enabled")),
            "configured": bool(status.get("configured")),
            "search_pool": str(status.get("search_pool") or ""),
            **dict(status.get("requests") or {}),
            "queued": max(0, queue["queued"] - queue["backfill_queued"]),
            "processing": max(0, queue["processing"] - queue["backfill_processing"]),
            "backfill_queued": queue["backfill_queued"],
            "backfill_processing": queue["backfill_processing"],
            "concurrency_limit": config.CONTACT_LOOKUP_MAX_CONCURRENT,
            "backfill_concurrency_limit": config.CONTACT_LOOKUP_BACKFILL_MAX_CONCURRENT,
            "per_user_limit": store.contact_lookup_user_limit(),
            "per_user_limit_timezone": "America/Los_Angeles",
            "per_user_unlimited_after": "16:00",
            "per_user_unlimited_weekends": ["Saturday", "Sunday"],
            "current_request_id": current_request_id,
        },
    }


@app.post("/internal/halo/contact-backfill")
def enqueue_halo_contact_backfill(body: HaloBackfillBatchIn):
    """Stage Halo Neon profiles in the shared low-priority lookup queue."""
    candidate_ids: list[int] = []
    external_refs: dict[int, str] = {}
    for profile in body.profiles:
        existing = store.get_candidate_by_source("halo_neon", profile.profile_id)
        if existing:
            candidate_id = int(existing["id"])
            store.update_candidate(
                candidate_id, name=profile.name.strip(),
                location=profile.location.strip(), hometown=profile.location.strip(),
            )
        else:
            candidate_id = int(store.add_candidate(
                profile.name, profile.location, source="halo_neon",
                source_id=profile.profile_id, hometown=profile.location,
            ))
        candidate_ids.append(candidate_id)
        external_refs[candidate_id] = profile.profile_id
    items = store.enqueue_contact_lookup_jobs(
        body.run_id, candidate_ids, "halo-neon-backfill",
        job_source="halo_backfill", priority=10,
        external_refs=external_refs, bypass_user_limit=True,
    )
    return {
        "status": "queued", "run_id": body.run_id,
        "items": [
            {
                "profile_id": item.get("external_ref"),
                "candidate_id": item.get("candidate_id"),
                "status": item.get("status"),
            }
            for item in items
        ],
    }


@app.post("/nexus/delivery-summary")
def nexus_delivery_summary(body: NexusDeliverySummaryIn, request: Request):
    actor = _request_user(request)
    if ats_routing.destination_for(actor) != "nexus":
        return {
            "selected": len(set(body.candidate_ids)), "uploaded": 0,
            "pending": 0, "already_in_nexus": 0,
            "waiting_for_resume": 0, "not_uploaded": 0,
            "enabled": False, "destination": "ceipal", "items": [],
        }
    waiting_resume_ids = []
    if nexus_delivery.enabled_for(str(actor.get("sub") or "local")):
        candidates = store.get_candidates(body.candidate_ids)
        for projected in contact_access.project_candidates(candidates):
            if (
                projected.get("contacts_trusted") is True
                and (projected.get("emails") or projected.get("phones"))
            ):
                waiting_resume_ids.append(int(projected["id"]))
    return store.nexus_delivery_summary(
        body.candidate_ids, waiting_resume_ids=waiting_resume_ids,
    )


@app.post("/contact-lookup/queue")
def enqueue_contact_lookups(body: ContactLookupBatchIn, request: Request):
    actor = _request_user(request)
    if not body.confirmed:
        raise HTTPException(400, "Confirm the selected candidates before queueing lookup.")
    user_id = str(actor.get("sub") or "local")
    try:
        items = store.enqueue_contact_lookup_jobs(
            body.run_id, body.candidate_ids, user_id,
            delivery_target=ats_routing.destination_for(actor),
            per_user_limit=healthboard_auth.medhunt_contact_lookup_limit(user_id),
        )
    except store.ContactLookupQueueLimitError as exc:
        raise HTTPException(
            429,
            f"{exc} You currently have {exc.active} active and requested {exc.requested} more.",
        ) from exc
    return {"status": "queued", "run_id": body.run_id, "items": [
        _public_contact_lookup_job(item) for item in items
    ]}


@app.get("/contact-lookup/queue/control")
def contact_lookup_queue_control(request: Request):
    actor = _request_user(request)
    user_id = str(actor.get("sub") or "local")
    limit = store.contact_lookup_user_limit(
        limit_override=healthboard_auth.medhunt_contact_lookup_limit(user_id),
    )
    return {
        "paused": store.contact_lookup_paused(str(actor.get("sub") or "local")),
        "selection_limit": limit,
        "limit_timezone": "America/Los_Angeles",
        "unlimited_after": "16:00",
        "unlimited_weekends": ["Saturday", "Sunday"],
    }


@app.post("/contact-lookup/queue/control")
def update_contact_lookup_queue_control(body: ContactLookupControlIn, request: Request):
    actor = _request_user(request)
    result = store.set_contact_lookup_paused(
        str(actor.get("sub") or "local"), body.paused,
    )
    result["selection_limit"] = store.contact_lookup_user_limit(
        limit_override=healthboard_auth.medhunt_contact_lookup_limit(
            str(actor.get("sub") or "local"),
        ),
    )
    result["limit_timezone"] = "America/Los_Angeles"
    result["unlimited_after"] = "16:00"
    result["unlimited_weekends"] = ["Saturday", "Sunday"]
    return result


@app.post("/contact-lookup/queue/status")
def contact_lookup_queue_status(body: ContactLookupBatchIn, request: Request):
    actor = _request_user(request)
    owner = str(actor.get("sub") or "local")
    data = store.list_contact_lookup_jobs(
        body.run_id, body.candidate_ids,
        requested_by=owner,
    )
    return {**data, "paused": store.contact_lookup_paused(owner), "items": [
        _public_contact_lookup_job(item) for item in data["items"]
    ]}


def _public_contact_lookup_job(item: dict) -> dict:
    request_id = "LQ-" + hashlib.sha256(
        str(item.get("job_key") or item.get("id") or "").encode()
    ).hexdigest()[:12].upper()
    return {
        "request_id": request_id,
        **{
            key: item.get(key)
            for key in ("candidate_id", "status", "attempts", "position", "result")
        },
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
        return healthboard_auth.verify_code(
            body.email.strip().lower(), body.code, body.challenge,
            client_ip=_request_client_ip(request),
        )
    except Exception as exc:
        logging.getLogger("medhunt.auth").info(
            "Healthcareboard code verification was rejected (%s).", type(exc).__name__,
        )
        raise HTTPException(401, "Invalid or expired sign-in code.") from exc


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


def _halo_conversation_in_scope(conversation: dict, scope: HaloScopeIn) -> bool:
    if scope.all_users:
        return True
    users = {str(value) for value in scope.user_ids}
    return bool(users & {
        str(conversation.get("initiated_by") or ""),
        str(conversation.get("assigned_recruiter_id") or ""),
    })


def _halo_conversation_public(conversation: dict, *, include_messages=False) -> dict:
    if not conversation:
        return {}
    fields = (
        "id", "candidate_id", "nexus_candidate_id", "candidate_name",
        "candidate_phone", "initiated_by", "assigned_recruiter_id",
        "assigned_recruiter_email", "assigned_recruiter_name", "status",
        "created", "updated", "last_message_at", "has_reply", "last_reply_at",
        "last_outbound_at", "last_outbound_user_id",
    )
    result = {key: conversation.get(key) for key in fields if key in conversation}
    if include_messages:
        result["messages"] = [{
            key: message.get(key) for key in (
                "id", "direction", "body", "status", "sender_user_id",
                "sender_name", "sender_number", "created", "updated",
            ) if key in message
        } for message in conversation.get("messages") or []]
    return result


@app.post("/internal/halo/devices")
def halo_devices(scope: HaloScopeIn):
    # Medhunt1 uses Halo email-code sessions and intentionally has no separate
    # device approval registry. Keep the Halo dashboard contract explicit.
    return {"items": [], "multi_device_accounts": []}


@app.post("/internal/halo/devices/{device_id}/approve")
def halo_approve_device(device_id: int, body: HaloDeviceActionIn):
    raise HTTPException(404, "Device request not found.")


@app.post("/internal/halo/conversations")
def halo_conversations(scope: HaloScopeIn):
    items = store.list_sms_conversations(
        user_ids=scope.user_ids,
        include_all=scope.all_users,
    )
    return {"items": [_halo_conversation_public(item) for item in items]}


@app.post("/internal/halo/conversations/{conversation_id}")
def halo_conversation(conversation_id: int, scope: HaloScopeIn):
    conversation = store.get_sms_conversation(conversation_id)
    if not conversation:
        raise HTTPException(404, "SMS conversation not found.")
    if not _halo_conversation_in_scope(conversation, scope):
        raise HTTPException(403, "Conversation is outside the requested organization scope.")
    return _halo_conversation_public(conversation, include_messages=True)


@app.post("/internal/halo/conversations/{conversation_id}/assign")
def halo_assign_conversation(conversation_id: int, body: HaloConversationAssignmentIn):
    conversation = store.get_sms_conversation(conversation_id)
    if not conversation:
        raise HTTPException(404, "SMS conversation not found.")
    if not _halo_conversation_in_scope(conversation, body):
        raise HTTPException(403, "Conversation is outside the requested organization scope.")
    if conversation.get("status") == "opted_out":
        raise HTTPException(409, "An opted-out candidate cannot be reassigned for outreach.")
    messages = conversation.get("messages") or []
    last_reply = max((float(message.get("created") or 0) for message in messages
                      if message.get("direction") == "inbound"), default=0)
    last_outbound = max((float(message.get("created") or 0) for message in messages
                         if message.get("direction") == "outbound"
                         and message.get("status") in {"accepted", "sent", "delivered"}),
                        default=0)
    if not last_reply:
        raise HTTPException(409, "Wait for a candidate reply before assigning.")
    if last_outbound > last_reply:
        raise HTTPException(409, "This conversation has already been answered.")
    conversation = store.update_sms_conversation(
        conversation_id,
        assigned_recruiter_id=body.recruiter_user_id,
        assigned_recruiter_email=body.recruiter_email,
        assigned_recruiter_name=body.recruiter_name,
    )
    return {"conversation": _halo_conversation_public(conversation, include_messages=True)}


@app.post("/internal/halo/conversations/{conversation_id}/reply")
def halo_reply_to_conversation(conversation_id: int, body: HaloConversationReplyIn):
    conversation = store.get_sms_conversation(conversation_id)
    if not conversation:
        raise HTTPException(404, "SMS conversation not found.")
    if not _halo_conversation_in_scope(conversation, body):
        raise HTTPException(403, "Conversation is outside the requested organization scope.")
    if conversation.get("status") == "opted_out":
        raise HTTPException(409, "This candidate opted out of SMS.")
    messages = conversation.get("messages") or []
    last_reply = max((float(message.get("created") or 0) for message in messages
                      if message.get("direction") == "inbound"), default=0)
    last_outbound = max((float(message.get("created") or 0) for message in messages
                         if message.get("direction") == "outbound"
                         and message.get("status") in {"accepted", "sent", "delivered"}),
                        default=0)
    if not last_reply:
        raise HTTPException(409, "Wait for a candidate reply before replying.")
    if last_outbound > last_reply:
        raise HTTPException(409, "This candidate has already received a reply to their latest message.")
    text = body.message.strip()
    if not text:
        raise HTTPException(422, "Reply cannot be blank.")
    request_id = body.request_id.strip() or uuid.uuid4().hex
    message, created = store.create_sms_message(
        conversation_id, "outbound", text, request_id=request_id,
        sender_user_id=body.actor_user_id, sender_name=body.actor_name,
        sender_number=body.sender_number, zoom_user_id=body.zoom_user_id,
    )
    if created:
        try:
            result = zoom_sms.send_sms(
                conversation["candidate_phone"], text,
                sender_number=body.sender_number,
                sender_user_id=body.zoom_user_id,
            )
            message_id = str(
                result.get("message_id") or result.get("id") or result.get("sid") or ""
            )
            store.update_sms_message(message["id"], status="accepted", zoom_message_id=message_id)
        except zoom_sms.ZoomSmsError as exc:
            store.update_sms_message(
                message["id"], status="failed", failure_reason=str(exc),
            )
            raise HTTPException(502, str(exc)) from exc
    conversation = store.get_sms_conversation(conversation_id)
    return _halo_conversation_public(conversation, include_messages=True)


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
    npi = re.sub(r"\D", "", body.npi or "")
    if len(npi) == 10:
        add_evidence_line(evidence_lines, "NPI", npi)
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


def _import_profile(body: ProfileImportIn, actor: dict | None = None):
    row = _profile_row(body)
    if row["job_id"] is not None and not store.get_job(row["job_id"]):
        raise HTTPException(404, "job not found")
    result = store.upsert_candidate_profiles([row])[0]
    return {**result, "candidate": _public_candidate(result["candidate"], actor)}


@app.post("/candidates/import")
def import_candidate(body: ProfileImportIn, request: Request):
    actor = _request_user(request)
    return _import_profile(body, actor)


@app.post("/candidates/import/batch")
def import_candidate_batch(body: ProfileBatchImportIn, request: Request):
    actor = _request_user(request)
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
            "candidate": _public_candidate(result["candidate"], actor),
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
def candidates(request: Request, job_id: int | None = None, stage: str | None = None):
    actor = _request_user(request)
    return [
        _public_candidate(row, actor)
        for row in store.list_candidates(job_id=job_id, stage=stage)
    ]

@app.get("/candidates/{cid}")
def candidate(cid: int, request: Request):
    actor = _request_user(request)
    c = store.get_candidate(cid)
    if not c:
        raise HTTPException(404, "not found")
    projected = _public_candidate(c, actor)
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


def _upload_ceipal_candidate(candidate_id: int, resume_id: int, user_id: str) -> str:
    return ceipal_delivery.queue_resume(candidate_id, resume_id, user_id)


def _store_resume_pdf(cid: int, filename: str, data: bytes, user_id: str = "local"):
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    contactable = contact_access.project_candidate(candidate)
    # Read the original PDF before the Medhunt contact page is prepended. Text
    # PDFs stay local and fast; only sparse/image-only pages invoke local OCR.
    # Extraction failure is fail-open for capture and fail-closed for identity:
    # no low-confidence value can replace captured platform data.
    extraction = resume_extraction.extract(data, candidate)
    accepted_resume_fields = extraction.get("accepted") or {}
    contact_sheet_candidate = dict(contactable)
    if isinstance(accepted_resume_fields, dict):
        if not str(contact_sheet_candidate.get("name") or "").strip():
            contact_sheet_candidate["name"] = accepted_resume_fields.get("full_name") or ""
        if not str(contact_sheet_candidate.get("location") or "").strip():
            contact_sheet_candidate["location"] = accepted_resume_fields.get("location") or ""
    destination = ats_routing.stored(candidate, user_id).get("destination") or "nexus"
    ceipal_sync_status = "disabled"
    nexus_contact_ready = bool(
        destination == "nexus"
        and nexus_delivery.enabled_for(user_id)
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
        nexus_delivery_job = (
            store.enqueue_nexus_delivery(
                cid, existing["id"], checksum, requested_by=user_id,
            )
            if queue_nexus else None
        )
        if destination == "ceipal":
            ceipal_sync_status = _upload_ceipal_candidate(cid, int(existing["id"]), user_id)
        result = {
            **existing,
            "contact_sheet_embedded": contact_sheet_embedded,
            "contacts_saved": bool(contactable.get("phones") or contactable.get("emails")),
            "deduplicated": True,
            "nexus_sync_status": (
                nexus_delivery_job.get("status") if nexus_delivery_job else nexus_skip_status
            ),
        }
        if destination == "ceipal":
            result["ceipal_sync_status"] = ceipal_sync_status
        return result
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
            requested_by=user_id,
            extraction=extraction,
            **uploaded,
        )
    else:
        resume = store.attach_resume(
            cid, filename, data, checksum_sha256=checksum,
            queue_nexus=queue_nexus,
            requested_by=user_id,
            extraction=extraction,
        )
    if destination == "ceipal":
        ceipal_sync_status = _upload_ceipal_candidate(cid, int(resume["id"]), user_id)
    result = {
        **resume,
        "contact_sheet_embedded": contact_sheet_embedded,
        "contacts_saved": bool(contactable.get("phones") or contactable.get("emails")),
    }
    if destination == "ceipal":
        result["ceipal_sync_status"] = ceipal_sync_status
    if not queue_nexus:
        result["nexus_sync_status"] = nexus_skip_status
    return result


@app.get("/session")
def authenticated_session():
    """Confirm that this extension copy shares the backend's local token."""
    return {"status": "ok"}


@app.post("/candidates/{cid}/resume/from-download")
def attach_downloaded_resume(cid: int, body: ResumeDownloadIn, request: Request):
    if not store.get_candidate(cid):
        raise HTTPException(404, "candidate not found")
    actor = _request_user(request)
    ats_routing.set_candidate_target(
        cid, ats_routing.destination_for(actor), str(actor.get("sub") or ""),
    )
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
    resume = _store_resume_pdf(cid, filename, data, str(actor.get("sub") or "local"))
    return {"attached": True, "resume": _public_resume(resume)}


@app.post("/candidates/{cid}/resume/from-browser")
def attach_captured_resume(cid: int, body: ResumeCaptureIn, request: Request):
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    actor = _request_user(request)
    ats_routing.set_candidate_target(
        cid, ats_routing.destination_for(actor), str(actor.get("sub") or ""),
    )
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
    resume = _store_resume_pdf(cid, filename, data, str(actor.get("sub") or "local"))
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
    current = contact_access.project_candidate(store.get_candidate(cid))
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
def _route_enriched_lookup(candidate_id: int, lookup: dict, actor: dict) -> dict:
    destination = ats_routing.destination_for(actor)
    user_id = str(actor.get("sub") or "local")
    ats_routing.set_candidate_target(candidate_id, destination, user_id)
    result = dict(lookup or {})
    return {**result, "ats_destination": destination}


def _quick_sourcer_selected() -> bool:
    """The public Medhunt product has one backend contact-lookup path."""
    return True


def _record_enrichment(request: Request | None, candidate_id: int, status: str,
                       *, provider: str, run_id: str = "") -> None:
    actor = _request_user(request)
    subject = str(actor.get("sub") or "local")
    normalized_status = str(status or "unknown")
    store.record_enrichment_event(
        subject, candidate_id, normalized_status,
        provider=provider, run_id=run_id,
    )
    token = str(getattr(
        getattr(request, "state", None), "healthboard_extension_token", "",
    ) or "")
    if not token or not healthboard_auth.enabled():
        return
    source = str((store.get_candidate(candidate_id) or {}).get("source") or provider)
    identity = f"{subject}|{run_id or time.time_ns()}|{candidate_id}|{normalized_status}"
    event_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
    try:
        healthboard_auth.report_enrichment(
            token,
            event_id=event_id,
            candidate_id=candidate_id,
            status=normalized_status,
            source=source,
            run_id=run_id,
        )
    except Exception as exc:
        # The paid/contact result is already complete. Analytics delivery must
        # never discard it; the local event remains available for reconciliation.
        logging.getLogger("medhunt.analytics").warning(
            "Healthcareboard enrichment event delivery failed (%s).",
            type(exc).__name__,
        )


@app.post("/candidates/{cid}/contact-lookup")
def enrich_one(cid: int, request: Request = None):
    actor = _request_user(request)
    if store.contact_lookup_paused(str(actor.get("sub") or "local")):
        raise HTTPException(409, "Contact lookup requests are paused. Resume them in the extension.")
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    result = quick_sourcer_client.lookup_candidate(cid)
    result = _route_enriched_lookup(cid, result, actor)
    _record_enrichment(
        request, cid,
        result.get("status") or result.get("enrich_status") or "unknown",
        provider="quick_sourcer",
    )
    if (
        result.get("status") != "blocked"
        and result.get("ats_destination", "nexus") == "nexus"
    ):
        nexus_delivery.queue_latest_resume_if_ready(cid, str(actor.get("sub") or "local"))
    return result


@app.post("/candidates/{cid}/identity/review")
def review_candidate_identity(cid: int, body: IdentityReviewIn, request: Request):
    actor = _request_user(request)
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
    return _public_candidate(store.get_candidate(cid), actor)


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


def _quick_sourcer_lookup_batch(body: ContactLookupBatchIn, actor: dict) -> dict:
    """Look each selected candidate up through Quick Sourcer, one at a time.

    The external API drives a real browser per person, so these cannot be
    parallelised; the side panel therefore sends small chunks and shows
    per-candidate progress. Every answer is returned exactly as the API
    delivered it — a miss is a miss, not a policy hold.
    """
    results = {}
    for candidate_id in dict.fromkeys(body.candidate_ids):
        candidate = store.get_candidate(candidate_id)
        if not candidate:
            results[str(candidate_id)] = {
                "status": "failed", "emails": [], "phones": [],
                "phone_contacts": [], "resume_required": False, "location_match": None,
            }
            continue
        try:
            lookup = quick_sourcer_client.lookup_candidate(candidate_id)
            lookup = _route_enriched_lookup(candidate_id, lookup, actor)
            results[str(candidate_id)] = lookup
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
        lookup = results[str(candidate_id)]
        if (
            lookup.get("status") == "found"
            and lookup.get("ats_destination", "nexus") == "nexus"
        ):
            try:
                nexus_delivery.queue_latest_resume_if_ready(
                    candidate_id, str(actor.get("sub") or "local"),
                )
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
    actor = _request_user(request)
    if store.contact_lookup_paused(str(actor.get("sub") or "local")):
        raise HTTPException(409, "Contact lookup requests are paused. Resume them in the extension.")
    """Vendor-neutral browser endpoint with a deliberately minimal response."""
    result = _quick_sourcer_lookup_batch(body, actor)
    for candidate_id, item in (result.get("results") or {}).items():
        _record_enrichment(
            request, int(candidate_id), item.get("status") or "unknown",
            provider="quick_sourcer", run_id=body.run_id,
        )
    return result


@app.post("/enrich/batch")
def enrich_batch(job_id: int | None = None):
    raise HTTPException(410, "Use the candidate contact lookup endpoint.")


# ---- Zoom Phone SMS ----
def _candidate_sms_phone(candidate: dict, supplied: str) -> str:
    projected = contact_access.project_candidate(candidate)
    selected = phone_policy.messaging_phone_detail(projected)
    phone = str((selected or {}).get("value") or "").strip()
    if not phone:
        raise HTTPException(400, "No verified phone number is available for this candidate.")
    return phone


@app.get("/messaging/status")
def messaging_status(request: Request):
    user = _request_user(request)
    zoom_sender = _resolve_zoom_sms_sender(request, strict=False) if zoom_sms.enabled() else None
    return {
        "enabled": zoom_sms.enabled(),
        "provider": "zoom_phone",
        "sender_configured": bool(zoom_sender),
        "sender_number": str((zoom_sender or {}).get("sender_number") or ""),
        "zoom_sender_required": bool(zoom_sms.enabled() and not zoom_sender),
        "reply_notifications_configured": bool(
            config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN and healthboard_auth.enabled()
        ),
    }


def _resolve_zoom_sms_sender(request: Request, *, strict: bool) -> dict | None:
    if healthboard_auth.enabled():
        token = str(getattr(request.state, "healthboard_extension_token", "") or "")
        try:
            sender = healthboard_auth.medhunt_zoom_sms_sender(token)
            if sender and sender.get("employer_id"):
                sender["access_token"] = healthboard_auth.organization_zoom_access(
                    str(sender["employer_id"]),
                )
                if not sender["access_token"]:
                    raise RuntimeError("The organization Zoom connection is unavailable")
            return sender
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            try:
                detail = str(exc.response.json().get("detail") or "")
            except (ValueError, AttributeError):
                detail = ""
            logging.getLogger("medhunt.zoom_sms").warning(
                "Halo rejected the recruiter Zoom sender assignment (%s).", status,
            )
            if strict and status in {401, 403, 404, 409, 422}:
                raise HTTPException(
                    status,
                    detail or "Your Halo administrator has not enabled Zoom messaging for your account.",
                ) from exc
            if strict:
                raise HTTPException(
                    503,
                    "Could not verify your Zoom sender assignment. Try again or contact your Halo administrator.",
                ) from exc
            return None
        except Exception as exc:
            logging.getLogger("medhunt.zoom_sms").warning(
                "Could not load the recruiter Zoom sender assignment (%s).",
                type(exc).__name__,
            )
            if strict:
                raise HTTPException(
                    503,
                    "Could not verify your Zoom sender assignment. Try again or contact your Halo administrator.",
                ) from exc
            return None
    if config.ZOOM_SMS_SENDER_NUMBER and config.ZOOM_SMS_SENDER_USER_ID:
        return {
            "sender_number": config.ZOOM_SMS_SENDER_NUMBER,
            "zoom_user_id": config.ZOOM_SMS_SENDER_USER_ID,
        }
    return None


@app.get("/candidates/{candidate_id}/sms-preview")
def sms_preview(candidate_id: int, phone: str, request: Request):
    _request_user(request)
    candidate = store.get_candidate(candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found.")
    verified_phone = _candidate_sms_phone(candidate, phone)
    return {
        "phone": verified_phone,
        "opted_out": store.is_dnc(verified_phone),
        "already_contacted": store.sms_candidate_contacted(candidate_id),
    }


@app.post("/messaging/sms")
def send_sms(body: SmsSendIn, request: Request):
    user = _request_user(request)
    if not zoom_sms.enabled():
        raise HTTPException(503, "Zoom Phone SMS is not configured.")
    zoom_sender = _resolve_zoom_sms_sender(request, strict=True)
    if not zoom_sender:
        raise HTTPException(
            409,
            "Ask your Halo administrator to assign your Zoom Phone number before sending.",
        )
    candidate = store.get_candidate(body.candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found.")
    phone = _candidate_sms_phone(candidate, body.phone)
    if store.is_dnc(phone):
        raise HTTPException(409, "This number has opted out and cannot be messaged.")
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Write a message before sending.")
    if len(text.split()) > 29:
        raise HTTPException(422, "SMS messages are limited to 29 words.")
    if "reply stop" not in text.casefold():
        text = f"{text}\n\nMedhunt recruiting. Reply STOP to opt out."
    if len(text) > 500:
        raise HTTPException(400, "Message is too long after the required opt-out notice.")
    request_id = body.request_id.strip() or uuid.uuid4().hex
    existing_message = store.get_sms_message_by_request_id(request_id)
    if existing_message:
        existing_conversation = store.get_sms_conversation(
            existing_message["conversation_id"]
        )
        same_request = bool(
            existing_conversation
            and int(existing_conversation.get("candidate_id") or 0) == body.candidate_id
            and str(existing_conversation.get("phone_key") or "") == store.contact_key(phone)
            and str(existing_conversation.get("initiated_by") or "") == str(user.get("sub") or "")
            and str(existing_message.get("body") or "") == text
        )
        if not same_request:
            raise HTTPException(409, "This SMS request ID has already been used.")
        return {"conversation": existing_conversation, "message": existing_message}
    if store.sms_candidate_contacted(body.candidate_id):
        raise HTTPException(409, "This candidate has already received SMS outreach.")
    if not store.claim_sms_outreach(body.candidate_id, phone):
        raise HTTPException(409, "This candidate has already received SMS outreach.")
    conversation = store.get_or_create_sms_conversation(
        body.candidate_id, phone,
        candidate_name=str(candidate.get("name") or ""),
        initiated_by=str(user.get("sub") or ""),
        sender_number=str(zoom_sender["sender_number"]),
        sender_user_id=str(zoom_sender["zoom_user_id"]),
    )
    message, created = store.create_sms_message(
        conversation["id"], "outbound", text, request_id=request_id,
        sender_user_id=str(user.get("sub") or ""),
        sender_name=str(user.get("email") or user.get("name") or ""),
        sender_number=str(zoom_sender["sender_number"]),
        zoom_user_id=str(zoom_sender["zoom_user_id"]),
    )
    if not created:
        return {"conversation": store.get_sms_conversation(conversation["id"]), "message": message}
    try:
        result = zoom_sms.send_sms(
            phone, text,
            sender_number=str(zoom_sender["sender_number"]),
            sender_user_id=str(zoom_sender["zoom_user_id"]),
            access_token=str(zoom_sender.get("access_token") or ""),
        )
        zoom_message_id = str(result.get("message_id") or result.get("id") or "")
        session_id = str(result.get("session_id") or "")
        message = store.update_sms_message(
            message["id"], status="accepted", zoom_message_id=zoom_message_id,
        )
        store.update_sms_outreach_claim(body.candidate_id, "sent")
        if session_id:
            conversation = store.update_sms_conversation(
                conversation["id"], zoom_session_id=session_id,
            )
        store.set_stage(body.candidate_id, "contacted")
        try:
            healthboard_auth.report_message_event(
                event_id=f"sent:{request_id}", conversation=conversation,
                event_type="sent", message_preview=text,
            )
        except Exception:
            logging.getLogger("medhunt.healthboard").exception("Send reporting failed")
    except zoom_sms.ZoomSmsError as exc:
        store.update_sms_message(message["id"], status="failed", failure_reason=str(exc))
        store.update_sms_outreach_claim(body.candidate_id, "failed")
        raise HTTPException(502, str(exc)) from exc
    return {"conversation": store.get_sms_conversation(conversation["id"]), "message": message}


@app.get("/messaging/conversations")
def conversations(request: Request):
    user = _request_user(request)
    return {"items": store.list_sms_conversations(str(user.get("sub") or ""))}


@app.get("/messaging/conversations/{conversation_id}")
def conversation(conversation_id: int, request: Request):
    user = _request_user(request)
    result = store.get_sms_conversation(conversation_id)
    if not result:
        raise HTTPException(404, "Conversation not found.")
    if str(user.get("role") or "").casefold() not in {"admin", "owner", "super_admin"} and str(user.get("sub")) not in {
        str(result.get("initiated_by") or ""), str(result.get("assigned_recruiter_id") or ""),
    }:
        raise HTTPException(403, "Conversation access denied.")
    return result


@app.get("/messaging/recruiters")
def messaging_recruiters(request: Request):
    _request_user(request)
    token = getattr(request.state, "healthboard_extension_token", "")
    if not token:
        return {"items": []}
    try:
        return {"items": healthboard_auth.list_recruiters(token)}
    except Exception as exc:
        logging.getLogger("medhunt.healthboard").warning("Recruiter lookup failed (%s).", type(exc).__name__)
        raise HTTPException(502, "Recruiter list is temporarily unavailable.") from exc


@app.post("/messaging/conversations/{conversation_id}/assign")
def assign_conversation(conversation_id: int, body: SmsAssignIn, request: Request):
    _request_user(request)
    current = store.get_sms_conversation(conversation_id)
    if not current:
        raise HTTPException(404, "Conversation not found.")
    token = getattr(request.state, "healthboard_extension_token", "")
    try:
        assigned = healthboard_auth.assign_conversation(
            token, conversation=current, recruiter_user_id=body.recruiter_user_id,
        )
    except Exception as exc:
        raise HTTPException(502, "Healthboard could not assign this conversation.") from exc
    result = store.update_sms_conversation(
        conversation_id,
        status="assigned",
        assigned_recruiter_id=str(assigned.get("user_id") or body.recruiter_user_id),
        assigned_recruiter_email=str(assigned.get("email") or ""),
        assigned_recruiter_name=str(assigned.get("name") or ""),
    )
    return {"conversation": result, "assignment": assigned}


@app.post("/integrations/zoom/webhook")
async def zoom_webhook(request: Request):
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid webhook payload.") from exc
    if payload.get("event") == "endpoint.url_validation":
        plain = str((payload.get("payload") or {}).get("plainToken") or "")
        if not config.ZOOM_WEBHOOK_SECRET_TOKEN or not plain:
            raise HTTPException(403, "Webhook validation is not configured.")
        return {"plainToken": plain, "encryptedToken": zoom_sms.validation_token(plain)}
    if not zoom_sms.validate_webhook(
        request.headers.get("x-zm-request-timestamp", ""), raw,
        request.headers.get("x-zm-signature", ""),
    ):
        raise HTTPException(403, "Invalid Zoom webhook signature.")
    event_type = str(payload.get("event") or "")
    obj = ((payload.get("payload") or {}).get("object") or {})
    message_id = str(obj.get("message_id") or "")
    session_id = str(obj.get("session_id") or "")
    event_key = f"{event_type}:{message_id or payload.get('event_ts') or hashlib.sha256(raw).hexdigest()}"
    if not store.claim_sms_webhook(event_key, event_type):
        return {"received": True, "duplicate": True}
    sender_phone = str((obj.get("sender") or {}).get("phone_number") or "")
    recipients = obj.get("to_members") or []
    recipient_phone = str((recipients[0] if recipients else {}).get("phone_number") or "")
    lookup_phone = sender_phone if event_type == "phone.sms_received" else recipient_phone
    current = store.find_sms_conversation(session_id=session_id, phone=lookup_phone)
    if not current:
        return {"received": True, "matched": False}
    if session_id and not current.get("zoom_session_id"):
        current = store.update_sms_conversation(current["id"], zoom_session_id=session_id)
    if event_type == "phone.sms_received":
        text = str(obj.get("message") or "")
        store.create_sms_message(current["id"], "inbound", text, request_id=event_key, status="received")
        current = store.update_sms_conversation(current["id"], status="replied")
        store.set_stage(int(current["candidate_id"]), "replied")
        keyword = text.strip().casefold()
        if keyword in {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}:
            store.record_sms_consent(
                int(current["candidate_id"]), current["candidate_phone"], "opted_out",
                "inbound_sms", f"Zoom webhook {event_key}", captured_by="zoom",
            )
            current = store.update_sms_conversation(current["id"], status="opted_out")
        try:
            healthboard_auth.report_message_event(
                event_id=event_key, conversation=current, event_type="received",
                message_preview=text,
            )
        except Exception:
            logging.getLogger("medhunt.healthboard").exception("Reply reporting failed")
    elif event_type in {"phone.sms_sent", "phone.sms_sent_failed"}:
        failed = bool(obj.get("failure_reason")) or event_type.endswith("failed")
        store.reconcile_outbound_sms(
            current["id"], zoom_message_id=message_id,
            status="failed" if failed else "sent",
            failure_reason=str(obj.get("failure_reason") or ""),
        )
    for opt in obj.get("phone_number_campaign_opt_statuses") or []:
        if str(opt.get("opt_status") or "").casefold() == "opt_out" or int(opt.get("opt_in_status") or 0) == 4:
            store.record_sms_consent(
                int(current["candidate_id"]), current["candidate_phone"], "opted_out",
                "inbound_sms", f"Zoom campaign opt-out {event_key}", captured_by="zoom",
            )
            current = store.update_sms_conversation(current["id"], status="opted_out")
    return {"received": True, "matched": True}

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
def build_professional_profile_resume(cid: int, body: ProfessionalProfileResumeIn, request: Request):
    """Render and store an explicitly labeled public-profile résumé.

    Storage and Nexus delivery intentionally pass through the same audited
    resume path as captured PDFs. Public appointment phone numbers are not
    accepted by this endpoint and therefore cannot become candidate contacts.
    """
    candidate = store.get_candidate(cid)
    if not candidate:
        raise HTTPException(404, "candidate not found")
    actor = _request_user(request)
    user_id = str(actor.get("sub") or "local")
    ats_routing.set_candidate_target(cid, ats_routing.destination_for(actor), user_id)
    candidate_source = str(candidate.get("source") or "").strip().casefold()
    source_rules = {
        "usnews": (
            lambda host, path: host == "health.usnews.com"
            and bool(re.match(r"^/(?:doctors|nurse-practitioners|physician-assistants|dentists)/", path, re.IGNORECASE)),
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
            and bool(re.match(r"^/doctor/[^/?#]+-overview/?$", path, re.IGNORECASE)),
            "WebMD",
        ),
        "npino": (
            lambda host, path: (host == "npino.com" or host.endswith(".npino.com"))
            and bool(re.match(r"^/[^/]+/\d{10}(?:-[^/]+)?/?$", path, re.IGNORECASE)),
            "NPI No.",
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
    # Older published extensions send structured specialty data with the
    # profile PDF but may not include it in the earlier candidate import.
    # Persist it before storing the PDF, which also queues Nexus delivery.
    existing_notes = str(candidate.get("notes") or "")
    existing_lines = {line.strip().casefold() for line in existing_notes.splitlines()}
    specialty_lines = [
        f"Specialty: {' '.join(str(value).split())[:240]}"
        for value in profile["specialties"][:20]
        if str(value).strip()
    ]
    new_lines = [line for line in specialty_lines if line.casefold() not in existing_lines]
    if new_lines:
        store.update_candidate(cid, notes="\n".join([*new_lines, existing_notes])[:20000])
    resume = _store_resume_pdf(
        cid,
        profile_resume.filename(candidate, profile),
        rendered,
        user_id,
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
