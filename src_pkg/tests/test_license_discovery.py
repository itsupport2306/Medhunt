import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sourcing import license_discovery, nexus_sync
import api as api_module


def test_uses_first_exact_location_matched_current_license():
    requested = []

    def respond(request):
        requested.append(str(request.url))
        path = request.url.path
        if path == "/find_web/public_search/users":
            return httpx.Response(200, text=(
                '<a href="https://www.doximity.com/pub/michelle-susco-md">Profile</a>'
            ))
        if path == "/pub/michelle-susco-md":
            return httpx.Response(200, text=(
                "<title>Dr. Michelle Susco, MD - Reston, VA | Pediatrics</title>"
                "<li>VA State Medical License 1994 - 2028</li>"
                "<li>CA State Medical License 2000 - 2020</li>"
            ))
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = license_discovery.discover(
            {"name": "Michelle Susco", "location": "Reston, VA"}, client=client,
        )
    assert result["source"] == "doximity"
    assert result["states"] == ["VA"]
    assert len(requested) == 2


def test_rejects_same_name_in_different_location_then_checks_healthgrades():
    requested = []

    def respond(request):
        requested.append(request.url.path)
        path = request.url.path
        if path == "/find_web/public_search/users":
            return httpx.Response(200, text='<a href="/pub/michelle-susco-md">Profile</a>')
        if path == "/pub/michelle-susco-md":
            return httpx.Response(200, text=(
                "<title>Dr. Michelle Susco, MD - Arlington, VA | Pediatrics</title>"
                "<li>VA State Medical License 1994 - 2028</li>"
            ))
        if path == "/usearch":
            return httpx.Response(200, text=(
                '<a href="https://www.healthgrades.com/physician/dr-michelle-susco-yplqs">Profile</a>'
            ))
        if path == "/physician/dr-michelle-susco-yplqs":
            return httpx.Response(200, text=(
                "<title>Dr. Michelle Susco, MD - Reston, VA | Healthgrades</title>"
                "<div>VA State Medical License Active through 2028</div>"
            ))
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = license_discovery.discover(
            {"name": "Michelle Susco", "location": "Reston, VA"}, client=client,
        )
    assert result["source"] == "healthgrades"
    assert result["states"] == ["VA"]
    assert requested == [
        "/find_web/public_search/users", "/pub/michelle-susco-md",
        "/usearch", "/physician/dr-michelle-susco-yplqs",
    ]


def test_discovered_state_reaches_nexus_identity_for_other_source():
    identity = nexus_sync._trusted_identity({
        "candidate": {
            "name": "Michelle Susco", "location": "Reston, VA",
            "source": "sharecare", "contacts_trusted": True,
            "emails": ["michelle@example.com"], "phones": [],
        },
        "license_discovery": {"source": "doximity", "states": ["VA"]},
    })
    assert identity["current_license_states"] == ["VA"]


def test_public_profile_import_preserves_dated_license_evidence():
    row = api_module._profile_row(api_module.ProfileImportIn(
        name="Michelle Susco", location="Reston, VA", source="doximity",
        source_url="https://www.doximity.com/pub/michelle-susco-md",
        licenses=["VA State Medical License 1994 - 2028"],
    ))
    assert "License: VA State Medical License 1994 - 2028" in row["notes"]
    assert nexus_sync._current_license_states(row, current_year=2026) == ["VA"]


def test_other_source_license_is_sent_as_nexus_local_preference():
    def respond(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/master/states"):
            return httpx.Response(200, json={"data": [
                {"stateId": 42, "code": "VA", "name": "Virginia"},
            ]})
        if request.url.path.endswith("/candidate/webhook/create"):
            assert '"preferredStateIds":[42]' in request.content.decode("latin-1")
            return httpx.Response(201, json={"id": 712})
        raise AssertionError(request.url)

    settings = nexus_sync.NexusSettings(
        enabled=True, base_url="https://nexus.invalid", auth_method="static",
        static_token="test-token", resume_doc_type_id="91",
        default_profile={
            "professionId": 10, "specialtyId": 20, "stateIds": {"VA": 42},
            "countryId": 40, "statusId": 50, "referralSourceId": 60,
            "jobTypeIds": ["PERM"],
        },
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = nexus_sync.NexusClient(settings, http_client=http)
        result = nexus_sync.process_delivery({
            "candidate": {
                "name": "Michelle Susco", "location": "Reston, VA",
                "source": "sharecare", "contacts_trusted": True,
                "emails": ["michelle@example.com"], "phones": [],
                "job_title": "Physician",
            },
            "license_discovery": {"source": "doximity", "states": ["VA"]},
            "resume": {"id": 7, "filename": "profile.pdf"},
        }, b"%PDF-1.7\nfixture", settings=settings, client=client)
    assert result["nexus_candidate_id"] == 712
