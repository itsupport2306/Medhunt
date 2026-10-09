"""Review or repair missing Nexus specialties from candidate resumes.

Dry run by default. No contact data or resume text is written to the report.
Use --resume-dir with PDFs named <candidateId>.pdf, or supply an authenticated
Nexus path via --resume-path (for example /.../candidates/{id}/resume).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from urllib.parse import quote

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_pkg"))
from sourcing import nexus_sync, resume_extraction  # noqa: E402
from sourcing.nexus_reference import load_reference  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
API_PREFIX = "/api/api-integration/v1/candidates"


def settings_from_files() -> nexus_sync.NexusSettings:
    env = dotenv_values(ROOT / ".env")
    reference = load_reference(
        env.get("NEXUS_REFERENCE_ENV", ""), env.get("NEXUS_REFERENCE_CONFIG", "")
    )
    credentials: dict[str, str] = {}
    for line in (ROOT / "new_api.txt").read_text(encoding="utf-8-sig").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            credentials.setdefault(key.strip().lstrip("\ufeff\u200b"), value.strip())
    if not credentials.get("username") or not credentials.get("password"):
        raise ValueError("new_api.txt must contain username and password")
    return nexus_sync.NexusSettings(
        enabled=True,
        base_url=env.get("NEXUS_API_BASE_URL") or "https://api-nexus.laboredge.com:9000",
        auth_method="password", token_url=reference.get("NEXUS_TOKEN_URL", ""),
        token_payload_style=reference.get("NEXUS_TOKEN_PAYLOAD_STYLE", "form"),
        username=credentials["username"], password=credentials["password"],
        org_code=credentials.get("organizationCode", ""),
        token_basic=reference.get("NEXUS_TOKEN_BASIC", ""),
        timeout_seconds=30, connect_timeout_seconds=8,
    )


def ids(value: object) -> list[int]:
    if value is None or value == "":
        return []
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def needs_update(candidate: dict, unknown_specialty_ids: set[int]) -> bool:
    current = set(ids(candidate.get("specialtyIds")))
    primary = set(ids(candidate.get("primarySpecialtyId")))
    selected = current | primary
    return not selected or selected <= unknown_specialty_ids


def classification(
    candidate: dict, extraction: dict, professions: list[dict], specialties: list[dict],
) -> tuple[int, tuple[int, ...], int] | None:
    """Select ordered specialties and a primary from résumé evidence."""
    fields = extraction.get("fields") or {}
    confidence = extraction.get("confidence") or {}
    if extraction.get("status") != "extracted" or extraction.get("conflicts"):
        return None
    role = str(fields.get("job_title") or "") if float(confidence.get("job_title") or 0) >= 0.8 else ""
    known_professions = set(ids(candidate.get("professionIds")))
    profession_by_label = {
        nexus_sync._label(row.get("name")): int(nexus_sync._master_id(row, "professions"))
        for row in nexus_sync._active(professions)
        if nexus_sync._master_id(row, "professions") is not None
    }
    unknown_profession = profession_by_label.get("unknown")
    known_professions.discard(unknown_profession)
    inferred = [profession_by_label.get(nexus_sync._label(label)) for label in nexus_sync._profession_labels(role)] if role else []
    inferred = [value for value in inferred if value and value != unknown_profession]
    if not inferred:
        # Some résumés omit a headline but carry an explicit professional
        # license or credential. Use it only when it identifies one profession.
        credential_professions: set[int] = set()
        for value in [
            *(fields.get("licenses") or []),
            *(fields.get("certifications") or []),
        ]:
            for label in nexus_sync._profession_labels(str(value)):
                profession = profession_by_label.get(nexus_sync._label(label))
                if profession and profession != unknown_profession:
                    credential_professions.add(profession)
        if len(credential_professions) == 1:
            inferred = list(credential_professions)
    if len(known_professions) == 1:
        profession_id = next(iter(known_professions))
        if inferred and profession_id not in inferred:
            return None  # conflicting evidence requires human review
    elif len(set(inferred)) == 1:
        profession_id = inferred[0]
    else:
        return None

    extracted = [
        str(value) for value in fields.get("specialties") or []
    ] if float(confidence.get("specialties") or 0) >= 0.75 else []
    headline_labels = list(nexus_sync._role_specialty_labels(role, ())) if role else []
    labels = nexus_sync._text_values([*headline_labels, *extracted])
    resolved: list[int] = []
    for label in labels:
        matched_id = None
        for equivalent in nexus_sync._specialty_master_labels([label]):
            matches = {
                int(nexus_sync._master_id(row, "specialties"))
                for row in nexus_sync._active(specialties)
                if nexus_sync._master_id(row, "specialties") is not None
                and int(row.get("professionId") or 0) == profession_id
                and nexus_sync._label(equivalent) in {
                    nexus_sync._label(row.get("name")), nexus_sync._label(row.get("label")),
                    nexus_sync._label(row.get("code")),
                }
                and nexus_sync._label(row.get("name")) != "unknown"
            }
            if len(matches) == 1:
                matched_id = next(iter(matches))
                break
        if matched_id is not None and matched_id not in resolved:
            resolved.append(matched_id)
    return (profession_id, tuple(resolved), resolved[0]) if resolved else None


def resume_bytes(client: nexus_sync.NexusClient, candidate_id: str, args: argparse.Namespace) -> bytes | None:
    if args.resume_dir:
        path = args.resume_dir / f"{candidate_id}.pdf"
        return path.read_bytes() if path.is_file() else None
    path = args.resume_path.replace("{id}", quote(candidate_id, safe=""))
    if not path.startswith(API_PREFIX + "/") or "://" in path:
        raise ValueError("--resume-path must be a same-origin Nexus candidate API path")
    response = client.request("GET", path, operation="resume download")
    return response.content if response.content.startswith(b"%PDF") else None


def run(args: argparse.Namespace) -> dict[str, int]:
    if not args.resume_dir and not args.resume_path:
        raise ValueError("Provide --resume-dir or --resume-path")
    if args.resume_dir and not args.resume_dir.is_dir():
        raise ValueError("Resume directory does not exist")
    if args.resume_path and "{id}" not in args.resume_path:
        raise ValueError("--resume-path must contain {id}")
    settings = settings_from_files()
    client = nexus_sync.NexusClient(settings)
    totals = {key: 0 for key in ("visible", "affected", "missing_resume", "unresolved", "proposed", "updated", "changed_since_review", "error")}
    reviewed = set()
    approved_proposals: dict[str, tuple[int, tuple[int, ...], int]] = {}
    if args.output.exists():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if item.get("status") == "updated":
                    reviewed.add(str(item["candidate_id"]))
                elif item.get("status") == "proposed":
                    approved_proposals[str(item["candidate_id"])] = (
                        int(item["profession_id"]),
                        tuple(int(value) for value in item["specialty_ids"]),
                        int(item["primary_specialty_id"]),
                    )
            except (KeyError, ValueError, TypeError):
                continue
    if args.apply and not approved_proposals:
        raise ValueError("Run a dry run to create and review proposals in --output before --apply")
    try:
        professions = client.get_master("professions")
        specialties = client.get_master("specialties")
        unknown_ids = {
            int(nexus_sync._master_id(row, "specialties"))
            for row in specialties
            if nexus_sync._master_id(row, "specialties") is not None
            and nexus_sync._label(row.get("name")) == "unknown"
        }
        unknown_profession_ids = {
            int(nexus_sync._master_id(row, "professions"))
            for row in professions
            if nexus_sync._master_id(row, "professions") is not None
            and nexus_sync._label(row.get("name")) == "unknown"
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("a", encoding="utf-8") as report:
            start = 0
            while True:
                response = client.request("POST", API_PREFIX + "/search", operation="candidate search", json={"pagingSortingDetails": {"start": start, "maxRowsToFetch": 100}})
                body = response.json()
                rows = nexus_sync._rows(body, preferred=("records",))
                if start == 0:
                    print("Nexus visible candidate count:", body.get("count"))
                if not rows:
                    break
                totals["visible"] += len(rows)
                for candidate in rows:
                    candidate_id = str(nexus_sync._row_id(candidate) or "")
                    if not candidate_id or candidate_id in reviewed or not needs_update(candidate, unknown_ids):
                        continue
                    totals["affected"] += 1
                    item: dict[str, object] = {"candidate_id": candidate_id}
                    try:
                        pdf = resume_bytes(client, candidate_id, args)
                        if not pdf or not pdf.startswith(b"%PDF"):
                            item["status"] = "missing_resume"
                        else:
                            extracted = resume_extraction.extract(pdf)
                            proposed = classification(candidate, extracted, professions, specialties)
                            if not proposed:
                                item["status"] = "unresolved"
                            else:
                                profession_id, specialty_ids, primary_specialty_id = proposed
                                item.update(
                                    status="proposed", profession_id=profession_id,
                                    specialty_ids=list(specialty_ids),
                                    primary_specialty_id=primary_specialty_id,
                                )
                                if args.apply:
                                    if approved_proposals.get(candidate_id) != proposed:
                                        item["status"] = "changed_since_review"
                                    else:
                                        latest = client.request("GET", API_PREFIX + "/" + quote(candidate_id, safe=""), operation="candidate recheck").json()
                                        known_latest_professions = set(ids(latest.get("professionIds"))) - unknown_profession_ids
                                        if not needs_update(latest, unknown_ids) or known_latest_professions - {profession_id}:
                                            item["status"] = "changed_since_review"
                                        else:
                                            patch = {"professionIds": [profession_id], "specialtyIds": list(specialty_ids), "primarySpecialtyId": primary_specialty_id}
                                            client.request("PATCH", API_PREFIX + "/" + quote(candidate_id, safe=""), operation="candidate classification update", write=True, json=patch)
                                            verified = client.request("GET", API_PREFIX + "/" + quote(candidate_id, safe=""), operation="candidate verification").json()
                                            if set(ids(verified.get("professionIds"))) != {profession_id} or set(ids(verified.get("specialtyIds"))) != set(specialty_ids) or ids(verified.get("primarySpecialtyId")) != [primary_specialty_id]:
                                                raise RuntimeError("Nexus did not retain the proposed classification")
                                            item["status"] = "updated"
                    except Exception as exc:
                        item["status"] = "error"
                        item["error_type"] = type(exc).__name__
                    totals[str(item["status"])] += 1
                    report.write(json.dumps(item, sort_keys=True) + "\n")
                    report.flush()
                    if args.limit and totals["affected"] >= args.limit:
                        return totals
                    if args.delay:
                        time.sleep(args.delay)
                start += len(rows)
                if len(rows) < 100:
                    break
        return totals
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--resume-dir", type=Path, help="PDFs named <candidateId>.pdf")
    source.add_argument("--resume-path", help="Authenticated Nexus path containing {id}")
    parser.add_argument("--apply", action="store_true", help="PATCH and verify proposed changes; default is dry run")
    parser.add_argument("--limit", type=int, default=0, help="Maximum affected candidates; 0 means all")
    parser.add_argument("--delay", type=float, default=0.1, help="Seconds between affected candidates")
    parser.add_argument("--output", type=Path, default=Path("nexus_specialty_backfill.jsonl"))
    args = parser.parse_args()
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
