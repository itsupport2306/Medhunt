"""Private, idempotency-aware delivery of candidates and resumes to Nexus.

The browser extension must never know Nexus credentials or tenant routing IDs.
This module therefore accepts only the backend's already-sanitized candidate
projection and the final enriched resume PDF.  Provider responses, match
evidence, and API credentials are deliberately excluded from every outbound
candidate payload and from public exception messages.

``process_delivery`` is synchronous so a durable outbox worker can decide when
to acknowledge, retry, or send a delivery for manual review. It creates a
candidate directly through the configured Nexus insert endpoint when there is
no saved Nexus link, then uploads the resume. Existing saved links are reused.
It does not search Nexus for duplicate candidates.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import httpx

from . import nexus_taxonomy, verification
from .person_name import identity_signature, is_name_suffix, normalize_person_name


_SURNAME_PARTICLES = {"da", "de", "del", "della", "der", "di", "du", "la", "le", "van", "von"}

# Public profile sites and Nexus occasionally use different labels for the
# same clinical specialty. Keep this list deliberately small and semantic:
# exact Nexus labels are always tried first, then only reviewed equivalents.
# Any value that still cannot be resolved uses Nexus's explicit Unknown pair
# instead of a guessed specialty from an unrelated profession.
_SPECIALTY_ALIASES: Mapping[str, tuple[str, ...]] = {
    "family medicine": ("Family Practice", "Family Practice/Primary Care"),
    "family practice": ("Family Medicine", "Family Practice/Primary Care"),
    "primary care": ("Family Practice/Primary Care", "Family Practice"),
    "thoracic surgery": ("Surgery-Thoracic", "CardioThoracic Surgery"),
    "cardiothoracic surgery": ("CardioThoracic Surgery", "Surgery-Thoracic"),
    "ob gyn": ("Obstetrics & Gynecology",),
    "obgyn": ("Obstetrics & Gynecology",),
    "obstetrics and gynecology": ("Obstetrics & Gynecology",),
    "or": ("Operating Room",),
    "er": ("Emergency Room",),
    "med surg": ("MedSurg",),
    "medical surgical": ("MedSurg",),
    "emergency": ("ER",),
    "endoscopy": ("GI / Endoscopy",),
}

_ROLE_SPECIALTY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Sterile Processing Tech (SPT)", re.compile(r"\bsterile\s+processing\s+tech(?:nician)?\b", re.I)),
    ("Interventional Radiology Tech", re.compile(r"\binterventional\s+radiology\s+tech(?:nician|nologist)?\b", re.I)),
    ("CT Tech", re.compile(r"\bct\s+tech(?:nician|nologist)?\b", re.I)),
    ("MRI Tech", re.compile(r"\bmri\s+tech(?:nician|nologist)?\b", re.I)),
    ("X-Ray Tech", re.compile(r"\bx[ -]?ray\s+tech(?:nician|nologist)?\b", re.I)),
    ("X-Ray Tech", re.compile(r"\bradiologic\s+technolog(?:ist|y)\b", re.I)),
    ("Long Term Acute Care (LTAC)", re.compile(r"\b(?:ltac|long\s+term\s+acute\s+care)\b", re.I)),
    ("MedSurg / Tele", re.compile(r"\bmed(?:ical)?[ -]?surg(?:ical)?.{0,20}\btele(?:metry)?\b", re.I)),
    ("MedSurg", re.compile(r"\bmed(?:ical)?[ -]?surg(?:ical)?\b", re.I)),
    ("Post-Partum", re.compile(r"\bpost[ -]?partum\b", re.I)),
    ("Psychiatric-Mental Health", re.compile(r"\b(?:psychiatric[ -]?mental\s+health|mental\s+health\s+nurse\s+practitioner|pmhnp)\b", re.I)),
    ("Dialysis", re.compile(r"\bhemodialysis\b", re.I)),
)


class NexusDeliveryError(RuntimeError):
    """Base class with a safe message suitable for an outbox status row."""

    category = "delivery_error"
    default_code = "nexus_delivery_error"

    def __init__(
        self,
        message: str,
        *,
        operation: str = "delivery",
        status_code: int | None = None,
        retry_after: float | None = None,
        code: str | None = None,
    ) -> None:
        # Do not retain response bodies or request URLs: they can contain
        # gateway diagnostics, candidate data, or tenant information.
        super().__init__(message)
        self.operation = operation
        self.status_code = status_code
        self.retry_after = retry_after
        self.code = code or self.default_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "code": self.code,
            "message": str(self),
            "operation": self.operation,
            "status_code": self.status_code,
            "retry_after": self.retry_after,
        }


class NexusRetryableError(NexusDeliveryError):
    """The request was read-only or known not to have committed; retry later."""

    category = "retryable"
    default_code = "nexus_retryable"


class NexusPermanentError(NexusDeliveryError):
    """Configuration or payload must be corrected before another attempt."""

    category = "permanent"
    default_code = "nexus_permanent"


class NexusIndeterminateError(NexusDeliveryError):
    """Automatic retry could duplicate or modify the wrong remote candidate."""

    category = "indeterminate"
    default_code = "nexus_indeterminate"


@dataclass(frozen=True)
class NexusSettings:
    enabled: bool = False
    base_url: str = "https://api-nexus.laboredge.com"
    auth_method: str = "static"
    token_url: str = ""
    token_payload_style: str = "form"
    client_id: str = ""
    client_secret: str = ""
    username: str = ""
    password: str = ""
    static_token: str = ""
    org_code: str = ""
    token_basic: str = ""
    resume_doc_type_id: str = ""
    default_profile: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    connect_timeout_seconds: float = 15.0
    master_cache_seconds: int = 600
    max_resume_bytes: int = 10 * 1024 * 1024

    @classmethod
    def from_config(cls, config_module: Any | None = None) -> "NexusSettings":
        if config_module is None:
            from . import config as config_module  # local import avoids cycles

        defaults = getattr(config_module, "NEXUS_DEFAULT_PROFILE", {}) or {}
        if isinstance(defaults, str):
            try:
                defaults = json.loads(defaults)
            except ValueError as exc:
                raise NexusPermanentError(
                    "Nexus default profile is not valid JSON.",
                    operation="configuration",
                ) from exc
        if not isinstance(defaults, Mapping):
            raise NexusPermanentError(
                "Nexus default profile must be a JSON object.",
                operation="configuration",
            )
        return cls(
            enabled=bool(
                getattr(
                    config_module,
                    "NEXUS_SYNC_ENABLED",
                    getattr(config_module, "NEXUS_ENABLED", False),
                )
            ),
            base_url=str(
                getattr(
                    config_module, "NEXUS_API_BASE_URL",
                    getattr(config_module, "NEXUS_BASE_URL", ""),
                ) or ""
            ).rstrip("/"),
            auth_method=str(getattr(config_module, "NEXUS_AUTH_METHOD", "static") or "static").lower(),
            token_url=str(getattr(config_module, "NEXUS_TOKEN_URL", "") or ""),
            token_payload_style=str(
                getattr(config_module, "NEXUS_TOKEN_PAYLOAD_STYLE", "form") or "form"
            ).lower(),
            client_id=str(getattr(config_module, "NEXUS_CLIENT_ID", "") or ""),
            client_secret=str(getattr(config_module, "NEXUS_CLIENT_SECRET", "") or ""),
            username=str(getattr(config_module, "NEXUS_USERNAME", "") or ""),
            password=str(getattr(config_module, "NEXUS_PASSWORD", "") or ""),
            static_token=str(getattr(config_module, "NEXUS_STATIC_TOKEN", "") or ""),
            org_code=str(getattr(config_module, "NEXUS_ORG_CODE", "") or ""),
            token_basic=str(getattr(config_module, "NEXUS_TOKEN_BASIC", "") or ""),
            resume_doc_type_id=str(
                getattr(config_module, "NEXUS_RESUME_DOC_TYPE_ID", "") or ""
            ),
            default_profile=dict(defaults),
            timeout_seconds=max(
                3.0, float(getattr(config_module, "NEXUS_TIMEOUT", 120.0) or 120.0)
            ),
            connect_timeout_seconds=max(
                1.0,
                float(getattr(config_module, "NEXUS_CONNECT_TIMEOUT", 15.0) or 15.0),
            ),
            master_cache_seconds=max(
                0, int(getattr(config_module, "NEXUS_MASTER_CACHE_SECONDS", 600) or 0)
            ),
            max_resume_bytes=max(
                1,
                int(
                    getattr(
                        config_module,
                        "NEXUS_MAX_RESUME_BYTES",
                        10 * 1024 * 1024,
                    )
                    or 10 * 1024 * 1024
                ),
            ),
        )


def _jwt_exp(token: str) -> int | None:
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        return int(payload["exp"])
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        binascii.Error,
    ):
        return None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After", "").strip()
    try:
        return max(0.0, float(raw)) if raw else None
    except ValueError:
        return None


def _response_error(
    response: httpx.Response,
    *,
    operation: str,
    write: bool,
) -> NexusDeliveryError:
    status = response.status_code
    message = f"Nexus {operation} returned HTTP {status}."
    common = {
        "operation": operation,
        "status_code": status,
        "retry_after": _retry_after(response),
    }
    if status in (429, 425) or (status == 408 and not write):
        return NexusRetryableError(message, **common)
    if status >= 500:
        cls = NexusIndeterminateError if write else NexusRetryableError
        return cls(message, **common)
    if write and status in (408, 409):
        return NexusIndeterminateError(message, **common)
    return NexusPermanentError(message, **common)


class NexusClient:
    MASTER_LISTS = {
        "professions",
        "specialties",
        "states",
        "countries",
        "referralsources",
        "candidatestatuses",
        "documenttypes",
    }

    def __init__(
        self,
        settings: NexusSettings,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(
                settings.timeout_seconds,
                connect=settings.connect_timeout_seconds,
            )
        )
        self._token = ""
        self._token_expires_at = 0.0
        self._refresh_token = ""
        self._token_verifier = ""
        self._auth_lock = threading.Lock()
        self._master_lock = threading.Lock()
        self._master_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def close(self) -> None:
        self._http.close()

    def _validate_auth(self) -> None:
        method = self.settings.auth_method
        if method == "static":
            if not self.settings.static_token:
                raise NexusPermanentError(
                    "Nexus static authentication is not configured.",
                    operation="authentication",
                )
            return
        if method not in {"password", "client_credentials"}:
            raise NexusPermanentError(
                "Nexus authentication mode is not supported.",
                operation="authentication",
            )
        if not self.settings.token_url:
            raise NexusPermanentError(
                "Nexus token endpoint is not configured.",
                operation="authentication",
            )
        if method == "password" and not (
            self.settings.username and self.settings.password
        ):
            raise NexusPermanentError(
                "Nexus username and password are not configured.",
                operation="authentication",
            )
        if method == "client_credentials" and not (
            (self.settings.client_id and self.settings.client_secret)
            or self.settings.token_basic
        ):
            raise NexusPermanentError(
                "Nexus client credentials are not configured.",
                operation="authentication",
            )

    def _post_token(self, payload: Mapping[str, Any]) -> str:
        headers: dict[str, str] = {}
        auth: tuple[str, str] | None = None
        if self.settings.client_id and self.settings.client_secret:
            auth = (self.settings.client_id, self.settings.client_secret)
        elif self.settings.token_basic:
            basic = self.settings.token_basic
            if basic.lower().startswith("basic "):
                basic = basic[6:].strip()
            headers["Authorization"] = f"Basic {basic}"
        params: dict[str, str] = {}
        if self.settings.org_code:
            params["organizationCode"] = self.settings.org_code
            headers["organizationCode"] = self.settings.org_code
        try:
            if self.settings.token_payload_style == "json":
                response = self._http.post(
                    self.settings.token_url,
                    json=dict(payload),
                    params=params,
                    auth=auth,
                    headers=headers,
                )
            else:
                response = self._http.post(
                    self.settings.token_url,
                    data=dict(payload),
                    params=params,
                    auth=auth,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise NexusRetryableError(
                "Nexus authentication service is temporarily unavailable.",
                operation="authentication",
            ) from exc
        if not 200 <= response.status_code < 300:
            raise _response_error(response, operation="authentication", write=False)
        try:
            body = response.json()
        except ValueError as exc:
            raise NexusRetryableError(
                "Nexus authentication returned an unreadable response.",
                operation="authentication",
            ) from exc
        inner = body.get("data") if isinstance(body, Mapping) else None
        sources = [body, inner] if isinstance(inner, Mapping) else [body]
        token = ""
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            for key in ("acess_token", "access_token", "accessToken", "token", "jwt"):
                if source.get(key):
                    token = str(source[key])
                    break
            if token:
                break
        if not token:
            raise NexusPermanentError(
                "Nexus authentication response did not contain an access token.",
                operation="authentication",
            )
        if isinstance(body, Mapping):
            self._refresh_token = str(body.get("refresh_token") or "")
            self._token_verifier = str(body.get("tokenVerifier") or "")
            try:
                expires_in = int(body.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0
        else:
            expires_in = 0
        expiry = _jwt_exp(token)
        self._token_expires_at = (
            time.time() + expires_in if expires_in else float(expiry or time.time() + 1500)
        )
        return token

    def _fetch_token(self) -> str:
        self._validate_auth()
        method = self.settings.auth_method
        if method == "static":
            token = self.settings.static_token
            self._token_expires_at = float(_jwt_exp(token) or time.time() + 3600)
            return token
        if method == "password":
            payload: dict[str, Any] = {
                "grant_type": "password",
                "username": self.settings.username,
                "password": self.settings.password,
            }
        else:
            payload = {"grant_type": "client_credentials"}
        if self.settings.org_code:
            payload["organizationCode"] = self.settings.org_code
        return self._post_token(payload)

    def _refresh(self) -> str:
        if not (self._refresh_token and self.settings.auth_method == "password"):
            return ""
        payload: dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        if self._token_verifier:
            payload["tokenVerifier"] = self._token_verifier
        if self.settings.org_code:
            payload["organizationCode"] = self.settings.org_code
        try:
            return self._post_token(payload)
        except NexusDeliveryError:
            # LaborEdge refresh tokens can be one-shot. A full sign-in is safe.
            self._refresh_token = ""
            return ""

    def _get_token(self, *, force: bool = False) -> str:
        with self._auth_lock:
            if force or not self._token or time.time() >= self._token_expires_at - 60:
                self._token = (self._refresh() if self._token else "") or self._fetch_token()
            return self._token

    def request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        write: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        if not self.settings.enabled:
            raise NexusPermanentError(
                "Nexus delivery is disabled.", operation="configuration"
            )
        if not self.settings.base_url:
            raise NexusPermanentError(
                "Nexus API address is not configured.", operation="configuration"
            )
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self._get_token()}"
        url = self.settings.base_url + path
        try:
            response = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            cls = NexusIndeterminateError if write else NexusRetryableError
            raise cls(
                f"Nexus {operation} did not return a response.", operation=operation
            ) from exc
        if response.status_code in (401, 403) and self.settings.auth_method != "static":
            headers["Authorization"] = f"Bearer {self._get_token(force=True)}"
            try:
                response = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                cls = NexusIndeterminateError if write else NexusRetryableError
                raise cls(
                    f"Nexus {operation} did not return a response.", operation=operation
                ) from exc
        if not 200 <= response.status_code < 300:
            raise _response_error(response, operation=operation, write=write)
        return response

    def search_candidates(
        self, *, email: str = "", phone: str = "", npi: str = ""
    ) -> list[dict[str, Any]]:
        supplied = [bool(email), bool(phone), bool(npi)]
        if sum(supplied) != 1:
            raise NexusPermanentError(
                "A duplicate search must contain exactly one identity value.",
                operation="duplicate_search",
            )
        payload: dict[str, Any] = {
            "pagingSortingDetails": {"start": 0, "maxRowsToFetch": 20}
        }
        key = "email" if email else "phone" if phone else "npi"
        payload[key] = email or phone or npi
        response = self.request(
            "POST",
            "/api/api-integration/v1/candidates/search",
            operation="duplicate search",
            json=payload,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise NexusRetryableError(
                "Nexus duplicate search returned an unreadable response.",
                operation="duplicate_search",
            ) from exc
        rows = _rows(body, preferred=("records",))
        return [dict(row) for row in rows]

    def get_master(self, name: str) -> list[dict[str, Any]]:
        if name not in self.MASTER_LISTS:
            raise NexusPermanentError(
                "Unknown Nexus master-data list.", operation="master_data"
            )
        now = time.time()
        with self._master_lock:
            cached = self._master_cache.get(name)
            if cached and cached[0] > now:
                return cached[1]
        response = self.request(
            "GET",
            f"/api/api-integration/v1/master/{name}",
            operation="master-data lookup",
        )
        try:
            rows = [dict(row) for row in _rows(response.json())]
        except ValueError as exc:
            raise NexusRetryableError(
                "Nexus master-data lookup returned an unreadable response.",
                operation="master_data",
            ) from exc
        with self._master_lock:
            self._master_cache[name] = (
                now + self.settings.master_cache_seconds,
                rows,
            )
        return rows

    def create_candidate_with_resume(
        self,
        profile: Mapping[str, Any],
        *,
        filename: str,
        content: bytes,
    ) -> httpx.Response:
        return self.request(
            "POST",
            "/api/api-integration/v1/candidate/webhook/create",
            operation="candidate creation",
            write=True,
            data={"profileData": json.dumps(dict(profile), separators=(",", ":"))},
            files=[("profile", (filename, content, "application/pdf"))],
        )

    def upload_resume(
        self,
        candidate_id: str | int,
        *,
        doc_type_id: int,
        filename: str,
        content: bytes,
        checksum: str = "",
    ) -> httpx.Response:
        notes = "Medhunt enriched resume"
        if checksum:
            notes += f" ({checksum[:12]})"
        response = self.request(
            "POST",
            f"/api/api-integration/v1/candidates/{quote(str(candidate_id), safe='')}/upload/documents",
            operation="resume upload",
            write=True,
            data={
                "uploadedDocuments[0].documentTypeId": str(doc_type_id),
                "uploadedDocuments[0].notes": notes,
            },
            files=[
                (
                    "uploadedDocuments[0].document",
                    (filename, content, "application/pdf"),
                )
            ],
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, Mapping):
            failed = body.get("uploadFailedDocumentNames") or []
            if isinstance(failed, (str, bytes)):
                failed = [failed]
            failed_names = {
                _safe_filename(value).casefold() for value in failed if value
            }
            # This request contains exactly one document, so any provider
            # failure entry means the resume was not accepted even if the HTTP
            # envelope itself was 2xx.
            if failed_names:
                raise NexusPermanentError(
                    "Nexus reported that the resume upload failed.",
                    operation="resume_upload",
                    code="nexus_resume_rejected",
                )
        return response


def _rows(body: Any, preferred: Sequence[str] = ()) -> list[Mapping[str, Any]]:
    if isinstance(body, list):
        return [row for row in body if isinstance(row, Mapping)]
    if not isinstance(body, Mapping):
        return []
    for key in (*preferred, "data", "results", "items", "content"):
        value = body.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
        if isinstance(value, Mapping):
            nested = _rows(value, preferred=preferred)
            if nested:
                return nested
    return []


def _row_id(row: Mapping[str, Any]) -> str | int | None:
    for key in ("candidateId", "id", "Id", "candidate_id"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _response_candidate_id(response: httpx.Response) -> str | int | None:
    try:
        body = response.json()
    except ValueError:
        body = None
    candidates = [body]
    if isinstance(body, Mapping):
        candidates.extend([body.get("data"), body.get("candidate")])
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            value = _row_id(candidate)
            if value is not None:
                return value
    location = response.headers.get("Location", "").rstrip("/")
    tail = location.rsplit("/", 1)[-1] if location else ""
    return tail or None


def _safe_filename(value: Any) -> str:
    name = re.split(r"[\\/]", str(value or "resume.pdf"))[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:180]
    if not name.lower().endswith(".pdf"):
        name = f"{name or 'resume'}.pdf"
    return name or "resume.pdf"


def _email(value: Any) -> str:
    result = str(value or "").strip().lower()
    return result if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", result) else ""


def _phone(value: Any) -> str:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        # The Nexus candidate webhook rejects E.164 for US numbers and
        # explicitly requires this display format. Keep this conversion at
        # the Nexus boundary; Medhunt's stored canonical phone is unchanged.
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if 10 <= len(digits) <= 15:
        return f"+{digits}"
    return ""


def _first_value(source: Mapping[str, Any], singular: Sequence[str], plural: str) -> Any:
    for key in singular:
        if source.get(key):
            return source[key]
    values = source.get(plural) or []
    if isinstance(values, (str, bytes)):
        return values
    return next((value for value in values if value), "")


def _text_values(value: Any) -> list[str]:
    values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]
    output: list[str] = []
    for item in values:
        cleaned = " ".join(str(item or "").split()).strip()[:240]
        if cleaned and cleaned.casefold() not in {current.casefold() for current in output}:
            output.append(cleaned)
    return output


def _candidate_specialties(candidate: Mapping[str, Any], accepted: Mapping[str, Any]) -> list[str]:
    """Return source-declared specialties in primary-first order.

    Captured platform data is authoritative. Older persisted Vivian profiles
    stored its two specialty fields under the ``Skills`` label, so that one
    narrowly-scoped legacy shape remains readable. Arbitrary skills or prose
    are never promoted to a Nexus specialty.
    """
    values = [
        *_text_values(candidate.get("specialty")),
        *_text_values(candidate.get("specialties")),
    ]
    legacy_vivian = str(candidate.get("source") or "").casefold() == "vivian"
    for raw_line in str(candidate.get("notes") or "").splitlines():
        line = " ".join(raw_line.split()).strip()
        match = re.match(r"^(specialt(?:y|ies))\s*:\s*(.+)$", line, re.I)
        if not match and legacy_vivian:
            match = re.match(r"^(skills)\s*:\s*(.+)$", line, re.I)
        if not match:
            continue
        values.extend(
            item.strip() for item in re.split(r"\s*[;,|]\s*", match.group(2))
            if item.strip()
        )
    values.extend(_text_values(accepted.get("specialty")))
    values.extend(_text_values(accepted.get("specialties")))
    return _text_values(values)


def _specialty_master_labels(values: Sequence[str]) -> list[str]:
    """Return source labels followed by approved Nexus label equivalents."""
    expanded: list[str] = []
    for value in _text_values(values):
        expanded.append(value)
        expanded.extend(nexus_taxonomy.specialty_labels((value,)))
        alias_key = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
        expanded.extend(_SPECIALTY_ALIASES.get(alias_key, ()))
    return _text_values(expanded)


def _trusted_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), Mapping) else payload
    assert isinstance(candidate, Mapping)
    extraction = payload.get("resume_extraction")
    accepted = (
        extraction.get("accepted")
        if isinstance(extraction, Mapping) and isinstance(extraction.get("accepted"), Mapping)
        else {}
    )
    if candidate.get("contacts_trusted") is not True:
        raise NexusPermanentError(
            "Candidate contacts are not approved for Nexus delivery.",
            operation="payload_validation",
        )

    # A singular preferred address is honored only when it is still present in
    # the trusted, DNC-filtered email collection. Otherwise the provider's
    # first surviving address is the deterministic primary address.
    trusted_emails = [
        _email(value) for value in candidate.get("emails") or [] if _email(value)
    ]
    preferred_email = _email(candidate.get("primary_email") or candidate.get("email"))
    email = preferred_email if preferred_email in trusted_emails else (
        trusted_emails[0] if trusted_emails else ""
    )
    phone = _phone(
        _first_value(
            candidate,
            ("latest_phone", "preferred_phone", "primary_phone", "phone"),
            "phones",
        )
    )
    # Some projections carry typed phone records before the flat list.
    if not phone:
        for item in candidate.get("phone_contacts") or []:
            if isinstance(item, Mapping):
                phone = _phone(item.get("value"))
                if phone:
                    break
    if not (email or phone):
        raise NexusPermanentError(
            "Candidate has no deliverable trusted contact.",
            operation="payload_validation",
        )

    display_name = normalize_person_name(str(
        candidate.get("name") or accepted.get("full_name") or ""
    ))
    first_name = normalize_person_name(str(
        candidate.get("first_name") or accepted.get("first_name") or ""
    ))
    last_name = normalize_person_name(str(
        candidate.get("last_name") or accepted.get("last_name") or ""
    ))
    middle_name = normalize_person_name(str(
        candidate.get("middle_name") or accepted.get("middle_name") or ""
    ))
    if not (first_name and last_name) and display_name:
        words = display_name.split()
        if len(words) >= 2:
            first_name = first_name or words[0]
            suffix = words.pop() if is_name_suffix(words[-1]) and len(words) >= 3 else ""
            surname_start = len(words) - 1
            while surname_start > 1 and words[surname_start - 1].casefold().strip(".,") in _SURNAME_PARTICLES:
                surname_start -= 1
            last_name = last_name or " ".join([
                *words[surname_start:], *([suffix] if suffix else []),
            ])
            middle_name = middle_name or " ".join(words[1:surname_start])

    location = str(candidate.get("location") or accepted.get("location") or "").strip()
    city = str(candidate.get("city") or accepted.get("city") or "").strip()
    state = str(
        candidate.get("state") or candidate.get("region")
        or accepted.get("state") or ""
    ).strip()
    country = str(candidate.get("country") or accepted.get("country") or "").strip()
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if not city and len(parts) >= 2:
        city = parts[0]
    if not state and len(parts) >= 2:
        state = parts[-2] if len(parts) >= 3 else parts[-1]
    parsed_location = verification.us_city_state(location)
    if parsed_location:
        parsed_city, parsed_state = (part.strip() for part in parsed_location.split(",", 1))
        city = city or parsed_city
        # Strip ZIP codes and normalize full state names before resolving the
        # state against Nexus master data.
        state = parsed_state
    if not country and len(parts) >= 3:
        country = parts[-1]
    source_specialties = _candidate_specialties(candidate, {})
    resume_specialties = _text_values([
        *_text_values(accepted.get("specialty")),
        *_text_values(accepted.get("specialties")),
    ])
    if not resume_specialties and isinstance(extraction, Mapping):
        # Older captures already stored this structured field. Read it only
        # during Nexus delivery so resume capture and downloads stay untouched.
        fields = extraction.get("fields")
        confidence = extraction.get("confidence")
        conflicts = extraction.get("conflicts") or []
        try:
            specialty_confidence = float(
                confidence.get("specialties") if isinstance(confidence, Mapping) else 0
            )
        except (TypeError, ValueError):
            specialty_confidence = 0
        if (
            isinstance(fields, Mapping)
            and specialty_confidence >= 0.75
            and "name" not in conflicts
        ):
            extracted = _text_values(fields.get("specialties"))
            if int(extraction.get("schema_version") or 0) < 2:
                # Older parser versions could match ordinary prose "or"/"er".
                extracted = [
                    value for value in extracted
                    if value.casefold() not in {"or", "er"}
                ]
            # Put headline/current-role specialties first so they become the
            # primary choice, then retain other resume specialties for Nexus.
            role_evidence = str(fields.get("job_title") or "")
            try:
                role_confidence = float(
                    confidence.get("job_title") if isinstance(confidence, Mapping) else 0
                )
            except (TypeError, ValueError):
                role_confidence = 0
            role_matches = (
                _role_specialty_labels(role_evidence, _profession_labels(role_evidence))
                if role_confidence >= 0.8 and role_evidence else ()
            )
            resume_specialties = _text_values([*role_matches, *extracted])
    return {
        "firstName": first_name,
        "middleName": middle_name,
        "lastName": last_name,
        "email": email,
        "phone": phone,
        "city": city,
        "state": state,
        "country": country,
        "role": str(
            candidate.get("job_title")
            or candidate.get("role")
            or candidate.get("title")
            or accepted.get("job_title")
            or ""
        ).strip(),
        "source_specialties": source_specialties,
        "resume_specialties": resume_specialties,
        "specialties": _text_values([*source_specialties, *resume_specialties]),
    }


def _active(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("active", True) is not False]


def _master_id(row: Mapping[str, Any], list_name: str = "") -> Any:
    preferred = {
        "specialties": ("specialtyId", "id", "value"),
        "professions": ("professionId", "id", "value"),
        "states": ("stateId", "id", "value"),
        "countries": ("countryId", "id", "value"),
        "referralsources": ("referralSourceId", "id", "value"),
        "candidatestatuses": ("candidateStatusId", "statusId", "id", "value"),
        "documenttypes": ("documentTypeId", "id", "value"),
    }.get(list_name, ("id", "value", "specialtyId", "professionId"))
    for key in preferred:
        if row.get(key) is not None:
            return row[key]
    return None


def _label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


_PROFESSION_ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CRNA", re.compile(r"\b(?:crna|nurse\s+anesthetist)\b", re.I)),
    ("Midwife", re.compile(r"\b(?:cnm|nurse\s+midwife|midwife)\b", re.I)),
    ("Nurse Practitioner", re.compile(
        r"\b(?:nurse\s+practitioner|np|aprn|fnp(?:-bc)?|pmhnp(?:-bc)?|"
        r"ag(?:ac|pc)?np(?:-bc)?|pnp(?:-bc)?)\b", re.I,
    )),
    ("LPN/LVN", re.compile(
        r"\b(?:lpn|lvn|licensed\s+(?:practical|vocational)\s+nurse)\b", re.I,
    )),
    ("CNA", re.compile(r"\b(?:cna|certified\s+nursing\s+assistant|nursing\s+assistant)\b", re.I)),
    ("RN", re.compile(
        r"\b(?:rn(?:-bc)?|registered\s+nurse|clinical\s+nurse|staff\s+nurse|"
        r"charge\s+nurse|travel\s+nurse|nurse\s+(?:manager|supervisor|educator))\b",
        re.I,
    )),
    ("Physician Assistant", re.compile(r"\b(?:physician\s+assistant|pa-c)\b", re.I)),
    ("Physician", re.compile(r"\b(?:physician|medical\s+doctor|m\.?d\.?|d\.?o\.?)\b", re.I)),
    ("Medical Assistant", re.compile(r"\b(?:medical\s+assistant|cma)\b", re.I)),
    ("Patient Care Technician", re.compile(r"\b(?:patient\s+care\s+tech(?:nician)?|pct)\b", re.I)),
    ("Surgical Services", re.compile(
        r"\b(?:surgical\s+(?:services|tech(?:nician|nologist)?)|operating\s+room\s+tech|cst)\b",
        re.I,
    )),
    ("Respiratory Therapy", re.compile(r"\b(?:respiratory\s+therap(?:ist|y)|rrt)\b", re.I)),
    ("Physical Therapy", re.compile(r"\bphysical\s+therap(?:ist|y)\b", re.I)),
    ("Occupational Therapy", re.compile(r"\boccupational\s+therap(?:ist|y)\b", re.I)),
    ("Speech Therapy", re.compile(
        r"\b(?:speech\s+(?:language\s+)?(?:pathologist|therapy)|slp)\b", re.I,
    )),
    ("Radiology/Imaging", re.compile(
        r"\b(?:radiolog(?:y|ic|ist)|imaging|sonograph(?:er|y)|x-?ray|ct\s+tech|mri\s+tech)\b",
        re.I,
    )),
    ("Laboratory", re.compile(
        r"\b(?:laboratory|lab\s+tech(?:nician|nologist)?|phlebotomist|histology|microbiology)\b",
        re.I,
    )),
    ("Pharmacy", re.compile(r"\b(?:pharmacist|pharmacy(?:\s+tech(?:nician)?)?)\b", re.I)),
    ("Paramedic", re.compile(r"\bparamedic\b", re.I)),
    ("EMT", re.compile(r"\bemt\b", re.I)),
    ("Dialysis Tech", re.compile(r"\bdialysis\s+tech(?:nician)?\b", re.I)),
    ("Behavioral Health", re.compile(r"\bbehavioral\s+health\b", re.I)),
    ("Social Services", re.compile(r"\b(?:social\s+worker|social\s+services)\b", re.I)),
)


def _profession_labels(role: str) -> tuple[str, ...]:
    """Return exact tenant profession labels without inferring an ID.

    The ID is always resolved from live Nexus master data.  Unknown roles use
    Nexus's explicit Unknown classification, which is safer than assigning an
    unrelated clinical profession merely to pass payload validation.
    """
    text = str(role or "").strip()
    for label, pattern in _PROFESSION_ROLE_PATTERNS:
        if pattern.search(text):
            return (label,)
    taxonomy_labels = nexus_taxonomy.profession_labels_for_role(text)
    if taxonomy_labels:
        return taxonomy_labels
    return ("Unknown",)


def _role_specialty_labels(
    role: str, profession_values: Sequence[str],
) -> tuple[str, ...]:
    labels = [
        label for label, pattern in _ROLE_SPECIALTY_PATTERNS
        if pattern.search(str(role or ""))
    ]
    labels.extend(nexus_taxonomy.specialty_labels_for_role(role, profession_values))
    return tuple(_text_values(labels))


def _exact_master_id(
    client: NexusClient,
    name: str,
    values: Sequence[str],
    *,
    description: str,
    extra_predicate: Any = None,
) -> int:
    wanted = {_label(value) for value in values if _label(value)}
    matches: dict[int, Mapping[str, Any]] = {}
    for row in _active(client.get_master(name)):
        labels = {
            _label(row.get("name")),
            _label(row.get("label")),
            _label(row.get("code")),
            _label(row.get("abbreviation")),
        }
        raw_id = _master_id(row, name)
        if raw_id is None or not (wanted & labels):
            continue
        if extra_predicate is not None and not extra_predicate(row):
            continue
        try:
            matches[int(raw_id)] = row
        except (TypeError, ValueError):
            continue
    if len(matches) != 1:
        raise NexusPermanentError(
            f"Nexus {description} could not be resolved unambiguously.",
            operation="master_data",
        )
    return next(iter(matches))


def _preferred_master_id(
    client: NexusClient,
    name: str,
    values: Sequence[str],
    *,
    description: str,
    extra_predicate: Any = None,
) -> int:
    """Resolve the first exact preferred label that has one unique ID."""
    rows = _active(client.get_master(name))
    for value in values:
        wanted = _label(value)
        matches: set[int] = set()
        for row in rows:
            labels = {
                _label(row.get("name")),
                _label(row.get("label")),
                _label(row.get("code")),
                _label(row.get("abbreviation")),
            }
            raw_id = _master_id(row, name)
            if not wanted or wanted not in labels or raw_id is None:
                continue
            if extra_predicate is not None and not extra_predicate(row):
                continue
            try:
                matches.add(int(raw_id))
            except (TypeError, ValueError):
                continue
        if len(matches) == 1:
            return next(iter(matches))
        if len(matches) > 1:
            break
    raise NexusPermanentError(
        f"Nexus {description} could not be resolved unambiguously.",
        operation="master_data",
    )


def _preferred_master_row(
    client: NexusClient,
    name: str,
    values: Sequence[str],
    *,
    description: str,
    extra_predicate: Any = None,
) -> Mapping[str, Any]:
    """Resolve the first exact preferred label and retain its related IDs."""
    rows = _active(client.get_master(name))
    for value in values:
        wanted = _label(value)
        matches: dict[int, Mapping[str, Any]] = {}
        for row in rows:
            labels = {
                _label(row.get("name")),
                _label(row.get("label")),
                _label(row.get("code")),
                _label(row.get("abbreviation")),
            }
            raw_id = _master_id(row, name)
            if not wanted or wanted not in labels or raw_id is None:
                continue
            if extra_predicate is not None and not extra_predicate(row):
                continue
            try:
                matches[int(raw_id)] = row
            except (TypeError, ValueError):
                continue
        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(matches) > 1:
            break
    raise NexusPermanentError(
        f"Nexus {description} could not be resolved unambiguously.",
        operation="master_data",
    )


def _default_id(profile: Mapping[str, Any], singular: str, plural: str = "") -> int | None:
    value = profile.get(singular)
    if not value and plural:
        values = profile.get(plural)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and values:
            value = values[0]
    try:
        return int(value) if value else None
    except (TypeError, ValueError) as exc:
        raise NexusPermanentError(
            f"Nexus default field {singular} must be numeric.",
            operation="configuration",
        ) from exc


def _unknown_classification(client: NexusClient) -> tuple[int, int]:
    """Resolve Nexus's internally consistent Unknown profession/specialty pair."""
    profession_id = _preferred_master_id(
        client, "professions", ("Unknown",), description="profession"
    )
    specialty_id = _preferred_master_id(
        client,
        "specialties",
        ("Unknown",),
        description="specialty",
        extra_predicate=lambda row: (
            not row.get("professionId")
            or int(row["professionId"]) == profession_id
        ),
    )
    return profession_id, specialty_id


def _unknown_specialty_for_profession(client: NexusClient, profession_id: int) -> int | None:
    """Keep an identified profession when Nexus has its own Unknown specialty."""
    try:
        return _preferred_master_id(
            client, "specialties", ("Unknown",), description="specialty",
            extra_predicate=lambda row: (
                bool(row.get("professionId"))
                and int(row["professionId"]) == profession_id
            ),
        )
    except NexusPermanentError:
        return None


def _matching_specialties(
    client: NexusClient, profession_id: int, labels: Sequence[str],
) -> list[Mapping[str, Any]]:
    """Match ordered candidate specialties to one Nexus profession."""
    matches: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for label in labels:
        try:
            row = _preferred_master_row(
                client, "specialties", _specialty_master_labels([label]),
                description="candidate specialty",
                extra_predicate=lambda item: (
                    _label(item.get("name")) != "unknown"
                    and (not item.get("professionId")
                         or int(item["professionId"]) == profession_id)
                ),
            )
        except NexusPermanentError:
            continue
        row_id = int(_master_id(row, "specialties"))
        if row_id not in seen:
            seen.add(row_id)
            matches.append(row)
    return matches


def _build_profile(
    client: NexusClient,
    identity: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> dict[str, Any]:
    # Defaults are administrator-controlled tenant routing. Candidate data is
    # copied from a strict allowlist below; arbitrary provider evidence cannot
    # enter this object.
    profile = dict(defaults)
    state_map = profile.pop("stateIds", {}) or {}
    profession_name = str(profile.pop("professionName", "") or "").strip()
    specialty_name = str(profile.pop("specialtyName", "") or "").strip()
    referral_name = str(profile.pop("referralSourceName", "") or "").strip()
    status_name = str(profile.pop("statusName", "") or "").strip()
    country_name = str(profile.pop("countryName", "") or "").strip()

    profile.update(
        {
            "firstName": identity["firstName"],
            "lastName": identity["lastName"],
            "email": identity["email"],
            "primaryEmail": identity["email"],
            "phone": identity["phone"],
            # Nexus webhook validation expects the canonical mobile field in
            # addition to the parser-compatible phone field. Medhunt's phone
            # policy has already selected the latest trusted number here.
            "cellPhone": identity["phone"],
            "sendMassEmails": False,
            "sendMassSms": False,
        }
    )
    if identity.get("middleName"):
        profile["middleName"] = identity["middleName"]
    city_text = str(identity.get("city") or "").strip()
    if not city_text:
        raise NexusPermanentError(
            "Candidate city is required for Nexus creation.",
            operation="payload_validation",
        )
    profile["city"] = city_text

    if not profile["firstName"] or not profile["lastName"]:
        raise NexusPermanentError(
            "Candidate first and last name are required for Nexus creation.",
            operation="payload_validation",
        )
    if not profile["email"] and not profile["phone"]:
        raise NexusPermanentError(
            "Nexus creation requires at least one trusted email or phone.",
            operation="payload_validation",
        )

    state_text = str(identity.get("state") or "").strip()
    if state_text:
        mapped = None
        if isinstance(state_map, Mapping):
            for key, value in state_map.items():
                if _label(key) == _label(state_text):
                    mapped = value
                    break
        if mapped:
            try:
                profile["stateId"] = int(mapped)
            except (TypeError, ValueError) as exc:
                raise NexusPermanentError(
                    "Nexus state mapping must contain numeric IDs.",
                    operation="configuration",
                ) from exc
        else:
            profile["stateId"] = _exact_master_id(
                client,
                "states",
                (state_text,),
                description="state",
            )
    else:
        raise NexusPermanentError(
            "Candidate state is required for Nexus creation; tenant defaults cannot replace candidate location.",
            operation="payload_validation",
        )

    if not _default_id(profile, "countryId"):
        country = str(identity.get("country") or country_name or "United States")
        values = (country, "USA", "US") if _label(country) in {
            "united states", "usa", "us"
        } else (country,)
        profile["countryId"] = _exact_master_id(
            client, "countries", values, description="country"
        )

    if not _default_id(profile, "statusId"):
        profile["statusId"] = _exact_master_id(
            client,
            "candidatestatuses",
            (status_name or "PROSPECT",),
            description="candidate status",
            extra_predicate=lambda row: str(row.get("module") or "CANDIDATE").upper()
            == "CANDIDATE",
        )

    if not _default_id(profile, "referralSourceId"):
        if referral_name:
            profile["referralSourceId"] = _exact_master_id(
                client,
                "referralsources",
                (referral_name,),
                description="referral source",
            )
        else:
            sources = [
                row for row in _active(client.get_master("referralsources"))
                if _master_id(row, "referralsources") is not None
            ]
            ids = {int(_master_id(row, "referralsources")) for row in sources}
            if len(ids) != 1:
                raise NexusPermanentError(
                    "Nexus referral source is not configured unambiguously.",
                    operation="configuration",
                )
            profile["referralSourceId"] = next(iter(ids))

    role = str(identity.get("role") or "")
    initial_profession_values = (
        (profession_name,) if profession_name else _profession_labels(role)
    )
    source_specialties = _text_values(identity.get("source_specialties"))
    resume_specialties = _text_values(identity.get("resume_specialties"))
    if "source_specialties" not in identity and "resume_specialties" not in identity:
        source_specialties = _text_values(identity.get("specialties"))
    # Source and current role determine primary priority. Additional résumé
    # specialties remain available for Nexus's specialtyIds array.
    actual_specialties = _text_values([
        *source_specialties,
        *_role_specialty_labels(role, initial_profession_values),
        *resume_specialties,
    ])
    taxonomy_row = nexus_taxonomy.classify(
        initial_profession_values, actual_specialties,
    )
    profession_values = (
        (taxonomy_row.profession,) if taxonomy_row else initial_profession_values
    )

    profession_id = _default_id(profile, "professionId", "professionIds")
    profession_inferred = False
    if not profession_id:
        profession_id = _preferred_master_id(
            client,
            "professions",
            profession_values,
            description="profession",
        )
        profession_inferred = not bool(profession_name)
    if profession_id:
        profile["professionId"] = profession_id
        profile["professionIds"] = [profession_id]

    specialty_id = _default_id(profile, "specialtyId", "specialtyIds")
    specialty_ids: list[int] = []
    if actual_specialties:
        specialty_rows = _matching_specialties(
            client, profession_id, actual_specialties,
        ) if profession_id else []
        if not specialty_rows:
            # A clearly named specialty can identify a profession when a
            # generic source role could not. Do this only for the first match;
            # every additional specialty is then constrained to that profession.
            first_row = None
            for label in actual_specialties:
                try:
                    first_row = _preferred_master_row(
                        client, "specialties", _specialty_master_labels([label]),
                        description="candidate specialty",
                        extra_predicate=lambda row: _label(row.get("name")) != "unknown",
                    )
                    break
                except NexusPermanentError:
                    continue
            if first_row is not None:
                related_profession = first_row.get("professionId")
                if related_profession:
                    profession_id = int(related_profession)
                    profile["professionId"] = profession_id
                    profile["professionIds"] = [profession_id]
                    specialty_rows = _matching_specialties(
                        client, profession_id, actual_specialties,
                    )
                else:
                    specialty_rows = [first_row]
        if not specialty_rows:
            specialty_id = _unknown_specialty_for_profession(client, profession_id)
            if specialty_id is None:
                profession_id, specialty_id = _unknown_classification(client)
                profile["professionId"] = profession_id
                profile["professionIds"] = [profession_id]
        else:
            specialty_ids = [
                int(_master_id(row, "specialties")) for row in specialty_rows
            ]
            specialty_id = specialty_ids[0]
    elif not specialty_id:
        try:
            specialty_id = _preferred_master_id(
                client,
                "specialties",
                (specialty_name,) if specialty_name else ("Unknown", "Other", "General"),
                description="specialty",
                extra_predicate=(
                    (lambda row: not row.get("professionId") or int(row["professionId"]) == profession_id)
                    if profession_id
                    else None
                ),
            )
        except NexusPermanentError:
            # Do not guess a specialty when the source only identifies a broad
            # profession (for example LPN/LVN). If the tenant has no generic
            # specialty for that profession, use its explicit Unknown pair.
            # Administrator-supplied mappings remain strict and are never
            # silently replaced.
            if specialty_name or not profession_inferred:
                raise
            profession_id, specialty_id = _unknown_classification(client)
            profile["professionId"] = profession_id
            profile["professionIds"] = [profession_id]
    if specialty_id:
        profile["specialtyId"] = specialty_id
        profile["primarySpecialtyId"] = specialty_id
        profile["specialtyIds"] = specialty_ids or [specialty_id]

    job_types = profile.get("jobTypeIds")
    allowed = {"LOCAL", "PERDIEM", "PERM", "TRAVEL"}
    if not isinstance(job_types, list) or not job_types:
        raise NexusPermanentError(
            "Nexus job types are not configured.", operation="configuration"
        )
    profile["jobTypeIds"] = [str(item).upper() for item in job_types]
    if any(item not in allowed for item in profile["jobTypeIds"]):
        raise NexusPermanentError(
            "Nexus job type configuration is invalid.", operation="configuration"
        )
    return profile


def _repair_linked_classification(
    client: NexusClient,
    candidate_id: str,
    identity: Mapping[str, Any],
    defaults: Mapping[str, Any],
    before_write: Any = None,
) -> bool:
    """Fill an existing candidate's Unknown specialty from clear new evidence.

    Do not replace a specific Nexus specialty or an unrelated known profession.
    This is only called when MedHunt already has a durable Nexus candidate link.
    """
    role = str(identity.get("role") or "")
    sources = _text_values(identity.get("source_specialties"))
    resume = _text_values(identity.get("resume_specialties"))
    if not (sources or resume or _role_specialty_labels(role, _profession_labels(role))):
        return False
    try:
        proposed = _build_profile(client, identity, defaults)
    except NexusPermanentError:
        # Existing résumé delivery must continue when candidate location or
        # unrelated creation-only defaults are no longer available locally.
        return False
    proposed_profession = int(proposed["professionId"])
    proposed_specialty = int(proposed["specialtyId"])
    proposed_specialties = [int(value) for value in proposed["specialtyIds"]]
    unknown_specialties = {
        int(_master_id(row, "specialties"))
        for row in _active(client.get_master("specialties"))
        if _master_id(row, "specialties") is not None
        and _label(row.get("name")) == "unknown"
    }
    if proposed_specialty in unknown_specialties:
        return False
    unknown_professions = {
        int(_master_id(row, "professions"))
        for row in _active(client.get_master("professions"))
        if _master_id(row, "professions") is not None
        and _label(row.get("name")) == "unknown"
    }
    path = f"/api/api-integration/v1/candidates/{quote(candidate_id, safe='')}"
    remote = client.request("GET", path, operation="linked candidate lookup").json()
    if not isinstance(remote, Mapping):
        raise NexusRetryableError(
            "Nexus linked candidate lookup returned an unreadable record.",
            operation="linked candidate lookup",
        )
    current_specialties = {
        int(value) for value in (
            [*(remote.get("specialtyIds") or []), remote.get("primarySpecialtyId")]
        ) if value not in (None, "")
    }
    if current_specialties and not current_specialties <= unknown_specialties:
        return False
    current_professions = {
        int(value) for value in (remote.get("professionIds") or [])
        if value not in (None, "")
    }
    known_professions = current_professions - unknown_professions
    if known_professions and proposed_profession not in known_professions:
        return False
    profession_ids = sorted(known_professions | {proposed_profession})
    patch = {
        "professionIds": profession_ids,
        "specialtyIds": proposed_specialties,
        "primarySpecialtyId": proposed_specialty,
    }
    if before_write:
        before_write("candidate_classification_update")
    client.request(
        "PATCH", path, operation="candidate classification update",
        write=True, json=patch,
    )
    verified = client.request("GET", path, operation="candidate classification verification").json()
    if (
        not isinstance(verified, Mapping)
        or proposed_profession not in (verified.get("professionIds") or [])
        or not set(proposed_specialties) <= set(verified.get("specialtyIds") or [])
        or int(verified.get("primarySpecialtyId") or 0) != proposed_specialty
    ):
        raise NexusIndeterminateError(
            "Nexus did not retain the linked candidate specialty update.",
            operation="candidate classification update",
        )
    return True


def _candidate_ids(rows: Sequence[Mapping[str, Any]], *, basis: str) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        value = _row_id(row)
        if value is None:
            raise NexusIndeterminateError(
                f"Nexus {basis} search returned a candidate without an ID.",
                operation="duplicate_search",
            )
        ids.add(str(value))
    if len(ids) > 1:
        raise NexusIndeterminateError(
            f"Nexus {basis} search matched multiple candidates.",
            operation="duplicate_search",
        )
    return ids


def _resolve_duplicate(
    email_rows: Sequence[Mapping[str, Any]],
    phone_rows: Sequence[Mapping[str, Any]],
    *,
    expected_name: str = "",
) -> tuple[str | None, list[str]]:
    email_ids = _candidate_ids(email_rows, basis="email")
    phone_ids = _candidate_ids(phone_rows, basis="phone")
    if email_ids and phone_ids and email_ids != phone_ids:
        raise NexusIndeterminateError(
            "Nexus email and phone belong to different candidates.",
            operation="duplicate_search",
        )
    ids = email_ids or phone_ids
    matched_by = [
        basis
        for basis, values in (("email", email_ids), ("phone", phone_ids))
        if values
    ]
    if ids and len(matched_by) == 1 and expected_name:
        selected_id = next(iter(ids))
        source_rows = email_rows if email_ids else phone_rows
        match = next(
            (row for row in source_rows if str(_row_id(row) or "") == selected_id),
            {},
        )
        returned_name = str(
            match.get("name") or match.get("fullName") or match.get("candidateName") or ""
        ).strip()
        if not returned_name:
            returned_name = " ".join(filter(None, (
                str(match.get("firstName") or "").strip(),
                str(match.get("lastName") or "").strip(),
            )))
        if returned_name:
            expected = identity_signature(expected_name)
            returned = identity_signature(returned_name)
            if not (
                expected.get("first")
                and expected.get("first") == returned.get("first")
                and expected.get("surname")
                and expected.get("surname") == returned.get("surname")
            ):
                raise NexusIndeterminateError(
                    "The unique Nexus contact match belongs to a different name.",
                    operation="duplicate_search",
                    code="nexus_contact_name_conflict",
                )
        else:
            raise NexusIndeterminateError(
                "The unique Nexus contact match did not include a name for corroboration.",
                operation="duplicate_search",
                code="nexus_contact_name_missing",
            )
    return (next(iter(ids)) if ids else None), matched_by


def _resume_metadata(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("resume") or payload.get("resume_metadata") or {}
    return value if isinstance(value, Mapping) else {}


def _resume_doc_type_id(client: NexusClient) -> int:
    configured = client.settings.resume_doc_type_id.strip()
    if configured:
        try:
            return int(configured)
        except ValueError as exc:
            raise NexusPermanentError(
                "Nexus resume document type ID must be numeric.",
                operation="configuration",
            ) from exc
    rows = _active(client.get_master("documenttypes"))

    def matching_ids(*, exact: bool) -> set[int]:
        matches: set[int] = set()
        for row in rows:
            labels = (
                row.get("label"),
                row.get("name"),
                row.get("code"),
            )
            normalized = {_label(value) for value in labels if _label(value)}
            is_match = (
                "candidate resume" in normalized
                if exact
                else any("resume" in value for value in normalized)
            )
            raw_id = _master_id(row, "documenttypes")
            if not is_match or raw_id is None:
                continue
            try:
                matches.add(int(raw_id))
            except (TypeError, ValueError):
                continue
        return matches

    exact_ids = matching_ids(exact=True)
    if len(exact_ids) == 1:
        return next(iter(exact_ids))
    if len(exact_ids) > 1:
        raise NexusPermanentError(
            "Nexus Candidate Resume document type is ambiguous.",
            operation="master_data",
        )

    # Some Nexus tenants call this simply "Resume".  The reference Nexus app
    # accepts that variant, but only use it here when it resolves to one unique
    # active type so a document can never be routed to an arbitrary category.
    fallback_ids = matching_ids(exact=False)
    if len(fallback_ids) == 1:
        return next(iter(fallback_ids))
    raise NexusPermanentError(
        "Nexus Candidate Resume document type could not be resolved unambiguously.",
        operation="master_data",
    )


_CLIENT_LOCK = threading.Lock()
_SHARED_CLIENT: NexusClient | None = None


def _shared_client(settings: NexusSettings) -> NexusClient:
    global _SHARED_CLIENT
    with _CLIENT_LOCK:
        if _SHARED_CLIENT is None or _SHARED_CLIENT.settings != settings:
            if _SHARED_CLIENT is not None:
                _SHARED_CLIENT.close()
            _SHARED_CLIENT = NexusClient(settings)
        return _SHARED_CLIENT


def process_delivery(
    payload: Mapping[str, Any],
    resume_pdf: bytes,
    *,
    settings: NexusSettings | None = None,
    client: NexusClient | None = None,
    before_write: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Deliver one trusted candidate and enriched resume to Nexus.

    This function performs no database acknowledgement itself.  The caller
    should persist the returned remote ID, retry :class:`NexusRetryableError`,
    and hold :class:`NexusIndeterminateError` for reconciliation.
    """
    if not isinstance(payload, Mapping):
        raise NexusPermanentError(
            "Nexus delivery payload must be an object.",
            operation="payload_validation",
        )
    settings = settings or NexusSettings.from_config()
    client = client or _shared_client(settings)
    if not settings.enabled:
        raise NexusPermanentError(
            "Nexus delivery is disabled.", operation="configuration"
        )
    if not isinstance(resume_pdf, bytes) or not resume_pdf.startswith(b"%PDF"):
        raise NexusPermanentError(
            "Nexus delivery requires a valid PDF resume.",
            operation="payload_validation",
        )
    if len(resume_pdf) > settings.max_resume_bytes:
        raise NexusPermanentError(
            "Resume exceeds the Nexus upload limit.",
            operation="payload_validation",
        )

    identity = _trusted_identity(payload)
    resume = _resume_metadata(payload)
    filename = _safe_filename(resume.get("filename") or payload.get("filename"))
    checksum = str(
        resume.get("checksum_sha256")
        or payload.get("checksum_sha256")
        or ""
    ).strip().lower()
    resume_id = resume.get("id", payload.get("resume_id"))

    linked_candidate_id = str(payload.get("nexus_candidate_id") or "").strip()
    if linked_candidate_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", linked_candidate_id):
            raise NexusPermanentError(
                "Stored Nexus candidate ID is invalid.",
                operation="payload_validation",
                code="nexus_candidate_id_invalid",
            )
        classification_updated = _repair_linked_classification(
            client, linked_candidate_id, identity, settings.default_profile,
            before_write=before_write,
        )
        doc_type_id = _resume_doc_type_id(client)
        if before_write and not classification_updated:
            before_write("resume_upload")
        client.upload_resume(
            linked_candidate_id,
            doc_type_id=doc_type_id,
            filename=filename,
            content=resume_pdf,
            checksum=checksum,
        )
        return {
            "status": "delivered",
            "action": "resume_uploaded",
            "classification_updated": classification_updated,
            "nexus_candidate_id": linked_candidate_id,
            "matched_by": ["stored_link"],
            "resume_id": resume_id,
            "checksum_sha256": checksum,
        }

    # No ATS duplicate lookup is performed. If this local candidate already
    # has a stored Nexus link, the resume is attached above; otherwise submit
    # the candidate directly; any duplicate handling is left to Nexus.
    profile = _build_profile(client, identity, settings.default_profile)
    if before_write:
        before_write("candidate_creation")
    response = client.create_candidate_with_resume(
        profile,
        filename=filename,
        content=resume_pdf,
    )
    remote_candidate_id = _response_candidate_id(response)
    if remote_candidate_id is None:
        raise NexusIndeterminateError(
            "Nexus accepted candidate creation without returning a candidate ID.",
            operation="candidate creation",
            code="nexus_create_unbound",
        )
    return {
        "status": "delivered",
        "action": "candidate_created",
        "nexus_candidate_id": remote_candidate_id,
        "matched_by": [],
        "resume_id": resume_id,
        "checksum_sha256": checksum,
    }


__all__ = [
    "NexusClient",
    "NexusDeliveryError",
    "NexusIndeterminateError",
    "NexusPermanentError",
    "NexusRetryableError",
    "NexusSettings",
    "process_delivery",
]
