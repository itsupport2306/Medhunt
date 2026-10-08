"""Quick Sourcer candidate lookup: the API's own answer, stored and returned.

No external request is made; the client's single HTTP call is stubbed. These
tests pin the behaviour the recruiter asked for: whatever Quick Sourcer
delivers is what the panel shows, with no identity threshold, no likelihood
floor, and no provider-trust gate in front of it. Do-not-contact suppression
still applies.
"""
from __future__ import annotations

import time

import httpx
import pytest

from sourcing import (
    config, contact_access, contact_lookup_queue, quick_sourcer_client, store,
)
import api as api_module


FOUND_PAYLOAD = {
    "found": True,
    "candidate_id": 2315,
    "name": "Joseph Michael Antario",
    "email": None,
    "phone": "(610) 217-3807",
    "address": "396 Little Creek Dr, Nazareth, PA 18064",
    "source": "familytreenow",
    "profile": {
        "age": 67,
        "born": "Jun 1959",
        "currentAddress": {
            "address": "396 Little Creek Dr, Nazareth, PA 18064",
            "county": "Northampton County",
            "dateRange": "Aug 1997 - Jul 2026",
            "propertyDetails": "$616,000 | 4 Bed",
        },
        "relatives": [{"name": "Paula M Antario", "age": 68, "relationship": "Possible Spouse"}],
    },
    "summary": {
        "names": ["Joseph Michael Antario"],
        "emails": ["joseph@example.test", "jo****1@example.test"],
        "phones": [
            {"number": "(610) 217-3807", "type": "Wireless", "carrier": "Verizon Wireless",
             "lastReported": "Jul 2026", "isPrimary": True},
            {"number": "(610) 837-8036", "type": "Landline", "carrier": "Verizon Pennsylvania",
             "lastReported": "Sep 2015", "isPrimary": False},
        ],
        "addresses": ["396 Little Creek Dr, Nazareth, PA 18064", "8 N Delaware Dr, Easton, PA 18042"],
        "education": [],
        "experience": [{"employer": "Everstream Analytics", "title": "Consultant",
                        "industry": "Transportation And Storage", "from": None, "to": None}],
    },
}


@pytest.fixture
def quick_sourcer_enabled(monkeypatch):
    monkeypatch.setattr(config, "QUICK_SOURCER_ENABLED", True)
    monkeypatch.setattr(config, "QUICK_SOURCER_API_KEY", "test-key")
    monkeypatch.setattr(config, "CONTACT_LOOKUP_PROVIDER", "quick_sourcer")


def _stub_api(monkeypatch, payload):
    calls = []

    def fake_call(method, path, body=None):
        calls.append((method, path, body))
        return payload

    monkeypatch.setattr(quick_sourcer_client, "_call", fake_call)
    return calls


def _candidate(name="Joseph Antario", location="Nazareth, Pennsylvania"):
    store.reset()
    return store.add_candidate(name, location, source="indeed", source_id=name.lower())


def test_lookup_stores_and_returns_every_delivered_contact(quick_sourcer_enabled, monkeypatch):
    calls = _stub_api(monkeypatch, FOUND_PAYLOAD)
    candidate_id = _candidate()

    result = quick_sourcer_client.lookup_candidate(candidate_id)

    assert calls[0][0] == "POST" and calls[0][1] == "/find"
    assert calls[0][2] == {"name": "Joseph Antario", "location": "Nazareth, Pennsylvania"}
    assert result["status"] == "found"
    # The usable email is kept; the site's own masked form is not a contact.
    assert result["emails"] == ["joseph@example.test"]
    # A landline is delivered by the API, so it is shown -- no mobile-only rule.
    assert result["phones"] == ["(610) 217-3807", "(610) 837-8036"]
    assert result["phone_contacts"] == [
        {"value": "(610) 217-3807", "kind": "mobile"},
        {"value": "(610) 837-8036", "kind": "other"},
    ]
    assert result["resume_required"] is True

    stored = store.get_candidate(candidate_id)
    assert stored["enrich_status"] == "success"
    assert stored["addresses"] == [
        "396 Little Creek Dr, Nazareth, PA 18064",
        "8 N Delaware Dr, Easton, PA 18042",
    ]
    assert stored["verification"]["source"] == "quick_sourcer"
    assert stored["verification"]["record"]["current_address"]["county"] == "Northampton County"


def test_stored_record_is_projected_to_the_panel_without_a_trust_gate(
    quick_sourcer_enabled, monkeypatch,
):
    _stub_api(monkeypatch, FOUND_PAYLOAD)
    candidate_id = _candidate()
    quick_sourcer_client.lookup_candidate(candidate_id)

    projected = contact_access.project_candidate(store.get_candidate(candidate_id))
    assert projected["emails"] == ["joseph@example.test"]
    assert projected["phones"] == ["(610) 217-3807", "(610) 837-8036"]
    assert projected["contact_source"] == "quick_sourcer"
    # The values are public-record data, so they are never labelled trusted.
    assert projected["contacts_trusted"] is False


def test_administrator_can_approve_quick_sourcer_for_private_sync(
    quick_sourcer_enabled, monkeypatch,
):
    monkeypatch.setattr(config, "QUICK_SOURCER_TRUSTED_FOR_SYNC", True)
    _stub_api(monkeypatch, FOUND_PAYLOAD)
    candidate_id = _candidate()
    quick_sourcer_client.lookup_candidate(candidate_id)

    projected = contact_access.project_candidate(store.get_candidate(candidate_id))

    assert projected["contacts_trusted"] is True
    assert projected["emails"] == ["joseph@example.test"]
    assert projected["phones"][0] == "(610) 217-3807"


def test_api_queues_a_preexisting_resume_after_quick_lookup(
    quick_sourcer_enabled, monkeypatch,
):
    candidate_id = _candidate()
    calls: list[int] = []
    monkeypatch.setattr(
        api_module.quick_sourcer_client,
        "lookup_candidate",
        lambda selected_id: {"status": "found", "candidate_id": selected_id},
    )
    monkeypatch.setattr(
        api_module.nexus_delivery,
        "queue_latest_resume_if_ready",
        lambda selected_id, _user_id="local": calls.append(selected_id),
    )

    result = api_module.enrich_one(candidate_id)

    assert result["status"] == "found"
    assert calls == [candidate_id]


def test_fresh_trusted_saved_contacts_skip_quick_sourcer(
    quick_sourcer_enabled, monkeypatch,
):
    candidate_id = _candidate()
    monkeypatch.setattr(contact_access, "project_candidate", lambda candidate: {
        **candidate,
        "emails": ["joseph@example.test"],
        "phones": ["(610) 217-3807"],
        "phone_contacts": [{"value": "(610) 217-3807", "kind": "mobile"}],
        "addresses": ["Nazareth, PA"],
        "contacts_trusted": True,
    })
    monkeypatch.setattr(
        quick_sourcer_client, "find",
        lambda *args, **kwargs: pytest.fail("saved contacts should skip Quick Sourcer"),
    )

    result = quick_sourcer_client.lookup_candidate(candidate_id)

    assert result == {
        "status": "found", "cached": True, "source": "saved_contact",
        "emails": ["joseph@example.test"], "phones": ["(610) 217-3807"],
        "phone_contacts": [{"value": "(610) 217-3807", "kind": "mobile"}],
        "addresses": ["Nazareth, PA"], "resume_required": True,
        "location_match": None,
    }


def test_refresh_bypasses_saved_contact_shortcut(quick_sourcer_enabled, monkeypatch):
    candidate_id = _candidate()
    monkeypatch.setattr(contact_access, "project_candidate", lambda candidate: {
        **candidate, "emails": ["joseph@example.test"], "phones": [],
        "phone_contacts": [], "addresses": [], "contacts_trusted": True,
    })
    calls = _stub_api(monkeypatch, {"found": False})

    result = quick_sourcer_client.lookup_candidate(candidate_id, refresh=True)

    assert result["status"] == "not_found"
    assert calls and calls[0][1] == "/find"


def test_retry_keeps_fifo_priority_over_new_queue_jobs(
    quick_sourcer_enabled, monkeypatch,
):
    monkeypatch.setattr(config, "CONTACT_LOOKUP_MAX_CONCURRENT", 1)
    first = _candidate("First Nurse", "Austin, Texas")
    second = store.add_candidate(
        "Second Nurse", "Dallas, Texas", source="indeed", source_id="second",
    )
    store.enqueue_contact_lookup_jobs("first-run", [first], "user-one")
    claimed = store.claim_contact_lookup_job()
    store.finish_contact_lookup_job(
        claimed["id"], "retry", {}, retry_at=quick_sourcer_client.time.time() - 1,
    )
    before = store.list_contact_lookup_jobs("first-run", [first], "user-one")
    store.enqueue_contact_lookup_jobs("second-run", [second], "user-two")
    after = store.list_contact_lookup_jobs("first-run", [first], "user-one")

    assert before["items"][0]["position"] == 0
    assert after["items"][0]["position"] == 0
    assert store.claim_contact_lookup_job()["candidate_id"] == first


def test_dispatcher_claims_queue_in_batches_and_workers_finish(
    quick_sourcer_enabled, monkeypatch,
):
    monkeypatch.setattr(config, "CONTACT_LOOKUP_MAX_CONCURRENT", 4)
    store.reset()
    candidate_ids = [
        store.add_candidate(
            f"Nurse {index}", "Austin, Texas", source="indeed",
            source_id=f"dispatcher-{index}",
        )
        for index in range(6)
    ]
    store.enqueue_contact_lookup_jobs(
        "dispatcher-run", candidate_ids, "dispatcher-user",
        per_user_limit=10,
    )
    monkeypatch.setattr(
        contact_lookup_queue.quick_sourcer_client,
        "lookup_candidate",
        lambda candidate_id, candidate=None: {
            "status": "not_found", "emails": [], "phones": [],
            "phone_contacts": [], "resume_required": False,
            "location_match": None,
        },
    )

    contact_lookup_queue.start()
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = store.list_contact_lookup_jobs(
                "dispatcher-run", candidate_ids, "dispatcher-user",
            )
            if status["complete"] == len(candidate_ids):
                break
            time.sleep(0.05)
        assert status["complete"] == len(candidate_ids)
        assert all(item["status"] == "not_found" for item in status["items"])
    finally:
        contact_lookup_queue.stop()


def test_do_not_contact_entries_are_still_suppressed(quick_sourcer_enabled, monkeypatch):
    _stub_api(monkeypatch, FOUND_PAYLOAD)
    candidate_id = _candidate()
    store.add_dnc("joseph@example.test", "opted out")
    store.add_dnc("(610) 217-3807", "opted out")

    result = quick_sourcer_client.lookup_candidate(candidate_id)

    assert result["emails"] == []
    assert result["phones"] == ["(610) 837-8036"]
    assert result["status"] == "found"
    # A phone remains usable even though the provider's email is masked and
    # the only unmasked email was suppressed.
    assert result["resume_required"] is True


def test_email_only_result_still_requests_resume(quick_sourcer_enabled, monkeypatch):
    _stub_api(monkeypatch, FOUND_PAYLOAD)
    candidate_id = _candidate()
    store.add_dnc("(610) 217-3807", "opted out")
    store.add_dnc("(610) 837-8036", "opted out")

    result = quick_sourcer_client.lookup_candidate(candidate_id)

    assert result["emails"] == ["joseph@example.test"]
    assert result["phones"] == []
    assert result["status"] == "found"
    assert result["resume_required"] is True


def test_a_miss_is_reported_as_a_miss(quick_sourcer_enabled, monkeypatch):
    _stub_api(monkeypatch, {"found": False})
    candidate_id = _candidate()

    result = quick_sourcer_client.lookup_candidate(candidate_id)

    assert result == {
        "status": "not_found", "emails": [], "phones": [], "phone_contacts": [],
        "resume_required": False, "location_match": None,
    }
    assert store.get_candidate(candidate_id)["enrich_status"] == "no_match"


def test_a_transport_failure_is_reported_as_failed(quick_sourcer_enabled, monkeypatch):
    def explode(method, path, body=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(quick_sourcer_client, "_call", explode)
    candidate_id = _candidate()

    result = quick_sourcer_client.lookup_candidate(candidate_id)

    assert result["status"] == "failed"
    assert store.get_candidate(candidate_id)["enrich_status"] == "error"


def test_gateway_failure_retries_once_inside_one_timeout_budget(
    quick_sourcer_enabled, monkeypatch,
):
    request = httpx.Request("POST", "https://quick-sourcer.test/find")
    responses = [
        httpx.Response(504, request=request),
        httpx.Response(200, request=request, json={"found": False}),
    ]
    timeouts = []

    def fake_request(*args, **kwargs):
        timeouts.append(float(kwargs["timeout"]))
        return responses.pop(0)

    monkeypatch.setattr(config, "QUICK_SOURCER_BASE_URL", "https://quick-sourcer.test")
    monkeypatch.setattr(config, "QUICK_SOURCER_TIMEOUT", 10.0)
    monkeypatch.setattr(quick_sourcer_client.httpx, "request", fake_request)
    monkeypatch.setattr(quick_sourcer_client.time, "sleep", lambda _seconds: None)

    assert quick_sourcer_client._call("POST", "/find", {"name": "A Nurse"}) == {
        "found": False,
    }
    assert len(timeouts) == 2
    assert 0 < timeouts[1] <= timeouts[0] <= 10.0


def test_batch_isolates_lookup_and_nexus_queue_failures(
    quick_sourcer_enabled, monkeypatch,
):
    first = _candidate("First Nurse", "Austin, Texas")
    second = store.add_candidate(
        "Second Nurse", "Dallas, Texas", source="linkedin", source_id="second-nurse",
    )

    def lookup(candidate_id):
        if candidate_id == first:
            raise ValueError("bad upstream row")
        return {
            "status": "found", "emails": ["second@example.test"], "phones": [],
            "phone_contacts": [], "resume_required": False, "location_match": None,
        }

    monkeypatch.setattr(api_module.quick_sourcer_client, "lookup_candidate", lookup)
    monkeypatch.setattr(
        api_module.nexus_delivery,
        "queue_latest_resume_if_ready",
        lambda _candidate_id, _user_id="local": (_ for _ in ()).throw(RuntimeError("queue offline")),
    )
    monkeypatch.setattr(
        api_module.ats_routing,
        "check_after_enrichment",
        lambda _candidate_id, destination, _user_id="local": {
            "state": "clear", "blocked": False, "target": destination,
        },
    )

    result = api_module._quick_sourcer_lookup_batch(api_module.ContactLookupBatchIn(
        candidate_ids=[first, second], run_id="linkedin-retry-run", confirmed=True,
    ), {"sub": "local", "delivery_targets": {"nexus": True}})

    assert result["results"][str(first)]["status"] == "failed"
    assert result["results"][str(second)]["status"] == "found"
    assert result["processed"] == 2
    assert result["matched"] == 1


def test_provider_found_response_is_not_rejected_by_identity_checks(quick_sourcer_enabled, monkeypatch):
    _stub_api(monkeypatch, {
        "found": True,
        "candidate_id": 2321,
        "name": "Accessibility",
        "source": "searchpeoplefree",
        "profile": {},
        "summary": {"emails": [], "phones": [], "addresses": ["404 - Not Found"]},
    })
    candidate_id = _candidate()

    result = quick_sourcer_client.lookup_candidate(candidate_id)

    assert result["status"] == "found"
    assert store.get_candidate(candidate_id)["addresses"] == []
