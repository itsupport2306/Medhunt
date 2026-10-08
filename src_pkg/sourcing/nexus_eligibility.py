"""Read-only Nexus ownership guard for enrichment and recruiter messaging."""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import replace
from typing import Any, Mapping

from . import config, contact_access, store
from .nexus_sync import NexusClient, NexusDeliveryError, NexusSettings


class NexusEligibilityUnavailable(RuntimeError):
    pass


_CLIENT: NexusClient | None = None
_CLIENT_LOCK = threading.Lock()


def enabled() -> bool:
    return bool(config.NEXUS_PRECHECK_ENABLED)


def _client() -> NexusClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            settings = replace(NexusSettings.from_config(), enabled=True)
            _CLIENT = NexusClient(settings)
        return _CLIENT


def _values(values: Any) -> list[str]:
    output: list[str] = []
    for value in values if isinstance(values, list) else []:
        text = str(value.get("value") if isinstance(value, Mapping) else value or "").strip()
        if text and text not in output:
            output.append(text)
    return output


def _phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else ""


def candidate_identifiers(candidate: Mapping[str, Any]) -> dict[str, list[str]]:
    """Return every available email and explicitly wireless phone for Nexus.

    This check runs after contact enrichment. Only contact types returned by
    the provider as wireless/mobile are searched; landlines and untyped phones
    are excluded. ``project_candidate`` applies trust and DNC filtering first.
    """
    projected = contact_access.project_candidate(dict(candidate))
    emails = list(dict.fromkeys(
        value.casefold() for value in _values(projected.get("emails")) if "@" in value
    ))
    wireless_phones = []
    for item in projected.get("phone_contacts") or []:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or item.get("type") or "").casefold()
        if kind not in {"mobile", "wireless", "cell", "cellular"}:
            continue
        phone = _phone(str(item.get("value") or ""))
        if phone and phone not in wireless_phones:
            wireless_phones.append(phone)
    return {"emails": emails, "phones": wireless_phones}


def _identity_key(identifiers: Mapping[str, list[str]]) -> str:
    stable = "|".join(
        f"{kind}:{value}" for kind in ("emails", "phones")
        for value in sorted(identifiers.get(kind) or [])
    )
    return hashlib.sha256(stable.encode()).hexdigest() if stable else "none"


def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().casefold() in {"1", "true", "yes"}


def _record_summary(record: Mapping[str, Any], matched_by: list[str]) -> dict[str, Any]:
    recruiter = str(record.get("recruiter") or record.get("staffingSpecialist") or "").strip()
    return {
        "candidate_id": str(record.get("candidateId") or record.get("id") or ""),
        "status": str(record.get("status") or "").strip(),
        "status_code": str(record.get("statusCode") or "").strip(),
        "recruiter": recruiter,
        "matched_by": matched_by,
    }


def check_candidate(candidate: Mapping[str, Any], *, fresh: bool = False) -> dict[str, Any]:
    if not enabled():
        return {"state": "disabled", "blocked": False, "checked": False}
    identifiers = candidate_identifiers(candidate)
    identity_key = _identity_key(identifiers)
    candidate_id = int(candidate.get("id") or 0)
    cached = store.get_nexus_candidate_check(candidate_id) if candidate_id else None
    if (
        not fresh and cached and cached.get("identity_key") == identity_key
        and float(cached.get("checked") or 0) + config.NEXUS_PRECHECK_CACHE_SECONDS > time.time()
    ):
        return dict(cached.get("result") or {})
    if not (identifiers["emails"] or identifiers["phones"]):
        # At this stage enrichment completed, but Nexus cannot be queried
        # without an email or explicitly wireless phone.
        result = {
            "state": "identity_search_unavailable", "blocked": True,
            "checked": False,
        }
        if candidate_id:
            store.save_nexus_candidate_check(candidate_id, identity_key, result)
        return result

    client = _client()
    matches: dict[str, dict[str, Any]] = {}
    matched_by: dict[str, list[str]] = {}
    try:
        for email in identifiers["emails"]:
            rows = client.search_candidates(email=email)
            for row in rows:
                key = str(row.get("candidateId") or row.get("id") or f"email:{email}")
                matches[key] = row
                matched_by.setdefault(key, []).append("email")
        for phone in identifiers["phones"]:
            rows = client.search_candidates(phone=phone)
            for row in rows:
                key = str(row.get("candidateId") or row.get("id") or f"phone:{phone}")
                matches[key] = row
                matched_by.setdefault(key, []).append("phone")
        statuses = client.get_master("candidatestatuses") if matches else []
    except NexusDeliveryError as exc:
        if candidate_id:
            store.save_nexus_candidate_check(candidate_id, identity_key, {
                "state": "unavailable", "blocked": True, "checked": False,
            })
        raise NexusEligibilityUnavailable("Nexus candidate verification is temporarily unavailable.") from exc

    active_ids = {
        str(row.get("id")) for row in statuses
        if row.get("id") is not None and _truthy(row.get("active"))
    }
    active_codes = {
        str(row.get("code") or "").strip().casefold() for row in statuses
        if _truthy(row.get("active"))
    }
    inactive_ids = {
        str(row.get("id")) for row in statuses
        if row.get("id") is not None and not _truthy(row.get("active"))
    }
    inactive_codes = {
        str(row.get("code") or "").strip().casefold() for row in statuses
        if not _truthy(row.get("active"))
    }
    blocked_records = []
    for key, row in matches.items():
        status_id = str(row.get("statusId") or "")
        status_code = str(row.get("statusCode") or "").strip().casefold()
        owned = bool(row.get("recruiter") or row.get("recruiterEmail") or row.get("staffingSpecialist"))
        is_active = status_id in active_ids or (status_code and status_code in active_codes)
        explicitly_inactive = status_id in inactive_ids or (
            status_code and status_code in inactive_codes
        )
        if is_active or (owned and not explicitly_inactive):
            summary = _record_summary(row, sorted(set(matched_by.get(key) or [])))
            blocked_records.append(summary)
    if blocked_records:
        primary = blocked_records[0]
        result = {
            "state": "active_in_nexus", "blocked": True, "checked": True,
            "status": primary["status"], "status_code": primary["status_code"],
            "recruiter": primary["recruiter"], "matched_by": primary["matched_by"],
            "match_count": len(blocked_records),
        }
    else:
        result = {"state": "clear", "blocked": False, "checked": True}
    if candidate_id:
        store.save_nexus_candidate_check(candidate_id, identity_key, result)
    return result


def stored_result(candidate: Mapping[str, Any]) -> dict[str, Any]:
    candidate_id = int(candidate.get("id") or 0)
    row = store.get_nexus_candidate_check(candidate_id) if candidate_id else None
    if not row or row.get("identity_key") != _identity_key(candidate_identifiers(candidate)):
        return {}
    return dict(row.get("result") or {})


__all__ = ["NexusEligibilityUnavailable", "check_candidate", "enabled", "stored_result"]
