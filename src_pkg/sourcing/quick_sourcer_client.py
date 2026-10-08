"""Quick Sourcer external contact lookup.

Quick Sourcer answers a name and location with a person's
contact details and the full public record behind them.  Behind its API a real
browser visits the underlying people-search site, so one uncached search takes
30-90 seconds; found records are cached locally so the panel can reopen them
instantly.

The API key is read only by this backend.  The browser extension calls the
local ``/quick-sourcer/*`` endpoints and never receives or stores the key.

Quick Sourcer's `found` result is authoritative for this provider. The backend
normalizes returned contact fields and applies do-not-contact suppression, but
does not discard a found response based on a second name or location check.
"""
from __future__ import annotations

import re
import time

import httpx

from . import config, contact_access, person_name, store

_PROVIDER = "quick_sourcer"
_MASKED_EMAIL_RE = re.compile(r"\*")
# Quick Sourcer scrapes live pages, so a failed page can surface as a
# "found" record built out of page furniture ("404 - Not Found", nav labels).
_SCRAPE_ARTIFACT_RE = re.compile(r"^\s*\d{3}\s*-\s*\w", re.IGNORECASE)


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _identity_key(value) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _request_key(name: str, location: str, dedicated_ip: bool = False) -> str:
    base = f"find:{_identity_key(name)}|{_identity_key(location)}"
    return f"{base}|pool:dedicated" if dedicated_ip else base


def _external_key(external_id) -> str:
    return f"id:{int(external_id)}"


def configured() -> bool:
    return bool(config.QUICK_SOURCER_ENABLED)


def status() -> dict:
    """Return internal readiness; public API code applies a stricter projection."""
    try:
        monitor = store.api_request_monitor(
            _PROVIDER, stale_seconds=float(config.QUICK_SOURCER_TIMEOUT) + 30,
        )
    except Exception:
        monitor = {
            "provider": _PROVIDER, "active": 0, "started_5m": 0,
            "completed_5m": 0, "failed_5m": 0,
            "average_response_ms_5m": None, "measured_responses_5m": 0,
            "oldest_active_seconds": 0, "measured_at": time.time(),
            "available": False,
        }
    return {
        "enabled": bool(config.QUICK_SOURCER_ENABLED),
        "configured": bool(config.QUICK_SOURCER_API_KEY),
        "base_url": config.QUICK_SOURCER_BASE_URL,
        "search_pool": "dedicated" if config.QUICK_SOURCER_DEDICATED_IP else "shared",
        "typical_seconds": [30, 90],
        "requests": monitor,
    }


def _cached(request_key: str) -> dict | None:
    if not config.QUICK_SOURCER_CACHE_TTL_SECONDS:
        return None
    row = store.get_provider_lookup(_PROVIDER, request_key)
    if not row:
        return None
    age = time.time() - float(row.get("created") or 0)
    if age > config.QUICK_SOURCER_CACHE_TTL_SECONDS:
        return None
    result = row.get("result") or {}
    if result.get("status") != "found":
        return None
    return {**result, "cached": True}


def _remember(request_key: str, candidate_id: int, result: dict) -> None:
    """Cache a found record; a miss or an error must stay retryable."""
    if result.get("status") != "found":
        return
    store.save_provider_lookup(
        _PROVIDER, "external", int(candidate_id or 0), request_key, "found",
        {**result, "cached": False},
    )
    external_id = result.get("external_id")
    if external_id and request_key != _external_key(external_id):
        store.save_provider_lookup(
            _PROVIDER, "external", int(candidate_id or 0),
            _external_key(external_id), "found", {**result, "cached": False},
        )


def _empty(status_value: str, error: str = "") -> dict:
    return {
        "status": status_value,
        "error": error,
        "cached": False,
        "external_id": None,
        "name": "",
        "source": "",
        "emails": [],
        "masked_emails": [],
        "phones": [],
        "addresses": [],
        "current_address": {},
        "age": None,
        "born": "",
        "also_known_as": [],
        "employment": [],
        "education": [],
        "relatives": [],
        "associates": [],
        "businesses": [],
    }


def _phones(payload: dict, summary: dict) -> list[dict]:
    output, seen = [], set()
    profile = payload.get("profile") or {}
    rows = []
    for collection in (
        summary.get("phones"), profile.get("phones"),
        profile.get("currentPhone"), profile.get("current_phone"),
        payload.get("phones"), payload.get("currentPhone"),
        payload.get("current_phone"),
    ):
        if isinstance(collection, list):
            rows.extend(collection)
        elif collection:
            rows.append(collection)
    for row in rows:
        details = row if isinstance(row, dict) else {}
        value = _text(
            details.get("number") or details.get("value") or details.get("phone")
            if details else row
        )
        if not value or value in seen:
            continue
        seen.add(value)
        output.append({
            "value": value,
            "type": _text(
                details.get("type") or details.get("phoneType")
                or details.get("phone_type") or details.get("lineType")
                or details.get("line_type")
            ),
            "carrier": _text(details.get("carrier")),
            "last_reported": _text(details.get("lastReported")),
            "primary": bool(details.get("isPrimary")),
        })
    fallback = payload.get("phone")
    if fallback:
        fallback_row = fallback if isinstance(fallback, dict) else {"number": fallback}
        value = _text(fallback_row.get("number") or fallback_row.get("value") or fallback_row.get("phone"))
        if value and value not in seen:
            output.append({
                "value": value,
                "type": _text(fallback_row.get("type") or fallback_row.get("phoneType") or fallback_row.get("phone_type")),
                "carrier": _text(fallback_row.get("carrier")),
                "last_reported": _text(fallback_row.get("lastReported")),
                "primary": bool(fallback_row.get("isPrimary", True)),
            })
    output.sort(key=lambda item: not item["primary"])
    return output


def _people(rows) -> list[dict]:
    output = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            entry = {
                "name": _text(row.get("name")),
                "age": row.get("age"),
                "relationship": _text(row.get("relationship")),
                "deceased": bool(row.get("deceased")),
            }
        else:
            entry = {"name": _text(row), "age": None, "relationship": "", "deceased": False}
        if entry["name"]:
            output.append(entry)
    return output[:60]


def _current_address(payload: dict) -> dict:
    profile = payload.get("profile") or {}
    current = (
        profile.get("currentAddress") or profile.get("current_address")
        or payload.get("currentAddress") or payload.get("current_address")
    )
    if isinstance(current, dict):
        line = _text(
            current.get("address") or current.get("value")
            or current.get("formattedAddress") or current.get("formatted_address")
        )
        if line:
            return {
                "address": line,
                "county": _text(current.get("county")),
                "date_range": _text(current.get("dateRange") or current.get("date_range")),
                "property_details": _text(
                    current.get("propertyDetails") or current.get("property_details")
                ),
            }
    line = (_text(current) if not isinstance(current, dict) else "") or _text(payload.get("address"))
    if not line:
        return {}
    return {"address": line, "county": "", "date_range": "", "property_details": ""}


def normalize(payload: dict | None) -> dict:
    """Reshape one Quick Sourcer response into a single stable contract.

    ``summary`` is already source-independent, so it leads; ``profile`` fills
    in the richer per-site detail the panel shows underneath the contacts.
    """
    source_payload = payload if isinstance(payload, dict) else {}
    top_level_data = any(
        source_payload.get(key)
        for key in (
            "name", "email", "phone", "address", "source", "profile",
            "summary", "currentAddress", "current_address",
        )
    )
    root_found = source_payload.get("found")
    root_explicit_not_found = (
        root_found is False or root_found == 0
        or (isinstance(root_found, str) and root_found.strip().casefold()
            in {"false", "0", "no", "not_found", "not found"})
    )
    if not top_level_data and not root_explicit_not_found:
        for envelope in ("data", "result", "candidate", "record"):
            nested = source_payload.get(envelope)
            if isinstance(nested, dict):
                source_payload = nested
                break

    has_record_data = any(
        source_payload.get(key)
        for key in (
            "name", "email", "phone", "address", "source", "profile",
            "summary", "currentAddress", "current_address",
        )
    )
    raw_found = source_payload.get("found")
    if isinstance(raw_found, str):
        normalized_found = raw_found.strip().casefold()
        provider_found = normalized_found in {"true", "1", "yes", "found"}
        provider_not_found = normalized_found in {"false", "0", "no", "not_found", "not found"}
    else:
        provider_found = raw_found is True or raw_found == 1
        provider_not_found = raw_found is False or raw_found == 0
    # Some successful responses carry useful profile/contact fields while
    # omitting `found` or returning it in a non-boolean form. Keep those
    # records; only an explicit empty/not-found response becomes a miss.
    if provider_not_found:
        return _empty("not_found")
    if not provider_found and not has_record_data:
        return _empty(
            "error",
            "Quick Sourcer response did not include a found result or record data.",
        )

    summary = source_payload.get("summary") or {}
    profile = source_payload.get("profile") or {}
    emails, masked, seen = [], [], set()
    raw_emails = []
    profile_emails = profile.get("emails")
    for collection in (summary.get("emails"), profile_emails, source_payload.get("emails")):
        if isinstance(collection, list):
            raw_emails.extend(collection)
        elif collection:
            raw_emails.append(collection)
    for single in (summary.get("email"), profile.get("email"), source_payload.get("email")):
        if single:
            raw_emails.append(single)
    for value in raw_emails:
        address = _text(
            value.get("email") or value.get("address") or value.get("value")
            if isinstance(value, dict) else value
        )
        key = address.casefold()
        if not address or key in seen:
            continue
        seen.add(key)
        # searchpeoplefree publishes pre-masked addresses to non-paying
        # visitors. They are shown as evidence, never as a usable contact.
        (masked if _MASKED_EMAIL_RE.search(address) else emails).append(address)

    addresses, address_seen = [], set()
    for value in summary.get("addresses") or []:
        line = _text(value)
        if not line or _SCRAPE_ARTIFACT_RE.match(line):
            continue
        if line.casefold() not in address_seen:
            address_seen.add(line.casefold())
            addresses.append(line)

    result = _empty("found")
    result.update({
        "external_id": source_payload.get("candidate_id"),
        "name": _text(
            source_payload.get("name") or profile.get("fullName")
            or next(iter(summary.get("names") or []), "")
        ),
        "source": _text(source_payload.get("source")),
        "emails": emails,
        "masked_emails": masked[:20],
        "phones": _phones(source_payload, summary),
        "addresses": addresses[:40],
        "current_address": _current_address(source_payload),
        "age": profile.get("age"),
        "born": _text(profile.get("born") or profile.get("birthYear")),
        "also_known_as": [
            _text(value) for value in (summary.get("names") or [])[:20] if _text(value)
        ],
        "employment": [
            {
                "employer": _text(row.get("employer")),
                "title": _text(row.get("title")),
                "industry": _text(row.get("industry")),
                "from": _text(row.get("from")),
                "to": _text(row.get("to")),
                "location": _text(row.get("location")),
            }
            for row in (summary.get("experience") or []) if isinstance(row, dict)
        ][:20],
        "education": [
            {
                "institution": _text(row.get("institution")),
                "degree": _text(row.get("degree")),
            }
            for row in (summary.get("education") or []) if isinstance(row, dict)
        ][:20],
        "relatives": _people(profile.get("relatives")),
        "associates": _people(profile.get("associates")),
        "businesses": [
            {"name": _text(row.get("name")), "address": _text(row.get("address"))}
            for row in (profile.get("businesses") or []) if isinstance(row, dict)
        ][:20],
    })
    return result


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    # Keep the configured timeout as one total budget. A gateway can return a
    # 502/503/504 early while the live-browser worker is being recycled; one
    # retry inside the remaining budget fixes that transient case without
    # allowing a single candidate to outlive the extension's request timeout.
    activity_id = store.begin_api_request(_PROVIDER, f"{method.upper()} {path}")
    outcome = "failed"
    deadline = time.monotonic() + config.QUICK_SOURCER_TIMEOUT
    response = None
    try:
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                raise httpx.TimeoutException("Quick Sourcer request budget expired.")
            try:
                response = httpx.request(
                    method,
                    f"{config.QUICK_SOURCER_BASE_URL}{path}",
                    json=payload,
                    headers={"X-API-Key": config.QUICK_SOURCER_API_KEY},
                    timeout=remaining,
                )
            except httpx.TransportError:
                if attempt or deadline - time.monotonic() <= 2:
                    raise
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic() - 1)))
                continue
            if response.status_code not in {502, 503, 504} or attempt:
                break
            if deadline - time.monotonic() <= 2:
                break
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic() - 1)))

        if response is None:
            raise httpx.TransportError("Quick Sourcer returned no response.")
        if response.status_code in (401, 403):
            raise PermissionError("Quick Sourcer rejected the configured API key.")
        response.raise_for_status()
        body = response.json()
        outcome = "completed"
        return body if isinstance(body, dict) else {}
    finally:
        store.finish_api_request(activity_id, outcome)


def _failure(exc: Exception, *, dedicated_ip: bool = False) -> dict:
    if isinstance(exc, PermissionError):
        return _empty("error", str(exc))
    if isinstance(exc, httpx.TimeoutException):
        return _empty(
            "error",
            "Quick Sourcer did not answer in time. A live search can take 30-90 seconds.",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 400 and dedicated_ip:
            return _empty(
                "error",
                "Quick Sourcer dedicated search is unavailable. Mark at least one Hub Search Endpoint as Dedicated.",
            )
        # The status is the only actionable detail here: a 5xx is the Hub or a
        # source site failing this one search, and it is worth retrying.
        return _empty(
            "error",
            f"Quick Sourcer returned HTTP {exc.response.status_code}. Try this candidate again.",
        )
    return _empty("error", f"Quick Sourcer request failed ({type(exc).__name__}).")


def find(name: str, location: str = "", candidate_id: int = 0,
         refresh: bool = False, dedicated_ip: bool | None = None) -> dict:
    """Search Quick Sourcer for one person, reusing a cached record by default."""
    person = _text(name)
    if not person:
        return _empty("error", "A candidate name is required.")
    if not configured():
        return _empty("disabled", "Quick Sourcer is not configured on this backend.")

    use_dedicated = (
        config.QUICK_SOURCER_DEDICATED_IP
        if dedicated_ip is None else bool(dedicated_ip)
    )
    request_key = _request_key(person, location, use_dedicated)
    if not refresh:
        hit = _cached(request_key)
        if hit:
            return hit
    try:
        request_payload = {"name": person, "location": _text(location)}
        if use_dedicated:
            request_payload["dedicated_ip"] = True
        payload = _call("POST", "/find", request_payload)
    except Exception as exc:
        return _failure(exc, dedicated_ip=use_dedicated)
    result = normalize(payload)
    if result.get("status") != "found":
        return result
    _remember(request_key, candidate_id, result)
    return result


def fetch(external_id: int, refresh: bool = False) -> dict:
    """Re-read a person Quick Sourcer has already found. No new search runs."""
    try:
        identifier = int(external_id)
    except (TypeError, ValueError):
        return _empty("error", "A Quick Sourcer record id is required.")
    if not configured():
        return _empty("disabled", "Quick Sourcer is not configured on this backend.")

    request_key = _external_key(identifier)
    row = store.get_provider_lookup(_PROVIDER, request_key)
    candidate = store.get_candidate(int(row.get("candidate_id") or 0)) if row else None
    if not candidate:
        return _empty("error", "No saved Quick Sourcer record is linked to that id.")
    if not refresh:
        hit = _cached(request_key)
        if hit:
            return hit
    try:
        payload = _call("GET", f"/candidates/{identifier}")
    except Exception as exc:
        return _failure(exc)
    result = normalize(payload)
    if result.get("status") != "found":
        return result
    _remember(request_key, 0, result)
    return result


# ---- candidate contact lookup ----
# When ``CONTACT_LOOKUP_PROVIDER`` selects Quick Sourcer, these functions answer
# the extension's lookups. Whatever the API delivers is stored and shown as-is:
# no identity threshold, no likelihood floor, no provider-trust gate. The one
# filter that still applies is the do-not-contact list, which is a compliance
# rule rather than a confidence judgement.
CONTACT_SOURCE = "quick_sourcer"


def phone_kind(phone: dict) -> str:
    """Map a reported line type onto the two kinds the panel renders."""
    phone_type = _text(phone.get("type")).casefold()
    return "mobile" if any(
        token in phone_type for token in ("wireless", "mobile", "cellular", "cell")
    ) else "other"


def public_lookup_result(result: dict | None) -> dict:
    """Return the browser's lookup contract, carrying the API's own values."""
    source = dict(result or {})
    status = str(source.get("status") or "error")
    if status == "found":
        emails = [str(value).strip() for value in source.get("emails") or [] if str(value).strip()]
        phone_rows = [
            {"value": _text(phone.get("value")), "kind": phone_kind(phone)}
            for phone in source.get("phones") or [] if _text(phone.get("value"))
        ]
        allowed = store.filter_dnc_groups({
            "emails": emails,
            "phones": [row["value"] for row in phone_rows],
        })
        allowed_phones = {store.contact_key(value) for value in allowed.get("phones") or []}
        phone_contacts = [
            row for row in phone_rows if store.contact_key(row["value"]) in allowed_phones
        ]
        emails = list(allowed.get("emails") or [])
        phones = [row["value"] for row in phone_contacts]
        has_usable_contact = bool(emails or phones)
    else:
        emails, phones, phone_contacts, has_usable_contact = [], [], [], False
    return {
        # The upstream `found` field determines match status. Contact
        # suppression can remove a channel, but it must not rewrite a found
        # API record into a no-match result.
        "status": (
            "found" if status == "found"
            else "not_found" if status == "not_found"
            else "failed"
        ),
        "emails": emails,
        "phones": phones,
        "phone_contacts": phone_contacts,
        # A usable phone or email is sufficient for Indeed resume capture.
        # Masked provider email labels remain display-only/non-contact data.
        "resume_required": bool(status == "found" and has_usable_contact),
        "location_match": None,
    }


def _stored_record(result: dict) -> dict:
    """Keep the reviewable detail beside the contacts, without the bulk."""
    return {
        "external_id": result.get("external_id"),
        "name": result.get("name"),
        "source_site": result.get("source"),
        "age": result.get("age"),
        "born": result.get("born"),
        "current_address": result.get("current_address") or {},
        "phones": result.get("phones") or [],
        "masked_emails": result.get("masked_emails") or [],
        "also_known_as": (result.get("also_known_as") or [])[:10],
        "employment": result.get("employment") or [],
        "education": result.get("education") or [],
    }


def apply_to_candidate(candidate_id: int, result: dict) -> dict:
    """Persist one Quick Sourcer record against a stored candidate."""
    public = public_lookup_result(result)
    found = result.get("status") == "found"
    has_usable_contact = bool(public["emails"] or public["phones"])
    now = time.time()
    expires = now + max(3600, config.QUICK_SOURCER_CACHE_TTL_SECONDS or 0)
    verification = {
        "source": CONTACT_SOURCE,
        "identity_status": "provider_found" if found else "unverified",
        "checked_at": now,
        "record": _stored_record(result) if result.get("status") == "found" else {},
        "evidence": {
            "contact_source": CONTACT_SOURCE,
            "source_site": result.get("source") or "",
            "external_id": result.get("external_id"),
            "lookup_status": result.get("status"),
            "error": result.get("error") or "",
        },
    }
    store.update_candidate(
        candidate_id,
        emails=public["emails"],
        phones=public["phones"],
        addresses=list(result.get("addresses") or [])[:40] if found else [],
        enrich_status="success" if found else (
            "error" if public["status"] == "failed" else "no_match"
        ),
        identity_provider=CONTACT_SOURCE,
        verification=verification,
        contact_verified_at=now if has_usable_contact else 0,
        contact_expires_at=expires if has_usable_contact else 0,
    )
    return public


def lookup_candidate(
    candidate_id: int, refresh: bool = False, *, candidate: dict | None = None,
) -> dict:
    """Reuse trusted saved contacts, or look the candidate up through Quick Sourcer.

    An uncached search takes 30-90 seconds because the API drives a real
    browser, so callers must run these one at a time rather than in parallel.
    Fresh, trusted contacts already saved for this exact candidate are returned
    directly without spending a Quick Sourcer request. Quick Sourcer contacts
    themselves continue to use the provider lookup cache and its TTL.
    """
    candidate = candidate or store.get_candidate(candidate_id)
    if not candidate:
        return {"status": "failed", "emails": [], "phones": [], "phone_contacts": [],
                "resume_required": False, "location_match": None}

    verification = candidate.get("verification") or {}
    if not refresh and verification.get("source") != CONTACT_SOURCE:
        saved = contact_access.project_candidate(candidate)
        if saved.get("contacts_trusted") and (saved.get("emails") or saved.get("phones")):
            return {
                "status": "found",
                "cached": True,
                "source": str(saved.get("contact_source") or "saved_contact"),
                "emails": list(saved.get("emails") or []),
                "phones": list(saved.get("phones") or []),
                "phone_contacts": list(saved.get("phone_contacts") or []),
                "addresses": list(saved.get("addresses") or []),
                "resume_required": True,
                "location_match": None,
            }

    name = person_name.normalize_person_name(candidate.get("name") or "") or str(
        candidate.get("name") or ""
    )
    result = find(
        name,
        str(candidate.get("location") or ""),
        candidate_id=candidate_id,
        refresh=refresh,
    )
    return apply_to_candidate(candidate_id, result)
