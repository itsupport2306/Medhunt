# Nexus specialty repair

The MedHunt delivery path now sends all matched specialties for one profession, with the source/current role or first résumé mention as the primary specialty. A résumé with several valid specialties no longer falls back to `Unknown` merely because there are several. New Nexus candidates receive the matched profession and specialties during creation. If MedHunt already has a durable Nexus link, a later résumé capture repairs an absent or `Unknown` specialty when the evidence matches that profession. A specific existing specialty is preserved.

The maintenance script scans the Nexus candidates visible to the credentials in the repository-root `new_api.txt`. It targets candidates whose specialty is absent or only `Unknown`, reads a PDF résumé, and proposes a profession, every matched specialty for that profession, and one primary specialty from live Nexus master data. The current role or résumé headline takes primary priority; otherwise the first specialty mentioned in the résumé is primary. Records without any matching specialty or with conflicting profession evidence are marked `unresolved`. It writes a JSONL report containing candidate IDs, statuses, and proposed numeric IDs; it does not save résumé text or contact details.

## Run with an exported résumé folder

Name each PDF `<candidateId>.pdf` and run from the repository root:

```powershell
python .worktrees/medhunt1_backend_preview_20261008/scripts/backfill_nexus_specialties.py --resume-dir C:\path\to\resumes --limit 100 --output nexus_specialty_review.jsonl
```

Review the proposed IDs in the report. To apply, rerun with `--apply` and the same source and output arguments. The script requires a matching proposal from that report, refetches each candidate before PATCH, updates only the three classification fields, and reads it again to verify the result. An `updated` candidate is skipped on subsequent runs.

## Run with a Nexus résumé download endpoint

If Nexus supplies an authenticated PDF endpoint, pass its **path** with `{id}` for the candidate ID, for example:

```powershell
python .worktrees/medhunt1_backend_preview_20261008/scripts/backfill_nexus_specialties.py --resume-path '/api/api-integration/v1/candidates/{id}/EXACT_RESUME_PATH' --limit 100 --output nexus_specialty_review.jsonl
```

Replace `EXACT_RESUME_PATH` with the documented endpoint. The script requires a same-origin candidate API path and a PDF response. The supplied `new_api.txt` contains login values but no download endpoint. `JOB_BOARD_API_PROD_3.2 (1).pdf` lists 26 API operations, including candidate search, detail, and PATCH, but no résumé download or parse operation. Live candidate search and detail responses expose no résumé field. If the download API returns JSON or needs a document ID, its adapter must be added before this mode can run.

The current Radix credentials returned about 8,290 visible candidates during a read-only test. The reported 55,000 may include candidates outside this account's API scope; the script prints the live visible count each run.
