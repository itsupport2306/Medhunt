"""HealthBoard-owned authentication and Medhunt activity reporting."""
from __future__ import annotations

import hashlib
import threading
import time

import httpx

from . import config

_CACHE_LOCK = threading.Lock()
_USER_CACHE: dict[str, tuple[float, dict]] = {}


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
                      status: str, source: str = "", platform: str = "",
                      provider: str = "", run_id: str = "",
                      occurred_at: str = "") -> bool:
    response = httpx.post(
        _url("/api/extension/activity/enrichment"),
        headers={"X-Capture-Token": str(token or "").strip()},
        json={
            "event_id": event_id,
            "candidate_id": str(candidate_id),
            "status": status,
            "source": source,
            "platform": platform,
            "provider": provider,
            "run_id": run_id,
            "occurred_at": occurred_at or None,
        },
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    # A duplicate is an acknowledgement: Halo already has this event, so the
    # durable outbox can be cleared without replaying it indefinitely.
    return True


def report_enrichment_service(*, user_id: str, event_id: str, candidate_id: int,
                              status: str, source: str = "", platform: str = "",
                              provider: str = "", run_id: str = "",
                              occurred_at: str = "") -> bool:
    token = config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN
    if not enabled() or not token:
        return False
    response = httpx.post(
        _url("/api/extension/activity/enrichment/service"),
        headers={"X-Medhunt-Service-Token": token},
        json={
            "user_id": user_id, "event_id": event_id,
            "candidate_id": str(candidate_id), "status": status,
            "source": source, "platform": platform,
            "provider": provider, "run_id": run_id,
            "occurred_at": occurred_at or None,
        },
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return True


def medhunt_zoom_sms_sender(token: str) -> dict | None:
    """Read the signed-in recruiter's Zoom sender assignment from Halo."""
    supplied = str(token or "").strip()
    if not enabled() or not supplied:
        return None
    response = httpx.get(
        _url("/api/extension/medhunt/sms-sender"),
        headers={"X-Capture-Token": supplied},
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    # Halo returns 409 when an administrator has not assigned this recruiter
    # a sender, or when their organization assignments disagree.
    if response.status_code == 409:
        return None
    response.raise_for_status()
    payload = response.json()
    number = str(payload.get("sender_number") or "").strip()
    zoom_user_id = str(payload.get("zoom_user_id") or "").strip()
    if not number or not zoom_user_id:
        return None
    return {"sender_number": number, "zoom_user_id": zoom_user_id}
