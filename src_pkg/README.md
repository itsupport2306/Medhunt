# Medhunt

A Chrome/Edge side-panel extension backed by FastAPI and Neon/PostgreSQL. It scans
candidate cards in authorized Indeed Smart Sourcing, Vivian Talent Pool,
ZipRecruiter recruiter results, LinkedIn People results, public NPI No.,
NPI Profile, U.S. News Doctor Finder, MediFind, and CommonSpirit Health provider directories, and NYSED
license-verification results, plus
individual LinkedIn `/in/` and Facebook profiles,
automatically saves captured profiles, supports reviewed contact
enrichment, ranks candidates against jobs, drafts compliant outreach, and
tracks the recruiting pipeline. SQLite remains available as the local test and
offline fallback.

Candidate data and API credentials are handled by the Python service. Secrets
are never placed in the extension.

## Windows team installer

The reproducible Windows package is built with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\packaging\build.ps1
```

The resulting `release\Medhunt-Setup.exe` installs the frozen local backend,
places the unpacked extension in a stable per-user folder, starts the backend
at Windows sign-in, and provides upgrade/uninstall entries and a browser setup
guide. Python is not required on the target computer. Chrome or Edge still
requires the user to choose **Load unpacked** once; ordinary Windows programs
cannot silently install an off-store extension into an unmanaged browser.

Trusted-team installer builds use a strict allowlist to package only the PDL and
Enformion lookup configuration. Database, storage, AI, email-validation, and
other project credentials remain excluded, and SQLite is forced locally.
Runtime configuration, data, and logs live under `%LOCALAPPDATA%\Radixsol` and
survive an in-place upgrade. Because embedded credentials can be extracted from
the EXE or installed backend configuration, distribute this package only to
approved team members and rotate its keys if a copy leaves that group.

## Features

- Guided scan and display of up to 100 candidate or provider rows loaded in
  Indeed, Vivian, ZipRecruiter, LinkedIn People, NPI No., NPI Profile,
  U.S. News Doctor Finder, MediFind, CommonSpirit Health, or NYSED
  results, plus individual LinkedIn and Facebook profile capture
- Scan progress, select-all/individual selection, and batched lookup progress
- Whole-selection contact lookup through the People Data Labs bulk endpoint
- Automatic batch persistence of every detected result to the configured database
- Source deduplication plus cross-platform master-candidate linking by verified provider ID
- Distinct Medhunt candidate workbench with compact ready/no-contact/retry filters
- Selection and reviewed licensed contact enrichment
- Internal-database-first identity resolution with NPPES healthcare corroboration
- PDL Identify/Search fallback for unresolved identities, followed by Person Enrichment
- Deterministic conflict rejection and explicit captured/review/rejected/verified states
- Identity confidence kept separate from email deliverability and phone ownership
- Indeed PDF Blob/fetch/XHR capture with visible download and private R2 storage
- Recruiter-controlled LinkedIn **More > Save to PDF** capture and private storage
- One-click capture and review of an open supported-platform profile
- Candidate contact lookup through Quick Sourcer, storing and showing exactly
  what the external API delivers, with do-not-contact suppression the only
  filter applied
- On-demand public-records detail for any captured or stored candidate
- Manual, pasted-text, or CSV intake
- Licensed Enformion/Endato enrichment
- Deterministic demo enrichment when credentials are not configured
- Job ranking using captured profile text
- Human-reviewed outreach drafts with no automatic sending
- Pipeline and do-not-contact management

The extension reads result cards loaded in the current page and scrolls the
current result container to collect virtualized cards. It does not advance to a
different results page automatically. Use it only with an account and candidate
information you are authorized to access.

## 1. Environment

The service automatically loads `.env` from the workspace root. The supplied
`.gitignore` excludes this file from source control.

It then loads `.env.local` with higher priority. The current local override
selects resources dedicated to this application:

```text
DATABASE_BACKEND=postgresql
DATABASE_NAME=radixsol_sourcing
MEDHUNT_DATABASE_SCHEMA=medhunt
STORAGE_ENABLED=1
S3_BUCKET=radixsol-sourcing-resumes
```

Important settings:

```text
DATABASE_URL=<pooled Neon PostgreSQL connection>
MEDHUNT_DATABASE_SCHEMA=medhunt
ENFORMION_AP_NAME=<licensed access profile>
ENFORMION_AP_PASSWORD=<licensed password>
PDL_API_KEY=<server-side key>
PDL_ENABLED=1
PDL_RUN_CREDIT_LIMIT=0
PDL_CACHE_TTL_SECONDS=2592000
GEMINI_API_KEY=<optional>
AI_MATCH_ENABLED=0
IDENTITY_MATCH_THRESHOLD=0.78
IDENTITY_AMBIGUITY_MARGIN=0.08
NPI_ENABLED=1
QUICK_SOURCER_API_KEY=<shared Hub key>
QUICK_SOURCER_ENABLED=1
CONTACT_LOOKUP_PROVIDER=quick_sourcer
VERIFY_EMAILS=0
NEVERBOUNCE_API_KEY=<optional>
```

When `DATABASE_BACKEND=sqlite`, the application ignores `DATABASE_URL` even if
the operating system or `.env` contains one. To enable Neon later, replace the
local override only after creating the dedicated Medhunt database. If a
PostgreSQL database is shared with Nexus, Medhunt uses the isolated `medhunt`
schema by default so tables such as `users` and `candidates` cannot collide
with the other application.
When `STORAGE_ENABLED=1`, downloaded resume PDFs are uploaded to the configured
private Cloudflare R2 bucket. The database stores the R2 object key, checksum,
MIME type, and size alongside the candidate record. Keep it disabled until the
bucket name and `DATABASE_URL` both point to resources dedicated to Medhunt.

## 2. Start the backend

From `src_pkg`:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn api:app --host 127.0.0.1 --port 8091
```

The web interface is available at
[http://127.0.0.1:8091](http://127.0.0.1:8091).

## 3. Load the extension

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select `src_pkg/frontend`.
5. Pin Medhunt and open its side panel.

After updating the source, use **Reload** on the extensions page and reload the
active Indeed, Vivian, ZipRecruiter, LinkedIn, Facebook, NPI No., NPI Profile,
U.S. News Doctor Finder, MediFind, CommonSpirit Health, or NYSED tab.
The Facebook adapter also carries a revision handshake, so the v3.22.2 panel can
replace an older parser in an already-open profile tab before it scans.

## 4. Save displayed platform candidates

1. Open an authorized Indeed employer/Smart Sourcing, Vivian Talent Pool,
   ZipRecruiter recruiter account, LinkedIn account, Facebook account, NPI No.
   directory, NPI Profile directory, U.S. News Doctor Finder search, MediFind
   specialty search, CommonSpirit Health Find a Doctor, or NYSED
   verification search.
2. Run a candidate search and wait for its result cards to load. LinkedIn
   supports both a People search-results page and one candidate's `/in/`
   profile. On Facebook, navigate to the individual person's profile from
   People search results or a recruiting post.
3. Open **Source Profiles** in the Medhunt side panel.
4. The panel lists every detected card with name, location, and headline.
5. All captured profiles are selected by default and automatically batch-saved.
   The status reports:

   ```text
   50 saved to local database · 48 new · 2 already present
   ```

6. The extension may scroll the result list while scanning and restores the
   original scroll position when finished. Use the refresh icon to rescan.

Automatic saving stores visible profile metadata and text but does not call
PDL or Enformion. This prevents an ordinary platform search from silently using
provider credits. While the **Source Profiles** view remains open,
changes to the loaded result cards trigger a debounced rescan and database
upsert automatically.

### Platform-specific behavior

- **Indeed:** displayed-card capture, profile opening, PDL enrichment, duplicate
  detection, and automatic permitted resume capture/storage.
- **Vivian:** candidate-card capture including discipline, specialty, license,
  certifications, education, and recent experience; PDL enrichment and database
  saving. Resume auto-capture is not enabled.
- **ZipRecruiter:** candidate-card capture from the recruiter page's React card
  data, with a visible-DOM fallback; PDL enrichment and database saving. Resume
  auto-capture is not enabled.
- **LinkedIn:** a People search captures the visible, currently loaded cards in
  one batch, including normalized profile URL, name, location, headline, and
  visible current role/employer. An individual `/in/{profile}/` page also
  captures visible experience, education, skills, and certification sections.
  Individual-profile location is anchored to LinkedIn's **Contact info** row so
  pronouns, connection degree, and header text cannot be mistaken for a city.
  Professional credentials are removed from the lookup name. The normalized
  profile URL is the source identity and must be corroborated by PDL before a
  LinkedIn contact is trusted. Medhunt checks Neon/internal identities first,
  then uses the existing NPPES/PDL pipeline. It does not call LinkedIn private
  APIs, open result profiles automatically, or advance to another results page.
- **Facebook:** supports individual-profile routing for `/username`,
  `profile.php?id=...`, and `/people/.../{id}` are accepted, while Search,
  Groups, Jobs, Marketplace, Messages, and other Facebook application routes
  are rejected. Medhunt anchors the identity to the visible profile header (so
  notification titles such as `(3) Facebook` cannot become candidate names)
  and captures the visible role, bio, current city, work, and education facts.
  The header may be outside Facebook's first `main` region; explicitly labelled
  international locations such as `Lives in Paris, France` are retained.
  Facebook may leave a hidden Search-results `main` mounted behind the opened
  profile, so Medhunt binds the name and details to the visible active-profile
  region only. Profiles with an explicit visible locked-profile notice are
  skipped: they are neither imported nor sent to an enrichment provider.
  The normalized Facebook URL is sent to PDL as the supported `profile`
  identity input alongside visible facts. Rich experience, skill, contact, and
  education panels shown by other products come from their backend database or
  enrichment providers, not from Facebook's profile header alone. Open a group
  post author's individual profile before capture; bulk group-member or post
  scraping is not performed.
- **NPI No. / NPI Profile:** captures visible individual-provider directory
  rows using the 10-digit NPI as the stable source identity. Organization and
  medical-practice rows are skipped. Medhunt retains the visible specialty and
  practice location and does not advance pagination automatically.
- **U.S. News Doctor Finder:** captures rendered doctor-result cards and open
  doctor profiles from their embedded `Physician` structured data. Medhunt
  retains the exact specialty, NPI, practice location, and hospital affiliation;
  it does not use source-listed phone numbers as verified enrichment contacts or
  advance pagination automatically.
- **MediFind:** captures doctors from specialty-result pages and individual
  doctor pages using embedded `Physician` structured data. Medhunt retains the
  canonical profile identity, exact specialty, biography, practice location,
  hospital affiliations, and visible board-certification statements. Public
  appointment phone numbers are not treated as verified candidate contacts,
  and pagination is not advanced automatically.
- **CommonSpirit Health:** captures provider cards from Find a Doctor results and
  individual provider pages using the canonical numeric provider/NPI URL. Medhunt
  retains the displayed name, exact specialties, affiliated organization,
  practice address, and provider page link. Clinic scheduling numbers are not
  treated as verified personal contacts, and pagination is not advanced.
- **NYSED:** captures visible individual license-verification results using the
  profession and license number as the stable source identity. Business-entity
  results are skipped, and Medhunt does not submit or broaden searches.

### Store a LinkedIn profile PDF

1. Open the exact LinkedIn profile (from a captured search row if needed) and
   let Medhunt scan/save that profile row.
2. Select **Save profile PDF** next to that candidate in the side panel.
3. LinkedIn's **More** button is highlighted. Open it and select **Save to PDF**.
4. Medhunt watches only for that PDF for five minutes, attaches it to the saved
   candidate, adds the stored phone/email contact sheet when available, and
   uploads it to the configured private R2 bucket (or SQLite in local mode).

The LinkedIn PDF step is intentionally visible and user-controlled. If LinkedIn
does not offer **Save to PDF** for the profile, Medhunt does not bypass that
restriction.

Vivian and ZipRecruiter may display only a last initial. Medhunt now checks
previously verified internal identities first, uses public NPPES evidence for
healthcare profiles, and uses PDL Search only when those zero-credit stages do
not uniquely resolve the identity. Curately's private index and APIs are not
part of this project and are not copied into Medhunt.

## 5. Enrich selected candidates

1. Select individual profiles or use **Select all**.
2. Select **Find contact details**.
3. Confirm the lookup. Full identities use the fast bulk path. Masked identities
   may require separate NPPES/PDL discovery calls and can take longer because
   provider rate limits still apply.

### Matched-candidate batch actions

After lookup, the action bar operates on the matched candidates:

- **ATS** opens the candidate records already saved in the Medhunt database.
- **Pool** creates or updates a named talent pool without duplicate membership.
- **Campaign** creates a draft campaign and queues its candidates; it sends nothing.
- **Email** creates individual, reviewable email drafts. Recruiters must open and
  send each message through their configured email application.
- **Export** downloads the matched contact and PDL evidence fields as CSV.

Talent pools, campaigns, memberships, and outreach drafts use Neon/PostgreSQL
when `DATABASE_URL` is configured and the local SQLite database otherwise.

The extension calls the local backend. It first searches verified internal
records. Healthcare profiles can then be corroborated with public NPPES data;
NPPES never supplies personal contacts and does not prove an active license.
Unresolved masked identities use PDL Search, while broader full-name discovery
uses PDL Identify. Only a unique identity proceeds to PDL Person Enrichment. The
request requires at least one outreach-safe field: dedicated mobile phone,
recommended/personal email, or work email. Returned primary/alternate names,
current and previous locations, title, employer/school, social profiles, matched
inputs, recency, and conflicts are scored deterministically. Contacts are saved
only for a `verified` identity; close alternatives are marked `review`, and hard
name or location conflicts are `rejected`. No external provider tab is opened,
and the PDL key remains server-side.

PDL responses are cached for 30 days by default, so retrying the same identity
does not buy the same result again. There is no application-level per-run cap
when `PDL_RUN_CREDIT_LIMIT=0`; PDL account billing and limits still apply. Set a
positive value to restore a safety cap. The result summary shows the credits
spent during that run. The final view provides match/no-match filters plus CSV
export.

### How a lookup stays fast

The side panel posts the whole selection to the private `POST /contact-lookup/batch` route in groups
of 100 rather than one request per card. For each group the backend:

- reads every candidate, its cached identity result, and the run's credit spend
  in three queries instead of two per candidate;
- answers fresh verified identities and cached results without contacting the provider;
- resolves incomplete identities through internal records, NPPES, and then paid
  PDL discovery; ambiguous results stop at recruiter review;
- sends the identities that remain to the People Data Labs **bulk** person
  endpoint (`/v5/person/bulk`, up to `PDL_BULK_MAX` identities per call), so a
  full page of results costs one provider round trip rather than one per card;
- resolves the do-not-contact list once for the run; and
- writes every provider result and candidate update in a single transaction.

Two selected cards that resolve to the same person are bought once. Identities
beyond the remaining run credit limit are reported as `budget_exhausted`
without a provider call, exactly as before. If the configured account cannot
use the bulk endpoint, the backend falls back to individual person-enrich calls
for that run and reports `"bulk": false`; results and credit accounting are
unchanged.

## How contact verification works

Contact lookup and verification are different operations:

1. The active sourcing platform provides the visible candidate identity signals
   that the recruiter is authorized to view: name, location, role, employers,
   education, credentials, and skills.
2. The backend checks verified Neon evidence first, then NPPES for healthcare
   corroboration, and PDL Identify/Search only for unresolved identities.
3. A deterministic scorer requires compatible name and location evidence,
   considers title/employer/school corroboration, rejects conflicts, and sends
   near-ties to review. Vector similarity or an LLM cannot approve a person.
4. Only after identity verification does Person Enrichment request a record
   containing at least one dedicated usable contact. Only `mobile_phone` is
   saved as a phone; recommended personal, personal, and work email fields may
   be saved as email. PDL's broader historical `phone_numbers` and `emails`
   collections remain identity/audit evidence. DNC suppression is applied
   before persistence.
5. The verified record stores its canonical name, provider person ID, evidence,
   verification timestamps, contact expiry, and cross-platform master link.
6. A provider identity match is not the same as email deliverability or phone
   ownership. Set `VERIFY_EMAILS=1` with a NeverBounce key for deliverability
   results. Medhunt performs phone-format checks locally and does not use a
   third-party phone lookup service.

### Cost-aware PDL → Enformion waterfall

When both credentials are configured, PDL remains the fast bulk source and
Enformion Person Search is fallback-only. The request order is:

1. Reuses a database contact at zero provider cost only after re-evaluating its
   original PDL likelihood or Enformion confidence against today's thresholds,
   exact name/location evidence, conflicts, freshness, mobile-number evidence,
   per-provider contact provenance, and a fingerprint of the captured source
   identity. An old `accepted=true` flag alone is never trusted. A changed source
   name or location invalidates the stored contacts and verification.
2. Uses free NPPES evidence for healthcare identity corroboration. NPPES is not
   a contact source and an NPI does not prove current licensure.
3. Calls PDL in bulk. Arbitrary middle names are ignored while first name and
   terminal surname must agree. Explicit full source aliases are sent in the
   same request, so they do not add calls. Automatic acceptance requires a
   compatible name plus an exact/matched location and PDL likelihood 6+, an
   exact social profile and likelihood 6+, or stronger independent context.
   Alias matches and historical-address matches require employer/school or
   social-profile corroboration. A same-state/different-city result requires a
   provider-confirmed source employer/school match; state alone never verifies
   identity. Weak contacts are withheld rather than displayed or saved.
4. Skips Enformion when accepted PDL already has both phone and email.
5. Makes a fresh licensed Enformion Person Search call only after the recruiter
   confirms the combined lookup-cost prompt and only when PDL has no accepted
   contact, falls below the gate, or is missing a contact type. The request also
   sends an explicit Enformion call maximum. The server enforces the lower of
   that maximum and `ENFORMION_RUN_CREDIT_LIMIT` (default `100`) across the run.
   Set the server limit to `0` to disable fresh Enformion calls. The fallback
   requires a US city/state or US ZIP because Enformion is US-focused; an
   international Facebook location does not consume a paid fallback call. It
   uses full name plus exact captured city/state; uncertain PDL contacts are
   never fed back as matching inputs.
6. Reviews up to 10 Person Search results and ranks identity evidence without
   using contact volume as a score. It compares first + terminal surname,
   documented aliases, current/recent/previous addresses, employer, school,
   role, DOB/age when supplied, and an independently captured relative when
   available. A provider's own relative list can corroborate a source-provided
   relationship but can never replace the primary person's name. A unique
   winner must clear the identity threshold and an 8-point margin; close people
   remain ambiguous. Mobile/wireless is always preferred. If no usable mobile
   remains, the result may expose an associated alternate number as **Other
   phone**. Enformion alternatives must be connected, not explicitly stale,
   and cannot be fax, pager, disconnected, inactive, or invalid. PDL associated
   numbers do not carry a connected guarantee and are never labeled as mobile.
7. Caches normalized Enformion results for 90 days by default. Cached fallbacks
   are evaluated before the consent and budget gates, so they remain usable at
   zero provider cost even after the fresh-call cap is reached. Provider values
   explicitly marked non-current are discarded, and DNC suppression still runs.

The extension never renders raw contacts merely because they exist in the
database. Every selected profile goes through this backend trust gate first.
Fresh accepted enrichment replaces the active contact set; older values remain
only in verification evidence/history and cannot be silently merged into the
current result.

Set `ENFORMION_VERIFY_PDL=0` to disable this fallback step. Keep
`ENFORMION_FALLBACK_ONLY=1`; universal second-provider calls are intentionally
disabled to control cost. Every internal provider-batch request defaults
`allow_enformion` to `false`; callers must opt in explicitly. The response
summary reports fallback `eligible`, `called`, `cached`, and `skipped_budget`
counts. Legacy local
credential names `Profile_name` and `Password` are recognized, but new installs
should use `ENFORMION_AP_NAME` and `ENFORMION_AP_PASSWORD`.

An accepted provider result corroborates an identity/contact association; it
does not prove current phone ownership or email deliverability. The UI labels
these values as provider-associated and not candidate-confirmed. Enformion contact data
is used for recruiting contact discovery only, never as an employment
eligibility or hiring-decision signal.

Gemini remains optional for writing assistance. It is not part of the identity
approval decision.

For a more complete profile, select **Open**, then use **Capture Indeed profile**
after the full Insights/resume view appears.

## Candidate contact lookup (Quick Sourcer)

Quick Sourcer answers the panel's candidate lookups. **Find contact details**
on a selection, and **Enrich** on a single candidate, both call the Quick
Sourcer external API on the Hub and store exactly what it returns — every
phone number it reports regardless of line type, every email address, and the
addresses on record. There is no identity threshold, no provider likelihood
floor, and no trust gate in front of that data: the recruiter sees the
external record itself. The one filter that still applies is the
do-not-contact list, which is a compliance obligation rather than a confidence
judgement.

`CONTACT_LOOKUP_PROVIDER` selects this. Set it to `people_data_labs` to
restore the PDL → Enformion waterfall and its verification policy, which
remains in the codebase and under test.

Quick Sourcer records carry `contact_source: "quick_sourcer"`. They are not
eligible for private downstream synchronization by default. An administrator
who has independently approved the configured feed can set
`QUICK_SOURCER_TRUSTED_FOR_SYNC=1`; DNC suppression still applies, and this
private policy flag is not exposed in the extension.

Any candidate reachable from the panel — a row in the scanned results, the
capture review sheet, or a saved candidate card — also carries a **Public
records** action that opens the full record on demand: phone numbers with line
type, carrier, and when each was last reported, email addresses, current and
past addresses with county and property detail, work history, aliases,
relatives, associates, and businesses.

The panel never holds the API key. `QUICK_SOURCER_API_KEY` is read by the
backend only, and the extension calls `POST /quick-sourcer/find` on
`127.0.0.1`. When no key is configured, the backend reports the feature as
disabled on `GET /health` and the action is not rendered.

A first search for one person opens the source site in a real browser on the
Hub, so it takes **30 to 90 seconds**, and the searches cannot be run in
parallel. Budget roughly a minute per selected candidate for an uncached
batch. The found record is then cached locally under
`QUICK_SOURCER_CACHE_TTL_SECONDS` (30 days by default) and reopens instantly;
**Search again** forces a fresh run. A miss is never cached, because a source
site that is busy or blocking automated searches can produce one, and a
transient `5xx` from the Hub is reported as a retryable failure rather than a
no-match.

The record shown by the **Public records** action is not written to the
candidate; only a lookup run through the flow above stores contacts.

## Matched resume capture

For Indeed only, after the provider waterfall produces at least one accepted
phone number or email address, the
extension opens that exact candidate and trusted-clicks the normal
**Download resume** action. A MAIN-world hook captures resume bytes produced by
Blob, fetch, or XHR, with the browser download URL as a fallback. This avoids
depending only on Chrome's download-complete event. The PDF is uploaded to the
private R2 bucket and its metadata is saved in Neon. If Indeed did not create a
visible file itself, the extension explicitly saves the captured PDF under
`Downloads/MedhuntResumes`. Profiles with only a phone or only an email are
also eligible for the same capture and Nexus delivery path.

Each enriched resume shows one latest trusted phone. Medhunt
keeps its existing contact policy: an accepted mobile/wireless number is
preferred, with a callable alternative used only when no accepted mobile is
available. Provider reporting dates order numbers inside that approved tier;
expired, untrusted, disconnected, and do-not-contact values are excluded. The
stored PDF remains an immutable capture snapshot. When Medhunt serves a resume
or sends it to Nexus, it regenerates the Medhunt contact page from the current
trusted record. A newer accepted phone therefore replaces the prior one, and a
stale contact page is removed if those contacts later expire or are suppressed.

## Standalone Indeed watcher

`src_pkg/watcher_frontend` is a separate extension for one continuously monitored
Indeed Smart Sourcing search. The recruiter prepares the search and location on
Indeed, selects **Most recent**, clicks **Find**, and then starts the watcher from
that exact results page. The watcher uses the authorized browser session, reapplies
**Most recent** on every later cycle, and
captures up to 100 currently rendered recent profiles. The first cycle processes
the visible baseline. Later cycles compare the stable Indeed source id, bounded
profile facts, and a normalized resume-update estimate; only new, changed, or
previously failed items are imported and sent through contact lookup.

The watcher uses a persistent `chrome.alarms` schedule and a single-flight lock,
so closing its side panel does not stop monitoring and overlapping triggers cannot
duplicate a run. It reuses the existing batch import, backend-owned enrichment,
resume capture, Cloudflare/Neon persistence, and durable Nexus delivery paths.
The browser and an authorized Indeed session must remain available. The watcher
does not bypass authentication or platform security challenges.

After the initial baseline completes, the watcher can email an administrator-
configured recruiter list when later scans detect new or updated resume cards.
SendGrid credentials and recipients remain in the local backend; they are never
embedded in or returned to the browser extension. Delivery records are stored
per event and recipient, so a failed recipient can be retried without resending
to recipients that already received the alert. Configure:

```dotenv
WATCHER_EMAIL_NOTIFICATIONS_ENABLED=1
WATCHER_NOTIFICATION_EMAILS=recruiter@example.com,team@example.com
SENDGRID_API_KEY=...
EMAIL_FROM=verified-sender@example.com
EMAIL_FROM_NAME=Medhunt
```

An existing Halo configuration can be imported without printing credentials:

```powershell
python .\packaging\import_sendgrid_config.py `
  --source C:\path\to\Halo\.env --destination .\.env.local
```

Build the unpacked extension and its per-user setup package with:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_watcher.ps1
```

`Medhunt-Watcher-Setup.exe` reads the existing Medhunt installation's local API
token and injects it into the installed watcher copy. It does not contain provider,
Cloudflare, database, or Nexus credentials and requires the normal Medhunt backend
to be installed first.

### Scanned and image-only resumes

Medhunt reads the PDF text layer locally. Pages without enough readable text
are rendered and passed through the Tesseract OCR runtime bundled in the team
installer. The private structured record can contain name, city/state/country,
role, email/phone evidence, skills, credentials, licenses, education, work
history, specialty, work authorization, availability, and a short summary.
The full OCR transcript is not stored.

The recruiting-platform profile remains authoritative. OCR never overwrites a
conflicting platform name or location, and phone/email found in a resume never
bypasses the verified-contact gate. Only high-confidence fields approved as
missing-data fills may be included in a Nexus profile. OCR work has a bounded
page count and time budget; a failure or unusually large scan does not prevent
the original resume from being stored.

## Optional durable Nexus delivery

Medhunt can automatically queue a newly saved enriched resume and its approved
candidate data for LaborEdge Nexus. This is a backend-only integration: the
extension receives neither Nexus credentials nor provider responses. Delivery
is disabled by default and is queued when the candidate has at least one
current trusted email or phone. The resume row and durable delivery record are
committed together, so restarting the service does not lose pending work.
PDFs larger than the configured Nexus upload limit are saved locally but are
reported as `skipped_resume_too_large` instead of entering a doomed queue.
If a resume was stored before trusted contacts became available, the newest
stored resume is queued automatically as soon as a later lookup supplies one
approved channel.

Set these backend variables for the authentication method supplied by Nexus:

```text
NEXUS_SYNC_ENABLED=1
NEXUS_BASE_URL=https://api-nexus.laboredge.com
NEXUS_AUTH_METHOD=password
NEXUS_TOKEN_URL=<tenant OAuth token URL>
NEXUS_USERNAME=<backend API user>
NEXUS_PASSWORD=<backend API password>
NEXUS_ORG_CODE=<organization code, when required>
NEXUS_DEFAULT_PROFILE={"referralSourceName":"MedHunt","jobTypeIds":["TRAVEL"]}
```

For `client_credentials`, provide `NEXUS_CLIENT_ID` and
`NEXUS_CLIENT_SECRET`. For `static`, provide `NEXUS_STATIC_TOKEN`.
For a password grant whose OAuth server requires a client Authorization header,
provide either `NEXUS_CLIENT_ID` plus `NEXUS_CLIENT_SECRET` or
`NEXUS_TOKEN_BASIC`. These values stay in backend configuration and are never
included in the browser extension. A trusted-team installer can also resolve
the allowlisted settings from `NEXUS_REFERENCE_ENV` and
`NEXUS_REFERENCE_CONFIG` while it is built.

For a hosted deployment, add the production Chrome extension origin after the
Chrome Web Store assigns its ID:

Healthcareboard-authenticated accounts use registered extension installations.
The first device is approved automatically; every later installation remains
pending until a Healthcareboard administrator approves it. Users can review and
revoke their devices but cannot approve devices themselves. The account screen
lists device names, last-use times, approval state, and revoke controls. Two approved devices are allowed by
default, controlled by `MEDHUNT_MAX_REGISTERED_DEVICES` (1-5). Registrations
expire after 90 inactive days by default, controlled by
`MEDHUNT_DEVICE_IDLE_DAYS` (7-365), and can be restored by a fresh email-code
sign-in. Revoked installations do not reactivate automatically. Approval,
revocation, expiry, and registration events are retained in
`extension_device_events`. Older extension builds that do not send an
installation identifier must be updated before this backend is deployed.

### Extension enrichment usage

Medhunt does not deduct or enforce Halo enrichment credits for candidate lookup.
Third-party provider billing, API limits, and configured lookup controls still
apply.

```text
MEDHUNT_EXTENSION_ORIGINS=chrome-extension://<32-character-extension-id>
MEDHUNT_ALLOW_UNLISTED_EXTENSION_ORIGINS=0
```

Render deployments fail closed for browser-extension origins when that value
is absent. Upload the extension as a draft, copy its assigned ID, configure the
origin in Render, redeploy the backend, and then publish the extension.
`NEXUS_RESUME_DOC_TYPE_ID`, state/name mappings, and the timeout/worker settings
in `.env.example` are optional tenant overrides.
`NEXUS_DEFAULT_PROFILE` must provide valid tenant routing, including at least
one `jobTypeIds` value and a referral source. It may provide a `jobId` or exact
profession/specialty fallback IDs. When the captured platform profile declares
a specialty, Medhunt first resolves that label from the live Nexus specialty
catalog and then tries a small reviewed set of equivalent Nexus labels; the
catalog's related profession ID keeps the two fields consistent. If no safe
mapping exists, Medhunt sends the tenant's explicit Unknown profession and
Unknown specialty pair. Profiles without a declared specialty can still use
the configured fallback, or the tenant's Unknown/Other/General master data.

For contacts, Nexus receives whichever trusted, non-DNC identifier is available:
email, phone, or both. The email is the first trusted address in provider order.
Phone selection prefers mobile/wireless over other callable lines, then
current/recent evidence, connectivity, corroborating-source count, and finally
stable provider order. Candidate creation and duplicate matching work with one
identifier; a second identifier is included when available.

Legacy source labels are normalized using the bundled
`sourcing/data/legacy_taxonomy.csv` catalogue before live Nexus master-data
resolution. This preserves the old profession/specialty vocabulary while
still sending only the current Nexus IDs. Set
`NEXUS_LEGACY_TAXONOMY_PATH` only when a deployment intentionally uses a
different, reviewed taxonomy file.

Before creation, Medhunt searches Nexus independently by trusted email and
phone. A unique compatible record is reused; multiple candidates, conflicting
email/phone owners, name conflicts, or ambiguous master-data mappings are held
for review. Read-only lookup failures and HTTP 429 responses are retried from
the durable queue with bounded backoff. Permanent validation failures stop,
while a network/server failure during create or upload is indeterminate and is
held for reconciliation instead of being blindly repeated and risking a
duplicate candidate. The
worker persists a `writing` boundary immediately before a Nexus mutation; if
the service stops in that window, the expired delivery becomes
`indeterminate` and requires reconciliation rather than automatic replay.

SQLite is not encrypted by this project. Protect the workstation and database,
apply an appropriate retention period, and delete candidate data when it is no
longer needed.

## Test

```powershell
python -m pytest -q
```

The tests cover SQLite fallback, batch import, deduplication, enrichment,
identity evidence, suppression, ranking, outreach, API behavior, extension
CORS, and Manifest V3 packaging. A browser smoke test and its 50-candidate
fixture are available in `tests/browser_smoke.py` and `tests/fixtures`. Run
`python tests/platform_adapter_smoke.py` for an offline browser check of the
Vivian, ZipRecruiter, LinkedIn, and Facebook adapters; it spends no provider credits.
Run `python tests/healthcare_directory_adapter_smoke.py` for the equivalent
NPI No., NPI Profile, NYSED, U.S. News Doctor Finder, and MediFind extraction check.
`python tests/quick_sourcer_panel_smoke.py` covers the public-records action and
its result sheet against a mocked backend, so it contacts no external API.

## Project layout

```text
sourcing/               Neon/PostgreSQL and SQLite backend modules
api.py                  FastAPI routes and extension CORS
frontend/
  manifest.json         Manifest V3 extension definition
  background.js         Opens the side panel
  indeed-content.js     Reads Indeed results, profiles, and resume downloads
  platform-main.js      Reads permitted ZipRecruiter React card data in page context
  platform-content.js   Adapters for Vivian and ZipRecruiter candidate cards
  linkedin-content.js   LinkedIn People results, profile, and guided PDF adapter
  facebook-content.js   Individual Facebook profile adapter
  index.html            Extension/web shell
  app.js                Side-panel client and automatic database sync
  styles.css            Desktop and side-panel layouts
tests/                  Demo-mode backend and extension checks
```

## API

`POST /jobs` · `POST /candidates/intake` · `POST /candidates/import` ·
`POST /candidates/import/batch` · `GET /candidates` ·
`POST /candidates/{id}/enrich` · `POST /candidates/{id}/pdl-enrich` ·
`POST /contact-lookup/batch` · `POST /pdl-enrich/batch` · `POST /candidates/{id}/identity/resolve` ·
`POST /candidates/{id}/identity/review` · `POST /enrich/batch` ·
`POST /jobs/{id}/rank` · `POST /outreach/draft` ·
`POST /outreach/{id}/approve` · `PATCH /candidates/{id}/stage` ·
`POST /dnc` · `GET /stats` · `GET /health` ·
`GET /quick-sourcer/status` · `POST /quick-sourcer/find` ·
`GET /quick-sourcer/candidates/{external_id}` ·
`GET /candidates/{id}/sms-preview` · `POST /messaging/sms` ·
`POST /integrations/twilio/webhook`

## Candidate SMS

Set `SMS_PROVIDER=twilio`, `TWILIO_SMS_ENABLED=1`, the Twilio account
credentials, `TWILIO_PHONE_NUMBER`, and the exact public `TWILIO_WEBHOOK_URL`.
Configure that URL as the Twilio number's incoming-message webhook using POST.
Set `SENDGRID_API_KEY` and `EMAIL_FROM`. `SMS_REPLY_NOTIFICATION_EMAILS`
defaults to the Radixsol fallback recipients and can be overridden with a
comma-separated list. Replies without a stored conversation are sent to these
fallback recipients; mapped replies also reach the initiating recruiter.

The extension builds the initial message from the candidate's captured first
name, specialty, title, city, and state. The backend permits one initial SMS
per candidate or phone number, honors the do-not-contact list, and records STOP
replies before sending the recruiter notification email.

Quick Sourcer uses the shared endpoint pool by default. Set
`QUICK_SOURCER_DEDICATED_IP=1` after marking at least one Search Endpoint as
Dedicated in Hub Settings. Medhunt then adds `dedicated_ip: true` to each new
`/external/find` request and keeps its dedicated results in a separate cache
namespace from shared-pool results.

## Compliance defaults

- Use platform integrations only with candidate data you are authorized to
  access and retain.
- Candidate SMS is sent through the configured Twilio account only after the
  recruiter reviews the generated message and selects Send.
- Email outreach remains a human-reviewed draft workflow.
- Nothing is sent automatically.
- Do-not-contact entries are enforced during enrichment and outreach.
- Honor opt-outs and applicable retention/deletion requirements.
