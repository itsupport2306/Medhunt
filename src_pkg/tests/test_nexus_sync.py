from __future__ import annotations

import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import nexus_sync


PDF = b"%PDF-1.7\nunit-test\n%%EOF"


def _settings(**changes):
    values = {
        "enabled": True,
        "base_url": "https://nexus.invalid",
        "auth_method": "static",
        "static_token": "private-static-token",
        "resume_doc_type_id": "91",
        "default_profile": {
            "professionId": 10,
            "specialtyId": 20,
            "stateIds": {"OH": 30},
            "countryId": 40,
            "statusId": 50,
            "referralSourceId": 60,
            "jobTypeIds": ["PERM"],
        },
    }
    values.update(changes)
    return nexus_sync.NexusSettings(**values)


def _payload(**candidate_changes):
    candidate = {
        "contacts_trusted": True,
        "name": "Jane Example, RN",
        "location": "Columbus, OH",
        "emails": ["Jane.Example@example.com"],
        "phones": ["(614) 555-0123"],
        "job_title": "Registered Nurse",
        # This resembles private upstream evidence and must never be forwarded.
        "verification": {"provider": "raw-provider-secret-marker"},
    }
    candidate.update(candidate_changes)
    return {
        "candidate": candidate,
        "resume": {
            "id": 7,
            "filename": "Jane Example.pdf",
            "checksum_sha256": "a" * 64,
        },
    }


def _client(settings, handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return nexus_sync.NexusClient(settings, http_client=http)


def test_no_duplicate_creates_candidate_with_whitelisted_trusted_fields():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert "raw-provider-secret-marker" not in content
            assert '"firstName":"Jane"' in content
            assert '"lastName":"Example"' in content
            assert '"email":"jane.example@example.com"' in content
            assert '"phone":"(614) 555-0123"' in content
            assert '"cellPhone":"(614) 555-0123"' in content
            assert '"sendMassEmails":false' in content
            assert '"sendMassSms":false' in content
            assert '"stateId":30' in content
            return httpx.Response(201, json={"id": 701})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(), PDF, settings=settings, client=_client(settings, handler)
    )

    searches = [
        json.loads(request.content)
        for request in requests
        if request.url.path.endswith("/candidates/search")
    ]
    assert searches == [
        {
            "pagingSortingDetails": {"start": 0, "maxRowsToFetch": 20},
            "email": "jane.example@example.com",
        },
        {
            "pagingSortingDetails": {"start": 0, "maxRowsToFetch": 20},
            "phone": "(614) 555-0123",
        },
    ]
    assert result == {
        "status": "delivered",
        "action": "candidate_created",
        "nexus_candidate_id": 701,
        "matched_by": [],
        "resume_id": 7,
        "checksum_sha256": "a" * 64,
    }


@pytest.mark.parametrize(
    ("emails", "phones", "expected_search_key"),
    [
        (["email-only@example.test"], [], "email"),
        ([], ["(614) 555-0142"], "phone"),
    ],
)
def test_candidate_creation_accepts_either_trusted_contact(
    emails, phones, expected_search_key,
):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert f'"{expected_search_key}"' in content or expected_search_key == "phone"
            if expected_search_key == "email":
                assert '"email":"email-only@example.test"' in content
                assert '"primaryEmail":"email-only@example.test"' in content
                assert '"phone":' not in content
                assert '"cellPhone":' not in content
            else:
                assert '"phone":"(614) 555-0142"' in content
                assert '"cellPhone":"(614) 555-0142"' in content
                assert '"email":' not in content
                assert '"primaryEmail":' not in content
            return httpx.Response(201, json={"id": 703})
        raise AssertionError(request.url)

    payload = _payload(emails=emails, phones=phones)
    result = nexus_sync.process_delivery(
        payload, PDF, settings=_settings(), client=_client(_settings(), handler),
    )
    searches = [json.loads(request.content) for request in requests
                if request.url.path.endswith("/candidates/search")]
    assert len(searches) == 1
    assert expected_search_key in searches[0]
    assert result["nexus_candidate_id"] == 703


def test_nursing_role_resolves_exact_rn_and_unknown_specialty_defaults():
    master_calls = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/master/professions"):
            master_calls.append("professions")
            return httpx.Response(200, json=[{"id": 10, "name": "RN", "active": True}])
        if request.url.path.endswith("/master/specialties"):
            master_calls.append("specialties")
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 999, "specialtyId": 20, "professionId": 10,
                        "name": "Unknown", "active": True,
                    },
                    {"specialtyId": 21, "professionId": 11, "name": "Unknown", "active": True},
                ],
            )
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"professionId":10' in content
            assert '"specialtyId":20' in content
            return httpx.Response(201, json={"id": 702})
        raise AssertionError(request.url)

    defaults = dict(_settings().default_profile)
    defaults.pop("professionId")
    defaults.pop("specialtyId")
    settings = _settings(default_profile=defaults)
    result = nexus_sync.process_delivery(
        _payload(), PDF, settings=settings, client=_client(settings, handler)
    )
    assert master_calls == ["professions", "specialties"]
    assert result["nexus_candidate_id"] == 702


def test_resume_extraction_specialties_reach_nexus_identity_in_document_order():
    identity = nexus_sync._trusted_identity({
        "candidate": {
            "contacts_trusted": True,
            "name": "Jane Example",
            "emails": ["jane@example.test"],
            "phones": [],
            "job_title": "Registered Nurse",
        },
        "resume_extraction": {
            "schema_version": 2,
            "status": "extracted",
            "fields": {"specialties": ["Med Surg", "ICU"]},
            "confidence": {"specialties": 0.76},
            "conflicts": [],
        },
    })
    assert identity["source_specialties"] == []
    assert identity["resume_specialties"] == ["Med Surg", "ICU"]
    assert identity["specialties"] == ["Med Surg", "ICU"]


def test_resume_specialty_extraction_keeps_source_specialty_primary():
    identity = nexus_sync._trusted_identity({
        "candidate": {
            "contacts_trusted": True,
            "name": "Jane Example",
            "emails": ["jane@example.test"],
            "phones": [],
            "specialty": "Emergency",
        },
        "resume_extraction": {
            "schema_version": 2,
            "status": "extracted",
            "fields": {"specialties": ["ICU"]},
            "confidence": {"specialties": 0.76},
            "conflicts": [],
        },
    })
    assert identity["specialties"] == ["Emergency", "ICU"]


def test_multiple_resume_specialties_map_primary_and_additional_ids():
    def handler(request):
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
                {"specialtyId": 21, "professionId": 10, "name": "ICU", "active": True},
                {"specialtyId": 22, "professionId": 10, "name": "Med Surg", "active": True},
            ])
        raise AssertionError(request.url)

    settings = _settings()
    profile = nexus_sync._build_profile(
        _client(settings, handler),
        {
            "firstName": "Jane", "lastName": "Example", "email": "jane@example.test",
            "phone": "", "state": "OH", "country": "United States",
            "role": "Registered Nurse", "source_specialties": [],
            "resume_specialties": ["Med Surg", "ICU"],
        },
        settings.default_profile,
    )
    assert profile["professionId"] == 10
    assert profile["specialtyIds"] == [22, 21]
    assert profile["primarySpecialtyId"] == 22


def test_linked_unknown_classification_is_repaired_before_resume_upload():
    requests = []
    candidate_reads = 0

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
                {"specialtyId": 21, "professionId": 10, "name": "ICU", "active": True},
            ])
        if request.url.path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 10, "name": "RN", "active": True},
                {"professionId": 99, "name": "Unknown", "active": True},
            ])
        if request.method == "GET" and request.url.path.endswith("/candidates/701"):
            nonlocal candidate_reads
            candidate_reads += 1
            if candidate_reads == 1:
                return httpx.Response(200, json={
                    "professionIds": [99], "specialtyIds": [20], "primarySpecialtyId": 20,
                })
            return httpx.Response(200, json={
                "professionIds": [10], "specialtyIds": [21], "primarySpecialtyId": 21,
            })
        if request.method == "PATCH" and request.url.path.endswith("/candidates/701"):
            patch = json.loads(request.content)
            assert patch == {
                "professionIds": [10], "specialtyIds": [21], "primarySpecialtyId": 21,
            }
            return httpx.Response(200, json={})
        if request.method == "POST" and request.url.path.endswith("/upload/documents"):
            return httpx.Response(200, json={"id": 1})
        raise AssertionError((request.method, request.url))

    settings = _settings()
    result = nexus_sync.process_delivery(
        {
            **_payload(emails=["jane@example.test"], phones=[]),
            "nexus_candidate_id": "701",
            "resume_extraction": {
                "schema_version": 2, "status": "extracted",
                "fields": {"specialties": ["ICU"]},
                "confidence": {"specialties": 0.76}, "conflicts": [],
            },
        },
        PDF, settings=settings, client=_client(settings, handler),
    )
    assert result["classification_updated"] is True
    assert result["nexus_candidate_id"] == "701"
    assert any(request.method == "PATCH" for request in requests)


def test_actual_candidate_specialty_overrides_generic_default_and_aligns_profession():
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {
                    "specialtyId": 321,
                    "professionId": 77,
                    "name": "Urology",
                    "active": True,
                },
                {
                    "specialtyId": 20,
                    "professionId": 10,
                    "name": "Unknown",
                    "active": True,
                },
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"specialtyId":321' in content
            assert '"primarySpecialtyId":321' in content
            assert '"specialtyIds":[321]' in content
            assert '"professionId":77' in content
            assert '"professionIds":[77]' in content
            assert '"specialtyId":20' not in content
            return httpx.Response(201, json={"id": 705})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(notes="Specialty: Urology"), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 705


def test_source_role_in_notes_resolves_profession_for_directory_profiles():
    """The import row stores source roles as explicit notes evidence."""
    identity = nexus_sync._trusted_identity({
        "candidate": {
            "contacts_trusted": True,
            "name": "Alex Provider",
            "location": "Columbus, OH",
            "emails": ["alex@example.test"],
            "phones": [],
            "notes": "Role: Nurse Anesthetist\nSpecialty: https://schema.org/Anesthesia",
        },
    })
    assert identity["role"] == "Nurse Anesthetist"
    assert identity["specialties"] == ["https://schema.org/Anesthesia"]


def test_sharecare_schema_uri_maps_to_live_nexus_specialty_and_role():
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 11, "name": "Nurse Anesthetist", "active": True},
            ])
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {
                    "specialtyId": 12, "professionId": 11,
                    "name": "Anesthesiology", "active": True,
                },
                {
                    "specialtyId": 99, "professionId": 1,
                    "name": "Unknown", "active": True,
                },
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"professionId":11' in content
            assert '"specialtyId":12' in content
            return httpx.Response(201, json={"id": 708})
        raise AssertionError(request.url)

    settings = _settings(default_profile={
        "stateIds": {"OH": 30},
        "countryId": 40,
        "statusId": 50,
        "referralSourceId": 60,
        "jobTypeIds": ["PERM"],
    })
    result = nexus_sync.process_delivery(
        _payload(
            name="Alex Provider",
            job_title=None,
            notes="Role: Nurse Anesthetist\nSpecialty: https://schema.org/Anesthesia",
        ),
        PDF,
        settings=settings,
        client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 708


def test_unmatched_candidate_specialty_uses_unknown_classification():
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 199, "professionId": 99, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 99, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"professionId":99' in content
            assert '"specialtyId":199' in content
            return httpx.Response(201, json={"id": 707})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(notes="Specialty: Unlisted Clinical Field"), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 707


@pytest.mark.parametrize(
    ("source_label", "nexus_label"),
    [
        ("Family Medicine", "Family Practice"),
        ("Primary Care", "Family Practice/Primary Care"),
        ("Thoracic Surgery", "Surgery-Thoracic"),
        ("OB-GYN", "Obstetrics & Gynecology"),
    ],
)
def test_candidate_specialty_uses_reviewed_nexus_alias(source_label, nexus_label):
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 321, "professionId": 10, "name": nexus_label, "active": True},
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"professionId":10' in content
            assert '"specialtyId":321' in content
            return httpx.Response(201, json={"id": 708})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(notes=f"Specialty: {source_label}"), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 708


def test_primary_email_must_belong_to_trusted_filtered_email_list():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert "blocked@example.com" not in content
            assert '"email":"safe@example.com"' in content
            return httpx.Response(201, json={"id": 706})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(
            primary_email="blocked@example.com",
            emails=["safe@example.com"],
        ),
        PDF,
        settings=settings,
        client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 706
    searches = [
        json.loads(request.content)
        for request in requests
        if request.url.path.endswith("/candidates/search")
    ]
    assert searches[0]["email"] == "safe@example.com"


def test_non_rn_nursing_role_resolves_from_live_profession_catalog():
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 12, "name": "Nurse Practitioner", "active": True},
            ])
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 22, "professionId": 12, "name": "Other", "active": True},
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"professionId":12' in content
            assert '"specialtyId":22' in content
            return httpx.Response(201, json={"id": 703})
        raise AssertionError(request.url)

    defaults = dict(_settings().default_profile)
    defaults.pop("professionId")
    defaults.pop("specialtyId")
    settings = _settings(default_profile=defaults)
    result = nexus_sync.process_delivery(
        _payload(job_title="Nurse Practitioner"), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 703


def test_unrecognized_role_uses_tenant_unknown_profession_and_specialty():
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 99, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 199, "professionId": 99, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"professionId":99' in content
            assert '"specialtyId":199' in content
            return httpx.Response(201, json={"id": 704})
        raise AssertionError(request.url)

    defaults = dict(_settings().default_profile)
    defaults.pop("professionId")
    defaults.pop("specialtyId")
    settings = _settings(default_profile=defaults)
    result = nexus_sync.process_delivery(
        _payload(job_title="Healthcare Program Coordinator"), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 704


def test_successful_create_without_remote_id_is_indeterminate():
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        if request.url.path.endswith("/candidate/webhook/create"):
            return httpx.Response(202, json={"accepted": True})
        raise AssertionError(request.url)

    settings = _settings()
    with pytest.raises(nexus_sync.NexusIndeterminateError) as raised:
        nexus_sync.process_delivery(
            _payload(), PDF, settings=settings, client=_client(settings, handler)
        )
    assert raised.value.code == "nexus_create_unbound"


def test_consistent_email_and_phone_duplicate_uploads_resume_only():
    writes = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(
                200,
                json={"records": [{"candidateId": 88}]},
            )
        if request.url.path.endswith("/candidates/88/upload/documents"):
            writes.append(request.url.path)
            return httpx.Response(204)
        if request.url.path.endswith("/candidate/webhook/create"):
            raise AssertionError("existing candidate must not be created again")
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(), PDF, settings=settings, client=_client(settings, handler)
    )

    assert writes == ["/api/api-integration/v1/candidates/88/upload/documents"]
    assert result["action"] == "resume_uploaded"
    assert result["nexus_candidate_id"] == "88"
    assert result["matched_by"] == ["email", "phone"]


def test_two_hundred_response_with_failed_document_is_not_acknowledged():
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": [{"candidateId": 88}]})
        if request.url.path.endswith("/candidates/88/upload/documents"):
            return httpx.Response(
                200,
                json={"uploadFailedDocumentNames": ["Jane Example.pdf"]},
            )
        raise AssertionError(request.url)

    settings = _settings()
    with pytest.raises(nexus_sync.NexusPermanentError) as raised:
        nexus_sync.process_delivery(
            _payload(), PDF, settings=settings, client=_client(settings, handler),
        )
    assert raised.value.code == "nexus_resume_rejected"


def test_resume_document_type_accepts_one_tenant_resume_label():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": [{"candidateId": 88}]})
        if request.url.path.endswith("/master/documenttypes"):
            return httpx.Response(200, json=[
                {"value": 91, "label": "Resume", "active": True},
                {"value": 92, "label": "Cover Letter", "active": True},
            ])
        if request.url.path.endswith("/candidates/88/upload/documents"):
            content = request.content.decode("latin-1")
            assert 'name="uploadedDocuments[0].documentTypeId"' in content
            assert "91" in content
            return httpx.Response(204)
        raise AssertionError(request.url)

    settings = _settings(resume_doc_type_id="")
    result = nexus_sync.process_delivery(
        _payload(), PDF, settings=settings, client=_client(settings, handler),
    )

    assert result["action"] == "resume_uploaded"
    assert paths.count("/api/api-integration/v1/master/documenttypes") == 1


def test_durable_candidate_link_skips_duplicate_search_and_create():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.url.path.endswith("/candidates/remote_77/upload/documents")
        return httpx.Response(204)

    settings = _settings()
    payload = _payload()
    payload["nexus_candidate_id"] = "remote_77"
    result = nexus_sync.process_delivery(
        payload, PDF, settings=settings, client=_client(settings, handler)
    )

    assert paths == [
        "/api/api-integration/v1/candidates/remote_77/upload/documents"
    ]
    assert result["matched_by"] == ["stored_link"]
    assert result["nexus_candidate_id"] == "remote_77"


def test_conflicting_email_and_phone_matches_are_never_written():
    writes = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={"records": [{"candidateId": 1 if "email" in body else 2}]},
            )
        writes.append(request.url.path)
        return httpx.Response(500)

    settings = _settings()
    with pytest.raises(nexus_sync.NexusIndeterminateError) as raised:
        nexus_sync.process_delivery(
            _payload(), PDF, settings=settings, client=_client(settings, handler)
        )
    assert "different candidates" in str(raised.value)
    assert writes == []


def test_single_contact_match_with_a_different_name_is_held_for_review():
    writes = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            body = json.loads(request.content)
            if "email" in body:
                return httpx.Response(200, json={"records": [{
                    "candidateId": 88,
                    "firstName": "Someone",
                    "lastName": "Else",
                }]})
            return httpx.Response(200, json={"records": []})
        writes.append(request.url.path)
        return httpx.Response(500)

    settings = _settings()
    with pytest.raises(
        nexus_sync.NexusIndeterminateError,
        match="different name",
    ) as raised:
        nexus_sync.process_delivery(
            _payload(), PDF, settings=settings, client=_client(settings, handler)
        )
    assert raised.value.code == "nexus_contact_name_conflict"
    assert writes == []


def test_single_contact_match_without_a_name_is_held_for_review():
    writes = []

    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            body = json.loads(request.content)
            rows = [{"candidateId": 88}] if "email" in body else []
            return httpx.Response(200, json={"records": rows})
        writes.append(request.url.path)
        return httpx.Response(500)

    settings = _settings()
    with pytest.raises(nexus_sync.NexusIndeterminateError) as raised:
        nexus_sync.process_delivery(
            _payload(), PDF, settings=settings, client=_client(settings, handler),
        )
    assert raised.value.code == "nexus_contact_name_missing"
    assert writes == []


def test_write_transport_failure_is_indeterminate_and_sanitized():
    def handler(request):
        if request.url.path.endswith("/candidates/search"):
            return httpx.Response(200, json={"records": []})
        raise httpx.ConnectError(
            "private-static-token jane.example@example.com https://nexus.invalid",
            request=request,
        )

    settings = _settings()
    with pytest.raises(nexus_sync.NexusIndeterminateError) as raised:
        nexus_sync.process_delivery(
            _payload(), PDF, settings=settings, client=_client(settings, handler)
        )
    public = str(raised.value)
    assert "private-static-token" not in public
    assert "jane.example@example.com" not in public
    assert "nexus.invalid" not in public
    assert raised.value.operation == "candidate creation"


def test_password_oauth_accepts_laboredge_acess_token_spelling():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/oauth/token":
            assert b"grant_type=password" in request.content
            assert request.url.params["organizationCode"] == "agency"
            return httpx.Response(
                200,
                json={"acess_token": "issued-token", "expires_in": 3600},
            )
        if request.url.path.endswith("/candidates/search"):
            assert request.headers["Authorization"] == "Bearer issued-token"
            return httpx.Response(200, json={"records": []})
        raise AssertionError(request.url)

    settings = _settings(
        auth_method="password",
        static_token="",
        token_url="https://nexus.invalid/oauth/token",
        username="api-user",
        password="api-password",
        org_code="agency",
    )
    client = _client(settings, handler)
    assert client.search_candidates(email="person@example.com") == []
    assert [request.url.path for request in seen] == [
        "/oauth/token",
        "/api/api-integration/v1/candidates/search",
    ]


def test_untrusted_projection_is_rejected_before_any_http_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    settings = _settings()
    with pytest.raises(nexus_sync.NexusPermanentError, match="not approved"):
        nexus_sync.process_delivery(
            _payload(contacts_trusted=False),
            PDF,
            settings=settings,
            client=_client(settings, handler),
        )
    assert calls == []


def test_error_dictionary_has_stable_code_and_retry_hint():
    error = nexus_sync.NexusRetryableError(
        "Try later.", operation="duplicate_search", retry_after=2.5
    )
    assert error.as_dict() == {
        "category": "retryable",
        "code": "nexus_retryable",
        "message": "Try later.",
        "operation": "duplicate_search",
        "status_code": None,
        "retry_after": 2.5,
    }
