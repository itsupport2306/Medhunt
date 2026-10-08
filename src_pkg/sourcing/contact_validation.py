"""Optional server-side email and phone validation.

API credentials never reach the browser extension. Calls are disabled by
default because email verification may consume credits and phone validation
still does not prove that a candidate owns a number.
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

from . import config


def verify_email(email: str) -> dict:
    if not config.VERIFY_EMAILS or not config.NEVERBOUNCE_API_KEY:
        return {"deliverability": "not_checked"}
    try:
        response = httpx.post(
            "https://api.neverbounce.com/v4.2/single/check",
            params={
                "key": config.NEVERBOUNCE_API_KEY,
                "email": email,
                "timeout": 10,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            return {"deliverability": "unknown", "service": "neverbounce"}
        return {
            "deliverability": str(payload.get("result") or "unknown"),
            "flags": payload.get("flags") or [],
            "service": "neverbounce",
        }
    except Exception as error:  # noqa: BLE001
        return {
            "deliverability": "unknown",
            "service": "neverbounce",
            "error": f"{type(error).__name__}: {error}",
        }


def verify_phone(phone: str) -> dict:
    username = config.TWILIO_API_KEY or config.TWILIO_ACCOUNT_SID
    password = config.TWILIO_API_KEY_SECRET or config.TWILIO_AUTH_TOKEN
    if not config.VERIFY_PHONES or not username or not password:
        return {"line_status": "not_checked", "identity_owner": "not_checked"}
    try:
        response = httpx.get(
            f"https://lookups.twilio.com/v2/PhoneNumbers/{quote(phone, safe='')}",
            params={"CountryCode": config.DEFAULT_PHONE_COUNTRY},
            auth=(username, password),
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        return {
            "line_status": "valid_range" if payload.get("valid") else "invalid",
            "formatted": payload.get("phone_number") or phone,
            "validation_errors": payload.get("validation_errors") or [],
            "identity_owner": "not_checked",
            "service": "twilio_lookup",
        }
    except Exception as error:  # noqa: BLE001
        return {
            "line_status": "unknown",
            "identity_owner": "not_checked",
            "service": "twilio_lookup",
            "error": f"{type(error).__name__}: {error}",
        }
