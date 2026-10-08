"""Keep each Medhunt recruiter on exactly one assigned ATS workflow."""
from __future__ import annotations

from collections.abc import Mapping

from . import store


def destination_for(identity: Mapping | None) -> str:
    """Use an explicit exclusive assignment, defaulting to Nexus.

    Conflicting legacy flags resolve to Nexus; Halo prevents creating new
    conflicts, and no candidate is sent to both ATSs.
    """
    targets = (identity or {}).get("delivery_targets") or {}
    return "ceipal" if targets.get("ceipal") is True and targets.get("nexus") is not True else "nexus"


def set_candidate_target(candidate_id: int, destination: str, user_id: str = "") -> None:
    """Persist the route for a candidate already validated by its caller."""
    owner = str(user_id or "local")
    store.set_candidate_ats_route(candidate_id, owner, destination)


def stored(
    candidate: Mapping | None, user_id: str = "", default_destination: str = "nexus",
) -> dict:
    source = candidate or {}
    user_route = store.get_candidate_ats_route(
        int(source.get("id") or 0), str(user_id or "local"),
    ) if source.get("id") else None
    routing = user_route or {}
    destination = str(
        routing.get("destination") or default_destination or "nexus"
    ).casefold()
    # Duplicate-presence checks against external ATSs are deliberately omitted.
    # They were synchronous to contact lookup and added substantial latency.
    # Candidate creation is sent directly to the assigned ATS; no remote
    # duplicate query is part of this route.
    return {"destination": destination, "eligibility": {}}


def check_after_enrichment(candidate_id: int, destination: str, user_id: str = "") -> dict:
    """Compatibility shim: ATS duplicate checks are disabled in this flow."""
    target = "ceipal" if destination == "ceipal" else "nexus"
    return {"state": "disabled", "blocked": False, "checked": False, "target": target}
