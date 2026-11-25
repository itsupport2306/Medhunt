"""Best-effort public state-license discovery for Nexus delivery.

Only dated license statements on an exact, location-matched profile are used.
Search pages and snippets are never evidence. Network failures leave the
candidate unchanged; they must not fail contact enrichment or Nexus delivery.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx

from . import resume_extraction


_SITES = (
    ("doximity", "www.doximity.com", "/pub/",
     "https://www.doximity.com/find_web/public_search/users", "query"),
    ("healthgrades", "www.healthgrades.com", "/physician/",
     "https://www.healthgrades.com/usearch", "what"),
    ("usnews", "health.usnews.com", "/doctors/",
     "https://health.usnews.com/doctors/search", "name"),
)
_STATE_NAMES = "|".join(
    re.escape(name) for name in sorted(resume_extraction._US_STATES, key=len, reverse=True)
    if len(name) > 2
)
_LICENSE = re.compile(
    rf"\b([A-Z]{{2}}|{_STATE_NAMES})\s+State\s+(?:Medical\s+)?Licen[cs]e\b"
    r".{0,90}?(?:Active\s+through\s+(20\d{2})|"
    r"(?:19|20)\d{2}\s*[-\u2013\u2014]\s*(20\d{2})|"
    r"Expires?\s+(?:\w+\s+\d{1,2},?\s+)?(20\d{2}))",
    re.I,
)
_NAME_SUFFIX = re.compile(r"\b(?:MD|DO|DDS|DMD|NP|CRNA|RN|PA-C|PhD|DNP)\b", re.I)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.text: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in {"p", "div", "li", "dt", "dd", "h1", "h2", "br"}:
            self.text.append(" ")

    def handle_data(self, data):
        self.text.append(data)
        if self._in_title:
            self.title += data


def _page(markup: str) -> _Page:
    result = _Page()
    result.feed(markup[:500_000])
    return result


def _name(value: str) -> str:
    # Directory profile titles append credentials, location, and specialty.
    # Split on Unicode or ASCII dashes before normalizing the name.
    value = re.split(r"\s*(?:\||\u2013|\u2014|\s+-\s+)\s*", value, maxsplit=1)[0]
    value = re.sub(r"\bDr\.?\s+", "", value, flags=re.I)
    value = _NAME_SUFFIX.sub("", value)
    parts = re.findall(r"[a-z]+", value.casefold())
    # Candidate sources may omit a middle initial shown in the profile title.
    parts = [part for index, part in enumerate(parts)
             if not (0 < index < len(parts) - 1 and len(part) == 1)]
    return "".join(parts)


def _candidate_state(location: str) -> str:
    names = resume_extraction._US_STATES
    for part in reversed(re.split(r"[,|]", location)):
        value = part.strip().casefold()
        if value in names:
            return names[value]
        if value.upper() in names.values():
            return value.upper()
    return ""


def _profile_matches(page: _Page, candidate: dict) -> bool:
    expected_name = _name(str(candidate.get("name") or ""))
    title = unescape(page.title)
    if not expected_name or _name(title) != expected_name:
        return False
    npi = str(candidate.get("npi") or candidate.get("source_id") or "").strip()
    if str(candidate.get("source") or "").casefold() in {
        "npino", "npiprofile", "usnews", "sharecare", "healthgrades"
    } and re.fullmatch(r"\d{10}", npi):
        if re.search(rf"\bNPI\s*(?:number\s*)?(?:#|:)?\s*{re.escape(npi)}\b", " ".join(page.text), re.I):
            return True
    state = _candidate_state(str(candidate.get("location") or ""))
    if not state:
        return False
    # A location on the candidate profile is required. License state itself
    # cannot prove identity because clinicians can hold multiple licenses.
    title_location = title.split("|", 1)[0]
    city = str(candidate.get("location") or "").split(",", 1)[0].strip()
    if city and "," in str(candidate.get("location") or ""):
        if not re.search(rf"\b{re.escape(city)}\b", title_location, re.I):
            return False
    if re.search(rf"\b{re.escape(state)}\b", title_location, re.I):
        return True
    full_name = next((name for name, code in resume_extraction._US_STATES.items()
                      if code == state and len(name) > 2), "")
    return bool(full_name and re.search(rf"\b{re.escape(full_name)}\b", title_location, re.I))


def _states(page: _Page) -> list[str]:
    text = re.sub(r"\s+", " ", " ".join(page.text))
    year = datetime.now(timezone.utc).year
    names = resume_extraction._US_STATES
    found = []
    for match in _LICENSE.finditer(text):
        if re.search(r"\b(?:inactive|expired|suspended|revoked|lapsed)\b", match.group(0), re.I):
            continue
        if int(next(group for group in match.groups()[1:] if group)) < year:
            continue
        code = names.get(match.group(1).casefold(), match.group(1).upper())
        if code in names.values() and code not in found:
            found.append(code)
    return found


def discover_with_diagnostics(candidate: dict, *, client: httpx.Client | None = None) -> tuple[dict, dict]:
    """Return verified evidence and bounded per-source outcomes."""
    diagnostics = {"sources": []}
    source = str(candidate.get("source") or "").casefold()
    candidate_npi = str(candidate.get("npi") or candidate.get("source_id") or "").strip()
    npi_can_verify = (
        source in {"npino", "npiprofile", "usnews", "sharecare", "healthgrades"}
        and re.fullmatch(r"\d{10}", candidate_npi) is not None
    )
    if not _name(str(candidate.get("name") or "")):
        diagnostics["reason"] = "missing_name"
        return {}, diagnostics
    if not _candidate_state(str(candidate.get("location") or "")) and not npi_can_verify:
        diagnostics["reason"] = "missing_location_and_verified_npi"
        return {}, diagnostics
    owned = client is None
    if owned:
        client = httpx.Client(timeout=httpx.Timeout(3.0), follow_redirects=False,
                              headers={"User-Agent": "Medhunt license verification/1.0"})
    assert client is not None
    deadline = time.monotonic() + 18.0
    try:
        for source, host, prefix, search_url, query_key in _SITES:
            if time.monotonic() >= deadline:
                diagnostics["reason"] = "deadline_reached"
                break
            outcome = {"source": source, "status": "no_matching_profile", "profiles_checked": 0}
            diagnostics["sources"].append(outcome)
            params = {query_key: str(candidate["name"])}
            if source == "healthgrades" and str(candidate.get("location") or "").strip():
                params["where"] = str(candidate["location"])
            try:
                search = client.get(search_url, params=params)
                if search.status_code != 200:
                    outcome["status"] = "search_http_error"
                    outcome["http_status"] = search.status_code
                    continue
                if len(search.content) > 750_000:
                    outcome["status"] = "search_too_large"
                    continue
                links = _page(search.text).links
                profiles_checked = 0
                for href in links[:150]:
                    if time.monotonic() >= deadline or profiles_checked >= 6:
                        break
                    try:
                        url = urljoin(search_url, unescape(href))
                        parts = urlsplit(url)
                    except ValueError:
                        continue
                    if (parts.scheme != "https"
                            or parts.hostname not in {host, host.removeprefix("www.")}
                            or not parts.path.startswith(prefix)
                            or parts.path in {"/doctors/search", "/doctors/"}):
                        continue
                    profiles_checked += 1
                    outcome["profiles_checked"] = profiles_checked
                    profile = client.get(url)
                    if profile.status_code != 200 or len(profile.content) > 750_000:
                        outcome["status"] = "profile_unavailable"
                        continue
                    page = _page(profile.text)
                    if not _profile_matches(page, candidate):
                        if outcome["status"] != "no_current_dated_license":
                            outcome["status"] = "identity_mismatch"
                        continue
                    states = _states(page)
                    if states:
                        outcome["status"] = "verified"
                        return {"source": source, "source_url": url, "states": states}, diagnostics
                    outcome["status"] = "no_current_dated_license"
            except httpx.HTTPError as exc:
                outcome["status"] = "network_error"
                outcome["error_type"] = type(exc).__name__
                continue
    finally:
        if owned:
            client.close()
    return {}, diagnostics


def discover(candidate: dict, *, client: httpx.Client | None = None) -> dict:
    """Return first verified source's states; do not mix uncertain profiles."""
    evidence, _ = discover_with_diagnostics(candidate, client=client)
    return evidence
