from __future__ import annotations

from io import BytesIO
import os
import sys

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import resume_extraction
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
