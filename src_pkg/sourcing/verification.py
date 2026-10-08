"""Deterministic identity and contact-evidence verification.

Identity resolution is deliberately rule based. AI may summarize evidence for a
reviewer, but it is never allowed to turn an ambiguous person into a verified
record or to attach contact data on its own.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from . import config, contact_validation, person_name, source_context

_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
_STATE_ALIASES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc",
}


def _normal(value) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _name_parts(value) -> list[str]:
    return list(person_name.identity_signature(value).get("tokens") or ())


def _name_suffix(value) -> str:
    return str(person_name.identity_signature(value).get("suffix") or "")


def name_evidence(expected, returned) -> dict:
    left, right = _name_parts(expected), _name_parts(returned)
    if not left or not right:
        return {
            "score": 0.0, "exact": False, "compatible": False,
            "surname_contained": False, "partial": False, "conflict": False,
        }
    left_signature = person_name.identity_signature(expected)
    right_signature = person_name.identity_signature(returned)
    first_match = left_signature["first"] == right_signature["first"]
    left_surname = tuple(left_signature.get("surname") or ())
    right_surname = tuple(right_signature.get("surname") or ())
    left_last = left_surname[-1] if left_surname else ""
    right_last = right_surname[-1] if right_surname else ""
    returned_tail = tuple((right_signature.get("tokens") or ())[1:])
    last_match = bool(
        left_surname
        and (
            left_surname == right_surname
            or (
                len(left_surname) > 1
                and len(returned_tail) >= len(left_surname)
                and returned_tail[-len(left_surname):] == left_surname
            )
        )
    )
    expected_suffix, returned_suffix = _name_suffix(expected), _name_suffix(returned)
    suffix_conflict = bool(
        expected_suffix and returned_suffix and expected_suffix != returned_suffix
    )
    # Match structured first and terminal surname. Arbitrary middle names are
    # already ignored by this comparison. A source surname merely occurring
    # in the middle of a longer provider name (``Jane Lee`` vs
    # ``Jane Lee Smith``) is not the same identity and must not auto-match.
    surname_contained = False
    last_initial = bool(
        len(left_surname) == len(right_surname) == 1
        and len(left_last) == 1 and right_last.startswith(left_last)
    )
    exact = bool(
        len(left) >= 2 and len(right) >= 2 and first_match and last_match
        and not suffix_conflict
    )
    compatible = exact
    partial = first_match and (last_match or last_initial)
    conflict = (len(left) >= 2 and len(right) >= 2) and (
        not first_match or not (last_match or last_initial) or suffix_conflict
    )
    return {
        "score": 1.0 if exact else (0.72 if partial else 0.0),
        "exact": exact, "compatible": compatible,
        "surname_contained": bool(surname_contained and not exact),
        "partial": partial, "conflict": conflict,
        "first_match": first_match, "last_match": last_match,
        "expected_suffix": expected_suffix, "returned_suffix": returned_suffix,
        "suffix_conflict": suffix_conflict,
    }


def _best_name_evidence(expected, primary, aliases, expected_aliases=None) -> dict:
    """Choose a primary/AKA match without allowing a relative to self-verify.

    Provider aliases describe the returned person and are admissible identity
    evidence. Relatives describe different people; they are deliberately not
    considered here, even when a relative has the source candidate's name.
    """
    candidates = [("primary", str(primary or ""))]
    candidates.extend(("alias", str(value or "")) for value in aliases or [])
    expected_names = [("source_name", str(expected or ""))]
    expected_names.extend(
        ("source_alias", str(value or "")) for value in expected_aliases or []
        if len(_name_parts(value)) >= 2
    )
    ranked = []
    for expected_source, expected_value in expected_names:
        for source, value in candidates:
            if not value.strip() or not expected_value.strip():
                continue
            evidence = name_evidence(expected_value, value)
            ranked.append((
                bool(evidence.get("exact")),
                bool(evidence.get("compatible")),
                float(evidence.get("score") or 0),
                expected_source == "source_name" and source == "primary",
                source, value, expected_source, expected_value, evidence,
            ))
    if not ranked:
        return name_evidence(expected, "")
    *_, source, value, expected_source, expected_value, best = max(
        ranked, key=lambda item: item[:4]
    )
    return {
        **best,
        "matched_value": value,
        "match_source": source,
        "alias_match": source == "alias" and bool(best.get("compatible")),
        "expected_value": expected_value,
        "expected_source": expected_source,
        "source_alias_match": (
            expected_source == "source_alias" and bool(best.get("compatible"))
        ),
    }


def _state(value: str) -> str:
    raw = str(value or "")
    normalized = _normal(raw)
    if not normalized:
        return ""

    # Prefer the right-hand address components. Searching the entire string in
    # dictionary order misclassifies cities named for another state, e.g.
    # "Kansas City, Missouri" as Kansas or "New York, Pennsylvania" as NY.
    components = [
        re.sub(r"\b\d{5}(?:\s+\d{4})?\b", "", _normal(component)).strip()
        for component in raw.split(",")
    ]
    components = [
        component for component in components
        if component and component not in {"united states", "united states of america", "usa", "us"}
    ]
    state_codes = set(_STATE_ALIASES.values())
    for component in reversed(components):
        if component in _STATE_ALIASES:
            return _STATE_ALIASES[component]
        if component in state_codes:
            return component

    # LinkedIn/other sources occasionally omit commas. In that case a state
    # must end the string rather than merely occur somewhere within a city.
    for name in sorted(_STATE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b$", normalized):
            return _STATE_ALIASES[name]
    last = normalized.split()[-1]
    if last in state_codes:
        return last
    return ""


def _city(value: str) -> str:
    first = str(value or "").split(",", 1)[0]
    return _normal(first)


def us_city_state(value: str) -> str:
    """Normalize a captured US location to ``City, ST`` for provider input."""
    raw = re.sub(r"\s+", " ", str(value or "")).strip(" ,")
    city = raw.split(",", 1)[0].strip() if raw else ""
    state = _state(raw)
    if not city or not state:
        return ""
    return f"{city}, {state.upper()}"


def location_evidence(expected: str, returned_values) -> dict:
    expected_city, expected_state = _city(expected), _state(expected)
    values = [str(value or "") for value in (returned_values or []) if str(value or "").strip()]
    if not expected_city or not expected_state:
        return {"score": 0.0, "exact": False, "state_match": False, "conflict": False}
    best = {"score": 0.0, "exact": False, "state_match": False, "conflict": False}
    saw_state = False
    for value in values:
        city, state = _city(value), _state(value)
        if not state:
            continue
        saw_state = True
        # A full address may put the city after the street component, but a
        # word-boundary substring is unsafe (York must not equal New York, and
        # Orange must not equal West Orange). Require one whole comma-delimited
        # address component to equal the expected city.
        # Enformion sometimes formats a full address as
        # ``street; city, state ZIP``.  Treat semicolons as address-component
        # separators too; otherwise the city is fused to the street and a
        # genuine exact city/state match is incorrectly rejected.
        city_components = {
            _normal(component)
            for component in re.split(r"[,;]", str(value or ""))
            if _normal(component)
        }
        city_present = expected_city in city_components
        if state == expected_state and (city == expected_city or city_present):
            return {"score": 1.0, "exact": True, "state_match": True, "conflict": False}
        if state == expected_state:
            best = {"score": 0.55, "exact": False, "state_match": True, "conflict": False}
    if saw_state and not best["state_match"]:
        best["conflict"] = True
    return best


def _structured_values(candidate: dict, prefixes: tuple[str, ...]) -> list[str]:
    values = []
    for line in str(candidate.get("notes") or "").splitlines():
        cleaned = line.strip()
        for prefix in prefixes:
            if cleaned.casefold().startswith(prefix):
                values.append(cleaned.split(":", 1)[-1].strip())
    return [value for value in values if value]


def _social_profile_key(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/").casefold()
    if host == "facebook.com" or host.endswith(".facebook.com"):
        if path == "/profile.php":
            identity = (parse_qs(parsed.query).get("id") or [""])[0].casefold()
            return f"facebook:id:{identity}" if identity else ""
        parts = [part for part in path.split("/") if part]
        return f"facebook:{parts[0]}" if len(parts) == 1 else ""
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[0] == "in":
            return f"linkedin:{parts[1]}"
    return ""


def _token_overlap(left_values, right_values) -> float:
    """Return the strongest individual source/provider value agreement.

    Joining a candidate's entire job history before comparing it can create a
    synthetic match from unrelated generic tokens spread across several jobs.
    Pairwise comparison keeps employer/school evidence positive-only and tied
    to one actual organization or role on each side.
    """
    stop = {"the", "and", "of", "at", "inc", "llc"}
    best = 0.0
    for left_value in left_values or []:
        left = set(_normal(left_value).split()) - stop
        if not left:
            continue
        for right_value in right_values or []:
            right = set(_normal(right_value).split()) - stop
            if not right:
                continue
            score = len(left & right) / min(len(left), len(right))
            best = max(best, score)
    return round(best, 3)


def _email_checks(emails: list[str]) -> list[dict]:
    checks = []
    for email in emails:
        check = {
            "value": email,
            "format_valid": bool(_EMAIL_RE.fullmatch(str(email or "").strip())),
            "deliverability": "not_checked",
        }
        if check["format_valid"]:
            check.update(contact_validation.verify_email(email))
        checks.append(check)
    return checks


def _phone_checks(phones: list[str]) -> list[dict]:
    checks = []
    for phone in phones:
        digits = re.sub(r"\D", "", str(phone or ""))
        check = {
            "value": phone, "format_valid": 10 <= len(digits) <= 15,
            "line_status": "not_checked", "identity_owner": "not_checked",
        }
        if check["format_valid"]:
            check.update(contact_validation.verify_phone(phone))
        checks.append(check)
    return checks


def assess(candidate: dict, result: dict, *, identity_only: bool = False) -> dict:
    """Score identity evidence and keep contacts only for a verified identity."""
    source = _normal(result.get("source", "")).replace(" ", "_") or "enrichment_provider"
    emails = [str(value).strip() for value in result.get("emails", []) if str(value).strip()]
    phones = [str(value).strip() for value in result.get("phones", []) if str(value).strip()]
    email_checks = [] if identity_only else _email_checks(emails)
    phone_checks = [] if identity_only else _phone_checks(phones)
    valid_contacts = True if identity_only else any(
        item["format_valid"] for item in email_checks + phone_checks
    )

    name = _best_name_evidence(
        candidate.get("name", ""), result.get("matched_name", ""),
        result.get("provider_aliases") or result.get("name_aliases")
        or result.get("aliases") or [],
        result.get("source_name_aliases")
        or person_name.source_alternate_names(candidate.get("notes") or ""),
    )
    locations = [result.get("provider_location", ""), *(result.get("addresses") or [])]
    location = location_evidence(candidate.get("location", ""), locations)
    matched_address_kind = ""
    for item in result.get("address_evidence") or []:
        if not isinstance(item, dict):
            continue
        address_match = location_evidence(
            candidate.get("location", ""), [item.get("value") or ""],
        )
        if not address_match.get("exact"):
            continue
        if item.get("is_current") is True:
            matched_address_kind = "current"
            break
        seen = str(item.get("last_seen") or item.get("last_reported") or "")
        year_match = re.search(r"\b(19|20)\d{2}\b", seen)
        if year_match:
            from datetime import date
            age = max(0, date.today().year - int(year_match.group(0)))
            kind = "recent" if age <= 5 else "historical"
        else:
            kind = "associated_undated"
        if matched_address_kind not in {"current", "recent"}:
            matched_address_kind = kind
    matched_input_list = list(dict.fromkeys(
        _normal(value) for value in result.get("matched_inputs", []) if _normal(value)
    ))
    matched_inputs = set(matched_input_list)
    expected_social = _social_profile_key(candidate.get("source_url", ""))
    returned_social = _social_profile_key(result.get("profile_url", ""))
    exact_social_profile_match = bool(
        expected_social and returned_social and expected_social == returned_social
    )
    provider_profile_match = bool(expected_social and "profile" in matched_inputs)
    social_profile_match = exact_social_profile_match or provider_profile_match
    # A state/region-only match is useful corroboration but is not an exact
    # location. Treating it as exact could attach one same-name person in a
    # different city. PDL's location/locality/street-address matches remain
    # eligible when they do not conflict with the returned address.
    provider_location_match = bool(
        matched_inputs & {"location", "locality", "street address"}
    )
    provider_region_match = "region" in matched_inputs
    if provider_location_match and not location["conflict"]:
        location["score"] = max(location["score"], 0.85)

    source_facts = source_context.extract(candidate)
    source_roles = list(source_facts.get("roles") or [])
    if not source_roles:
        source_roles = [str(candidate.get("notes") or "").splitlines()[0]] if str(candidate.get("notes") or "").strip() else []
    source_orgs = [
        *(source_facts.get("companies") or []),
        *(source_facts.get("schools") or []),
    ]
    provider_roles = [result.get("provider_job_title", ""), *(result.get("provider_roles") or [])]
    provider_orgs = [result.get("provider_company", ""), *(result.get("provider_organizations") or [])]
    role_overlap = _token_overlap(source_roles, provider_roles)
    organization_overlap = _token_overlap(source_orgs, provider_orgs)
    provider_org_input_match = bool(matched_inputs & {"company", "school"})
    strong_organization_match = bool(
        provider_org_input_match or organization_overlap >= 0.8
    )
    corroborators = sum(value >= 0.5 for value in (role_overlap, organization_overlap))

    try:
        provider_score = max(0.0, min(1.0, float(result.get("confidence") or 0)))
    except (TypeError, ValueError):
        provider_score = 0.0
    score = round(
        (name["score"] * 0.48) + (location["score"] * 0.27)
        + (provider_score * 0.10) + (role_overlap * 0.08)
        + (organization_overlap * 0.07), 3,
    )
    # An exact returned social URL, or PDL's explicit ``matched=profile``
    # evidence, plus an exact visible name is strong identity evidence when
    # Facebook exposes no current city. Retain any visible location conflict.
    if social_profile_match and name.get("compatible"):
        score = max(score, round(0.82 + (provider_score * 0.10), 3))
    # A same-state move is useful only with independent workplace/school
    # corroboration. State alone is never treated as a 90% identity match.
    state_and_organization_match = bool(
        location.get("state_match") and strong_organization_match
        and provider_score >= 0.6
        # PDL explicitly reports which supplied input fields matched. A
        # provider's returned employer alone is not independent proof because
        # common employers can contain many same-name people.
        and provider_org_input_match
        and result.get("allow_state_context_match") is True
    )
    if state_and_organization_match and name.get("exact"):
        score = max(score, round(0.79 + (provider_score * 0.10), 3))
    selection = result.get("selection") or {}
    selected_identity = selection.get("identity_evidence") or {}
    try:
        selection_score = float(
            selection.get("selection_score")
            or selected_identity.get("score") or 0
        )
        winner_margin = float(
            selection.get("winner_margin")
            if selection.get("winner_margin") is not None else 1.0
        )
    except (TypeError, ValueError):
        selection_score, winner_margin = 0.0, 0.0
    ranked_provider_identity = bool(
        source == "enformion"
        and selection.get("selection") == "ranked_identity_match"
        and selected_identity.get("admissible") is True
        and selection_score >= config.IDENTITY_MATCH_THRESHOLD
        and winner_margin >= config.IDENTITY_AMBIGUITY_MARGIN
    )
    if ranked_provider_identity:
        score = max(score, round(selection_score, 3))
    conflicts = []
    if name["conflict"]:
        conflicts.append("name")
    if location["conflict"]:
        conflicts.append("location")

    full_name = len(_name_parts(candidate.get("name", ""))) >= 2
    name_anchor = bool(name.get("exact"))
    identity_verified = (
        result.get("status") == "success"
        and valid_contacts and not conflicts and name_anchor and full_name
        and (
            location["exact"] or provider_location_match or social_profile_match
            or state_and_organization_match or ranked_provider_identity
        )
        and score >= config.IDENTITY_MATCH_THRESHOLD
    )
    # Partial surnames need a separate discovery/corroboration stage; a direct
    # provider contact response is never sufficient on its own.
    if identity_verified:
        identity_status = "verified"
    elif result.get("status") != "success" or not valid_contacts:
        identity_status = "no_match"
    elif conflicts:
        identity_status = "rejected"
    else:
        identity_status = "review"

    return {
        "identity_status": identity_status,
        "identity_confidence": score,
        # Keep the stable method identifier for stored-record compatibility;
        # the evidence fields below carry the policy detail.
        "method": "deterministic_identity_evidence_v1",
        "source": source,
        "evidence": {
            "name": name, "location": location,
            "provider_confidence": provider_score,
            "social_profile_match": social_profile_match,
            "exact_social_profile_match": exact_social_profile_match,
            "provider_profile_match": provider_profile_match,
            "provider_location_match": provider_location_match,
            "provider_region_match": provider_region_match,
            "matched_address_kind": matched_address_kind,
            "expected_social_profile": expected_social,
            "returned_social_profile": returned_social,
            "matched_inputs": matched_input_list,
            "role_overlap": role_overlap,
            "organization_overlap": organization_overlap,
            "provider_organization_input_match": provider_org_input_match,
            "strong_organization_match": strong_organization_match,
            "state_and_organization_match": state_and_organization_match,
            "ranked_provider_identity": ranked_provider_identity,
            "provider_selection_score": selection_score,
            "provider_winner_margin": winner_margin,
            "provider_selection_evidence": selected_identity,
            "corroborating_signals": corroborators,
            "conflicts": conflicts,
            "required_field": str(result.get("required_field") or "usable_contact"),
            # Relatives are retained for audit/discovery only. A provider's
            # own relative list cannot independently prove that the returned
            # person is the source candidate.
            "provider_relatives": list(result.get("provider_relatives") or [])[:20],
            "relative_identity_used": False,
            "local_validation_bypassed": False,
        },
        "emails": email_checks,
        "phones": phone_checks,
        "disclaimer": (
            "Identity verification means the provider record matched deterministic source facts. "
            "Email deliverability and phone ownership remain separate checks unless shown otherwise."
        ),
    }
