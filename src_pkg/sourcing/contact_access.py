"""Safe outward projection of provider-supplied candidate contacts."""
from __future__ import annotations

import time

from . import config, store, trust_policy


def _public_record_projection(
    source: dict, projected: dict, *, blocked_contact_keys=None,
) -> dict:
    """Return a Quick Sourcer record exactly as the API delivered it.

    This path is deliberately free of identity thresholds and provider-trust
    rules: the recruiter asked for the external record itself. Do-not-contact
    suppression still applies, because that is a compliance obligation rather
    than a confidence judgement.
    """
    phones = [str(value or "").strip() for value in source.get("phones") or []]
    reported = {
        str(item.get("value") or "").strip(): item
        for item in (((source.get("verification") or {}).get("record") or {}).get("phones") or [])
        if isinstance(item, dict)
    }
    allowed = store.filter_dnc_groups(
        {
            "emails": source.get("emails") or [],
            "phones": phones,
            "addresses": source.get("addresses") or [],
        },
        blocked_keys=blocked_contact_keys,
    )
    projected.update({
        "emails": list(allowed.get("emails") or []),
        "phones": list(allowed.get("phones") or []),
        "phone_contacts": [
            {
                "value": value,
                "kind": "mobile" if any(token in str(
                    (reported.get(value) or {}).get("type")
                    or (reported.get(value) or {}).get("phoneType")
                    or (reported.get(value) or {}).get("phone_type")
                    or (reported.get(value) or {}).get("lineType")
                    or (reported.get(value) or {}).get("line_type") or ""
                ).casefold() for token in ("wireless", "mobile", "cellular", "cell")) else "other",
            }
            for value in allowed.get("phones") or []
        ],
        "addresses": list(allowed.get("addresses") or []),
        # The administrator controls whether the feed is trusted for ATS sync.
        "contacts_trusted": bool(config.QUICK_SOURCER_TRUSTED_FOR_SYNC),
        "contact_source": "quick_sourcer",
    })
    return projected


def project_candidate(candidate: dict | None, *, blocked_contact_keys=None) -> dict:
    """Return a copy containing only fresh, currently trusted, non-DNC contacts."""
    source = candidate or {}
    projected = {
        **source,
        "emails": [],
        "phones": [],
        "phone_contacts": [],
        "addresses": [],
        "contacts_trusted": False,
    }
    if str((source.get("verification") or {}).get("source") or "") == "quick_sourcer":
        return _public_record_projection(
            source, projected, blocked_contact_keys=blocked_contact_keys,
        )
    try:
        fresh = float(source.get("contact_expires_at") or 0) > time.time()
    except (TypeError, ValueError):
        fresh = False
    if not fresh:
        return projected

    contacts = trust_policy.trusted_provider_contacts(
        source, source, blocked_contact_keys=blocked_contact_keys,
    )
    if not contacts:
        return projected
    allowed = store.filter_dnc_groups(
        {
            "emails": contacts.get("emails") or [],
            "phones": contacts.get("phones") or [],
            "addresses": contacts.get("addresses") or [],
        },
        blocked_keys=blocked_contact_keys,
    )
    if not (allowed.get("emails") or allowed.get("phones")):
        return projected
    projected.update({
        "emails": list(allowed.get("emails") or []),
        "phones": list(allowed.get("phones") or []),
        "phone_contacts": [
            item for item in contacts.get("phone_contacts") or []
            if store.contact_key(item.get("value")) in {
                store.contact_key(value) for value in allowed.get("phones") or []
            }
        ],
        "addresses": list(allowed.get("addresses") or []),
        "contacts_trusted": True,
    })
    return projected


def project_candidates(candidates) -> list[dict]:
    """Project a candidate selection with one DNC database read."""
    rows = list(candidates or [])
    if not rows:
        return []
    blocked = store.dnc_contact_keys()
    return [
        project_candidate(candidate, blocked_contact_keys=blocked)
        for candidate in rows
    ]
