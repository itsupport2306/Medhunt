"""Offline orchestration tests for Enformion's owner-bound relative pivot."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api as api_module
from sourcing import config, enformion_client as ef, multi_provider, phone_policy, store


def _pdl_no_match() -> dict:
    return {"status": "no_match", "emails": [], "phones": []}


def _relative_discovery(*, household_phone="(404) 555-0199") -> dict:
    """A household response; none of these contacts may reach the candidate."""
    return {
        "status": "relative_pivot", "source": "enformion",
        "matched_name": "Jane Marie Doe",
        "matched_name_type": "provider_relative",
        "relative_tahoe_id": "TH-JANE",
        "phones": [household_phone],
        "emails": ["household@example.test"],
        "addresses": ["Atlanta, GA"],
        "credits_spent": 1,
        "household_contact_used": False,
        "relative_bridge_used": False,
        "selection": {
            "selection": "unique_relative_pivot",
            "relative_tahoe_id": "TH-JANE",
            "matched_name": "Jane Marie Doe",
            "matched_name_type": "provider_relative",
            "household_primary_name": "Robert Smith",
            "household_location_exact": True,
        },
    }


def _target_result() -> dict:
    result = ef._map_tahoe_search({"persons": [{
        "fullName": "Jane Marie Doe", "tahoeId": "TH-JANE",
        "addresses": [{"city": "Atlanta", "state": "GA", "isCurrent": True}],
        "phoneNumbers": [{
            "phoneNumber": "4045550105", "phoneType": "Wireless",
            "isConnected": True,
        }],
    }]}, "TH-JANE", "Jane Doe", "Atlanta, GA")
    result["credits_spent"] = 1
    return result


def _enable_fallback(monkeypatch, *, limit=10):
    monkeypatch.setattr(config, "ENFORMION_VERIFY_PDL", True)
    monkeypatch.setattr(config, "ENFORMION_FALLBACK_ONLY", True)
    monkeypatch.setattr(config, "ENFORMION_RUN_CREDIT_LIMIT", limit)
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)


def test_relative_pivot_resolves_candidate_owner_and_counts_two_calls(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _enable_fallback(monkeypatch)
    calls = []

    def initial(name, location, **_context):
        calls.append(("initial", name, location))
        return _relative_discovery()

    def followup(tahoe_id, name, location, **_context):
        calls.append(("tahoe", tahoe_id, name, location))
        return _target_result()

    monkeypatch.setattr(ef, "enrich", initial)
    monkeypatch.setattr(ef, "resolve_tahoe_id", followup)
    run_id = "relative_owner_12345678"
    checked = multi_provider.verify_batch(
        [candidate_id], {candidate_id: _pdl_no_match()}, run_id,
        allow_enformion=True, max_enformion_calls=2,
    )

    result = checked["results"][candidate_id]
    assert calls == [
        ("initial", "Jane Doe", "Atlanta, GA"),
        ("tahoe", "TH-JANE", "Jane Doe", "Atlanta, GA"),
    ]
    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0105"]
    assert "(404) 555-0199" not in result["phones"]
    assert "household@example.test" not in result["emails"]
    evidence = result["contact_verification"]
    assert evidence["enformion_relative_bridge_used"] is True
    assert evidence["enformion_household_contact_used"] is False
    assert evidence["enformion_relative_resolution"]["owner_bound"] is True
    assert evidence["fallback_credits_spent"] == 2
    assert checked["summary"]["called"] == 2
    assert checked["summary"]["relative_pivots"] == 1
    assert checked["summary"]["relative_pivot_called"] == 1
    assert checked["summary"]["run_credits_spent"] == 2
    assert store.provider_run_credits("enformion", run_id) == 2
    assert store.get_candidate(candidate_id)["phones"] == ["(404) 555-0105"]


def test_relative_pivot_cache_replay_is_free_and_needs_no_new_consent(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _enable_fallback(monkeypatch)
    monkeypatch.setattr(ef, "enrich", lambda *_args, **_kwargs: _relative_discovery())
    monkeypatch.setattr(ef, "resolve_tahoe_id", lambda *_args, **_kwargs: _target_result())

    first = multi_provider.verify_batch(
        [candidate_id], {candidate_id: _pdl_no_match()}, "relative_cache_first_123",
        allow_enformion=True, max_enformion_calls=2,
    )
    assert first["summary"]["called"] == 2
    monkeypatch.setattr(
        ef, "enrich",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fresh initial call")),
    )
    monkeypatch.setattr(
        ef, "resolve_tahoe_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fresh Tahoe call")),
    )
    replay = multi_provider.verify_batch(
        [candidate_id], {candidate_id: _pdl_no_match()}, "relative_cache_replay_123",
        allow_enformion=False, max_enformion_calls=0,
    )

    result = replay["results"][candidate_id]
    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0105"]
    assert result["contact_verification"]["fallback_credits_spent"] == 0
    assert replay["summary"]["called"] == 0
    assert replay["summary"]["relative_pivot_cached"] == 1
    assert replay["summary"]["run_credits_spent"] == 0


def test_relative_pivot_stops_at_budget_and_discards_household_contacts(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _enable_fallback(monkeypatch, limit=1)
    monkeypatch.setattr(ef, "enrich", lambda *_args, **_kwargs: _relative_discovery())
    monkeypatch.setattr(
        ef, "resolve_tahoe_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("budgeted Tahoe call")),
    )
    run_id = "relative_budget_12345678"
    checked = multi_provider.verify_batch(
        [candidate_id], {candidate_id: _pdl_no_match()}, run_id,
        allow_enformion=True, max_enformion_calls=2,
    )

    result = checked["results"][candidate_id]
    assert result["status"] == "no_match"
    assert result["emails"] == [] and result["phones"] == []
    assert result["contact_verification"]["fallback_credits_spent"] == 1
    assert checked["summary"]["called"] == 1
    assert checked["summary"]["relative_pivot_skipped"] == 1
    assert checked["summary"]["skipped_budget"] == 1
    assert store.provider_run_credits("enformion", run_id) == 1
    stored = store.get_candidate(candidate_id)
    assert stored["emails"] == [] and stored["phones"] == []


def test_wrong_tahoe_owner_response_is_sanitized(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _enable_fallback(monkeypatch)
    monkeypatch.setattr(ef, "enrich", lambda *_args, **_kwargs: _relative_discovery())
    monkeypatch.setattr(ef, "resolve_tahoe_id", lambda *_args, **_kwargs: {
        "status": "success", "source": "enformion",
        "matched_name": "Jane Doe", "provider_location": "Atlanta, GA",
        "bridge_target_tahoe_id": "TH-WRONG",
        "relative_bridge_used": True, "household_contact_used": False,
        "phones": ["(404) 555-0199"], "emails": ["wrong@example.test"],
        "addresses": ["Atlanta, GA"], "credits_spent": 1,
        "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
        "phone_evidence": [{
            "value": "(404) 555-0199", "type": "Wireless",
            "is_connected": True, "mobile_or_wireless": True, "accepted": True,
        }],
    })
    checked = multi_provider.verify_batch(
        [candidate_id], {candidate_id: _pdl_no_match()},
        "relative_wrong_owner_123", allow_enformion=True,
        max_enformion_calls=2,
    )

    result = checked["results"][candidate_id]
    assert result["status"] == "no_match"
    assert result["emails"] == [] and result["phones"] == []
    resolution = result["contact_verification"]["enformion_relative_resolution"]
    assert resolution["owner_bound"] is False
    assert resolution["target_tahoe_id"] == "TH-JANE"
    assert store.get_candidate(candidate_id)["phones"] == []


def test_tahoe_identifier_matching_is_exact_not_case_or_whitespace_normalized():
    initial_request = {
        "name": "Jane Doe", "location": "Atlanta, GA",
        "request_key": "initial-key", "aliases": [], "companies": [],
        "schools": [], "roles": [], "relatives": [],
    }
    assert multi_provider._relative_request(initial_request, {
        **_relative_discovery(),
        "relative_tahoe_id": "TH-JANE",
        "selection": {
            **_relative_discovery()["selection"],
            "relative_tahoe_id": "th-jane",
        },
    }) is None

    pivot_request = multi_provider._relative_request(
        initial_request, _relative_discovery(),
    )
    assert pivot_request is not None
    unsafe = multi_provider._safe_relative_result(pivot_request, {
        **_target_result(), "bridge_target_tahoe_id": "th-jane",
    })
    assert unsafe["status"] == "no_match"
    assert unsafe["phones"] == []
    assert unsafe["relative_resolution"]["owner_bound"] is False


def test_public_batch_uses_only_quick_sourcer(monkeypatch):
    captured = []

    def fake_lookup(body, _actor=None):
        captured.append(list(body.candidate_ids))
        return {
            "status": "ok",
            "results": {
                str(candidate_id): {
                    "status": "found", "emails": [], "phones": [],
                    "phone_contacts": [], "resume_required": False,
                    "location_match": None,
                }
                for candidate_id in body.candidate_ids
            },
            "processed": len(body.candidate_ids),
            "matched": len(body.candidate_ids),
        }

    monkeypatch.setattr(api_module, "_quick_sourcer_lookup_batch", fake_lookup)
    response = api_module.contact_lookup_batch(api_module.ContactLookupBatchIn(
        candidate_ids=[101, 102, 103], run_id="quick_only_123", confirmed=True,
    ))

    assert response["matched"] == 3
    assert captured == [[101, 102, 103]]


def test_quick_sourcer_receives_captured_hometown_without_legacy_retry(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", hometown="Savannah, GA",
        source="facebook", source_url="https://facebook.com/jane.doe",
        source_id="jane.doe",
    )
    calls = []

    def fake_lookup(value):
        candidate = store.get_candidate(value)
        calls.append((candidate["location"], candidate["hometown"]))
        return {
            "status": "not_found", "emails": [], "phones": [],
            "phone_contacts": [], "resume_required": False,
            "location_match": None,
        }

    monkeypatch.setattr(api_module.quick_sourcer_client, "lookup_candidate", fake_lookup)
    response = api_module.contact_lookup_batch(api_module.ContactLookupBatchIn(
        candidate_ids=[candidate_id], run_id="hometown_pool_123", confirmed=True,
    ))
    assert response["results"][str(candidate_id)]["status"] == "not_found"
    assert calls == [("Atlanta, GA", "Savannah, GA")]
