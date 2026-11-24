import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sourcing import config, license_discovery, nexus_sync, store
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


def test_checks_later_profile_and_records_blocked_sources():
    def respond(request):
        path = request.url.path
        if path == "/find_web/public_search/users":
            return httpx.Response(200, text="".join(
                f'<a href="/pub/person-{index}">Profile</a>' for index in range(1, 5)
            ))
        if path.startswith("/pub/person-"):
            city = "Reston" if path.endswith("4") else "Arlington"
            return httpx.Response(200, text=(
                f"<title>Dr. Michelle Susco, MD - {city}, VA | Pediatrics</title>"
                "<li>VA State Medical License Active through 2028</li>"
            ))
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        evidence, diagnostics = license_discovery.discover_with_diagnostics(
            {"name": "Michelle Susco", "location": "Reston, VA"}, client=client,
        )
    assert evidence["states"] == ["VA"]
    assert diagnostics["sources"][0] == {
        "source": "doximity", "status": "verified", "profiles_checked": 4,
    }


def test_same_npi_allows_changed_practice_city():
    page = license_discovery._page(
        "<title>Dr. Michelle Susco, MD - Arlington, VA | Pediatrics</title>"
        "<div>NPI number: 1760443691</div>"
    )
    assert license_discovery._profile_matches(page, {
        "name": "Michelle Susco", "location": "Reston, VA",
        "source": "npino", "source_id": "1760443691",
    })


def test_blocked_sites_are_reported_without_claiming_license():
    def respond(request):
        return httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        evidence, diagnostics = license_discovery.discover_with_diagnostics(
            {"name": "Michelle Susco", "location": "Reston, VA"}, client=client,
        )
    assert evidence == {}
    assert [item["http_status"] for item in diagnostics["sources"]] == [403, 403, 403]


def test_delivery_records_lookup_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "medhunt.sqlite"))
    with store._conn() as connection:
        connection.execute(
            """INSERT INTO nexus_deliveries
               (candidate_id,resume_id,identity_key,status,lease_until,created,updated)
               VALUES (1,1,'person-1','processing',123,1,1)"""
        )
    assert store.save_nexus_license_discovery(
        1, {"sources": [{"source": "healthgrades", "status": "search_http_error", "http_status": 403}]},
        expected_lease_until=123,
    )
    with store._conn() as connection:
        saved = connection.execute(
            "SELECT license_discovery FROM nexus_deliveries WHERE id=1"
        ).fetchone()
    assert json.loads(saved["license_discovery"])["sources"][0]["http_status"] == 403


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
        if request.url.path.endswith("/master/states"):
            return httpx.Response(200, json={"data": [
                {"stateId": 42, "code": "VA", "name": "Virginia"},
            ]})
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
        identity = nexus_sync._trusted_identity({
            "candidate": {
                "name": "Michelle Susco", "location": "Reston, VA",
                "source": "sharecare", "contacts_trusted": True,
                "emails": ["michelle@example.com"], "phones": [],
                "job_title": "Physician",
            },
            "license_discovery": {"source": "doximity", "states": ["VA"]},
            "resume": {"id": 7, "filename": "profile.pdf"},
        })
        profile = nexus_sync._build_profile(client, identity, settings.default_profile)
    assert profile["preferredStateIds"] == [42]


def test_existing_nexus_candidate_merges_current_license_state():
    saved = {"preferredStateIds": [11]}

    def respond(request):
        if request.url.path.endswith("/master/states"):
            return httpx.Response(200, json={"data": [
                {"stateId": 42, "code": "VA", "name": "Virginia"},
            ]})
        if request.url.path.endswith("/candidates/712"):
            if request.method == "PATCH":
                saved.update(json.loads(request.content))
                return httpx.Response(200, json=saved)
            return httpx.Response(200, json=saved)
        raise AssertionError(request.url)

    settings = nexus_sync.NexusSettings(
        enabled=True, base_url="https://nexus.invalid", auth_method="static",
        static_token="test-token",
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = nexus_sync.NexusClient(settings, http_client=http)
        changed = nexus_sync._update_preferred_license_states(
            client, "712", {"current_license_states": ["VA"]},
        )
    assert changed is True
    assert saved["preferredStateIds"] == [11, 42]
