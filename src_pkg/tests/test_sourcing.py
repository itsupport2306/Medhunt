"""End-to-end sourcing tests in demo mode (no live Enformion key needed)."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

os.environ["ENFORMION_DEMO"] = "1"
os.environ["SOURCING_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = ""
os.environ["STORAGE_ENABLED"] = "0"
os.environ["PDL_API_KEY"] = ""
os.environ["PDL_ENABLED"] = "0"
os.environ["PDL_TRUST_PROVIDER_MATCH"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import api as api_module
from sourcing import (
    store,
    intake,
    enrich,
    ranking,
    outreach,
    enformion_client as ef,
    verification,
    config,
    storage,
    resume_enrichment,
    pdl_client,
    identity_resolution,
    multi_provider,
    phone_policy,
    trust_policy,
    contact_access,
    person_name,
    healthboard_auth,
    quick_sourcer_client,
    profile_resume,
    ceipal_delivery,
    contact_lookup_queue,
    zoom_sms,
)

# Tests always use isolated SQLite and mocked object storage, regardless of the
# developer's active .env.local cloud configuration.
config.DATABASE_URL = ""
config.STORAGE_ENABLED = False
config.DEMO_MODE = True
config.PDL_API_KEY = ""
config.PDL_ENABLED = False


def test_ceipal_delivery_accepts_email_or_phone_without_resume(monkeypatch):
    delivered = []
    saved = []
    monkeypatch.setattr(store, "get_candidate", lambda cid: {"id": cid, "name": "Jane Smith", "location": "Boston, MA"})
    monkeypatch.setattr(store, "get_candidate_ats_route", lambda cid, owner: {
        "destination": "ceipal", "eligibility": {},
    })
    monkeypatch.setattr(store, "set_candidate_ats_route", lambda *args: saved.append(args))
    monkeypatch.setattr(healthboard_auth, "medhunt_ceipal_candidate", lambda **kwargs: (
        delivered.append(kwargs) or {"state": "uploaded_to_ceipal", "applicant_id": "app-1"}
    ))
    for emails, phones in [(["jane@example.test"], []), ([], ["+16175550100"])]:
        monkeypatch.setattr(contact_access, "project_candidate", lambda _candidate: {
            "contacts_trusted": True, "emails": emails, "phones": phones,
            "phone_contacts": [{"value": value, "kind": "mobile"} for value in phones],
        })
        assert ceipal_delivery.upload_candidate(12, "recruiter-1") == "uploaded"
        assert delivered[-1]["candidate"]["emails"] == emails
        assert delivered[-1]["candidate"]["phones"] == phones
    assert len(saved) == 2


def test_ceipal_duplicate_result_is_idempotent(monkeypatch):
    monkeypatch.setattr(store, "get_candidate", lambda cid: {"id": cid, "name": "Jane Smith"})
    monkeypatch.setattr(store, "get_candidate_ats_route", lambda cid, owner: {
        "destination": "ceipal",
        "eligibility": {"ceipal_upload": {"state": "already_in_ceipal", "applicant_id": "app-1"}},
    })
    def unexpected_submission(**kwargs):
        raise AssertionError("confirmed CEIPAL duplicate should not be submitted again")

    monkeypatch.setattr(healthboard_auth, "medhunt_ceipal_candidate", unexpected_submission)

    assert ceipal_delivery.upload_candidate(12, "recruiter-1") == "already_in_ceipal"


def test_ceipal_upload_waits_for_resume_after_contact_lookup(monkeypatch):
    events = []
    monkeypatch.setattr(store, "contact_lookup_paused", lambda owner: False)
    monkeypatch.setattr(store, "get_candidate", lambda cid: {"id": cid, "name": "Jane Smith"})
    monkeypatch.setattr(quick_sourcer_client, "configured", lambda: True)
    monkeypatch.setattr(quick_sourcer_client, "lookup_candidate", lambda *args, **kwargs: {
        "status": "found", "emails": ["jane@example.test"], "phones": [],
    })
    monkeypatch.setattr(contact_lookup_queue.ats_routing, "set_candidate_target", lambda *args: events.append("route"))
    monkeypatch.setattr(contact_lookup_queue, "_terminal", lambda *args: events.append("terminal") or {"status": "succeeded"})
    result = contact_lookup_queue._process_job({
        "id": 1, "candidate_id": 12, "requested_by": "recruiter-1",
        "delivery_target": "ceipal", "job_source": "extension",
    })
    assert result == {"status": "succeeded"}
    assert events == ["route", "terminal"]


def test_ceipal_upload_runs_after_resume_is_stored(monkeypatch):
    from pypdf import PdfWriter
    from io import BytesIO

    store.reset()
    candidate_id = store.add_candidate(
        "Jane Smith", "Boston, MA", source="indeed",
        source_url="https://employers.indeed.com/profile/jane", source_id="jane-123",
    )
    store.set_candidate_ats_route(candidate_id, "recruiter-1", "ceipal")
    events = []
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    pdf = BytesIO()
    writer.write(pdf)

    monkeypatch.setattr(contact_access, "project_candidate", lambda _candidate: {
        "contacts_trusted": True, "emails": ["jane@example.test"],
        "phones": [], "phone_contacts": [],
    })
    monkeypatch.setattr(api_module.resume_extraction, "extract", lambda *args: {})
    monkeypatch.setattr(api_module.resume_enrichment, "add_contact_sheet", lambda data, _candidate: (data, False))
    attach = store.attach_resume
    def attach_then_record(*args, **kwargs):
        resume = attach(*args, **kwargs)
        events.append("resume_stored")
        return resume
    monkeypatch.setattr(store, "attach_resume", attach_then_record)
    monkeypatch.setattr(healthboard_auth, "medhunt_ceipal_candidate", lambda **kwargs: (
        events.append(("ceipal", kwargs["candidate"]["candidate_id"]))
        or {"state": "uploaded_to_ceipal", "applicant_id": "app-1"}
    ))

    result = api_module._store_resume_pdf(
        candidate_id, "jane.pdf", pdf.getvalue(), "recruiter-1",
    )
    assert events == ["resume_stored"]
    assert result["ceipal_sync_status"] == "queued"
    assert api_module._public_resume(result)["ceipal_sync_status"] == "queued"
    assert store.get_ceipal_delivery(candidate_id, "recruiter-1")["status"] == "pending"
    assert ceipal_delivery.process_once()["status"] == "uploaded"
    assert events == ["resume_stored", ("ceipal", str(candidate_id))]
    assert store.get_ceipal_delivery(candidate_id, "recruiter-1")["status"] == "uploaded"


def test_ceipal_unknown_outcome_is_not_retried_or_requeued(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Jane Smith", "Boston, MA", source="indeed")
    store.set_candidate_ats_route(candidate_id, "recruiter-1", "ceipal")
    resume = store.attach_resume(candidate_id, "jane.pdf", b"%PDF-test")
    monkeypatch.setattr(contact_access, "project_candidate", lambda _candidate: {
        "contacts_trusted": True, "emails": ["jane@example.test"],
        "phones": [], "phone_contacts": [],
    })
    calls = []
    def uncertain_remote(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("unknown remote outcome")
    monkeypatch.setattr(healthboard_auth, "medhunt_ceipal_candidate", uncertain_remote)

    assert ceipal_delivery.queue_resume(candidate_id, resume["id"], "recruiter-1") == "queued"
    assert ceipal_delivery.process_once()["status"] == "indeterminate"
    assert ceipal_delivery.process_once() is None
    assert ceipal_delivery.queue_resume(candidate_id, resume["id"], "recruiter-1") == "indeterminate"
    assert len(calls) == 1
    assert store.get_ceipal_delivery(candidate_id, "recruiter-1")["status"] == "indeterminate"


def test_ceipal_resume_queue_coexists_with_legacy_table(monkeypatch):
    store.reset()
    with store._conn() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS ceipal_deliveries("
            "candidate_id INTEGER PRIMARY KEY,status TEXT,created REAL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO ceipal_deliveries(candidate_id,status,created) "
            "VALUES(999,'failed',0)"
        )
    candidate_id = store.add_candidate("Jane Smith", "Boston, MA", source="indeed")
    store.set_candidate_ats_route(candidate_id, "recruiter-1", "ceipal")
    resume = store.attach_resume(candidate_id, "jane.pdf", b"%PDF-test")
    monkeypatch.setattr(contact_access, "project_candidate", lambda _candidate: {
        "contacts_trusted": True, "emails": ["jane@example.test"],
        "phones": [], "phone_contacts": [],
    })
    assert ceipal_delivery.queue_resume(candidate_id, resume["id"], "recruiter-1") == "queued"
    assert store.get_ceipal_delivery(candidate_id, "recruiter-1")["status"] == "pending"
    with store._conn() as connection:
        legacy = connection.execute(
            "SELECT status FROM ceipal_deliveries WHERE candidate_id=999"
        ).fetchone()
    assert legacy["status"] == "failed"


def test_public_api_requires_healthboard_session_without_origin_header(monkeypatch):
    """Hosted clients cannot bypass sign-in by simply omitting Origin."""
    monkeypatch.setattr(config, "HEALTHBOARD_BASE_URL", "https://board.example.test")

    async def exercise():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/auth/me")).status_code == 401

            monkeypatch.setattr(healthboard_auth, "verify_extension_token", lambda token: {
                "user_id": "42",
                "email": "recruiter@example.test",
                "role": "recruiter",
            })
            response = await client.get(
                "/auth/me", headers={"X-HealthBoard-Extension-Token": "opaque-token"},
            )
            assert response.status_code == 200
            assert response.json()["user"]["user_id"] == "42"

    asyncio.run(exercise())


def _pdl_mobile_contact(value: str) -> dict:
    return {
        "phones": [value],
        "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
        "phone_evidence": [{
            "value": value, "type": "mobile", "source_field": "mobile_phone",
            "mobile_or_wireless": True, "is_connected": None, "accepted": True,
        }],
    }


def _save_current_trusted_pdl(candidate_id: int, *, likelihood: int = 9) -> dict:
    """Persist a PDL result using the same evidence path as a live lookup."""
    candidate = store.get_candidate(candidate_id)
    matched_inputs = ["name", "location"]
    if candidate.get("source") in ("linkedin", "facebook") and candidate.get("source_url"):
        matched_inputs.append("profile")
    saved = enrich.save_provider_result(candidate_id, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        **_pdl_mobile_contact("(404) 555-1212"),
        "addresses": ["Atlanta, Georgia"],
        "provider_location": "Atlanta, Georgia",
        "matched_inputs": matched_inputs,
        "confidence": .9, "likelihood": likelihood,
        "pdl_id": "pdl-jane-cache",
    })
    assert saved["enrich_status"] == "success"
    candidate = store.get_candidate(candidate_id)
    multi_provider._mark_primary_success(candidate, saved)
    return store.get_candidate(candidate_id)


def test_intake_paste_formats():
    rows = intake.parse(
        "Jane Doe, Atlanta, GA\nJohn Smith - Dallas TX\n"
        "Maria Lopez | Chicago, IL\nAda Lovelace – London\nBob"
    )
    assert len(rows) == 5
    assert rows[0]["name"] == "Jane Doe" and "Atlanta" in rows[0]["location"]
    assert rows[3] == {"name": "Ada Lovelace", "location": "London"}
    assert rows[4]["name"] == "Bob" and rows[4]["location"] == ""


def test_intake_csv():
    rows = intake.parse("name,location\nJane Doe,Atlanta GA\nJohn Smith,Dallas TX")
    assert len(rows) == 2 and rows[1]["name"] == "John Smith"


def test_enrichment_demo_returns_contact():
    r = ef.enrich("Jane Doe", "Atlanta, GA")
    assert r["status"] == "success"
    assert r["phones"] and r["emails"]
    # deterministic
    assert ef.enrich("Jane Doe", "Atlanta, GA")["emails"] == r["emails"]


def test_linkedin_credentials_are_not_treated_as_the_person_surname():
    captured = "Silvia Lopez-Clarke, CST, BSN, RN, CNOR"
    assert person_name.normalize_person_name(captured) == "Silvia Lopez-Clarke"
    assert person_name.normalize_person_name(
        "Anna Nicole Gabiosa BSN RN PCCN"
    ) == "Anna Nicole Gabiosa"
    assert person_name.normalize_person_name("John Doe, Jr.") == "John Doe Jr."
    assert verification.name_evidence(captured, "Silvia Lopez-Clarke")["exact"] is True
    assert pdl_client._lookup_name({"name": captured}) == "Silvia Lopez-Clarke"

    request = multi_provider._request({
        "name": captured,
        "location": "Bon Aqua, Tennessee, United States",
    }, "pdl_no_accepted_contact")
    assert request["name"] == "Silvia Lopez-Clarke"
    assert request["location"] == "Bon Aqua, TN"
    assert request["seed_type"] == (
        "enformion-person-search-v11-source-alias-relative"
    )


def test_middle_flexible_pdl_query_and_name_comparison_remain_structured():
    candidate = {"name": "Jane Marie Anne Doe", "location": "Atlanta, GA"}
    assert pdl_client._lookup_name_parameter(candidate) == [
        "Jane Marie Anne Doe", "jane doe",
    ]
    assert verification.name_evidence("Jane Doe", "Jane Marie Anne Doe")["exact"] is True
    assert verification.name_evidence("Jane Lee", "Jane Lee Smith")["exact"] is False

    # Accent folding is a comparison aid, not substring matching.
    assert verification.name_evidence("María García", "Maria Garcia")["exact"] is True
    suffix_conflict = verification.name_evidence("John Smith Jr", "John Smith Sr")
    assert suffix_conflict["exact"] is False
    assert suffix_conflict["suffix_conflict"] is True
    assert verification.name_evidence(
        "Silvia Lopez-Clarke", "Silvia Maria Lopez Clarke",
    )["exact"] is True
    assert verification.name_evidence(
        "Silvia Lopez-Clarke", "Silvia Clarke",
    )["exact"] is False


def test_facebook_aliases_and_nursing_titles_never_enter_lookup_identity(monkeypatch):
    cases = {
        "Rhonda Hampton (Rhonda Hampton)": "Rhonda Hampton",
        "(Rhonda Hampton)": "Rhonda Hampton",
        "PrincessJulana Rubia (Intet)": "PrincessJulana Rubia",
        "(Intet)": "Intet",
        "Kathy Sirianni (Kathy Scott Sirianni)": "Kathy Sirianni",
        "Kathy Shaiken RN": "Kathy Shaiken",
        "MaudelineE Infirmière-Registered Nurse": "MaudelineE",
        "Jane Doe, Registered Nurse": "Jane Doe",
        "Jane Doe Nurse": "Jane Doe",
        # Do not damage an ambiguous but plausible legal surname.
        "Mary Nurse": "Mary Nurse",
    }
    for captured, expected in cases.items():
        assert person_name.normalize_person_name(captured) == expected
        assert pdl_client._lookup_name({"name": captured}) == expected

    parsed = person_name.parse_person_name("Kathy Sirianni (Kathy Scott Sirianni)")
    assert parsed.alternate_names == ("Kathy Scott Sirianni",)
    parsed = person_name.parse_person_name("MaudelineE Infirmière-Registered Nurse")
    assert parsed.descriptors == ("Infirmière-Registered Nurse",)

    row = api_module._profile_row(api_module.ProfileImportIn(
        name="PrincessJulana Rubia (Intet)",
        location="Digos",
        headline="Registered Nurse",
        source="facebook",
        source_url="https://www.facebook.com/princess.rubia",
        source_id="princess.rubia",
    ))
    assert row["name"] == "PrincessJulana Rubia"
    assert "Source alternate name: Intet" in row["notes"]
    assert "Registered Nurse" in row["notes"]

    # Use the exact structured note emitted by the import path. This guards
    # against the lookup consumers drifting to the older shorter prefix.
    alias_row = api_module._profile_row(api_module.ProfileImportIn(
        name="Elizabeth Cruz (Elizabeth Lopez)",
        location="Indian Land, South Carolina",
        headline="Registered Nurse",
        source="facebook",
        source_url="https://www.facebook.com/elizabeth.cruz",
        source_id="elizabeth.cruz",
    ))
    assert person_name.source_alternate_names(alias_row["notes"]) == (
        "Elizabeth Lopez",
    )
    assert [
        item["value"] for item in pdl_client._source_name_alias_evidence(alias_row)
    ] == ["Elizabeth Lopez"]
    assert pdl_client._lookup_name_parameter(alias_row) == [
        "Elizabeth Cruz", "Elizabeth Lopez",
    ]
    fallback_request = multi_provider._request(
        alias_row, "pdl_no_accepted_contact",
    )
    assert fallback_request["aliases"] == ["Elizabeth Lopez"]
    source_alias_assessment = verification.assess(alias_row, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Elizabeth Lopez",
        "provider_location": "Indian Land, South Carolina",
        "addresses": ["Indian Land, South Carolina"],
        "emails": ["elizabeth@example.test"], "phones": [],
        "confidence": .9,
    })
    assert source_alias_assessment["evidence"]["name"]["source_alias_match"] is True

    # A branded/single-token Facebook identity is stored for review but must
    # never purchase PDL discovery or pass through to another lookup provider.
    monkeypatch.setattr(
        pdl_client, "_client",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )
    discovery = pdl_client._discover_identity({
        "name": "MaudelineE Infirmière-Registered Nurse",
        "location": "Montreal, QC",
        "source": "facebook",
    })
    assert discovery["status"] == "skipped"
    assert discovery["credits_spent"] == 0

    resolved = identity_resolution.resolve({
        "id": 999999,
        "name": "MaudelineE Infirmière-Registered Nurse",
        "location": "Montreal, QC",
        "identity_status": "captured",
    }, discover=lambda _candidate: (_ for _ in ()).throw(
        AssertionError("discovery must not run")
    ))
    assert resolved["status"] == "no_match"
    assert resolved["credits_spent"] == 0


def test_pdl_lookup_evidence_labels_the_actual_social_platform():
    evidence = pdl_client._lookup_evidence({
        "name": "Silvia Lopez-Clarke, CST, BSN, RN, CNOR",
        "location": "Bon Aqua, Tennessee, United States",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/in/silvia-lopez-clarke-123/",
    })
    assert evidence["profile_supplied"] is True
    assert evidence["profile_platform"] == "linkedin"


def test_enformion_mapper_handles_nested_name_and_contact_evidence():
    result = ef._map({
        "person": {
            "name": {"firstName": "Jane", "middleName": "Q", "lastName": "Doe"},
            "identityScore": 91,
            "phones": [
                {
                    "phoneNumber": "4045550123", "type": "Wireless",
                    "isConnected": True, "isCurrent": True,
                    "lastReportedDate": "07/2026",
                },
                {
                    "phoneNumber": "4045550198", "type": "Landline",
                    "isConnected": True,
                },
                {
                    "phoneNumber": "4045550197", "type": "Mobile",
                    "isConnected": False,
                },
            ],
            "emails": [{"emailAddress": "jane@example.com", "isCurrent": True}],
            "addresses": [{
                "street": "123 Main St", "city": "Atlanta", "state": "GA",
                "zip": "30303", "isCurrent": True,
            }],
        }
    }, "Jane Doe", request_fields=["name", "location"])
    assert result["matched_name"] == "Jane Q Doe"
    assert result["phones"] == ["(404) 555-0123"]
    assert result["emails"] == ["jane@example.com"]
    assert "Atlanta, GA 30303" in result["addresses"][0]
    assert result["confidence"] == 0.91
    assert result["phone_evidence"][0]["type"] == "Wireless"
    assert result["phone_evidence"][0]["accepted"] is True
    assert result["phone_evidence"][1]["rejection_reason"] == "not_explicitly_mobile_or_wireless"
    assert result["phone_evidence"][2]["rejection_reason"] == "not_confirmed_connected"


def test_enformion_person_search_request_uses_reference_shape(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, json, headers):
            captured.update({"url": url, "json": json, "headers": headers})
            return httpx.Response(
                200,
                json={"persons": [{
                    "fullName": "Jane Doe",
                    "addresses": [{"city": "Atlanta", "state": "GA"}],
                    "phoneNumbers": [{
                        "phoneNumber": "4045550123", "isConnected": True,
                        "phoneOrder": 1, "phoneType": "Wireless",
                    }],
                    "emailAddresses": [{
                        "emailAddress": "jane@example.com", "emailOrdinal": 1,
                    }],
                }]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(config, "DEMO_MODE", False)
    monkeypatch.setattr(ef.httpx, "Client", FakeClient)
    result = ef.enrich("Jane Doe", "Atlanta, GA")
    assert result["status"] == "success"
    assert captured["json"] == {
        "FirstName": "Jane", "MiddleName": "", "LastName": "Doe",
        "Page": 1, "ResultsPerPage": 10,
        "Addresses": [{"AddressLine1": "", "AddressLine2": "Atlanta, GA"}],
    }
    assert captured["headers"]["galaxy-search-type"] == "Person"
    assert result["phone_evidence"][0]["is_connected"] is True
    assert result["selection"]["selection"] == "unique_exact_name_location"
    assert result["credits_spent"] == 1


def test_enformion_sanitizes_linkedin_credentials_and_full_us_location(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, json, headers):
            captured.update({"json": json, "headers": headers})
            return httpx.Response(
                200,
                json={"persons": [{
                    "fullName": "Silvia Lopez-Clarke",
                    "addresses": [{"city": "Bon Aqua", "state": "TN"}],
                    "phoneNumbers": [{
                        "phoneNumber": "6155550123", "isConnected": True,
                        "phoneType": "Wireless",
                    }],
                }]},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(config, "DEMO_MODE", False)
    monkeypatch.setattr(ef.httpx, "Client", FakeClient)
    result = ef.enrich(
        "Silvia Lopez-Clarke, CST, BSN, RN, CNOR",
        "Bon Aqua, Tennessee, United States",
    )

    assert captured["json"] == {
        "FirstName": "Silvia", "MiddleName": "", "LastName": "Lopez-Clarke",
        "Page": 1, "ResultsPerPage": 10,
        "Addresses": [{"AddressLine1": "", "AddressLine2": "Bon Aqua, TN"}],
    }
    assert result["status"] == "success"
    assert result["matched_name"] == "Silvia Lopez-Clarke"


def test_enformion_person_search_rejects_ambiguous_exact_people():
    person = {
        "fullName": "Jane Doe",
        "addresses": [{"city": "Atlanta", "state": "GA"}],
        "phoneNumbers": [{
            "phoneNumber": "4045550123", "isConnected": True, "phoneType": "Wireless",
        }],
    }
    result = ef._map_search(
        {"persons": [person, {**person, "phoneNumbers": [{"phoneNumber": "7705550123"}]}]},
        "Jane Doe", "Atlanta, GA", request_fields=["name", "location"],
    )
    assert result["status"] == "no_match"
    assert result["selection"]["selection"] == "ambiguous_exact_matches"
    assert result["phones"] == []


def test_enformion_person_search_does_not_choose_contact_rich_wrong_city():
    result = ef._map_search({"persons": [
        {
            "fullName": "Jane Doe",
            "addresses": [{"city": "Savannah", "state": "GA"}],
            "phoneNumbers": [{
                "phoneNumber": "9125550123", "isConnected": True, "phoneType": "Wireless",
            }],
            "emailAddresses": [{"emailAddress": "wrong@example.com"}],
        },
        {
            "fullName": "Jane Doe",
            "addresses": [{"city": "Atlanta", "state": "GA"}],
            "phoneNumbers": [{
                "phoneNumber": "4045550199", "isConnected": True, "phoneType": "Mobile",
            }],
        },
    ]}, "Jane Doe", "Atlanta, GA", request_fields=["name", "location"])
    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0199"]
    assert result["emails"] == []


def test_enformion_person_search_accepts_semicolon_delimited_full_address():
    result = ef._map_search({"persons": [{
        "fullName": "Virginia Fagan",
        "addresses": [{
            "fullAddress": "4521 Bonnywood Dr; Mesquite, TX 75150-8280",
        }],
        "phoneNumbers": [{
            "phoneNumber": "9725550199", "isConnected": True,
            "phoneType": "Wireless",
        }],
    }]}, "Virginia Fagan", "Mesquite, TX", request_fields=["name", "location"])

    assert result["status"] == "success"
    assert result["selection"]["selection"] == "unique_exact_name_location"
    assert result["phones"] == ["(972) 555-0199"]
    assert result["provider_location"] == (
        "4521 Bonnywood Dr; Mesquite, TX 75150-8280"
    )


def test_enformion_uses_connected_callable_fallbacks_not_disconnected_mobile():
    result = ef._map({"person": {
        "fullName": "Jane Doe",
        "phoneNumbers": [
            {"phoneNumber": "4045550101", "phoneType": "Landline", "isConnected": True},
            {"phoneNumber": "4045550102", "phoneType": "VoIP", "isConnected": True},
            {"phoneNumber": "4045550103", "isConnected": True},
            {"phoneNumber": "4045550104", "phoneType": "Cellular", "isConnected": False},
        ],
    }}, "Jane Doe")
    assert result["phones"] == [
        "(404) 555-0101", "(404) 555-0102", "(404) 555-0103",
    ]
    assert result["status"] == "success"
    assert result["phone_policy"] == phone_policy.OTHER_PHONE_POLICY
    assert len(result["phone_evidence"]) == 4
    assert all(item["accepted"] is False for item in result["phone_evidence"])


def test_pdl_review_routes_to_independent_enformion_fallback():
    assert multi_provider._fallback_reason({
        "status": "no_match",
        "verification": {
            "identity_status": "review",
            "evidence": {"pdl_quality_gate": {"accepted": False}},
        },
    }) == "pdl_identity_review"
    assert multi_provider._fallback_reason({
        "status": "no_match",
        "verification": {
            "identity_status": "rejected",
            "evidence": {"pdl_quality_gate": {"accepted": False}},
        },
    }) == "pdl_identity_conflict"


def test_enformion_fallback_location_gate_is_us_only():
    assert multi_provider._specific_location("Boston, MA") is True
    assert multi_provider._specific_location("Paris, TX") is True
    assert multi_provider._specific_location("Philadelphia, Pennsylvania, United States") is True
    assert multi_provider._specific_location("02108") is True
    assert multi_provider._specific_location("Paris, France") is False
    assert multi_provider._specific_location("Paris, France 75001") is False
    assert multi_provider._specific_location("London, United Kingdom") is False


def test_non_us_pdl_no_match_explains_enformion_skip(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate(
        "Mehakpreet Kaur", "Sydney, NSW", source="facebook",
        source_url="https://www.facebook.com/example.person",
        source_id="example.person",
    )
    monkeypatch.setattr(config, "ENFORMION_VERIFY_PDL", True)
    monkeypatch.setattr(config, "ENFORMION_FALLBACK_ONLY", True)
    checked = multi_provider.verify_batch(
        [candidate_id],
        {candidate_id: pdl_client._no_match_result(
            "PDL completed the lookup but returned no record meeting required=mobile_phone.",
            store.get_candidate(candidate_id),
        )},
        "non_us_fallback_12345678",
        allow_enformion=True,
        max_enformion_calls=1,
    )

    result = checked["results"][candidate_id]
    assert result["contact_verification"]["status"] == "fallback_ineligible"
    assert result["contact_verification"]["fallback_called"] is False
    assert "exact US city/state" in result["message"]
    assert checked["summary"]["called"] == 0
    assert checked["summary"]["skipped_missing_identity"] == 1


def test_multi_provider_uses_enformion_only_as_fallback_without_live_calls(monkeypatch):
    store.reset()
    primary_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    fallback_id = store.add_candidate("John Smith", "Dallas, TX", source="indeed")
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)
    monkeypatch.setattr(multi_provider, "_cached", lambda _key, **_kwargs: None)
    calls = []

    def fake_enformion(name, location, *, phone="", email=""):
        calls.append((name, location, phone, email))
        return {
            "status": "success", "source": "enformion", "matched_name": name,
            "emails": ["john@example.com"], "phones": ["(972) 555-0199"],
            "addresses": ["500 Elm St, Dallas, TX 75201"],
            "provider_location": "Dallas, TX", "confidence": 0.9,
            "request_fields": ["name", "location"],
            "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
            "phone_evidence": [{
                "value": "(972) 555-0199", "type": "Wireless",
                "is_connected": True, "mobile_or_wireless": True, "accepted": True,
            }],
        }

    monkeypatch.setattr(ef, "enrich", fake_enformion)
    pdl_results = {
        primary_id: {
            "status": "success", "provider": "people_data_labs",
            "emails": ["jane@example.com"],
            **_pdl_mobile_contact("404-555-0123"),
            "verification": {"identity_status": "verified", "evidence": {
                "required_field": "mobile_phone",
                "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
                "pdl_phone_evidence": _pdl_mobile_contact(
                    "404-555-0123"
                )["phone_evidence"],
                "pdl_quality_gate": {"accepted": True, "rule": "exact_name_location_and_high_likelihood"},
            }},
        },
        fallback_id: {
            "status": "no_match", "provider": "people_data_labs",
            "emails": [], "phones": [],
            "verification": {"identity_status": "verified", "evidence": {
                "required_field": "mobile_phone",
                "pdl_quality_gate": {"accepted": False, "rule": "below_automatic_acceptance_threshold"},
            }},
        },
    }
    verified = multi_provider.verify_batch(
        [primary_id, fallback_id], pdl_results, "enformion_test_run",
        allow_enformion=True, max_enformion_calls=2,
    )
    assert verified["results"][primary_id]["contact_verification"]["status"] == "pdl_high_confidence"
    assert verified["results"][fallback_id]["contact_verification"]["status"] == "enformion_fallback"
    assert verified["results"][primary_id]["contacts_trusted"] is True
    assert verified["results"][fallback_id]["contacts_trusted"] is True
    assert calls == [("John Smith", "Dallas, TX", "", "")]
    assert verified["summary"]["called"] == 1
    assert verified["summary"]["skipped_cost"] == 1
    assert verified["summary"]["matches"] == 1
    stored = store.get_candidate(fallback_id)
    assert stored["verification"]["contact_verification_status"] == "enformion_fallback"
    assert stored["emails"] == ["john@example.com"]
    assert stored["phones"] == ["(972) 555-0199"]
    assert stored["verification"]["evidence"]["phone_policy"] == phone_policy.MOBILE_PHONE_POLICY


def test_enformion_access_error_is_exposed_as_provider_failure(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)
    monkeypatch.setattr(multi_provider, "_cached", lambda _key, **_kwargs: None)
    monkeypatch.setattr(ef, "enrich", lambda name, location: {
        "status": "error", "source": "enformion",
        "error": "Enformion HTTP 400: Invalid Input: Access denied.",
        "provider_error_code": "Invalid Input",
        "emails": [], "phones": [], "addresses": [],
    })
    checked = multi_provider.verify_batch(candidate_id and [candidate_id], {
        candidate_id: {
            "status": "no_match", "provider": "people_data_labs",
            "emails": [], "phones": [],
        },
    }, "enformion_access_test", allow_enformion=True, max_enformion_calls=1)
    result = checked["results"][candidate_id]
    assert result["status"] == "no_match"
    assert "Access denied" in result["message"]
    assert result["contact_verification"]["enformion_error_code"] == "Invalid Input"


def test_enformion_fallback_defaults_to_no_fresh_paid_call(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)
    monkeypatch.setattr(multi_provider, "_cached", lambda _key, **_kwargs: None)
    calls = []
    monkeypatch.setattr(ef, "enrich", lambda name, location: calls.append((name, location)))

    checked = multi_provider.verify_batch([candidate_id], {
        candidate_id: {"status": "no_match", "emails": [], "phones": []},
    }, "no_consent_12345678")

    assert calls == []
    assert checked["summary"]["eligible"] == 1
    assert checked["summary"]["called"] == 0
    assert checked["summary"]["skipped_consent"] == 1
    assert checked["results"][candidate_id]["contact_verification"]["status"] == "fallback_not_authorized"


def test_enformion_dedupes_request_key_and_keeps_linkedin_review_gate(monkeypatch):
    store.reset()
    ids = [
        store.add_candidate(
            "Alex Morgan", "Atlanta, GA", source="linkedin",
            source_url=f"https://www.linkedin.com/in/alex-morgan-{suffix}/",
        )
        for suffix in ("one", "two")
    ]
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)
    monkeypatch.setattr(multi_provider, "_cached", lambda _key, **_kwargs: None)
    monkeypatch.setattr(config, "ENFORMION_RUN_CREDIT_LIMIT", 10)
    calls = []

    def fake_enformion(name, location):
        calls.append((name, location))
        return {
            "status": "success", "source": "enformion", "matched_name": name,
            "emails": ["alex@example.test"], "phones": ["(404) 555-0199"],
            "addresses": ["Atlanta, GA"], "provider_location": "Atlanta, GA",
            "confidence": 0.95, "credits_spent": 1,
            "phone_evidence": [{
                "value": "(404) 555-0199", "type": "Wireless",
                "is_connected": True, "mobile_or_wireless": True,
                "accepted": True,
            }],
        }

    monkeypatch.setattr(ef, "enrich", fake_enformion)
    pdl_results = {
        candidate_id: {"status": "no_match", "emails": [], "phones": []}
        for candidate_id in ids
    }
    checked = multi_provider.verify_batch(
        ids, pdl_results, "dedupe_enformion_12345678",
        allow_enformion=True, max_enformion_calls=10,
    )

    assert calls == [("Alex Morgan", "Atlanta, GA")]
    assert checked["summary"]["eligible"] == 2
    assert checked["summary"]["called"] == 1
    assert checked["summary"]["skipped_budget"] == 0
    assert all(
        checked["results"][candidate_id]["contact_verification"]["status"]
        == "enformion_identity_review"
        for candidate_id in ids
    )


def test_enformion_server_and_request_caps_use_lower_limit(monkeypatch):
    store.reset()
    ids = [
        store.add_candidate(name, location, source="indeed")
        for name, location in (("Jane Doe", "Atlanta, GA"), ("John Smith", "Dallas, TX"))
    ]
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)
    monkeypatch.setattr(multi_provider, "_cached", lambda _key, **_kwargs: None)
    monkeypatch.setattr(config, "ENFORMION_RUN_CREDIT_LIMIT", 2)
    calls = []
    monkeypatch.setattr(ef, "enrich", lambda name, location: (
        calls.append((name, location)) or {
            "status": "no_match", "emails": [], "phones": [],
            "addresses": [], "credits_spent": 1,
        }
    ))
    checked = multi_provider.verify_batch(
        ids,
        {candidate_id: {"status": "no_match", "emails": [], "phones": []} for candidate_id in ids},
        "lower_cap_12345678", allow_enformion=True, max_enformion_calls=1,
    )

    assert len(calls) == 1
    assert checked["summary"]["effective_limit"] == 1
    assert checked["summary"]["called"] == 1
    assert checked["summary"]["skipped_budget"] == 1


def test_enformion_cached_fallback_is_free_without_fresh_call_consent(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    candidate = store.get_candidate(candidate_id)
    request = multi_provider._request(candidate, "pdl_no_accepted_contact")
    cached_result = {
        "status": "success", "source": "enformion", "matched_name": "Jane Doe",
        "emails": ["jane@example.test"], "phones": ["(404) 555-0199"],
        "addresses": ["Atlanta, GA"], "provider_location": "Atlanta, GA",
        "confidence": 0.95, "credits_spent": 1,
        "phone_evidence": [{
            "value": "(404) 555-0199", "type": "Wireless",
            "is_connected": True, "mobile_or_wireless": True, "accepted": True,
        }],
    }
    store.save_provider_lookup(
        "enformion", "old_enformion_run", candidate_id,
        request["request_key"], "success", cached_result, credits_spent=1,
    )
    monkeypatch.setattr(config, "ENFORMION_VERIFY_PDL", True)
    monkeypatch.setattr(config, "ENFORMION_FALLBACK_ONLY", True)
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)
    monkeypatch.setattr(ef, "enrich", lambda *_args, **_kwargs: pytest.fail("fresh provider call"))

    checked = multi_provider.verify_batch([candidate_id], {
        candidate_id: {"status": "no_match", "emails": [], "phones": []},
    }, "cache_reuse_12345678")

    assert checked["summary"]["called"] == 0
    assert checked["summary"]["cached"] == 1
    assert checked["summary"]["skipped_consent"] == 0
    assert checked["results"][candidate_id]["contact_verification"]["status"] == "enformion_fallback"
    assert checked["results"][candidate_id]["contact_verification"]["fallback_credits_spent"] == 0


def test_cached_semicolon_address_fallback_is_reverified_without_provider_call(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate(
        "Virginia Fagan", "Mesquite, TX", source="facebook",
        source_url="https://www.facebook.com/profile.php?id=100012086986917",
        source_id="id:100012086986917",
    )
    candidate = store.get_candidate(candidate_id)
    request = multi_provider._request(candidate, "pdl_below_quality_gate")
    cached_result = {
        "status": "success", "source": "enformion",
        "matched_name": "Virginia Ruth Fagan",
        "emails": ["virginia@example.test"],
        "phones": ["(214) 537-1353"],
        "addresses": ["4521 Bonnywood Dr; Mesquite, TX 75150-8280"],
        "provider_location": "4521 Bonnywood Dr; Mesquite, TX 75150-8280",
        "confidence": 0.8, "credits_spent": 1,
        "selection": {
            "selection": "unique_exact_name_location", "exact_candidates": 1,
        },
        "phone_evidence": [{
            "value": "(214) 537-1353", "type": "Wireless",
            "is_connected": True, "mobile_or_wireless": True, "accepted": True,
        }],
    }
    store.save_provider_lookup(
        "enformion", "old_virginia_run", candidate_id,
        request["request_key"], "success", cached_result, credits_spent=1,
    )
    monkeypatch.setattr(config, "ENFORMION_VERIFY_PDL", True)
    monkeypatch.setattr(config, "ENFORMION_FALLBACK_ONLY", True)
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)
    monkeypatch.setattr(ef, "enrich", lambda *_args, **_kwargs: pytest.fail("fresh provider call"))
    pdl_result = {
        "status": "no_match", "emails": [], "phones": [],
        "verification": {"identity_status": "verified", "evidence": {
            "pdl_quality_gate": {"accepted": False},
        }},
    }

    checked = multi_provider.verify_batch(
        [candidate_id], {candidate_id: pdl_result}, "virginia_cache_12345678",
    )
    result = checked["results"][candidate_id]

    assert checked["summary"]["called"] == 0
    assert checked["summary"]["cached"] == 1
    assert result["status"] == "success"
    assert result["contacts_trusted"] is True
    assert result["phones"] == ["(214) 537-1353"]
    assert result["contact_verification"]["status"] == "enformion_fallback"
    assert result["contact_verification"]["enformion_location_exact"] is True
    assert result["contact_verification"]["fallback_credits_spent"] == 0


def test_enformion_cap_is_shared_across_same_run_invocations(monkeypatch):
    store.reset()
    ids = [
        store.add_candidate(name, location, source="indeed")
        for name, location in (("Jane Doe", "Atlanta, GA"), ("John Smith", "Dallas, TX"))
    ]
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)
    monkeypatch.setattr(multi_provider, "_cached", lambda _key, **_kwargs: None)
    monkeypatch.setattr(config, "ENFORMION_RUN_CREDIT_LIMIT", 1)
    calls = []
    monkeypatch.setattr(ef, "enrich", lambda name, location: (
        calls.append((name, location)) or {
            "status": "no_match", "emails": [], "phones": [],
            "addresses": [], "credits_spent": 0,
        }
    ))
    run_id = "shared_cap_12345678"
    first = multi_provider.verify_batch(
        [ids[0]], {ids[0]: {"status": "no_match", "emails": [], "phones": []}},
        run_id, allow_enformion=True, max_enformion_calls=1,
    )
    second = multi_provider.verify_batch(
        [ids[1]], {ids[1]: {"status": "no_match", "emails": [], "phones": []}},
        run_id, allow_enformion=True, max_enformion_calls=1,
    )

    assert len(calls) == 1
    assert first["summary"]["called"] == 1
    assert second["summary"]["called"] == 0
    assert second["summary"]["skipped_budget"] == 1


def test_full_pipeline():
    store.reset()
    job = store.create_job("Radiologic Technologist", "Atlanta, GA", "radiologic technologist imaging xray CT")
    ids = store.add_candidates_bulk(
        [{"name": "Jane Doe", "location": "Atlanta, GA"},
         {"name": "John Smith", "location": "Dallas, TX"}], job_id=job)
    assert len(ids) == 2
    res = enrich.enrich_batch(job_id=job)
    assert res["matched"] == 2
    ranking.rank_job(job)
    cands = store.list_candidates(job_id=job)
    assert all(c["enrich_status"] == "success" for c in cands)
    assert all(c["emails"] for c in cands)
    assert all(c["verification"]["identity_status"] == "verified" for c in cands)
    assert all(c["identity_status"] == "verified" for c in cands)
    resume = store.attach_resume(cands[0]["id"], "candidate-resume.pdf", b"%PDF-1.4 fixture")
    assert store.list_resumes(cands[0]["id"])[0]["id"] == resume["id"]
    assert store.get_resume(cands[0]["id"], resume["id"])["data"].startswith(b"%PDF")
    # outreach draft respects compliance + advances stage
    d = outreach.draft_for_candidate(cands[0]["id"], job=store.get_job(job))
    assert d["status"] == "draft" and d["body"]
    assert store.get_candidate(cands[0]["id"])["stage"] == "contacted"


def test_batch_action_apis_create_pool_campaign_and_reviewable_email_drafts():
    store.reset()
    first = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    second = store.add_candidate("No Email Candidate", "Dallas, TX", source="indeed")
    _save_current_trusted_pdl(first)

    async def exercise_batch_actions():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assigned = await client.post(
                "/pools/assign",
                json={"name": "Nursing Pool", "candidate_ids": [first, second]},
            )
            assert assigned.status_code == 200
            assert assigned.json()["added"] == 2
            duplicate = await client.post(
                "/pools/assign",
                json={"name": "Nursing Pool", "candidate_ids": [first]},
            )
            assert duplicate.json()["added"] == 0
            assert duplicate.json()["already_present"] == 1
            pools = await client.get("/pools")
            assert pools.json()[0]["candidate_count"] == 2

            campaign = await client.post(
                "/campaigns",
                json={"name": "August RN Outreach", "candidate_ids": [first, second]},
            )
            assert campaign.status_code == 200
            assert campaign.json()["status"] == "draft"
            assert campaign.json()["candidate_count"] == 2

            drafts = await client.post(
                "/outreach/draft/batch",
                json={"candidate_ids": [first, second], "channel": "email"},
            )
            assert drafts.status_code == 200
            assert drafts.json()["created"] == 1
            assert drafts.json()["failed"] == 1
            assert drafts.json()["drafts"][0]["to"] == ["jane@example.test"]

    asyncio.run(exercise_batch_actions())


def test_direct_enformion_persists_current_trusted_contacts(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    phone_evidence = [{
        "value": "(404) 555-0199", "type": "Wireless",
        "is_connected": True, "mobile_or_wireless": True, "accepted": True,
    }]
    email_evidence = [{
        "value": "jane@example.com", "is_current": True, "accepted": True,
    }]
    monkeypatch.setattr(ef, "enrich", lambda name, location: {
        "status": "success", "source": "enformion", "matched_name": name,
        "emails": ["jane@example.com"], "phones": ["(404) 555-0199"],
        "addresses": ["123 Main St, Atlanta, GA 30303"],
        "provider_location": "Atlanta, GA", "confidence": .94,
        "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
        "phone_evidence": phone_evidence, "email_evidence": email_evidence,
    })

    result = enrich.enrich_candidate(candidate_id)
    assert result["enrich_status"] == "success"
    stored = store.get_candidate(candidate_id)
    verification_record = stored["verification"]
    evidence = verification_record["evidence"]
    contact_evidence = evidence["contact_verification"]
    assert contact_evidence["status"] == "enformion_direct"
    assert contact_evidence["automatic_use_allowed"] is True
    assert contact_evidence["enformion_identity_status"] == "verified"
    assert contact_evidence["enformion_identity_confidence"] >= (
        config.IDENTITY_MATCH_THRESHOLD
    )
    assert contact_evidence["enformion_name_exact"] is True
    assert contact_evidence["enformion_location_exact"] is True
    assert contact_evidence["enformion_conflicts"] == []
    assert contact_evidence["enformion_phone_evidence"] == phone_evidence
    assert contact_evidence["enformion_email_evidence"] == email_evidence
    assert evidence["contact_trust_policy"] == trust_policy.CONTACT_TRUST_POLICY
    assert evidence["source_identity_fingerprint"] == (
        trust_policy.source_identity_fingerprint(stored)
    )
    assert verification_record["provider_contacts"] == {
        "emails": ["jane@example.com"],
        "phones": ["(404) 555-0199"],
        "addresses": ["123 Main St, Atlanta, GA 30303"],
    }
    trusted_contacts = trust_policy.trusted_provider_contacts(stored, stored)
    assert {
        key: trusted_contacts[key] for key in ("emails", "phones", "addresses")
    } == verification_record["provider_contacts"]
    assert trusted_contacts["phone_contacts"] == [{
        "value": "(404) 555-0199", "kind": "mobile", "label": "Mobile",
    }]
    assert pdl_client._already_complete(stored)["trusted_cache"] is True


def test_do_not_contact_suppresses():
    store.reset()
    cid = store.add_candidate("Blocked Person", "Atlanta, GA")
    r = ef.enrich("Blocked Person", "Atlanta, GA")
    store.add_dnc(r["emails"][0], "opted out")
    out = enrich.enrich_candidate(cid)
    assert r["emails"][0] not in out["emails"]  # suppressed
    # outreach refuses when no usable contact remains
    store.update_candidate(cid, emails=[], phones=[])
    d = outreach.draft_for_candidate(cid)
    assert "error" in d


def test_other_phone_is_callable_but_never_used_as_an_sms_mobile(monkeypatch):
    store.reset()
    cid = store.add_candidate("Jane Doe", "Atlanta, GA")
    monkeypatch.setattr(contact_access, "project_candidate", lambda candidate: {
        **candidate,
        "emails": [],
        "phones": ["(404) 555-0109"],
        "phone_contacts": [{
            "value": "(404) 555-0109", "kind": "other", "label": "Other phone",
        }],
        "contacts_trusted": True,
    })
    assert "error" in outreach.draft_for_candidate(cid, channel="sms")
    call_draft = outreach.draft_for_candidate(cid, channel="phone")
    assert call_draft["to"] == ["(404) 555-0109"]


def test_outward_contact_projection_rejects_legacy_and_expired_contacts():
    store.reset()
    legacy_id = store.add_candidate("Legacy Person", "Atlanta, GA", source="indeed")
    store.update_candidate(
        legacy_id, emails=["legacy@example.test"], phones=["(404) 555-0100"],
        enrich_status="success", contact_expires_at=time.time() + 3600,
    )
    legacy = contact_access.project_candidate(store.get_candidate(legacy_id))
    assert legacy["contacts_trusted"] is False
    assert legacy["emails"] == []
    assert legacy["phones"] == []
    assert "error" in outreach.draft_for_candidate(legacy_id)

    expired_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _save_current_trusted_pdl(expired_id)
    store.update_candidate(expired_id, contact_expires_at=time.time() - 1)
    expired = contact_access.project_candidate(store.get_candidate(expired_id))
    assert expired["contacts_trusted"] is False
    assert expired["emails"] == []
    assert expired["phones"] == []
    assert "error" in outreach.draft_for_candidate(expired_id)


def test_outward_contact_projection_and_outreach_accept_current_trusted_pdl():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _save_current_trusted_pdl(candidate_id)

    projected = contact_access.project_candidate(store.get_candidate(candidate_id))
    assert projected["contacts_trusted"] is True
    assert projected["emails"] == ["jane@example.test"]
    assert projected["phones"] == ["(404) 555-1212"]

    draft = outreach.draft_for_candidate(candidate_id)
    assert draft["status"] == "draft"
    assert draft["to"] == ["jane@example.test"]


def test_current_pdl_associated_phone_projects_as_other_when_mobile_is_absent():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    other = "(404) 555-0198"
    phone_evidence = [{
        "value": other, "type": "landline", "source_field": "phone_numbers",
        "source_fields": ["phone_numbers"], "is_connected": None,
        "mobile_or_wireless": False, "accepted": False,
    }]
    saved = enrich.save_provider_result(candidate_id, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        "phones": [other], "phone_policy": phone_policy.OTHER_PHONE_POLICY,
        "phone_evidence": phone_evidence,
        "associated_phone_evidence": phone_evidence,
        "addresses": ["Atlanta, Georgia"], "provider_location": "Atlanta, Georgia",
        "matched_inputs": ["name", "location"], "confidence": .9,
        "likelihood": 9, "pdl_id": "pdl-jane-other-phone",
    })
    assert saved["enrich_status"] == "success"
    multi_provider._mark_primary_success(store.get_candidate(candidate_id), saved)
    projected = contact_access.project_candidate(store.get_candidate(candidate_id))
    assert projected["phones"] == [other]
    assert projected["phone_contacts"] == [{
        "value": other, "kind": "other", "label": "Other phone",
    }]


def test_candidate_list_and_detail_project_only_current_trusted_contacts():
    store.reset()
    legacy_id = store.add_candidate("Legacy Person", "Atlanta, GA", source="indeed")
    store.update_candidate(
        legacy_id, emails=["legacy@example.test"], phones=["(404) 555-0100"],
        enrich_status="success", contact_expires_at=time.time() + 3600,
    )

    expired_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _save_current_trusted_pdl(expired_id)
    store.update_candidate(expired_id, contact_expires_at=time.time() - 1)

    trusted_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _save_current_trusted_pdl(trusted_id)

    async def exercise_candidate_reads():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed_response = await client.get("/candidates")
            assert listed_response.status_code == 200
            listed = {int(item["id"]): item for item in listed_response.json()}

            for candidate_id in (legacy_id, expired_id):
                assert "contacts_trusted" not in listed[candidate_id]
                assert listed[candidate_id]["emails"] == []
                assert listed[candidate_id]["phones"] == []

                detail_response = await client.get(f"/candidates/{candidate_id}")
                assert detail_response.status_code == 200
                detail = detail_response.json()
                assert "contacts_trusted" not in detail
                assert detail["emails"] == []
                assert detail["phones"] == []

            assert "contacts_trusted" not in listed[trusted_id]
            assert listed[trusted_id]["emails"] == ["jane@example.test"]
            assert listed[trusted_id]["phones"] == ["(404) 555-1212"]
            assert "verification" not in listed[trusted_id]
            assert "provider_person_id" not in listed[trusted_id]
            assert "identity_evidence" not in listed[trusted_id]
            trusted_detail = (await client.get(f"/candidates/{trusted_id}")).json()
            assert "contacts_trusted" not in trusted_detail
            assert trusted_detail["emails"] == ["jane@example.test"]
            assert trusted_detail["phones"] == ["(404) 555-1212"]

    asyncio.run(exercise_candidate_reads())


def test_resume_storage_embeds_only_current_trusted_contacts():
    from pypdf import PdfReader, PdfWriter

    store.reset()
    source = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(source)
    source_pdf = source.getvalue()

    legacy_id = store.add_candidate("Legacy Person", "Atlanta, GA", source="indeed")
    store.update_candidate(
        legacy_id, emails=["legacy@example.test"], phones=["(404) 555-0100"],
        enrich_status="success", contact_expires_at=time.time() + 3600,
    )
    legacy_resume = api_module._store_resume_pdf(legacy_id, "legacy.pdf", source_pdf)
    assert legacy_resume["contact_sheet_embedded"] is False
    assert legacy_resume["contacts_saved"] is False
    assert store.get_resume(legacy_id, legacy_resume["id"])["data"] == source_pdf

    expired_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _save_current_trusted_pdl(expired_id)
    store.update_candidate(expired_id, contact_expires_at=time.time() - 1)
    expired_resume = api_module._store_resume_pdf(expired_id, "expired.pdf", source_pdf)
    assert expired_resume["contact_sheet_embedded"] is False
    assert expired_resume["contacts_saved"] is False
    assert store.get_resume(expired_id, expired_resume["id"])["data"] == source_pdf

    trusted_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _save_current_trusted_pdl(trusted_id)
    trusted_resume = api_module._store_resume_pdf(trusted_id, "trusted.pdf", source_pdf)
    assert trusted_resume["contact_sheet_embedded"] is True
    assert trusted_resume["contacts_saved"] is True
    stored = store.get_resume(trusted_id, trusted_resume["id"])["data"]
    reader = PdfReader(BytesIO(stored))
    assert len(reader.pages) == 2
    text = reader.pages[0].extract_text()
    assert "jane@example.test" in text
    assert "(404) 555-1212" in text


def test_ranking_orders_contactable_and_location():
    store.reset()
    job = store.create_job("Nurse", "Atlanta, GA", "registered nurse healthcare")
    c1 = store.add_candidate("A Local", "Atlanta, GA", job)
    c2 = store.add_candidate("B Far", "Seattle, WA", job)
    enrich.enrich_batch(job_id=job)
    ranking.rank_job(job)
    ranked = store.list_candidates(job_id=job)
    assert ranked[0]["fit_score"] >= ranked[-1]["fit_score"]


def test_verification_keeps_identity_and_deliverability_separate():
    result = verification.assess(
        {"name": "Jane Doe", "location": "Atlanta, GA", "notes": "Registered Nurse"},
        {
            "status": "success",
            "matched_name": "Jane Doe",
            "addresses": ["100 Main St, Atlanta, GA"],
            "emails": ["jane@example.org"],
            "phones": ["(404) 555-1212"],
            "confidence": 0.9,
        },
    )
    assert result["identity_status"] == "verified"
    assert result["emails"][0]["format_valid"] is True
    assert result["emails"][0]["deliverability"] == "not_checked"
    assert result["phones"][0]["identity_owner"] == "not_checked"


def test_location_evidence_does_not_treat_city_substring_as_exact():
    result = verification.location_evidence("York, PA", ["New York, PA"])

    assert result["exact"] is False


def test_location_evidence_matches_exact_city_component_in_street_address():
    result = verification.location_evidence(
        "Atlanta, GA", ["123 Main Street, Atlanta, Georgia 30303"],
    )

    assert result["exact"] is True
    assert result["state_match"] is True
    assert result["conflict"] is False


def test_location_evidence_uses_missouri_not_kansas_from_city_name():
    result = verification.location_evidence(
        "Kansas City, MO", ["Kansas City, Missouri"],
    )

    assert result["exact"] is True
    assert result["state_match"] is True
    assert result["conflict"] is False


def test_location_evidence_uses_trailing_pennsylvania_not_new_york_city_name():
    result = verification.location_evidence(
        "New York, PA", ["New York, Pennsylvania"],
    )

    assert result["exact"] is True
    assert result["state_match"] is True
    assert result["conflict"] is False


def test_region_match_cannot_replace_exact_city_for_pdl_acceptance():
    candidate = {
        "name": "Jane Doe", "location": "Atlanta, GA",
        "notes": "Role: Registered Nurse\nEmployer: Metro Health",
    }
    provider_result = {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "provider_location": "Savannah, Georgia",
        "provider_job_title": "Registered Nurse",
        "provider_company": "Metro Health",
        "emails": ["jane@example.test"], "phones": ["(404) 555-1212"],
        "matched_inputs": ["name", "region"],
        "confidence": .9, "likelihood": 9,
    }

    different_city = verification.assess(candidate, provider_result)
    different_gate = enrich._pdl_quality_gate(different_city, provider_result)
    assert different_city["identity_confidence"] >= config.IDENTITY_MATCH_THRESHOLD
    assert different_city["evidence"]["location"]["state_match"] is True
    assert different_city["evidence"]["location"]["exact"] is False
    assert different_city["evidence"]["provider_region_match"] is True
    assert different_city["evidence"]["provider_location_match"] is False
    assert different_city["identity_status"] != "verified"
    assert different_gate["accepted"] is False

    exact_city_result = {**provider_result, "provider_location": "Atlanta, Georgia"}
    exact_city = verification.assess(candidate, exact_city_result)
    exact_gate = enrich._pdl_quality_gate(exact_city, exact_city_result)
    assert exact_city["evidence"]["location"]["exact"] is True
    assert exact_city["evidence"]["provider_region_match"] is True
    assert exact_city["evidence"]["provider_location_match"] is False
    assert exact_city["identity_status"] == "verified"
    assert exact_gate["accepted"] is True


def test_pdl_alias_with_exact_current_location_is_accepted_but_context_still_helps():
    store.reset()
    candidate_id = store.add_candidate(
        "Elizabeth Cruz", "Atlanta, GA", source="indeed",
    )
    base = {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Elizabeth Marie Jones",
        "name_aliases": ["Elizabeth Cruz"],
        "emails": ["elizabeth@example.test"],
        **_pdl_mobile_contact("(404) 555-1212"),
        "addresses": ["Atlanta, Georgia"],
        "provider_location": "Atlanta, Georgia",
        "matched_inputs": ["name", "location"],
        "confidence": .8, "likelihood": 8, "pdl_id": "alias-no-context",
    }
    review = enrich.save_provider_result(candidate_id, base)
    gate = review["verification"]["evidence"]["pdl_quality_gate"]
    assert review["enrich_status"] == "success"
    assert gate["rule"] == "exact_name_location_and_high_likelihood"
    assert review["emails"] == ["elizabeth@example.test"]

    store.reset()
    candidate_id = store.add_candidate(
        "Elizabeth Cruz", "Atlanta, GA",
        notes="Employer: Emory Healthcare", source="indeed",
    )
    accepted = enrich.save_provider_result(candidate_id, {
        **base,
        "provider_company": "Emory Healthcare",
        "provider_organizations": ["Emory Healthcare"],
        "matched_inputs": ["name", "location", "company"],
        "pdl_id": "alias-with-context",
    })
    gate = accepted["verification"]["evidence"]["pdl_quality_gate"]
    assert accepted["enrich_status"] == "success"
    assert gate["accepted"] is True
    assert gate["alias_match"] is True
    assert accepted["emails"] == ["elizabeth@example.test"]


def test_historical_pdl_address_requires_current_source_context():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    base = {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        **_pdl_mobile_contact("(404) 555-1212"),
        "addresses": ["Atlanta, Georgia", "Seattle, Washington"],
        "address_evidence": [{
            "value": "Atlanta, Georgia", "is_current": False,
            "last_seen": "2018-01-01",
        }],
        "provider_location": "Seattle, Washington",
        "matched_inputs": ["name"],
        "confidence": .9, "likelihood": 9, "pdl_id": "old-address-only",
    }
    review = enrich.save_provider_result(candidate_id, base)
    gate = review["verification"]["evidence"]["pdl_quality_gate"]
    assert review["enrich_status"] == "review"
    assert gate["rule"] == "historical_address_needs_independent_corroboration"

    store.reset()
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", notes="Employer: Emory Healthcare",
        source="indeed",
    )
    accepted = enrich.save_provider_result(candidate_id, {
        **base,
        "provider_company": "Emory Healthcare",
        "provider_organizations": ["Emory Healthcare"],
        "matched_inputs": ["name", "company"],
        "pdl_id": "old-address-with-context",
    })
    assert accepted["enrich_status"] == "success"
    assert accepted["verification"]["evidence"]["pdl_quality_gate"]["accepted"] is True


def test_same_state_pdl_match_requires_provider_confirmed_organization():
    store.reset()
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", notes="Employer: Emory Healthcare",
        source="indeed",
    )
    base = {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        **_pdl_mobile_contact("(404) 555-1212"),
        "addresses": ["Savannah, Georgia"],
        "provider_location": "Savannah, Georgia",
        "provider_company": "Emory Healthcare",
        "provider_organizations": ["Emory Healthcare"],
        "confidence": .9, "likelihood": 9,
    }
    state_only = enrich.save_provider_result(candidate_id, {
        **base, "matched_inputs": ["name", "region"],
        "pdl_id": "same-state-only",
    })
    assert state_only["enrich_status"] != "success"
    assert state_only["verification"]["evidence"]["state_and_organization_match"] is False

    store.reset()
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", notes="Employer: Emory Healthcare",
        source="indeed",
    )
    corroborated = enrich.save_provider_result(candidate_id, {
        **base, "matched_inputs": ["name", "region", "company"],
        "pdl_id": "same-state-company",
    })
    evidence = corroborated["verification"]["evidence"]
    assert corroborated["enrich_status"] == "success"
    assert evidence["state_and_organization_match"] is True
    assert evidence["pdl_quality_gate"]["rule"] == "exact_name_state_and_organization"


def test_provider_relative_name_is_never_used_as_the_primary_identity():
    assessment = verification.assess({
        "name": "Jane Doe", "location": "Atlanta, GA",
    }, {
        "source": "enformion", "status": "success",
        "matched_name": "Alice Smith", "provider_relatives": ["Jane Doe"],
        "provider_location": "Atlanta, GA", "addresses": ["Atlanta, GA"],
        "emails": ["alice@example.test"], "phones": ["(404) 555-1212"],
        "confidence": .99,
    })
    assert assessment["identity_status"] == "rejected"
    assert assessment["evidence"]["name"]["conflict"] is True
    assert assessment["evidence"]["relative_identity_used"] is False


def test_linkedin_pdl_acceptance_requires_confirmed_source_profile():
    candidate = {
        "name": "Jane Doe", "location": "Atlanta, GA",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/in/jane-doe-rn/",
    }
    provider_result = {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "provider_location": "Atlanta, Georgia",
        "emails": ["jane@example.test"], "phones": ["(404) 555-1212"],
        "matched_inputs": ["name", "location"],
        "confidence": .9, "likelihood": 9,
    }

    unconfirmed = verification.assess(candidate, provider_result)
    unconfirmed_gate = enrich._pdl_quality_gate(unconfirmed, provider_result)
    assert unconfirmed["identity_status"] == "verified"
    assert unconfirmed["evidence"]["expected_social_profile"] == "linkedin:jane-doe-rn"
    assert unconfirmed["evidence"]["social_profile_match"] is False
    assert unconfirmed_gate["accepted"] is False
    assert unconfirmed_gate["rule"] == "source_social_profile_not_confirmed"

    matched_result = {
        **provider_result,
        "matched_inputs": ["name", "location", "profile"],
    }
    confirmed = verification.assess(candidate, matched_result)
    confirmed_gate = enrich._pdl_quality_gate(confirmed, matched_result)
    assert confirmed["evidence"]["provider_profile_match"] is True
    assert confirmed["evidence"]["social_profile_match"] is True
    assert confirmed_gate["accepted"] is True
    assert confirmed_gate["rule"] == "exact_name_and_social_profile"


def test_identity_discovery_requires_exact_city_for_nppes_and_internal_records():
    candidate = {
        "name": "Jane Doe", "location": "Atlanta, GA",
        "notes": "Role: Registered Nurse\nEmployer: Metro Health",
    }
    for provider in ("nppes", "internal_database"):
        different_city = {
            "full_name": "Jane Doe", "location_name": "Savannah, Georgia",
            "job_title": "Registered Nurse", "job_company_name": "Metro Health",
            "provider": provider, "provider_person_id": f"{provider}-jane-doe",
            "confidence": 1,
            "matched_inputs": [
                "name", "region" if provider == "nppes" else "location",
            ],
        }
        scored = identity_resolution.score(candidate, different_city)
        assert scored["score"] >= config.IDENTITY_MATCH_THRESHOLD
        assert scored["evidence"]["location"]["state_match"] is True
        assert scored["evidence"]["location"]["exact"] is False
        assert scored["eligible"] is False
        assert identity_resolution.choose(
            candidate, [different_city], provider,
        )["status"] == "no_match"

        exact_city = {**different_city, "location_name": "Atlanta, Georgia"}
        assert identity_resolution.score(candidate, exact_city)["eligible"] is True
        assert identity_resolution.choose(
            candidate, [exact_city], provider,
        )["status"] == "verified"


def test_internal_identity_resolution_expands_masked_name_without_provider_credit():
    store.reset()
    master = store.add_candidate(
        "Jane Doe", "Atlanta, GA", notes="Role: Registered Nurse", source="indeed",
    )
    store.update_candidate(
        master, canonical_name="Jane Doe", identity_status="verified",
        identity_score=0.94, identity_provider="people_data_labs",
        provider_person_id="pdl-jane-doe", identity_verified_at=123,
        identity_evidence={"provider_job_title": "Registered Nurse"},
    )
    alias = store.add_candidate(
        "Jane D", "Atlanta, GA", notes="Role: Registered Nurse", source="vivian",
    )
    resolution = identity_resolution.resolve(store.get_candidate(alias))
    assert resolution["status"] == "verified"
    assert resolution["provider"] == "internal_database"
    assert resolution["canonical_name"] == "Jane Doe"
    assert resolution["credits_spent"] == 0
    identity_resolution.persist(alias, resolution)
    stored = store.get_candidate(alias)
    assert stored["master_candidate_id"] == master
    assert stored["provider_person_id"] == "pdl-jane-doe"


def test_identity_resolution_sends_equal_candidates_to_review():
    candidate = {
        "name": "Jane D", "location": "Atlanta, GA",
        "notes": "Role: Registered Nurse",
    }
    people = [
        {
            "full_name": name, "location_name": "Atlanta, Georgia",
            "job_title": "Registered Nurse", "provider": "nppes",
            "confidence": 1, "matched_inputs": ["name", "region"],
        }
        for name in ("Jane Doe", "Jane Davis")
    ]
    resolution = identity_resolution.choose(candidate, people, "nppes")
    assert resolution["status"] == "review"
    assert len(resolution["alternatives"]) == 2


def test_verified_provider_id_marks_cross_platform_duplicate():
    store.reset()
    first = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    payload = {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        **_pdl_mobile_contact("(404) 555-1212"), "addresses": ["Atlanta, Georgia"],
        "provider_location": "Atlanta, Georgia", "matched_inputs": ["name", "location"],
        "confidence": .9, "likelihood": 9, "pdl_id": "person-123",
    }
    assert enrich.save_provider_result(first, payload)["enrich_status"] == "success"
    second = store.add_candidate("Jane Doe", "Atlanta, GA", source="vivian")
    assert enrich.save_provider_result(second, payload)["enrich_status"] == "success"
    assert store.get_candidate(second)["master_candidate_id"] == first


def test_low_likelihood_pdl_contact_is_withheld_for_fallback():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    saved = enrich.save_provider_result(candidate_id, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        **_pdl_mobile_contact("(404) 555-1212"), "addresses": ["Atlanta, Georgia"],
        "provider_location": "Atlanta, Georgia", "matched_inputs": ["name", "location"],
        "confidence": .9, "likelihood": 3, "pdl_id": "weak-person-123",
    })
    assert saved["enrich_status"] == "review"
    assert saved["emails"] == []
    assert saved["phones"] == []
    assert saved["verification"]["evidence"]["pdl_quality_gate"]["accepted"] is False
    assert store.get_candidate(candidate_id)["emails"] == []


def test_exact_name_and_location_accepts_pdl_likelihood_six(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "PDL_AUTO_ACCEPT_MIN_LIKELIHOOD", 6)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    saved = enrich.save_provider_result(candidate_id, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        **_pdl_mobile_contact("(404) 555-1212"), "addresses": ["Atlanta, Georgia"],
        "provider_location": "Atlanta, Georgia", "matched_inputs": ["name", "location"],
        "confidence": .6, "likelihood": 6, "pdl_id": "six-person-123",
    })
    assert saved["enrich_status"] == "success"
    gate = saved["verification"]["evidence"]["pdl_quality_gate"]
    assert gate["accepted"] is True
    assert gate["minimum_likelihood"] == 6


def test_internal_verified_contact_is_reused_without_provider_credit(monkeypatch):
    store.reset()
    master = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _save_current_trusted_pdl(master)
    alias = store.add_candidate("Jane Doe", "Atlanta, GA", source="linkedin")
    store.update_candidate(
        alias, canonical_name="Jane Doe", identity_status="verified",
        identity_score=.94, identity_provider="internal_database",
        provider_person_id="pdl-jane-cache", master_candidate_id=master,
    )
    provider_calls = []
    monkeypatch.setattr(config, "PDL_API_KEY", "server-only-test-key")
    monkeypatch.setattr(config, "PDL_ENABLED", True)
    monkeypatch.setattr(
        pdl_client, "_call",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )
    reused = pdl_client.enrich_candidate(alias, "trusted_reuse_123")
    assert reused["status"] == "success"
    assert reused["trusted_cache"] is True
    assert reused["credits_spent"] == 0
    assert reused["emails"] == ["jane@example.test"]
    assert provider_calls == []
    assert store.get_candidate(alias)["phones"] == ["(404) 555-1212"]


def test_weak_stored_pdl_likelihood_is_not_reused():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    stored = _save_current_trusted_pdl(candidate_id)
    verification_record = dict(stored["verification"])
    evidence = dict(verification_record["evidence"])
    evidence["pdl_likelihood"] = 3
    # Simulate an older policy that accepted a weak result. Reuse must apply
    # today's threshold to the stored evidence, not trust this old boolean.
    evidence["pdl_quality_gate"] = {"accepted": True}
    verification_record["evidence"] = evidence
    store.update_candidate(candidate_id, verification=verification_record)

    current = store.get_candidate(candidate_id)
    assert current["emails"] == ["jane@example.test"]
    assert pdl_client._trusted_contact_source(current) is None
    assert pdl_client._already_complete(current) is None


def test_stored_linkedin_pdl_contact_requires_social_match_for_reuse():
    store.reset()
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", source="linkedin",
        source_url="https://www.linkedin.com/in/jane-doe-rn/",
        source_id="jane-doe-rn",
    )
    saved = enrich.save_provider_result(candidate_id, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        **_pdl_mobile_contact("(404) 555-1212"),
        "addresses": ["Atlanta, Georgia"],
        "provider_location": "Atlanta, Georgia",
        "matched_inputs": ["name", "location", "profile"],
        "confidence": .9, "likelihood": 9,
        "pdl_id": "pdl-linkedin-jane",
    })
    assert saved["enrich_status"] == "success"
    multi_provider._mark_primary_success(store.get_candidate(candidate_id), saved)
    trusted = store.get_candidate(candidate_id)
    assert pdl_client._trusted_contact_source(trusted) is not None

    verification_record = dict(trusted["verification"])
    evidence = dict(verification_record["evidence"])
    evidence.update({
        "social_profile_match": False,
        "exact_social_profile_match": False,
        "provider_profile_match": False,
        "matched_inputs": ["name", "location"],
    })
    verification_record["evidence"] = evidence
    store.update_candidate(candidate_id, verification=verification_record)

    unconfirmed = store.get_candidate(candidate_id)
    assert unconfirmed["emails"] == ["jane@example.test"]
    assert pdl_client._trusted_contact_source(unconfirmed) is None
    assert pdl_client._already_complete(unconfirmed) is None


def test_source_location_change_invalidates_stored_contact_trust():
    store.reset()
    imported = store.upsert_candidate_profiles([{
        "name": "Jane Doe", "location": "Atlanta, GA",
        "source": "linkedin", "source_id": "jane-doe-rn",
        "source_url": "https://www.linkedin.com/in/jane-doe-rn/",
    }])[0]
    candidate_id = imported["id"]
    trusted = _save_current_trusted_pdl(candidate_id)
    assert trusted["verification"]["evidence"]["contact_trust_policy"] == (
        trust_policy.CONTACT_TRUST_POLICY
    )

    corrected = store.upsert_candidate_profiles([{
        "name": "Jane Doe", "location": "Philadelphia, PA",
        "source": "linkedin", "source_id": "jane-doe-rn",
        "source_url": "https://www.linkedin.com/in/jane-doe-rn/",
    }])[0]
    refreshed = corrected["candidate"]
    assert corrected["id"] == candidate_id
    assert corrected["imported"] is False
    assert refreshed["location"] == "Philadelphia, PA"
    assert refreshed["emails"] == []
    assert refreshed["phones"] == []
    assert refreshed["addresses"] == []
    assert refreshed["verification"] == {}
    assert refreshed["enrich_status"] == "pending"
    assert refreshed["identity_status"] == "captured"
    assert refreshed["provider_person_id"] == ""
    assert refreshed["contact_expires_at"] == 0


def test_distinct_linkedin_source_ids_do_not_merge_or_inherit_contacts_sqlite():
    store.reset()
    shared = {"name": "Jane Doe", "location": "Atlanta, GA", "source": "linkedin"}
    first = store.upsert_candidate_profiles([{
        **shared,
        "source_id": "jane-doe-clinical",
        "source_url": "https://www.linkedin.com/in/jane-doe-clinical/",
    }])[0]
    trusted = _save_current_trusted_pdl(first["id"])
    assert trusted["emails"] == ["jane@example.test"]

    second = store.upsert_candidate_profiles([{
        **shared,
        "source_id": "jane-doe-travel",
        "source_url": "https://www.linkedin.com/in/jane-doe-travel/",
    }])[0]
    assert second["imported"] is True
    assert second["id"] != first["id"]
    assert second["candidate"]["source_id"] == "jane-doe-travel"
    assert second["candidate"]["source_url"].endswith("/jane-doe-travel/")
    assert second["candidate"]["emails"] == []
    assert second["candidate"]["phones"] == []
    assert second["candidate"]["provider_person_id"] == ""
    assert second["candidate"]["master_candidate_id"] is None

    repeated = store.upsert_candidate_profiles([{
        **shared,
        "source_id": "jane-doe-clinical",
        "source_url": "https://www.linkedin.com/in/jane-doe-clinical/",
    }])[0]
    assert repeated["imported"] is False
    assert repeated["id"] == first["id"]
    assert len([row for row in store.list_candidates() if row["source"] == "linkedin"]) == 2


def test_postgres_upsert_keeps_distinct_source_ids_and_same_id_dedup(monkeypatch):
    """Exercise PostgreSQL batch canonicalization without a live Neon database."""
    shared = {"name": "Jane Doe", "location": "Atlanta, GA", "source": "linkedin"}
    profiles = [
        {
            **shared,
            "source_id": "jane-doe-clinical",
            "source_url": "https://www.linkedin.com/in/jane-doe-clinical/",
        },
        {
            **shared,
            "source_id": "jane-doe-clinical",
            "source_url": "https://www.linkedin.com/in/jane-doe-clinical/",
        },
        {
            **shared,
            "source_id": "jane-doe-travel",
            "source_url": "https://www.linkedin.com/in/jane-doe-travel/",
        },
    ]

    class FakePostgres:
        postgres = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, args):
            sql = " ".join(query.split())
            assert "i.source_id<>'' AND c.source=i.source AND c.source_id=i.source_id" in sql
            assert "OR ( i.source_id='' AND LOWER(c.source)=LOWER(i.source)" in sql
            # Same source_id was canonically deduplicated, while the second
            # non-empty source_id survived despite equal name and location.
            assert len(args) == 22
            rows = []
            for offset in range(0, len(args), 11):
                (
                    ordinal, name, location, hometown, _job_id, _notes, source,
                    source_url, source_id, _created, _updated,
                ) = args[offset:offset + 11]
                existing = source_id == "jane-doe-clinical"
                rows.append({
                    "input_ordinal": ordinal,
                    "imported": not existing,
                    "id": 101 if existing else 202,
                    "name": name,
                        "location": location,
                        "hometown": hometown,
                    "source": source,
                    "source_id": source_id,
                    "source_url": source_url,
                    "emails": json.dumps(["jane@example.test"] if existing else []),
                    "phones": json.dumps(["(404) 555-1212"] if existing else []),
                    "addresses": "[]",
                    "verification": "{}",
                    "identity_evidence": "{}",
                    "provider_person_id": "pdl-existing" if existing else "",
                    "master_candidate_id": None,
                })
            return rows

    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://unit-test")
    monkeypatch.setattr(store, "_conn", lambda: FakePostgres())
    results = store.upsert_candidate_profiles(profiles)
    assert [row["id"] for row in results] == [101, 101, 202]
    assert results[0]["candidate"]["emails"] == ["jane@example.test"]
    assert results[2]["candidate"]["emails"] == []
    assert results[2]["candidate"]["phones"] == []
    assert results[2]["candidate"]["provider_person_id"] == ""


def test_rejected_linkedin_social_match_cannot_reuse_other_slug_contacts():
    store.reset()
    first = store.add_candidate(
        "Jane Doe", "Atlanta, GA", source="linkedin",
        source_id="jane-doe-clinical",
        source_url="https://www.linkedin.com/in/jane-doe-clinical/",
    )
    _save_current_trusted_pdl(first)
    second = store.add_candidate(
        "Jane Doe", "Atlanta, GA", source="linkedin",
        source_id="jane-doe-travel",
        source_url="https://www.linkedin.com/in/jane-doe-travel/",
    )
    rejected = enrich.save_provider_result(second, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["wrong@example.test"],
        **_pdl_mobile_contact("(404) 555-0199"),
        "addresses": ["Atlanta, Georgia"],
        "provider_location": "Atlanta, Georgia",
        "matched_inputs": ["name", "location"],
        "confidence": .9, "likelihood": 9,
        "pdl_id": "pdl-jane-cache",
    })
    assert rejected["enrich_status"] == "review"
    assert rejected["verification"]["evidence"]["pdl_quality_gate"]["rule"] == (
        "source_social_profile_not_confirmed"
    )
    stored = store.get_candidate(second)
    assert stored["provider_person_id"] == ""
    assert stored["master_candidate_id"] is None
    assert stored["emails"] == [] and stored["phones"] == []

    # Defense in depth for rows written by an older build: even a shared PDL
    # person ID/master pointer cannot cross two explicit LinkedIn slugs.
    store.update_candidate(
        second, provider_person_id="pdl-jane-cache", master_candidate_id=first,
        identity_status="verified",
    )
    legacy = store.get_candidate(second)
    assert pdl_client._trusted_contact_source(legacy) is None
    assert pdl_client._already_complete(legacy) is None

    # Also quarantine a contact already copied by an older build, whose copied
    # fingerprint alone would otherwise make the alias look self-verified.
    original = store.get_candidate(first)
    copied_verification = json.loads(json.dumps(original["verification"]))
    copied_evidence = copied_verification["evidence"]
    copied_evidence["source_identity_fingerprint"] = (
        trust_policy.source_identity_fingerprint(legacy)
    )
    copied_evidence["internal_contact_reuse"] = {
        "source_candidate_id": first, "reused_at": time.time(),
    }
    store.update_candidate(
        second,
        emails=original["emails"], phones=original["phones"],
        addresses=original["addresses"], verification=copied_verification,
        contact_verified_at=original["contact_verified_at"],
        contact_expires_at=original["contact_expires_at"],
    )
    copied = store.get_candidate(second)
    assert pdl_client._trusted_contact_source(copied) is None
    assert contact_access.project_candidate(copied)["contacts_trusted"] is False
    assert contact_access.project_candidate(copied)["emails"] == []


def test_linkedin_enformion_only_result_requires_review_but_social_pdl_can_supplement():
    store.reset()
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", source="linkedin",
        source_id="jane-doe-clinical",
        source_url="https://www.linkedin.com/in/jane-doe-clinical/",
    )
    enformion_result = {
        "status": "success", "source": "enformion", "matched_name": "Jane Doe",
        "emails": ["jane@example.test"], "phones": ["(404) 555-1212"],
        "addresses": ["Atlanta, Georgia"], "provider_location": "Atlanta, Georgia",
        "confidence": .9, "request_fields": ["name", "location"],
        "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
        "phone_evidence": [{
            "value": "(404) 555-1212", "type": "Wireless", "is_connected": True,
            "mobile_or_wireless": True, "accepted": True,
        }],
    }
    request = {
        "reason": "pdl_no_accepted_contact", "independent": True,
        "seed_type": "independent_name_location_v4_mobile_wireless_only",
    }
    review = multi_provider._apply_fallback(
        store.get_candidate(candidate_id),
        {"status": "no_match", "emails": [], "phones": [], "verification": {
            "identity_status": "review", "evidence": {
                "pdl_quality_gate": {"accepted": False},
            },
        }},
        enformion_result, request, cached=False,
    )
    assert review["status"] == "review"
    assert review["provider"] == "enformion_identity_review"
    assert review["contacts_trusted"] is False
    assert review["emails"] == [] and review["phones"] == []
    stored = store.get_candidate(candidate_id)
    assert stored["emails"] == [] and stored["phones"] == []
    assert stored["contact_expires_at"] == 0

    # A contact accepted by an older build must also fail today's reuse gate;
    # database presence is not evidence that it belongs to this LinkedIn slug.
    now = time.time()
    store.update_candidate(
        candidate_id,
        emails=["legacy@example.test"], phones=["(404) 555-1212"],
        identity_status="verified", contact_verified_at=now,
        contact_expires_at=now + 3600,
        verification={
            "source": "enformion", "identity_status": "verified",
            "provider_contacts": {
                "emails": ["legacy@example.test"],
                "phones": ["(404) 555-1212"], "addresses": [],
            },
            "evidence": {
                "contact_trust_policy": trust_policy.CONTACT_TRUST_POLICY,
                "source_identity_fingerprint": trust_policy.source_identity_fingerprint(stored),
                "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
                "contact_verification": {
                    "status": "enformion_fallback", "automatic_use_allowed": True,
                    "enformion_identity_status": "verified",
                    "enformion_identity_confidence": .99,
                    "enformion_name_exact": True, "enformion_location_exact": True,
                    "enformion_conflicts": [],
                    "enformion_phone_evidence": [{
                        "value": "(404) 555-1212", "type": "Wireless",
                        "is_connected": True, "mobile_or_wireless": True,
                        "accepted": True,
                    }],
                },
            },
        },
    )
    assert pdl_client._trusted_contact_source(store.get_candidate(candidate_id)) is None

    accepted_pdl = enrich.save_provider_result(candidate_id, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        "phones": [], "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
        "phone_evidence": [], "addresses": ["Atlanta, Georgia"],
        "provider_location": "Atlanta, Georgia",
        "matched_inputs": ["name", "location", "profile"],
        "confidence": .9, "likelihood": 9, "pdl_id": "pdl-linkedin-jane",
    })
    no_overlap = multi_provider._apply_fallback(
        store.get_candidate(candidate_id), {**accepted_pdl, "status": "success"},
        {
            **enformion_result,
            "emails": ["different-person@example.test"],
        },
        request, cached=False,
    )
    assert no_overlap["contact_verification"]["status"] == "pdl_high_confidence"
    assert no_overlap["enformion_supplement_review"]["status"] == "enformion_supplement_review"
    assert no_overlap["phones"] == []
    assert "withheld for review" in no_overlap["message"]
    persisted_pdl_only = contact_access.project_candidate(store.get_candidate(candidate_id))
    assert persisted_pdl_only["contacts_trusted"] is True
    assert persisted_pdl_only["emails"] == ["jane@example.test"]
    assert persisted_pdl_only["phones"] == []

    supplemented = multi_provider._apply_fallback(
        store.get_candidate(candidate_id), {**accepted_pdl, "status": "success"},
        enformion_result, request, cached=False,
    )
    assert supplemented["status"] == "success"
    assert supplemented["provider"] == "people_data_labs+enformion"
    assert supplemented["contact_verification"]["status"] == "enformion_supplemented"
    assert supplemented["contact_verification"]["cross_provider_contact_overlap"] is True
    assert supplemented["contact_verification"]["cross_provider_overlap_emails"] == ["jane@example.test"]
    assert supplemented["emails"] == ["jane@example.test"]
    assert supplemented["phones"] == ["(404) 555-1212"]

    # A legacy LinkedIn supplemented record without explicit cross-provider
    # binding cannot be silently reused from the database.
    legacy = store.get_candidate(candidate_id)
    legacy_verification = legacy["verification"]
    legacy_contact = legacy_verification["evidence"]["contact_verification"]
    legacy_contact.pop("cross_provider_contact_overlap", None)
    legacy_contact.pop("cross_provider_overlap_emails", None)
    legacy_contact.pop("cross_provider_overlap_phones", None)
    store.update_candidate(candidate_id, verification=legacy_verification)
    assert pdl_client._trusted_contact_source(store.get_candidate(candidate_id)) is None


def test_legacy_untyped_phone_is_not_reused_as_mobile():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    now = time.time()
    store.update_candidate(
        candidate_id, canonical_name="Jane Doe", identity_status="verified",
        phones=["(404) 555-0100"], emails=["jane@example.test"],
        contact_verified_at=now, contact_expires_at=now + 3600,
        verification={
            "source": "people_data_labs", "identity_status": "verified",
            "evidence": {
                "required_field": "mobile_phone",
                "pdl_quality_gate": {"accepted": True},
                "contact_verification": {
                    "status": "pdl_high_confidence", "automatic_use_allowed": True,
                },
            },
        },
    )
    assert pdl_client._trusted_contact_source(store.get_candidate(candidate_id)) is None
    assert pdl_client._already_complete(store.get_candidate(candidate_id)) is None


def test_fresh_pdl_mobile_quarantines_legacy_untyped_candidate_phone():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    store.update_candidate(candidate_id, phones=["(404) 555-0100"])
    saved = enrich.save_provider_result(candidate_id, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane@example.test"],
        **_pdl_mobile_contact("(404) 555-0199"),
        "addresses": ["Atlanta, Georgia"], "provider_location": "Atlanta, Georgia",
        "matched_inputs": ["name", "location"], "confidence": .9,
        "likelihood": 9, "pdl_id": "mobile-policy-person",
    })
    assert saved["phones"] == ["(404) 555-0199"]
    assert saved["stored_phones"] == ["(404) 555-0199"]
    assert saved["verification"]["evidence"]["legacy_untyped_phones_quarantined"] == [
        "(404) 555-0100"
    ]


def test_fresh_pdl_response_replaces_older_mobile_and_address():
    store.reset()
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    previous = _save_current_trusted_pdl(candidate_id)
    assert previous["phones"] == ["(404) 555-1212"]
    assert previous["addresses"] == ["Atlanta, Georgia"]
    assert phone_policy.verification_phone_policy(previous) == (
        phone_policy.MOBILE_PHONE_POLICY
    )

    saved = enrich.save_provider_result(candidate_id, {
        "source": "people_data_labs", "status": "success",
        "matched_name": "Jane Doe", "emails": ["jane.new@example.test"],
        **_pdl_mobile_contact("(404) 555-1313"),
        "addresses": ["123 New St, Atlanta, GA"],
        "provider_location": "Atlanta, Georgia",
        "matched_inputs": ["name", "location"],
        "confidence": .9, "likelihood": 9,
        "pdl_id": "pdl-jane-cache",
    })

    assert saved["stored_phones"] == ["(404) 555-1313"]
    assert saved["stored_addresses"] == ["123 New St, Atlanta, GA"]
    evidence = saved["verification"]["evidence"]
    assert evidence["previous_phones_replaced"] == ["(404) 555-1212"]
    assert evidence["previous_addresses_replaced"] == ["Atlanta, Georgia"]
    refreshed = store.get_candidate(candidate_id)
    assert refreshed["phones"] == ["(404) 555-1313"]
    assert refreshed["addresses"] == ["123 New St, Atlanta, GA"]


def test_enformion_persistence_accepts_connected_landline_fallback():
    candidate = {
        "name": "Jane Doe", "location": "Atlanta, GA",
    }
    result = {
        "status": "success", "source": "enformion", "matched_name": "Jane Doe",
        "emails": ["jane@example.test"], "phones": ["(404) 555-0100"],
        "addresses": ["Atlanta, GA"], "provider_location": "Atlanta, GA",
        "confidence": .9,
        "phone_evidence": [{
            "value": "(404) 555-0100", "type": "Landline", "is_connected": True,
            "mobile_or_wireless": False, "accepted": False,
        }],
    }
    assessment = verification.assess(candidate, result)
    emails, phones = multi_provider._allowed_contacts(result, assessment)
    assert emails == ["jane@example.test"]
    assert phones == ["(404) 555-0100"]


def test_pdl_fallback_caches_matches_and_enforces_run_credit_limit(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "PDL_API_KEY", "server-only-test-key")
    monkeypatch.setattr(config, "PDL_ENABLED", True)
    monkeypatch.setattr(config, "PDL_RUN_CREDIT_LIMIT", 2)
    monkeypatch.setattr(config, "PDL_CACHE_TTL_SECONDS", 3600)
    calls = []

    def fake_call(name, location, companies=None, schools=None):
        calls.append((name, location))
        city, state_code = [part.strip() for part in location.split(",", 1)]
        state_name = pdl_client._STATE_NAMES[state_code.lower()]
        response = httpx.Response(
            200,
            headers={"X-Call-Credits-Spent": "1"},
            request=httpx.Request("GET", "https://api.peopledatalabs.com/v5/person/enrich"),
        )
        first, *middle, last = name.split()
        return response, {
            "likelihood": 9,
            "matched": ["name", "location"],
            "data": {
                "first_name": first,
                "last_name": last,
                "full_name": name,
                "location_name": f"{city}, {state_name}, United States",
                "personal_emails": [f"{first.lower()}.{last.lower()}@example.test"],
                "mobile_phone": "(404) 555-0187",
                "phone_numbers": ["(404) 555-0187"],
            },
        }

    monkeypatch.setattr(pdl_client, "_call", fake_call)
    run_id = "testrun_12345678"
    first_id = store.add_candidate("Alex Morgan", "Atlanta, GA", source="indeed")
    first = pdl_client.enrich_candidate(first_id, run_id)
    assert first["status"] == "success"
    assert first["provider"] == "people_data_labs"
    assert first["credits_spent"] == 1
    assert first["emails"] and first["phones"]
    assert store.get_candidate(first_id)["verification"]["source"] == "people_data_labs"

    # Removing the local contacts simulates a retry. The provider response is
    # replayed from cache and does not spend another credit.
    store.update_candidate(first_id, emails=[], phones=[], enrich_status="pending")
    cached = pdl_client.enrich_candidate(first_id, "another_run_1234")
    assert cached["status"] == "success"
    assert cached["cached"] is True
    assert cached["credits_spent"] == 0
    assert len(calls) == 1

    second_id = store.add_candidate("Taylor Reed", "Austin, TX", source="indeed")
    second = pdl_client.enrich_candidate(second_id, run_id)
    assert second["status"] == "success"
    assert second["run_credits_spent"] == 2

    third_id = store.add_candidate("Jordan Lee", "Miami, FL", source="indeed")
    blocked = pdl_client.enrich_candidate(third_id, run_id)
    assert blocked["status"] == "budget_exhausted"
    assert blocked["credits_spent"] == 0
    assert len(calls) == 2


def test_pdl_provider_trust_flag_cannot_bypass_identity_validation(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "PDL_TRUST_PROVIDER_MATCH", True)
    candidate_id = store.add_candidate("Alex Morgan", "Atlanta, GA", source="indeed")
    saved = enrich.save_provider_result(candidate_id, {
        "source": "people_data_labs",
        "status": "success",
        "matched_name": "Alex Morgan",
        "emails": ["provider-raw-contact"],
        "phones": [],
        "addresses": ["Atlanta, Georgia, United States"],
        "confidence": 0.1,
    })
    assert saved["enrich_status"] == "no_match"
    assert saved["emails"] == []
    assert saved["verification"]["method"] == "deterministic_identity_evidence_v1"
    assert saved["verification"]["evidence"]["local_validation_bypassed"] is False
    assert store.get_candidate(candidate_id)["emails"] == []
    assert pdl_client._unique([True, False, "real@example.test"]) == ["real@example.test"]
    assert pdl_client._retry_cached_local_rejection({
        "status": "no_match",
        "message": "People Data Labs identity/contact evidence was not strong enough to save.",
    }) is True


def _pdl_person(name, location):
    city, state_code = [part.strip() for part in location.split(",", 1)]
    state_name = pdl_client._STATE_NAMES[state_code.lower()]
    first, *_middle, last = name.split()
    return {
        "full_name": name,
        "location_name": f"{city}, {state_name}, United States",
        "personal_emails": [f"{first.lower()}.{last.lower()}@example.test"],
        "mobile_phone": "(404) 555-0187",
        # The broad collection deliberately includes a different associated
        # line; only mobile_phone may reach normalized output.
        "phone_numbers": ["(404) 555-0198"],
        "job_title": "Registered Nurse",
        "job_company_name": "Example Medical Center",
    }


def _enable_pdl(monkeypatch, run_credit_limit=2):
    monkeypatch.setattr(config, "PDL_API_KEY", "server-only-test-key")
    monkeypatch.setattr(config, "PDL_ENABLED", True)
    monkeypatch.setattr(config, "PDL_RUN_CREDIT_LIMIT", run_credit_limit)
    monkeypatch.setattr(config, "PDL_CACHE_TTL_SECONDS", 3600)


def test_pdl_batch_resolves_a_selection_with_one_bulk_call(monkeypatch):
    store.reset()
    _enable_pdl(monkeypatch, run_credit_limit=2)
    bulk_calls = []

    def fake_bulk_call(identities):
        bulk_calls.append(list(identities))
        response = httpx.Response(
            200,
            headers={"X-Call-Credits-Spent": str(len(identities))},
            request=httpx.Request("POST", "https://api.peopledatalabs.com/v5/person/bulk"),
        )
        return response, [
            {
                "status": 200,
                "likelihood": 9,
                "matched": ["name", "location"],
                "data": _pdl_person(name, location),
            }
            for name, location, _companies, _schools in identities
        ]

    def forbidden_single_call(name, location, companies=None, schools=None):
        raise AssertionError("the batch path must not fall back to single lookups")

    monkeypatch.setattr(pdl_client, "_bulk_call", fake_bulk_call)
    monkeypatch.setattr(pdl_client, "_call", forbidden_single_call)

    ids = [
        store.add_candidate("Alex Morgan", "Atlanta, GA", source="indeed"),
        store.add_candidate("Taylor Reed", "Austin, TX", source="indeed"),
        store.add_candidate("Jordan Lee", "Miami, FL", source="indeed"),
    ]
    batch = pdl_client.enrich_candidates(ids, "batchrun_12345678")
    results = batch["results"]

    # One provider request for the whole selection, capped at the run budget.
    assert len(bulk_calls) == 1
    assert bulk_calls[0] == [
        ("Alex Morgan", "Atlanta, GA", [], []),
        ("Taylor Reed", "Austin, TX", [], []),
    ]
    assert results[ids[0]]["status"] == "success"
    assert results[ids[0]]["emails"] and results[ids[0]]["phones"]
    assert results[ids[1]]["status"] == "success"
    assert results[ids[2]]["status"] == "budget_exhausted"
    assert batch["credits_spent"] == 2
    assert batch["run_credits_spent"] == 2
    assert store.get_candidate(ids[0])["verification"]["source"] == "people_data_labs"
    assert store.get_candidate(ids[0])["verification"]["evidence"]["pdl_likelihood"] == 9
    assert store.get_candidate(ids[0])["verification"]["evidence"]["matched_inputs"] == ["name", "location"]
    assert store.get_candidate(ids[0])["verification"]["evidence"]["provider_job_title"] == "Registered Nurse"
    assert store.get_candidate(ids[1])["emails"]
    assert store.get_candidate(ids[2])["emails"] == []

    # A retry replays the cached identity instead of buying it again.
    store.update_candidate(ids[0], emails=[], phones=[], enrich_status="pending")
    replay = pdl_client.enrich_candidates([ids[0]], "replayrun_1234567")
    assert replay["credits_spent"] == 0
    assert replay["results"][ids[0]]["status"] == "success"
    assert replay["results"][ids[0]]["cached"] is True
    assert store.get_candidate(ids[0])["emails"]
    assert len(bulk_calls) == 1


def test_masked_healthcare_name_uses_npi_before_pdl_contact_lookup(monkeypatch):
    store.reset()
    _enable_pdl(monkeypatch, run_credit_limit=5)
    monkeypatch.setattr(identity_resolution.npi_client, "search", lambda candidate: [{
        "full_name": "Jane Doe", "location_name": "Atlanta, GA",
        "job_title": "Registered Nurse", "roles": ["Registered Nurse"],
        "provider_person_id": "1234567890", "provider": "nppes",
        "confidence": 1, "matched_inputs": ["name", "region"],
    }])
    calls = []

    def fake_call(name, location, companies=None, schools=None):
        calls.append((name, location))
        response = httpx.Response(
            200, headers={"X-Call-Credits-Spent": "1"},
            request=httpx.Request("GET", "https://api.peopledatalabs.com/v5/person/enrich"),
        )
        person = _pdl_person("Jane Doe", location)
        person["id"] = "pdl-jane-doe"
        return response, {
            "likelihood": 9, "matched": ["name", "location"], "data": person,
        }

    monkeypatch.setattr(pdl_client, "_call", fake_call)
    candidate_id = store.add_candidate(
        "Jane D", "Atlanta, GA", notes="Role: Registered Nurse", source="vivian",
    )
    result = pdl_client.enrich_candidates([candidate_id], "masked_12345678")["results"][candidate_id]
    assert result["status"] == "success"
    assert calls == [("Jane Doe", "Atlanta, GA")]
    stored = store.get_candidate(candidate_id)
    assert stored["canonical_name"] == "Jane Doe"
    assert stored["identity_status"] == "verified"
    assert stored["provider_person_id"] == "pdl-jane-doe"
    assert stored["identity_evidence"]["upstream_resolution"]["resolution_attempts"]


def test_repeated_pdl_verification_does_not_recursively_embed_itself(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", notes="Role: Registered Nurse", source="indeed",
    )
    candidate = store.get_candidate(candidate_id)
    first = _pdl_person("Jane Doe", "Atlanta, GA")
    first["id"] = "pdl-jane-doe"
    first_result = pdl_client._build_result(
        candidate, first, 9, ["name", "location"],
    )
    saved = enrich.save_provider_result(candidate_id, first_result)
    assert saved["enrich_status"] == "success"

    current = store.get_candidate(candidate_id)
    second_result = pdl_client._build_result(
        current, first, 9, ["name", "location"],
    )
    replayed = enrich.save_provider_result(candidate_id, second_result)
    assert replayed["enrich_status"] == "success"
    evidence = store.get_candidate(candidate_id)["identity_evidence"]
    assert "upstream_resolution" not in evidence


def test_pdl_batch_dedupes_identities_and_falls_back_without_bulk(monkeypatch):
    store.reset()
    _enable_pdl(monkeypatch, run_credit_limit=5)
    single_calls = []

    def bulk_unavailable(identities):
        return httpx.Response(
            404,
            request=httpx.Request("POST", "https://api.peopledatalabs.com/v5/person/bulk"),
        ), []

    def fake_call(name, location, companies=None, schools=None):
        single_calls.append((name, location))
        response = httpx.Response(
            200,
            headers={"X-Call-Credits-Spent": "1"},
            request=httpx.Request("GET", "https://api.peopledatalabs.com/v5/person/enrich"),
        )
        return response, {
            "likelihood": 9,
            "matched": ["name", "location"],
            "data": _pdl_person(name, location),
        }

    monkeypatch.setattr(pdl_client, "_bulk_call", bulk_unavailable)
    monkeypatch.setattr(pdl_client, "_call", fake_call)

    first = store.add_candidate("Alex Morgan", "Atlanta, GA", source="indeed")
    duplicate = store.add_candidate("Alex Morgan", "Atlanta, GA", source="indeed")
    other = store.add_candidate("Taylor Reed", "Austin, TX", source="indeed")
    batch = pdl_client.enrich_candidates([first, duplicate, other], "dedupe_12345678")

    # An account without bulk access still gets results, and the repeated
    # identity is purchased once for both cards.
    assert sorted(single_calls) == [("Alex Morgan", "Atlanta, GA"), ("Taylor Reed", "Austin, TX")]
    assert batch["credits_spent"] == 2
    assert batch["bulk"] is False
    assert batch["results"][first]["status"] == "success"
    assert batch["results"][duplicate]["status"] == "success"
    assert store.get_candidate(duplicate)["emails"] == store.get_candidate(first)["emails"]
    assert store.provider_run_credits("people_data_labs", "dedupe_12345678") == 2


def test_pdl_batch_skips_incomplete_identities_and_honors_dnc(monkeypatch):
    store.reset()
    _enable_pdl(monkeypatch, run_credit_limit=5)

    def fake_call(name, location, companies=None, schools=None):
        response = httpx.Response(
            200,
            headers={"X-Call-Credits-Spent": "1"},
            request=httpx.Request("GET", "https://api.peopledatalabs.com/v5/person/enrich"),
        )
        return response, {
            "likelihood": 9,
            "matched": ["name", "location"],
            "data": _pdl_person(name, location),
        }

    # Only one identity survives the guards, so the batch uses the cheaper
    # single-person endpoint rather than a one-item bulk request.
    monkeypatch.setattr(pdl_client, "_call", fake_call)
    store.add_dnc("alex.morgan@example.test", "opted out")
    suppressed = store.add_candidate("Alex Morgan", "Atlanta, GA", source="indeed")
    no_location = store.add_candidate("Casey Brooks", "", source="indeed")
    single_name = store.add_candidate("Robin", "Denver, CO", source="indeed")

    results = pdl_client.enrich_candidates(
        [suppressed, no_location, single_name], "guards_12345678",
    )["results"]
    assert results[no_location]["status"] == "skipped"
    assert results[single_name]["status"] == "skipped"
    # The only email was suppressed, so the phone alone still matches.
    assert "alex.morgan@example.test" not in store.get_candidate(suppressed)["emails"]
    assert results[suppressed]["status"] == "success"
    assert results[suppressed]["phones"]


def test_facebook_profile_uses_pdl_social_input_without_location(monkeypatch):
    store.reset()
    _enable_pdl(monkeypatch, run_credit_limit=2)
    calls = []

    def fake_call(name, location, companies=None, schools=None, profile=""):
        calls.append((name, location, profile))
        response = httpx.Response(
            200,
            headers={"X-Call-Credits-Spent": "1"},
            request=httpx.Request("GET", "https://api.peopledatalabs.com/v5/person/enrich"),
        )
        person = _pdl_person(name, "Portland, OR")
        person["facebook_url"] = "facebook.com/jane.doe.rn"
        return response, {
            "likelihood": 9,
            "matched": ["name", "profile"],
            "data": person,
        }

    monkeypatch.setattr(pdl_client, "_call", fake_call)
    candidate_id = store.add_candidate(
        "Jane Doe", "", notes="Role: Registered Nurse",
        source="facebook", source_url="https://www.facebook.com/jane.doe.rn",
        source_id="jane.doe.rn",
    )
    result = pdl_client.enrich_candidates(
        [candidate_id], "facebook_12345678",
    )["results"][candidate_id]

    assert calls == [(
        "Jane Doe", "", "https://www.facebook.com/jane.doe.rn",
    )]
    assert result["status"] == "success", result
    stored = store.get_candidate(candidate_id)
    assert stored["identity_status"] == "verified"
    assert stored["emails"] and stored["phones"]
    assert stored["verification"]["evidence"]["social_profile_match"] is True


def test_linkedin_profile_uses_pdl_social_input_with_country_only_location(monkeypatch):
    store.reset()
    _enable_pdl(monkeypatch, run_credit_limit=2)
    calls = []

    def fake_call(name, location, companies=None, schools=None, profile=""):
        calls.append((name, location, companies or [], schools or [], profile))
        response = httpx.Response(
            200,
            headers={"X-Call-Credits-Spent": "1"},
            request=httpx.Request("GET", "https://api.peopledatalabs.com/v5/person/enrich"),
        )
        person = _pdl_person(name, "Portland, OR")
        person.update({
            "linkedin_url": "linkedin.com/in/avinash-patel-b902343a4",
            "job_title": "Registered Nurse",
            "job_company_name": "Dr. Amit Dorkar Multispeciality Hospital",
        })
        return response, {
            "likelihood": 9,
            "matched": ["name", "profile"],
            "data": person,
        }

    monkeypatch.setattr(pdl_client, "_call", fake_call)
    candidate_id = store.add_candidate(
        "Avinash Patel", "India",
        notes=(
            "Headline: Registered Nurse at Dr. Amit Dorkar Multispeciality Hospital\n"
            "Employer: Dr. Amit Dorkar Multispeciality Hospital\n"
            "School: Gujarat University"
        ),
        source="linkedin",
        source_url="https://www.linkedin.com/in/avinash-patel-b902343a4/",
        source_id="avinash-patel-b902343a4",
    )
    result = pdl_client.enrich_candidates(
        [candidate_id], "linkedin_12345678",
    )["results"][candidate_id]

    assert calls == [(
        "Avinash Patel", "India",
        ["Dr. Amit Dorkar Multispeciality Hospital"],
        ["Gujarat University"],
        "https://www.linkedin.com/in/avinash-patel-b902343a4",
    )]
    assert result["status"] == "success", result
    assert result["verification"]["evidence"]["social_profile_match"] is True


def test_pdl_social_profile_normalization_rejects_non_profile_routes():
    assert pdl_client._social_profile({
        "source": "facebook",
        "source_url": "https://www.facebook.com/profile.php?id=12345&sk=about",
    }) == "https://www.facebook.com/profile.php?id=12345"
    assert pdl_client._social_profile({
        "source": "facebook", "source_url": "https://www.facebook.com/groups/nurses",
    }) == ""
    assert pdl_client._social_profile({
        "source": "facebook", "source_url": "https://www.facebook.com/search",
    }) == ""
    assert pdl_client._social_profile({
        "source": "linkedin", "source_url": "https://www.linkedin.com/in/jane-doe/",
    }) == "https://www.linkedin.com/in/jane-doe"


def test_pdl_batch_handles_provider_404_without_backend_error(monkeypatch):
    store.reset()
    _enable_pdl(monkeypatch, run_credit_limit=2)
    response = httpx.Response(
        404,
        headers={"X-Call-Credits-Spent": "0"},
        request=httpx.Request("GET", "https://api.peopledatalabs.com/v5/person/enrich"),
    )
    monkeypatch.setattr(
        pdl_client, "_call",
        lambda name, location, companies=None, schools=None: (
            response, {"status": 404},
        ),
    )
    candidate_id = store.add_candidate(
        "Ayana Smith", "Little Rock, AR", source="indeed",
    )

    batch = pdl_client.enrich_candidates([candidate_id], "notfound_12345678")
    result = batch["results"][candidate_id]

    assert result["status"] == "no_match"
    assert result["credits_spent"] == 0
    assert "usable-contact policy" in result["message"]
    assert result["reason_code"] == "pdl_required_contact_no_match"
    assert result["lookup_evidence"]["provider_queried"] is True
    assert result["lookup_evidence"]["location_supplied"] is True


def test_pdl_rejects_conflicting_returned_location(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "PDL_API_KEY", "server-only-test-key")
    monkeypatch.setattr(config, "PDL_ENABLED", True)
    monkeypatch.setattr(config, "PDL_RUN_CREDIT_LIMIT", 2)
    response = httpx.Response(
        200,
        headers={"X-Call-Credits-Spent": "1"},
        request=httpx.Request("GET", "https://api.peopledatalabs.com/v5/person/enrich"),
    )
    monkeypatch.setattr(
        pdl_client, "_call",
        lambda name, location, companies=None, schools=None: (response, {
            "likelihood": 10,
            "matched": ["name", "region"],
            "data": {
                "full_name": "Alex Morgan",
                "location_name": "Seattle, Washington, United States",
                "personal_emails": ["wrong-person@example.test"],
                "phone_numbers": ["(206) 555-0199"],
            },
        }),
    )
    candidate_id = store.add_candidate("Alex Morgan", "Atlanta, GA", source="indeed")
    result = pdl_client.enrich_candidate(candidate_id, "mismatch_123456")
    assert result["status"] == "no_match"
    assert result["credits_spent"] == 1
    stored = store.get_candidate(candidate_id)
    assert stored["emails"] == []
    assert stored["phones"] == []
    assert stored["identity_status"] == "rejected"
    assert "location" in stored["identity_evidence"]["conflicts"]


def test_pdl_normalizes_provider_contacts_without_local_identity_validation():
    candidate = {"name": "Alex Morgan", "location": "Atlanta, GA"}
    person = _pdl_person(candidate["name"], candidate["location"])

    missing_location = pdl_client._build_result(candidate, person, 9, ["name"])
    assert missing_location["status"] == "success"
    assert missing_location["confidence"] == 0.9

    confirmed = pdl_client._build_result(
        candidate, person, 9, ["name", "location"],
    )
    assert confirmed["status"] == "success"
    assert confirmed["matched_inputs"] == ["name", "location"]
    assert confirmed["likelihood"] == 9
    assert confirmed["provider_job_title"] == "Registered Nurse"
    assert confirmed["provider_company"] == "Example Medical Center"

    normalized_location = pdl_client._build_result(
        candidate, person, 9, ["name", "locality"],
    )
    assert normalized_location["status"] == "success"

    matched_school = pdl_client._build_result(
        candidate, person, 9, ["name", "school"],
    )
    assert matched_school["status"] == "success"

    moved_person = {
        **person,
        "location_name": "Charlotte, North Carolina, United States",
    }
    historical_locality = pdl_client._build_result(
        candidate, moved_person, 5, ["name", "locality"],
    )
    assert historical_locality["status"] == "success"

    strong_school = pdl_client._build_result(
        candidate, moved_person, 8, ["name", "school"],
    )
    assert strong_school["status"] == "success"

    weak_school = pdl_client._build_result(
        candidate, moved_person, 7, ["name", "school"],
    )
    assert weak_school["status"] == "success"

    state_only = pdl_client._build_result(
        candidate, moved_person, 9, ["name", "region"],
    )
    assert state_only["status"] == "success"

    no_match_evidence = pdl_client._build_result(candidate, moved_person, 2, [])
    assert no_match_evidence["status"] == "success"
    assert no_match_evidence["confidence"] == 0.2

    mobile_only = pdl_client._build_result(candidate, {
        "full_name": "Alex Morgan",
        "mobile_phone": "+14045550187",
        "phone_numbers": ["+14045550198"],
        "job_title": "Registered Nurse",
    }, 8, ["name"])
    assert mobile_only["status"] == "success"
    assert mobile_only["phones"] == ["+14045550187"]
    assert mobile_only["phone_policy"] == phone_policy.MOBILE_PHONE_POLICY
    assert mobile_only["phone_evidence"][0]["source_field"] == "mobile_phone"
    assert mobile_only["provider_job_title"] == "Registered Nurse"

    generic_only = pdl_client._build_result(candidate, {
        "full_name": "Alex Morgan",
        "phone_numbers": ["+14045550198"],
    }, 8, ["name"])
    assert generic_only["status"] == "success"
    assert generic_only["phones"] == ["+14045550198"]
    assert generic_only["phone_policy"] == phone_policy.OTHER_PHONE_POLICY


def test_pdl_requests_match_evidence_and_versions_cache_policy(monkeypatch):
    captured = {}

    class FakeClient:
        def get(self, url, **kwargs):
            captured["single"] = kwargs
            return httpx.Response(404, request=httpx.Request("GET", url))

        def post(self, url, **kwargs):
            captured["bulk"] = kwargs
            return httpx.Response(200, json=[], request=httpx.Request("POST", url))

    monkeypatch.setattr(pdl_client, "_client", lambda: FakeClient())
    monkeypatch.setattr(config, "PDL_MIN_LIKELIHOOD", 4)

    companies = ["Example Medical Center"]
    schools = ["Example College"]
    pdl_client._call("Alex Morgan", "Atlanta, GA", companies, schools)
    pdl_client._bulk_call([("Alex Morgan", "Atlanta, GA", companies, schools)])

    assert captured["single"]["params"]["min_likelihood"] == 4
    assert captured["single"]["params"]["include_if_matched"] == "true"
    expected_required = (
        "(mobile_phone OR phone_numbers OR recommended_personal_email OR personal_emails OR work_email)"
    )
    assert captured["single"]["params"]["required"] == expected_required
    assert "data_include" not in captured["single"]["params"]
    assert captured["single"]["params"]["company"] == companies
    assert captured["single"]["params"]["school"] == schools
    bulk_params = captured["bulk"]["json"]["requests"][0]["params"]
    assert captured["bulk"]["json"]["include_if_matched"] is True
    assert bulk_params["min_likelihood"] == 4
    assert bulk_params["include_if_matched"] is True
    assert bulk_params["required"] == expected_required
    assert "data_include" not in bulk_params
    assert bulk_params["company"] == companies
    assert bulk_params["school"] == schools

    candidate = {"name": "Alex Morgan", "location": "Atlanta, GA"}
    threshold_four_key = pdl_client._request_key(candidate)
    monkeypatch.setattr(config, "PDL_MIN_LIKELIHOOD", 6)
    assert pdl_client._request_key(candidate) != threshold_four_key


def test_pdl_extracts_only_structured_employer_and_school_signals():
    candidate = {
        "name": "Heather Godwin",
        "location": "Port Saint Lucie, FL",
        "notes": "\n".join((
            "Heather Godwin",
            "Port Saint Lucie, FL",
            "Registered Nurse",
            "Lawnwood Regional Medical Center",
            "(2017-Present)",
            "Registered Nurse (PRN)",
            "St. Lucie Medical Center",
            "(2019-2020)",
            "Associate of Science, Nursing",
            "Community College of Allegheny County",
            "RN",
        )),
    }
    companies, schools = pdl_client._profile_match_inputs(candidate)
    assert companies == [
        "Lawnwood Regional Medical Center", "St. Lucie Medical Center",
    ]
    assert schools == ["Community College of Allegheny County"]

    abbreviation_candidate = {
        "name": "Kelly Hensley",
        "location": "Macy, IN",
        "notes": "ASN, Nursing\nAncilla College",
    }
    _, abbreviation_schools = pdl_client._profile_match_inputs(abbreviation_candidate)
    assert abbreviation_schools == ["Ancilla College"]

    platform_candidate = {
        "name": "Jane Candidate",
        "location": "Portland, OR",
        "notes": "\n".join((
            "Role: Registered Nurse",
            "Employer: Example Medical Center",
            "Degree: Bachelor of Science, Nursing",
            "School: Example College",
            "Skills: ICU, telemetry",
        )),
    }
    companies, schools = pdl_client._profile_match_inputs(platform_candidate)
    assert companies == ["Example Medical Center"]
    assert schools == ["Example College"]


def test_resume_contact_sheet_preserves_source_pages():
    from pypdf import PdfReader, PdfWriter

    source = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(source)
    enriched, embedded = resume_enrichment.add_contact_sheet(source.getvalue(), {
        "id": 42,
        "name": "Alex Morgan",
        "location": "Atlanta, GA",
        "emails": ["alex.morgan@example.test"],
        "phones": ["(404) 555-0187"],
        "confidence": 0.94,
        "verification": {"source": "people_data_labs"},
    })

    reader = PdfReader(BytesIO(enriched))
    contact_text = reader.pages[0].extract_text()
    assert embedded is True
    assert len(reader.pages) == 2
    assert "Alex Morgan" in contact_text
    assert "alex.morgan@example.test" in contact_text
    assert "(404) 555-0187" in contact_text
    assert reader.metadata["/RadixsolCandidateId"] == "42"
    repeated, repeated_embedded = resume_enrichment.add_contact_sheet(source.getvalue(), {
        "id": 42,
        "name": "Alex Morgan",
        "location": "Atlanta, GA",
        "emails": ["alex.morgan@example.test"],
        "phones": ["(404) 555-0187"],
        "confidence": 0.94,
        "verification": {"source": "people_data_labs"},
    })
    assert repeated_embedded is True
    assert repeated == enriched


def test_resume_contact_sheet_contains_only_latest_trusted_phone():
    from pypdf import PdfReader, PdfWriter

    older = "(404) 555-0171"
    latest = "(404) 555-0172"
    source = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(source)
    enriched, embedded = resume_enrichment.add_contact_sheet(source.getvalue(), {
        "id": 43,
        "name": "Alex Morgan",
        "emails": ["alex.morgan@example.test"],
        "phones": [older, latest],
        "phone_evidence": [
            {
                "value": older, "type": "mobile", "source_field": "mobile_phone",
                "source_fields": ["mobile_phone"], "mobile_or_wireless": True,
                "accepted": True, "last_seen": "2022-04-01",
            },
            {
                "value": latest, "type": "mobile", "source_field": "mobile_phone",
                "source_fields": ["mobile_phone"], "mobile_or_wireless": True,
                "accepted": True, "last_seen": "2026-04-01",
            },
        ],
    })

    reader = PdfReader(BytesIO(enriched))
    contact_text = reader.pages[0].extract_text()
    assert embedded is True
    assert latest in contact_text
    assert older not in contact_text
    assert reader.metadata["/RadixsolPhones"] == latest


def test_resume_contact_sheet_refreshes_without_stacking_or_stale_contacts():
    from pypdf import PdfReader, PdfWriter

    source = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(source)
    old_phone = "(404) 555-0101"
    new_phone = "(404) 555-0102"
    old_pdf, embedded = resume_enrichment.add_contact_sheet(source.getvalue(), {
        "id": 99,
        "name": "Alex Morgan",
        "emails": ["alex@example.test"],
        "phones": [old_phone],
    })
    assert embedded is True

    refreshed, refreshed_embedded = resume_enrichment.refresh_contact_sheet(old_pdf, {
        "id": 99,
        "name": "Alex Morgan",
        "emails": ["alex@example.test"],
        "phones": [new_phone],
    })
    refreshed_reader = PdfReader(BytesIO(refreshed))
    refreshed_text = refreshed_reader.pages[0].extract_text()
    assert refreshed_embedded is True
    assert len(refreshed_reader.pages) == 2
    assert new_phone in refreshed_text
    assert old_phone not in refreshed_text
    assert refreshed_reader.metadata["/RadixsolPhones"] == new_phone

    stripped, stripped_embedded = resume_enrichment.refresh_contact_sheet(
        refreshed, {"id": 99, "name": "Alex Morgan", "emails": [], "phones": []},
    )
    stripped_reader = PdfReader(BytesIO(stripped))
    assert stripped_embedded is False
    assert len(stripped_reader.pages) == 1
    assert stripped_reader.metadata.get("/RadixsolPhones") is None


def test_marked_resume_refresh_failure_is_fail_closed():
    import pytest

    unsafe = b"%PDF-broken /RadixsolCandidateId Medhunt Sourcing Assistant"
    with pytest.raises(resume_enrichment.ContactSheetRefreshError):
        resume_enrichment.refresh_contact_sheet(
            unsafe,
            {"id": 99, "name": "Alex Morgan", "emails": [], "phones": []},
        )


def test_public_lookup_result_is_strict_and_fail_closed():
    accepted_mobile = {
        "value": "(404) 555-0100", "type": "mobile",
        "source_field": "mobile_phone", "source_fields": ["mobile_phone"],
        "mobile_or_wireless": True, "is_connected": None, "accepted": True,
    }
    hidden = {
        "provider": "internal-provider",
        "confidence": 0.99,
        "likelihood": 10,
        "credits_spent": 1,
        "addresses": ["1 Hidden Street"],
        "identity_status": "verified",
        "error": "raw provider diagnostic",
    }
    trusted = api_module._public_lookup_result({
        **hidden,
        "status": "success",
        "contacts_trusted": True,
        "emails": ["jane@example.test"],
        "phones": ["(404) 555-0100"],
        "phone_evidence": [accepted_mobile],
    })
    assert trusted == {
        "status": "found",
        "emails": ["jane@example.test"],
        "phones": ["(404) 555-0100"],
        "phone_contacts": [{"value": "(404) 555-0100", "kind": "mobile"}],
        "resume_required": True,
        "location_match": None,
    }

    hometown = api_module._public_lookup_result({
        **hidden,
        "status": "success",
        "contacts_trusted": True,
        "emails": ["jane@example.test"],
        "phones": ["(404) 555-0100"],
        "phone_evidence": [accepted_mobile],
        "matched_location_context": {
            "type": "hometown", "value": "Wichita, KS",
        },
    })
    assert hometown["location_match"] == {
        "type": "hometown", "value": "Wichita, KS",
    }

    untrusted = api_module._public_lookup_result({
        **hidden,
        "status": "success",
        "emails": ["wrong@example.test"],
        "phones": ["(404) 555-0199"],
        "matched_location_context": {
            "type": "hometown", "value": "Untrusted, XX",
        },
    })
    assert untrusted == {
        "status": "not_found", "emails": [], "phones": [],
        "phone_contacts": [],
        "resume_required": False, "location_match": None,
    }

    trusted_but_untyped = api_module._public_lookup_result({
        **hidden,
        "status": "success",
        "contacts_trusted": True,
        "phones": ["(404) 555-0199"],
    })
    assert trusted_but_untyped == {
        "status": "not_found", "emails": [], "phones": [],
        "phone_contacts": [],
        "resume_required": False, "location_match": None,
    }

    email_only = api_module._public_lookup_result({
        **hidden,
        "status": "success",
        "verification": {"evidence": {"contact_verification": {
            "automatic_use_allowed": True,
        }}},
        "emails": ["jane@example.test"],
        "phones": [],
    })
    assert email_only == {
        "status": "found", "emails": ["jane@example.test"], "phones": [],
        "phone_contacts": [],
        "resume_required": True, "location_match": None,
    }

    other_phone = api_module._public_lookup_result({
        **hidden,
        "status": "success",
        "contacts_trusted": True,
        "phones": ["(404) 555-0188"],
        "phone_evidence": [{
            "value": "(404) 555-0188", "type": "landline",
            "source_field": "phone_numbers", "accepted": False,
        }],
    })
    assert other_phone == {
        "status": "found", "emails": [], "phones": ["(404) 555-0188"],
        "phone_contacts": [{"value": "(404) 555-0188", "kind": "other"}],
        "resume_required": True, "location_match": None,
    }

    failed = api_module._public_lookup_result({**hidden, "status": "error"})
    assert failed == {
        "status": "failed", "emails": [], "phones": [],
        "phone_contacts": [],
        "resume_required": False, "location_match": None,
    }
    serialized = json.dumps([
        trusted, hometown, untrusted, trusted_but_untyped, email_only,
        other_phone, failed,
    ]).casefold()
    assert not any(term in serialized for term in (
        "provider", "confidence", "likelihood", "credit", "identity",
        "address", "diagnostic", "evidence", "policy",
    ))


def test_facebook_context_is_preserved_for_quick_sourcer(monkeypatch):
    store.reset()
    row = api_module._profile_row(api_module.ProfileImportIn(
        name="Elizabeth Cruz (Liz)",
        location="Virginia Beach, Virginia",
        hometown="Wichita, Kansas",
        source="facebook",
        source_url="https://www.facebook.com/elizabeth.cruz",
        source_id="elizabeth.cruz",
    ))
    assert row["name"] == "Elizabeth Cruz"
    assert row["location"] == "Virginia Beach, Virginia"
    assert "Facebook hometown: Wichita, Kansas" in row["notes"]
    candidate_id = store.add_candidate(**row)
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
        candidate_ids=[candidate_id], run_id="hometown_12345678", confirmed=True,
    ))
    result = response["results"][str(candidate_id)]
    assert calls == [("Virginia Beach, Virginia", "Wichita, Kansas")]
    assert result["status"] == "not_found"
    stored = store.get_candidate(candidate_id)
    assert stored["location"] == "Virginia Beach, Virginia"
    assert stored["hometown"] == "Wichita, Kansas"


def test_hometown_retry_gate_and_location_cache_identity():
    definitive = {
        "status": "no_match",
        "reason_code": "pdl_required_mobile_no_match",
        "emails": [], "phones": [],
    }
    assert api_module._hometown_retry_allowed(definitive)
    assert not api_module._hometown_retry_allowed({
        "status": "error", "error": "timeout",
    })
    assert not api_module._hometown_retry_allowed({
        **definitive,
        "contact_verification": {
            "status": "fallback_no_match",
            "enformion_selection": {"selection": "ambiguous_exact_matches"},
        },
    })

    candidate = {
        "id": 91,
        "name": "Elizabeth Cruz",
        "location": "Virginia Beach, Virginia",
        "source": "facebook",
        "source_url": "https://www.facebook.com/elizabeth.cruz",
        "source_id": "elizabeth.cruz",
        "notes": "",
    }
    hometown_candidate = pdl_client._candidate_with_location_override(
        candidate, {91: "Wichita, Kansas"},
    )
    assert hometown_candidate["location"] == "Wichita, Kansas"
    assert pdl_client._request_key(candidate) != pdl_client._request_key(hometown_candidate)
    # Accepted hometown contacts remain bound to the durable source profile,
    # so the next lookup can reuse them without changing current location.
    assert trust_policy.source_identity_fingerprint(candidate) == (
        trust_policy.source_identity_fingerprint(hometown_candidate)
    )


def test_provider_waterfall_uses_transient_hometown_override(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate(
        "Elizabeth Cruz", "Virginia Beach, Virginia",
        source="facebook",
        source_url="https://www.facebook.com/elizabeth.cruz",
        source_id="elizabeth.cruz",
        hometown="Wichita, Kansas",
    )
    store.update_candidate(
        candidate_id,
        identity_status="verified",
        canonical_name="Elizabeth Cruz",
    )
    captured = {}

    class LookupSlot:
        def __enter__(self):
            return True

        def __exit__(self, *_args):
            return False

    def fake_run_batch(cids, run_id, *, location_overrides=None):
        captured["pdl_ids"] = list(cids)
        captured["pdl_run"] = run_id
        captured["pdl_overrides"] = dict(location_overrides or {})
        return {
            "results": {candidate_id: definitive.copy()},
            "credits_spent": 0,
            "run_credits_spent": 0,
        }

    definitive = {
        "status": "no_match",
        "reason_code": "pdl_required_mobile_no_match",
        "emails": [], "phones": [],
    }
    monkeypatch.setattr(pdl_client, "configured", lambda: True)
    monkeypatch.setattr(pdl_client, "_lookup_slot", lambda: LookupSlot())
    monkeypatch.setattr(pdl_client, "_run_batch", fake_run_batch)
    pdl_client.enrich_candidates(
        [candidate_id], "provider_override_12345678",
        location_overrides={candidate_id: "Wichita, Kansas"},
    )
    assert captured["pdl_overrides"] == {candidate_id: "Wichita, Kansas"}

    original_request = multi_provider._request

    def capture_request(candidate, reason):
        captured["ef_location"] = candidate["location"]
        captured["ef_source_location"] = candidate["_source_identity_location"]
        # Returning None proves the plumbing without making any provider call.
        return None

    monkeypatch.setattr(config, "ENFORMION_VERIFY_PDL", True)
    monkeypatch.setattr(config, "ENFORMION_FALLBACK_ONLY", True)
    monkeypatch.setattr(multi_provider, "_request", capture_request)
    multi_provider.verify_batch(
        [candidate_id], {candidate_id: definitive}, "provider_override_12345678",
        location_overrides={candidate_id: "Wichita, Kansas"},
    )
    assert captured["ef_location"] == "Wichita, Kansas"
    assert captured["ef_source_location"] == "Virginia Beach, Virginia"
    assert store.get_candidate(candidate_id)["location"] == "Virginia Beach, Virginia"


def test_local_api_token_protects_cross_origin_api_access(monkeypatch):
    store.reset()
    token = "test-local-api-token-with-enough-entropy"
    monkeypatch.setattr(config, "LOCAL_API_TOKEN", token)
    candidate_id = store.add_candidate("Token Test", "Atlanta, GA", source="indeed")
    resume = store.attach_resume(
        candidate_id, "token-test.pdf", b"%PDF-1.4 local token test"
    )
    extension_origin = f"chrome-extension://{'a' * 32}"

    async def exercise_gate():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8091"
        ) as client:
            # Health and browser assets stay public so setup can probe readiness.
            assert (
                await client.get("/health", headers={"Origin": extension_origin})
            ).status_code == 200
            assert (
                await client.get("/session", headers={"Origin": extension_origin})
            ).status_code == 401

            # An arbitrary extension or website cannot read PII or trigger a
            # mutation merely because the service is bound to loopback.
            assert (
                await client.get("/candidates", headers={"Origin": extension_origin})
            ).status_code == 401
            assert (
                await client.post(
                    "/jobs",
                    headers={"Origin": "https://attacker.example"},
                    json={"title": "must not be created"},
                )
            ).status_code == 401

            authorized = await client.get(
                "/candidates",
                headers={
                    "Origin": extension_origin,
                    "X-Medhunt-Token": token,
                },
            )
            assert authorized.status_code == 200
            assert authorized.json()[0]["name"] == "Token Test"
            authorized_session = await client.get(
                "/session",
                headers={
                    "Origin": extension_origin,
                    "X-Medhunt-Token": token,
                },
            )
            assert authorized_session.status_code == 200
            assert authorized_session.json() == {"status": "ok"}
            authorized_mutation = await client.post(
                "/jobs",
                headers={
                    "Origin": extension_origin,
                    "X-Medhunt-Token": token,
                },
                json={"title": "Authenticated extension job"},
            )
            assert authorized_mutation.status_code == 200

            # The backend-hosted UI remains usable without embedding its token,
            # and direct CLI/download requests intentionally carry no Origin.
            same_origin = await client.post(
                "/jobs",
                headers={"Origin": "http://127.0.0.1:8091"},
                json={"title": "Same-origin job"},
            )
            assert same_origin.status_code == 200
            direct = await client.get(
                f"/candidates/{candidate_id}/resumes/{resume['id']}"
            )
            assert direct.status_code == 200
            assert direct.content.startswith(b"%PDF")

            # A different loopback origin is still cross-origin and must not be
            # confused with the exact origin serving the web UI.
            assert (
                await client.get(
                    "/candidates", headers={"Origin": "http://localhost:8091"}
                )
            ).status_code == 401

            preflight = await client.options(
                "/candidates",
                headers={
                    "Origin": extension_origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "X-Medhunt-Token",
                },
            )
            assert preflight.status_code == 200

    asyncio.run(exercise_gate())


def test_hosted_extension_origin_gate_fails_closed(monkeypatch):
    extension_origin = f"chrome-extension://{'b' * 32}"
    monkeypatch.setattr(config, "EXTENSION_ALLOW_UNLISTED_ORIGINS", False)
    monkeypatch.setattr(config, "EXTENSION_ALLOWED_ORIGINS", ())

    async def exercise_gate():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://medhunt.test"
        ) as client:
            blocked = await client.get(
                "/health", headers={"Origin": extension_origin}
            )
            assert blocked.status_code == 403
            assert blocked.json()["detail"] == (
                "This extension installation is not authorized."
            )

            monkeypatch.setattr(
                config, "EXTENSION_ALLOWED_ORIGINS", (extension_origin,)
            )
            allowed = await client.get(
                "/health", headers={"Origin": extension_origin}
            )
            assert allowed.status_code == 200

    asyncio.run(exercise_gate())
    titles = {job["title"] for job in store.list_jobs()}
    assert titles == {"Authenticated extension job", "Same-origin job"}


def test_api_workflow_and_extension_cors(monkeypatch):
    store.reset()

    def fake_quick_lookup(candidate_id):
        candidate = store.get_candidate(candidate_id)
        return quick_sourcer_client.apply_to_candidate(candidate_id, {
            "status": "found",
            "name": candidate["name"],
            "source": "test fixture",
            "emails": [f"candidate{candidate_id}@example.test"],
            "phones": [{"value": "(404) 555-0105", "type": "Wireless"}],
            "addresses": [candidate.get("location") or "Atlanta, GA"],
        })

    monkeypatch.setattr(quick_sourcer_client, "lookup_candidate", fake_quick_lookup)

    async def exercise_api():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            assert health.status_code == 200
            health_body = health.json()
            # Browser-facing capability fields stay vendor-neutral and never
            # disclose credentials or backend endpoints.
            assert set(health_body) == {
                "status", "service", "version", "records_lookup",
                "lookup_behavior",
            }
            assert health_body["status"] == "ok"
            assert health_body["service"] == "medhunt-api"
            assert health_body["version"] == api_module.APP_VERSION
            assert set(health_body["records_lookup"]) == {
                "enabled", "typical_seconds",
            }
            assert health_body["lookup_behavior"] == "sequential"

            job_response = await client.post("/jobs", json={
                "title": "Nurse",
                "location": "Atlanta, GA",
                "description": "registered nurse healthcare",
            })
            assert job_response.status_code == 200
            job_id = job_response.json()["id"]

            intake_response = await client.post("/candidates/intake", json={
                "text": "Jane Doe, Atlanta, GA",
                "job_id": job_id,
            })
            assert intake_response.status_code == 200
            candidate_id = intake_response.json()["ids"][0]
            single_lookup = await client.post(
                f"/candidates/{candidate_id}/contact-lookup"
            )
            assert single_lookup.status_code == 200
            assert set(single_lookup.json()) == {
                "status", "emails", "phones", "phone_contacts",
                "resume_required", "location_match", "ats_destination",
            }
            assert (await client.post(f"/jobs/{job_id}/rank")).json()["ranked"] == 1

            draft = await client.post("/outreach/draft", json={
                "candidate_id": candidate_id,
                "job_id": job_id,
            })
            assert draft.status_code == 200
            outreach_id = draft.json()["outreach_id"]
            approved = await client.post(f"/outreach/{outreach_id}/approve")
            assert approved.json()["status"] == "approved"
            assert (await client.post("/outreach/999999/approve")).status_code == 404

            indeed_profile = {
                "name": "Alex Morgan",
                "location": "Atlanta, GA",
                "headline": "Registered Nurse",
                "notes": "Registered Nurse\nEmergency care\nBLS certification",
                "source": "indeed",
                "source_url": "https://employers.indeed.com/smartsourcing?candidateId=abc123",
                "source_id": "abc123",
                "job_id": job_id,
            }
            first_import = await client.post("/candidates/import", json=indeed_profile)
            assert first_import.status_code == 200
            assert first_import.json()["imported"] is True
            imported_id = first_import.json()["id"]

            # The batch lookup answers every selected candidate in one call and
            # reports per-candidate results keyed by candidate id.
            batch = await client.post("/contact-lookup/batch", json={
                "candidate_ids": [imported_id, candidate_id],
                "run_id": "apibatch_12345678",
                "confirmed": True,
            })
            assert batch.status_code == 200
            batch_body = batch.json()
            assert batch_body["status"] == "ok"
            assert set(batch_body["results"]) == {str(imported_id), str(candidate_id)}
            assert batch_body["results"][str(imported_id)]["status"] == "found"
            assert all(
                set(result) == {
                    "status", "emails", "phones", "phone_contacts",
                    "resume_required", "location_match", "ats_destination",
                }
                for result in batch_body["results"].values()
            )
            assert set(batch_body) == {"status", "results", "processed", "matched"}
            assert not any(
                term in json.dumps(batch_body).casefold()
                for term in ("pdl", "people_data", "enformion", "provider", "credit", "likelihood")
            )
            assert (await client.post("/contact-lookup/batch", json={
                "candidate_ids": [], "run_id": "apibatch_12345678",
            })).status_code == 422
            assert (await client.post("/contact-lookup/batch", json={
                "candidate_ids": [imported_id], "run_id": "short",
            })).status_code == 422

            assert "source" not in first_import.json()["candidate"]
            assert "BLS certification" in first_import.json()["candidate"]["notes"]
            assert (await client.post(f"/candidates/{imported_id}/contact-lookup")).status_code == 200
            resume_dir = Path(tempfile.mkdtemp())
            resume_path = resume_dir / "Alex-Morgan-resume.pdf"
            resume_path.write_bytes(b"%PDF-1.4 test resume")
            original_resume_dir = config.RESUME_DOWNLOAD_DIR
            config.RESUME_DOWNLOAD_DIR = resume_dir.resolve()
            try:
                attached = await client.post(
                    f"/candidates/{imported_id}/resume/from-download",
                    json={"path": str(resume_path), "filename": resume_path.name},
                )
                assert attached.status_code == 200
                public_resume = attached.json()["resume"]
                resume_id = public_resume["id"]
                assert not {
                    "object_key", "bucket", "public_url", "checksum_sha256",
                    "nexus_sync_status", "storage_provider",
                } & set(public_resume)
                downloaded = await client.get(
                    f"/candidates/{imported_id}/resumes/{resume_id}"
                )
                assert downloaded.status_code == 200
                assert downloaded.content.startswith(b"%PDF")
            finally:
                config.RESUME_DOWNLOAD_DIR = original_resume_dir

            captured = await client.post(
                f"/candidates/{imported_id}/resume/from-browser",
                json={
                    "content_base64": base64.b64encode(
                        b"leading bytes%PDF-1.4 browser-captured resume"
                    ).decode("ascii"),
                    "filename": "browser-captured-resume.pdf",
                },
            )
            assert captured.status_code == 200
            captured_resume_id = captured.json()["resume"]["id"]
            captured_download = await client.get(
                f"/candidates/{imported_id}/resumes/{captured_resume_id}"
            )
            assert captured_download.status_code == 200
            assert captured_download.content.startswith(b"%PDF")

            duplicate_capture = await client.post(
                f"/candidates/{imported_id}/resume/from-browser",
                json={
                    "content_base64": base64.b64encode(
                        b"leading bytes%PDF-1.4 browser-captured resume"
                    ).decode("ascii"),
                    "filename": "browser-captured-resume.pdf",
                },
            )
            assert duplicate_capture.status_code == 200
            assert duplicate_capture.json()["resume"]["id"] == captured_resume_id
            assert duplicate_capture.json()["resume"]["deduplicated"] is True
            candidate_view = await client.get(f"/candidates/{imported_id}")
            assert candidate_view.status_code == 200
            assert all(
                not {
                    "object_key", "bucket", "public_url", "checksum_sha256",
                    "nexus_sync_status", "storage_provider",
                } & set(item)
                for item in candidate_view.json()["resumes"]
            )

            second_import = await client.post("/candidates/import", json=indeed_profile)
            assert second_import.status_code == 200
            assert second_import.json()["imported"] is False
            assert second_import.json()["id"] == imported_id

            fuller_name = await client.post("/candidates/import", json={
                **indeed_profile,
                "name": "Alex Jordan Morgan",
            })
            assert fuller_name.status_code == 200
            assert fuller_name.json()["id"] == imported_id
            assert fuller_name.json()["candidate"]["name"] == "Alex Jordan Morgan"

            batch_profile = {
                **indeed_profile,
                "name": "Taylor Reed",
                "source_id": "xyz789",
                "source_url": "https://employers.indeed.com/smartsourcing?candidateId=xyz789",
            }
            batch = await client.post("/candidates/import/batch", json={
                "profiles": [indeed_profile, batch_profile],
                "job_id": job_id,
                "search_url": "https://employers.indeed.com/smartsourcing",
            })
            assert batch.status_code == 200
            assert batch.json()["saved"] == 2
            assert batch.json()["imported"] == 1
            assert batch.json()["existing"] == 1
            assert batch.json()["database"] == "sqlite"

            directory_batch = await client.post("/candidates/import/batch", json={
                "profiles": [
                    {
                        "name": "Clara Zee", "location": "The Woodlands, TX",
                        "headline": "Family Medicine", "roles": ["Physician"],
                        "specialties": ["Family Medicine", "Primary Care"],
                        "source": "commonspirit",
                        "source_url": "https://www.commonspirit.org/find-a-doctor/clara-zee-1407550627",
                        "source_id": "1407550627",
                    },
                    {
                        "name": "Raja Flores", "location": "New York, NY",
                        "headline": "Cardiothoracic Surgery", "roles": ["Physician"],
                        "specialties": ["Cardiothoracic Surgery"],
                        "source": "sharecare",
                        "source_url": "https://providers.sharecare.com/doctor/dr-raja-flores",
                        "source_id": "1306821244",
                    },
                ],
                "search_url": "https://providers.sharecare.com/find-a-doctor/specialty/cardiothoracic-surgery",
            })
            assert directory_batch.status_code == 200, directory_batch.text
            directory_profiles = directory_batch.json()["results"]
            assert directory_batch.json()["saved"] == 2
            assert [store.get_candidate(item["id"])["source"] for item in directory_profiles] == [
                "commonspirit", "sharecare",
            ]
            assert "Specialty: Cardiothoracic Surgery" in directory_profiles[1]["candidate"]["notes"]

            preflight = await client.options("/health", headers={
                "Origin": f"chrome-extension://{'a' * 32}",
                "Access-Control-Request-Method": "GET",
            })
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"].startswith("chrome-extension://")

    asyncio.run(exercise_api())


def test_usnews_professional_profile_resume_is_generated_stored_and_deduplicated():
    from pypdf import PdfReader

    store.reset()
    candidate_id = store.add_candidate(
        "Gianpiero D. Palermo", "New York, NY", source="usnews",
        source_url="https://health.usnews.com/doctors/gianpiero-palermo-766542",
        source_id="1750648267",
    )
    body = {
        "kind": "public_professional_profile",
        "source_label": "U.S. News Doctor Finder",
        "source_url": "https://health.usnews.com/doctors/gianpiero-palermo-766542",
        "headline": "Obstetrics & Gynecology — Reproductive Endocrinology & Infertility",
        "summary": "Public professional overview for Dr. Palermo.",
        "credentials": ["MD"],
        "specialties": ["Obstetrics & Gynecology"],
        "subspecialties": ["Reproductive Endocrinology & Infertility"],
        "hospitals": ["NewYork-Presbyterian Hospital-Columbia and Cornell"],
        "education": [
            "New York Presbyterian Hospital — Residency, Obstetrics and Gynecology, 1999-2002",
            "University of Bari Faculty of Medicine — Medical School",
        ],
        "certifications": [
            "American Board of Obstetrics and Gynecology — Certified in Obstetrics & Gynecology"
        ],
        "licenses": ["NY State Medical License — Active through 2027"],
        "languages": ["English"],
        "years_experience": "21+",
        "npi": "1750648267",
        "address": "1305 York Ave, New York, NY 10021",
        "location": "New York, NY",
    }

    async def exercise():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                f"/candidates/{candidate_id}/professional-profile-resume", json=body,
            )
            assert first.status_code == 200, first.text
            payload = first.json()
            assert payload["attached"] is True
            assert payload["document_type"] == "public_professional_profile"
            assert payload["resume"]["filename"].endswith(".pdf")
            resume_id = payload["resume"]["id"]
            assert "Specialty: Obstetrics & Gynecology" in store.get_candidate(candidate_id)["notes"]

            downloaded = await client.get(
                f"/candidates/{candidate_id}/resumes/{resume_id}",
            )
            assert downloaded.status_code == 200
            reader = PdfReader(BytesIO(downloaded.content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            assert "Gianpiero D. Palermo" in text
            assert "HOSPITAL AFFILIATIONS" in text
            assert "EDUCATION & TRAINING" in text
            assert "MEDICAL LICENSURE" in text
            assert "not candidate-authored" in text
            assert "1750648267" in text

            duplicate = await client.post(
                f"/candidates/{candidate_id}/professional-profile-resume", json=body,
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["resume"]["id"] == resume_id
            assert duplicate.json()["resume"]["deduplicated"] is True
            assert len(store.list_resumes(candidate_id)) == 1

            medifind_id = store.add_candidate(
                "Brian E. Louie", "Seattle, WA", source="medifind",
                source_url="https://www.medifind.com/doctors/brian-e-louie/10650877",
                source_id="10650877",
            )
            medifind_body = {
                **body,
                "source_label": "MediFind",
                "source_url": "https://www.medifind.com/doctors/brian-e-louie/10650877",
                "headline": "Thoracic Surgery",
                "summary": "Public MediFind professional overview.",
                "specialties": ["Thoracic Surgery"],
                "subspecialties": [],
                "hospitals": ["Swedish Medical Center"],
                "education": [],
                "licenses": [],
                "certifications": ["Board certified in American Board Of Surgery"],
                "npi": "",
                "address": "1101 Madison Street, Suite 900, Seattle, WA 98104",
                "location": "Seattle, WA",
            }
            medifind_resume = await client.post(
                f"/candidates/{medifind_id}/professional-profile-resume",
                json=medifind_body,
            )
            assert medifind_resume.status_code == 200, medifind_resume.text
            assert medifind_resume.json()["resume"]["filename"].endswith(".pdf")

            commonspirit_id = store.add_candidate(
                "Clara Zee", "The Woodlands, TX", source="commonspirit",
                source_url="https://www.commonspirit.org/find-a-doctor/clara-zee-1407550627",
                source_id="1407550627",
            )
            commonspirit_body = {
                **medifind_body,
                "source_label": "CommonSpirit Health",
                "source_url": "https://www.commonspirit.org/find-a-doctor/clara-zee-1407550627",
                "headline": "Family Medicine",
                "summary": "Public CommonSpirit provider overview.",
                "specialties": ["Family Medicine", "Primary Care"],
                "hospitals": ["Baylor St. Luke's Medical Group"],
                "npi": "1407550627",
                "address": "6769 Lake Woodlands Drive, Suite E, The Woodlands, TX 77382",
                "location": "The Woodlands, TX",
            }
            commonspirit_resume = await client.post(
                f"/candidates/{commonspirit_id}/professional-profile-resume",
                json=commonspirit_body,
            )
            assert commonspirit_resume.status_code == 200, commonspirit_resume.text
            assert commonspirit_resume.json()["resume"]["filename"].endswith(".pdf")

            sharecare_id = store.add_candidate(
                "Raja Flores", "New York, NY", source="sharecare",
                source_url="https://providers.sharecare.com/doctor/dr-raja-flores",
                source_id="1306821244",
            )
            sharecare_body = {
                **medifind_body,
                "source_label": "Sharecare",
                "source_url": "https://providers.sharecare.com/doctor/dr-raja-flores",
                "headline": "Cardiothoracic Surgery",
                "summary": "Public Sharecare professional overview.",
                "specialties": ["Cardiothoracic Surgery"],
                "hospitals": ["Mount Sinai Morningside"],
                "education": ["Albert Einstein College of Medicine"],
                "certifications": ["American Board of Thoracic Surgery"],
                "licenses": ["New York State Medical License"],
                "npi": "1306821244",
                "address": "1470 Madison Ave, New York, NY 10029",
                "location": "New York, NY",
            }
            sharecare_resume = await client.post(
                f"/candidates/{sharecare_id}/professional-profile-resume",
                json=sharecare_body,
            )
            assert sharecare_resume.status_code == 200, sharecare_resume.text

            npino_id = store.add_candidate(
                "Ayumi E Belanger", "Green Cove Springs, FL", source="npino",
                source_url="https://npino.com/nurse/1003001058-ms.-ayumi-e-belanger/",
                source_id="1003001058",
            )
            npino_body = {
                **sharecare_body,
                "source_label": "NPI No.",
                "source_url": "https://npino.com/nurse/1003001058-ms.-ayumi-e-belanger/",
                "headline": "Physician Assistant - Medical",
                "summary": "Ayumi E Belanger is listed in the NPI registry with NPI 1003001058.",
                "specialties": ["Physician Assistant - Medical"],
                "hospitals": [], "education": [], "certifications": [],
                "licenses": ["PA12345"], "npi": "1003001058",
                "address": "", "location": "",
            }
            npino_resume = await client.post(
                f"/candidates/{npino_id}/professional-profile-resume", json=npino_body,
            )
            assert npino_resume.status_code == 200, npino_resume.text
            assert npino_resume.json()["resume"]["filename"].endswith(".pdf")
            assert sharecare_resume.json()["resume"]["filename"].endswith(".pdf")

            wrong_sharecare_url = await client.post(
                f"/candidates/{sharecare_id}/professional-profile-resume",
                json={**sharecare_body, "source_url": "https://sharecare.com/doctor/dr-raja-flores"},
            )
            assert wrong_sharecare_url.status_code == 400

            wrong_medifind_url = await client.post(
                f"/candidates/{medifind_id}/professional-profile-resume",
                json={**medifind_body, "source_url": "https://example.com/doctors/wrong/1"},
            )
            assert wrong_medifind_url.status_code == 400

            wrong_source = store.add_candidate("Wrong Source", "", source="indeed")
            rejected = await client.post(
                f"/candidates/{wrong_source}/professional-profile-resume", json=body,
            )
            assert rejected.status_code == 400

    asyncio.run(exercise())


def test_profile_resume_fingerprint_changes_with_public_credentials():
    candidate = {"name": "Jane Clinician", "location": "Boston, MA"}
    profile = {
        "source_url": "https://health.usnews.com/doctors/jane-clinician-123",
        "education": ["Example University — Medical School"],
    }
    first = profile_resume.fingerprint(candidate, profile)
    second = profile_resume.fingerprint(
        candidate, {**profile, "licenses": ["MA State Medical License — Active"]},
    )
    assert first != second
    assert profile_resume.filename(candidate, profile).endswith(f"{first[:12]}.pdf")


def test_cloud_resume_api_stores_r2_metadata(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Cloud Resume", "Atlanta, GA", source="indeed")
    resume_dir = Path(tempfile.mkdtemp()).resolve()
    resume_path = resume_dir / "cloud-resume.pdf"
    pdf = b"%PDF-1.4 cloud fixture"
    resume_path.write_bytes(pdf)

    monkeypatch.setattr(config, "RESUME_DOWNLOAD_DIR", resume_dir)
    monkeypatch.setattr(config, "STORAGE_ENABLED", True)
    monkeypatch.setattr(storage, "upload_resume", lambda cid, filename, data: {
        "storage_provider": "r2",
        "object_key": f"resumes/{cid}/fixture-{filename}",
        "bucket": "radixsol-test-resumes",
        "public_url": "",
        "checksum_sha256": "fixture-checksum",
        "etag": "fixture-etag",
    })
    monkeypatch.setattr(storage, "download_resume", lambda key: pdf)

    async def exercise_cloud_resume():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            attached = await client.post(
                f"/candidates/{candidate_id}/resume/from-download",
                json={"path": str(resume_path), "filename": resume_path.name},
            )
            assert attached.status_code == 200
            metadata = attached.json()["resume"]
            assert "storage_provider" not in metadata
            assert "bucket" not in metadata
            stored = store.get_resume(candidate_id, metadata["id"])
            assert stored["storage_provider"] == "r2"
            assert stored["bucket"] == "radixsol-test-resumes"
            assert stored["data"] == b""
            downloaded = await client.get(
                f"/candidates/{candidate_id}/resumes/{metadata['id']}"
            )
            assert downloaded.status_code == 200
            assert downloaded.content == pdf

    asyncio.run(exercise_cloud_resume())


def test_frontend_is_manifest_v3_compatible():
    source_root = Path(__file__).parents[1]
    project_root = source_root.parent
    frontend = source_root / "frontend"
    manifest = json.loads((frontend / "manifest.json").read_text(encoding="utf-8"))
    index = (frontend / "index.html").read_text(encoding="utf-8")
    app_script = (frontend / "app.js").read_text(encoding="utf-8")
    styles = (frontend / "styles.css").read_text(encoding="utf-8")
    content_script = (frontend / "indeed-content.js").read_text(encoding="utf-8")
    platform_script = (frontend / "platform-content.js").read_text(encoding="utf-8")
    platform_main = (frontend / "platform-main.js").read_text(encoding="utf-8")
    linkedin_script = (frontend / "linkedin-content.js").read_text(encoding="utf-8")
    facebook_script = (frontend / "facebook-content.js").read_text(encoding="utf-8")
    healthcare_directory_script = (frontend / "healthcare-directory-content.js").read_text(encoding="utf-8")
    background_script = (frontend / "background.js").read_text(encoding="utf-8")
    launcher = (source_root / "backend_launcher.py").read_text(encoding="utf-8")
    installer = (project_root / "packaging" / "setup_installer.py").read_text(encoding="utf-8")
    run_script = (project_root / "run-benchmark-backend.ps1").read_text(encoding="utf-8")

    assert manifest["manifest_version"] == 3
    assert manifest["version"] == api_module.APP_VERSION
    assert "medhunt" in manifest["name"].casefold()
    assert "radixsol" not in manifest["name"].casefold()
    assert "medhunt" in manifest["action"]["default_title"].casefold()
    assert "radixsol" not in manifest["action"]["default_title"].casefold()
    assert "medhunt" in index.casefold()
    assert "medhunt-mark" in app_script
    assert "radixsol scout" not in app_script.casefold()
    assert 'const DEFAULT_BACKEND = "http://127.0.0.1:8091";' in app_script
    assert 'const BACKEND_STORAGE_KEY = "medhuntBenchmarkABackendUrl";' in app_script
    assert 'if (DEFAULT_BACKEND.startsWith("https://"))' in app_script
    assert "DEFAULT_PORT = 8091" in launcher
    assert 'os.getenv("RADIXSOL_PORT", "8091")' in installer
    assert "--port 8091" in run_script
    assert manifest["side_panel"]["default_path"] == "index.html"
    assert "http://127.0.0.1/*" in manifest["host_permissions"]
    assert "*://*.indeed.com/*" in manifest["host_permissions"]
    assert "*://*.vivian.com/*" in manifest["host_permissions"]
    assert "*://*.ziprecruiter.com/*" in manifest["host_permissions"]
    assert "*://*.linkedin.com/*" in manifest["host_permissions"]
    assert "*://*.facebook.com/*" in manifest["host_permissions"]
    assert "*://npino.com/*" in manifest["host_permissions"]
    assert "https://eservices.nysed.gov/*" in manifest["host_permissions"]
    assert "*://npiprofile.com/*" in manifest["host_permissions"]
    assert "https://health.usnews.com/doctors/*" in manifest["host_permissions"]
    assert "https://health.usnews.com/nurse-practitioners/*" in manifest["host_permissions"]
    assert "https://health.usnews.com/physician-assistants/*" in manifest["host_permissions"]
    assert "https://health.usnews.com/dentists/*" in manifest["host_permissions"]
    assert "https://doctor.webmd.com/*" in manifest["host_permissions"]
    assert "*://*.medifind.com/*" in manifest["host_permissions"]
    assert "*://*.commonspirit.org/*" in manifest["host_permissions"]
    assert "https://providers.sharecare.com/find-a-doctor/*" in manifest["host_permissions"]
    assert "https://providers.sharecare.com/doctor/*" in manifest["host_permissions"]
    assert not any("usphonebook" in host.lower() for host in manifest["host_permissions"])
    assert "scripting" in manifest["permissions"]
    assert "downloads" in manifest["permissions"]
    assert "debugger" in manifest["permissions"]
    # Adapters are injected after first-run consent; the manifest must not read
    # supported pages merely because the extension was installed.
    assert manifest["content_scripts"] == []
    assert 'mainScript: "inject.js"' in app_script
    assert 'contentScript: "indeed-content.js"' in app_script
    assert 'mainScript: "platform-main.js"' in app_script
    assert 'contentScript: "platform-content.js"' in app_script
    assert 'contentScript: "linkedin-content.js"' in app_script
    assert 'contentScript: "facebook-content.js"' in app_script
    assert 'contentScript: "healthcare-directory-content.js"' in app_script
    inject_script = (frontend / "inject.js").read_text(encoding="utf-8")
    assert "URL.createObjectURL" in inject_script
    assert "XMLHttpRequest" in inject_script
    assert "response.clone().arrayBuffer()" in inject_script
    assert "medhuntProfileDataConsentV1" in app_script
    assert "chrome.storage.local.set({ [key]: value }" in app_script
    assert "chrome.storage.sync" not in app_script
    assert "[401, 403].includes(Number(error?.status))" in app_script
    assert "await saveDisplayedIndeedCandidates(result.page_url);" not in app_script
    assert (frontend / "privacy.html").is_file()
    assert "RADIXSOL_CAPTURE_INDEED_PROFILE" in content_script
    assert "RADIXSOL_LIST_INDEED_CANDIDATES" in content_script
    assert "RADIXSOL_SCAN_INDEED_CANDIDATES" in content_script
    assert "RADIXSOL_PLATFORM_SCAN_PROGRESS" in content_script
    assert "RADIXSOL_OPEN_INDEED_CANDIDATE" in content_script
    assert "RADIXSOL_DOWNLOAD_INDEED_RESUME" in content_script
    assert "exact-selected-card+changed-profile-panel" in content_script
    assert "realClick" in content_script
    assert "RADIXSOL_TRUSTED_INDEED_CLICK" in content_script
    assert "text.length > 40 || !/^download\\b/i.test(text)" in content_script
    assert "trigger?.contains?.(element)" not in content_script
    assert "a[href$='.pdf']" in content_script
    assert "hasCompleteIndeedContact" in app_script
    assert "timeout: 300000" in app_script
    assert "recoverStoredResume" in app_script
    assert "fetchStoredResumeBlob" in app_script
    assert "headers: authenticatedApiHeaders()" in app_script
    assert "url: blobUrl" in app_script
    assert "X-HealthBoard-Extension-Token" in app_script
    assert "RADIXSOL_PLATFORM_RESULTS_CHANGED" in content_script
    assert "Source Profiles" in index
    assert 'data-qa="Candidate Card"' in platform_script
    assert "encryptedJobseekerId" in platform_main
    assert "RADIXSOL_SCAN_PLATFORM_CANDIDATES" in platform_script
    assert "RADIXSOL_SCAN_PLATFORM_CANDIDATES" in linkedin_script
    assert "RADIXSOL_GUIDE_LINKEDIN_PDF" in linkedin_script
    assert "RADIXSOL_AUTO_LINKEDIN_PDF" in linkedin_script
    assert "RADIXSOL_TRUSTED_LINKEDIN_CLICK" in linkedin_script
    assert "automaticPdfDownload" in linkedin_script
    assert "RESERVED_ROUTES" in facebook_script
    assert "profile.php" in facebook_script
    assert "RADIXSOL_SCAN_PLATFORM_CANDIDATES" in facebook_script
    assert "RADIXSOL_SCAN_PLATFORM_CANDIDATES" in healthcare_directory_script
    assert "healthcare-directory-v9" in healthcare_directory_script
    assert "U.S. News Doctor Finder" in healthcare_directory_script
    assert "MediFind" in healthcare_directory_script
    assert "CommonSpirit Health" in healthcare_directory_script
    assert "Sharecare" in healthcare_directory_script
    assert 'key: "usnews"' in app_script
    assert 'key: "medifind"' in app_script
    assert "professional-profile-resume" in app_script
    assert 'new Set(["usnews", "medifind", "commonspirit", "sharecare", "webmd", "npino"])' in app_script
    assert 'key: "sharecare"' in app_script
    assert "captureProfessionalProfileInBackground" in app_script
    assert "startProfessionalProfileResumeBatch" in app_script
    assert "professionalProfileResumeQueue.push(profile)" in app_script
    assert "sameProfessionalProfileUrl(activeSourcingPageUrl, professionalProfile.source_url)" in app_script
    assert "profile_document" in healthcare_directory_script
    assert 'return "usnews"' in background_script
    watcher_manifest = json.loads(
        (source_root / "watcher_frontend" / "manifest.json").read_text(encoding="utf-8")
    )
    assert not any(
        any(host in permission for host in ("npino.com", "nysed.gov", "npiprofile.com", "usnews.com", "medifind.com", "commonspirit.org", "sharecare.com"))
        for permission in watcher_manifest["host_permissions"]
    )
    assert "RADIXSOL_ARM_LINKEDIN_PDF_CAPTURE" in background_script
    assert "RADIXSOL_TRUSTED_LINKEDIN_CLICK" in background_script
    assert "radixsolLinkedinPdfCapture" in background_script
    assert "linkedinProfileSlug(tab?.url || \"\") !== sourceSlug" in background_script
    assert "Number(download.tabId) !== armedTabId" in background_script
    assert "await matchesArmedLinkedinPdf(download, linkedin)" in background_script
    assert "(sameTab || linkedinOrigin)" not in background_script
    assert "guidedPdfCapture" in app_script
    assert "automaticPdfCapture" in app_script
    assert "startLinkedinResumeBatch" in app_script
    assert "linkedinResumeCompletion" in app_script
    assert "SOURCING_PLATFORMS" in app_script
    assert "CST|CNOR|PCCN|PHN" in linkedin_script
    assert "lookup-indeed" in app_script
    assert "USPhoneBook" not in app_script
    assert "/contact-lookup/batch" in app_script
    assert 'result?.status === "found"' in app_script
    assert 'resume_required: status === "found"' in app_script
    assert 'lookup?.location_match?.type === "hometown"' in app_script
    assert "Contact found using From location:" in app_script
    assert "escapeHtml(result.location_match.value)" in app_script
    # The side panel must not go back to one request per selected candidate.
    assert "CONTACT_BATCH_SIZE" in app_script
    assert "indeedLookupScope" in app_script
    assert "resumeQueue" in app_script
    assert "placeholder-action" not in app_script
    assert "open-batch-ats" not in app_script
    assert "assign-batch-pool" not in app_script
    assert "create-batch-campaign" not in app_script
    assert "draft-batch-email" not in app_script
    assert "report-issue" not in app_script
    assert "X-Api-Key" not in app_script
    client_bundle = "\n".join((
        app_script, styles, index, manifest["description"],
    )).casefold()
    for hidden_term in (
        "people data labs", "pdl", "enformion", "endato", "nppes", "neon",
        "cloudflare", "sqlite", "likelihood", "mobile_phone", "gemini",
        "neverbounce", "twilio", "usphonebook", "provider credit",
            "quick sourcer", "quick_sourcer", "quick-sourcer",
        "api_key", "client_secret",
    ):
        assert hidden_term not in client_bundle
    assert "50 candidates captured" not in app_script
    assert "onclick=" not in index
    assert '<script src="app.js"></script>' in index


def test_release_frontend_preserves_template_literal_whitespace():
    """The production builder must never rewrite HTML inside template strings."""
    import importlib.util

    project_root = Path(__file__).parents[2]
    builder_path = project_root / "packaging" / "build_frontend.py"
    spec = importlib.util.spec_from_file_location("radixsol_build_frontend", builder_path)
    builder = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(builder)

    output = Path(tempfile.mkdtemp()) / "extension"
    builder.build(project_root / "src_pkg" / "frontend", output)
    release_script = (output / "app.js").read_text(encoding="utf-8")
    assert 'matched${indeedResultFilter === "matched" ? " active" : ""}' in release_script
    assert "${indeedCandidates.length} profiles ready" in release_script
    assert "Hide prior snapshots while the server checks" not in release_script


def test_chrome_store_package_is_minimal_and_contains_no_private_provider_details():
    import importlib.util
    import zipfile

    project_root = Path(__file__).parents[2]
    builder_path = project_root / "packaging" / "build_chrome_store.py"
    spec = importlib.util.spec_from_file_location("medhunt_chrome_store", builder_path)
    builder = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.path.insert(0, str(builder_path.parent))
    try:
        spec.loader.exec_module(builder)
    finally:
        sys.path.remove(str(builder_path.parent))

    output = Path(tempfile.mkdtemp()) / "medhunt.zip"
    api_base = "https://medhunt-api.example.org"
    builder.package(project_root / "src_pkg" / "frontend", output, api_base)

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "privacy.html" in names
        assert "icons/medhunt-128.png" in names
        assert not any("watcher" in name.casefold() for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert f"{api_base}/*" in manifest["host_permissions"]
        assert "http://127.0.0.1/*" not in manifest["host_permissions"]
        assert "http://localhost/*" not in manifest["host_permissions"]
        scripts = "\n".join(
            archive.read(name).decode("utf-8")
            for name in names if name.endswith(".js")
        ).casefold()
        app_script = archive.read("app.js").decode("utf-8")
        assert 'hasResults ? "" : " candidate-queue-card"' in app_script
        assert 'indeedSelected.has(key) ? " selected" : ""' in app_script
        assert "radixsol" not in scripts
        assert not any(term in scripts for term in builder.FORBIDDEN_CLIENT_TERMS)


def test_trusted_lookup_config_uses_strict_allowlist_and_roundtrips():
    """The team installer includes only approved backend integrations."""
    import importlib.util
    from dotenv import dotenv_values

    project_root = Path(__file__).parents[2]
    helper_path = project_root / "packaging" / "provision_local_config.py"
    spec = importlib.util.spec_from_file_location("radixsol_provision_config", helper_path)
    helper = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(helper)

    temporary = Path(tempfile.mkdtemp())
    source = temporary / ".env"
    destination = temporary / "lookup-config.env"
    reference_env = temporary / "nexus.env"
    reference_config = temporary / "nexus_config.py"
    reference_env.write_text(
        "NEXUS_BASE_URL=https://nexus.test\n"
        "NEXUS_AUTH_METHOD=password\n"
        "NEXUS_TOKEN_URL=https://nexus.test/token\n"
        "NEXUS_USERNAME=nexus-user\n"
        "NEXUS_PASSWORD=nexus-password\n"
        "NEXUS_DEFAULT_PROFILE='{\"jobTypeIds\":[1],\"referralSourceName\":\"Medhunt\"}'\n",
        encoding="utf-8",
    )
    reference_config.write_text(
        "import os\n"
        "NEXUS_TOKEN_BASIC = os.getenv('NEXUS_TOKEN_BASIC', 'test-basic').strip()\n",
        encoding="utf-8",
    )
    source.write_text(
        "PDL_API_KEY=primary-test-secret\n"
        "Profile_name=licensed-team-profile\n"
        "Password='secondary secret with spaces'\n"
        "PDL_MIN_LIKELIHOOD=2\n"
        "PDL_STAGED_RETRY_ENABLED=1\n"
        "PDL_STAGED_RETRY_MAX=5\n"
        "DATABASE_BACKEND=postgresql\n"
        "DATABASE_NAME=medhunt_test\n"
        "DATABASE_URL=postgresql://database.test/base\n"
        "STORAGE_ENABLED=1\n"
        "S3_ENDPOINT_URL=https://r2.test\n"
        "S3_ACCESS_KEY=r2-access\n"
        "S3_SECRET_KEY=r2-secret\n"
        "S3_BUCKET=medhunt-resumes\n"
        "S3_SECRET_ACCESS_KEY=must-not-be-packaged\n"
        "GEMINI_API_KEY=must-not-be-packaged\n"
        "MELISSA_LICENSE_KEY=must-not-be-packaged\n"
        "QUICK_SOURCER_API_KEY=external-lookup-secret\n"
        "QUICK_SOURCER_BASE_URL=https://hub.test/api/quick-sourcer/external\n"
        f"NEXUS_REFERENCE_ENV='{reference_env}'\n"
        f"NEXUS_REFERENCE_CONFIG='{reference_config}'\n"
        "NEXUS_SYNC_ENABLED=1\n"
        "QUICK_SOURCER_TRUSTED_FOR_SYNC=1\n",
        encoding="utf-8",
    )

    primary, secondary, quick_sourcer = helper.provision(source, destination)
    assert primary and secondary and quick_sourcer
    generated = destination.read_text(encoding="utf-8")
    values = dotenv_values(destination)
    assert values["PDL_API_KEY"] == "primary-test-secret"
    assert values["ENFORMION_AP_NAME"] == "licensed-team-profile"
    assert values["ENFORMION_AP_PASSWORD"] == "secondary secret with spaces"
    assert values["PDL_ENABLED"] == "1"
    assert values["PDL_STAGED_RETRY_ENABLED"] == "1"
    assert values["PDL_STAGED_RETRY_MAX"] == "5"
    assert values["ENFORMION_ENABLED"] == "1"
    assert values["DATABASE_BACKEND"] == "postgresql"
    assert values["DATABASE_NAME"] == "medhunt_test"
    assert values["DATABASE_URL"] == "postgresql://database.test/base"
    assert values["STORAGE_ENABLED"] == "1"
    assert values["S3_BUCKET"] == "medhunt-resumes"
    assert values["QUICK_SOURCER_TRUSTED_FOR_SYNC"] == "1"
    assert values["NEXUS_SYNC_ENABLED"] == "1"
    assert values["NEXUS_TOKEN_BASIC"] == "test-basic"
    assert helper.nexus_ready(dict(values)) is True
    assert helper.cloud_ready(dict(values)) is True
    # The packaged application performs candidate lookups through Quick
    # Sourcer, so its key and endpoint ship with the same rotatable baseline.
    assert values["QUICK_SOURCER_API_KEY"] == "external-lookup-secret"
    assert values["QUICK_SOURCER_BASE_URL"] == "https://hub.test/api/quick-sourcer/external"
    assert values["QUICK_SOURCER_ENABLED"] == "1"
    assert values["CONTACT_LOOKUP_PROVIDER"] == "quick_sourcer"
    for excluded in (
        "NEXUS_REFERENCE_ENV", "NEXUS_REFERENCE_CONFIG",
        "S3_SECRET_ACCESS_KEY", "GEMINI_API_KEY",
        "MELISSA_LICENSE_KEY", "must-not-be-packaged",
    ):
        assert excluded not in generated

    try:
        helper.dotenv_line("PDL_API_KEY", "value\ninjected=1")
    except ValueError:
        pass
    else:
        raise AssertionError("Configuration values containing newlines must be rejected")


def test_installer_refreshes_baseline_and_preserves_non_storage_admin_override(monkeypatch):
    import importlib.util
    from dotenv import dotenv_values

    project_root = Path(__file__).parents[2]
    installer_path = project_root / "packaging" / "setup_installer.py"
    spec = importlib.util.spec_from_file_location("radixsol_setup_installer", installer_path)
    installer = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(installer)

    assert installer.APP_VERSION == "3.25.4"
    assert "medhunt" in installer.APP_NAME.casefold()
    assert "radixsol" not in installer.APP_NAME.casefold()
    assert "medhunt" in installer.RUN_VALUE.casefold()

    temporary = Path(tempfile.mkdtemp())
    data = temporary / "Radixsol"
    config_dir = data / "config"
    config_dir.mkdir(parents=True)
    override = config_dir / ".env.local"
    override.write_text(
        "# administrator override\n"
        "PDL_TIMEOUT=30\n"
        "DATABASE_BACKEND=sqlite\n"
        "DATABASE_URL=\n"
        "STORAGE_ENABLED=0\n"
        "S3_ENDPOINT_URL=\n"
        "S3_ACCESS_KEY=\n"
        "S3_SECRET_KEY=\n"
        "S3_BUCKET=\n",
        encoding="utf-8",
    )
    original_override = override.read_bytes()

    bundled = temporary / "lookup-config.env"
    bundled.write_text(
        "DATABASE_BACKEND='postgresql'\n"
        "DATABASE_URL='postgresql://database.test/medhunt'\n"
        "STORAGE_ENABLED='1'\n"
        "S3_ENDPOINT_URL='https://r2.test'\n"
        "S3_ACCESS_KEY='access'\n"
        "S3_SECRET_KEY='secret'\n"
        "S3_BUCKET='resumes'\n"
        "PDL_API_KEY='first'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "data_root", lambda: data)
    installed = installer.install_bundled_lookup_config(bundled)
    assert installed.read_bytes() == bundled.read_bytes()
    assert override.read_bytes() == original_override

    bundled.write_text(
        bundled.read_text(encoding="utf-8").replace("PDL_API_KEY='first'", "PDL_API_KEY='rotated'"),
        encoding="utf-8",
    )
    installer.install_bundled_lookup_config(bundled)
    assert "rotated" in installed.read_text(encoding="utf-8")
    assert override.read_bytes() == original_override
    assert not list(config_dir.glob(".env.*.tmp"))

    installer.enforce_central_storage_config(installed)
    override_values = dotenv_values(override)
    assert override_values["PDL_TIMEOUT"] == "30"
    for key in installer.CENTRAL_STORAGE_OVERRIDE_KEYS:
        assert key not in override_values

    token = installer.ensure_local_api_token()
    assert len(token) >= 40
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert dotenv_values(override)["MEDHUNT_LOCAL_API_TOKEN"] == token
    assert installer.ensure_local_api_token() == token
    assert override.read_text(encoding="utf-8").count("MEDHUNT_LOCAL_API_TOKEN") == 1

    extension = temporary / "extension"
    extension.mkdir()
    extension_script = extension / "app.js"
    marker = "__MEDHUNT_LOCAL_API_TOKEN__"
    extension_script.write_text(
        f'const LOCAL_API_TOKEN = "{marker}";\n', encoding="utf-8"
    )
    installer.inject_extension_token(extension, token)
    injected = extension_script.read_text(encoding="utf-8")
    assert marker not in injected
    assert f'"{token}"' in injected

    # Invalid or duplicate legacy assignments are never raw-injected into JS.
    override.write_text(
        "PDL_TIMEOUT=30\n"
        "MEDHUNT_LOCAL_API_TOKEN='short'\n"
        "MEDHUNT_LOCAL_API_TOKEN='also-invalid'\n",
        encoding="utf-8",
    )
    replacement = installer.ensure_local_api_token()
    rewritten = override.read_text(encoding="utf-8")
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,128}", replacement)
    assert rewritten.count("MEDHUNT_LOCAL_API_TOKEN") == 1
    assert dotenv_values(override)["MEDHUNT_LOCAL_API_TOKEN"] == replacement
    assert "PDL_TIMEOUT=30" in rewritten
    assert not list(config_dir.glob("..env.local.*.tmp"))

    backend_root = temporary / "installed"
    backend_executable = backend_root / "backend" / "RadixsolBackend.exe"
    backend_executable.parent.mkdir(parents=True)
    backend_executable.touch()
    launch = {}

    def capture_launch(args, **kwargs):
        launch["args"] = args
        launch["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(installer.subprocess, "Popen", capture_launch)
    monkeypatch.setattr(installer, "backend_ready", lambda: True)
    assert installer.launch_backend(backend_root) is True
    assert launch["args"] == [str(backend_executable)]
    assert launch["kwargs"]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_installer_stops_backend_bootloader_tree_before_upgrade(monkeypatch):
    import importlib.util

    project_root = Path(__file__).parents[2]
    installer_path = project_root / "packaging" / "setup_installer.py"
    spec = importlib.util.spec_from_file_location("medhunt_setup_stop_test", installer_path)
    installer = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(installer)

    temporary = Path(tempfile.mkdtemp())
    executable = temporary / "backend" / "RadixsolBackend.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    calls = []

    def capture_run(args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(installer.subprocess, "run", capture_run)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)
    installer.stop_backend(temporary)

    assert calls[0][0] == [str(executable), "--shutdown"]
    assert calls[1][0] == [
        "taskkill.exe", "/F", "/T", "/IM", "RadixsolBackend.exe",
    ]
    assert calls[1][1]["stdout"] is installer.subprocess.DEVNULL
    assert calls[1][1]["stderr"] is installer.subprocess.DEVNULL


def test_windows_version_resources_use_current_medhunt_branding():
    project_root = Path(__file__).parents[2]
    setup_version = (project_root / "packaging" / "version_setup.txt").read_text(
        encoding="utf-8"
    )
    backend_version = (project_root / "packaging" / "version_backend.txt").read_text(
        encoding="utf-8"
    )
    for resource in (setup_version, backend_version):
        assert "filevers=(3,22,15,0)" in resource
        assert "prodvers=(3,22,15,0)" in resource
        assert "3.25.4" in resource
        assert "Medhunt" in resource
        assert "Radixsol Sourcing Assistant" not in resource


def test_windows_build_requires_compiled_runtime_and_ocr_assets():
    build_script = (
        Path(__file__).parents[2] / "packaging" / "build.ps1"
    ).read_text(encoding="utf-8")
    assert "--collect-all pydantic_core" in build_script
    assert "$BundledPydanticCore" in build_script
    assert "$BundledTessdata" in build_script
    assert "Packaged backend is missing the compiled pydantic_core module." in build_script


def test_extension_checks_authenticated_session_and_explains_stale_copy():
    app_script = (
        Path(__file__).parents[1] / "frontend" / "app.js"
    ).read_text(encoding="utf-8")
    assert 'await api("/session", { timeout: 6000 })' in app_script
    assert "Number(error?.status) === 401" in app_script
    assert "Reload Medhunt from the installed extension folder." in app_script


def test_frontend_locks_captured_candidates_during_lookup():
    frontend = Path(__file__).parents[1] / "frontend"
    app_script = (
        frontend / "app.js"
    ).read_text(encoding="utf-8")
    content_script = (frontend / "indeed-content.js").read_text(encoding="utf-8")
    lookup_script = app_script.split(
        "async function lookupSelectedIndeedCandidates()", 1
    )[1].split("function toggleAllIndeedCandidates()", 1)[0]
    import_script = app_script.split(
        "async function performDisplayedIndeedSave(searchUrl)", 1
    )[1].split("async function scanIndeedCandidates(options = {})", 1)[0]

    assert "let indeedLookupInProgress = false;" in app_script
    assert "let indeedScanGeneration = 0;" in app_script
    assert "let indeedLookupProfiles = [];" in app_script
    assert "indeedLookupProfiles = profiles.slice();" in app_script
    assert 'indeedLookupFor(profile)?.status === "failed"' in app_script
    assert "No candidates in this result filter" in app_script
    assert "scanGeneration !== indeedScanGeneration" in app_script
    assert 'indeedLookupInProgress || ["scanning", "lookup", "results"].includes' in app_script
    assert "Only irrelevant, duplicate, or incomplete items were detected and skipped." in app_script
    assert "finally {\n    indeedLookupInProgress = false;" in app_script
    assert "profile._candidateId = candidateId;" in import_script
    assert "_databaseExisting" not in import_script
    assert "candidate.emails" not in import_script
    assert "candidate.phones" not in import_script
    assert "indeedLookupState.set" not in import_script
    assert "for (const profile of profiles) {" in lookup_script
    assert 'status: "looking_up"' in lookup_script
    assert "if (!hasCompleteIndeedContact(existing)" not in lookup_script
    assert lookup_script.index("renderIndeedProfiles();") < lookup_script.index(
        "await saveDisplayedIndeedCandidates(activeSourcingPageUrl, profiles);"
    )
    assert "lookupTargets.push(profile);" in lookup_script
    assert "allowPageIdentity = false" in content_script
    assert "linkedSourceUrl || allowPageIdentity" in content_script
    assert "root.contains(closestCard)" in content_script
    assert "const sourceId = cardAutoSourceId || identity.sourceId ||" in content_script
    assert "const cards = displayedResultCards();" in content_script
    assert "context.nameElement, context.root, true" in content_script


def test_zoom_sms_sends_directly_and_is_idempotent(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Taylor Nurse", "Atlanta, GA", source="sharecare")
    quick_sourcer_client.apply_to_candidate(candidate_id, {
        "status": "found",
        "name": "Taylor Nurse",
        "source": "test fixture",
        "emails": [],
        "phones": [{"value": "+14045550123", "type": "Wireless"}],
        "addresses": ["Atlanta, GA"],
    })
    monkeypatch.setattr(config, "ZOOM_SMS_ENABLED", True)
    monkeypatch.setattr(config, "ZOOM_SMS_SENDER_NUMBER", "+14045550999")
    monkeypatch.setattr(config, "ZOOM_SMS_SENDER_USER_ID", "zoom-user-1")
    monkeypatch.setattr(zoom_sms, "send_sms", lambda phone, message, **kwargs: {
        "message_id": "zoom-message-1", "session_id": "zoom-session-1",
    })

    async def exercise():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "candidate_id": candidate_id,
                "phone": "+14045550123",
                "message": "Hi Taylor, are you open to an opportunity?",
                "request_id": "sms-test-request-1",
            }
            sent = await client.post("/messaging/sms", json=body)
            assert sent.status_code == 200, sent.text
            conversation = sent.json()["conversation"]
            assert conversation["zoom_session_id"] == "zoom-session-1"
            assert conversation["messages"][0]["status"] == "accepted"
            assert "Reply STOP to opt out" in conversation["messages"][0]["body"]

            repeated = await client.post("/messaging/sms", json=body)
            assert repeated.status_code == 200
            assert len(repeated.json()["conversation"]["messages"]) == 1

            monkeypatch.setattr(config, "ZOOM_WEBHOOK_SECRET_TOKEN", "webhook-secret")
            incoming = {
                "event": "phone.sms_received", "event_ts": int(time.time() * 1000),
                "payload": {"object": {
                    "message_id": "zoom-inbound-1", "session_id": "zoom-session-1",
                    "message": "STOP", "sender": {"phone_number": "+14045550123"},
                    "to_members": [{"phone_number": "+14045550999"}],
                }},
            }
            raw = json.dumps(incoming, separators=(",", ":")).encode()
            timestamp = str(int(time.time()))
            signature = "v0=" + hmac.new(
                b"webhook-secret", b"v0:" + timestamp.encode() + b":" + raw,
                hashlib.sha256,
            ).hexdigest()
            webhook = await client.post(
                "/integrations/zoom/webhook", content=raw,
                headers={
                    "Content-Type": "application/json",
                    "x-zm-request-timestamp": timestamp,
                    "x-zm-signature": signature,
                },
            )
            assert webhook.status_code == 200, webhook.text
            assert store.is_dnc("+14045550123") is True
            assert store.get_sms_consent(candidate_id, "+14045550123")["status"] == "opted_out"
            saved = store.get_sms_conversation(conversation["id"])
            assert saved["status"] == "opted_out"
            assert saved["messages"][-1]["body"] == "STOP"

    asyncio.run(exercise())


def test_zoom_sms_preserves_halo_messaging_permission_denial(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from starlette.requests import Request

    monkeypatch.setattr(healthboard_auth, "enabled", lambda: True)
    response = httpx.Response(
        403,
        json={"detail": "Your organization has paused or disabled your messaging access."},
        request=httpx.Request("GET", "https://halo.example.test/api/extension/medhunt/sms-sender"),
    )

    def denied(_token):
        raise httpx.HTTPStatusError("denied", request=response.request, response=response)

    monkeypatch.setattr(healthboard_auth, "medhunt_zoom_sms_sender", denied)
    request = Request({"type": "http", "method": "POST", "path": "/messaging/sms", "headers": []})
    request.state.healthboard_extension_token = "extension-token"

    with pytest.raises(HTTPException) as caught:
        api_module._resolve_zoom_sms_sender(request, strict=True)
    assert caught.value.status_code == 403
    assert "paused or disabled" in caught.value.detail


def test_zoom_sms_direct_send_does_not_require_a_test_number_allowlist(monkeypatch):
    store.reset()
    allowed_id = store.add_candidate("Owned Test Phone", "Atlanta, GA", source="test")
    blocked_id = store.add_candidate("Unlisted Phone", "Atlanta, GA", source="test")
    for candidate_id, phone in (
        (allowed_id, "+14045550123"),
        (blocked_id, "+14045550124"),
    ):
        quick_sourcer_client.apply_to_candidate(candidate_id, {
            "status": "found", "name": "Test Recipient", "source": "test fixture",
            "emails": [], "phones": [{"value": phone, "type": "Wireless"}],
            "addresses": ["Atlanta, GA"],
        })
    monkeypatch.setattr(config, "ZOOM_SMS_ENABLED", True)
    monkeypatch.setattr(config, "ZOOM_SMS_SENDER_NUMBER", "+14045550999")
    monkeypatch.setattr(config, "ZOOM_SMS_SENDER_USER_ID", "zoom-user-1")
    calls = []
    monkeypatch.setattr(zoom_sms, "send_sms", lambda phone, message, **kwargs: (
        calls.append((phone, message)) or {
            "message_id": "zoom-test-message", "session_id": "zoom-test-session",
        }
    ))

    async def exercise():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            sent = await client.post("/messaging/sms", json={
                "candidate_id": allowed_id, "phone": "+14045550123",
                "message": "Test message", "request_id": "allowlisted-test-message",
            })
            assert sent.status_code == 200, sent.text
            assert len(calls) == 1

            second = await client.post("/messaging/sms", json={
                "candidate_id": blocked_id, "phone": "+14045550124",
                "message": "A second verified candidate", "request_id": "unlisted-test-message",
            })
            assert second.status_code == 200
            assert len(calls) == 2

    asyncio.run(exercise())


def test_zoom_sms_rejects_messages_over_29_words_before_calling_zoom(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate("Morgan Clinician", "Seattle, WA", source="medifind")
    quick_sourcer_client.apply_to_candidate(candidate_id, {
        "status": "found", "name": "Morgan Clinician", "source": "test fixture",
        "emails": [], "phones": [{"value": "+12065550123", "type": "Wireless"}],
        "addresses": ["Seattle, WA"],
    })
    monkeypatch.setattr(config, "ZOOM_SMS_ENABLED", True)
    monkeypatch.setattr(config, "ZOOM_SMS_SENDER_NUMBER", "+12065550999")
    monkeypatch.setattr(config, "ZOOM_SMS_SENDER_USER_ID", "zoom-user-2")
    sent_messages = []
    monkeypatch.setattr(zoom_sms, "send_sms", lambda phone, message, **kwargs: (
        sent_messages.append((phone, message)) or {
            "message_id": f"zoom-{len(sent_messages)}", "session_id": "zoom-session",
        }
    ))

    async def exercise():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/messaging/sms", json={
                "candidate_id": candidate_id, "phone": "+12065550123",
                "message": " ".join(f"word{index}" for index in range(30)),
                "request_id": "over-word-limit",
            })
            assert response.status_code == 422
            assert "29 words" in response.json()["detail"]
            assert sent_messages == []

    asyncio.run(exercise())
