"""Shared provenance and preference rules for provider-supplied phones."""
from __future__ import annotations

import re
from datetime import datetime, timezone


MOBILE_PHONE_POLICY = "explicit_mobile_or_wireless_v1"
OTHER_PHONE_POLICY = "mobile_preferred_callable_fallback_v1"

_MOBILE_TYPE_TOKENS = {"mobile", "wireless", "cell", "cellular"}
_CONFLICTING_TYPE_TOKENS = {
    "business", "fax", "fixed", "home", "landline", "office", "pager", "voip", "work",
}
_NON_CALLABLE_TYPE_TOKENS = {"fax", "pager"}
_UNUSABLE_STATUS_TOKENS = {"disconnected", "inactive", "invalid"}


def normalized_phone_type(value) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def is_explicit_mobile_type(value) -> bool:
    """Return true only for an explicit, non-contradictory mobile classification."""
    tokens = set(normalized_phone_type(value).split())
    return bool(tokens & _MOBILE_TYPE_TOKENS) and not bool(tokens & _CONFLICTING_TYPE_TOKENS)


def evidence_accepts_mobile(item: dict | None, *, require_connected: bool = True) -> bool:
    item = item or {}
    return bool(
        item.get("accepted") is True
        and item.get("mobile_or_wireless") is True
        and (not require_connected or item.get("is_connected") is True)
    )


def _phone_key(value) -> str:
    """Return a formatting-insensitive key without weakening number identity."""
    digits = re.sub(r"\D", "", str(value or ""))
    return (digits[-10:] if len(digits) >= 10 else digits) or normalized_phone_type(value)


def _source_fields(item: dict) -> set[str]:
    raw_fields = item.get("source_fields") or []
    if isinstance(raw_fields, str):
        raw_fields = [raw_fields]
    return {
        normalized_phone_type(value).replace(" ", "_")
        for value in [item.get("source_field"), *raw_fields]
        if str(value or "").strip()
    }


def _mobile_evidence_accepts(item: dict | None) -> bool:
    """Re-evaluate one PDL or Enformion evidence item fail-closed.

    PDL's dedicated ``mobile_phone`` field is itself an explicit mobile
    classification and does not provide a connected flag. Enformion evidence
    has no source-field marker, so it must contain both an explicit mobile line
    type and ``is_connected=true``. Broad PDL phone-history fields are never
    promoted to usable contacts, even if their cached flags are inconsistent.
    """
    if not isinstance(item, dict) or not str(item.get("value") or "").strip():
        return False
    if item.get("is_connected") is False or item.get("is_current") is False:
        return False
    status_text = " ".join(
        normalized_phone_type(item.get(field))
        for field in ("status", "line_status", "phone_status", "connection_status")
    )
    if set(status_text.split()) & _UNUSABLE_STATUS_TOKENS:
        return False
    source_fields = _source_fields(item)
    raw_type = item.get("type") or item.get("normalized_type") or ""
    dedicated_pdl_mobile = "mobile_phone" in source_fields
    if source_fields and not dedicated_pdl_mobile:
        return False
    if raw_type and not is_explicit_mobile_type(raw_type):
        return False
    if dedicated_pdl_mobile:
        return evidence_accepts_mobile(item, require_connected=False)
    return bool(
        is_explicit_mobile_type(raw_type)
        and evidence_accepts_mobile(item, require_connected=True)
    )


def _other_evidence_accepts(item: dict | None) -> bool:
    """Return true for a provider-associated, callable non-mobile number.

    PDL's ``phone_numbers``/``phones`` collections are associated-number
    evidence and do not expose connectivity. Enformion does expose line
    connectivity, so its fallback values must be connected. Fax and pager
    records are never treated as callable candidate numbers.
    """
    if not isinstance(item, dict) or not str(item.get("value") or "").strip():
        return False
    if item.get("is_connected") is False or item.get("is_current") is False:
        return False
    raw_type = item.get("type") or item.get("normalized_type") or ""
    type_tokens = set(normalized_phone_type(raw_type).split())
    if type_tokens & _NON_CALLABLE_TYPE_TOKENS:
        return False
    status_text = " ".join(
        normalized_phone_type(item.get(field))
        for field in ("status", "line_status", "phone_status", "connection_status")
    )
    if set(status_text.split()) & _UNUSABLE_STATUS_TOKENS:
        return False
    source_fields = _source_fields(item)
    if "mobile_phone" in source_fields:
        return False
    if source_fields:
        return bool(source_fields & {"phone_numbers", "phones"})
    return bool(
        item.get("is_connected") is True
        and item.get("is_current") is not False
    )


def _phone_evidence_groups(record: dict | None):
    source = record or {}
    verification = source.get("verification") or {}
    evidence = verification.get("evidence") or {}
    contact = source.get("contact_verification") or evidence.get("contact_verification") or {}
    nested_enformion = source.get("enformion") or {}
    contact_status = str(contact.get("status") or "")
    accepted_contact = contact if (
        not contact_status
        or (
            contact.get("automatic_use_allowed") is True
            and contact_status in {
                "enformion_direct", "enformion_fallback", "enformion_supplemented",
            }
        )
    ) else {}
    return (
        source.get("phone_evidence") or [],
        source.get("associated_phone_evidence") or [],
        source.get("pdl_phone_evidence") or [],
        source.get("pdl_associated_phone_evidence") or [],
        evidence.get("pdl_phone_evidence") or [],
        evidence.get("pdl_associated_phone_evidence") or [],
        source.get("enformion_phone_evidence") or [],
        evidence.get("enformion_phone_evidence") or [],
        accepted_contact.get("enformion_phone_evidence") or [],
        nested_enformion.get("phone_evidence") or [],
    )


def _evidence_keys(record: dict | None, predicate) -> set[str]:
    return {
        _phone_key(item.get("value"))
        for group in _phone_evidence_groups(record)
        for item in group
        if predicate(item)
    }


def accepted_mobile_phone_values(record: dict | None) -> list[str]:
    """Return only record phones backed by current PDL/Enformion evidence.

    The helper understands direct provider results as well as the combined
    verification shape written by the provider waterfall. Rejected historical
    evidence remains available for audit but can never authorize a phone value.
    """
    source = record or {}
    allowed = _evidence_keys(source, _mobile_evidence_accepts)
    output, seen = [], set()
    for value in source.get("phones") or []:
        cleaned = str(value or "").strip()
        key = _phone_key(cleaned)
        if not cleaned or not key or key not in allowed or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
        if len(output) >= 20:
            break
    return output


def admissible_phone_values(record: dict | None) -> list[str]:
    """Return every evidenced callable candidate before tier preference."""
    source = record or {}
    allowed = (
        _evidence_keys(source, _mobile_evidence_accepts)
        | _evidence_keys(source, _other_evidence_accepts)
    )
    output, seen = [], set()
    for value in source.get("phones") or []:
        cleaned = str(value or "").strip()
        key = _phone_key(cleaned)
        if not cleaned or not key or key not in allowed or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
        if len(output) >= 20:
            break
    return output


def preferred_phone_details(record: dict | None) -> list[dict]:
    """Prefer mobile/wireless numbers, otherwise return callable alternatives.

    Values must exist in the record's selected ``phones`` list and be backed by
    current provider evidence. The public label is deliberately vendor-neutral.
    """
    source = record or {}
    candidate_keys = {
        _phone_key(value) for value in source.get("phones") or []
        if _phone_key(value)
    }
    mobile = _evidence_keys(source, _mobile_evidence_accepts) & candidate_keys
    other = _evidence_keys(source, _other_evidence_accepts) & candidate_keys
    selected_keys = mobile or other
    kind = "mobile" if mobile else "other"
    label = "Mobile" if mobile else "Other phone"
    output, seen = [], set()
    for value in source.get("phones") or []:
        cleaned = str(value or "").strip()
        key = _phone_key(cleaned)
        if not cleaned or not key or key not in selected_keys or key in seen:
            continue
        seen.add(key)
        output.append({"value": cleaned, "kind": kind, "label": label})
        if len(output) >= 20:
            break
    return output


def preferred_phone_values(record: dict | None) -> list[str]:
    return [item["value"] for item in preferred_phone_details(record)]


def _reported_timestamp(value) -> float:
    """Parse the bounded date shapes returned by the contact providers."""
    if value in (None, "") or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return numeric if numeric > 0 else 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    if re.fullmatch(r"\d{10,13}(?:\.\d+)?", text):
        numeric = float(text)
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        pass
    for pattern in (
        "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%Y%m%d",
        "%b %d, %Y", "%B %d, %Y", "%b %Y", "%B %Y", "%Y-%m", "%Y",
    ):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def latest_phone_detail(record: dict | None) -> dict | None:
    """Choose one latest phone from an already approved contact projection.

    The normal provider policy still decides which tier is usable: accepted
    mobile/wireless numbers win over callable alternatives. Recency only
    orders numbers inside that tier, so a newer landline cannot displace an
    accepted mobile number. ``phone_contacts`` is a deliberate fallback for
    vendor-neutral projections that already passed their caller's DNC gate.
    """
    source = record or {}
    selected = preferred_phone_details(source)
    if not selected:
        candidate_values = {
            _phone_key(value) for value in source.get("phones") or [] if _phone_key(value)
        }
        projected, seen = [], set()
        for item in source.get("phone_contacts") or []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            key = _phone_key(value)
            kind = "mobile" if str(item.get("kind") or "").casefold() == "mobile" else "other"
            if not value or key not in candidate_values or key in seen:
                continue
            seen.add(key)
            projected.append({
                "value": value,
                "kind": kind,
                "label": "Mobile" if kind == "mobile" else "Other phone",
            })
        mobile = [item for item in projected if item["kind"] == "mobile"]
        selected = mobile or projected
    if not selected:
        # Backward compatibility for approved projections written before
        # phone_contacts existed. Resume enrichment never passes a raw provider
        # response to this helper.
        first = next(
            (str(value or "").strip() for value in source.get("phones") or []
             if str(value or "").strip()),
            "",
        )
        if not first:
            return None
        selected = [{"value": first, "kind": "other", "label": "Phone"}]

    verification_record = (source.get("verification") or {}).get("record") or {}
    groups = [*_phone_evidence_groups(source), verification_record.get("phones") or []]
    evidence_by_key: dict[str, list[tuple[int, dict]]] = {}
    ordinal = 0
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            key = _phone_key(item.get("value") or item.get("number") or item.get("phone"))
            if key:
                evidence_by_key.setdefault(key, []).append((ordinal, item))
            ordinal += 1

    def item_rank(index: int, detail: dict) -> tuple:
        key = _phone_key(detail.get("value"))
        best = (0, 0.0, 0, 0, -10_000_000)
        for evidence_index, item in evidence_by_key.get(key) or []:
            reported = max(
                _reported_timestamp(item.get("last_seen")),
                _reported_timestamp(item.get("last_reported")),
                _reported_timestamp(item.get("lastReportedDate")),
            )
            try:
                sources = max(0, int(item.get("num_sources") or 0))
            except (TypeError, ValueError):
                sources = 0
            rank = (
                int(item.get("is_current") is True),
                reported,
                int(item.get("is_connected") is True),
                sources,
                -evidence_index,
            )
            if rank > best:
                best = rank
        return (*best, -index)

    _, detail = max(enumerate(selected), key=lambda pair: item_rank(*pair))
    key = _phone_key(detail.get("value"))
    reported = max(
        (
            max(
                _reported_timestamp(item.get("last_seen")),
                _reported_timestamp(item.get("last_reported")),
                _reported_timestamp(item.get("lastReportedDate")),
            )
            for _, item in evidence_by_key.get(key) or []
        ),
        default=0.0,
    )
    return {
        **detail,
        "reported_at": reported,
        "has_reported_recency": bool(reported),
    }


def messaging_phone_detail(record: dict | None) -> dict | None:
    """Choose the SMS destination using the product's explicit priority.

    Use the most recently reported wireless number when recency is available,
    otherwise the first wireless number supplied by the provider. If the
    candidate has no accepted wireless number, use the first callable phone.
    """
    source = record or {}
    selected = preferred_phone_details(source)
    if not selected:
        candidate_keys = {
            _phone_key(value) for value in source.get("phones") or [] if _phone_key(value)
        }
        projected, seen = [], set()
        for item in source.get("phone_contacts") or []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            key = _phone_key(value)
            kind = "mobile" if str(item.get("kind") or "").casefold() == "mobile" else "other"
            if not value or key not in candidate_keys or key in seen:
                continue
            seen.add(key)
            projected.append({
                "value": value,
                "kind": kind,
                "label": "Mobile" if kind == "mobile" else "Other phone",
            })
        wireless = [item for item in projected if item["kind"] == "mobile"]
        selected = wireless or projected
    if not selected:
        first = next(
            (str(value or "").strip() for value in source.get("phones") or []
             if str(value or "").strip()),
            "",
        )
        if not first:
            return None
        return {
            "value": first,
            "kind": "other",
            "label": "Phone",
            "reported_at": 0.0,
            "has_reported_recency": False,
        }

    # Callable fallback numbers retain provider order. Recency only selects
    # among wireless numbers, matching the SMS priority promised to users.
    if selected[0].get("kind") != "mobile":
        return {**selected[0], "reported_at": 0.0, "has_reported_recency": False}

    verification_record = (source.get("verification") or {}).get("record") or {}
    groups = [*_phone_evidence_groups(source), verification_record.get("phones") or []]
    reported_by_key: dict[str, float] = {}
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            key = _phone_key(item.get("value") or item.get("number") or item.get("phone"))
            if not key:
                continue
            reported_by_key[key] = max(
                reported_by_key.get(key, 0.0),
                _reported_timestamp(item.get("last_seen")),
                _reported_timestamp(item.get("last_reported")),
                _reported_timestamp(item.get("lastReportedDate")),
            )

    best_index, best_reported = 0, 0.0
    for index, detail in enumerate(selected):
        reported = reported_by_key.get(_phone_key(detail.get("value")), 0.0)
        if reported > best_reported:
            best_index, best_reported = index, reported
    detail = selected[best_index] if best_reported else selected[0]
    return {
        **detail,
        "reported_at": best_reported,
        "has_reported_recency": bool(best_reported),
    }


def selected_phone_policy(record: dict | None) -> str:
    details = preferred_phone_details(record)
    if details and details[0]["kind"] == "other":
        return OTHER_PHONE_POLICY
    return MOBILE_PHONE_POLICY


def verification_phone_policy(record: dict | None) -> str:
    verification = (record or {}).get("verification") or {}
    evidence = verification.get("evidence") or {}
    contact_evidence = evidence.get("contact_verification") or {}
    return str(evidence.get("phone_policy") or contact_evidence.get("phone_policy") or "")
