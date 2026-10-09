from __future__ import annotations

import hashlib
import os
import sys
from io import BytesIO

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import config, nexus_delivery, nexus_sync, store


def _resume_pdf(name="Jane Doe"):
    from reportlab.pdfgen import canvas

    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(72, 720, name)
    pdf.drawString(72, 690, "Registered Nurse")
    pdf.save()
    return output.getvalue()


def _enable_nexus_delivery(candidate_id):
    return store.set_candidate_delivery_route(
        candidate_id, "test-user", nexus=True,
    )


def test_role_prefers_clinical_resume_role_over_mislabeled_employer():
    candidate = {
        "name": "Candace Robertson",
        "location": "Lima, OH",
        "source": "indeed",
        "notes": (
            "Headline: SHAWNEE MANOR\n"
            "Role: SHAWNEE MANOR\n"
            "Candace Robertson\n"
            "Lima, OH\n"
            "RN\n"
            "SHAWNEE MANOR\n"
            "(2023–Present)"
        ),
    }

    assert nexus_delivery._role(candidate) == "RN"
import api as api_module


def test_resume_and_nexus_outbox_commit_together(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")

    resume = store.attach_resume(
        candidate_id,
        "jane.pdf",
        b"%PDF-test",
        checksum_sha256="a" * 64,
        queue_nexus=True,
    )
    delivery = store.get_nexus_delivery_for_resume(resume["id"])

    assert resume["nexus_sync_status"] == "pending"
    assert delivery["candidate_id"] == candidate_id
    assert delivery["resume_checksum"] == "a" * 64
    assert delivery["identity_key"] == f"master:{candidate_id}"
    assert delivery["status"] == "pending"


def test_nexus_outbox_claim_retry_and_link_are_idempotent(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    resume = store.attach_resume(
        candidate_id, "jane.pdf", b"%PDF-test", queue_nexus=True,
    )

    first = store.claim_nexus_delivery()
    assert first["resume_id"] == resume["id"]
    assert first["status"] == "processing"
    assert first["attempts"] == 1

    store.finish_nexus_delivery(first["id"], "retry", error="temporary", retry_at=0)
    second = store.claim_nexus_delivery()
    assert second["id"] == first["id"]
    assert second["attempts"] == 2

    link = store.save_nexus_candidate_link(
        second["identity_key"], candidate_id, "9001",
    )
    repeated = store.save_nexus_candidate_link(
        second["identity_key"], candidate_id, "9001",
    )
    assert link["nexus_candidate_id"] == "9001"
    assert repeated["nexus_candidate_id"] == "9001"

    store.finish_nexus_delivery(
        second["id"], "succeeded", nexus_candidate_id="9001",
        operation="created_candidate",
    )
    assert store.claim_nexus_delivery() is None
    assert store.get_nexus_delivery_for_resume(resume["id"])["status"] == "succeeded"


def test_root_and_duplicate_share_one_nexus_identity(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    root_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    duplicate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="linkedin")
    store.update_candidate(duplicate_id, master_candidate_id=root_id)

    root_resume = store.attach_resume(
        root_id, "root.pdf", b"%PDF-root", queue_nexus=True,
    )
    duplicate_resume = store.attach_resume(
        duplicate_id, "duplicate.pdf", b"%PDF-duplicate", queue_nexus=True,
    )

    root_delivery = store.get_nexus_delivery_for_resume(root_resume["id"])
    duplicate_delivery = store.get_nexus_delivery_for_resume(duplicate_resume["id"])
    assert root_delivery["identity_key"] == f"master:{root_id}"
    assert duplicate_delivery["identity_key"] == f"master:{root_id}"


def test_stale_worker_cannot_ack_a_released_nexus_lease(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    resume = store.attach_resume(
        candidate_id, "jane.pdf", b"%PDF-test", queue_nexus=True,
    )
    claimed = store.claim_nexus_delivery(lease_seconds=600)

    assert store.finish_nexus_delivery(
        claimed["id"], "succeeded", nexus_candidate_id="7001",
        expected_lease_until=claimed["lease_until"] + 1,
    ) is False
    assert store.get_nexus_delivery_for_resume(resume["id"])["status"] == "processing"
    assert store.finish_nexus_delivery(
        claimed["id"], "succeeded", nexus_candidate_id="7001",
        expected_lease_until=claimed["lease_until"],
    ) is True


def test_expired_prewrite_lease_is_reclaimed_before_same_identity_pending(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    first_resume = store.attach_resume(
        candidate_id, "first.pdf", b"%PDF-first", checksum_sha256="a" * 64,
        queue_nexus=True,
    )
    store.attach_resume(
        candidate_id, "second.pdf", b"%PDF-second", checksum_sha256="b" * 64,
        queue_nexus=True,
    )
    first = store.claim_nexus_delivery(lease_seconds=600)
    with store._conn() as connection:
        connection.execute(
            "UPDATE nexus_deliveries SET lease_until=0 WHERE id=?", (first["id"],),
        )

    reclaimed = store.claim_nexus_delivery(lease_seconds=600)
    assert reclaimed["id"] == first["id"]
    assert reclaimed["resume_id"] == first_resume["id"]


def test_expired_writing_delivery_becomes_indeterminate_not_replayed(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    resume = store.attach_resume(
        candidate_id, "jane.pdf", b"%PDF-test", queue_nexus=True,
    )
    claimed = store.claim_nexus_delivery(lease_seconds=600)
    assert store.mark_nexus_delivery_writing(
        claimed["id"], expected_lease_until=claimed["lease_until"],
        operation="resume_upload",
    ) is True
    with store._conn() as connection:
        connection.execute(
            "UPDATE nexus_deliveries SET lease_until=0 WHERE id=?", (claimed["id"],),
        )

    assert store.claim_nexus_delivery(lease_seconds=600) is None
    delivery = store.get_nexus_delivery_for_resume(resume["id"])
    assert delivery["status"] == "indeterminate"
    assert "outcome_unknown" in delivery["last_error"]


def test_identity_rekey_migrates_existing_link_and_delivery(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    root_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    child_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="linkedin")
    resume = store.attach_resume(
        child_id, "jane.pdf", b"%PDF-test", queue_nexus=True,
    )
    store.save_nexus_candidate_link(f"master:{child_id}", child_id, "7001")

    store.update_candidate(child_id, master_candidate_id=root_id)

    delivery = store.get_nexus_delivery_for_resume(resume["id"])
    assert delivery["identity_key"] == f"master:{root_id}"
    assert store.get_nexus_candidate_link(f"master:{root_id}")[
        "nexus_candidate_id"
    ] == "7001"
    assert store.get_nexus_candidate_link(f"master:{child_id}") is None


def test_attach_resume_checksum_lock_returns_one_row_and_one_outbox(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    first = store.attach_resume(
        candidate_id, "jane.pdf", b"%PDF-test", checksum_sha256="c" * 64,
        queue_nexus=True,
    )
    second = store.attach_resume(
        candidate_id, "jane-copy.pdf", b"%PDF-test", checksum_sha256="c" * 64,
        queue_nexus=True,
    )

    assert second["id"] == first["id"]
    assert second["deduplicated"] is True
    assert len(store.list_resumes(candidate_id)) == 1
    assert len(store.list_nexus_deliveries(candidate_id)) == 1


def test_oversized_nexus_resume_is_saved_but_not_queued(monkeypatch):
    from pypdf import PdfWriter

    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    monkeypatch.setattr(config, "NEXUS_MAX_RESUME_BYTES", 10)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _enable_nexus_delivery(candidate_id)
    candidate = store.get_candidate(candidate_id)
    monkeypatch.setattr(
        api_module.contact_access,
        "project_candidate",
        lambda _candidate: {
            **candidate,
            "contacts_trusted": True,
            "emails": ["jane@example.test"],
            "phones": ["(404) 555-0199"],
        },
    )
    source = BytesIO()
    source.write(_resume_pdf())

    result = api_module._store_resume_pdf(candidate_id, "jane.pdf", source.getvalue())

    assert result["nexus_sync_status"] == "skipped_resume_too_large"
    assert store.get_nexus_delivery_for_resume(result["id"]) is None


def test_resume_is_not_queued_without_explicit_contact_gate(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    resume = store.attach_resume(candidate_id, "jane.pdf", b"%PDF-test")

    assert resume["nexus_sync_status"] == "disabled"
    assert store.get_nexus_delivery_for_resume(resume["id"]) is None


def test_existing_latest_resume_is_queued_when_trusted_contacts_arrive(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _enable_nexus_delivery(candidate_id)
    older = store.attach_resume(
        candidate_id, "older.pdf", b"%PDF-older", checksum_sha256="a" * 64,
    )
    latest = store.attach_resume(
        candidate_id, "latest.pdf", b"%PDF-latest", checksum_sha256="b" * 64,
    )
    candidate = store.get_candidate(candidate_id)
    monkeypatch.setattr(
        nexus_delivery.contact_access,
        "project_candidate",
        lambda _candidate: {
            **candidate,
            "contacts_trusted": True,
            "emails": ["jane@example.test"],
            "phones": ["(404) 555-0199"],
        },
    )

    queued = nexus_delivery.queue_latest_resume_if_ready(candidate_id)

    assert queued["resume_id"] == latest["id"]
    assert queued["status"] == "pending"
    assert store.get_nexus_delivery_for_resume(older["id"]) is None


def test_approved_quick_sourcer_record_queues_existing_resume(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    monkeypatch.setattr(config, "QUICK_SOURCER_TRUSTED_FOR_SYNC", True)
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", source="indeed", source_id="qs-sync",
    )
    _enable_nexus_delivery(candidate_id)
    latest = store.attach_resume(
        candidate_id, "latest.pdf", b"%PDF-latest", checksum_sha256="d" * 64,
    )
    store.update_candidate(
        candidate_id,
        emails=["jane@example.test"],
        phones=["(404) 555-0199"],
        contact_expires_at=9999999999,
        verification={
            "source": "quick_sourcer",
            "record": {
                "phones": [{"value": "(404) 555-0199", "type": "Wireless"}],
            },
        },
    )

    queued = nexus_delivery.queue_latest_resume_if_ready(candidate_id)

    assert queued["resume_id"] == latest["id"]
    assert queued["status"] == "pending"


@pytest.mark.parametrize(
    ("emails", "phones"),
    [(["jane@example.test"], []), ([], ["(404) 555-0199"])],
)
def test_existing_latest_resume_queues_with_either_contact(monkeypatch, emails, phones):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _enable_nexus_delivery(candidate_id)
    latest = store.attach_resume(
        candidate_id, "latest.pdf", b"%PDF-latest", checksum_sha256="e" * 64,
    )
    candidate = store.get_candidate(candidate_id)
    monkeypatch.setattr(
        nexus_delivery.contact_access,
        "project_candidate",
        lambda _candidate: {
            **candidate,
            "contacts_trusted": True,
            "emails": emails,
            "phones": phones,
        },
    )

    queued = nexus_delivery.queue_latest_resume_if_ready(candidate_id)

    assert queued["resume_id"] == latest["id"]
    assert queued["status"] == "pending"


def test_worker_delivers_projected_contact_and_persists_link(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", source="indeed", notes="Role: Registered Nurse",
    )
    resume_pdf = _resume_pdf()
    resume = store.attach_resume(
        candidate_id, "jane.pdf", resume_pdf, queue_nexus=True,
        checksum_sha256="b" * 64,
    )
    candidate = store.get_candidate(candidate_id)
    projected = {
        **candidate,
        "contacts_trusted": True,
        "emails": ["jane@example.test"],
        "phones": ["(404) 555-0199"],
        "phone_contacts": [{"value": "(404) 555-0199", "kind": "mobile"}],
    }
    monkeypatch.setattr(
        nexus_delivery.contact_access, "project_candidate", lambda _candidate: projected,
    )
    captured = {}

    def deliver(payload, resume_pdf, *, before_write=None):
        captured.update(payload)
        captured["uploaded_resume_pdf"] = resume_pdf
        assert resume_pdf.startswith(b"%PDF")
        assert before_write is not None
        before_write("candidate_creation")
        return {
            "action": "candidate_created",
            "nexus_candidate_id": 7001,
        }

    monkeypatch.setattr(nexus_delivery.nexus_sync, "process_delivery", deliver)

    assert nexus_delivery.process_once() == {
        "status": "succeeded", "delivery_id": 1,
    }
    assert captured["candidate"]["latest_phone"] == "(404) 555-0199"
    assert captured["candidate"]["primary_email"] == "jane@example.test"
    assert captured["candidate"]["job_title"] == "Registered Nurse"
    assert captured["resume"]["checksum_sha256"] == hashlib.sha256(
        captured["uploaded_resume_pdf"]
    ).hexdigest()
    delivery = store.get_nexus_delivery_for_resume(resume["id"])
    assert delivery["status"] == "succeeded"
    assert delivery["nexus_candidate_id"] == "7001"
    assert store.get_nexus_candidate_link(delivery["identity_key"])[
        "nexus_candidate_id"
    ] == "7001"


def test_worker_delivers_source_name_mismatch_with_candidate_cover(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    _enable_nexus_delivery(candidate_id)
    resume = store.attach_resume(
        candidate_id, "wrong-person.pdf", _resume_pdf("Jennifer Merlo"),
        queue_nexus=True,
    )
    monkeypatch.setattr(
        nexus_delivery.contact_access,
        "project_candidate",
        lambda candidate: {
            **candidate,
            "contacts_trusted": True,
            "emails": ["jane@example.test"],
            "phones": ["(404) 555-0199"],
        },
    )

    uploaded = {}

    def upload(_payload, resume_pdf, *, before_write=None):
        from pypdf import PdfReader

        uploaded["pages"] = [
            page.extract_text() or ""
            for page in PdfReader(BytesIO(resume_pdf)).pages
        ]
        before_write("candidate_creation")
        return {"action": "candidate_created", "nexus_candidate_id": "7002"}

    monkeypatch.setattr(nexus_delivery.nexus_sync, "process_delivery", upload)

    result = nexus_delivery.process_once()
    delivery = store.get_nexus_delivery_for_resume(resume["id"])
    assert result["status"] == "succeeded"
    assert delivery["status"] == "succeeded"
    assert "Jane Doe" in uploaded["pages"][0]
    assert "Jennifer Merlo" in uploaded["pages"][1]


def test_worker_holds_duplicate_conflict_for_review(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    resume = store.attach_resume(
        candidate_id, "jane.pdf", _resume_pdf(), queue_nexus=True,
    )
    monkeypatch.setattr(
        nexus_delivery.contact_access,
        "project_candidate",
        lambda candidate: {
            **candidate,
            "contacts_trusted": True,
            "emails": ["jane@example.test"],
            "phones": ["(404) 555-0199"],
            "phone_contacts": [{"value": "(404) 555-0199", "kind": "mobile"}],
        },
    )

    def conflict(_payload, _resume_pdf, *, before_write=None):
        raise nexus_sync.NexusIndeterminateError(
            "Nexus email and phone belong to different candidates.",
            operation="duplicate_search",
        )

    monkeypatch.setattr(nexus_delivery.nexus_sync, "process_delivery", conflict)

    result = nexus_delivery.process_once()
    delivery = store.get_nexus_delivery_for_resume(resume["id"])
    assert result["status"] == "review"
    assert delivery["status"] == "review"
    assert "different candidates" in delivery["last_error"]


def test_worker_holds_remote_success_when_identity_link_conflicts(monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "NEXUS_SYNC_ENABLED", True)
    first_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    second_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="linkedin")
    store.save_nexus_candidate_link(f"candidate:{first_id}", first_id, "7001")
    resume = store.attach_resume(
        second_id, "jane.pdf", _resume_pdf(), queue_nexus=True,
    )
    monkeypatch.setattr(
        nexus_delivery.contact_access,
        "project_candidate",
        lambda candidate: {
            **candidate,
            "contacts_trusted": True,
            "emails": ["jane@example.test"],
            "phones": ["(404) 555-0199"],
        },
    )
    def remote_success(_payload, _pdf, *, before_write=None):
        assert before_write is not None
        before_write("resume_upload")
        return {"action": "resume_uploaded", "nexus_candidate_id": "7001"}

    monkeypatch.setattr(
        nexus_delivery.nexus_sync, "process_delivery", remote_success,
    )

    result = nexus_delivery.process_once()
    delivery = store.get_nexus_delivery_for_resume(resume["id"])

    assert result == {"status": "review", "delivery_id": delivery["id"]}
    assert delivery["status"] == "review"
    assert delivery["nexus_candidate_id"] == "7001"
    assert "manual reconciliation" in delivery["last_error"]
