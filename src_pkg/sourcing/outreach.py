"""
Outreach drafting with compliance guardrails.

Drafts a personalized recruiting email per candidate (Gemini if available, else a
solid template). NOTHING is sent automatically — drafts are saved for human review.
Enforces the do-not-contact list and email-first default (phone/SMS need TCPA consent).
"""
from __future__ import annotations

from . import store, llm, config, contact_access

_TEMPLATE = (
    "Hi {first},\n\n"
    "I'm a recruiter at Radixsol and came across your background for a {title} "
    "opportunity{loc}. Based on your experience, I think there could be a strong fit.\n\n"
    "Would you be open to a brief call this week to discuss the role, the team, and "
    "compensation? If the timing isn't right, just reply STOP and I won't reach out again.\n\n"
    "Best regards,\nRadixsol Talent Team\n"
)


def _first_name(name: str) -> str:
    return (name or "there").strip().split()[0] if name else "there"


def draft_for_candidate(candidate_id: int, job: dict | None = None,
                        channel: str | None = None) -> dict:
    cand = store.get_candidate(candidate_id)
    if not cand:
        return {"error": "candidate not found"}
    channel = channel or config.DEFAULT_CHANNEL
    contactable = contact_access.project_candidate(cand)

    # compliance gates
    normalized_channel = str(channel or "").strip().casefold()
    if normalized_channel == "email":
        targets = list(contactable.get("emails") or [])
    elif normalized_channel in {"sms", "text", "text_message"}:
        targets = [
            item.get("value") for item in contactable.get("phone_contacts") or []
            if item.get("kind") == "mobile" and item.get("value")
        ]
    else:
        targets = list(contactable.get("phones") or [])
    if not targets:
        return {"error": f"No usable {channel} on file (missing or on do-not-contact list). "
                         "Enrich the candidate first."}

    title = (job or {}).get("title", "an open") if job else "an open"
    loc = f" in {cand.get('location')}" if cand.get("location") else ""
    first = _first_name(cand["name"])

    subject = f"{title} opportunity at Radixsol"
    body = _TEMPLATE.format(first=first, title=title, loc=loc)

    if llm.available():
        prompt = f"""Write a short, warm, professional recruiting outreach EMAIL (<=150 words).
Personalize to the candidate and role. Include a clear call to action for a brief call,
and an opt-out line ("reply STOP to opt out"). Do not fabricate specifics you weren't given.

Candidate: {cand['name']} ({cand.get('location','')})
Role: {title}
Company: Radixsol (staffing & workforce solutions)"""
        out = llm.generate(prompt, temperature=0.6, max_tokens=400)
        if out:
            body = out
            if "stop" not in body.lower():
                body += "\n\n(Reply STOP to opt out.)"

    oid = store.save_outreach(candidate_id, channel, subject, body, status="draft")
    if cand["stage"] in ("new", "enriched"):
        store.set_stage(candidate_id, "contacted")  # marks intent; actual send is manual
    return {"outreach_id": oid, "candidate_id": candidate_id, "channel": channel,
            "to": targets, "subject": subject, "body": body, "status": "draft",
            "compliance": config.COMPLIANCE_NOTICE}


def approve_and_mark_sent(outreach_id: int) -> dict:
    """Human approves; we record it as sent (the actual send is done by your email
    provider — this tool does not blast messages)."""
    if not store.mark_outreach(outreach_id, "approved"):
        return {"error": "outreach draft not found"}
    return {"outreach_id": outreach_id, "status": "approved",
            "note": "Send via your own email system; then mark as sent."}
