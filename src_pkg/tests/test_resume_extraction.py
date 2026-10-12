from __future__ import annotations

from io import BytesIO
import os
import sys

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import resume_extraction, resume_enrichment
from sourcing import nexus_sync
from sourcing import store


@pytest.fixture(autouse=True)
def _enable_resume_ocr(monkeypatch):
    monkeypatch.setattr(resume_extraction.config, "RESUME_OCR_ENABLED", True)


def _pdf(lines: list[str]) -> bytes:
    output = BytesIO()
    page = canvas.Canvas(output, pagesize=letter, invariant=1)
    y = 740
    for line in lines:
        page.drawString(48, y, line)
        y -= 24
    page.showPage()
    page.save()
    return output.getvalue()


def test_text_resume_extracts_structured_fields_without_ocr(monkeypatch):
    def unexpected_ocr(_data, _indexes):
        raise AssertionError("text page must not invoke OCR")

    monkeypatch.setattr(resume_extraction, "_ocr_pages", unexpected_ocr)
    result = resume_extraction.extract(_pdf([
        "Jane Marie Example, RN",
        "Location: Columbus, OH",
        "Registered Nurse",
        "jane.example@example.com | (614) 555-0123",
        "Skills",
        "Critical Care | Telemetry | Patient Education",
        "Education",
        "Bachelor of Science in Nursing - Example University",
    ]), {})

    assert result["status"] == "extracted"
    assert result["source"] == "embedded_text"
    assert result["scanned"] is False
    assert result["pages_ocr"] == 0
    assert result["fields"]["full_name"] == "Jane Marie Example"
    assert result["fields"]["city"] == "Columbus"
    assert result["fields"]["state"] == "OH"
    assert result["fields"]["country"] == "United States"
    assert result["fields"]["emails"] == ["jane.example@example.com"]
    assert result["accepted"]["full_name"] == "Jane Marie Example"
    assert result["accepted"]["location"] == "Columbus, OH, United States"


def test_resume_specialties_keep_document_order_for_primary_choice():
    result = resume_extraction.extract(_pdf([
        "Med Surg Registered Nurse",
        "Experience",
        "Med Surg Unit, 2024-present",
        "ICU Unit, 2020-2023",
    ]), {})
    assert result["fields"]["specialties"][:2] == ["Med Surg", "ICU"]


def test_short_nursing_unit_acronyms_require_uppercase():
    ordinary = resume_extraction.extract(_pdf([
        "Registered Nurse",
        "ICU or telemetry experience for emergency patients",
    ]), {})
    assert "OR" not in ordinary["fields"]["specialties"]
    assert "ER" not in ordinary["fields"]["specialties"]
    clinical = resume_extraction.extract(_pdf([
        "OR Registered Nurse",
        "ER experience",
    ]), {})
    assert "OR" in clinical["fields"]["specialties"]
    assert "ER" in clinical["fields"]["specialties"]


def test_resume_upload_preparation_parses_specialty_and_reparses_old_schema():
    pdf = _pdf([
        "Jane Example",
        "Operating Room Registered Nurse",
        "OR and ICU experience",
    ])
    original, parsed, name = resume_enrichment.prepare_candidate_resume(
        pdf,
        {"name": "Jane Example"},
        {
            "schema_version": 1,
            "fields": {"full_name": "Jane Example"},
            "confidence": {"full_name": 0.95},
        },
    )

    assert original == pdf
    assert name == "Jane Example"
    assert parsed["schema_version"] == resume_extraction.SCHEMA_VERSION
    assert parsed["fields"]["specialties"][:3] == ["Operating Room", "OR", "ICU"]


def test_real_pdf_nursing_specialties_reach_nexus_profile():
    pdf = _pdf([
        "Jane Example",
        "Location: Columbus, OH",
        "ICU Registered Nurse",
        "Experience",
        "ICU Unit, 2024-present",
        "Med Surg Unit, 2020-2023",
    ])
    candidate = {
        "contacts_trusted": True, "name": "Jane Example",
        "location": "Columbus, OH", "job_title": "Registered Nurse",
        "emails": ["jane@example.com"], "phones": [],
    }
    identity = nexus_sync._trusted_identity({
        "candidate": candidate,
        "resume_extraction": resume_extraction.extract(pdf, candidate),
    })

    class MasterClient:
        def get_master(self, name):
            if name == "specialties":
                return [
                    {"specialtyId": 21, "professionId": 10, "name": "ICU", "active": True},
                    {"specialtyId": 22, "professionId": 10, "name": "MedSurg", "active": True},
                ]
            raise AssertionError(name)

    profile = nexus_sync._build_profile(MasterClient(), identity, {
        "professionId": 10, "specialtyId": 20, "stateIds": {"OH": 30},
        "countryId": 40, "statusId": 50, "referralSourceId": 60,
        "jobTypeIds": ["PERM"],
    })
    assert profile["specialtyIds"] == [21, 22]
    assert profile["primarySpecialtyId"] == 21


def test_image_only_resume_uses_local_ocr_for_sparse_pages(monkeypatch):
    scanned = _pdf([])
    calls = []

    def local_ocr(_data, indexes):
        calls.append(indexes)
        return {0: (
            "Jane Example, RN\n"
            "Location: Columbus, OH\n"
            "Registered Nurse\n"
            "jane.example@example.com\n"
        )}

    monkeypatch.setattr(resume_extraction, "_ocr_pages", local_ocr)
    result = resume_extraction.extract(scanned, {})

    assert calls == [[0]]
    assert result["status"] == "extracted"
    assert result["source"] == "local_ocr"
    assert result["scanned"] is True
    assert result["pages_ocr"] == 1
    assert result["fields"]["full_name"] == "Jane Example"
    assert result["fields"]["country"] == "United States"


def test_platform_identity_wins_and_conflicting_ocr_is_not_accepted(monkeypatch):
    monkeypatch.setattr(
        resume_extraction,
        "_ocr_pages",
        lambda _data, _indexes: {
            0: "Wrong Person\nLocation: Cleveland, OH\nRegistered Nurse\n"
        },
    )
    result = resume_extraction.extract(_pdf([]), {
        "name": "Jane Example",
        "location": "Columbus, OH",
        "job_title": "Registered Nurse",
    })

    assert result["conflicts"] == ["location", "name"]
    assert "full_name" not in result["accepted"]
    assert "location" not in result["accepted"]
    assert "job_title" not in result["accepted"]


def test_nexus_uses_only_preapproved_ocr_gap_fields():
    payload = {
        "candidate": {
            "contacts_trusted": True,
            "name": "Jane Example",
            "emails": ["jane@example.com"],
            "phones": ["614-555-0123"],
        },
        "resume_extraction": {
            "accepted": {
                "location": "Columbus, OH, United States",
                "city": "Columbus",
                "state": "OH",
                "country": "United States",
                "job_title": "Registered Nurse",
            },
            "fields": {
                "full_name": "Wrong Person",
                "city": "Cleveland",
            },
        },
    }

    identity = nexus_sync._trusted_identity(payload)
    assert identity["firstName"] == "Jane"
    assert identity["lastName"] == "Example"
    assert identity["city"] == "Columbus"
    assert identity["state"] == "OH"
    assert identity["country"] == "United States"
    assert identity["role"] == "Registered Nurse"


def test_ocr_contact_data_never_becomes_a_trusted_nexus_contact():
    payload = {
        "candidate": {
            "contacts_trusted": True,
            "name": "Jane Example",
            "location": "Columbus, OH",
        },
        "resume_extraction": {
            "accepted": {},
            "fields": {
                "emails": ["unverified@example.com"],
                "phones": ["614-555-9999"],
            },
        },
    }

    try:
        nexus_sync._trusted_identity(payload)
    except nexus_sync.NexusPermanentError as exc:
        assert "no deliverable trusted contact" in str(exc).lower()
    else:  # pragma: no cover - protects the security boundary explicitly
        raise AssertionError("OCR contacts bypassed the trusted-contact gate")


def test_resume_extraction_is_persisted_with_the_resume_atomically():
    candidate_id = store.add_candidate("OCR Storage Example", "Columbus, OH")
    extraction = {
        "schema_version": 1,
        "status": "extracted",
        "source": "local_ocr",
        "accepted": {"country": "United States"},
        "fields": {"country": "United States"},
    }
    resume = store.attach_resume(
        candidate_id,
        "scan.pdf",
        b"%PDF-test",
        checksum_sha256="b" * 64,
        extraction=extraction,
    )

    saved = store.get_resume_extraction(resume["id"], candidate_id)
    assert saved is not None
    assert saved["extraction"] == extraction


def test_location_header_is_not_taken_as_the_person_name():
    """A bare "City ST" header line must not become the candidate's name.

    Returning a place as the person's name conflicts with the captured
    platform identity, and that conflict discards every resume specialty
    before Nexus sees it, so the candidate is filed as Unknown.
    """
    pdf = _pdf([
        "Dedicated and compassionate Registered Nurse with progressive",
        "career history in direct patient care and care coordination.",
        "Manchester KY",
        "Experience",
        "Med Surg Unit, 2024-present",
        "ICU Unit, 2020-2023",
    ])
    candidate = {
        "contacts_trusted": True, "name": "Ginny Minton",
        "location": "Manchester, KY", "job_title": "Registered Nurse",
        "emails": ["ginny@example.com"], "phones": [],
    }
    result = resume_extraction.extract(pdf, candidate)

    assert not result["fields"].get("full_name")
    assert "name" not in result["conflicts"]

    identity = nexus_sync._trusted_identity({
        "candidate": candidate, "resume_extraction": result,
    })
    assert identity["firstName"] == "Ginny"
    assert identity["lastName"] == "Minton"
    assert identity["specialties"], "resume specialties must reach Nexus"


def test_middle_dot_header_extracts_the_person_name():
    """Headers use U+00B7; splitting only on U+2022 loses the whole line."""
    result = resume_extraction.extract(_pdf([
        "Kimberly Gaiser, BSN, RN  \u00b7 Florence, KY 41042",
        "Professional Summary",
        "Emergency registered nurse",
        "ER Unit, 2022-present",
    ]), {})
    assert result["fields"]["full_name"] == "Kimberly Gaiser"


def test_name_printed_one_word_per_line_is_joined():
    """Header layouts that stack one word per line never meet the 2-word
    minimum on a single line, so the name used to be lost entirely."""
    result = resume_extraction.extract(_pdf([
        "AMANDA",
        "WILBUR",
        "REGISTERED",
        "NURSE",
        "BSN",
        "Experience",
        "ICU Unit, 2024-present",
    ]), {})
    assert result["fields"]["full_name"] == "AMANDA WILBUR"


def test_wrapped_summary_prose_is_never_returned_as_a_person_name():
    """Rejecting the location header lets the scan continue into the summary
    paragraph; a clause such as "and monitored responses." must not win."""
    result = resume_extraction.extract(_pdf([
        "Dedicated and compassionate Registered Nurse",
        "and monitored responses.",
        "optimal healing and comfort.",
        "Experience",
    ]), {})
    assert not result["fields"].get("full_name")
    assert result["conflicts"] == []


def test_contact_only_candidate_still_gets_a_nexus_name_from_the_resume():
    """Nexus requires a first and last name, so a candidate captured with
    nothing but an email or a phone depends entirely on the resume header."""
    pdf = _pdf([
        "AMANDA",
        "WILBUR",
        "REGISTERED",
        "NURSE",
        "Experience",
        "ICU Unit, 2024-present",
    ])
    identity = nexus_sync._trusted_identity({
        "candidate": {
            "contacts_trusted": True, "name": "",
            "emails": ["amanda@example.com"], "phones": [],
        },
        "resume_extraction": resume_extraction.extract(pdf, {}),
    })
    assert identity["firstName"] == "AMANDA"
    assert identity["lastName"] == "WILBUR"
    assert identity["email"] == "amanda@example.com"
