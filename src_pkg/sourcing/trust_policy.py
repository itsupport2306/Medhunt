"""Current-policy checks for reusing provider contacts from the database.

Database presence is not trust. A stored contact may be stale or may have been
accepted by an older, weaker policy. These helpers re-evaluate the stored
evidence against today's thresholds before a zero-credit reuse.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from . import config, person_name, phone_policy, store, verification


CONTACT_TRUST_POLICY = "provider_contact_reuse_v4_callable_phone_fallback"


def _normal(value) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _profile_key(candidate: dict) -> str:
    source = _normal(candidate.get("source"))
    source_id = _normal(candidate.get("source_id"))
    if source_id:
        return f"{source}:{source_id}"
    raw_url = str(candidate.get("source_url") or "").strip()
    try:
        parsed = urlparse(raw_url)
        path = parsed.path.rstrip("/").casefold()
        return f"{source}:{(parsed.hostname or '').casefold()}:{path}"
    except ValueError:
        return f"{source}:{_normal(raw_url)}"


def linkedin_profile_identity(candidate: dict) -> str:
    """Return the stable LinkedIn member slug for an explicit source profile."""
    if _normal(candidate.get("source")) != "linkedin":
        return ""
    source_id = str(candidate.get("source_id") or "").strip().casefold()
    if source_id:
        return source_id
    raw_url = str(candidate.get("source_url") or "").strip()
    try:
        parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    parts = [part.casefold() for part in parsed.path.split("/") if part]
    if (host == "linkedin.com" or host.endswith(".linkedin.com")) and (
        len(parts) == 2 and parts[0] == "in"
    ):
        return parts[1]
    return ""


def source_identity_fingerprint(candidate: dict) -> str:
    # Provider matching may use a transient Facebook hometown on a controlled
    # second pass. Contacts still belong to the captured source profile whose
    # durable identity includes its current/Lives location, so never bind the
    # reusable contact proof to the temporary lookup location.
    identity_location = candidate.get(
        "_source_identity_location", candidate.get("location")
    )
    identity_parts = [
        CONTACT_TRUST_POLICY,
        _normal(person_name.normalize_person_name(candidate.get("name"))),
        _normal(identity_location),
    ]
    hometown = _normal(candidate.get("hometown"))
    if hometown:
        identity_parts.extend(("facebook_hometown", hometown))
    identity_parts.append(_profile_key(candidate))
    material = "|".join(identity_parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _pdl_is_current(verification_record: dict) -> bool:
    evidence = verification_record.get("evidence") or {}
    name = evidence.get("name") or {}
    location = evidence.get("location") or {}
    likelihood = _number(evidence.get("pdl_likelihood"))
    conflicts = evidence.get("conflicts") or []
    social_match = bool(evidence.get("social_profile_match"))
    source_profile_expected = bool(evidence.get("expected_social_profile"))
    location_match = bool(location.get("exact") or evidence.get("provider_location_match"))
    context_match = max(
        _number(evidence.get("role_overlap")),
        _number(evidence.get("organization_overlap")),
    )
    supporting_rule = bool(
        (social_match and likelihood >= config.PDL_PROFILE_ACCEPT_MIN_LIKELIHOOD)
        or (location_match and likelihood >= config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD)
        or (
            evidence.get("state_and_organization_match") is True
            and likelihood >= config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD
        )
        or (
            context_match >= 0.5
            and likelihood >= max(9, config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD)
        )
    )
    return bool(
        verification_record.get("identity_status") in ("verified", "recruiter_confirmed")
        and name.get("compatible", name.get("exact"))
        and not conflicts
        and (
            not source_profile_expected or social_match
            or (
                location_match
                and evidence.get("strong_organization_match") is True
                and likelihood >= config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD
            )
        )
        and supporting_rule
    )


def _enformion_is_current(contact_evidence: dict) -> bool:
    return bool(
        contact_evidence.get("status") in (
            "enformion_direct", "enformion_fallback", "enformion_supplemented",
        )
        and contact_evidence.get("automatic_use_allowed") is True
        and contact_evidence.get("enformion_identity_status") == "verified"
        and _number(contact_evidence.get("enformion_identity_confidence"))
            >= config.IDENTITY_MATCH_THRESHOLD
        and (
            contact_evidence.get("enformion_name_exact") is True
            or contact_evidence.get("enformion_name_alias_match") is True
        )
        and (
            contact_evidence.get("enformion_location_exact") is True
            or contact_evidence.get("enformion_ranked_identity_match") is True
        )
        and _number(contact_evidence.get("enformion_winner_margin"), 1.0)
            >= config.IDENTITY_AMBIGUITY_MARGIN
        and not (contact_evidence.get("enformion_conflicts") or [])
    )


def _same_current_source_identity(candidate: dict, source: dict, evidence: dict) -> bool:
    if int(candidate.get("id") or 0) == int(source.get("id") or 0):
        # Older builds could copy a master record's verification onto an alias
        # and rewrite only the fingerprint.  That is not independent proof for
        # a particular LinkedIn member, so do not grandfather those contacts.
        if linkedin_profile_identity(candidate) and evidence.get("internal_contact_reuse"):
            return False
        return bool(
            evidence.get("source_identity_fingerprint")
            == source_identity_fingerprint(candidate)
        )

    # A captured LinkedIn /in/ URL identifies a particular source profile.
    # Same-name/same-city evidence cannot authorize copying contacts from a
    # different LinkedIn slug, even when an older rejected lookup left the same
    # provider_person_id or master_candidate_id on both database rows.
    linkedin_profile = linkedin_profile_identity(candidate)
    if linkedin_profile and linkedin_profile != linkedin_profile_identity(source):
        return False

    # Cross-platform master reuse is permitted only when the newly captured
    # identity independently agrees with the verified source on full name and
    # exact city/state. A shared provider ID alone is not sufficient.
    matched_name = source.get("canonical_name") or source.get("name") or ""
    provider_location = (
        evidence.get("provider_location") or source.get("location") or ""
    )
    name = verification.name_evidence(candidate.get("name", ""), matched_name)
    location = verification.location_evidence(
        candidate.get("location", ""), [provider_location, source.get("location", "")],
    )
    return bool(name.get("exact") and location.get("exact") and not name.get("conflict"))


def trusted_provider_contacts(
    candidate: dict, source: dict, *, blocked_contact_keys=None,
) -> dict | None:
    """Return provider-originated values only when all current trust rules pass."""
    verification_record = source.get("verification") or {}
    evidence = verification_record.get("evidence") or {}
    contact_evidence = evidence.get("contact_verification") or {}
    provider_contacts = verification_record.get("provider_contacts") or {}
    status = str(contact_evidence.get("status") or "")

    if evidence.get("contact_trust_policy") != CONTACT_TRUST_POLICY:
        return None
    if source.get("identity_status") not in ("verified", "recruiter_confirmed"):
        return None
    if not _same_current_source_identity(candidate, source, evidence):
        return None
    if not isinstance(provider_contacts, dict):
        return None

    if status in ("enformion_direct", "enformion_fallback", "enformion_supplemented"):
        if not _enformion_is_current(contact_evidence):
            return None
        if linkedin_profile_identity(candidate) and status != "enformion_supplemented":
            return None
        if status == "enformion_supplemented":
            if not _pdl_is_current(verification_record):
                return None
            if linkedin_profile_identity(candidate) and not (
                contact_evidence.get("cross_provider_contact_overlap") is True
                and (
                    contact_evidence.get("cross_provider_overlap_emails")
                    or contact_evidence.get("cross_provider_overlap_phones")
                )
            ):
                return None
    elif status == "pdl_high_confidence" or verification_record.get("source") == "people_data_labs":
        if not _pdl_is_current(verification_record):
            return None
    else:
        return None

    phone_candidates = [
        *(provider_contacts.get("phones") or []),
        *(
            item.get("value") for item in evidence.get("pdl_phone_evidence") or []
            if item.get("value")
        ),
        *(
            item.get("value") for item in evidence.get("pdl_associated_phone_evidence") or []
            if item.get("value")
        ),
        *(
            item.get("value") for item in contact_evidence.get("enformion_phone_evidence") or []
            if item.get("value")
        ),
    ]
    phone_record = {
        "phones": phone_candidates,
        "verification": verification_record,
    }
    admissible = phone_policy.admissible_phone_values(phone_record)
    allowed_groups = store.filter_dnc_groups(
        {
            "emails": provider_contacts.get("emails") or [],
            "phones": admissible,
            "addresses": provider_contacts.get("addresses") or [],
        },
        blocked_keys=blocked_contact_keys,
    )
    phone_record["phones"] = allowed_groups.get("phones") or []
    phone_contacts = phone_policy.preferred_phone_details(phone_record)
    phones = [item["value"] for item in phone_contacts]
    if phones and evidence.get("phone_policy") not in {
        phone_policy.MOBILE_PHONE_POLICY, phone_policy.OTHER_PHONE_POLICY,
    }:
        return None
    emails = [
        str(value).strip() for value in allowed_groups.get("emails") or []
        if str(value).strip()
    ]
    addresses = [
        str(value).strip() for value in allowed_groups.get("addresses") or []
        if str(value).strip()
    ]
    if not (emails or phones):
        return None
    return {
        "emails": emails[:20], "phones": phones[:20],
        "phone_contacts": phone_contacts[:20], "addresses": addresses[:20],
    }
