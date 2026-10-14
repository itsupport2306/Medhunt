from __future__ import annotations

import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import nexus_sync


PDF = b"%PDF-1.7\nunit-test\n%%EOF"


def test_selected_physician_classification_overrides_nursing_defaults():
    class MasterClient:
        def get_master(self, name):
            return {
                "professions": [
                    {"professionId": 10, "name": "RN", "active": True},
                    {"professionId": 77, "name": "Physician", "active": True},
                ],
                "specialties": [
                    {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
                    {"specialtyId": 88, "professionId": 77,
                     "name": "Hematology/Oncology", "active": True},
                ],
            }[name]

    identity = {
        "firstName": "Raja", "middleName": "", "lastName": "Flores",
        "email": "raja@example.test", "phone": "", "city": "Columbus",
        "state": "OH", "country": "United States", "role": "Registered Nurse",
        "source_specialties": ["ICU"], "resume_specialties": ["General"],
        "nexus_classification": {
            "offering": "Physician", "profession": "Physician",
            "specialty": "Hematology/Oncology",
        },
    }
    profile = nexus_sync._build_profile(MasterClient(), identity, _settings().default_profile)
    assert profile["professionIds"] == [77]
    assert profile["specialtyIds"] == [88]
    assert profile["primarySpecialtyId"] == 88
    assert "recruiterId" not in profile


def test_selected_specialty_is_verified_and_repaired_after_nexus_creation():
    selected = {
        "offering": "Physician", "profession": "Physician",
        "specialty": "Hematology/Oncology", "recruiter_id": 57,
    }
    patched = False

    def handler(request):
        nonlocal patched
        path = request.url.path
        if path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 10, "name": "RN", "active": True},
                {"professionId": 77, "name": "Physician", "active": True},
            ])
        if path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
                {"specialtyId": 88, "professionId": 77,
                 "name": "Hematology/Oncology", "active": True},
            ])
        if path.endswith("/master/recruiters"):
            return httpx.Response(200, json=[
                {"id": 57, "name": "Tina Mihic", "active": True},
                {"id": 58, "name": "Inactive Recruiter", "active": False},
            ])
        if path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"professionIds":[77]' in content
            assert '"specialtyIds":[88]' in content
            assert '"primarySpecialtyId":88' in content
            assert '"recruiterId"' not in content
            return httpx.Response(201, json={"id": 709})
        if path.endswith("/candidates/709") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [77],
                "specialtyIds": [88] if patched else [20],
                "primarySpecialtyId": 88 if patched else 20,
                "recruiterId": 57 if patched else None,
            })
        if path.endswith("/candidates/709") and request.method == "PATCH":
            assert json.loads(request.content) == {
                "professionIds": [77], "specialtyIds": [88],
                "primarySpecialtyId": 88,
                "recruiterId": 57,
            }
            patched = True
            return httpx.Response(200, json={"id": 709})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(nexus_classification=selected), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert patched
    assert result["classification_updated"] is True


def test_selected_recruiter_is_sent_with_single_contact_candidate():
    selected = {
        "offering": "Physician", "profession": "Physician",
        "specialty": "Hematology/Oncology", "recruiter_id": 57,
    }

    def handler(request):
        path = request.url.path
        if path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 77, "name": "Physician", "active": True},
            ])
        if path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 88, "professionId": 77,
                 "name": "Hematology/Oncology", "active": True},
            ])
        if path.endswith("/master/recruiters"):
            return httpx.Response(200, json=[
                {"id": 57, "name": "Tina Mihic", "active": True},
            ])
        if path.endswith("/candidates") and request.method == "POST":
            profile = json.loads(request.content)
            assert profile["recruiterId"] == 57
            assert profile["primarySpecialtyId"] == 88
            return httpx.Response(201, json={"id": 710})
        if path.endswith("/candidates/710/upload/documents"):
            return httpx.Response(200, json={"uploadFailedDocumentNames": []})
        if path.endswith("/candidates/710") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [77], "specialtyIds": [88],
                "primarySpecialtyId": 88, "recruiterId": 57,
            })
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(emails=[], nexus_classification=selected), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 710
    assert result["classification_updated"] is False


def test_selected_classification_repairs_already_linked_nexus_candidate():
    payload = _payload(nexus_classification={
        "offering": "Physician", "profession": "Physician",
        "specialty": "Hematology/Oncology",
    })
    payload["nexus_candidate_id"] = "88"
    payload["classification_only"] = True
    patched = False

    def handler(request):
        nonlocal patched
        path = request.url.path
        if path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 10, "name": "RN", "active": True},
                {"professionId": 77, "name": "Physician", "active": True},
            ])
        if path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 20, "professionId": 10, "name": "ICU", "active": True},
                {"specialtyId": 88, "professionId": 77,
                 "name": "Hematology/Oncology", "active": True},
            ])
        if path.endswith("/candidates/88") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [77] if patched else [10],
                "specialtyIds": [88] if patched else [20],
                "primarySpecialtyId": 88 if patched else 20,
            })
        if path.endswith("/candidates/88") and request.method == "PATCH":
            assert json.loads(request.content) == {
                "professionIds": [77], "specialtyIds": [88], "primarySpecialtyId": 88,
            }
            patched = True
            return httpx.Response(200, json={"id": 88})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        payload, PDF, settings=settings, client=_client(settings, handler),
    )
    assert patched
    assert result["classification_updated"] is True
    assert result["action"] == "classification_refreshed"


def test_selected_classification_removes_other_linked_specialties():
    payload = _payload(nexus_classification={
        "offering": "Physician", "profession": "Physician",
        "specialty": "Hematology/Oncology",
    })
    payload["nexus_candidate_id"] = "88"
    payload["classification_only"] = True
    patched = False

    def handler(request):
        nonlocal patched
        path = request.url.path
        if path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 77, "name": "Physician", "active": True},
            ])
        if path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 88, "professionId": 77,
                 "name": "Hematology/Oncology", "active": True},
                {"specialtyId": 89, "professionId": 77,
                 "name": "Internal Medicine", "active": True},
            ])
        if path.endswith("/candidates/88") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [77],
                "specialtyIds": [88] if patched else [88, 89],
                "primarySpecialtyId": 88,
            })
        if path.endswith("/candidates/88") and request.method == "PATCH":
            assert json.loads(request.content)["specialtyIds"] == [88]
            patched = True
            return httpx.Response(200, json={"id": 88})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        payload, PDF, settings=settings, client=_client(settings, handler),
    )
    assert patched
    assert result["classification_updated"] is True


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


def test_unlinked_candidate_is_created_with_whitelisted_trusted_fields():
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
    assert searches == []
    assert result == {
        "status": "delivered",
        "action": "candidate_created",
        "nexus_candidate_id": 701,
        "matched_by": [],
        "resume_id": 7,
        "checksum_sha256": "a" * 64,
    }


@pytest.mark.parametrize(
    ("email", "phone", "present", "absent"),
    [
        ("jane@example.com", "", {"email", "primaryEmail"}, {"phone", "cellPhone"}),
        ("", "+16145550123", {"phone", "cellPhone"}, {"email", "primaryEmail"}),
    ],
)
def test_profile_omits_missing_contact_channel(email, phone, present, absent):
    profile = nexus_sync._build_profile(
        object(),
        {
            "firstName": "Jane", "middleName": "", "lastName": "Example",
            "email": email, "phone": phone, "city": "Columbus", "state": "OH",
            "country": "United States", "role": "Registered Nurse",
            "source_specialties": [], "resume_specialties": [], "specialties": [],
        },
        {
            "professionId": 10, "specialtyId": 20, "stateIds": {"OH": 30},
            "countryId": 40, "statusId": 50, "referralSourceId": 60,
            "jobTypeIds": ["PERM"],
        },
    )

    assert present <= profile.keys()
    assert absent.isdisjoint(profile.keys())


@pytest.mark.parametrize(
    ("emails", "phones", "present", "absent"),
    [
        (["jane@example.com"], [], "primaryEmail", "cellPhone"),
        ([], ["6145550123"], "cellPhone", "primaryEmail"),
    ],
)
def test_single_contact_uses_candidate_api_then_uploads_resume(
    emails, phones, present, absent,
):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/candidates"):
            profile = json.loads(request.content)
            assert present in profile
            assert absent not in profile
            assert "professionId" not in profile
            assert "specialtyId" not in profile
            assert profile["professionIds"] == [10]
            assert profile["primarySpecialtyId"] == 20
            return httpx.Response(201, json={"id": 701})
        if request.url.path.endswith("/candidates/701/upload/documents"):
            assert b"uploadedDocuments[0].documentTypeId" in request.content
            return httpx.Response(200, json={"uploadFailedDocumentNames": []})
        raise AssertionError(request.url)

    settings = _settings()
    linked = []
    result = nexus_sync.process_delivery(
        _payload(emails=emails, phones=phones), PDF,
        settings=settings, client=_client(settings, handler),
        after_create=linked.append,
    )
    assert linked == ["701"]
    assert result["nexus_candidate_id"] == 701
    assert result["action"] == "candidate_created"
    assert calls == [
        "/api/api-integration/v1/candidates",
        "/api/api-integration/v1/candidates/701/upload/documents",
    ]


def test_single_contact_resume_failure_keeps_created_candidate_id():
    def handler(request):
        if request.url.path.endswith("/candidates"):
            return httpx.Response(201, json={"id": 702})
        if request.url.path.endswith("/candidates/702/upload/documents"):
            return httpx.Response(500)
        raise AssertionError(request.url)

    settings = _settings()
    linked = []
    with pytest.raises(nexus_sync.NexusIndeterminateError) as raised:
        nexus_sync.process_delivery(
            _payload(emails=[]), PDF,
            settings=settings, client=_client(settings, handler),
            after_create=linked.append,
        )
    assert linked == ["702"]
    assert raised.value.nexus_candidate_id == "702"


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


def test_cross_profession_specialty_does_not_override_known_role():
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
            assert '"specialtyId":20' in content
            assert '"primarySpecialtyId":20' in content
            assert '"specialtyIds":[20]' in content
            assert '"professionId":10' in content
            assert '"professionIds":[10]' in content
            assert '"specialtyId":321' not in content
            return httpx.Response(201, json={"id": 705})
        if request.url.path.endswith("/candidates/705") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [10], "specialtyIds": [20],
                "primarySpecialtyId": 20,
            })
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(notes="Specialty: Urology"), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 705


@pytest.mark.parametrize(("job_title", "specialty"), [
    ("RN Case Manager", "Case Manager"),
    ("Hemodialysis Registered Nurse", "Dialysis"),
    ("RN ICU", "ICU"),
])
def test_current_role_supplies_approved_nexus_specialty(job_title, specialty):
    def handler(request):
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 321, "professionId": 10, "name": specialty, "active": True},
                {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"professionId":10' in content
            assert '"specialtyId":321' in content
            return httpx.Response(201, json={"id": 720})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(job_title=job_title), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 720


def test_source_specialty_precedes_role_in_nexus_classification():
    def handler(request):
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 321, "professionId": 10, "name": "Dialysis", "active": True},
                {"specialtyId": 322, "professionId": 10, "name": "ICU", "active": True},
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"specialtyId":321' in content
            assert '"specialtyIds":[321,322]' in content
            return httpx.Response(201, json={"id": 721})
        if request.url.path.endswith("/candidates/721") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [10], "specialtyIds": [321],
                "primarySpecialtyId": 321,
            })
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(job_title="RN ICU", notes="Specialty: Dialysis"), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 721
    assert result["classification_updated"] is False


def test_stored_resume_specialty_is_read_only_during_nexus_delivery():
    payload = _payload(job_title="Registered Nurse")
    payload["resume_extraction"] = {
        "fields": {"specialties": ["ICU", "OR"]},
        "confidence": {"specialties": 0.76},
        "accepted": {},
        "conflicts": [],
    }
    identity = nexus_sync._trusted_identity(payload)
    assert identity["source_specialties"] == []
    assert identity["resume_specialties"] == ["ICU"]
    payload["resume_extraction"]["conflicts"] = ["name"]
    assert nexus_sync._trusted_identity(payload)["resume_specialties"] == ["ICU"]


def test_resume_headline_disambiguates_multiple_nursing_units():
    payload = _payload(job_title="Registered Nurse")
    payload["resume_extraction"] = {
        "fields": {"job_title": "ICU Registered Nurse", "specialties": ["ICU", "Med Surg"]},
        "confidence": {"job_title": 0.82, "specialties": 0.76},
        "accepted": {}, "conflicts": [],
    }
    assert nexus_sync._trusted_identity(payload)["resume_specialties"] == ["ICU", "Med Surg"]


def test_new_candidate_creation_sends_nursing_resume_specialty_to_nexus():
    payload = _payload(job_title="Registered Nurse")
    payload["resume_extraction"] = {
        "fields": {"job_title": "ICU Registered Nurse", "specialties": ["ICU", "Med Surg"]},
        "confidence": {"job_title": 0.82, "specialties": 0.76},
        "accepted": {}, "conflicts": [],
    }

    def handler(request):
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 321, "professionId": 10, "name": "ICU", "active": True},
                {"specialtyId": 322, "professionId": 10, "name": "Med Surg", "active": True},
                {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            content = request.content.decode("latin-1")
            assert '"professionIds":[10]' in content
            assert '"specialtyIds":[321,322]' in content
            assert '"primarySpecialtyId":321' in content
            return httpx.Response(201, json={"id": 722})
        if request.url.path.endswith("/candidates/722") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [10], "specialtyIds": [321],
                "primarySpecialtyId": 321,
            })
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        payload, PDF, settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 722
    assert result["classification_updated"] is False


@pytest.mark.parametrize("stored_primary", [20, 999])
def test_new_candidate_specialty_is_corrected_when_nexus_changes_it(stored_primary):
    payload = _payload(job_title="Registered Nurse")
    payload["resume_extraction"] = {
        "fields": {"job_title": "ICU Registered Nurse", "specialties": ["ICU", "Med Surg"]},
        "confidence": {"job_title": 0.82, "specialties": 0.76},
        "accepted": {}, "conflicts": [],
    }
    patched = False

    def handler(request):
        nonlocal patched
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 321, "professionId": 10, "name": "ICU", "active": True},
                {"specialtyId": 322, "professionId": 10, "name": "Med Surg", "active": True},
                {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
                {"specialtyId": 999, "professionId": 10, "name": "Charge", "active": True},
            ])
        if request.url.path.endswith("/candidate/webhook/create"):
            return httpx.Response(201, json={"id": 723})
        if request.url.path.endswith("/candidates/723") and request.method == "GET":
            primary = 321 if patched else stored_primary
            return httpx.Response(200, json={
                "professionIds": [10], "specialtyIds": [primary],
                "primarySpecialtyId": primary,
            })
        if request.url.path.endswith("/candidates/723") and request.method == "PATCH":
            assert json.loads(request.content) == {
                "professionIds": [10], "specialtyIds": [321, 322],
                "primarySpecialtyId": 321,
            }
            patched = True
            return httpx.Response(200, json={"id": 723})
        raise AssertionError(request.url)

    settings = _settings()
    writes = []
    result = nexus_sync.process_delivery(
        payload, PDF, settings=settings, client=_client(settings, handler),
        before_write=writes.append,
    )

    assert result["classification_updated"] is True
    assert result["nexus_candidate_id"] == 723
    assert writes == ["candidate_creation"]


@pytest.mark.parametrize("stored_primary", [20, 999])
def test_linked_candidate_specialty_is_updated_from_new_resume(stored_primary):
    payload = _payload(job_title="Registered Nurse")
    payload["nexus_candidate_id"] = "88"
    payload["resume_extraction"] = {
        "fields": {"job_title": "ICU Registered Nurse", "specialties": ["ICU", "Med Surg"]},
        "confidence": {"job_title": 0.82, "specialties": 0.76},
        "accepted": {}, "conflicts": [],
    }
    patched = False

    def handler(request):
        nonlocal patched
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 321, "professionId": 10, "name": "ICU", "active": True},
                {"specialtyId": 322, "professionId": 10, "name": "Med Surg", "active": True},
                {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
                {"specialtyId": 999, "professionId": 10, "name": "Telemetry", "active": True},
            ])
        if request.url.path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 10, "name": "RN", "active": True},
                {"professionId": 99, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/candidates/88") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [10],
                "specialtyIds": [321, 322] if patched else [stored_primary],
                "primarySpecialtyId": 321 if patched else stored_primary,
            })
        if request.url.path.endswith("/candidates/88") and request.method == "PATCH":
            assert json.loads(request.content) == {
                "professionIds": [10], "specialtyIds": [321, 322], "primarySpecialtyId": 321,
            }
            patched = True
            return httpx.Response(200, json={"id": 88})
        if request.url.path.endswith("/candidates/88/upload/documents"):
            assert patched
            return httpx.Response(204)
        raise AssertionError(request.url)

    settings = _settings()
    writes = []
    result = nexus_sync.process_delivery(
        payload, PDF, settings=settings, client=_client(settings, handler),
        before_write=writes.append,
    )
    assert result["classification_updated"] is True
    assert result["action"] == "resume_uploaded"
    assert writes == ["candidate_classification_update"]


def test_linked_candidate_specific_specialty_is_preserved():
    payload = _payload(job_title="RN ICU")
    payload["nexus_candidate_id"] = "88"

    def handler(request):
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 321, "professionId": 10, "name": "ICU", "active": True},
                {"specialtyId": 322, "professionId": 10, "name": "Telemetry", "active": True},
                {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 10, "name": "RN", "active": True},
            ])
        if request.url.path.endswith("/candidates/88") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [10], "specialtyIds": [322],
                "primarySpecialtyId": 322,
            })
        if request.url.path.endswith("/candidates/88/upload/documents"):
            return httpx.Response(204)
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        payload, PDF, settings=settings, client=_client(settings, handler),
    )
    assert result["classification_updated"] is False


def test_unmatched_specialty_preserves_known_nursing_profession():
    def handler(request):
        if request.url.path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 10, "name": "RN", "active": True},
                {"professionId": 99, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 20, "professionId": 10, "name": "Unknown", "active": True},
                {"specialtyId": 199, "professionId": 99, "name": "Unknown", "active": True},
            ])
        raise AssertionError(request.url)

    settings = _settings(default_profile={
        "stateIds": {"OH": 30}, "countryId": 40, "statusId": 50,
        "referralSourceId": 60, "jobTypeIds": ["PERM"],
    })
    client = _client(settings, handler)
    identity = nexus_sync._trusted_identity(_payload(job_title="RN", specialty="Unlisted Unit"))
    profile = nexus_sync._build_profile(client, identity, settings.default_profile)
    assert profile["professionId"] == 10
    assert profile["specialtyId"] == 20


@pytest.mark.parametrize(
    ("role", "specialty", "expected_profession", "expected_specialty"),
    [
        ("Nurse Practitioner - Acute Care", "Nurse Practitioner - Acute Care", 12, 22),
        ("Physician", "ICU", 13, 23),
    ],
)
def test_unmatched_specialty_does_not_replace_known_profession(
    role, specialty, expected_profession, expected_specialty,
):
    def handler(request):
        if request.url.path.endswith("/master/professions"):
            return httpx.Response(200, json=[
                {"professionId": 12, "name": "Nurse Practitioner", "active": True},
                {"professionId": 13, "name": "Physician", "active": True},
                {"professionId": 10, "name": "RN", "active": True},
                {"professionId": 99, "name": "Unknown", "active": True},
            ])
        if request.url.path.endswith("/master/specialties"):
            return httpx.Response(200, json=[
                {"specialtyId": 22, "professionId": 12, "name": "Other", "active": True},
                {"specialtyId": 23, "professionId": 13, "name": "Other", "active": True},
                {"specialtyId": 20, "professionId": 10, "name": "ICU", "active": True},
                {"specialtyId": 199, "professionId": 99, "name": "Unknown", "active": True},
            ])
        raise AssertionError(request.url)

    settings = _settings(default_profile={
        "stateIds": {"OH": 30}, "countryId": 40, "statusId": 50,
        "referralSourceId": 60, "jobTypeIds": ["PERM"],
    })
    client = _client(settings, handler)
    identity = nexus_sync._trusted_identity(
        _payload(job_title=role, specialty=specialty)
    )
    profile = nexus_sync._build_profile(client, identity, settings.default_profile)
    assert profile["professionIds"] == [expected_profession]
    assert profile["primarySpecialtyId"] == expected_specialty


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
        if request.url.path.endswith("/candidates/708") and request.method == "GET":
            return httpx.Response(200, json={
                "professionIds": [10], "specialtyIds": [321],
                "primarySpecialtyId": 321,
            })
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(notes=f"Specialty: {source_label}"), PDF,
        settings=settings, client=_client(settings, handler),
    )
    assert result["nexus_candidate_id"] == 708
    assert result["classification_updated"] is False


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
    assert not any(
        request.url.path.endswith("/candidates/search") for request in requests
    )


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


def test_unlinked_candidate_does_not_search_contacts_before_creation():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/candidate/webhook/create"):
            return httpx.Response(201, json={"id": 88})
        raise AssertionError(request.url)

    settings = _settings()
    result = nexus_sync.process_delivery(
        _payload(), PDF, settings=settings, client=_client(settings, handler)
    )

    assert paths == ["/api/api-integration/v1/candidate/webhook/create"]
    assert result["action"] == "candidate_created"
    assert result["nexus_candidate_id"] == 88
    assert result["matched_by"] == []


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
    payload = _payload()
    payload["nexus_candidate_id"] = "88"
    with pytest.raises(nexus_sync.NexusPermanentError) as raised:
        nexus_sync.process_delivery(
            payload, PDF, settings=settings, client=_client(settings, handler),
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
    payload = _payload()
    payload["nexus_candidate_id"] = "88"
    result = nexus_sync.process_delivery(
        payload, PDF, settings=settings, client=_client(settings, handler),
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
