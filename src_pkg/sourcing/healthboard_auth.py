"""HealthBoard-owned authentication and Medhunt activity reporting."""
from __future__ import annotations

import hashlib
import threading
import time

import httpx

from . import config

_CACHE_LOCK = threading.Lock()
_USER_CACHE: dict[str, tuple[float, dict]] = {}
_LOOKUP_LIMIT_CACHE: dict[str, tuple[float, int]] = {}
_ATS_CONFIGURATION_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}


def enabled() -> bool:
    return bool(config.HEALTHBOARD_BASE_URL)


def _url(path: str) -> str:
    return f"{config.HEALTHBOARD_BASE_URL.rstrip('/')}{path}"


def request_code(email: str, *, client_ip: str = "") -> dict:
    headers = {"X-Forwarded-For": client_ip} if client_ip else None
    response = httpx.post(
        _url("/api/extension/auth/request-code"),
        json={"email": email},
        headers=headers,
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def verify_code(email: str, code: str, challenge: str, *, client_ip: str = "") -> dict:
    headers = {"X-Forwarded-For": client_ip} if client_ip else None
    response = httpx.post(
        _url("/api/extension/auth/verify-code"),
        json={"email": email, "code": code, "challenge": challenge},
        headers=headers,
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def verify_extension_token(token: str) -> dict:
    """Resolve an opaque extension token through HealthBoard with a short cache."""
    supplied = str(token or "").strip()
    if not enabled() or not supplied:
        raise ValueError("HealthBoard extension authentication is unavailable")
    key = hashlib.sha256(supplied.encode()).hexdigest()
    now = time.time()
    with _CACHE_LOCK:
        cached = _USER_CACHE.get(key)
        if cached and cached[0] > now:
            return dict(cached[1])
    response = httpx.get(
        _url("/api/extension/auth/me"),
        headers={"X-Capture-Token": supplied},
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    user = response.json()
    if not user.get("user_id"):
        raise ValueError("HealthBoard did not return a user identity")
    with _CACHE_LOCK:
        _USER_CACHE[key] = (now + config.HEALTHBOARD_AUTH_CACHE_SECONDS, dict(user))
    return user


def report_enrichment(token: str, *, event_id: str, candidate_id: int,
                      status: str, source: str = "", run_id: str = "") -> bool:
    response = httpx.post(
        _url("/api/extension/activity/enrichment"),
        headers={"X-Capture-Token": str(token or "").strip()},
        json={
            "event_id": event_id,
            "candidate_id": str(candidate_id),
            "status": status,
            "source": source,
            "run_id": run_id,
        },
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return bool(response.json().get("recorded", True))


def report_enrichment_service(*, user_id: str, event_id: str, candidate_id: int,
                              status: str, source: str = "", run_id: str = "") -> bool:
    token = config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN
    if not enabled() or not token or not user_id:
        return False
    response = httpx.post(
        _url("/api/extension/activity/enrichment/service"),
        headers={"X-Medhunt-Service-Token": token},
        json={
            "user_id": str(user_id), "event_id": str(event_id),
            "candidate_id": str(candidate_id), "status": str(status),
            "source": str(source), "provider": "quick_sourcer",
            "run_id": str(run_id),
        },
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return bool(response.json().get("recorded", True))


def report_halo_profile_backfill(*, profile_id: str, candidate_id: int,
                                 status: str, result: dict, attempts: int = 0) -> bool:
    """Return a low-priority queue result to Halo's Neon profile record."""
    token = config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN
    if not enabled() or not token or not profile_id:
        raise RuntimeError("Halo backfill callback is not configured.")
    response = httpx.post(
        _url("/api/extension/medhunt/contact-backfill-result"),
        headers={"X-Medhunt-Service-Token": token},
        json={
            "profile_id": str(profile_id), "candidate_id": int(candidate_id),
            "status": str(status), "attempts": int(attempts),
            "emails": list(result.get("emails") or []),
            "phones": list(result.get("phones") or []),
            "phone_contacts": list(result.get("phone_contacts") or []),
        },
        timeout=max(10.0, config.HEALTHBOARD_AUTH_TIMEOUT),
    )
    response.raise_for_status()
    return bool(response.json().get("recorded"))


def medhunt_ceipal_candidate(*, user_id: str, candidate: dict) -> dict:
    """Ask Halo to upload this Ceipal-assigned candidate directly.

    Ceipal secrets stay in Halo's server environment; Medhunt sends only the
    enriched candidate fields and the authenticated Halo user id.
    """
    token = config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN
    if not enabled() or not token:
        raise RuntimeError("Halo service access is not configured for Ceipal routing.")
    response = httpx.post(
        _url("/api/extension/medhunt/ceipal-candidate"),
        headers={"X-Medhunt-Service-Token": token},
        json={"user_id": str(user_id or ""), **dict(candidate or {})},
        timeout=config.MEDHUNT_CEIPAL_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        try:
            detail = str(response.json().get("detail") or "")
        except ValueError:
            detail = ""
        raise RuntimeError(detail or f"Halo Ceipal upload returned HTTP {response.status_code}.")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Halo Ceipal upload returned an invalid response.")
    return payload


def medhunt_contact_lookup_limit(user_id: str) -> int | None:
    """Read the recruiter organization's Quick Sourcer outstanding limit."""
    owner = str(user_id or "").strip()
    token = config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN
    if not owner or not enabled() or not token:
        return None
    now = time.time()
    with _CACHE_LOCK:
        cached = _LOOKUP_LIMIT_CACHE.get(owner)
        if cached and cached[0] > now:
            return cached[1]
    try:
        response = httpx.post(
            _url("/api/extension/medhunt/contact-lookup-limit"),
            headers={"X-Medhunt-Service-Token": token},
            json={"user_id": owner},
            timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
        )
        response.raise_for_status()
        limit = int(response.json().get("per_user_limit"))
        if not 1 <= limit <= 80:
            return None
    except (httpx.HTTPError, TypeError, ValueError):
        with _CACHE_LOCK:
            cached = _LOOKUP_LIMIT_CACHE.get(owner)
            return cached[1] if cached else None
    with _CACHE_LOCK:
        _LOOKUP_LIMIT_CACHE[owner] = (now + 60, limit)
    return limit


def list_recruiters(token: str) -> list[dict]:
    response = httpx.get(
        _url("/api/extension/team/recruiters"),
        headers={"X-Capture-Token": str(token or "").strip()},
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return list(payload.get("items") or [])


def assign_conversation(token: str, *, conversation: dict, recruiter_user_id: str) -> dict:
    response = httpx.post(
        _url("/api/extension/medhunt/conversations/assign"),
        headers={"X-Capture-Token": str(token or "").strip()},
        json={
            "conversation_id": str(conversation.get("id") or ""),
            "candidate_id": str(conversation.get("candidate_id") or ""),
            "nexus_candidate_id": str(conversation.get("nexus_candidate_id") or ""),
            "candidate_name": str(conversation.get("candidate_name") or ""),
            "recruiter_user_id": str(recruiter_user_id or ""),
        },
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def medhunt_zoom_sms_sender(token: str) -> dict | None:
    """Return this signed-in recruiter's Zoom sender assignment from Halo."""
    supplied = str(token or "").strip()
    if not enabled() or not supplied:
        return None
    response = httpx.get(
        _url("/api/extension/medhunt/sms-sender"),
        headers={"X-Capture-Token": supplied},
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    number = str(payload.get("sender_number") or "").strip()
    zoom_user_id = str(payload.get("zoom_user_id") or "").strip()
    return {
        "sender_number": number,
        "zoom_user_id": zoom_user_id,
        "employer_id": str(payload.get("employer_id") or "").strip(),
        "messaging_status": str(payload.get("messaging_status") or "enabled"),
    } if number and zoom_user_id else None


def organization_zoom_access(employer_id: str) -> str:
    """Get a short-lived organization OAuth token over the service channel."""
    if not enabled() or not config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN or not employer_id:
        return ""
    response = httpx.post(
        _url("/api/extension/medhunt/zoom-access"),
        headers={"X-Medhunt-Service-Token": config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN},
        json={"employer_id": employer_id},
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return str(response.json().get("access_token") or "")


def organization_ats_configuration(user_id: str, provider: str) -> dict:
    """Read encrypted-at-rest organization ATS settings over the service channel."""
    owner = str(user_id or "").strip()
    destination = str(provider or "").strip().casefold()
    if not owner or destination not in {"nexus", "ceipal"}:
        return {}
    if not enabled() or not config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN:
        return {}
    key = (owner, destination)
    now = time.time()
    with _CACHE_LOCK:
        cached = _ATS_CONFIGURATION_CACHE.get(key)
        if cached and cached[0] > now:
            return dict(cached[1])
    response = httpx.post(
        _url("/api/extension/medhunt/ats-configuration"),
        headers={"X-Medhunt-Service-Token": config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN},
        json={"user_id": owner, "provider": destination},
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    result = dict(payload.get("settings") or {}) if payload.get("configured") else {}
    if payload.get("managed"):
        result["_managed"] = True
        result["_configured"] = bool(payload.get("configured"))
    with _CACHE_LOCK:
        _ATS_CONFIGURATION_CACHE[key] = (now + 5, dict(result))
    return result


def report_message_event(*, event_id: str, conversation: dict, event_type: str,
                         message_preview: str = "") -> bool:
    """Report asynchronous Zoom activity without exposing a user's session."""
    token = config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN
    if not enabled() or not token:
        return False
    response = httpx.post(
        _url("/api/extension/medhunt/events"),
        headers={"X-Medhunt-Service-Token": token},
        json={
            "event_id": str(event_id),
            "conversation_id": str(conversation.get("id") or ""),
            "candidate_id": str(conversation.get("candidate_id") or ""),
            "nexus_candidate_id": str(conversation.get("nexus_candidate_id") or ""),
            "candidate_name": str(conversation.get("candidate_name") or ""),
            "initiated_by_user_id": str(conversation.get("initiated_by") or ""),
            "assigned_recruiter_user_id": str(conversation.get("assigned_recruiter_id") or ""),
            "event_type": str(event_type),
            "message_preview": str(message_preview or "")[:240],
        },
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return bool(response.json().get("recorded", True))
