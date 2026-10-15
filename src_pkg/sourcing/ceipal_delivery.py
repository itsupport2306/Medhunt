"""Deliver Ceipal-assigned, verified contacts through the Halo service channel."""
from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from pathlib import Path

from . import config, contact_access, healthboard_auth, storage, store


_STOP = threading.Event()
_THREADS: list[threading.Thread] = []
_THREAD_LOCK = threading.Lock()


def queue_resume(candidate_id: int, resume_id: int, user_id: str) -> str:
    """Queue only a stored resume with trusted contact details."""
    owner = str(user_id or "local")
    route = store.get_candidate_ats_route(candidate_id, owner) or {}
    if route.get("destination") != "ceipal":
        return "disabled"
    eligibility = dict(route.get("eligibility") or {})
    previous = eligibility.get("ceipal_upload") or {}
    if previous.get("state") in {"uploaded_to_ceipal", "already_in_ceipal"}:
        return "uploaded" if previous["state"] == "uploaded_to_ceipal" else "already_in_ceipal"
    candidate = store.get_candidate(candidate_id)
    projected = contact_access.project_candidate(candidate or {})
    if not projected.get("contacts_trusted") or not (
        projected.get("emails") or projected.get("phones")
    ):
        return "waiting_for_contact"
    existing = store.get_ceipal_delivery(candidate_id, owner) or {}
    if existing.get("status") in {"uploaded", "already_in_ceipal", "indeterminate"}:
        return str(existing["status"])
    if existing.get("status") in {"processing", "writing"}:
        return "queued"
    eligibility["ceipal_upload"] = {"state": "queued", "checked": False}
    store.set_candidate_ats_route(candidate_id, owner, "ceipal", eligibility)
    store.enqueue_ceipal_delivery(candidate_id, resume_id, owner)
    return "queued"


def _resume_payload(candidate_id: int, resume_id: int) -> dict:
    resume = store.get_resume(candidate_id, resume_id)
    if not resume:
        raise RuntimeError("Stored Ceipal resume was not found.")
    data = bytes(resume.get("data") or b"")
    if not data and resume.get("storage_provider") == "r2":
        data = storage.download_resume(str(resume.get("object_key") or ""))
    if not data.startswith(b"%PDF") or len(data) > config.RESUME_MAX_BYTES:
        raise RuntimeError("Stored Ceipal resume is unavailable or invalid.")
    filename = Path(str(resume.get("filename") or "resume.pdf")).name
    if not filename.lower().endswith(".pdf"):
        filename = f"{filename}.pdf"
    return {
        "resume_filename": filename[:180],
        "resume_content_base64": base64.b64encode(data).decode("ascii"),
        "resume_sha256": hashlib.sha256(data).hexdigest(),
    }


def upload_candidate(candidate_id: int, user_id: str, resume_id: int | None = None) -> str:
    candidate = store.get_candidate(candidate_id)
    if not candidate:
        return "failed"
    owner = str(user_id or "local")
    route = store.get_candidate_ats_route(candidate_id, owner) or {}
    if route.get("destination") != "ceipal":
        return "disabled"
    eligibility = dict(route.get("eligibility") or {})
    previous = eligibility.get("ceipal_upload") or {}
    if previous.get("state") in {"uploaded_to_ceipal", "already_in_ceipal"}:
        return "uploaded" if previous.get("state") == "uploaded_to_ceipal" else "already_in_ceipal"

    projected = contact_access.project_candidate(candidate)
    if not projected.get("contacts_trusted") or not (
        projected.get("emails") or projected.get("phones")
    ):
        return "waiting_for_contact"
    wireless_phones = [
        str(item.get("value") or "").strip()
        for item in projected.get("phone_contacts") or []
        if isinstance(item, dict)
        and str(item.get("kind") or "").casefold() in {"wireless", "mobile"}
    ]
    payload = {
        "candidate_id": str(candidate_id),
        "name": str(candidate.get("canonical_name") or candidate.get("name") or "").strip(),
        "location": str(candidate.get("location") or "").strip(),
        "emails": list(projected.get("emails") or []),
        "phones": list(projected.get("phones") or []),
        "wireless_phones": list(dict.fromkeys(wireless_phones)),
    }
    try:
        if resume_id is not None:
            payload.update(_resume_payload(candidate_id, resume_id))
        result = healthboard_auth.medhunt_ceipal_candidate(
            user_id=owner, candidate=payload,
        )
        state = str(result.get("state") or "uploaded_to_ceipal")
        if state == "uploaded_to_ceipal" and resume_id is not None and result.get("resume_submitted") is not True:
            raise RuntimeError("Halo did not confirm that the resume was submitted to Ceipal.")
        eligibility["ceipal_upload"] = {
            "state": state,
            "applicant_id": str(result.get("applicant_id") or ""),
            "checked": bool(result.get("checked", True)),
            "resume_submitted": result.get("resume_submitted") is True,
        }
        store.set_candidate_ats_route(candidate_id, owner, "ceipal", eligibility)
        return "uploaded" if state == "uploaded_to_ceipal" else state
    except Exception as exc:
        eligibility["ceipal_upload"] = {
            "state": "indeterminate",
            "error": str(exc)[:240],
            "checked": False,
        }
        store.set_candidate_ats_route(candidate_id, owner, "ceipal", eligibility)
        logging.getLogger("medhunt.ceipal").warning(
            "Ceipal upload failed for candidate %s: %s", candidate_id,
            str(exc)[:240],
        )
        return "indeterminate"


def process_once() -> dict | None:
    delivery = store.claim_ceipal_delivery()
    if not delivery:
        return None
    logging.getLogger("medhunt.ceipal").info(
        "Ceipal delivery claimed queue_age_seconds=%.2f",
        max(0.0, time.time() - float(delivery.get("created") or time.time())),
    )
    delivery_id = int(delivery["id"])
    lease_until = float(delivery["lease_until"])
    if not store.mark_ceipal_delivery_writing(delivery_id, lease_until):
        return {"status": "indeterminate", "delivery_id": delivery_id}
    try:
        result = upload_candidate(
            int(delivery["candidate_id"]), str(delivery["user_id"]), int(delivery["resume_id"]),
        )
        status = result if result in {"uploaded", "already_in_ceipal"} else "indeterminate"
        store.finish_ceipal_delivery(delivery_id, lease_until, status)
        return {"status": status, "delivery_id": delivery_id}
    except Exception as exc:
        store.finish_ceipal_delivery(
            delivery_id, lease_until, "indeterminate", type(exc).__name__,
        )
        logging.getLogger("medhunt.ceipal").exception("CEIPAL delivery worker failed")
        return {"status": "indeterminate", "delivery_id": delivery_id}


def _run() -> None:
    while not _STOP.is_set():
        started = time.monotonic()
        try:
            processed = process_once()
        except Exception:
            logging.getLogger("medhunt.ceipal").exception("CEIPAL delivery dispatcher failed")
            processed = None
        if processed is None:
            _STOP.wait(3.0)
        else:
            logging.getLogger("medhunt.ceipal").info(
                "Ceipal delivery status=%s duration_seconds=%.2f",
                processed.get("status"), time.monotonic() - started,
            )


def start() -> None:
    global _THREADS
    if not (config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN and healthboard_auth.enabled()):
        return
    with _THREAD_LOCK:
        if any(thread.is_alive() for thread in _THREADS):
            return
        _STOP.clear()
        _THREADS = [
            threading.Thread(
                target=_run, name=f"medhunt-ceipal-delivery-{index + 1}", daemon=True,
            )
            for index in range(config.CEIPAL_DELIVERY_WORKERS)
        ]
        for thread in _THREADS:
            thread.start()


def stop(timeout: float = 5.0) -> None:
    global _THREADS
    with _THREAD_LOCK:
        threads = list(_THREADS)
        _STOP.set()
    for thread in threads:
        if thread.is_alive():
            thread.join(max(0.0, float(timeout)))
    with _THREAD_LOCK:
        if not any(thread.is_alive() for thread in threads):
            _THREADS = []
