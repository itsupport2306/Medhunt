from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time

from . import (
    ats_routing, config, healthboard_auth, nexus_delivery,
    quick_sourcer_client, store,
)


_STOP = threading.Event()
_THREADS: list[threading.Thread] = []
_DISPATCH_THREAD: threading.Thread | None = None
_READY_JOBS: queue.Queue = queue.Queue(
    maxsize=config.CONTACT_LOOKUP_MAX_CONCURRENT,
)
_THREAD_LOCK = threading.Lock()
_MAX_ATTEMPTS = 3


def _terminal(job: dict, status: str, result: dict) -> dict:
    store.finish_contact_lookup_job(int(job["id"]), status, result)
    candidate_id = int(job["candidate_id"])
    user_id = str(job.get("requested_by") or "")
    run_id = str(job.get("run_id") or "")
    public_status = str(result.get("status") or status)
    store.record_enrichment_event(
        user_id or "local", candidate_id, public_status,
        provider="quick_sourcer", run_id=run_id,
    )
    try:
        candidate = store.get_candidate(candidate_id) or {}
        identity = f"{user_id}|{run_id}|{candidate_id}|{public_status}"
        if str(job.get("job_source") or "") != "halo_backfill":
            healthboard_auth.report_enrichment_service(
                user_id=user_id,
                event_id=hashlib.sha256(identity.encode()).hexdigest()[:32],
                candidate_id=candidate_id,
                status=public_status,
                source=str(candidate.get("source") or "quick_sourcer"),
                run_id=run_id,
            )
    except Exception as exc:
        logging.getLogger("medhunt.analytics").warning(
            "Queued enrichment reporting failed (%s).", type(exc).__name__,
        )
    return {"status": status, "job_id": int(job["id"])}


def _retry(job: dict, reason: str) -> dict:
    attempts = max(1, int(job.get("attempts") or 1))
    if attempts >= _MAX_ATTEMPTS:
        result = {
            "status": "failed", "emails": [], "phones": [],
            "phone_contacts": [], "resume_required": False,
            "location_match": None,
        }
        if str(job.get("job_source") or "") == "halo_backfill":
            try:
                healthboard_auth.report_halo_profile_backfill(
                    profile_id=str(job.get("external_ref") or ""),
                    candidate_id=int(job["candidate_id"]), status="failed",
                    result=result, attempts=attempts,
                )
            except Exception:
                logging.getLogger("medhunt.lookup").warning(
                    "Could not report terminal Halo backfill failure for candidate %s.",
                    job.get("candidate_id"),
                )
        return _terminal(job, "failed", result)
    delay = 15.0 * (3 ** (attempts - 1))
    store.finish_contact_lookup_job(
        int(job["id"]), "retry", {}, error=reason,
        retry_at=time.time() + delay,
    )
    return {"status": "retry", "job_id": int(job["id"]), "retry_in": delay}


def _process_job(job: dict) -> dict:
    candidate_id = int(job["candidate_id"])
    if store.contact_lookup_paused(str(job.get("requested_by") or "")):
        store.defer_contact_lookup_job(int(job["id"]))
        return {"status": "paused", "job_id": int(job["id"])}
    if not quick_sourcer_client.configured():
        return _retry(job, "lookup_provider_unavailable")
    candidate = store.get_candidate(candidate_id)
    if not candidate:
        return _terminal(job, "failed", {
            "status": "failed", "emails": [], "phones": [],
            "phone_contacts": [], "resume_required": False,
            "location_match": None,
        })
    try:
        if str(job.get("job_source") or "") == "halo_backfill":
            healthboard_auth.report_halo_profile_backfill(
                profile_id=str(job.get("external_ref") or ""),
                candidate_id=candidate_id, status="processing", result={},
                attempts=int(job.get("attempts") or 0),
            )
        result = quick_sourcer_client.lookup_candidate(
            candidate_id, candidate=candidate,
        )
        if result.get("status") == "failed":
            return _retry(job, "temporary_lookup_failure")
        if str(job.get("job_source") or "") == "halo_backfill":
            # Callback before making the queue item terminal. If Halo is
            # temporarily unavailable, retrying reuses the Quick Sourcer cache
            # instead of spending another live search.
            healthboard_auth.report_halo_profile_backfill(
                profile_id=str(job.get("external_ref") or ""),
                candidate_id=candidate_id,
                status=str(result.get("status") or "failed"),
                result=result,
                attempts=int(job.get("attempts") or 0),
            )
            status = "succeeded" if result.get("status") == "found" else "not_found"
            return _terminal(job, status, result)
        destination = "ceipal" if str(job.get("delivery_target") or "nexus").casefold() == "ceipal" else "nexus"
        user_id = str(job.get("requested_by") or "")
        ats_routing.set_candidate_target(candidate_id, destination, user_id)
        found = result.get("status") == "found"
        if found:
            result["ats_destination"] = destination
        status = "succeeded" if result.get("status") == "found" else "not_found"
        terminal = _terminal(job, status, result)
        # Make the contact result visible to the extension before doing
        # optional ATS work. A newly enriched candidate normally has no resume
        # yet, and resume capture will queue delivery when it is saved.
        if found and destination == "nexus":
            try:
                nexus_delivery.queue_latest_resume_if_ready(candidate_id, user_id)
            except Exception as exc:
                logging.getLogger("medhunt.nexus").warning(
                    "Nexus queueing deferred for candidate %s (%s).",
                    candidate_id, type(exc).__name__,
                )
        return terminal
    except Exception as exc:
        logging.getLogger("medhunt.lookup").warning(
            "Queued candidate lookup failed for id %s (%s).",
            candidate_id, type(exc).__name__,
        )
        return _retry(job, f"unexpected_{type(exc).__name__}")


def process_once() -> dict | None:
    job = store.claim_contact_lookup_job()
    return _process_job(job) if job else None


def _run() -> None:
    while not _STOP.is_set():
        try:
            job = _READY_JOBS.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            _process_job(job)
        except Exception:
            logging.getLogger("medhunt.lookup").exception("Contact lookup queue worker failed")
        finally:
            _READY_JOBS.task_done()


def _dispatch() -> None:
    """Fill the local worker queue with one durable database claim batch."""
    while not _STOP.is_set():
        capacity = max(0, config.CONTACT_LOOKUP_MAX_CONCURRENT - _READY_JOBS.qsize())
        if not capacity:
            _STOP.wait(0.25)
            continue
        try:
            jobs = store.claim_contact_lookup_jobs(capacity)
        except Exception:
            logging.getLogger("medhunt.lookup").exception("Contact lookup dispatcher failed")
            _STOP.wait(2.0)
            continue
        for job in jobs:
            if _STOP.is_set():
                break
            _READY_JOBS.put(job)
        if not jobs:
            _STOP.wait(1.0)


def start() -> None:
    global _THREADS, _DISPATCH_THREAD
    with _THREAD_LOCK:
        if any(thread.is_alive() for thread in _THREADS):
            return
        _STOP.clear()
        _THREADS = [
            threading.Thread(
                target=_run,
                name=f"contact-lookup-queue-{worker_number:02d}",
                daemon=True,
            )
            for worker_number in range(1, config.CONTACT_LOOKUP_MAX_CONCURRENT + 1)
        ]
        _DISPATCH_THREAD = threading.Thread(
            target=_dispatch,
            name="contact-lookup-dispatcher",
            daemon=True,
        )
        for thread in _THREADS:
            thread.start()
        _DISPATCH_THREAD.start()


def stop() -> None:
    global _THREADS, _DISPATCH_THREAD
    _STOP.set()
    deadline = time.monotonic() + 3.0
    if _DISPATCH_THREAD and _DISPATCH_THREAD.is_alive():
        _DISPATCH_THREAD.join(timeout=max(0.0, deadline - time.monotonic()))
    for thread in _THREADS:
        remaining = deadline - time.monotonic()
        if thread.is_alive() and remaining > 0:
            thread.join(timeout=remaining)
    _THREADS = []
    _DISPATCH_THREAD = None
