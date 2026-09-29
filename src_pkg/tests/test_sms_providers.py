from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api as api_module
from sourcing import config, store, zoom_sms


def test_zoom_webhook_signature(monkeypatch):
    monkeypatch.setattr(config, "ZOOM_WEBHOOK_SECRET_TOKEN", "test-secret")
    timestamp = str(int(time.time()))
    body = json.dumps({"event": "phone.sms_received"}, separators=(",", ":")).encode()
    signature = "v0=" + hmac.new(
        b"test-secret", b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256,
    ).hexdigest()

    assert zoom_sms.validate_webhook(timestamp, body, signature)
    assert not zoom_sms.validate_webhook(timestamp, body, "v0=invalid")


def test_zoom_send_and_cross_provider_duplicate_protection(monkeypatch):
    store.reset()
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA",
        notes="Role: Registered Nurse",
    )
    candidate = store.get_candidate(candidate_id)
    monkeypatch.setattr(
        api_module.contact_access,
        "project_candidate",
        lambda _candidate: {
            **candidate,
            "contacts_trusted": True,
            "phones": ["(404) 555-0100"],
            "phone_contacts": [{
                "value": "(404) 555-0100", "kind": "mobile", "label": "Mobile",
            }],
        },
    )
    monkeypatch.setattr(api_module.zoom_sms, "enabled", lambda: True)
    sent_messages = []
    monkeypatch.setattr(
        api_module.zoom_sms, "send_sms",
        lambda phone, message, **sender: sent_messages.append(
            (message, sender.get("sender_number"), sender.get("sender_user_id"))
        ) or {"message_id": "zoom-message-1"},
    )
    monkeypatch.setattr(config, "ZOOM_SMS_SENDER_NUMBER", "+14045550199")
    monkeypatch.setattr(config, "ZOOM_SMS_SENDER_USER_ID", "zoom-user-1")

    async def exercise():
        transport = httpx.ASGITransport(app=api_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status = await client.get("/messaging/status")
            assert status.status_code == 200
            assert status.json()["providers"]["zoom"] == {
                "enabled": True, "sender_configured": True,
            }
            preview = await client.get(
                f"/candidates/{candidate_id}/sms-preview",
                params={"phone": "(404) 555-0100"},
            )
            assert preview.status_code == 200
            assert "message" not in preview.json()
            assert "missing_fields" not in preview.json()
            payload = {
                "candidate_id": candidate_id,
                "phone": "(404) 555-0100",
                "message": "Custom recruiter message for Jane.",
                "provider": "zoom",
                "request_id": "zoom-request-1",
            }
            sent = await client.post("/messaging/sms", json=payload)
            assert sent.status_code == 200
            assert sent.json()["provider"] == "zoom"
            assert sent_messages == [(
                "Custom recruiter message for Jane.", "+14045550199", "zoom-user-1",
            )]
            assert sent.json()["message"]["body"] == "Custom recruiter message for Jane."
            duplicate = await client.post(
                "/messaging/sms",
                json={**payload, "request_id": "zoom-request-2"},
            )
            assert duplicate.status_code == 409

            empty_message = await client.post(
                "/messaging/sms",
                json={**payload, "message": "   ", "request_id": "zoom-request-3"},
            )
            assert empty_message.status_code == 400

    asyncio.run(exercise())
