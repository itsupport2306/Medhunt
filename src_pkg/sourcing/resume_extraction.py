"""Private, local extraction for text and scanned PDF resumes.

The original resume remains the source of truth.  This module reads an
existing text layer first and invokes the bundled Tesseract executable only
for pages that contain too little machine-readable text.  It deliberately
stores structured fields rather than the full OCR transcript.

Candidate data captured directly from a recruiting platform is authoritative.
OCR may fill a missing field only after a conservative confidence check; it
never replaces trusted contact data or a conflicting platform identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping

from . import config
from .person_name import identity_signature, normalize_person_name


SCHEMA_VERSION = 1
_MAX_FIELD_ITEMS = 20
_NAME_STOP = {
    "address", "availability", "certifications", "contact", "curriculum",
    "education", "email", "experience", "licenses", "location", "objective",
    "phone", "profile", "references", "resume", "skills", "summary", "vitae",
}
_SECTION_ALIASES = {
    "skills": {"skills", "technical skills", "core competencies", "competencies"},
    "certifications": {"certifications", "certificates", "credentials"},
    "licenses": {"licenses", "licensure", "licenses and certifications"},
    "work_history": {"experience", "work experience", "professional experience", "employment history"},
    "education": {"education", "academic background", "education and training"},
    "summary": {"summary", "professional summary", "profile", "objective"},
}
_ALL_HEADINGS = {item for values in _SECTION_ALIASES.values() for item in values}
_EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.()-]*)?(?:\(\s*)?\d{3}(?:\s*\))?[\s.-]*\d{3}[\s.-]*\d{4}(?!\d)"
)
_LOCATION_LABEL_RE = re.compile(
    r"^(?:address|based\s+in|city|current\s+location|location|lives\s+in|resides\s+in)\s*[:\-]\s*(.+)$",
    re.I,
)
_LOCATION_ORGANIZATION_RE = re.compile(
    r"\b(?:academy|clinic|college|company|health|hospital|institute|medical|school|university)\b",
    re.I,
)
_ROLE_LABEL_RE = re.compile(r"^(?:current\s+title|job\s+title|profession|role|title)\s*[:\-]\s*(.+)$", re.I)
_WORK_AUTH_RE = re.compile(
    r"\b(?:authorized\s+to\s+work|work\s+authori[sz]ation|visa\s+status|citizen(?:ship)?|green\s+card|permanent\s+resident)\b",
    re.I,
)
_AVAILABILITY_RE = re.compile(
    r"\b(?:available\s+(?:immediately|from|to\s+start)|availability|notice\s+period|start\s+date)\b",
    re.I,
)
_HEALTHCARE_ROLE_RE = re.compile(
    r"\b(?:registered\s+nurse|licensed\s+(?:practical|vocational)\s+nurse|"
    r"nurse\s+practitioner|nursing\s+assistant|clinical\s+nurse|staff\s+nurse|"
    r"charge\s+nurse|travel\s+nurse|rn|lpn|lvn|cna|aprn|crna|physician|"
    r"therapist|technologist|technician)\b",
    re.I,
)
_SPECIALTIES = (
    "ICU", "Critical Care", "Emergency", "ER", "Telemetry", "Medical-Surgical",
    "Med Surg", "Operating Room", "OR", "PACU", "Labor and Delivery", "L&D",
    "Pediatrics", "Oncology", "Dialysis", "Home Health", "Behavioral Health",
    "Psychiatric", "Cardiology", "Endoscopy", "Neonatal", "NICU",
)
_CREDENTIAL_RE = re.compile(
    r"\b(?:RN(?:-BC)?|BSN|MSN|DNP|LPN|LVN|CNA|APRN|FNP(?:-BC)?|PMHNP(?:-BC)?|"
    r"CRNA|CCRN|CEN|CNOR|BLS|ACLS|PALS|TNCC|ENPC|NRP|CPR|CST)\b",
    re.I,
)

_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
_US_CODES = set(_US_STATES.values())
_CANADA_REGIONS = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
}
_AUSTRALIA_REGIONS = {"ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA"}
_COUNTRIES = {
    "us": "United States", "usa": "United States", "u.s.": "United States",
    "u.s.a.": "United States", "united states": "United States",
    "united states of america": "United States", "canada": "Canada",
    "australia": "Australia", "india": "India", "philippines": "Philippines",
    "united kingdom": "United Kingdom", "uk": "United Kingdom", "england": "United Kingdom",
    "france": "France", "germany": "Germany", "ireland": "Ireland", "mexico": "Mexico",
    "new zealand": "New Zealand", "south africa": "South Africa", "nigeria": "Nigeria",
    "pakistan": "Pakistan", "singapore": "Singapore", "united arab emirates": "United Arab Emirates",
    "uae": "United Arab Emirates",
}


@dataclass(frozen=True)
class _Location:
    location: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    confidence: float = 0.0


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t,;|\u2022")


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()


def _bounded_unique(values, limit: int = _MAX_FIELD_ITEMS) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        cleaned = _clean(value)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned[:300])
        if len(output) >= limit:
            break
    return output


def _text_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data), strict=False)
    pages: list[str] = []
    for page in reader.pages[: config.RESUME_OCR_MAX_PAGES]:
        try:
            pages.append(str(page.extract_text() or ""))
        except Exception:
            pages.append("")
    return pages


def _tesseract() -> tuple[str, str]:
    configured = str(config.RESUME_OCR_TESSERACT_CMD or "").strip()
    candidates = [configured] if configured else []
    executable = shutil.which("tesseract")
    if executable:
        candidates.append(executable)
    bundled_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "tesseract"
    candidates.append(str(bundled_root / "tesseract.exe"))
    program_files = os.getenv("ProgramFiles", r"C:\Program Files")
    candidates.append(str(Path(program_files) / "Tesseract-OCR" / "tesseract.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            root = Path(candidate).resolve().parent
            tessdata = root / "tessdata"
            return str(Path(candidate).resolve()), str(tessdata) if tessdata.is_dir() else ""
    return "", ""


def _ocr_pages(data: bytes, indexes: list[int]) -> dict[int, str]:
    command, tessdata = _tesseract()
    if not command or not indexes:
        return {}
    try:
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(data)
    except Exception:
        return {}

    environment = os.environ.copy()
    if tessdata:
        environment["TESSDATA_PREFIX"] = tessdata
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result: dict[int, str] = {}
    zoom = max(1.0, float(config.RESUME_OCR_DPI) / 72.0)
    deadline = time.monotonic() + float(config.RESUME_OCR_TOTAL_TIMEOUT)
    try:
        for index in indexes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if index >= len(document):
                continue
            try:
                page = document[index]
                bitmap = page.render(scale=zoom)
                image = bitmap.to_pil()
                buffer = BytesIO()
                image.save(buffer, format="PNG")
                png = buffer.getvalue()
                bitmap.close()
                page.close()
                completed = subprocess.run(
                    [command, "stdin", "stdout", "-l", config.RESUME_OCR_LANGUAGE, "--psm", "6"],
                    input=png,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=max(
                        0.5,
                        min(float(config.RESUME_OCR_PAGE_TIMEOUT), remaining),
                    ),
                    check=False,
                    creationflags=flags,
                    env=environment,
                )
                if completed.returncode == 0:
                    result[index] = completed.stdout.decode("utf-8", errors="replace")
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
    finally:
        document.close()
    return result


def _split_name(value: str) -> dict[str, str]:
    normalized = normalize_person_name(value)
    words = normalized.split()
    if len(words) < 2:
        return {}
    signature = identity_signature(normalized)
    surname_words = len(signature.get("surname") or ())
    # identity_signature tokenizes hyphenated names, so retain the display
    # surname by using the terminal word unless a known particle is present.
    last_start = len(words) - 1
    while last_start > 1 and words[last_start - 1].casefold().strip(".,") in {
        "da", "de", "del", "della", "der", "di", "du", "la", "le", "van", "von",
    }:
        last_start -= 1
    del surname_words
    return {
        "full_name": normalized,
        "first_name": words[0],
        "middle_name": " ".join(words[1:last_start]),
        "last_name": " ".join(words[last_start:]),
    }


def _looks_like_name(value: str) -> bool:
    cleaned = normalize_person_name(value)
    words = cleaned.split()
    if not 2 <= len(words) <= 5 or len(cleaned) > 80:
        return False
    if any(char.isdigit() for char in cleaned) or "@" in cleaned:
        return False
    tokens = {_key(word) for word in words}
    if tokens & _NAME_STOP:
        return False
    return all(re.fullmatch(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*\.?", word, re.UNICODE) for word in words)


def _name(lines: list[str]) -> tuple[dict[str, str], float]:
    for position, line in enumerate(lines[:20]):
        for segment in re.split(r"[|\u2022\t]", line):
            segment = re.sub(r"^(?:candidate\s+name|name)\s*[:\-]\s*", "", segment, flags=re.I)
            candidate = normalize_person_name(segment)
            if _looks_like_name(candidate):
                return _split_name(candidate), 0.91 if position < 5 else 0.84
    return {}, 0.0


def _country(value: str) -> str:
    return _COUNTRIES.get(_key(value), "")


def _state(value: str) -> tuple[str, str]:
    cleaned = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", _clean(value)).strip(" ,")
    key = _key(cleaned)
    upper = cleaned.upper().replace(".", "")
    if key in _US_STATES:
        return _US_STATES[key], "United States"
    if upper in _US_CODES:
        return upper, "United States"
    if upper in _CANADA_REGIONS:
        return upper, "Canada"
    if upper in _AUSTRALIA_REGIONS:
        return upper, "Australia"
    return cleaned, ""


def _parse_location(value: str, *, labeled: bool = False) -> _Location:
    cleaned = _clean(value)
    if not cleaned or "@" in cleaned or len(cleaned) > 180:
        return _Location()
    cleaned = re.sub(r"^(?:address|based\s+in|city|current\s+location|location|lives\s+in)\s*[:\-]\s*", "", cleaned, flags=re.I)
    parts = [_clean(part) for part in cleaned.split(",") if _clean(part)]
    if len(parts) < 2:
        return _Location()

    explicit_country = _country(parts[-1])
    city = state = country = ""
    if explicit_country:
        country = explicit_country
        if len(parts) >= 3:
            city = parts[-3]
            state, inferred = _state(parts[-2])
            country = country or inferred
        else:
            city = parts[-2]
    else:
        state, country = _state(parts[-1])
        if not country:
            return _Location()
        city = parts[-2]

    # A street address may precede the city. Never use a digit-bearing part as
    # the city, and require actual alphabetic city evidence.
    if not city or any(char.isdigit() for char in city) or not re.search(r"[A-Za-z]", city):
        return _Location()
    if _LOCATION_ORGANIZATION_RE.search(city):
        return _Location()
    display = ", ".join(part for part in (city, state, country) if part)
    confidence = 0.91 if labeled else (0.87 if country else 0.80)
    return _Location(display, city, state, country, confidence)


def _location(lines: list[str]) -> _Location:
    for line in lines[:50]:
        match = _LOCATION_LABEL_RE.match(line)
        if match:
            parsed = _parse_location(match.group(1), labeled=True)
            if parsed.location:
                return parsed
    for line in lines[:35]:
        for segment in re.split(r"[|\u2022]", line):
            parsed = _parse_location(segment)
            if parsed.location:
                return parsed
    return _Location()


def _section_values(lines: list[str], section: str, limit: int = 12) -> list[str]:
    aliases = _SECTION_ALIASES[section]
    active = False
    output: list[str] = []
    for line in lines:
        heading = _key(line.rstrip(":"))
        if heading in _ALL_HEADINGS:
            active = heading in aliases
            continue
        if not active:
            continue
        if 2 <= len(line) <= 300:
            output.extend(re.split(r"\s*[|\u2022;]\s*|\s{3,}", line))
        if len(output) >= limit:
            break
    return _bounded_unique(output, limit)


def _fields(text: str) -> tuple[dict[str, Any], dict[str, float]]:
    lines = _bounded_unique((_clean(line) for line in text.splitlines()), limit=1500)
    values: dict[str, Any] = {}
    confidence: dict[str, float] = {}

    parsed_name, name_confidence = _name(lines)
    values.update(parsed_name)
    if parsed_name:
        confidence.update({key: name_confidence for key in parsed_name})

    parsed_location = _location(lines)
    if parsed_location.location:
        values.update({
            "location": parsed_location.location,
            "city": parsed_location.city,
            "state": parsed_location.state,
            "country": parsed_location.country,
        })
        confidence.update({
            key: parsed_location.confidence
            for key in ("location", "city", "state", "country") if values.get(key)
        })

    emails = _bounded_unique(
        (match.group(1).lower() for match in _EMAIL_RE.finditer(text)), 8,
    )
    phones = []
    for line in text.splitlines():
        if re.search(r"\bfax\b", line, re.I):
            continue
        phones.extend(match.group(0) for match in _PHONE_RE.finditer(line))
    phones = _bounded_unique(phones, 8)
    if emails:
        values["emails"] = emails
        confidence["emails"] = 0.96
    if phones:
        values["phones"] = phones
        confidence["phones"] = 0.91

    role = ""
    for line in lines[:30]:
        match = _ROLE_LABEL_RE.match(line)
        if match and _HEALTHCARE_ROLE_RE.search(match.group(1)):
            role = _clean(match.group(1))
            break
    if not role:
        role = next((line for line in lines[:15] if _HEALTHCARE_ROLE_RE.search(line) and len(line) <= 120), "")
    if role:
        values["job_title"] = role
        confidence["job_title"] = 0.82

    skills = _section_values(lines, "skills", 20)
    certifications = _section_values(lines, "certifications", 15)
    licenses = _section_values(lines, "licenses", 15)
    credentials = _bounded_unique(
        (match.group(0).upper() for match in _CREDENTIAL_RE.finditer(text)), 20,
    )
    if credentials:
        certifications = _bounded_unique([*certifications, *credentials], 20)
    for key, items in (
        ("skills", skills), ("certifications", certifications), ("licenses", licenses),
        ("work_history", _section_values(lines, "work_history", 20)),
        ("education", _section_values(lines, "education", 15)),
    ):
        if items:
            values[key] = items
            confidence[key] = 0.72

    specialties = _bounded_unique(
        specialty for specialty in _SPECIALTIES
        if re.search(rf"\b{re.escape(specialty)}\b", text, re.I)
    )
    if specialties:
        values["specialties"] = specialties
        confidence["specialties"] = 0.76

    authorization = next((_clean(line) for line in lines if _WORK_AUTH_RE.search(line)), "")
    availability = next((_clean(line) for line in lines if _AVAILABILITY_RE.search(line)), "")
    summary = " ".join(_section_values(lines, "summary", 5))[:1200]
    for key, value in (
        ("work_authorization", authorization),
        ("availability", availability),
        ("experience_summary", summary),
    ):
        if value:
            values[key] = value
            confidence[key] = 0.72
    return values, confidence


def _same_identity(left: str, right: str) -> bool:
    a = identity_signature(left)
    b = identity_signature(right)
    return bool(a.get("first") and a.get("first") == b.get("first") and a.get("surname") == b.get("surname"))


def _candidate_location(candidate: Mapping[str, Any]) -> _Location:
    direct = _parse_location(str(candidate.get("location") or ""), labeled=True)
    city = _clean(candidate.get("city")) or direct.city
    state, inferred_country = _state(_clean(candidate.get("state") or candidate.get("region")) or direct.state)
    country = _country(_clean(candidate.get("country"))) or _clean(candidate.get("country")) or direct.country or inferred_country
    location = _clean(candidate.get("location")) or ", ".join(part for part in (city, state, country) if part)
    return _Location(location, city, state, country, 1.0 if location else 0.0)


def _accepted(fields: Mapping[str, Any], confidence: Mapping[str, float], candidate: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    accepted: dict[str, Any] = {}
    conflicts: list[str] = []
    platform_name = normalize_person_name(str(candidate.get("name") or ""))
    resume_name = normalize_person_name(str(fields.get("full_name") or ""))
    if platform_name:
        if resume_name and not _same_identity(platform_name, resume_name):
            conflicts.append("name")
    elif resume_name and float(confidence.get("full_name") or 0) >= config.RESUME_OCR_ACCEPT_CONFIDENCE:
        for key in ("full_name", "first_name", "middle_name", "last_name"):
            if fields.get(key):
                accepted[key] = fields[key]

    source_location = _candidate_location(candidate)
    resume_location = _Location(
        _clean(fields.get("location")), _clean(fields.get("city")),
        _clean(fields.get("state")), _clean(fields.get("country")),
        float(confidence.get("location") or 0),
    )
    if source_location.location:
        city_matches = not resume_location.city or _key(source_location.city) == _key(resume_location.city)
        state_matches = not resume_location.state or _key(source_location.state) == _key(resume_location.state)
        if resume_location.location and not (city_matches and state_matches):
            conflicts.append("location")
        elif city_matches and state_matches:
            # Fill only missing components that corroborate the captured place.
            if not source_location.city and resume_location.city:
                accepted["city"] = resume_location.city
            if not source_location.state and resume_location.state:
                accepted["state"] = resume_location.state
            if not source_location.country and resume_location.country:
                accepted["country"] = resume_location.country
    elif resume_location.location and resume_location.confidence >= config.RESUME_OCR_ACCEPT_CONFIDENCE:
        for key in ("location", "city", "state", "country"):
            if fields.get(key):
                accepted[key] = fields[key]

    has_role = any(_clean(candidate.get(key)) for key in ("job_title", "role", "title"))
    if not has_role and fields.get("job_title") and float(confidence.get("job_title") or 0) >= config.RESUME_OCR_ROLE_CONFIDENCE:
        accepted["job_title"] = fields["job_title"]
    return accepted, sorted(set(conflicts))


def extract(data: bytes, candidate: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Extract a bounded structured projection from a PDF resume.

    Failures never block resume storage.  The return object is safe to persist
    privately: it excludes the full document/OCR transcript and contains an
    explicit list of fields approved only for filling platform-data gaps.
    """
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "unavailable",
        "source": "none",
        "scanned": False,
        "pages_total": 0,
        "pages_ocr": 0,
        "fields": {},
        "confidence": {},
        "provenance": {},
        "accepted": {},
        "conflicts": [],
    }
    if not config.RESUME_OCR_ENABLED or not isinstance(data, bytes) or not data.startswith(b"%PDF"):
        return base
    try:
        pages = _text_pages(data)
    except Exception:
        return base
    base["pages_total"] = len(pages)
    sparse = [
        index for index, text in enumerate(pages)
        if len(re.sub(r"\W+", "", text)) < config.RESUME_OCR_MIN_PAGE_CHARS
    ]
    base["scanned"] = bool(pages and len(sparse) == len(pages))
    try:
        ocr = _ocr_pages(data, sparse) if sparse else {}
    except Exception:
        ocr = {}
    combined: list[str] = []
    for index, embedded in enumerate(pages):
        combined.append(ocr.get(index) or embedded)
    has_embedded = any(index not in sparse and _clean(text) for index, text in enumerate(pages))
    has_ocr = any(_clean(text) for text in ocr.values())
    base["pages_ocr"] = len([text for text in ocr.values() if _clean(text)])
    base["source"] = "mixed" if has_embedded and has_ocr else ("local_ocr" if has_ocr else ("embedded_text" if has_embedded else "none"))
    text = "\n".join(combined)
    if not _clean(text):
        return base
    try:
        fields, confidence = _fields(text)
        accepted, conflicts = _accepted(fields, confidence, candidate or {})
    except Exception:
        return base
    base.update({
        "status": "extracted" if fields else "no_fields",
        "fields": fields,
        "confidence": confidence,
        "provenance": {key: base["source"] for key in fields},
        "accepted": accepted,
        "conflicts": conflicts,
    })
    return base


__all__ = ["SCHEMA_VERSION", "extract"]
