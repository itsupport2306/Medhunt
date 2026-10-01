"""Durable delivery of routed Medhunt candidates to CEIPAL Candidate Pass."""
from __future__ import annotations

import re
import threading
import time

import httpx

from . import config, contact_access, store

_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_LOCK = threading.Lock()


def _first(values) -> str:
    if isinstance(values, str):
        return values.strip()
    return str(next((item for item in values or [] if item), "")).strip()


def _note(candidate: dict, label: str) -> str:
    match = re.search(
        rf"(?im)^\s*{re.escape(label)}\s*:\s*([^\r\n]+)",
        str(candidate.get("notes") or ""),
    )
    return " ".join(match.group(1).split()) if match else ""


def _payload(candidate: dict) -> list[dict]:
    projected = contact_access.project_candidate(candidate)
    parts = str(candidate.get("name") or "").strip().split()
    location = str(candidate.get("location") or "").split(",", 1)
    first_name = parts[0] if parts else ""
    last_name = parts[-1] if len(parts) > 1 else ""
    middle_name = " ".join(parts[1:-1]) if len(parts) > 2 else ""
    return [{
        "first_name": first_name,
        "middle_name": middle_name,
        "last_name": last_name,
        "email_address": _first(projected.get("emails")),
        "mobile_number": _first(projected.get("phones")),
        "address": _first(projected.get("addresses")),
        "city": location[0].strip() if location else "",
        "state": location[1].strip() if len(location) > 1 else "",
        "source": "Medhunt",
        "job_title": _note(candidate, "Role") or _note(candidate, "Headline"),
        "skills": _note(candidate, "Specialty"),
        "primary_skills": _note(candidate, "Specialty"),
        "additional_comments": f"Enriched by Medhunt; source: {candidate.get('source') or ''}",
        "filename": "",
        "resume_content": "",
    }]


def _token() -> str:
    response = httpx.post(
        config.CEIPAL_AUTH_URL,
        json={
            "email": config.CEIPAL_EMAIL,
            "password": config.CEIPAL_PASSWORD,
            "api_key": config.CEIPAL_API_KEY,
            "json": 1,
        },
        timeout=config.CEIPAL_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    for source in (body, body.get("data") if isinstance(body, dict) else None):
        if isinstance(source, dict):
            for key in ("access_token", "token", "auth_token"):
                if source.get(key):
                    return str(source[key])
    raise RuntimeError("CEIPAL authentication returned no token")


def process_once() -> dict | None:
    if not config.CEIPAL_CONFIGURED:
        return None
    delivery = store.claim_ceipal_delivery()
    if not delivery:
        return None
    candidate_id = int(delivery["candidate_id"])
    attempts = int(delivery.get("attempts") or 1)
    try:
        candidate = store.get_candidate(candidate_id)
        route = store.get_candidate_delivery_route(candidate_id)
        if not candidate or not route or not route.get("ceipal_enabled"):
            store.finish_ceipal_delivery(candidate_id, "cancelled")
            return {"status": "cancelled", "candidate_id": candidate_id}
        response = httpx.post(
            config.CEIPAL_CANDIDATE_URL,
            json=_payload(candidate),
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=config.CEIPAL_TIMEOUT,
        )
        response.raise_for_status()
        store.finish_ceipal_delivery(candidate_id, "succeeded")
        return {"status": "succeeded", "candidate_id": candidate_id}
    except Exception as exc:
        if attempts >= config.CEIPAL_MAX_ATTEMPTS:
            status, retry_at = "failed", 0
        else:
            status = "retry"
            retry_at = time.time() + min(300.0, float(2 ** min(attempts, 8)))
        store.finish_ceipal_delivery(
            candidate_id, status, error=f"{type(exc).__name__}: {exc}", retry_at=retry_at,
        )
        return {"status": status, "candidate_id": candidate_id}


def queue_candidate(candidate_id: int) -> dict | None:
    if not config.CEIPAL_CONFIGURED:
        return None
    route = store.get_candidate_delivery_route(candidate_id)
    if not route or not route.get("ceipal_enabled"):
        return None
    return store.enqueue_ceipal_delivery(candidate_id)


def _run() -> None:
    while not _STOP.is_set():
        if process_once() is None:
            _STOP.wait(config.CEIPAL_WORKER_INTERVAL_SECONDS)


def start() -> None:
    global _THREAD
    if not config.CEIPAL_CONFIGURED:
        return
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_run, name="medhunt-ceipal-delivery", daemon=True)
        _THREAD.start()


def stop(timeout: float = 5.0) -> None:
    global _THREAD
    with _LOCK:
        thread = _THREAD
        _STOP.set()
    if thread and thread.is_alive():
        thread.join(timeout)
    _THREAD = None
