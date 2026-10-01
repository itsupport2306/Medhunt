"""Durable background delivery from Medhunt storage to LaborEdge Nexus."""
from __future__ import annotations

import hashlib
import re
import threading
import time

from . import (
    config,
    contact_access,
    nexus_sync,
    phone_policy,
    resume_enrichment,
    source_context,
    storage,
    store,
)


_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_THREAD_LOCK = threading.Lock()

_CLINICAL_ROLE_RE = re.compile(
    r"\b(?:"
    r"aprn|cna|cnm|crna|emt|lpn|lvn|np|pa-c|pct|rn|rrt|cst|"
    r"registered\s+nurse|licensed\s+(?:practical|vocational)\s+nurse|"
    r"nurse\s+(?:anesthetist|midwife|practitioner)|nursing\s+assistant|"
    r"clinical\s+nurse|staff\s+nurse|charge\s+nurse|travel\s+nurse|"
    r"physician(?:\s+assistant)?|medical\s+assistant|patient\s+care\s+tech(?:nician)?|"
    r"surgical\s+(?:services|tech(?:nician|nologist)?)|"
    r"respiratory\s+therap(?:ist|y)|physical\s+therap(?:ist|y)|"
    r"occupational\s+therap(?:ist|y)|speech\s+(?:language\s+)?(?:pathologist|therapy)|"
    r"radiolog(?:y|ic|ist)|imaging|sonograph(?:er|y)|x-?ray|"
    r"pharmac(?:ist|y)|paramedic|laboratory|phlebotomist|dialysis\s+tech|"
    r"behavioral\s+health|social\s+worker|midwife"
    r")\b",
    re.IGNORECASE,
)


def queue_latest_resume_if_ready(candidate_id: int) -> dict | None:
    """Queue a pre-existing latest resume after contacts become deliverable.

    Indeed normally captures the resume after enrichment, but recruiter uploads
    and restored sessions can produce the opposite order. This closes that gap
    without sending every historical version or weakening the trusted-contact
    gate used by normal capture-time delivery.
    """
    if not config.NEXUS_SYNC_ENABLED:
        return None
    route = store.get_candidate_delivery_route(int(candidate_id))
    if not route or not bool(route.get("nexus_enabled")):
        return None
    candidate = store.get_candidate(int(candidate_id))
    projected = contact_access.project_candidate(candidate)
    if not (
        projected.get("contacts_trusted") is True
        and (projected.get("emails") or projected.get("phones"))
    ):
        return None
    resumes = store.list_resumes(int(candidate_id))
    if not resumes:
        return None
    latest = resumes[0]
    if int(latest.get("size") or 0) > config.NEXUS_MAX_RESUME_BYTES:
        return None
    return store.enqueue_nexus_delivery(
        int(candidate_id),
        int(latest["id"]),
        str(latest.get("checksum_sha256") or ""),
    )


def _role(candidate: dict) -> str:
    """Choose a clinical role without mistaking an employer for a title.

    Platform markup occasionally labels the employer as both ``Headline`` and
    ``Role``.  The shared source parser also recovers bounded resume-style
    role/company/date triples, so prefer a clinically recognizable role from
    that complete context.  Preserve the first structured value only as a
    last resort; Nexus will map an unrecognized value to its tenant-approved
    Unknown classification rather than guessing.
    """
    values = [
        candidate.get("job_title"), candidate.get("role"), candidate.get("title"),
        *(source_context.extract(candidate).get("roles") or []),
    ]
    cleaned = []
    seen = set()
    for value in values:
        role = " ".join(str(value or "").split())[:240]
        key = role.casefold()
        if role and key not in seen:
            seen.add(key)
            cleaned.append(role)
    return next(
        (role for role in cleaned if _CLINICAL_ROLE_RE.search(role)),
        cleaned[0] if cleaned else "",
    )


def _resume_bytes(resume: dict) -> bytes:
    data = bytes(resume.get("data") or b"")
    if not data and resume.get("storage_provider") == "r2":
        data = storage.download_resume(str(resume.get("object_key") or ""))
    if not data.startswith(b"%PDF"):
        raise nexus_sync.NexusPermanentError(
            "Stored resume is unavailable or is not a PDF.",
            operation="resume_storage",
            code="stored_resume_unavailable",
        )
    return data


def _payload(delivery: dict, candidate: dict, resume: dict) -> dict:
    projected = contact_access.project_candidate(candidate)
    primary_email = next(
        (str(value or "").strip() for value in projected.get("emails") or []
         if str(value or "").strip()),
        "",
    )
    if primary_email:
        projected["primary_email"] = primary_email
    latest = phone_policy.latest_phone_detail(projected)
    if latest:
        projected["latest_phone"] = latest["value"]
    role = _role(candidate)
    if role:
        projected["job_title"] = role
    link = store.get_nexus_candidate_link(delivery.get("identity_key") or "")
    extraction_row = store.get_resume_extraction(
        resume.get("id"), candidate.get("id"),
    )
    extraction = (
        extraction_row.get("extraction")
        if isinstance(extraction_row, dict) else {}
    )
    return {
        "candidate": projected,
        # This is the local parser's bounded structured projection, never the
        # full OCR transcript. Nexus accepts only its pre-approved gap-fill
        # fields after the platform candidate projection has been applied.
        "resume_extraction": extraction if isinstance(extraction, dict) else {},
        "resume": {
            "id": resume.get("id"),
            "filename": resume.get("filename") or "resume.pdf",
            "checksum_sha256": resume.get("checksum_sha256") or "",
        },
        "nexus_candidate_id": (
            link.get("nexus_candidate_id") if link else ""
        ),
    }


def _retry_at(delivery: dict, error: nexus_sync.NexusRetryableError) -> float:
    if error.retry_after is not None:
        return time.time() + max(0.5, min(3600.0, float(error.retry_after)))
    attempts = max(1, int(delivery.get("attempts") or 1))
    return time.time() + min(300.0, float(2 ** min(attempts, 8)))


def process_once() -> dict | None:
    """Process at most one leased outbox row; return a non-PII status summary."""
    if not config.NEXUS_SYNC_ENABLED:
        return None
    delivery = store.claim_nexus_delivery(
        lease_seconds=config.NEXUS_LEASE_SECONDS,
    )
    if not delivery:
        return None
    delivery_id = int(delivery["id"])
    attempts = max(1, int(delivery.get("attempts") or 1))
    lease_until = float(delivery.get("lease_until") or 0)
    try:
        candidate = store.get_candidate(int(delivery["candidate_id"]))
        resume = store.get_resume(
            int(delivery["candidate_id"]), int(delivery["resume_id"])
        )
        if not candidate or not resume:
            raise nexus_sync.NexusPermanentError(
                "Queued candidate or resume no longer exists.",
                operation="local_storage",
                code="local_record_missing",
            )
        source_pdf = _resume_bytes(resume)
        extraction_row = store.get_resume_extraction(
            int(resume["id"]), int(candidate["id"]),
        )
        try:
            source_pdf, extraction, _resume_name = resume_enrichment.prepare_candidate_resume(
                source_pdf, candidate,
                extraction_row.get("extraction") if extraction_row else None,
            )
        except resume_enrichment.ContactSheetRefreshError as exc:
            raise nexus_sync.NexusPermanentError(
                "Stored contact page could not be removed safely.",
                operation="payload_validation",
                code="resume_contact_refresh_failed",
            ) from exc
        if not extraction_row or extraction_row.get("extraction") != extraction:
            store.save_resume_extraction(
                int(resume["id"]), int(candidate["id"]), extraction,
            )
        payload = _payload(delivery, candidate, resume)
        try:
            resume_pdf, _ = resume_enrichment.refresh_contact_sheet(
                source_pdf, payload["candidate"],
            )
        except resume_enrichment.ContactSheetRefreshError as exc:
            raise nexus_sync.NexusPermanentError(
                "Stored contact page could not be refreshed safely.",
                operation="payload_validation",
                code="resume_contact_refresh_failed",
            ) from exc
        payload["resume"]["checksum_sha256"] = hashlib.sha256(resume_pdf).hexdigest()
        def before_write(operation: str) -> None:
            if not store.mark_nexus_delivery_writing(
                delivery_id,
                expected_lease_until=lease_until,
                operation=operation,
            ):
                raise nexus_sync.NexusIndeterminateError(
                    "Nexus delivery lease was lost before the remote write.",
                    operation="delivery_lease",
                    code="nexus_lease_lost",
                )

        result = nexus_sync.process_delivery(
            payload, resume_pdf, before_write=before_write,
        )
        nexus_candidate_id = str(result.get("nexus_candidate_id") or "").strip()
        if not nexus_candidate_id:
            raise nexus_sync.NexusIndeterminateError(
                "Nexus delivery completed without a candidate identifier.",
                operation="delivery",
                code="nexus_candidate_unbound",
            )
        try:
            store.complete_nexus_delivery(
                delivery_id, delivery["identity_key"],
                int(delivery["candidate_id"]), nexus_candidate_id,
                operation=str(result.get("action") or "delivered"),
                expected_lease_until=lease_until,
            )
        except Exception:  # remote write already succeeded; never retry blindly
            # A remote write has already succeeded. A conflicting local link or
            # stale lease must never cause an automatic duplicate upload.
            store.finish_nexus_delivery(
                delivery_id, "review", nexus_candidate_id=nexus_candidate_id,
                operation="identity_link",
                error="nexus_link_conflict: manual reconciliation required",
                expected_lease_until=lease_until,
            )
            return {"status": "review", "delivery_id": delivery_id}
        return {"status": "succeeded", "delivery_id": delivery_id}
    except nexus_sync.NexusRetryableError as exc:
        if attempts >= config.NEXUS_MAX_ATTEMPTS:
            store.finish_nexus_delivery(
                delivery_id, "failed", operation=exc.operation,
                error=f"{exc.code}: retry limit reached",
                expected_lease_until=lease_until,
            )
            return {"status": "failed", "delivery_id": delivery_id}
        store.finish_nexus_delivery(
            delivery_id, "retry", operation=exc.operation,
            error=f"{exc.code}: {exc}", retry_at=_retry_at(delivery, exc),
            expected_lease_until=lease_until,
        )
        return {"status": "retry", "delivery_id": delivery_id}
    except nexus_sync.NexusIndeterminateError as exc:
        status = "review" if exc.operation == "duplicate_search" else "indeterminate"
        store.finish_nexus_delivery(
            delivery_id, status, operation=exc.operation,
            error=f"{exc.code}: {exc}",
            expected_lease_until=lease_until,
        )
        return {"status": status, "delivery_id": delivery_id}
    except nexus_sync.NexusPermanentError as exc:
        status = "review" if exc.operation in {
            "duplicate_search", "master_data", "payload_validation",
        } else "failed"
        store.finish_nexus_delivery(
            delivery_id, status, operation=exc.operation,
            error=f"{exc.code}: {exc}",
            expected_lease_until=lease_until,
        )
        return {"status": status, "delivery_id": delivery_id}
    except Exception as exc:  # noqa: BLE001 - keep the worker alive, without PII
        if attempts >= config.NEXUS_MAX_ATTEMPTS:
            status, retry_at = "failed", 0
        else:
            status = "retry"
            retry_at = time.time() + min(300.0, float(2 ** min(attempts, 8)))
        store.finish_nexus_delivery(
            delivery_id, status, operation="worker",
            error=f"unexpected_{type(exc).__name__}", retry_at=retry_at,
            expected_lease_until=lease_until,
        )
        return {"status": status, "delivery_id": delivery_id}


def _run() -> None:
    while not _STOP.is_set():
        try:
            processed = process_once()
        except Exception:  # a lease or database race is retried on the next tick
            processed = None
        if processed is None:
            _STOP.wait(config.NEXUS_WORKER_INTERVAL_SECONDS)


def start() -> None:
    global _THREAD
    if not config.NEXUS_SYNC_ENABLED:
        return
    with _THREAD_LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_run, name="medhunt-nexus-delivery", daemon=True,
        )
        _THREAD.start()


def stop(timeout: float = 5.0) -> None:
    global _THREAD
    with _THREAD_LOCK:
        thread = _THREAD
        _STOP.set()
    if thread and thread.is_alive():
        thread.join(max(0.0, float(timeout)))
    with _THREAD_LOCK:
        if _THREAD is thread and (not thread or not thread.is_alive()):
            _THREAD = None


__all__ = ["process_once", "queue_latest_resume_if_ready", "start", "stop"]
