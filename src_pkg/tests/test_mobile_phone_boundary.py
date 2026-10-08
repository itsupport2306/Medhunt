from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import multi_provider, phone_policy, store


def _pdl_verification(*, phone_evidence=None) -> dict:
    return {
        "identity_status": "verified",
        "evidence": {
            "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
            "pdl_quality_gate": {"accepted": True},
            "pdl_phone_evidence": list(phone_evidence or []),
        },
    }


def _pdl_mobile(value: str) -> dict:
    return {
        "value": value,
        "type": "mobile",
        "source_field": "mobile_phone",
        "source_fields": ["mobile_phone"],
        "mobile_or_wireless": True,
        "is_connected": None,
        "accepted": True,
    }


def _enformion_mobile(value: str, *, connected=True) -> dict:
    return {
        "value": value,
        "type": "Wireless",
        "mobile_or_wireless": True,
        "is_connected": connected,
        "accepted": connected,
    }


def test_shared_mobile_filter_accepts_only_backed_pdl_and_enformion_values():
    pdl_value = "+1 404 555 0101"
    enformion_value = "(404) 555-0102"
    forged_landline = "(404) 555-0103"
    disconnected_mobile = "(404) 555-0104"
    result = {
        "phones": [
            pdl_value, enformion_value, forged_landline,
            disconnected_mobile, pdl_value,
        ],
        "phone_evidence": [
            _pdl_mobile("+14045550101"),
            {
                "value": forged_landline,
                "type": "Landline",
                "source_field": "phone_numbers",
                "mobile_or_wireless": True,
                "is_connected": True,
                "accepted": True,
            },
        ],
        "verification": {"evidence": {"contact_verification": {
            "enformion_phone_evidence": [
                _enformion_mobile(enformion_value),
                _enformion_mobile(disconnected_mobile, connected=False),
            ],
        }}},
    }

    assert phone_policy.accepted_mobile_phone_values(result) == [
        pdl_value, enformion_value,
    ]


def test_pdl_boundary_uses_associated_phone_as_labeled_fallback():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    candidate = store.get_candidate(candidate_id)
    unsafe_phone = "(404) 555-0199"
    result = {
        "status": "success",
        "provider": "people_data_labs",
        "emails": ["jane@example.test"],
        "phones": [unsafe_phone],
        "addresses": ["Atlanta, GA"],
        "verification": _pdl_verification(phone_evidence=[{
            "value": unsafe_phone,
            "type": "Landline",
            "source_field": "phone_numbers",
            "mobile_or_wireless": False,
            "is_connected": True,
            "accepted": False,
        }]),
    }

    assert multi_provider._pdl_accepted(result) is True
    assert multi_provider._complete(result) is False
    assert multi_provider._fallback_reason(result) == "pdl_missing_contact_type"

    marked = multi_provider._mark_primary_success(candidate, result)
    assert marked["contacts_trusted"] is True
    assert marked["emails"] == ["jane@example.test"]
    assert marked["phones"] == [unsafe_phone]
    assert phone_policy.preferred_phone_details(marked) == [{
        "value": unsafe_phone, "kind": "other", "label": "Other phone",
    }]
    stored = store.get_candidate(candidate_id)
    assert stored["verification"]["provider_contacts"]["phones"] == [unsafe_phone]
    assert stored["verification"]["evidence"]["phone_policy"] == (
        phone_policy.OTHER_PHONE_POLICY
    )


def test_mobile_precedes_other_and_invalid_alternates_never_appear():
    mobile = "(404) 555-0100"
    landline = "(404) 555-0101"
    record = {
        "phones": [landline, mobile],
        "phone_evidence": [
            _pdl_mobile(mobile),
            {
                "value": landline, "source_field": "phone_numbers",
                "type": "landline", "accepted": False,
            },
        ],
    }
    assert phone_policy.preferred_phone_values(record) == [mobile]

    unsafe = {
        "phones": ["4045550102", "4045550103", "4045550104"],
        "phone_evidence": [
            {"value": "4045550102", "type": "fax", "is_connected": True},
            {"value": "4045550103", "type": "landline", "is_connected": False},
            {"value": "4045550104", "type": "landline", "is_connected": True,
             "is_current": False},
        ],
    }
    assert phone_policy.preferred_phone_values(unsafe) == []

    disconnected_mobile_with_landline = {
        "phones": ["4045550105", "4045550106"],
        "phone_evidence": [
            _enformion_mobile("4045550105", connected=False),
            {"value": "4045550106", "type": "landline", "is_connected": True,
             "is_current": True},
        ],
    }
    assert phone_policy.preferred_phone_details(disconnected_mobile_with_landline) == [{
        "value": "4045550106", "kind": "other", "label": "Other phone",
    }]


def test_latest_phone_uses_provider_recency_inside_the_preferred_tier():
    older_mobile = "(404) 555-0101"
    newer_mobile = "(404) 555-0102"
    newer_landline = "(404) 555-0103"
    record = {
        "phones": [older_mobile, newer_landline, newer_mobile],
        "phone_evidence": [
            {**_pdl_mobile(older_mobile), "is_current": True, "last_seen": "2023-06-01"},
            {
                "value": newer_landline, "type": "landline",
                "source_field": "phone_numbers", "is_current": True,
                "last_seen": "2026-07-01", "accepted": False,
            },
            {**_pdl_mobile(newer_mobile), "is_current": True, "last_seen": "2026-05-01"},
        ],
    }

    selected = phone_policy.latest_phone_detail(record)

    assert selected["value"] == newer_mobile
    assert selected["kind"] == "mobile"
    assert selected["has_reported_recency"] is True


def test_latest_phone_prefers_explicit_current_then_keeps_stable_undated_order():
    first = "(404) 555-0111"
    current = "(404) 555-0112"
    current_record = {
        "phones": [first, current],
        "phone_evidence": [
            _pdl_mobile(first),
            {**_pdl_mobile(current), "is_current": True},
        ],
    }
    assert phone_policy.latest_phone_detail(current_record)["value"] == current

    undated_record = {
        "phones": [first, current],
        "phone_evidence": [_pdl_mobile(first), _pdl_mobile(current)],
    }
    selected = phone_policy.latest_phone_detail(undated_record)
    assert selected["value"] == first
    assert selected["has_reported_recency"] is False


def test_latest_phone_supports_an_already_filtered_public_projection():
    record = {
        "phones": ["(404) 555-0121", "(404) 555-0122"],
        "phone_contacts": [
            {"value": "(404) 555-0121", "kind": "other"},
            {"value": "(404) 555-0122", "kind": "mobile"},
        ],
        "verification": {"record": {"phones": [
            {"value": "(404) 555-0122", "last_reported": "2025-12-01"},
        ]}},
    }
    assert phone_policy.latest_phone_detail(record)["value"] == "(404) 555-0122"


def test_phone_dnc_cannot_be_bypassed_with_different_formatting():
    store.reset()
    store.add_dnc("(404) 555-0100", "opted out")
    assert store.is_dnc("+1 404-555-0100") is True
    assert store.filter_dnc(["404.555.0100", "(404) 555-0101"]) == [
        "(404) 555-0101",
    ]


def test_pdl_phone_only_result_requires_per_number_mobile_evidence():
    unsafe = {
        "status": "success",
        "provider": "people_data_labs",
        "emails": [],
        "phones": ["(404) 555-0199"],
        "verification": _pdl_verification(),
    }
    assert phone_policy.accepted_mobile_phone_values(unsafe) == []
    assert multi_provider._pdl_accepted(unsafe) is False

    mobile = "(404) 555-0105"
    accepted = {
        **unsafe,
        "phones": [mobile],
        "phone_evidence": [_pdl_mobile(mobile)],
        "verification": _pdl_verification(phone_evidence=[_pdl_mobile(mobile)]),
    }
    assert phone_policy.accepted_mobile_phone_values(accepted) == [mobile]
    assert multi_provider._pdl_accepted(accepted) is True


def test_fallback_replaces_unbacked_pdl_phone_with_accepted_enformion_mobile():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    candidate = store.get_candidate(candidate_id)
    unsafe_phone = "(404) 555-0199"
    pdl_result = {
        "status": "success",
        "provider": "people_data_labs",
        "emails": ["jane@example.test"],
        "phones": [unsafe_phone],
        "addresses": ["Atlanta, GA"],
        "verification": _pdl_verification(),
    }
    mobile = "(404) 555-0105"
    enformion_result = {
        "status": "success",
        "source": "enformion",
        "matched_name": "Jane Doe",
        "emails": [],
        "phones": [mobile],
        "addresses": ["Atlanta, GA"],
        "provider_location": "Atlanta, GA",
        "confidence": 0.95,
        "phone_evidence": [_enformion_mobile(mobile)],
    }
    request = multi_provider._request(candidate, "pdl_missing_contact_type")

    merged = multi_provider._apply_fallback(
        candidate, pdl_result, enformion_result, request,
        cached=False,
    )

    assert merged["status"] == "success"
    assert merged["phones"] == [mobile]
    assert unsafe_phone not in merged["phones"]
    assert store.get_candidate(candidate_id)["phones"] == [mobile]
