"""Resume capture must remain usable while Nexus classification is enabled."""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import os
import sys

import httpx
import pytest
from pypdf import PdfWriter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api as api_module
from sourcing import config, nexus_delivery, nexus_sync, store


@pytest.mark.parametrize("source", ["indeed", "vivian", "facebook", "npino"])
def test_resume_attach_and_download_with_nexus_enabled(source, monkeypatch, tmp_path):
    store.reset()
    monkeypatch.setattr(config, "RESUME_DOWNLOAD_DIR", tmp_path.resolve())
    monkeypatch.setattr(nexus_delivery, "enabled_for", lambda _user_id: True)
    candidate_id = store.add_candidate("Jane Example", "Columbus, OH", source=source)
    candidate = store.get_candidate(candidate_id)
    monkeypatch.setattr(
        api_module.contact_access,
        "project_candidate",
        lambda _candidate: {
            **candidate,
            "contacts_trusted": True,
            "emails": ["jane@example.test"],
            "phones": ["(614) 555-0123"],
        },
    )
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    pdf = output.getvalue()
    path = tmp_path / "resume.pdf"
    path.write_bytes(pdf)

    async def exercise():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            browser_body = {
                    "content_base64": base64.b64encode(pdf).decode("ascii"),
                    "filename": "browser.pdf",
                }
            if source == "indeed":
                browser_body["profile"] = {
                    "name": "Jane Example",
                    "location": "Columbus, OH",
                    "headline": "ICU Registered Nurse",
                    "roles": ["ICU Registered Nurse"],
                    "notes": "Work Experience\nICU Registered Nurse\nTelemetry Unit",
                    "source": "indeed",
                }
            for route, body in (
                ("from-browser", browser_body),
                ("from-download", {"path": str(path), "filename": path.name}),
            ):
                attached = await client.post(
                    f"/candidates/{candidate_id}/resume/{route}", json=body,
                )
                assert attached.status_code == 200, attached.text
                resume_id = attached.json()["resume"]["id"]
                downloaded = await client.get(
                    f"/candidates/{candidate_id}/resumes/{resume_id}"
                )
                assert downloaded.status_code == 200
                assert downloaded.content.startswith(b"%PDF")
            if source == "indeed":
                refreshed = store.get_candidate(candidate_id)
                assert "Specialty: ICU" in refreshed["notes"]
                assert nexus_sync._candidate_specialties(refreshed, {})[:2] == [
                    "ICU", "Telemetry",
                ]

    asyncio.run(exercise())
