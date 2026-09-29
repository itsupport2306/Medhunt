"""Server-side Zoom Phone SMS client and webhook verification."""
from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time

import httpx

from . import config

_TOKEN_LOCK = threading.Lock()
_TOKEN = ""
_TOKEN_EXPIRES = 0.0


class ZoomSmsError(RuntimeError):
    pass


def enabled() -> bool:
    # Sender numbers and Zoom Phone user IDs are assigned per recruiter in
    # Halo, so the shared Medhunt environment only needs the Zoom account API.
    return bool(config.ZOOM_SMS_ENABLED)


def _access_token() -> str:
    global _TOKEN, _TOKEN_EXPIRES
    now = time.time()
    with _TOKEN_LOCK:
        if _TOKEN and _TOKEN_EXPIRES > now + 60:
            return _TOKEN
        basic = base64.b64encode(
            f"{config.ZOOM_CLIENT_ID}:{config.ZOOM_CLIENT_SECRET}".encode()
        ).decode()
        try:
            response = httpx.post(
                config.ZOOM_OAUTH_URL,
                params={"grant_type": "account_credentials", "account_id": config.ZOOM_ACCOUNT_ID},
                headers={"Authorization": f"Basic {basic}"},
                timeout=config.ZOOM_SMS_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ZoomSmsError(f"Zoom OAuth request failed ({type(exc).__name__}).") from exc
        if response.status_code >= 400:
            raise ZoomSmsError(f"Zoom OAuth failed ({response.status_code}).")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ZoomSmsError("Zoom OAuth returned an invalid response.") from exc
        _TOKEN = str(payload.get("access_token") or "")
        if not _TOKEN:
            raise ZoomSmsError("Zoom OAuth returned no access token.")
        _TOKEN_EXPIRES = now + max(300, int(payload.get("expires_in") or 3600))
        return _TOKEN


def send_sms(to_number: str, message: str, *, sender_number: str = "",
             sender_user_id: str = "") -> dict:
    if not enabled():
        raise ZoomSmsError("Zoom Phone SMS is not configured.")
    sender_number = str(sender_number or config.ZOOM_SMS_SENDER_NUMBER).strip()
    sender_user_id = str(sender_user_id or config.ZOOM_SMS_SENDER_USER_ID).strip()
    if not sender_number or not sender_user_id:
        raise ZoomSmsError("A Zoom Phone sender number has not been assigned to this user.")
    payload = {
        "message": str(message),
        "to_members": [{"phone_number": str(to_number)}],
        "sender": {"phone_number": sender_number},
    }
    try:
        response = httpx.post(
            f"{config.ZOOM_API_BASE_URL}/phone/sms/messages",
            params={"user_id": sender_user_id},
            json=payload,
            headers={"Authorization": f"Bearer {_access_token()}"},
            timeout=config.ZOOM_SMS_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ZoomSmsError(f"Zoom SMS request failed ({type(exc).__name__}).") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("message") or response.text
        except Exception:
            detail = response.text
        raise ZoomSmsError(f"Zoom SMS failed ({response.status_code}): {detail}"[:1000])
    if not response.content:
        return {}
    try:
        result = response.json()
    except ValueError as exc:
        raise ZoomSmsError("Zoom returned an invalid response.") from exc
    if isinstance(result, list):
        return dict(result[0]) if result and isinstance(result[0], dict) else {}
    return dict(result) if isinstance(result, dict) else {}


def validate_webhook(timestamp: str, raw_body: bytes, signature: str) -> bool:
    secret = config.ZOOM_WEBHOOK_SECRET_TOKEN
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    message = b"v0:" + str(timestamp).encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature))


def validation_token(plain_token: str) -> str:
    return hmac.new(
        config.ZOOM_WEBHOOK_SECRET_TOKEN.encode(),
        str(plain_token or "").encode(),
        hashlib.sha256,
    ).hexdigest()
