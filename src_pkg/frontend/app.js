"use strict";

const $ = (selector, element = document) => element.querySelector(selector);
const IS_EXTENSION = ["chrome-extension:", "moz-extension:"].includes(location.protocol);
const DEFAULT_BACKEND = "https://medhunt1.onrender.com";
const HOSTED_AUTH_REQUIRED = IS_EXTENSION && DEFAULT_BACKEND.startsWith("https://");
const LOCAL_API_TOKEN = "__MEDHUNT_LOCAL_API_TOKEN__";
const BACKEND_STORAGE_KEY = "medhuntBenchmarkABackendUrl";
const AUTH_STORAGE_KEY = "medhuntHealthBoardSession";
const DEVICE_STORAGE_KEY = "medhuntExtensionInstallationId";
const PRIVACY_CONSENT_KEY = "medhuntProfileDataConsentV1";
const STAGES = ["new", "enriched", "contacted", "replied", "submitted", "rejected"];
const CONTACT_BATCH_SIZE = 100;
// A public-records lookup drives a real browser per person and cannot be
// parallelised, so a bulk request would sit unanswered well past any sane
// timeout. Send one candidate per request instead and let each row resolve as
// its own answer arrives.
const RECORD_LOOKUP_BATCH_SIZE = 1;
const CONTACT_BATCH_TIMEOUT = 180000;
const RECORD_LOOKUP_TIMEOUT = 190000;
const SOURCING_PLATFORMS = {
  indeed: {
    key: "indeed",
    label: "Indeed",
    host: (hostname) => hostname === "indeed.com" || hostname.endsWith(".indeed.com"),
    contentScript: "indeed-content.js",
    mainScript: "inject.js",
    adapterRevision: "indeed-capture-v6",
    adapterRequestType: "RADIXSOL_INDEED_V6_REQUEST",
    resumeCapture: true,
  },
  vivian: {
    key: "vivian",
    label: "Vivian",
    host: (hostname) => hostname === "vivian.com" || hostname.endsWith(".vivian.com"),
    contentScript: "platform-content.js",
    adapterRevision: "platform-capture-v3",
    adapterRequestType: "RADIXSOL_PLATFORM_V3_REQUEST",
    resumeCapture: false,
  },
  ziprecruiter: {
    key: "ziprecruiter",
    label: "ZipRecruiter",
    host: (hostname) => hostname === "ziprecruiter.com" || hostname.endsWith(".ziprecruiter.com"),
    contentScript: "platform-content.js",
    mainScript: "platform-main.js",
    adapterRevision: "platform-capture-v3",
    adapterRequestType: "RADIXSOL_PLATFORM_V3_REQUEST",
    resumeCapture: false,
  },
  linkedin: {
    key: "linkedin",
    label: "LinkedIn",
    host: (hostname) => hostname === "linkedin.com" || hostname.endsWith(".linkedin.com"),
    contentScript: "linkedin-content.js",
    adapterRevision: "linkedin-capture-v5",
    adapterRequestType: "RADIXSOL_LINKEDIN_V5_REQUEST",
    resumeCapture: false,
    guidedPdfCapture: true,
    automaticPdfCapture: true,
    capturePrompt: "People results page or individual /in/ profile",
  },
  facebook: {
    key: "facebook",
    label: "Facebook",
    host: (hostname) => hostname === "facebook.com" || hostname.endsWith(".facebook.com"),
    contentScript: "facebook-content.js",
    adapterRevision: "facebook-profile-v8",
    adapterRequestType: "RADIXSOL_FACEBOOK_V8_REQUEST",
    resumeCapture: false,
    guidedPdfCapture: false,
    singleProfile: true,
  },
  npino: {
    key: "npino",
    label: "NPI No.",
    host: (hostname) => hostname === "npino.com" || hostname.endsWith(".npino.com"),
    contentScript: "healthcare-directory-content.js",
    adapterRevision: "healthcare-directory-v13",
    adapterRequestType: "RADIXSOL_HEALTHCARE_DIRECTORY_V13_REQUEST",
    resumeCapture: false,
  },
  nysed: {
    key: "nysed",
    label: "NYSED",
    host: (hostname) => hostname === "eservices.nysed.gov",
    contentScript: "healthcare-directory-content.js",
    adapterRevision: "healthcare-directory-v13",
    adapterRequestType: "RADIXSOL_HEALTHCARE_DIRECTORY_V13_REQUEST",
    resumeCapture: false,
  },
  npiprofile: {
    key: "npiprofile",
    label: "NPI Profile",
    host: (hostname) => hostname === "npiprofile.com" || hostname.endsWith(".npiprofile.com"),
    contentScript: "healthcare-directory-content.js",
    adapterRevision: "healthcare-directory-v13",
    adapterRequestType: "RADIXSOL_HEALTHCARE_DIRECTORY_V13_REQUEST",
    resumeCapture: false,
  },
  usnews: {
    key: "usnews",
    label: "U.S. News Doctor Finder",
    host: (hostname, url) => hostname === "health.usnews.com" &&
      /^\/(?:doctors|nurse-practitioners)(?:\/|$)/i.test(url?.pathname || ""),
    contentScript: "healthcare-directory-content.js",
    adapterRevision: "healthcare-directory-v13",
    adapterRequestType: "RADIXSOL_HEALTHCARE_DIRECTORY_V13_REQUEST",
    resumeCapture: false,
  },
  medifind: {
    key: "medifind",
    label: "MediFind",
    host: (hostname, url) => (hostname === "medifind.com" || hostname.endsWith(".medifind.com"))
      && /^\/(?:doctors|specialty)(?:\/|$)/i.test(url?.pathname || ""),
    contentScript: "healthcare-directory-content.js",
    adapterRevision: "healthcare-directory-v13",
    adapterRequestType: "RADIXSOL_HEALTHCARE_DIRECTORY_V13_REQUEST",
    resumeCapture: false,
  },
  commonspirit: {
    key: "commonspirit",
    label: "CommonSpirit Health",
    host: (hostname, url) => (hostname === "commonspirit.org" || hostname.endsWith(".commonspirit.org"))
      && /^\/(?:search|find-a-(?:doctor|location))(?:\/|$)/i.test(url?.pathname || ""),
    contentScript: "healthcare-directory-content.js",
    adapterRevision: "healthcare-directory-v13",
    adapterRequestType: "RADIXSOL_HEALTHCARE_DIRECTORY_V13_REQUEST",
    resumeCapture: false,
  },
  sharecare: {
    key: "sharecare",
    label: "Sharecare",
    host: (hostname, url) => hostname === "providers.sharecare.com"
      && /^\/(?:find-a-doctor|doctor)(?:\/|$)/i.test(url?.pathname || ""),
    contentScript: "healthcare-directory-content.js",
    adapterRevision: "healthcare-directory-v13",
    adapterRequestType: "RADIXSOL_HEALTHCARE_DIRECTORY_V13_REQUEST",
    resumeCapture: false,
  },
  webmd: {
    key: "webmd",
    label: "WebMD",
    host: (hostname, url) => hostname === "doctor.webmd.com"
      && (/^\/results(?:\/|$)/i.test(url?.pathname || "")
        || /^\/providers\/specialty(?:\/|$)/i.test(url?.pathname || "")
        || /^\/doctor\/[^/]+-overview\/?$/i.test(url?.pathname || "")),
    contentScript: "healthcare-directory-content.js",
    adapterRevision: "healthcare-directory-v13",
    adapterRequestType: "RADIXSOL_HEALTHCARE_DIRECTORY_V13_REQUEST",
    resumeCapture: false,
  },
};
const PROFESSIONAL_PROFILE_SOURCES = new Set(["usnews", "medifind", "commonspirit", "sharecare", "webmd"]);

let apiBase = IS_EXTENSION ? DEFAULT_BACKEND : "";
let backendHealth = null;
let authConfig = { enabled: false, provider: "healthboard" };
let authSession = null;
let extensionCredits = { enabled: false, balance: null, status: "loading" };
let extensionDeviceId = "";
let privacyConsent = false;
let extensionWorkspaceStarted = false;
let extensionWorkspaceStarting = false;
let pendingLogin = null;
let jobs = [];
let activeJobId = null;
let activeView = IS_EXTENSION ? "indeed" : "candidates";
let activeDraft = null;
let activeSmsContext = null;
let activeIndeedProfile = null;
let publicRecordReturnProfile = null;
let activePublicRecordResult = null;
let activeSourcingPlatform = SOURCING_PLATFORMS.indeed;
let activeSourcingPageUrl = "";
let activeSourcingTabId = 0;
let activeSourcingWindowId = 0;
let activeSourcingContextKey = "";
let activePageIndicatorLabel = "Candidate page";
let indeedCandidates = [];
let indeedSelected = new Set();
let indeedSaveStatus = null;
const indeedSavePromises = new Map();
const professionalProfileResumePromises = new Map();
let indeedScanState = { phase: "idle", found: 0, total: 0 };
let indeedLookupState = new Map();
let indeedLookupSummary = null;
let indeedResultFilter = "all";
let indeedLookupScope = new Set();
let indeedLookupProfiles = [];
let indeedAutoScanTimer = null;
let indeedProgressRefreshTimer = null;
let indeedLookupInProgress = false;
let indeedResumeBatchState = {
  active: false,
  total: 0,
  processed: 0,
  saved: 0,
  failed: 0,
  sourceTabId: 0,
  sourceWindowId: 0,
  sourceContextKey: "",
  sourcePageUrl: "",
};
let indeedResumeNavigationGrace = { sourceTabId: 0, sourcePageUrl: "", until: 0 };
let indeedScanGeneration = 0;
let sourcingContextTimer = null;
let pendingSourcingContext = null;
let skippedProfileCount = 0;
let toastTimer = null;
const resumeDownloadWaiters = new Map();
const linkedinPdfCaptureTimers = new Map();
const processingResumeEvents = new Set();

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#039;",
  })[character]);
}

function initials(name) {
  return String(name || "?")
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0] || "")
    .join("")
    .toUpperCase();
}

function notify(message, type = "") {
  const toast = $("#toast");
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast${type ? ` ${type}` : ""}`;
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 4200);
}

function setBusy(button, busy) {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = "Working…";
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
    delete button.dataset.originalText;
  }
}

function friendlyActionError(error) {
  const message = String(error?.message || "");
  if (Number(error?.status) === 402) return message || "You are out of extension credits. Ask your organization admin or manager for more.";
  if (/device|installation|approval/i.test(message) && /registered|approved|revoked|expired|waiting/i.test(message)) return message;
  if (
    Number(error?.status) === 401 ||
    /invalid local api token|unauthorized|authentication required/i.test(message)
  ) {
    if (authConfig.enabled) return "Please sign in to Medhunt before continuing.";
    return "This extension copy is out of date. Reload Medhunt from the installed extension folder.";
  }
  if (/timed out/i.test(message)) return "The service took too long. Please try again.";
  if (/\b(502|503|504)\b|temporarily (busy|unavailable)/i.test(message)) {
    return "Contact lookup is temporarily busy. Retry this selection in a moment.";
  }
  if (/failed to fetch|network|connection|backend|service unavailable/i.test(message)) {
    return "The local service is unavailable. Start Medhunt and try again.";
  }
  if (/profiles could not be saved/i.test(message)) return message;
  if (/supported candidate page|open .* page|candidate page/i.test(message)) return message;
  if (/select at least one/i.test(message)) return message;
  return IS_EXTENSION
    ? "This action could not be completed. Please try again."
    : (message || "Something went wrong.");
}

async function withBusy(button, work) {
  setBusy(button, true);
  try {
    await work();
  } catch (error) {
    notify(friendlyActionError(error), "error");
  } finally {
    setBusy(button, false);
  }
}

async function api(path, options = {}) {
  const { timeout = 30000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    await ensureExtensionDeviceId();
    const headers = authenticatedApiHeaders(fetchOptions.headers);
    const response = await fetch(`${apiBase}${path}`, {
      ...fetchOptions,
      headers,
      signal: controller.signal,
    });
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json")
      ? await response.json()
      : await response.text();

    if (!response.ok) {
      const detail = payload && typeof payload === "object" ? payload.detail : payload;
      const requestError = new Error(detail || `Backend returned ${response.status}.`);
      requestError.status = response.status;
      throw requestError;
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The backend request timed out.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function setConnection(health = null) {
  const connection = $("#connection");
  const mode = $("#mode");

  mode.className = "pill";
  if (IS_EXTENSION) {
    connection.textContent = health ? "Ready" : "Unavailable";
    connection.className = `connection ${health ? "online" : "offline"}`;
    mode.textContent = health ? "ready" : "offline";
    mode.classList.add(health ? "live" : "offline");
    return;
  }
  if (!health) {
    connection.textContent = `Backend unavailable at ${apiBase || location.origin}`;
    connection.className = "connection offline";
    mode.textContent = "offline";
    return;
  }

  connection.textContent = `Connected to ${apiBase || location.origin}`;
  connection.className = "connection online";
  mode.textContent = health.mode || "online";
  mode.classList.add(health.mode === "demo" ? "demo" : "live");
}

async function refreshHealth(showSuccess = false) {
  try {
    const health = await api("/health", { timeout: HOSTED_AUTH_REQUIRED ? 30000 : 4000 });
    if (IS_EXTENSION) {
      if (authConfig.enabled && authSession?.extension_token) {
        await api("/auth/me", { timeout: 6000 });
      } else if (!authConfig.enabled) {
        await api("/session", { timeout: 6000 });
      }
    }
    backendHealth = health;
    setConnection(health);
    updateSourceHeaderServiceStatus();
    if (showSuccess) notify(IS_EXTENSION ? "Ready." : `Backend connected in ${health.mode} mode.`);
    return health;
  } catch (error) {
    backendHealth = null;
    setConnection();
    updateSourceHeaderServiceStatus();
    if (showSuccess) notify(IS_EXTENSION ? "Service unavailable." : (error.message || "Backend is unavailable."), "error");
    return null;
  }
}

function normalizeBackendUrl(raw) {
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("Enter a valid backend URL.");
  }
  const local = parsed.protocol === "http:" && ["127.0.0.1", "localhost"].includes(parsed.hostname);
  if ((!local && parsed.protocol !== "https:") || parsed.username || parsed.password) {
    throw new Error("Use HTTP on localhost for development or HTTPS for a hosted backend.");
  }
  return parsed.origin;
}

function readExtensionSetting(key) {
  return new Promise((resolve) => {
    chrome.storage.local.get([key], (result) => resolve(result[key]));
  });
}

function writeExtensionSetting(key, value) {
  return new Promise((resolve) => {
    chrome.storage.local.set({ [key]: value }, resolve);
  });
}

async function loadBackendConfig() {
  if (!IS_EXTENSION) return;
  // Chrome keeps extension storage across upgrades. Public packages have an
  // HTTPS backend baked in, so an address saved by an older development build
  // must never override the deployed service after upgrading.
  if (DEFAULT_BACKEND.startsWith("https://")) {
    apiBase = normalizeBackendUrl(DEFAULT_BACKEND);
    await writeExtensionSetting(BACKEND_STORAGE_KEY, apiBase);
    return;
  }
  const saved = await readExtensionSetting(BACKEND_STORAGE_KEY);
  if (saved) {
    try {
      apiBase = normalizeBackendUrl(saved);
    } catch {
      apiBase = DEFAULT_BACKEND;
    }
  }
}

async function loadJobs() {
  jobs = await api("/jobs");
  if (activeJobId && !jobs.some((job) => Number(job.id) === Number(activeJobId))) {
    activeJobId = null;
  }
  if (!activeJobId && jobs.length) activeJobId = Number(jobs[0].id);
}

function backendError(error) {
  if (IS_EXTENSION) {
    return `
      <div class="notice error"><strong>Service unavailable</strong></div>
      <div class="card">
        <button type="button" class="btn" data-action="retry">Retry</button>
      </div>`;
  }
  return `
    <div class="notice error">
      <strong>Backend connection failed.</strong><br>
      ${escapeHtml(error.message || "Start the local service, then try again.")}
    </div>
    <div class="card">
      <h3>Connect the extension</h3>
      <p class="muted small">
        Start <strong>Medhunt Service</strong> from the Windows Start menu,
        then select <strong>Retry</strong>. Developers can also run the local
        Python service from the project folder.
      </p>
      <div class="row mt">
        <button type="button" class="btn teal" data-action="retry">Retry</button>
        <button type="button" class="btn ghost" data-action="navigate" data-view="settings">Settings</button>
      </div>
    </div>`;
}

function kpi(label, value) {
  return `<div class="kpi"><div class="label">${escapeHtml(label)}</div><div class="value">${Number(value) || 0}</div></div>`;
}

function stageClass(stage) {
  return STAGES.includes(stage) ? `stage stage-${stage}` : "stage stage-new";
}

const ROW_CONTACT_LIMIT = 3;

function moreContactsNote(shown, total) {
  const hidden = Math.max(0, total - shown);
  return hidden
    ? `<span class="lookup-detail">+${hidden} more — open Public records</span>`
    : "";
}

function publicPhoneContacts(record) {
  const phones = Array.isArray(record?.phones) ? record.phones : [];
  const typed = Array.isArray(record?.phone_contacts)
    ? record.phone_contacts.filter((item) => item && typeof item.value === "string")
    : [];
  if (typed.length) return typed;
  return phones.map((value) => ({ value, kind: "phone" }));
}

function publicPhoneLabel(kind) {
  if (kind === "mobile") return "Mobile";
  if (kind === "other") return "Other phone";
  return "Phone";
}

function candidateCard(candidate) {
  const email = Array.isArray(candidate.emails) ? candidate.emails[0] : "";
  const phoneContact = publicPhoneContacts(candidate)[0] || null;
  const phone = phoneContact?.value || "";
  const phoneLabel = publicPhoneLabel(phoneContact?.kind);
  const address = Array.isArray(candidate.addresses) ? candidate.addresses[0] : "";
  // Candidate responses are already projected by the server; the browser does
  // not receive or re-evaluate internal trust evidence.
  const successful = Boolean(email || phone);
  const stage = STAGES.includes(candidate.stage) ? candidate.stage : "new";
  const publicRecord = candidate.records_available
    ? `<span class="contact-origin">Public record</span>`
    : "";
  const contact = successful
    ? `<div class="contact">
        ${publicRecord}
        <div class="contact-line"><span class="contact-key">✉</span><span>${escapeHtml(email || "No usable email")}</span></div>
        <div class="contact-line"><span class="contact-key">☎</span><span>${escapeHtml(phone ? `${phoneLabel}: ${phone}` : "No usable phone")}</span></div>
        <div class="contact-line"><span class="contact-key">⌂</span><span class="muted">${escapeHtml(address || candidate.location || "")}</span></div>
        ${moreContactsNote(2, (candidate.emails?.length || 0) + publicPhoneContacts(candidate).length)}
      </div>`
    : `<div class="contact"><span class="muted">No verified contact saved.</span></div>`;

  return `<article class="candidate-card">
    <div class="candidate-head">
      <div class="candidate-avatar">${escapeHtml(initials(candidate.name))}</div>
      <div>
        <div class="candidate-name">${escapeHtml(candidate.name)}</div>
        <div class="candidate-location">${escapeHtml(candidate.location || "")}</div>
      </div>
      <div class="fit"><div class="number">${Number(candidate.fit_score) || 0}</div><div class="label">FIT</div></div>
    </div>
    <div><span class="${stageClass(stage)}">${escapeHtml(stage)}</span></div>
    ${contact}
    <div class="candidate-actions">
      <button type="button" class="btn teal sm" data-action="enrich" data-id="${Number(candidate.id)}">Enrich</button>
      ${publicRecordButton(candidate.name, candidate.location, candidate.id)}
      <button type="button" class="btn sm" data-action="draft" data-id="${Number(candidate.id)}">Draft outreach</button>
      ${phoneContact?.kind === "mobile" ? `<button type="button" class="btn sm" data-action="compose-sms" data-candidate-id="${Number(candidate.id)}" data-candidate-name="${escapeHtml(candidate.name)}" data-phone="${escapeHtml(phone)}">Send SMS</button>` : ""}
      <button type="button" class="btn ghost sm" data-action="move" data-id="${Number(candidate.id)}">Move ▾</button>
    </div>
  </article>`;
}

function jobOptions(includeAll = false) {
  const all = includeAll
    ? `<option value=""${activeJobId ? "" : " selected"}>All candidates</option>`
    : `<option value="">No job selected</option>`;
  return all + jobs.map((job) => (
    `<option value="${Number(job.id)}"${Number(job.id) === Number(activeJobId) ? " selected" : ""}>${escapeHtml(job.title)}</option>`
  )).join("");
}

async function viewCandidates() {
  $("#title").textContent = "Candidates";
  try {
    const suffix = activeJobId ? `?job_id=${encodeURIComponent(activeJobId)}` : "";
    const [candidates, stats] = await Promise.all([
      api(`/candidates${suffix}`),
      api("/stats"),
    ]);
    $("#content").innerHTML = `
      <div class="notice">Candidate contact access · human approval required · do-not-contact enforced.</div>
      <div class="kpis">
        ${kpi("Candidates", stats.total_candidates)}
        ${kpi("Enriched", stats.enriched)}
        ${kpi("Contacted", stats.by_stage?.contacted)}
        ${kpi("Jobs", stats.jobs)}
        ${kpi("Do-Not-Contact", stats.dnc)}
      </div>
      <div class="card">
        <div class="row spread">
          <div class="row grow">
            <label class="muted small" for="jobSelect">Job</label>
            <select id="jobSelect" class="field-auto">${jobOptions(true)}</select>
          </div>
          <div class="row">
            ${IS_EXTENSION ? `<button type="button" class="btn capture-btn" data-action="capture-indeed">Capture sourcing profile</button>` : ""}
            <button type="button" class="btn teal" data-action="enrich-all">Enrich all</button>
            <button type="button" class="btn ghost" data-action="rank-all"${activeJobId ? "" : " disabled"}>Rank vs job</button>
            <button type="button" class="btn" data-action="navigate" data-view="add">Add candidates</button>
          </div>
        </div>
      </div>
      <div class="cards">
        ${candidates.length
          ? candidates.map(candidateCard).join("")
          : `<div class="card"><p class="muted">No candidates found for this selection.</p><button type="button" class="btn" data-action="navigate" data-view="add">Add candidates</button></div>`}
      </div>`;
  } catch (error) {
    setConnection();
    $("#content").innerHTML = backendError(error);
  }
}

function viewAdd() {
  $("#title").textContent = "Add Candidates";
  $("#content").innerHTML = `
    ${IS_EXTENSION ? `<div class="card capture-card">
      <h3>Import from a sourcing platform</h3>
      <p class="muted small">Open an authorized candidate or provider page in Indeed, Vivian, ZipRecruiter, LinkedIn, Facebook, NPI No., NPI Profile, U.S. News Doctor Finder, MediFind, CommonSpirit Health, Sharecare, or NYSED, then capture it for review and contact enrichment.</p>
      <button type="button" class="btn capture-btn" data-action="capture-indeed">Capture current sourcing profile</button>
    </div>` : ""}
    <div class="card">
      <h3>Choose the destination job</h3>
      <select id="addJobSelect">${jobOptions(false)}</select>
      <p class="muted small">Candidates can be added without a job, but ranking requires one.</p>
    </div>
    <div class="card">
      <h3>Create a job</h3>
      <div class="row">
        <input id="jobTitle" class="grow-2" placeholder="Job title (for example, Radiologic Technologist)">
        <input id="jobLocation" class="grow" placeholder="Location (for example, Atlanta, GA)">
        <button type="button" class="btn" data-action="create-job">Save job</button>
      </div>
      <textarea id="jobDescription" class="mt" rows="3" placeholder="Job description and key skills used for fit ranking"></textarea>
    </div>
    <div class="card">
      <h3>Add candidate names</h3>
      <p class="muted small">Paste names you are entitled to work with—one per line, or CSV with a <code>name</code> column.</p>
      <textarea id="candidatePaste" rows="8" placeholder="Jane Doe, Atlanta, GA&#10;John Smith - Dallas, TX&#10;Maria Lopez | Chicago, IL"></textarea>
      <div class="row mt">
        <button type="button" class="btn teal" data-action="submit-intake">Add candidates</button>
        <span class="muted small" id="intakeMessage"></span>
      </div>
      <p class="muted small mt">This tool does not scrape job platforms. Bring names from your ATS, applicants, referrals, or a manual list.</p>
    </div>`;
}

async function createJob() {
  const title = $("#jobTitle")?.value.trim();
  if (!title) throw new Error("Enter a job title.");
  const result = await api("/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title,
      location: $("#jobLocation")?.value.trim() || "",
      description: $("#jobDescription")?.value.trim() || "",
    }),
  });
  await loadJobs();
  activeJobId = Number(result.id);
  notify("Job saved.");
  viewAdd();
}

async function submitIntake() {
  const text = $("#candidatePaste")?.value || "";
  if (!text.trim()) throw new Error("Paste at least one candidate name.");
  const result = await api("/candidates/intake", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, job_id: activeJobId }),
  });
  notify(`Added ${result.added} candidate${result.added === 1 ? "" : "s"}.`);
  await go("candidates");
}

async function enrichCandidate(id) {
  const result = await api(`/candidates/${id}/contact-lookup`, { method: "POST" });
  if (result?.credits_remaining != null) {
    extensionCredits = { enabled: true, balance: Math.max(0, Number(result.credits_remaining)) };
    updateSourceCreditBalance();
  }
  notify("Candidate enriched.");
  await viewCandidates();
}

async function enrichAll() {
  const suffix = activeJobId ? `?job_id=${encodeURIComponent(activeJobId)}` : "";
  const result = await api(`/enrich/batch${suffix}`, { method: "POST", timeout: 120000 });
  notify(`Enriched ${result.matched} of ${result.processed} processed candidates.`);
  await viewCandidates();
}

async function rankAll() {
  if (!activeJobId) throw new Error("Select a job before ranking.");
  const result = await api(`/jobs/${activeJobId}/rank`, { method: "POST" });
  notify(`Ranked ${result.ranked} candidates against ${result.job}.`);
  await viewCandidates();
}

async function moveCandidate(id) {
  const requested = prompt(`Move to: ${STAGES.join(", ")}`);
  if (requested === null) return;
  const stage = requested.trim().toLowerCase();
  if (!STAGES.includes(stage)) throw new Error("Choose a valid pipeline stage.");
  await api(`/candidates/${id}/stage`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage }),
  });
  notify(`Candidate moved to ${stage}.`);
  await viewCandidates();
}

function closeModal() {
  $("#modalRoot").replaceChildren();
  activeDraft = null;
  activeIndeedProfile = null;
  activePublicRecordResult = null;
  publicRecordReturnProfile = null;
}

function sendTabMessage(tabId, message, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error("The candidate page did not respond. Wait for it to load and try again."));
    }, timeoutMs);
    chrome.tabs.sendMessage(tabId, message, (response) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const error = chrome.runtime.lastError;
      if (error) {
        reject(new Error(error.message));
        return;
      }
      resolve(response);
    });
  });
}

function sendExtensionMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      const error = chrome.runtime.lastError;
      if (error) {
        reject(new Error(error.message));
        return;
      }
      resolve(response);
    });
  });
}

function platformForUrl(value) {
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    return Object.values(SOURCING_PLATFORMS).find((platform) => platform.host(hostname, url)) || null;
  } catch {
    return null;
  }
}

function normalizedSourcingUrl(value) {
  try {
    const url = new URL(value);
    url.hash = "";
    return url.href;
  } catch {
    return String(value || "");
  }
}

const INDEED_TRANSIENT_SEARCH_PARAMS = new Set([
  "candidate", "candidate_id", "candidateid", "drawer", "modal", "profile_id",
  "profileid", "resume_id", "resumeid", "selected_candidate", "selectedcandidate",
]);

function canonicalIndeedSearchUrl(value) {
  try {
    const url = new URL(value);
    url.hash = "";
    for (const key of Array.from(url.searchParams.keys())) {
      if (INDEED_TRANSIENT_SEARCH_PARAMS.has(key.toLowerCase())) url.searchParams.delete(key);
    }
    url.searchParams.sort();
    return url.href;
  } catch {
    return String(value || "");
  }
}

function sameIndeedSearchContext(firstValue, secondValue) {
  if (!firstValue || !secondValue) return false;
  if (canonicalIndeedSearchUrl(firstValue) === canonicalIndeedSearchUrl(secondValue)) return true;
  try {
    const first = new URL(firstValue);
    const second = new URL(secondValue);
    if (first.origin !== second.origin || first.pathname !== second.pathname) return false;
    const onlyTransientKeys = (url) => {
      const keys = Array.from(url.searchParams.keys()).map((key) => key.toLowerCase());
      return keys.length > 0 && keys.every((key) => INDEED_TRANSIENT_SEARCH_PARAMS.has(key));
    };
    return onlyTransientKeys(first) || onlyTransientKeys(second);
  } catch {
    return false;
  }
}

function sourcingContextKey(tab, platform) {
  return `${Number(tab?.id) || 0}|${platform?.key || "unsupported"}|${normalizedSourcingUrl(tab?.url)}`;
}

function readChromeSetting(key) {
  return new Promise((resolve) => chrome.storage.local.get([key], (result) => resolve(result[key])));
}

function writeChromeSetting(key, value) {
  return new Promise((resolve) => chrome.storage.local.set({ [key]: value }, resolve));
}

async function readChromeSession(key) {
  // Authentication is remembered only inside this installed Chrome profile.
  // storage.local survives panel/browser restarts and extension upgrades, but
  // unlike storage.sync it is not copied to another laptop or Google account.
  const persisted = await new Promise((resolve) => chrome.storage.local.get(
    [key], (result) => resolve(result[key]),
  ));
  if (persisted) return persisted;

  // Migrate a still-valid session created by an older Medhunt build so the
  // user does not have to enter one final email code after upgrading.
  if (!chrome.storage.session) return null;
  const legacy = await new Promise((resolve) => chrome.storage.session.get(
    [key], (result) => resolve(result[key]),
  ));
  if (!legacy) return null;
  await new Promise((resolve) => chrome.storage.local.set({ [key]: legacy }, resolve));
  await new Promise((resolve) => chrome.storage.session.remove([key], resolve));
  return legacy;
}

async function writeChromeSession(key, value) {
  if (value) {
    await new Promise((resolve) => chrome.storage.local.set({ [key]: value }, resolve));
  } else {
    await new Promise((resolve) => chrome.storage.local.remove([key], resolve));
  }
  if (chrome.storage.session) {
    await new Promise((resolve) => chrome.storage.session.remove([key], resolve));
  }
}

function authenticatedApiHeaders(initialHeaders = {}) {
  const headers = new Headers(initialHeaders || {});
  if (LOCAL_API_TOKEN && !LOCAL_API_TOKEN.startsWith("__MEDHUNT_")) {
    headers.set("X-Medhunt-Token", LOCAL_API_TOKEN);
  }
  if (authSession?.extension_token) {
    headers.set("X-HealthBoard-Extension-Token", authSession.extension_token);
  }
  if (extensionDeviceId) headers.set("X-Medhunt-Device-ID", extensionDeviceId);
  return headers;
}

function extensionDeviceName() {
  const platform = navigator.userAgentData?.platform || navigator.platform || "Computer";
  const browser = /Edg\//.test(navigator.userAgent)
    ? "Edge"
    : /Chrome\//.test(navigator.userAgent) ? "Chrome" : "Browser";
  return `${platform} · ${browser}`.slice(0, 200);
}

async function ensureExtensionDeviceId() {
  if (!IS_EXTENSION) return "";
  if (extensionDeviceId) return extensionDeviceId;
  let value = await readChromeSetting(DEVICE_STORAGE_KEY);
  if (!/^[A-Za-z0-9_-]{40,64}$/.test(String(value || ""))) {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    const binary = Array.from(bytes, (byte) => String.fromCharCode(byte)).join("");
    value = btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
    await writeChromeSetting(DEVICE_STORAGE_KEY, value);
  }
  extensionDeviceId = value;
  return extensionDeviceId;
}

async function fetchStoredResumeBlob(candidateId, resumeId) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60000);
  try {
    await ensureExtensionDeviceId();
    const response = await fetch(
      `${apiBase}/candidates/${Number(candidateId)}/resumes/${Number(resumeId)}`,
      { headers: authenticatedApiHeaders(), signal: controller.signal },
    );
    if (!response.ok) {
      const type = response.headers.get("content-type") || "";
      const payload = type.includes("application/json")
        ? await response.json()
        : await response.text();
      const detail = payload && typeof payload === "object" ? payload.detail : payload;
      const requestError = new Error(detail || `Backend returned ${response.status}.`);
      requestError.status = response.status;
      throw requestError;
    }
    const blob = await response.blob();
    if (!blob.size || /application\/json/i.test(blob.type || "")) {
      throw new Error("The stored resume response was not a PDF file.");
    }
    return blob;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The resume download timed out.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function releaseResumeBlobUrlLater(url, delay = 300000) {
  setTimeout(() => URL.revokeObjectURL(url), delay);
}

async function loadAuth() {
  if (!IS_EXTENSION) return;
  await ensureExtensionDeviceId();
  // Read the remembered identity before contacting the hosted service. A
  // Render cold start must not make a signed-in user appear logged out.
  authSession = await readChromeSession(AUTH_STORAGE_KEY) || null;
  try {
    const configured = await api("/auth/config", { timeout: 6000 });
    authConfig = {
      ...configured,
      enabled: Boolean(configured?.enabled || HOSTED_AUTH_REQUIRED),
      provider: configured?.provider || "healthboard",
    };
  } catch {
    // A public package is always account-gated. Keep the login entry point
    // visible when the capability check is temporarily unavailable so a
    // network failure cannot silently downgrade the UI to local mode.
    authConfig = { enabled: HOSTED_AUTH_REQUIRED, provider: "healthboard" };
  }
  if (authConfig.enabled && authSession?.extension_token) {
    try {
      if (authSession.device_pending) {
        const status = await api("/auth/device-status", { timeout: 30000 });
        if (!status.approved) {
          authSession.device = status.device;
          await writeChromeSession(AUTH_STORAGE_KEY, authSession);
          renderAuthState();
          return;
        }
        authSession.device_pending = false;
        authSession.device = status.device;
        authSession.user = status.user;
      }
      const current = await api("/auth/me", { timeout: 30000 });
      authSession.user = current.user;
      await writeChromeSession(AUTH_STORAGE_KEY, authSession);
    } catch (error) {
      // Only an explicit authentication rejection invalidates the remembered
      // login. Timeouts, Render cold starts, and temporary 5xx responses do not.
      if ([401, 403].includes(Number(error?.status))) {
        if (/waiting for approval/i.test(String(error?.message || ""))) {
          authSession.device_pending = true;
          await writeChromeSession(AUTH_STORAGE_KEY, authSession);
        } else {
          authSession = null;
          await writeChromeSession(AUTH_STORAGE_KEY, null);
        }
      }
    }
  }
  renderAuthState();
}

async function refreshExtensionCredits() {
  if (!IS_EXTENSION || !authSession?.extension_token) {
    extensionCredits = { enabled: false, balance: null, status: "unavailable" };
    updateSourceCreditBalance();
    return;
  }
  extensionCredits = { enabled: false, balance: null, status: "loading" };
  updateSourceCreditBalance();
  try {
    const result = await api("/credits/enrichment", { timeout: 15000 });
    extensionCredits = {
      enabled: Boolean(result?.enabled && Number.isFinite(Number(result.balance))),
      balance: result?.balance == null ? null : Math.max(0, Number(result.balance)),
      status: result?.enabled && Number.isFinite(Number(result.balance)) ? "ready" : "unavailable",
    };
  } catch {
    // A transient balance check must not discard the signed-in session. The
    // backend independently fails closed before making a billable lookup.
    extensionCredits = { enabled: false, balance: null, status: "unavailable" };
  }
  updateSourceCreditBalance();
}

function updateSourceCreditBalance() {
  const badge = $("#sourceCreditBalance");
  if (!badge) return;
  badge.hidden = false;
  if (extensionCredits.enabled) {
    badge.textContent = `${Number(extensionCredits.balance || 0).toLocaleString()} lookup credits`;
    badge.title = "Candidate-enrichment credits. Cached contact results do not use credits.";
  } else if (extensionCredits.status === "loading") {
    badge.textContent = "Credits loading…";
    badge.title = "Loading your candidate-enrichment credit balance.";
  } else {
    badge.textContent = "Credits unavailable";
    badge.title = "Credit balance is unavailable. Check your connection or ask your administrator.";
  }
}

function renderAuthState() {
  const logoutButton = $("#logoutButton");
  const sourceLogoutButton = $("#sourceLogoutButton");
  const avatar = $("#userAvatar");
  const signedIn = Boolean(authConfig.enabled && authSession?.extension_token);
  if (logoutButton) logoutButton.hidden = !signedIn;
  if (sourceLogoutButton) sourceLogoutButton.hidden = !signedIn;
  if (!avatar) return;
  if (authConfig.enabled && authSession?.user) {
    const user = authSession.user;
    avatar.textContent = initials(user.name || user.email || "User");
    avatar.setAttribute("aria-label", user.name || user.email || "Signed-in user");
  } else if (authConfig.enabled) {
    avatar.textContent = "?";
    avatar.setAttribute("aria-label", "Sign in to Medhunt");
  } else {
    avatar.textContent = "MT";
    avatar.setAttribute("aria-label", "Medhunt local workspace");
  }
}

async function login({ requirePrivacyConsent = false } = {}) {
  if (!authConfig.enabled) throw new Error("Healthcareboard login is not configured.");
  pendingLogin = null;
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="medhuntLoginTitle">
      <div class="privacy-dialog-header">
        <img class="privacy-dialog-logo" src="icons/medhunt-logo.png" alt="" aria-hidden="true">
        <div><span class="privacy-eyebrow">Healthcareboard access</span><h2 id="medhuntLoginTitle">Sign in to Medhunt</h2></div>
      </div>
      <p class="muted">Use the email address on your Healthcareboard recruiter account.</p>
      <label>Email<input id="medhuntLoginEmail" type="email" autocomplete="email" maxlength="320" required></label>
      ${requirePrivacyConsent ? `<label class="consent-check"><input id="medhuntPrivacyAgreement" type="checkbox"> <span>I understand and agree to this candidate-data processing. <button type="button" class="inline-link" data-action="open-privacy">Read privacy notice</button></span></label>` : ""}
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="close-modal">Cancel</button>
        <button type="button" class="btn teal" data-action="request-login-code">Send code</button>
      </div>
    </div></div>`;
  $("#medhuntLoginEmail")?.focus();
}

async function requestLoginCode() {
  const email = String($("#medhuntLoginEmail")?.value || "").trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new Error("Enter a valid email address.");
  const consentCheckbox = $("#medhuntPrivacyAgreement");
  if (consentCheckbox && !consentCheckbox.checked) {
    throw new Error("Select the agreement checkbox before continuing.");
  }
  const response = await api("/auth/request-code", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }), timeout: 15000,
  });
  pendingLogin = {
    email,
    challenge: response.challenge,
    privacyConsent: Boolean(consentCheckbox),
  };
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="medhuntCodeTitle">
      <div class="privacy-dialog-header">
        <img class="privacy-dialog-logo" src="icons/medhunt-logo.png" alt="" aria-hidden="true">
        <div><span class="privacy-eyebrow">Healthcareboard access</span><h2 id="medhuntCodeTitle">Enter your email code</h2></div>
      </div>
      <p class="muted">We sent a six-digit code to ${escapeHtml(email)}. It expires in 10 minutes.</p>
      <label>Verification code<input id="medhuntLoginCode" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" required></label>
      ${pendingLogin.privacyConsent ? `<p class="login-consent-note">Your candidate-data consent will be saved after the code is verified.</p>` : ""}
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="login">Use another email</button>
        <button type="button" class="btn teal" data-action="verify-login-code">Verify & sign in</button>
      </div>
    </div></div>`;
  $("#medhuntLoginCode")?.focus();
}

async function verifyLoginCode() {
  const code = String($("#medhuntLoginCode")?.value || "").trim();
  if (!pendingLogin || !/^\d{6}$/.test(code)) throw new Error("Enter the six-digit code from your email.");
  const loginConsent = Boolean(pendingLogin.privacyConsent);
  const verified = await api("/auth/verify-code", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...pendingLogin,
      code,
      device_id: await ensureExtensionDeviceId(),
      device_name: extensionDeviceName(),
    }), timeout: 15000,
  });
  authSession = {
    extension_token: verified.extension_token,
    user: verified.user,
    device: verified.device,
    device_pending: verified.device_approval_required === true,
  };
  await writeChromeSession(AUTH_STORAGE_KEY, authSession);
  if (loginConsent) {
    privacyConsent = true;
    await writeChromeSetting(PRIVACY_CONSENT_KEY, true);
  }
  pendingLogin = null;
  renderAuthState();
  if (authSession.device_pending) {
    showPendingDeviceApproval();
    return;
  }
  closeModal();
  if (!extensionWorkspaceStarted && privacyConsent) {
    await startExtensionWorkspace();
    notify(`Signed in as ${verified.user.email}.`);
    return;
  }
  await refreshHealth();
  try { await loadJobs(); } catch { /* the status banner explains a backend outage */ }
  await go(activeView);
  notify(`Signed in as ${verified.user.email}.`);
}

async function logout() {
  authSession = null;
  extensionCredits = { enabled: false, balance: null, status: "unavailable" };
  await writeChromeSession(AUTH_STORAGE_KEY, null);
  renderAuthState();
  updateSourceCreditBalance();
  closeModal();
  notify("Signed out.");
}

function deviceTimestamp(value) {
  const timestamp = Number(value || 0);
  return timestamp ? new Date(timestamp * 1000).toLocaleString() : "Not yet";
}

function showPendingDeviceApproval() {
  const device = authSession?.device || {};
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="devicePendingTitle">
      <div class="privacy-dialog-header">
        <img class="privacy-dialog-logo" src="icons/medhunt-logo.png" alt="" aria-hidden="true">
        <div><span class="privacy-eyebrow">Device security</span><h2 id="devicePendingTitle">Approval required</h2></div>
      </div>
      <p>This installation is signed in, but it cannot access candidate data yet.</p>
      <div class="notice"><strong>${escapeHtml(device.device_name || extensionDeviceName())}</strong><br><span class="muted small">Request ending ${escapeHtml(device.installation_suffix || "")}</span></div>
      <p class="muted small">Ask a Healthcareboard administrator to approve this installation. No verification code needs to be shared.</p>
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="logout">Cancel sign-in</button>
        <button type="button" class="btn teal" data-action="check-device-approval">Check approval</button>
      </div>
    </div></div>`;
}

async function checkDeviceApproval() {
  if (!authSession?.extension_token) return login();
  const status = await api("/auth/device-status", { timeout: 30000 });
  authSession.device = status.device;
  authSession.user = status.user;
  authSession.device_pending = !status.approved;
  await writeChromeSession(AUTH_STORAGE_KEY, authSession);
  renderAuthState();
  if (!status.approved) {
    showPendingDeviceApproval();
    notify("This device is still waiting for approval.");
    return;
  }
  closeModal();
  notify("This device is approved.");
  if (!privacyConsent) {
    showPrivacyConsent();
    return;
  }
  await startExtensionWorkspace();
}

function deviceCard(device, isAdmin = false) {
  const owner = device.user_name || device.user_email || device.user_id || "Account user";
  const canRevoke = device.status !== "revoked" && !device.current;
  return `<div class="device-row">
    <div class="device-row-main">
      <div class="row spread"><strong>${escapeHtml(device.device_name || "Medhunt browser")}</strong><span class="pill ${device.status === "approved" ? "live" : ""}">${escapeHtml(device.status)}</span></div>
      <span class="muted small">${escapeHtml(owner)}${device.current ? " · This device" : ""}</span>
      ${isAdmin && Number(device.approved_device_count) > 1 ? `<span class="muted small">${Number(device.approved_device_count)} approved devices on this account</span>` : ""}
      <span class="muted small">Last used ${escapeHtml(deviceTimestamp(device.last_seen))} · ID …${escapeHtml(device.installation_suffix || "")}</span>
    </div>
    <div class="row device-actions">
      ${canRevoke ? `<button type="button" class="btn sm ghost" data-action="revoke-device" data-device-id="${Number(device.id)}">Revoke</button>` : ""}
    </div>
  </div>`;
}

async function showAccount() {
  const result = await api("/auth/devices");
  const devices = result.items || [];
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <div class="dialog device-dialog" role="dialog" aria-modal="true" aria-labelledby="deviceAccountTitle">
      <div class="privacy-dialog-header">
        <img class="privacy-dialog-logo" src="icons/medhunt-logo.png" alt="" aria-hidden="true">
        <div><span class="privacy-eyebrow">Account security</span><h2 id="deviceAccountTitle">Registered devices</h2></div>
      </div>
      <p class="muted small">Your registered devices. Organization and platform administrators manage device approvals in Halo.</p>
      <div class="device-list">${devices.length ? devices.map((device) => deviceCard(device, false)).join("") : `<p class="muted">No registered devices.</p>`}</div>
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="logout">Sign out</button>
        <button type="button" class="btn" data-action="close-modal">Close</button>
      </div>
    </div></div>`;
}

async function revokeDevice(button) {
  await api(`/auth/devices/${Number(button.dataset.deviceId)}/revoke`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: "Revoked from Medhunt account settings" }),
  });
  notify("Device access revoked.");
  await showAccount();
}

function showPrivacyConsent() {
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <div class="dialog privacy-dialog" role="dialog" aria-modal="true" aria-labelledby="medhuntPrivacyTitle" aria-describedby="medhuntPrivacyDescription">
      <div class="privacy-dialog-header">
        <img class="privacy-dialog-logo" src="icons/medhunt-logo.png" alt="" aria-hidden="true">
        <div><span class="privacy-eyebrow">Secure workspace</span><h2 id="medhuntPrivacyTitle">Before Medhunt reads profile data</h2></div>
      </div>
      <p class="privacy-dialog-lede">Review how candidate information is handled before you continue.</p>
      <div class="privacy-dialog-copy" id="medhuntPrivacyDescription">
        <p>Medhunt reads professional information visible on supported candidate pages. Nothing is sent while the page is only being detected.</p>
        <p>When you select candidates and choose <strong>Find contact details</strong>, Medhunt sends the selected names, locations, roles, employers, education, specialties, and profile links to the Medhunt backend. It may then retrieve professional contact details, create or upload a resume, and send the resulting candidate record to your configured recruiting system.</p>
        <p>Use Medhunt only for candidate data your organization is authorized to process. Medhunt does not send outreach automatically.</p>
      </div>
      <label class="consent-check"><input id="medhuntPrivacyAgreement" type="checkbox"> <span>I understand and agree to this candidate-data processing.</span></label>
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="open-privacy">Read privacy notice</button>
        <button type="button" class="btn ghost" data-action="decline-privacy">Not now</button>
        <button type="button" class="btn teal" data-action="accept-privacy">Agree and continue</button>
      </div>
    </div></div>`;
}

async function openPrivacyNotice() {
  const url = `${apiBase}/privacy`;
  await chrome.tabs.create({ url, active: true });
}

async function declinePrivacyConsent() {
  privacyConsent = false;
  await writeChromeSetting(PRIVACY_CONSENT_KEY, false);
  closeModal();
  clearCapturedProfileState("consent-required");
  renderSourcingStatus(
    "Consent required",
    "Medhunt will not read or transmit candidate profile data until you review and accept the data-use notice.",
    { retry: false },
  );
}

async function acceptPrivacyConsent() {
  if (!$("#medhuntPrivacyAgreement")?.checked) {
    throw new Error("Select the agreement checkbox before continuing.");
  }
  privacyConsent = true;
  await writeChromeSetting(PRIVACY_CONSENT_KEY, true);
  closeModal();
  await startExtensionWorkspace();
}

const PASSIVE_TAB_CONTEXT_REASONS = new Set(["activated", "window-focused", "removed"]);

function isPassiveSourcingContextEvent(reason, tab, platform) {
  if (!PASSIVE_TAB_CONTEXT_REASONS.has(String(reason || "")) || !tab?.id || !platform) return false;
  if (sourcingContextKey(tab, platform) === activeSourcingContextKey) return true;
  return (
    platform.key === "indeed" && activeSourcingPlatform?.key === "indeed" &&
    Number(tab.id) === Number(activeSourcingTabId) &&
    sameIndeedSearchContext(tab.url || "", activeSourcingPageUrl)
  );
}

function sourcingPageEligibility(platform, value) {
  if (!platform) return { eligible: false, reason: "unsupported" };
  let url;
  try {
    url = new URL(value);
  } catch {
    return { eligible: false, reason: "loading" };
  }
  if (platform.key === "linkedin") {
    const eligible = /^\/in\/[^/?#]+\/?$/i.test(url.pathname) ||
      /^\/search\/results\/(?:people|all)(?:\/|$)/i.test(url.pathname);
    return { eligible, reason: eligible ? "" : "linkedin-route" };
  }
  if (platform.key === "facebook") {
    const eligible = Boolean(globalThis.RadixsolProfileQuality?.validProfileUrl(url.href, "facebook"));
    return { eligible, reason: eligible ? "" : "facebook-route" };
  }
  return { eligible: true, reason: "" };
}

function activateSourcingContext(tab, platform, reset = true) {
  const nextKey = sourcingContextKey(tab, platform);
  const changed = nextKey !== activeSourcingContextKey;
  activeSourcingPlatform = platform || activeSourcingPlatform;
  activePageIndicatorLabel = platform?.label || "Candidate page";
  activeSourcingPageUrl = String(tab?.url || "");
  activeSourcingTabId = Number(tab?.id) || 0;
  activeSourcingWindowId = Number(tab?.windowId) || 0;
  activeSourcingContextKey = nextKey;
  if (changed && reset) clearCapturedProfileState("idle");
  return changed;
}

async function activeSourcingTab(findExisting = false) {
  if (!IS_EXTENSION) throw new Error("Platform capture is only available in the browser extension.");
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const currentPlatform = tab?.id ? platformForUrl(tab.url) : null;
  if (currentPlatform) return { tab, platform: currentPlatform };
  if (findExisting) {
    const tabs = await chrome.tabs.query({ currentWindow: true });
    const supported = tabs.find((candidate) => candidate?.id && platformForUrl(candidate.url));
    if (supported) {
      await chrome.tabs.update(supported.id, { active: true });
      return { tab: supported, platform: platformForUrl(supported.url) };
    }
  }
  throw new Error("Open a supported Medhunt candidate or provider page first.");
}

function clearCapturedProfileState(phase = "idle") {
  activeIndeedProfile = null;
  indeedCandidates = [];
  indeedSelected = new Set();
  indeedLookupState = new Map();
  indeedLookupScope = new Set();
  indeedLookupProfiles = [];
  indeedLookupSummary = null;
  indeedResultFilter = "all";
  indeedSaveStatus = null;
  skippedProfileCount = 0;
  indeedScanState = { phase, found: 0, total: 0 };
}

function sourcingWorkInProgress() {
  return indeedLookupInProgress || indeedResumeBatchState.active;
}

function isProtectedIndeedResumeNavigation(tabId = 0, pageUrl = "") {
  const candidateTabId = Number(tabId) || 0;
  if (indeedResumeBatchState.active) {
    const sameTab = !candidateTabId || candidateTabId === Number(indeedResumeBatchState.sourceTabId);
    return sameTab && (!pageUrl || sameIndeedSearchContext(pageUrl, indeedResumeBatchState.sourcePageUrl));
  }
  const sameTab = !candidateTabId || candidateTabId === Number(indeedResumeNavigationGrace.sourceTabId);
  return Date.now() < Number(indeedResumeNavigationGrace.until) && sameTab && (
    !pageUrl || sameIndeedSearchContext(pageUrl, indeedResumeNavigationGrace.sourcePageUrl)
  );
}

async function sendSourcingMessage(message, findExisting = false, expectedContext = null) {
  const { tab, platform } = await activeSourcingTab(findExisting);
  if (expectedContext && (
    Number(expectedContext.tabId) !== Number(tab.id) ||
    expectedContext.key !== sourcingContextKey(tab, platform)
  )) {
    throw new Error("The active candidate page changed during the scan.");
  }
  activateSourcingContext(tab, platform, false);
  const injectPlatformScript = async () => {
    if (platform.mainScript) {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: [platform.mainScript],
        world: "MAIN",
      });
    }
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: [platform.contentScript],
    });
  };
  const currentAdapterMessage = platform.adapterRequestType
    ? { ...message, type: platform.adapterRequestType, original_type: message.type }
    : message;
  try {
    const response = await sendTabMessage(tab.id, message);
    // An unpacked extension reload does not replace a listener already living
    // in an open Facebook tab.  Upgrade it in place when its adapter revision
    // differs, including when the stale listener returned an extraction error.
    if (platform.adapterRevision && response?.adapter_revision !== platform.adapterRevision) {
      await injectPlatformScript();
      const upgraded = await sendTabMessage(tab.id, currentAdapterMessage);
      return { ...upgraded, _sourceTabId: tab.id, _sourceWindowId: tab.windowId };
    }
    return { ...response, _sourceTabId: tab.id, _sourceWindowId: tab.windowId };
  } catch {
    await injectPlatformScript();
    const response = await sendTabMessage(tab.id, currentAdapterMessage);
    return { ...response, _sourceTabId: tab.id, _sourceWindowId: tab.windowId };
  }
}

async function sendIndeedResumeMessage(message, sourceTabId = 0) {
  let tab = null;
  if (sourceTabId) {
    try {
      tab = await chrome.tabs.get(Number(sourceTabId));
    } catch {
      tab = null;
    }
  }
  if (!tab?.id || platformForUrl(tab.url)?.key !== "indeed") {
    throw new Error("The Indeed tab used for this lookup is no longer open.");
  }
  await chrome.tabs.update(tab.id, { active: true });
  const currentMessage = {
    ...message,
    type: SOURCING_PLATFORMS.indeed.adapterRequestType,
    original_type: message.type,
  };
  try {
    const response = await sendTabMessage(tab.id, currentMessage);
    if (response?.adapter_revision === SOURCING_PLATFORMS.indeed.adapterRevision) return response;
    throw new Error("The Indeed page adapter needs to be refreshed.");
  } catch {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id }, files: [SOURCING_PLATFORMS.indeed.mainScript], world: "MAIN",
    });
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["indeed-content.js"] });
    return sendTabMessage(tab.id, currentMessage);
  }
}

async function captureIndeedProfile() {
  const result = await sendSourcingMessage({ type: "RADIXSOL_CAPTURE_PLATFORM_PROFILE" });
  if (!result?.ok) {
    throw new Error(result?.error || `The visible ${activeSourcingPlatform.label} profile could not be read.`);
  }
  const checked = globalThis.RadixsolProfileQuality?.sanitizeProfile(result.profile, {
    platform: activeSourcingPlatform.key,
    pageUrl: result.page_url || activeSourcingPageUrl,
    singleProfile: true,
  });
  if (checked && !checked.profile) {
    throw new Error("This page does not contain a usable candidate identity.");
  }
  showIndeedImport(checked?.profile || result.profile);
}

function showIndeedImport(profile) {
  activeIndeedProfile = profile;
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="importTitle">
      <h3 id="importTitle">Review ${escapeHtml(activeSourcingPlatform.label)} profile</h3>
      <div class="notice">Confirm the extracted identity before contact enrichment. Only this reviewed profile will be imported.</div>
      <label class="field-label" for="importName">Candidate name</label>
      <input id="importName" value="${escapeHtml(profile.name || "")}">
      <label class="field-label" for="importLocation">Location</label>
      <input id="importLocation" value="${escapeHtml(profile.location || "")}">
      <label class="field-label" for="importHeadline">Headline or current role</label>
      <input id="importHeadline" value="${escapeHtml(profile.headline || "")}">
      <label class="field-label" for="importJobSelect">Add to job</label>
      <select id="importJobSelect">${jobOptions(false)}</select>
      <details class="mt">
        <summary>Captured profile text</summary>
        <textarea id="importNotes" class="mt" rows="8">${escapeHtml(profile.notes || "")}</textarea>
      </details>
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="close-modal">Cancel</button>
        ${publicRecordButton(profile.name, profile.location, 0, "btn")}
        <button type="button" class="btn teal" data-action="import-indeed">Import & enrich</button>
      </div>
    </section>
  </div>`;
}

async function importIndeedProfile() {
  if (!activeIndeedProfile) throw new Error(`Capture a ${activeSourcingPlatform.label} profile first.`);
  const name = $("#importName")?.value.trim();
  if (!name) throw new Error("Confirm the candidate name before importing.");
  const selectedJob = $("#importJobSelect")?.value;
  const jobId = selectedJob ? Number(selectedJob) : null;

  const imported = await api("/candidates/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...activeIndeedProfile,
      name,
      location: $("#importLocation")?.value.trim() || "",
      headline: $("#importHeadline")?.value.trim() || "",
      notes: $("#importNotes")?.value.trim() || "",
      job_id: jobId,
    }),
  });
  const enriched = await api(`/candidates/${imported.id}/contact-lookup`, {
    method: "POST",
    timeout: 60000,
  });
  const candidateJob = imported.candidate?.job_id;
  activeJobId = candidateJob ?? jobId;
  closeModal();
  await go("candidates");

  if (enriched.status === "error") {
    notify("Profile imported, but contact lookup could not be completed.", "error");
    return;
  }
  const contacts = (enriched.emails?.length || 0) + (enriched.phones?.length || 0);
  notify(`${imported.imported ? "Imported" : "Opened existing"} ${activeSourcingPlatform.label} candidate with ${contacts} contact result${contacts === 1 ? "" : "s"}.`);
}

// ---- public-record lookup ----
// Provider selection, credentials, and trust decisions stay in the backend.
function publicRecordAvailable() {
  return Boolean(backendHealth?.records_lookup?.enabled);
}

function isBrowserDownloadSurface(value) {
  try {
    const url = new URL(value);
    return ["chrome:", "edge:"].includes(url.protocol) &&
      ["downloads", "download-internals"].includes(url.hostname.toLowerCase());
  } catch {
    return false;
  }
}

async function sendLinkedinPdfMessage(tabId, messageType) {
  const currentMessage = {
    type: SOURCING_PLATFORMS.linkedin.adapterRequestType,
    original_type: messageType,
  };
  try {
    const response = await sendTabMessage(Number(tabId), currentMessage);
    if (response?.adapter_revision === SOURCING_PLATFORMS.linkedin.adapterRevision) return response;
    throw new Error("The LinkedIn page adapter needs to be refreshed.");
  } catch {
    await chrome.scripting.executeScript({
      target: { tabId: Number(tabId) },
      files: ["linkedin-content.js"],
    });
    return sendTabMessage(Number(tabId), currentMessage);
  }
}

async function waitForLinkedinProfile(tabId, sourceUrl, timeoutMs = 30000) {
  const expectedSlug = linkedinSlug(sourceUrl);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    let tab = null;
    try { tab = await chrome.tabs.get(Number(tabId)); } catch { tab = null; }
    if (tab && linkedinSlug(tab.url || "") === expectedSlug && tab.status === "complete") return tab;
    await wait(250);
  }
  throw new Error("LinkedIn did not finish opening the exact candidate profile.");
}

function sameProfessionalProfileUrl(firstValue, secondValue) {
  try {
    const first = new URL(firstValue);
    const second = new URL(secondValue);
    const firstPath = first.pathname.replace(/\/+$/, "").toLowerCase();
    const secondPath = second.pathname.replace(/\/+$/, "").toLowerCase();
    return first.hostname.toLowerCase() === second.hostname.toLowerCase() && firstPath === secondPath;
  } catch {
    return false;
  }
}

async function captureProfessionalProfileInBackground(profile, timeoutMs = 45000) {
  const sourceUrl = String(profile?.source_url || "").trim();
  const platform = platformForUrl(sourceUrl);
  if (!sourceUrl || !platform || !PROFESSIONAL_PROFILE_SOURCES.has(platform.key)) {
    throw new Error("The professional profile link is unavailable.");
  }
  const tab = await chrome.tabs.create({ url: sourceUrl, active: false });
  const tabId = Number(tab?.id);
  if (!tabId) throw new Error(`The ${platform.label} profile tab could not be opened.`);
  const deadline = Date.now() + timeoutMs;
  try {
    let loaded = false;
    while (Date.now() < deadline) {
      let current = null;
      try { current = await chrome.tabs.get(tabId); } catch { current = null; }
      if (!current) throw new Error(`The ${platform.label} profile tab was closed.`);
      if (sameProfessionalProfileUrl(current.url || "", sourceUrl) && current.status === "complete") {
        loaded = true;
        break;
      }
      await wait(250);
    }
    if (!loaded) throw new Error(`${platform.label} did not finish opening the exact candidate profile.`);

    const message = {
      type: platform.adapterRequestType,
      original_type: "RADIXSOL_CAPTURE_PLATFORM_PROFILE",
    };
    let lastError = null;
    while (Date.now() < deadline) {
      try {
        const response = await sendTabMessage(tabId, message, 5000);
        if (response?.ok && response.profile?.profile_document) return response.profile;
        lastError = new Error(response?.error || `${platform.label} is still loading the profile details.`);
      } catch (error) {
        lastError = error;
        // The content script can attach just after tabs.status becomes
        // complete. Inject it once if the first message arrives too early.
        try {
          await chrome.scripting.executeScript({ target: { tabId }, files: ["healthcare-directory-content.js"] });
        } catch { /* The next poll will report a useful timeout if the tab closed. */ }
      }
      await wait(750);
    }
    throw lastError || new Error(`${platform.label} did not expose the professional profile details.`);
  } finally {
    await chrome.tabs.remove(tabId).catch(() => {});
  }
}

function professionalProfileImportPayload(profile) {
  const bounded = (value, limit) => String(value ?? "").trim().slice(0, limit);
  const list = (values, limit, maxChars = 240) => {
    const output = [];
    const seen = new Set();
    for (const value of Array.isArray(values) ? values : []) {
      const item = bounded(value, maxChars).replace(/\s+/g, " ");
      const key = item.toLowerCase();
      if (!item || seen.has(key)) continue;
      seen.add(key);
      output.push(item);
      if (output.length >= limit) break;
    }
    return output;
  };
  const specialties = profileSpecialties(profile, list);
  return {
    name: bounded(profile.name, 200),
    location: bounded(profile.location, 500),
    hometown: bounded(profile.hometown, 500),
    headline: bounded(profile.headline, 500),
    roles: list(profile.roles, 20),
    employers: list(profile.employers, 20),
    schools: list(profile.schools, 20),
    specialties,
    alternate_names: list([...(profile.alternate_names || []), ...(profile.aliases || [])], 10, 160),
    notes: bounded(profile.notes, 20000),
    source: bounded(profile.source, 50),
    source_url: bounded(profile.source_url, 2000),
    source_id: bounded(profile.source_id, 500),
  };
}

// Resume metadata is durable in the Medhunt service and the bytes are kept in
// private object storage. Reuse that central copy before opening a source
// profile again for another recruiter or a reopened panel.
async function hydrateStoredResume(profile) {
  const candidateId = Number(profile?._candidateId);
  if (!candidateId) return null;
  try {
    const candidate = await api(`/candidates/${candidateId}`, { timeout: 15000 });
    const resume = Array.isArray(candidate?.resumes) ? candidate.resumes[0] : null;
    if (!resume || !Number(resume.id)) return null;
    // A public-profile PDF may have been stored before the contact lookup
    // completed.  Do not reuse that un-enriched snapshot: the backend will
    // rebuild it through the normal profile path and prepend the current
    // contact sheet.  The filename is the durable marker written by
    // _store_resume_pdf when the sheet was embedded.
    const hasContacts = Boolean(
      (Array.isArray(candidate?.emails) && candidate.emails.length)
      || (Array.isArray(candidate?.phones) && candidate.phones.length),
    );
    const hasContactSheet = /\s-\s+enriched\.pdf$/i.test(String(resume.filename || ""));
    if (hasContacts && !hasContactSheet) return null;
    const current = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      ...current,
      resume,
      resume_status: "stored",
      resume_error: "",
    });
    updateIndeedLookupProgressUi(profile);
    return resume;
  } catch {
    return null;
  }
}

// Keep source-declared specialty labels separate from generic skills. This
// accepts structured adapter fields and explicit Specialty: notes emitted by
// healthcare-directory adapters, without guessing that an arbitrary skill or
// headline is a recruiting-system specialty.
function profileSpecialties(profile, list) {
  const values = [profile?.specialty, ...(Array.isArray(profile?.specialties) ? profile.specialties : [])];
  for (const line of String(profile?.notes || "").split(/\r?\n/)) {
    const match = line.match(/^specialt(?:y|ies)\s*:\s*(.+)$/i);
    if (match) values.push(...match[1].split(/\s*[;,|]\s*/));
  }
  return list(values, 20, 240);
}

async function enrichProfessionalProfileAndResume(profile) {
  const previous = indeedLookupFor(profile);
  indeedLookupState.set(profile._selectionKey, {
    ...previous,
    resume_status: "opening",
    resume_error: "",
  });
  updateIndeedLookupProgressUi(profile);
  const captured = profile.profile_document?.kind === "public_professional_profile"
    && profile._detail_capture_ready !== false
    && sameProfessionalProfileUrl(activeSourcingPageUrl, profile.source_url)
    ? profile
    : await captureProfessionalProfileInBackground(profile);
  const platform = SOURCING_PLATFORMS[profile.source];
  if (!captured?.profile_document) {
    throw new Error(`${platform?.label || "The profile"} did not contain professional details.`);
  }

  // Keep the selection identity/source id from the search card so the rich
  // profile updates the existing candidate instead of creating a duplicate.
  Object.assign(profile, captured, {
    source: profile.source,
    source_id: profile.source_id || captured.source_id,
    _candidateId: profile._candidateId,
    _selectionKey: profile._selectionKey,
    _sourceTabId: profile._sourceTabId,
    _sourceWindowId: profile._sourceWindowId,
    _sourceContextKey: profile._sourceContextKey,
    _detailProfileCaptured: true,
  });
  const current = indeedLookupFor(profile);
  indeedLookupState.set(profile._selectionKey, {
    ...current,
    resume_status: "capturing",
    resume_error: "",
  });
  updateIndeedLookupProgressUi(profile);
  await api("/candidates/import/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profiles: [professionalProfileImportPayload(profile)], job_id: activeJobId, search_url: activeSourcingPageUrl || "" }),
    timeout: 120000,
  });
  return ensureProfessionalProfileResume(profile);
}

function startProfessionalProfileResumeBatch(profiles) {
  const pending = (profiles || []).filter((profile) => (
    PROFESSIONAL_PROFILE_SOURCES.has(profile?.source)
      && Number(profile._candidateId)
      && hasCompleteIndeedContact(indeedLookupFor(profile))
      && (!indeedLookupFor(profile).resume || !profile._detailProfileCaptured)
  ));
  if (!pending.length || indeedResumeBatchState.active) return Promise.resolve();

  return (async () => {
    const queue = [];
    for (const profile of pending) {
      if (!(await hydrateStoredResume(profile))) queue.push(profile);
    }
    if (!queue.length) return;
    const first = queue[0];
    indeedResumeBatchState = {
      active: true,
      total: queue.length,
      processed: 0,
      saved: 0,
      failed: 0,
      sourceTabId: Number(first._sourceTabId || activeSourcingTabId),
      sourceWindowId: Number(first._sourceWindowId || activeSourcingWindowId),
      sourceContextKey: String(first._sourceContextKey || activeSourcingContextKey),
      sourcePageUrl: String(activeSourcingPageUrl || first.source_url || ""),
      platform: first.source,
    };
    updateSourceHeaderProgressUi();
    try {
      for (const profile of queue) {
        let saved = false;
        try {
          saved = Boolean(await enrichProfessionalProfileAndResume(profile));
        } catch (error) {
          const latest = indeedLookupFor(profile);
          indeedLookupState.set(profile._selectionKey, {
            ...latest,
            resume_status: "failed",
            resume_error: friendlyActionError(error),
          });
          updateIndeedLookupProgressUi(profile);
        }
        indeedResumeBatchState.processed += 1;
        if (saved) indeedResumeBatchState.saved += 1;
        else indeedResumeBatchState.failed += 1;
        updateSourceHeaderProgressUi();
      }
    } finally {
      const completed = { ...indeedResumeBatchState };
      indeedResumeBatchState = { ...indeedResumeBatchState, active: false };
      updateSourceHeaderProgressUi();
      if (completed.saved) {
        notify(
          `${completed.saved} professional profile resume${completed.saved === 1 ? "" : "s"} saved` +
          `${completed.failed ? `; ${completed.failed} unavailable` : ""}.`,
          completed.failed ? "error" : "",
        );
      } else if (completed.failed) {
        notify("Professional profile resumes could not be saved.", "error");
      }
    }
  })();
}

function linkedinResumeCompletion(candidateId, timeoutMs = 150000) {
  const id = Number(candidateId);
  let timer = null;
  let settled = false;
  let resolvePromise;
  let rejectPromise;
  const cleanup = () => {
    clearTimeout(timer);
    if (resumeDownloadWaiters.get(id)?.token === token) resumeDownloadWaiters.delete(id);
  };
  const token = Symbol(`linkedin-resume-${id}`);
  const promise = new Promise((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  const finish = (callback, value) => {
    if (settled) return;
    settled = true;
    cleanup();
    callback(value);
  };
  resumeDownloadWaiters.set(id, {
    token,
    resolve: (value) => finish(resolvePromise, value),
    reject: (error) => finish(rejectPromise, error),
  });
  timer = setTimeout(() => {
    const waiter = resumeDownloadWaiters.get(id);
    if (waiter?.token === token) waiter.reject(new Error("LinkedIn did not complete the PDF download."));
  }, timeoutMs);
  return {
    promise,
    cancel: (error) => {
      const waiter = resumeDownloadWaiters.get(id);
      if (waiter?.token === token) waiter.reject(error instanceof Error ? error : new Error(String(error)));
    },
  };
}

function sequentialLookupMode() {
  return backendHealth?.lookup_behavior === "sequential";
}

function lookupDurationEstimate(count) {
  // The backend reports the API's own 30-90 second range; quote the midpoint
  // so a 45-profile selection is not started expecting a quick answer.
  const [fast = 30, slow = 90] = backendHealth?.records_lookup?.typical_seconds || [];
  const minutes = Math.max(1, Math.round((count * ((fast + slow) / 2)) / 60));
  return `${minutes} minute${minutes === 1 ? "" : "s"}`;
}

function publicRecordButton(name, location, candidateId = 0, className = "btn ghost sm") {
  if (!publicRecordAvailable() || !String(name || "").trim()) return "";
  return `<button type="button" class="${className}" data-action="public-records"
    data-qs-name="${escapeHtml(name)}"
    data-qs-location="${escapeHtml(location || "")}"
    data-qs-candidate="${Number(candidateId) || 0}">Candidate details</button>`;
}

function publicRecordRetryAttributes(context) {
  return `data-qs-name="${escapeHtml(context.name || "")}"
    data-qs-location="${escapeHtml(context.location || "")}"
    data-qs-candidate="${Number(context.candidateId) || 0}"`;
}

function showPublicRecordProgress(name) {
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <section class="sheet qs-sheet" role="dialog" aria-modal="true" aria-labelledby="qsProgressTitle">
      <h3 id="qsProgressTitle">Searching public records</h3>
      <div class="qs-progress">
        <span class="qs-spinner" aria-hidden="true"></span>
        <div>
          <strong>${escapeHtml(name)}</strong>
          <p class="muted">A first-time search opens the source site in a real browser, so it usually takes 30 to 90 seconds. Keep this panel open.</p>
        </div>
      </div>
    </section>
  </div>`;
}

function publicRecordPhoneLine(phone) {
  const detail = [
    phone.type,
    phone.carrier,
    phone.last_reported ? `last seen ${phone.last_reported}` : "",
  ].filter(Boolean).join(" · ");
  return `<li>
    <span class="qs-value">${escapeHtml(phone.value)}</span>
    ${phone.primary ? `<span class="qs-tag">primary</span>` : ""}
    ${detail ? `<small class="muted">${escapeHtml(detail)}</small>` : ""}
  </li>`;
}

function publicRecordPersonLine(person) {
  const detail = [
    person.relationship,
    person.age ? `age ${person.age}` : "",
    person.deceased ? "deceased" : "",
  ].filter(Boolean).join(" · ");
  return `<li><span class="qs-value">${escapeHtml(person.name)}</span>${
    detail ? `<small class="muted">${escapeHtml(detail)}</small>` : ""
  }</li>`;
}

function publicRecordList(title, items, renderItem, { collapsed = false } = {}) {
  if (!items?.length) return "";
  const body = `<ul class="qs-list">${items.map(renderItem).join("")}</ul>`;
  if (!collapsed) return `<section class="qs-block"><h4>${escapeHtml(title)}</h4>${body}</section>`;
  return `<details class="qs-block">
    <summary>${escapeHtml(title)} (${items.length})</summary>${body}
  </details>`;
}

function publicRecordResultBody(result) {
  const current = result.current_address || {};
  const identity = [
    result.age ? `age ${result.age}` : "",
    result.born ? `born ${result.born}` : "",
    result.source ? `source: ${result.source}` : "",
    result.cached ? "saved result" : "",
  ].filter(Boolean).join(" · ");

  return `
    <div class="qs-identity">
      <strong>${escapeHtml(result.name || "Unnamed record")}</strong>
      ${identity ? `<small class="muted">${escapeHtml(identity)}</small>` : ""}
    </div>
    ${publicRecordList("Phone numbers", result.phones, publicRecordPhoneLine)}
    ${publicRecordList("Email addresses", result.emails, (email) => (
      `<li><span class="qs-value">${escapeHtml(email)}</span></li>`
    ))}
    ${result.masked_emails?.length ? `<section class="qs-block">
      <h4>Masked email addresses</h4>
      <ul class="qs-list">${result.masked_emails.map((email) => (
        `<li><span class="qs-value">${escapeHtml(email)}</span></li>`
      )).join("")}</ul>
      <p class="muted small">The source site masks these for non-paying visitors. They cannot be unmasked here.</p>
    </section>` : ""}
    ${current.address ? `<section class="qs-block">
      <h4>Current address</h4>
      <div class="qs-value">${escapeHtml(current.address)}</div>
      ${[current.county, current.date_range, current.property_details].filter(Boolean).map((line) => (
        `<small class="muted">${escapeHtml(line)}</small>`
      )).join("")}
    </section>` : ""}
    ${publicRecordList("Work history", result.employment, (job) => {
      const detail = [
        job.industry,
        [job.from, job.to].filter(Boolean).join(" – "),
      ].filter(Boolean).join(" · ");
      return `<li><span class="qs-value">${escapeHtml([job.title, job.employer].filter(Boolean).join(" — "))}</span>${
        detail ? `<small class="muted">${escapeHtml(detail)}</small>` : ""
      }</li>`;
    })}
    ${publicRecordList("Education", result.education, (school) => (
      `<li><span class="qs-value">${escapeHtml([school.institution, school.degree].filter(Boolean).join(" — "))}</span></li>`
    ))}
    ${publicRecordList("Also known as", result.also_known_as, (alias) => (
      `<li><span class="qs-value">${escapeHtml(alias)}</span></li>`
    ), { collapsed: true })}
    ${publicRecordList("Addresses on record", result.addresses, (address) => (
      `<li><span class="qs-value">${escapeHtml(address)}</span></li>`
    ), { collapsed: true })}
    ${publicRecordList("Relatives", result.relatives, publicRecordPersonLine, { collapsed: true })}
    ${publicRecordList("Associates", result.associates, publicRecordPersonLine, { collapsed: true })}
    ${publicRecordList("Businesses", result.businesses, (business) => (
      `<li><span class="qs-value">${escapeHtml(business.name)}</span>${
        business.address ? `<small class="muted">${escapeHtml(business.address)}</small>` : ""
      }</li>`
    ), { collapsed: true })}`;
}

function showPublicRecordResult(result, context) {
  const searched = [context.name, context.location].filter(Boolean).join(" · ");
  const retry = publicRecordRetryAttributes(context);
  const found = result.status === "found";
  const messages = {
    not_found: "No public record matched this name. A miss can be temporary when the source site is busy or blocking automated searches, so a second attempt is often worth it.",
    disabled: "Public records lookup is not available. Ask the administrator to check the service.",
    error: result.error || "The lookup could not be completed.",
  };
  activePublicRecordResult = found ? result : null;

  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <section class="sheet qs-sheet" role="dialog" aria-modal="true" aria-labelledby="qsTitle">
      <h3 id="qsTitle">Public records</h3>
      <div class="notice">Searched ${escapeHtml(searched || context.name || "")}. These details come from public people-search sites and are shown for review only — they are not saved as verified candidate contacts.</div>
      ${found ? publicRecordResultBody(result) : `<p class="qs-empty">${escapeHtml(messages[result.status] || messages.error)}</p>`}
      <div class="row modal-actions">
        ${publicRecordReturnProfile ? `<button type="button" class="btn ghost" data-action="public-records-back">Back to review</button>` : ""}
        <button type="button" class="btn ghost" data-action="close-modal">Close</button>
        ${found ? `<button type="button" class="btn" data-action="public-records-copy">Copy details</button>` : ""}
        ${result.status === "disabled" ? "" : `<button type="button" class="btn teal" data-action="public-records-refresh" ${retry}>${found ? "Search again" : "Try again"}</button>`}
      </div>
    </section>
  </div>`;
}

function publicRecordClipboardText(result) {
  const lines = [result.name || ""];
  if (result.phones?.length) {
    lines.push("", "Phones:");
    result.phones.forEach((phone) => {
      const detail = [phone.type, phone.carrier, phone.last_reported].filter(Boolean).join(", ");
      lines.push(`  ${phone.value}${detail ? ` (${detail})` : ""}`);
    });
  }
  if (result.emails?.length) lines.push("", "Emails:", ...result.emails.map((email) => `  ${email}`));
  if (result.current_address?.address) {
    lines.push("", `Current address: ${result.current_address.address}`);
  }
  if (result.employment?.length) {
    lines.push("", "Work:");
    result.employment.forEach((job) => {
      lines.push(`  ${[job.title, job.employer].filter(Boolean).join(" — ")}`);
    });
  }
  return lines.join("\n").trim();
}

async function copyPublicRecordResult() {
  if (!activePublicRecordResult) throw new Error("Open a public records result first.");
  await navigator.clipboard.writeText(publicRecordClipboardText(activePublicRecordResult));
  notify("Public record details copied to the clipboard.");
}

async function publicRecordLookup({ name, location = "", candidateId = 0, refresh = false }) {
  const person = String(name || "").trim();
  if (!person) throw new Error("Confirm the candidate name before searching public records.");
  if (!publicRecordAvailable()) {
    throw new Error("Public records lookup is not configured on this backend.");
  }
  const context = { name: person, location: String(location || "").trim(), candidateId };
  showPublicRecordProgress(person);
  let result;
  try {
    result = await api("/records/find", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: context.name,
        location: context.location,
        candidate_id: Number(candidateId) || null,
        refresh: Boolean(refresh),
      }),
      // One uncached search drives a real browser on the Hub side.
      timeout: 190000,
    });
  } catch (error) {
    showPublicRecordResult({ status: "error", error: error.message }, context);
    return;
  }
  showPublicRecordResult(result, context);
}

async function publicRecordFromButton(button, refresh = false) {
  // A lookup started from the capture review sheet replaces that sheet, so
  // remember the reviewed profile and offer a way back to it.
  publicRecordReturnProfile = activeIndeedProfile || null;
  await publicRecordLookup({
    name: button.dataset.qsName,
    location: button.dataset.qsLocation,
    candidateId: Number(button.dataset.qsCandidate) || 0,
    refresh,
  });
}

function reopenCaptureReview() {
  const profile = publicRecordReturnProfile;
  publicRecordReturnProfile = null;
  if (!profile) throw new Error("The reviewed profile is no longer available.");
  showIndeedImport(profile);
}

function indeedProfileKey(profile, index) {
  if (profile.source_id) return `id:${profile.source_id}`;
  return `row:${index}:${profile.name || ""}:${profile.location || ""}`;
}

function updateIndeedSelectionUi() {
  const count = indeedSelected.size;
  const counter = $("#indeedSelectedCount");
  if (counter) counter.textContent = String(count);
  const lookupLabel = $("#indeedLookupLabel");
  if (lookupLabel) {
    lookupLabel.textContent = count
      ? "Find contact details"
      : "Select profiles to continue";
  }
  document.querySelectorAll("[data-requires-indeed-selection]").forEach((button) => {
    button.disabled = count === 0;
  });
  document.querySelectorAll(".capture-row").forEach((row) => {
    row.classList.toggle("selected", indeedSelected.has(row.dataset.profileKey));
  });
  const selectionToggle = $(".selection-toggle");
  if (selectionToggle) {
    selectionToggle.textContent = indeedCandidates.length > 0 && count === indeedCandidates.length
      ? "Clear selection"
      : "Select all";
  }
  const selectAll = $("#indeedSelectAll");
  if (selectAll) {
    selectAll.checked = indeedCandidates.length > 0 && count === indeedCandidates.length;
    selectAll.indeterminate = count > 0 && count < indeedCandidates.length;
  }
}

function indeedLookupFor(profile) {
  return indeedLookupState.get(profile._selectionKey) || { status: "pending" };
}

function isIndeedMatch(result) {
  return result?.status === "found" &&
    ((result.emails?.length || 0) > 0 || (result.phones?.length || 0) > 0);
}

function hasCompleteIndeedContact(result) {
  return isIndeedMatch(result) && result?.resume_required === true;
}

function indeedResultStatus(profile) {
  const result = indeedLookupFor(profile);
  const resumeActionDisabled = indeedResumeBatchState.active
    ? ` disabled aria-disabled="true"`
    : "";
  const resume = result.resume
    ? `<button type="button" class="resume-link" data-action="open-resume" data-candidate-id="${Number(profile._candidateId)}" data-resume-id="${Number(result.resume.id)}"${resumeActionDisabled}>Open resume</button>`
    : "";
  const resumeStatus = ["armed", "downloading", "building", "opening", "capturing"].includes(result.resume_status)
    ? `<span class="lookup-detail">${result.resume_status === "building" ? "Building professional profile" : result.resume_status === "opening" ? "Opening professional profile" : result.resume_status === "capturing" ? "Reading professional profile" : "Resume in progress"}</span>`
    : result.resume_status === "failed" || result.resume_error
      ? `<span class="lookup-detail">Resume unavailable</span>`
      : "";
  if (result.status === "looking_up") {
    return `<span class="lookup-searching"><i aria-hidden="true"></i>Checking contact</span>`;
  }
  if (isIndeedMatch(result)) {
    const emails = result.emails || [];
    const phoneContacts = publicPhoneContacts(result);
    const hometownMatch = result.location_match?.type === "hometown" &&
      result.location_match?.value
      ? `<span class="lookup-detail">Contact found using From location: ${escapeHtml(result.location_match.value)}</span>`
      : "";
    const shownEmails = emails.slice(0, ROW_CONTACT_LIMIT);
    const shownPhones = phoneContacts.slice(0, ROW_CONTACT_LIMIT);
    const mobile = phoneContacts.find((phone) => phone.kind === "mobile");
    return `<div class="lookup-contact">
      <span class="lookup-state match"><i aria-hidden="true"></i>Contact ready</span>
      ${shownEmails.map((email) => `<span class="lookup-value">${escapeHtml(email)}</span>`).join("")}
      ${shownPhones.map((phone) => `<span class="lookup-value">${escapeHtml(`${publicPhoneLabel(phone.kind)}: ${phone.value}`)}</span>`).join("")}
      ${moreContactsNote(
        shownEmails.length + shownPhones.length,
        emails.length + phoneContacts.length,
      )}
      ${hometownMatch}
      ${resume}
      ${resumeStatus}
      ${mobile && profile._candidateId ? `<button type="button" class="resume-link" data-action="compose-sms" data-candidate-id="${Number(profile._candidateId)}" data-candidate-name="${escapeHtml(profile.name)}" data-phone="${escapeHtml(mobile.value)}">Send SMS</button>` : ""}
    </div>`;
  }
  if (result.status === "not_found") {
    return `<div class="lookup-outcome">
      <span class="lookup-state no-match"><i aria-hidden="true"></i>Contact unavailable</span>
      ${resume}${resumeStatus}
    </div>`;
  }
  if (result.status === "failed") {
    return `<div class="lookup-outcome">
      <span class="lookup-state lookup-error"><i aria-hidden="true"></i>Needs retry</span>
      ${resume}${resumeStatus}
    </div>`;
  }
  return resume;
}

function linkedinPdfControl(profile, index) {
  if (!activeSourcingPlatform.guidedPdfCapture) return "";
  const result = indeedLookupFor(profile);
  if (result.resume) {
    return `<div class="linkedin-pdf-tools">
      <button type="button" class="linkedin-pdf-button stored" data-action="open-resume"
        data-candidate-id="${Number(profile._candidateId)}" data-resume-id="${Number(result.resume.id)}">
        Open resume
      </button>
    </div>`;
  }
  if (result.resume_status === "armed") {
    return `<div class="linkedin-pdf-tools">
      <span>Resume in progress</span>
      <button type="button" class="linkedin-pdf-button cancel" data-action="cancel-linkedin-pdf" data-index="${index}">Cancel</button>
    </div>`;
  }
  if (result.resume_status === "downloading") {
    return `<div class="linkedin-pdf-tools"><span>Resume in progress</span></div>`;
  }
  if (!hasCompleteIndeedContact(result)) {
    return `<div class="linkedin-pdf-tools"><span>Find a phone number or email before saving a resume.</span></div>`;
  }
  return `<div class="linkedin-pdf-tools">
    ${result.resume_error ? `<span>Resume unavailable</span>` : ""}
    <button type="button" class="linkedin-pdf-button" data-action="capture-linkedin-pdf" data-index="${index}">Save resume</button>
  </div>`;
}

function indeedFilteredProfiles() {
  const inLookupView = indeedScanState.phase === "results";
  // Render from the immutable selection snapshot. Indeed can replace or
  // virtualize its result-card DOM while a backend lookup is in flight.
  const sourceProfiles = inLookupView && indeedLookupProfiles.length
    ? indeedLookupProfiles
    : indeedCandidates;
  const scopedProfiles = inLookupView && indeedLookupScope.size
    ? sourceProfiles.filter((profile) => indeedLookupScope.has(profile._selectionKey))
    : sourceProfiles;
  if (indeedResultFilter === "matched") {
    return scopedProfiles.filter((profile) => isIndeedMatch(indeedLookupFor(profile)));
  }
  if (indeedResultFilter === "no_match") {
    return scopedProfiles.filter((profile) => indeedLookupFor(profile)?.status === "not_found");
  }
  if (indeedResultFilter === "failed") {
    return scopedProfiles.filter((profile) => indeedLookupFor(profile)?.status === "failed");
  }
  return scopedProfiles;
}

function sourceHeaderProgressSnapshot() {
  if (indeedResumeBatchState.active) {
    const total = Math.max(0, Number(indeedResumeBatchState.total) || 0);
    const processed = Math.max(0, Number(indeedResumeBatchState.processed) || 0);
    const current = total > 0 ? Math.min(processed, total) : processed;
    return {
      kind: "resume",
      current,
      total,
      determinate: total > 0,
      percent: total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0,
      status: `Saving resumes - ${current} of ${total}`,
      ariaLabel: "Resume storage progress",
      ariaValueText: `${current} of ${total} resumes processed`,
    };
  }
  if (indeedScanState.phase === "scanning") {
    const observed = Math.max(0, Number(indeedScanState.found) || 0);
    const total = Math.max(0, Number(indeedScanState.total) || 0);
    const determinate = total > 0;
    const current = determinate ? Math.min(observed, total) : observed;
    return {
      kind: "scan",
      current,
      total,
      determinate,
      percent: determinate ? Math.min(100, Math.round((current / total) * 100)) : 35,
      status: determinate ? `Reading profiles - ${current} of ${total}` : `Reading profiles - ${current} found`,
      ariaLabel: "Candidate profile scan progress",
      ariaValueText: determinate
        ? `${current} of ${total} candidate profiles found`
        : `${current} candidate profiles found; total is still being determined`,
    };
  }
  if (indeedScanState.phase === "lookup") {
    const processed = Math.max(0, Number(indeedLookupSummary?.processed) || 0);
    const total = Math.max(0, Number(indeedLookupSummary?.total) || indeedSelected.size);
    const current = total > 0 ? Math.min(processed, total) : processed;
    const contactsFound = Math.max(0, Number(indeedLookupSummary?.matched) || 0);
    return {
      kind: "lookup",
      current,
      total,
      determinate: total > 0,
      percent: total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0,
      status: `Checking contacts - ${current} of ${total}`,
      ariaLabel: "Contact lookup progress",
      ariaValueText: `${current} of ${total} candidates checked; ${contactsFound} ${contactsFound === 1 ? "contact" : "contacts"} found`,
    };
  }
  return null;
}

function updateIndeedResumeControlsUi() {
  const busy = indeedResumeBatchState.active;
  document.querySelectorAll([
    '[data-action="refresh-indeed"].inline-scan-action',
    '[data-action="retry-failed-lookups"]',
    '[data-action="open-indeed-result"]',
    '[data-action="open-resume"]',
    '[data-action="public-records"]',
  ].join(",")).forEach((button) => {
    button.disabled = busy;
    button.setAttribute("aria-disabled", busy ? "true" : "false");
  });
}

function updateSourceHeaderProgressUi() {
  const progress = sourceHeaderProgressSnapshot();
  const status = $("#sourceHeaderStatus");
  const header = $("[data-testid='source-header']");
  const track = $("#sourceHeaderProgress");
  const meter = $("[data-testid='source-progressbar']");
  const bar = $("#sourceHeaderProgressBar");
  const rescan = header?.querySelector(".panel-rescan-button");
  if (status) {
    status.textContent = progress?.status || (backendHealth ? "Ready to find contacts" : "Service offline");
  }
  if (header) {
    header.classList.toggle("is-busy", Boolean(progress));
    header.setAttribute("aria-busy", progress ? "true" : "false");
    header.dataset.progressKind = progress?.kind || "none";
  }
  if (rescan) {
    rescan.disabled = Boolean(progress);
    rescan.setAttribute("aria-disabled", progress ? "true" : "false");
  }
  updateIndeedResumeControlsUi();
  if (!track || !meter || !bar) return;
  track.className = `header-progress${progress ? " is-active" : " is-inactive"}${progress && !progress.determinate ? " is-indeterminate" : ""}`;
  track.dataset.kind = progress?.kind || "none";
  track.dataset.mode = progress ? (progress.determinate ? "determinate" : "indeterminate") : "inactive";
  if (!progress) {
    meter.removeAttribute("role");
    track.setAttribute("aria-hidden", "true");
    meter.removeAttribute("aria-label");
    meter.removeAttribute("aria-valuemin");
    meter.removeAttribute("aria-valuemax");
    meter.removeAttribute("aria-valuenow");
    meter.removeAttribute("aria-valuetext");
    bar.style.width = "0%";
    return;
  }
  meter.setAttribute("role", "progressbar");
  track.removeAttribute("aria-hidden");
  meter.setAttribute("aria-label", progress.ariaLabel);
  meter.setAttribute("aria-valuetext", progress.ariaValueText);
  if (progress.determinate) {
    meter.setAttribute("aria-valuemin", "0");
    meter.setAttribute("aria-valuemax", String(progress.total));
    meter.setAttribute("aria-valuenow", String(progress.current));
  } else {
    meter.removeAttribute("aria-valuemin");
    meter.removeAttribute("aria-valuemax");
    meter.removeAttribute("aria-valuenow");
  }
  bar.style.width = `${progress.percent}%`;
}

function indeedPanelHeader() {
  const platformLabel = escapeHtml(activePageIndicatorLabel);
  const progress = sourceHeaderProgressSnapshot();
  const serviceState = progress?.status || (backendHealth ? "Ready to find contacts" : "Service offline");
  return `
    <header class="source-shell-header panel-brand${progress ? " is-busy" : ""}" data-testid="source-header" data-progress-kind="${progress?.kind || "none"}" aria-busy="${progress ? "true" : "false"}">
      <img class="medhunt-mark" src="icons/medhunt-logo.png" alt="Med Hunt">
      <div class="source-brand-copy">
        <strong class="sr-only">Medhunt</strong>
        <span id="sourceHeaderStatus">${escapeHtml(serviceState)}</span>
      </div>
      <div class="source-header-actions">
        <span class="active-page-indicator"><i aria-hidden="true"></i>${platformLabel}</span>
        <span class="source-credit-balance" id="sourceCreditBalance">${extensionCredits.enabled ? `${Number(extensionCredits.balance || 0).toLocaleString()} lookup credits` : extensionCredits.status === "loading" ? "Credits loading…" : "Credits unavailable"}</span>
        <button type="button" class="panel-account-button" id="sourceLogoutButton" data-action="logout" aria-label="Log out of Medhunt" title="Log out"${authConfig.enabled && authSession?.extension_token ? "" : " hidden"}>Log out</button>
        <button type="button" class="panel-rescan-button" data-action="refresh-indeed" title="Scan current page" aria-label="Scan current page" aria-disabled="${progress ? "true" : "false"}"${progress ? " disabled" : ""}>
          <span aria-hidden="true">&#8635;</span>
        </button>
      </div>
      <div id="sourceHeaderProgress" class="header-progress${progress ? " is-active" : " is-inactive"}${progress && !progress.determinate ? " is-indeterminate" : ""}" data-testid="source-progress" data-kind="${progress?.kind || "none"}" data-mode="${progress ? (progress.determinate ? "determinate" : "indeterminate") : "inactive"}"
        ${progress ? "" : `aria-hidden="true"`}>
        <div data-testid="source-progressbar"${progress ? ` role="progressbar" aria-label="${escapeHtml(progress.ariaLabel)}" aria-valuetext="${escapeHtml(progress.ariaValueText)}"${progress.determinate ? ` aria-valuemin="0" aria-valuemax="${progress.total}" aria-valuenow="${progress.current}"` : ""}` : ""}>
          <span id="sourceHeaderProgressBar" data-testid="source-progress-fill" style="width:${progress?.percent || 0}%"></span>
        </div>
      </div>
    </header>`;
}

function updateSourceHeaderServiceStatus() {
  const status = $("#sourceHeaderStatus");
  if (!status) return;
  const progress = sourceHeaderProgressSnapshot();
  status.textContent = progress?.status || (backendHealth ? "Ready to find contacts" : "Service offline");
}

function renderSourcingStatus(title, message, options = {}) {
  const tone = options.tone === "error" ? " error" : "";
  const action = options.retry === false ? "" : `
    <button type="button" class="status-action" data-action="refresh-indeed">Scan current page</button>`;
  $("#title").textContent = "Source Profiles";
  $("#content").innerHTML = `
    <section class="indeed-workflow">
      ${indeedPanelHeader()}
      <div class="source-status${tone}" role="status">
        <strong>${escapeHtml(title)}</strong>
        <span>${escapeHtml(message)}</span>
        ${action}
      </div>
    </section>`;
}

function renderIndeedScanning() {
  $("#title").textContent = "Profiles";
  $("#content").innerHTML = `
    <section class="indeed-workflow scan-view">
      ${indeedPanelHeader()}
      <div class="source-status scan-status" aria-live="polite">
        <strong>Reading candidate profiles</strong>
        <span>Profiles will appear here when the scan is complete.</span>
      </div>
    </section>`;
}

function renderIndeedProfiles(scan = {}) {
  const profiles = indeedFilteredProfiles();
  const hasResults = indeedScanState.phase === "results" && Boolean(indeedLookupSummary);
  const isLookingUp = indeedScanState.phase === "lookup";
  const matched = Number(indeedLookupSummary?.matched) || 0;
  const noMatch = Number(indeedLookupSummary?.no_match) || 0;
  const failed = Number(indeedLookupSummary?.errors) || 0;
  $("#content").innerHTML = `
    <section class="indeed-workflow">
      ${indeedPanelHeader()}

      ${hasResults ? `
        <div class="result-workbench-head">
          <div><span class="section-kicker">Lookup complete</span><strong>${matched + noMatch + failed} profiles reviewed</strong></div>
          <button type="button" class="inline-scan-action" data-action="refresh-indeed" aria-disabled="${indeedResumeBatchState.active ? "true" : "false"}"${indeedResumeBatchState.active ? " disabled" : ""}>New scan</button>
        </div>
        <div class="result-summary${failed ? " has-errors" : ""}" role="tablist" aria-label="Filter lookup results" data-testid="result-filters">
          <button type="button" role="tab" aria-selected="${indeedResultFilter === "all"}" tabindex="${indeedResultFilter === "all" ? "0" : "-1"}" class="summary-tile${indeedResultFilter === "all" ? " active" : ""}" data-action="filter-indeed-results" data-filter="all">
            <span class="summary-signal all" aria-hidden="true"></span><span>All</span><strong>${matched + noMatch + failed}</strong>
          </button>
          <button type="button" role="tab" aria-selected="${indeedResultFilter === "matched"}" tabindex="${indeedResultFilter === "matched" ? "0" : "-1"}" class="summary-tile matched${indeedResultFilter === "matched" ? " active" : ""}" data-action="filter-indeed-results" data-filter="matched">
            <span class="summary-signal" aria-hidden="true"></span><span>Ready</span><strong>${matched}</strong>
          </button>
          <button type="button" role="tab" aria-selected="${indeedResultFilter === "no_match"}" tabindex="${indeedResultFilter === "no_match" ? "0" : "-1"}" class="summary-tile${indeedResultFilter === "no_match" ? " active" : ""}" data-action="filter-indeed-results" data-filter="no_match">
            <span class="summary-signal" aria-hidden="true"></span><span>No contact</span><strong>${noMatch}</strong>
          </button>
          ${failed ? `<button type="button" role="tab" aria-selected="${indeedResultFilter === "failed"}" tabindex="${indeedResultFilter === "failed" ? "0" : "-1"}" class="summary-tile failed${indeedResultFilter === "failed" ? " active" : ""}" data-action="filter-indeed-results" data-filter="failed">
            <span class="summary-signal" aria-hidden="true"></span><span>Retry</span><strong>${failed}</strong>
          </button>` : ""}
        </div>` : `
        <div class="capture-toolbar">
          <div class="queue-heading">
            <span class="section-kicker">Profiles on this page</span>
            <strong>${indeedCandidates.length} profiles ready</strong>
          </div>
          <button type="button" class="text-button selection-toggle" data-action="toggle-all-indeed">${indeedSelected.size === indeedCandidates.length ? "Clear selection" : "Select all"}</button>
        </div>`}

      <div class="indeed-candidate-list${hasResults ? " result-list" : ""}" id="indeedCandidateList" data-testid="profile-list">
        ${profiles.length
          ? profiles.map((profile) => {
          const key = profile._selectionKey;
          const originalIndex = indeedCandidates.indexOf(profile);
          const searchText = [profile.name, profile.location, profile.headline, ...(profile.roles || [])]
            .filter(Boolean).join(" ").toLowerCase();
          const avatar = `<div class="capture-avatar" aria-hidden="true">${escapeHtml(initials(profile.name))}</div>`;
          const identity = `<button type="button" class="capture-identity" data-action="open-indeed-result" data-index="${originalIndex}" aria-disabled="${indeedResumeBatchState.active ? "true" : "false"}"${indeedResumeBatchState.active ? " disabled" : ""}>
            <strong>${escapeHtml(profile.name)}</strong>
            <span class="candidate-meta">
              <span>${escapeHtml(profile.location || "Location not listed")}</span>
              ${profile.headline ? `<small>${escapeHtml(profile.headline)}</small>` : ""}
            </span>
          </button>`;
          const primary = hasResults
            ? `${avatar}${identity}`
            : `<div class="capture-row-primary">
                <label class="candidate-select-control"><input type="checkbox" class="indeed-select" data-key="${escapeHtml(key)}"${indeedSelected.has(key) ? " checked" : ""}${isLookingUp ? " disabled" : ""}><span class="sr-only">Select ${escapeHtml(profile.name)}</span></label>
                ${avatar}
                ${identity}
              </div>`;
          return `<article class="capture-row${hasResults ? "" : " candidate-queue-card"}${indeedSelected.has(key) ? " selected" : ""}" data-profile-key="${escapeHtml(key)}" data-search="${escapeHtml(searchText)}">
            ${primary}
            ${hasResults || isLookingUp ? `<div class="capture-result">${indeedResultStatus(profile)}</div>` : ""}
            ${publicRecordButton(profile.name, profile.location, profile._candidateId || 0, "capture-row-action")}
            ${linkedinPdfControl(profile, originalIndex)}
          </article>`;
        }).join("")
          : hasResults
            ? `<div class="empty-results"><strong>No candidates in this result filter</strong></div>`
            : `<div class="empty-results"><strong>No profiles found</strong><span>Open a supported candidate page and scan again.</span></div>`}
      </div>

      ${!hasResults && skippedProfileCount ? `<div class="capture-note">${skippedProfileCount} irrelevant or duplicate profile${skippedProfileCount === 1 ? " was" : "s were"} skipped.</div>` : ""}
      ${!hasResults ? `<div id="indeedSaveStatus" class="sync-status small ${escapeHtml(indeedSaveStatus?.state || "muted")}">${escapeHtml(indeedSaveStatus?.message || "")}</div>` : ""}

      ${hasResults ? (failed ? `<div class="results-actions">
          <span>${failed} profile${failed === 1 ? " needs" : "s need"} another attempt.</span>
          <button type="button" class="status-action secondary" data-action="retry-failed-lookups" aria-disabled="${indeedResumeBatchState.active ? "true" : "false"}"${indeedResumeBatchState.active ? " disabled" : ""}>Retry failed</button>
        </div>` : "") : `
        <div class="workflow-footer command-dock" data-testid="action-dock">
          <div class="selection-summary" aria-live="polite">
            <strong id="indeedSelectedCount">${indeedSelected.size}</strong>
            <span>selected</span>
          </div>
          <button type="button" class="lookup-button" data-action="lookup-indeed" data-requires-indeed-selection${indeedSelected.size ? "" : " disabled"}>
            <span id="indeedLookupLabel">${indeedSelected.size ? "Find contact details" : "Select profiles to continue"}</span>
            <span class="lookup-arrow" aria-hidden="true">&#8594;</span>
          </button>
        </div>`}
    </section>`;
  updateIndeedSelectionUi();
}

function showIndeedSaveStatus(state, message) {
  indeedSaveStatus = { state, message };
  const element = $("#indeedSaveStatus");
  if (!element) return;
  element.textContent = message;
  element.className = `sync-status small ${state}`;
}

async function ensureProfessionalProfileResume(profile) {
  const candidateId = Number(profile?._candidateId);
  const documentProfile = profile?.profile_document;
  if (
    !["usnews", "medifind", "commonspirit", "sharecare", "webmd"].includes(profile?.source) || !candidateId
    || !hasCompleteIndeedContact(indeedLookupFor(profile))
    || documentProfile?.kind !== "public_professional_profile"
  ) return null;
  const key = `${candidateId}|${JSON.stringify(documentProfile)}`;
  if (professionalProfileResumePromises.has(key)) {
    return professionalProfileResumePromises.get(key);
  }
  const previous = indeedLookupFor(profile);
  indeedLookupState.set(profile._selectionKey, {
    ...previous,
    resume_status: "building",
    resume_error: "",
  });
  updateIndeedLookupProgressUi(profile);
  const pending = (async () => {
    try {
      const attached = await api(`/candidates/${candidateId}/professional-profile-resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(documentProfile),
        timeout: 300000,
      });
      if (!attached?.attached || !Number(attached?.resume?.id)) {
        throw new Error("The generated professional profile response was incomplete.");
      }
      const latest = indeedLookupFor(profile);
      indeedLookupState.set(profile._selectionKey, {
        ...latest,
        resume: attached.resume,
        resume_status: "stored",
        resume_error: "",
      });
      updateIndeedLookupProgressUi(profile);
      // Keep the generated public-profile PDF in the backend/R2 as the
      // authoritative copy, and also save a local copy for the recruiter.
      // A browser download failure must not turn a successfully stored resume
      // into a false backend failure.
      if (IS_EXTENSION) {
        await saveStoredResumeDownload(profile, candidateId, attached.resume).catch(() => {});
      }
      return attached.resume;
    } catch (error) {
      professionalProfileResumePromises.delete(key);
      const latest = indeedLookupFor(profile);
      indeedLookupState.set(profile._selectionKey, {
        ...latest,
        resume_status: "failed",
        resume_error: friendlyActionError(error),
      });
      updateIndeedLookupProgressUi(profile);
      return null;
    }
  })();
  professionalProfileResumePromises.set(key, pending);
  return pending;
}

async function saveDisplayedIndeedCandidates(searchUrl, requestedProfiles = indeedCandidates) {
  const candidates = Array.isArray(requestedProfiles) ? requestedProfiles.filter(Boolean) : [];
  if (!candidates.length) {
    showIndeedSaveStatus("muted", "No selected profiles to save.");
    return null;
  }
  const selectionKey = candidates.map((profile) => profile._selectionKey || profile.source_id || "")
    .sort().join("|");
  const contextKey = `${activeSourcingContextKey || `${activeSourcingPlatform.key}|${searchUrl || ""}`}|${selectionKey}`;
  if (indeedSavePromises.has(contextKey)) return indeedSavePromises.get(contextKey);
  const snapshot = candidates.map((profile) => ({ ...profile }));
  const savePromise = performDisplayedIndeedSave(searchUrl, snapshot, contextKey);
  indeedSavePromises.set(contextKey, savePromise);
  try {
    return await savePromise;
  } finally {
    if (indeedSavePromises.get(contextKey) === savePromise) indeedSavePromises.delete(contextKey);
  }
}

async function approveCandidateIdentity(candidateId, profileKey, canonicalName) {
  if (!candidateId || !canonicalName) throw new Error("A review candidate name is required.");
  await api(`/candidates/${candidateId}/identity/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: "approved", canonical_name: canonicalName }),
  });
  const current = indeedLookupState.get(profileKey) || {};
  indeedLookupState.set(profileKey, {
    ...current,
    status: "review_approved",
    message: `Approved ${canonicalName}. Click Lookup again to fetch verified contacts.`,
    emails: [], phones: [], addresses: [],
  });
  renderIndeedProfiles();
}

async function performDisplayedIndeedSave(searchUrl) {
  const snapshot = arguments[1] || indeedCandidates.map((profile) => ({ ...profile }));
  const contextKey = arguments[2] || activeSourcingContextKey;
  if (contextKey === activeSourcingContextKey) {
    showIndeedSaveStatus("saving", `Saving ${snapshot.length} displayed profiles...`);
  }
  // Send only the versioned profile-import contract. Content scripts may add
  // adapter-only fields, and older platform pages can expose unexpectedly
  // large evidence arrays. Neither should invalidate an otherwise usable
  // batch before the backend can assign candidate IDs.
  const boundedText = (value, limit) => String(value ?? "").trim().slice(0, limit);
  const boundedList = (values, limit, maxChars = 240) => {
    const output = [];
    const seen = new Set();
    for (const value of Array.isArray(values) ? values : []) {
      const cleaned = boundedText(value, maxChars).replace(/\s+/g, " ");
      const key = cleaned.toLowerCase();
      if (!cleaned || seen.has(key)) continue;
      seen.add(key);
      output.push(cleaned);
      if (output.length >= limit) break;
    }
    return output;
  };
  const profiles = snapshot.map((profile) => ({
    name: boundedText(profile.name, 200),
    location: boundedText(profile.location, 500),
    hometown: boundedText(profile.hometown, 500),
    headline: boundedText(profile.headline, 500),
    roles: boundedList(profile.roles, 20),
    employers: boundedList(profile.employers, 20),
    schools: boundedList(profile.schools, 20),
    specialties: profileSpecialties(profile, boundedList),
    alternate_names: boundedList([
      ...(Array.isArray(profile.alternate_names) ? profile.alternate_names : []),
      ...(Array.isArray(profile.aliases) ? profile.aliases : []),
    ], 10, 160),
    notes: boundedText(profile.notes, 20000),
    source: boundedText(profile.source, 50),
    source_url: boundedText(profile.source_url, 2000),
    source_id: boundedText(profile.source_id, 500),
  }));
  try {
    const result = await api("/candidates/import/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        profiles,
        job_id: activeJobId,
        search_url: searchUrl || "",
      }),
      timeout: 120000,
    });
    const professionalProfiles = [];
    (result.results || []).forEach((saved, index) => {
      const source = snapshot[index];
      if (!source) return;
      const candidateId = Number(saved.id);
      source._candidateId = candidateId;
      let currentProfile = null;
      for (const collection of [indeedCandidates, indeedLookupProfiles]) {
        const profile = collection.find((item) => (
          item._selectionKey === source._selectionKey &&
          Number(item._sourceTabId || 0) === Number(source._sourceTabId || 0)
        ));
        if (profile) {
          profile._candidateId = candidateId;
          currentProfile ||= profile;
        }
      }
      const professionalProfile = currentProfile || source;
      if (
        professionalProfile.profile_document
        && professionalProfile._detail_capture_ready !== false
        && PROFESSIONAL_PROFILE_SOURCES.has(professionalProfile.source)
        && sameProfessionalProfileUrl(activeSourcingPageUrl, professionalProfile.source_url)
        && hasCompleteIndeedContact(indeedLookupFor(professionalProfile))
      ) {
        professionalProfile._detailProfileCaptured = true;
        professionalProfiles.push(professionalProfile);
      }
    });
    for (const profile of professionalProfiles) {
      void ensureProfessionalProfileResume(profile);
    }
    if (contextKey === activeSourcingContextKey) {
      showIndeedSaveStatus(
        "saved",
        `${result.saved} saved; ${result.imported} new; ${result.existing} already present`,
      );
    }
    return result;
  } catch (error) {
    if (Number(error?.status) === 401 && authConfig.enabled) {
      authSession = null;
      await writeChromeSession(AUTH_STORAGE_KEY, null);
      renderAuthState();
      await login();
    }
    if (contextKey === activeSourcingContextKey) {
      const message = Number(error?.status) === 422
        ? "The source returned incomplete profile data. Refresh the page and scan again."
        : Number(error?.status) === 404
          ? "The saved job selection is no longer available. Scan the page again."
          : friendlyActionError(error);
      showIndeedSaveStatus(
        "failed",
        Number(error?.status) === 401
          ? message
          : `Profiles could not be saved automatically. ${message}`,
      );
    }
    return null;
  }
}

async function scanIndeedCandidates(options = {}) {
  const { quiet = false, preserveSelection = false } = options;
  if (IS_EXTENSION && !privacyConsent) {
    if (!quiet) showPrivacyConsent();
    return;
  }
  if (sourcingWorkInProgress()) {
    if (!quiet) notify(
      indeedResumeBatchState.active
        ? "Resumes are still being saved."
        : "A candidate lookup is already in progress.",
    );
    return;
  }
  const scanGeneration = ++indeedScanGeneration;
  let scanContext = null;
  let previousPageUrl = activeSourcingPageUrl;
  if (IS_EXTENSION) {
    try {
      const current = await activeSourcingTab(false);
      const eligibility = sourcingPageEligibility(current.platform, current.tab.url || "");
      const nextContextKey = sourcingContextKey(current.tab, current.platform);
      const changed = nextContextKey !== activeSourcingContextKey;
      if (changed) previousPageUrl = "";
      activateSourcingContext(current.tab, current.platform, changed);
      scanContext = { tabId: current.tab.id, key: nextContextKey };
      if (!eligibility.eligible) {
        clearCapturedProfileState("unsupported");
        const guidance = current.platform.key === "linkedin"
          ? "Open a LinkedIn People search or an individual profile. This panel updates automatically."
          : "Open an individual Facebook profile. Search, Groups, Pages, and Marketplace are skipped.";
        renderSourcingStatus("No candidate profile on this page", guidance, { retry: false });
        return;
      }
    } catch (error) {
      if (quiet) return;
      clearCapturedProfileState("unsupported");
      renderSourcingStatus(
        "Ready for a candidate page",
        "Open Indeed, Vivian, ZipRecruiter, LinkedIn, Facebook, NPI No., NPI Profile, U.S. News Doctor Finder, MediFind, CommonSpirit Health, Sharecare, or NYSED. The panel will detect it automatically.",
        { retry: false },
      );
      return;
    }
  }
  $("#title").textContent = "Profiles";
  if (!quiet) {
    indeedScanState = { phase: "scanning", found: 0, total: 0 };
    indeedLookupSummary = null;
    indeedResultFilter = "all";
    renderIndeedScanning();
  }
  try {
    const previousSelection = new Set(indeedSelected);
    const previouslySelectedAll = indeedCandidates.length > 0 &&
      previousSelection.size === indeedCandidates.length;
    const result = await sendSourcingMessage({
      type: quiet
        ? "RADIXSOL_LIST_PLATFORM_CANDIDATES"
        : "RADIXSOL_SCAN_PLATFORM_CANDIDATES",
    }, false, scanContext);
    if (IS_EXTENSION && scanContext) {
      const [latestTab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const latestPlatform = latestTab?.id ? platformForUrl(latestTab.url) : null;
      if (!latestTab?.id || !latestPlatform || sourcingContextKey(latestTab, latestPlatform) !== scanContext.key) {
        scheduleActiveSourcingSync("scan-context-changed", 80);
        return;
      }
    }
    if (!result?.ok && result?.error_code === "FACEBOOK_PROFILE_LOCKED") {
      if (scanGeneration !== indeedScanGeneration || (quiet && indeedLookupInProgress)) return;
      // A Facebook SPA can replace a previously captured public profile with a
      // locked one. Clear every prior selection before rendering the skip so
      // stale candidates cannot be saved or enriched.
      clearCapturedProfileState("locked");
      activeSourcingPageUrl = result.page_url || activeSourcingPageUrl;
      renderSourcingStatus(
        "Profile unavailable",
        "This profile is locked, private, or incomplete. Open a public candidate profile to continue.",
        { retry: false },
      );
      return;
    }
    if (!result?.ok && result?.error_code === "FACEBOOK_PAGE_UNSUPPORTED") {
      if (scanGeneration !== indeedScanGeneration || (quiet && indeedLookupInProgress)) return;
      clearCapturedProfileState("unsupported");
      activeSourcingPageUrl = result.page_url || activeSourcingPageUrl;
      renderSourcingStatus(
        "This Facebook page was skipped",
        "Only public personal candidate profiles are captured. Business Pages and unrelated sections are ignored.",
        { retry: false },
      );
      return;
    }
    if (
      !result?.ok && activeSourcingPlatform.key === "facebook"
      && ["FACEBOOK_PROFILE_LOADING", "FACEBOOK_PROFILE_NAME_NOT_FOUND"].includes(result?.error_code)
    ) {
      if (scanGeneration !== indeedScanGeneration || (quiet && indeedLookupInProgress)) return;
      clearCapturedProfileState("loading");
      activeSourcingPageUrl = result.page_url || activeSourcingPageUrl;
      renderSourcingStatus(
        "Loading candidate profile",
        "The scan will start automatically when the profile name and details are ready.",
        { retry: false },
      );
      return;
    }
    if (!result?.ok) throw new Error("Profiles could not be read.");
    if (
      scanGeneration !== indeedScanGeneration ||
      scanContext?.key !== activeSourcingContextKey ||
      (quiet && indeedLookupInProgress)
    ) return;
    activeSourcingPageUrl = result.page_url || activeSourcingPageUrl;
    const quality = globalThis.RadixsolProfileQuality?.sanitizeProfiles(
      result.profiles || [],
      {
        platform: activeSourcingPlatform.key,
        pageUrl: result.page_url || activeSourcingPageUrl,
        singleProfile: Boolean(activeSourcingPlatform.singleProfile),
      },
    ) || { profiles: result.profiles || [], skippedCount: 0 };
    skippedProfileCount = Number(quality.skippedCount) || 0;
    const nextCandidates = quality.profiles.map((profile, index) => ({
      ...profile,
      _selectionKey: indeedProfileKey(profile, index),
      _sourceTabId: Number(result._sourceTabId || activeSourcingTabId),
      _sourceWindowId: Number(result._sourceWindowId || activeSourcingWindowId),
      _sourceContextKey: scanContext?.key || activeSourcingContextKey,
    }));
    const sameCapturedIdentity = (
      activeSourcingPlatform.key !== "facebook"
      || !indeedCandidates.length
      || !nextCandidates.length
      || indeedCandidates[0].source_id === nextCandidates[0].source_id
    );
    const keepSelection = preserveSelection && sameCapturedIdentity
      && (!previousPageUrl || previousPageUrl === (result.page_url || activeSourcingPageUrl));
    indeedCandidates = nextCandidates;
    if (!keepSelection) {
      indeedLookupState = new Map();
      indeedLookupScope = new Set();
      indeedLookupProfiles = [];
    }
    if (quiet) {
      indeedLookupSummary = null;
      indeedResultFilter = "all";
    }
    indeedSelected = keepSelection
      ? (previouslySelectedAll
        ? new Set(indeedCandidates.map((profile) => profile._selectionKey))
        : new Set(
        indeedCandidates
          .map((profile) => profile._selectionKey)
          .filter((key) => previousSelection.has(key)),
        ))
      : new Set(indeedCandidates.map((profile) => profile._selectionKey));
    indeedScanState = {
      phase: "captured",
      found: indeedCandidates.length,
      total: Number(result.expected_count) || indeedCandidates.length,
    };
    indeedSaveStatus = null;
    if (!indeedCandidates.length) {
      renderSourcingStatus(
        "No candidate profiles found",
        skippedProfileCount
          ? "Only irrelevant, duplicate, or incomplete items were detected and skipped."
          : "Wait for the candidate results to load, or open an individual candidate profile.",
      );
      return;
    }
    // Detection stays inside the browser. Candidate data is imported only
    // after the recruiter explicitly starts a lookup or resume capture.
    renderIndeedProfiles(result);
  } catch (error) {
    if (scanContext?.key && scanContext.key !== activeSourcingContextKey) return;
    if (quiet) {
      if (activeSourcingPlatform.key === "facebook") {
        clearCapturedProfileState("error");
        renderIndeedProfiles();
        return;
      }
      showIndeedSaveStatus("failed", `Automatic rescan failed: ${error.message}`);
      return;
    }
    indeedCandidates = [];
    indeedSelected = new Set();
    indeedScanState = { phase: "error", found: 0, total: 0 };
    renderSourcingStatus(
      "Profiles could not be read",
      "Wait for the current page to finish loading, then scan it again.",
      { tone: "error" },
    );
  }
}

async function viewIndeed() {
  await scanIndeedCandidates();
}

async function synchronizeActiveSourcingTab(reason = "changed") {
  if (!IS_EXTENSION || activeView !== "indeed") return;
  if (!privacyConsent) {
    renderSourcingStatus(
      "Consent required",
      "Review the data-use notice before Medhunt reads candidate profile information.",
      { retry: false },
    );
    return;
  }
  if (sourcingWorkInProgress()) {
    pendingSourcingContext = { reason };
    return;
  }
  let tab = null;
  try {
    [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  } catch {
    tab = null;
  }
  if (sourcingWorkInProgress()) {
    pendingSourcingContext = {
      reason,
      tab_id: Number(tab?.id) || 0,
      window_id: Number(tab?.windowId) || 0,
      url: String(tab?.url || ""),
      platform: platformForUrl(tab?.url)?.key || "",
    };
    return;
  }
  // Opening the browser's Downloads bubble can produce a duplicate focus
  // event, while opening the full Downloads page temporarily activates a
  // chrome:// or edge:// tab. Neither action changes the sourcing context and
  // neither may clear results or start another scan.
  if (isBrowserDownloadSurface(tab?.url || "")) return;
  const platform = tab?.id ? platformForUrl(tab.url) : null;
  if (platform?.key === "indeed" && isProtectedIndeedResumeNavigation(tab?.id, tab?.url)) return;
  if (!tab?.id || !platform) {
    const nextKey = sourcingContextKey(tab, null);
    if (nextKey !== activeSourcingContextKey) {
      indeedScanGeneration += 1;
      activeSourcingContextKey = nextKey;
      activeSourcingTabId = Number(tab?.id) || 0;
      activeSourcingWindowId = Number(tab?.windowId) || 0;
      activeSourcingPageUrl = String(tab?.url || "");
      activePageIndicatorLabel = "Candidate page";
      clearCapturedProfileState("unsupported");
    }
    renderSourcingStatus(
      "Ready for a candidate page",
      "Open Indeed, Vivian, ZipRecruiter, LinkedIn, Facebook, NPI No., NPI Profile, U.S. News Doctor Finder, MediFind, CommonSpirit Health, Sharecare, or NYSED. The panel will update automatically.",
      { retry: false },
    );
    return;
  }

  // Focus changes and closing a browser-internal Downloads tab are passive.
  // Chrome reports the now-active source tab as "removed" and Indeed may have
  // added a transient candidate/drawer parameter while creating the PDF. Keep
  // the completed workbench intact even when the download remains unfinished.
  if (isPassiveSourcingContextEvent(reason, tab, platform)) return;

  const eligibility = sourcingPageEligibility(platform, tab.url || "");
  const nextKey = sourcingContextKey(tab, platform);
  const changed = nextKey !== activeSourcingContextKey;
  if (changed) {
    indeedScanGeneration += 1;
    activateSourcingContext(tab, platform, true);
    closeModal();
  }
  if (!eligibility.eligible) {
    clearCapturedProfileState("unsupported");
    const guidance = platform.key === "linkedin"
      ? "Open a LinkedIn People search or an individual profile. This panel updates automatically."
      : "Open an individual Facebook profile. Search, Groups, Pages, and Marketplace are skipped.";
    renderSourcingStatus("No candidate profile on this page", guidance, { retry: false });
    return;
  }
  if (tab.status && tab.status !== "complete") {
    clearCapturedProfileState("loading");
    renderSourcingStatus("Loading candidate page", "The scan will start automatically when the page is ready.", { retry: false });
    return;
  }
  await scanIndeedCandidates({ quiet: false, preserveSelection: !changed });
}

function scheduleActiveSourcingSync(reason = "changed", delay = 180) {
  clearTimeout(sourcingContextTimer);
  sourcingContextTimer = setTimeout(() => {
    sourcingContextTimer = null;
    synchronizeActiveSourcingTab(reason).catch(() => {
      renderSourcingStatus(
        "Candidate page unavailable",
        "Wait for the current page to finish loading, then scan it again.",
        { tone: "error" },
      );
    });
  }, delay);
}

if (IS_EXTENSION) {
  chrome.runtime.onMessage.addListener((message, sender) => {
    if (message?.type === "RADIXSOL_ACTIVE_TAB_CHANGED") {
      if (sourcingWorkInProgress()) pendingSourcingContext = message;
      else if (
        message.platform === "indeed" &&
        isProtectedIndeedResumeNavigation(message.tab_id, message.url)
      ) return false;
      else scheduleActiveSourcingSync(message.reason || "changed", message.status === "complete" ? 120 : 240);
      return false;
    }
    if (message?.type === "RADIXSOL_RESUME_DOWNLOADED") {
      handleDownloadedResume(message);
      return false;
    }
    if (message?.type === "RADIXSOL_LINKEDIN_PDF_CAPTURE_STARTED") {
      const profile = indeedCandidates.find(
        (candidate) => Number(candidate._candidateId) === Number(message.candidateId),
      );
      if (profile) {
        const current = indeedLookupFor(profile);
        indeedLookupState.set(profile._selectionKey, {
          ...current,
          resume_status: "downloading",
          resume_error: "",
        });
        if (activeView === "indeed") renderIndeedProfiles();
      }
      return false;
    }
    if (message?.type === "RADIXSOL_LINKEDIN_PDF_CAPTURE_FAILED") {
      const profile = indeedCandidates.find(
        (candidate) => Number(candidate._candidateId) === Number(message.candidateId),
      );
      if (profile) {
        clearTimeout(linkedinPdfCaptureTimers.get(Number(profile._candidateId)));
        linkedinPdfCaptureTimers.delete(Number(profile._candidateId));
        const current = indeedLookupFor(profile);
        indeedLookupState.set(profile._selectionKey, {
          ...current,
          resume_status: "failed",
          resume_error: "Resume unavailable",
        });
        if (activeView === "indeed") renderIndeedProfiles();
      }
      closeModal();
      notify("Resume unavailable.", "error");
      return false;
    }
    if (activeView !== "indeed") {
      return false;
    }
    if (
      ["RADIXSOL_PLATFORM_SCAN_PROGRESS", "RADIXSOL_PLATFORM_RESULTS_CHANGED"].includes(message?.type) &&
      sender?.tab?.id && (
        sender.tab.active === false || Number(sender.tab.id) !== Number(activeSourcingTabId)
      )
    ) return false;
    if (
      indeedResumeBatchState.active || (
        message?.platform === "indeed" &&
        isProtectedIndeedResumeNavigation(
          sender?.tab?.id || activeSourcingTabId,
          message.page_url || sender?.tab?.url || "",
        )
      )
    ) return false;
    if (message?.type === "RADIXSOL_PLATFORM_SCAN_PROGRESS") {
      if (message.platform && message.platform !== activeSourcingPlatform?.key) return false;
      indeedScanState = {
        phase: "scanning",
        found: Number(message.found) || 0,
        total: Number(message.total) || 0,
      };
      if (!$(".scan-view")) renderIndeedScanning();
      updateSourceHeaderProgressUi();
      // Some directory pages finish their progressive scan before the side
      // panel receives the final response. Refresh from the page snapshot as
      // soon as the reported total is reached so results do not require a
      // panel close/reopen cycle.
      if (
        ["npino", "npiprofile", "nysed", "usnews", "medifind", "commonspirit", "sharecare", "webmd"].includes(message.platform)
        && Number(message.total) > 0
        && Number(message.found) >= Number(message.total)
        && !indeedLookupInProgress
      ) {
        clearTimeout(indeedProgressRefreshTimer);
        indeedProgressRefreshTimer = setTimeout(() => {
          indeedProgressRefreshTimer = null;
          scanIndeedCandidates({ quiet: true, preserveSelection: true });
        }, 250);
      }
      return false;
    }
    if (message?.type !== "RADIXSOL_PLATFORM_RESULTS_CHANGED") return false;
    if (message.platform && message.platform !== activeSourcingPlatform?.key) return false;
    const facebookIdentityChanged = message.platform === "facebook" && message.identity_changed === true;
    if (facebookIdentityChanged) {
      indeedScanGeneration += 1;
      clearCapturedProfileState("scanning");
      activeSourcingPageUrl = message.page_url || "";
      if (activeView === "indeed") renderIndeedScanning();
    }
    // Opening a candidate from a LinkedIn People batch navigates the same tab
    // to /in/. Keep the captured batch in the side panel so the recruiter can
    // return to it, enrich it, and use the exact-profile PDF action. A manual
    // refresh still replaces it with the currently open individual profile.
    if (
      message.platform === "linkedin" &&
      isLinkedinPeopleSearchUrl(activeSourcingPageUrl) &&
      (message.result_page === false || message.view === "profile")
    ) return false;
    if (
      !facebookIdentityChanged
      && (indeedLookupInProgress || ["scanning", "lookup", "results"].includes(indeedScanState.phase))
    ) return false;
    if ($("#modalRoot")?.childElementCount) return false;
    clearTimeout(indeedAutoScanTimer);
    indeedAutoScanTimer = setTimeout(() => {
      scanIndeedCandidates({ quiet: true, preserveSelection: true });
    }, 800);
    return false;
  });
}

async function openIndeedResult(index) {
  if (indeedResumeBatchState.active) {
    throw new Error("Wait for the current resume to finish saving.");
  }
  const profile = indeedCandidates[index];
  if (!profile) throw new Error("That displayed candidate is no longer available.");
  const result = await sendSourcingMessage({
    type: "RADIXSOL_OPEN_PLATFORM_CANDIDATE",
    index: profile.result_index ?? index,
  });
  if (!result?.ok) throw new Error(result?.error || `${activeSourcingPlatform.label} could not open that candidate.`);
  if (profile._candidateId && activeSourcingPlatform.resumeCapture) {
    await sendExtensionMessage({
      type: "RADIXSOL_SET_ACTIVE_CANDIDATE",
      candidateId: profile._candidateId,
      name: profile.name,
      sourceId: profile.source_id || "",
    });
  }
  notify(`Opened ${profile.name}.`);
}

function linkedinSlug(value) {
  try {
    return decodeURIComponent(new URL(value).pathname.match(/^\/in\/([^/?#]+)/i)?.[1] || "").toLowerCase();
  } catch {
    return "";
  }
}

function isLinkedinPeopleSearchUrl(value) {
  try {
    const url = new URL(value);
    return (url.hostname === "linkedin.com" || url.hostname.endsWith(".linkedin.com")) &&
      /^\/search\/results\/(?:people|all)(?:\/|$)/i.test(url.pathname);
  } catch {
    return false;
  }
}

async function captureLinkedinPdf(index) {
  const profile = indeedCandidates[index];
  if (!profile || activeSourcingPlatform.key !== "linkedin") {
    throw new Error("Open the captured profile first.");
  }
  if (!hasCompleteIndeedContact(indeedLookupFor(profile))) {
    throw new Error("Find a phone number or email before saving a resume.");
  }
  if (!profile._candidateId) await saveDisplayedIndeedCandidates(activeSourcingPageUrl, [profile]);
  if (!profile._candidateId) throw new Error("The profile is not ready for resume capture.");

  const { tab, platform } = await activeSourcingTab(false);
  if (platform.key !== "linkedin" || linkedinSlug(tab.url) !== String(profile.source_id || "").toLowerCase()) {
    throw new Error(`Open ${profile.name}'s exact profile before starting resume capture.`);
  }

  const armed = await sendExtensionMessage({
    type: "RADIXSOL_ARM_LINKEDIN_PDF_CAPTURE",
    tabId: tab.id,
    candidateId: profile._candidateId,
    name: profile.name,
    sourceUrl: profile.source_url,
  });
  if (!armed?.ok) throw new Error("Resume capture could not start.");

  let guide = null;
  const guideRequest = {
    type: SOURCING_PLATFORMS.linkedin.adapterRequestType,
    original_type: "RADIXSOL_AUTO_LINKEDIN_PDF",
  };
  try {
    guide = await sendTabMessage(tab.id, guideRequest);
    if (guide?.adapter_revision !== SOURCING_PLATFORMS.linkedin.adapterRevision) {
      throw new Error("The LinkedIn page adapter needs to be refreshed.");
    }
  } catch {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["linkedin-content.js"] });
    guide = await sendTabMessage(tab.id, guideRequest);
  }
  if (!guide?.ok) {
    await sendExtensionMessage({ type: "RADIXSOL_DISARM_LINKEDIN_PDF_CAPTURE" }).catch(() => {});
    throw new Error(guide?.error || "LinkedIn did not make Save to PDF available.");
  }
  await chrome.tabs.update(tab.id, { active: true });

  const current = indeedLookupFor(profile);
  indeedLookupState.set(profile._selectionKey, {
    ...current,
    resume_status: "armed",
    resume_error: "",
  });
  const candidateId = Number(profile._candidateId);
  clearTimeout(linkedinPdfCaptureTimers.get(candidateId));
  linkedinPdfCaptureTimers.set(candidateId, setTimeout(async () => {
    await sendExtensionMessage({ type: "RADIXSOL_DISARM_LINKEDIN_PDF_CAPTURE" }).catch(() => {});
    const latest = indeedLookupFor(profile);
    if (["armed", "downloading"].includes(latest.resume_status)) {
      indeedLookupState.set(profile._selectionKey, {
        ...latest,
        resume_status: "failed",
        resume_error: "Resume unavailable",
      });
      if (activeView === "indeed") renderIndeedProfiles();
      closeModal();
      notify("Resume capture expired.", "error");
    }
    linkedinPdfCaptureTimers.delete(candidateId);
  }, 5 * 60 * 1000));
  renderIndeedProfiles();
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <section class="sheet linkedin-pdf-guide" role="dialog" aria-modal="true" aria-labelledby="linkedinPdfTitle">
      <h3 id="linkedinPdfTitle">Save ${escapeHtml(profile.name)}'s resume</h3>
      <div class="notice">Resume capture is ready.</div>
      <ol>
        <li>Complete the highlighted PDF action in the open profile.</li>
        <li>Keep this panel open until the resume is ready.</li>
      </ol>
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="close-modal">Keep waiting</button>
        <button type="button" class="btn" data-action="cancel-linkedin-pdf" data-index="${index}">Cancel capture</button>
      </div>
    </section>
  </div>`;
  notify("Resume capture ready.");
}

async function cancelLinkedinPdf(index) {
  await sendExtensionMessage({ type: "RADIXSOL_DISARM_LINKEDIN_PDF_CAPTURE" });
  const profile = indeedCandidates[index];
  if (profile) {
    clearTimeout(linkedinPdfCaptureTimers.get(Number(profile._candidateId)));
    linkedinPdfCaptureTimers.delete(Number(profile._candidateId));
    const current = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      ...current,
      resume_status: "",
      resume_error: "",
    });
  }
  closeModal();
  renderIndeedProfiles();
  notify("Resume capture cancelled.");
}

function selectedIndeedProfiles() {
  return indeedCandidates.filter((profile) => indeedSelected.has(profile._selectionKey));
}

function updateIndeedLookupProgressUi(profile = null) {
  updateSourceHeaderProgressUi();
  if (profile) {
    const row = Array.from(document.querySelectorAll("[data-profile-key]"))
      .find((element) => element.dataset.profileKey === profile._selectionKey);
    const result = row?.querySelector(".capture-result");
    if (result) result.innerHTML = indeedResultStatus(profile);
  }
}

async function lookupSelectedIndeedCandidates() {
  if (indeedResumeBatchState.active) {
    throw new Error("Wait for the current resumes to finish saving.");
  }
  if (indeedLookupInProgress) throw new Error("A candidate lookup is already in progress.");
  const profiles = selectedIndeedProfiles();
  if (!profiles.length) throw new Error(`Select at least one ${activeSourcingPlatform.label} profile.`);
  indeedLookupProfiles = profiles.slice();
  clearTimeout(indeedAutoScanTimer);
  indeedAutoScanTimer = null;
  indeedLookupInProgress = true;
  // Invalidate any quiet scan that was already awaiting a content-script
  // response before the recruiter pressed Lookup.
  indeedScanGeneration += 1;
  try {
    if (!backendHealth) await refreshHealth();
    if (!backendHealth) throw new Error("The local service is unavailable.");
    const lookupPrompt = sequentialLookupMode()
      ? `Look up ${profiles.length} selected candidate${profiles.length === 1 ? "" : "s"}?\n\n` +
        `Each one is searched live against public records, about a minute per ` +
        `candidate, so this should take around ${lookupDurationEstimate(profiles.length)}. ` +
        `Results appear row by row and already-searched candidates are kept if you stop.`
      : `Look up ${profiles.length} selected candidate${profiles.length === 1 ? "" : "s"}?`;
    if (!confirm(lookupPrompt)) {
      return;
    }

  indeedScanState.phase = "lookup";
  indeedLookupSummary = {
    total: profiles.length,
    processed: 0,
    matched: 0,
    no_match: 0,
    errors: 0,
  };
  indeedLookupScope = new Set(profiles.map((profile) => profile._selectionKey));
  const resumeQueue = [];
  const linkedinResumeQueue = [];
  const professionalProfileResumeQueue = [];
  const lookupRunId = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID().replaceAll("-", "")
    : `lookup_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  for (const profile of profiles) {
    // Hide prior snapshots while the server checks the current record.
    const previous = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      status: "looking_up",
      emails: [],
      phones: [],
      resume_required: false,
      resume: previous.resume || null,
      resume_status: previous.resume_status || "",
      resume_error: previous.resume_error || "",
    });
  }
  renderIndeedProfiles();

  if (profiles.some((profile) => !profile._candidateId)) {
    const saved = await saveDisplayedIndeedCandidates(activeSourcingPageUrl, profiles);
    if (!saved && profiles.some((profile) => !profile._candidateId)) {
      if (authConfig.enabled && !authSession?.extension_token) return;
      throw new Error(
        `${activeSourcingPlatform.label} profiles could not be saved. ` +
        "Wait for the page to finish loading, scan again, and retry.",
      );
    }
  }

  const lookupTargets = [];
  for (const profile of profiles) {
    const existing = indeedLookupFor(profile);
    if (!profile._candidateId) {
      indeedLookupState.set(profile._selectionKey, {
        status: "failed",
        emails: [],
        phones: [],
        resume_required: false,
      });
      indeedLookupSummary.errors += 1;
      indeedLookupSummary.processed += 1;
      updateIndeedLookupProgressUi(profile);
      continue;
    }
    lookupTargets.push(profile);
  }

  function applyLookupResult(profile, lookup) {
    const previous = indeedLookupFor(profile);
    const status = ["found", "not_found", "failed"].includes(lookup?.status)
      ? lookup.status
      : "failed";
    const result = {
      status,
      emails: status === "found" && Array.isArray(lookup?.emails) ? lookup.emails : [],
      phones: status === "found" && Array.isArray(lookup?.phones) ? lookup.phones : [],
      phone_contacts: status === "found" && Array.isArray(lookup?.phone_contacts)
        ? lookup.phone_contacts
            .filter((item) => item && typeof item.value === "string" && ["mobile", "other"].includes(item.kind))
            .map((item) => ({ value: item.value, kind: item.kind }))
        : [],
      resume_required: status === "found" && lookup?.resume_required === true,
      location_match: status === "found" &&
        lookup?.location_match?.type === "hometown" &&
        typeof lookup.location_match.value === "string" &&
        lookup.location_match.value.trim()
        ? { type: "hometown", value: lookup.location_match.value.trim().slice(0, 160) }
        : null,
      resume: previous.resume || null,
      resume_status: previous.resume_status || "",
      resume_error: previous.resume_error || "",
    };
    indeedLookupState.set(profile._selectionKey, result);
    if (profile.source === "indeed" && hasCompleteIndeedContact(result) && !result.resume) {
      resumeQueue.push(profile);
    }
    if (
      profile.source === "linkedin" && activeSourcingPlatform.automaticPdfCapture &&
      hasCompleteIndeedContact(result) && !result.resume
    ) {
      linkedinResumeQueue.push(profile);
    }
    if (
      PROFESSIONAL_PROFILE_SOURCES.has(profile.source)
      && hasCompleteIndeedContact(result)
      && (!result.resume || !profile._detailProfileCaptured)
    ) {
      professionalProfileResumeQueue.push(profile);
    }
    if (isIndeedMatch(result)) indeedLookupSummary.matched += 1;
    else if (result.status === "failed") indeedLookupSummary.errors += 1;
    else indeedLookupSummary.no_match += 1;
    indeedLookupSummary.processed += 1;
    updateIndeedLookupProgressUi(profile);
  }

  const publicRecordLookup = sequentialLookupMode();
  const chunkSize = publicRecordLookup ? RECORD_LOOKUP_BATCH_SIZE : CONTACT_BATCH_SIZE;
  const chunkTimeout = publicRecordLookup
    ? RECORD_LOOKUP_TIMEOUT
    : CONTACT_BATCH_TIMEOUT;
  for (let start = 0; start < lookupTargets.length; start += chunkSize) {
    const chunk = lookupTargets.slice(start, start + chunkSize);
    let payload;
    try {
      payload = await api("/contact-lookup/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          candidate_ids: chunk.map((profile) => profile._candidateId),
          run_id: lookupRunId,
          confirmed: true,
        }),
        timeout: chunkTimeout,
      });
      if (payload?.credits_remaining != null) {
        extensionCredits = { enabled: true, balance: Math.max(0, Number(payload.credits_remaining)) };
        updateSourceCreditBalance();
      }
    } catch (error) {
      if (Number(error?.status) === 402) {
        for (const profile of lookupTargets.slice(start)) {
          applyLookupResult(profile, { status: "failed" });
        }
        indeedScanState.phase = "results";
        renderIndeedProfiles();
        throw error;
      }
      for (const profile of chunk) {
        applyLookupResult(profile, {
          status: "failed",
        });
      }
      continue;
    }

    const byCandidate = payload?.results || {};
    for (const profile of chunk) {
      applyLookupResult(profile, byCandidate[String(profile._candidateId)] || {
        status: "failed",
      });
    }
  }

  indeedScanState.phase = "results";
  indeedResultFilter = "all";
  renderIndeedProfiles();
  notify(
    `${indeedLookupSummary.matched} found; ${indeedLookupSummary.no_match} not found` +
    `${indeedLookupSummary.errors ? `; ${indeedLookupSummary.errors} could not be checked` : ""}`,
    indeedLookupSummary.errors ? "error" : "",
  );
    if (professionalProfileResumeQueue.length) {
      await startProfessionalProfileResumeBatch(professionalProfileResumeQueue);
    } else if (resumeQueue.length) {
      void startIndeedResumeBatch(resumeQueue);
    } else if (linkedinResumeQueue.length) {
      void startLinkedinResumeBatch(linkedinResumeQueue);
    }
  } finally {
    indeedLookupInProgress = false;
    if (pendingSourcingContext && !indeedResumeBatchState.active) {
      const pending = pendingSourcingContext;
      pendingSourcingContext = null;
      scheduleActiveSourcingSync(pending.reason || "lookup-finished", 100);
    }
  }
}

function toggleAllIndeedCandidates() {
  indeedSelected = indeedSelected.size === indeedCandidates.length
    ? new Set()
    : new Set(indeedCandidates.map((profile) => profile._selectionKey));
  document.querySelectorAll(".indeed-select").forEach((checkbox) => {
    checkbox.checked = indeedSelected.has(checkbox.dataset.key);
  });
  updateIndeedSelectionUi();
}

function filterIndeedResults(filter) {
  indeedResultFilter = ["all", "matched", "no_match", "failed"].includes(filter)
    ? filter
    : "all";
  renderIndeedProfiles();
  queueMicrotask(() => {
    document.querySelector(`[data-action="filter-indeed-results"][data-filter="${indeedResultFilter}"]`)?.focus();
  });
}

async function retryFailedLookups() {
  if (indeedResumeBatchState.active) {
    throw new Error("Wait for the current resumes to finish saving.");
  }
  const failedProfiles = indeedLookupProfiles.filter(
    (profile) => indeedLookupFor(profile)?.status === "failed",
  );
  if (!failedProfiles.length) {
    notify("There are no failed lookups to retry.");
    return;
  }
  indeedCandidates = failedProfiles;
  indeedSelected = new Set(failedProfiles.map((profile) => profile._selectionKey));
  indeedScanState.phase = "captured";
  indeedLookupSummary = null;
  indeedResultFilter = "all";
  renderIndeedProfiles();
  await lookupSelectedIndeedCandidates();
}

async function attachDownloadedResume(message) {
  const candidateId = Number(message.candidateId);
  if (!candidateId || !message.path) throw new Error("The completed resume download could not be identified.");
  const attached = await api(`/candidates/${candidateId}/resume/from-download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      path: message.path,
      filename: message.filename || "",
    }),
    timeout: 60000,
  });
  const profile = indeedCandidates.find(
    (candidate) => Number(candidate._candidateId) === candidateId,
  );
  if (profile) {
    clearTimeout(linkedinPdfCaptureTimers.get(candidateId));
    linkedinPdfCaptureTimers.delete(candidateId);
    const current = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      ...current,
      resume: attached.resume,
      resume_status: "stored",
      resume_error: "",
    });
    if (activeView === "indeed" && ["captured", "results"].includes(indeedScanState.phase)) {
      renderIndeedProfiles();
    }
  }
  if (message.platform === "linkedin") closeModal();
  notify("Resume ready.");
  return attached.resume;
}

async function handleDownloadedResume(message) {
  const eventId = String(message?.event_id || "");
  if (eventId && processingResumeEvents.has(eventId)) return;
  if (eventId) processingResumeEvents.add(eventId);
  const candidateId = Number(message.candidateId);
  const waiter = resumeDownloadWaiters.get(candidateId);
  try {
    const resume = await attachDownloadedResume(message);
    waiter?.resolve(resume);
    if (eventId) {
      await sendExtensionMessage({
        type: "RADIXSOL_ACK_RESUME_EVENT",
        event_id: eventId,
      }).catch(() => {});
    }
  } catch (error) {
    waiter?.reject(error);
    if (message.platform === "linkedin") {
      const profile = indeedCandidates.find(
        (candidate) => Number(candidate._candidateId) === candidateId,
      );
      clearTimeout(linkedinPdfCaptureTimers.get(candidateId));
      linkedinPdfCaptureTimers.delete(candidateId);
      if (profile) {
        const current = indeedLookupFor(profile);
        indeedLookupState.set(profile._selectionKey, {
          ...current,
          resume_status: "failed",
          resume_error: "Resume unavailable",
        });
        if (activeView === "indeed") renderIndeedProfiles();
      }
      closeModal();
    }
    notify("Resume unavailable.", "error");
  } finally {
    if (eventId) processingResumeEvents.delete(eventId);
  }
}

async function processPendingResumeEvents() {
  if (!IS_EXTENSION || !backendHealth) return;
  const pending = await sendExtensionMessage({
    type: "RADIXSOL_GET_PENDING_RESUME_EVENTS",
  }).catch(() => ({ events: [] }));
  for (const event of pending?.events || []) await handleDownloadedResume(event);
}

async function saveStoredResumeDownload(profile, candidateId, resume) {
  if (!hasCompleteIndeedContact(indeedLookupFor(profile))) return null;
  const safeName = String(profile.name || "candidate")
    .replace(/[^a-z0-9 _-]/gi, "_")
    .trim()
    .replace(/\s+/g, "_") || "candidate";
  const blob = await fetchStoredResumeBlob(candidateId, resume.id);
  const blobUrl = URL.createObjectURL(blob);
  try {
    const downloadId = await chrome.downloads.download({
      url: blobUrl,
      filename: `MedhuntResumes/${safeName}_resume.pdf`,
      saveAs: false,
    });
    releaseResumeBlobUrlLater(blobUrl);
    return downloadId;
  } catch (error) {
    URL.revokeObjectURL(blobUrl);
    throw error;
  }
}

async function recoverStoredResume(candidateId, uploadStartedAt, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const candidate = await api(`/candidates/${Number(candidateId)}`, { timeout: 15000 });
      const resume = (candidate.resumes || []).find(
        (item) => Number(item.created || 0) >= uploadStartedAt - 5,
      );
      if (resume) {
        return {
          ...resume,
          contact_sheet_embedded: /enriched\.pdf$/i.test(resume.filename || ""),
          recovered_after_timeout: true,
        };
      }
    } catch {
      // The original request can still be finishing in the backend. Keep
      // polling until its resume metadata is committed.
    }
    await wait(2500);
  }
  return null;
}

async function downloadMatchedLinkedinPdf(profile, sourceTabId) {
  const candidateId = Number(profile?._candidateId);
  const current = indeedLookupFor(profile);
  if (
    profile?.source !== "linkedin" || !candidateId || current.resume ||
    !hasCompleteIndeedContact(current) || !linkedinSlug(profile.source_url || "")
  ) return false;
  if (await hydrateStoredResume(profile)) return true;

  indeedLookupState.set(profile._selectionKey, {
    ...current,
    resume_status: "downloading",
    resume_error: "",
  });
  updateIndeedLookupProgressUi(profile);

  let completion = null;
  try {
    await chrome.tabs.update(Number(sourceTabId), {
      active: true,
      url: profile.source_url,
    });
    await waitForLinkedinProfile(sourceTabId, profile.source_url);
    await wait(650);

    const armed = await sendExtensionMessage({
      type: "RADIXSOL_ARM_LINKEDIN_PDF_CAPTURE",
      tabId: Number(sourceTabId),
      candidateId,
      name: profile.name,
      sourceUrl: profile.source_url,
    });
    if (!armed?.ok) throw new Error(armed?.error || "LinkedIn PDF capture could not start.");

    completion = linkedinResumeCompletion(candidateId);
    const started = await sendLinkedinPdfMessage(sourceTabId, "RADIXSOL_AUTO_LINKEDIN_PDF");
    if (!started?.ok) throw new Error(started?.error || "LinkedIn did not make Save to PDF available.");
    const resume = await completion.promise;
    await saveStoredResumeDownload(profile, candidateId, resume).catch(() => {});
    return true;
  } catch (error) {
    completion?.cancel(error);
    await sendExtensionMessage({ type: "RADIXSOL_DISARM_LINKEDIN_PDF_CAPTURE" }).catch(() => {});
    const latest = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      ...latest,
      resume_status: "failed",
      resume_error: "Resume unavailable",
    });
    updateIndeedLookupProgressUi(profile);
    return false;
  }
}

function startLinkedinResumeBatch(profiles) {
  const queue = (profiles || []).filter((profile) => (
    profile?.source === "linkedin" && Number(profile._candidateId) && linkedinSlug(profile.source_url || "")
  ));
  if (!queue.length || indeedResumeBatchState.active) return Promise.resolve();

  const first = queue[0];
  indeedResumeBatchState = {
    active: true,
    total: queue.length,
    processed: 0,
    saved: 0,
    failed: 0,
    sourceTabId: Number(first._sourceTabId || activeSourcingTabId),
    sourceWindowId: Number(first._sourceWindowId || activeSourcingWindowId),
    sourceContextKey: String(first._sourceContextKey || activeSourcingContextKey),
    sourcePageUrl: String(activeSourcingPageUrl || first.source_url || ""),
    platform: "linkedin",
  };
  updateSourceHeaderProgressUi();

  return (async () => {
    try {
      for (const profile of queue) {
        let sourceTab = null;
        try { sourceTab = await chrome.tabs.get(Number(indeedResumeBatchState.sourceTabId)); } catch { sourceTab = null; }
        if (!sourceTab?.id) {
          const remaining = queue.length - indeedResumeBatchState.processed;
          indeedResumeBatchState.processed += remaining;
          indeedResumeBatchState.failed += remaining;
          break;
        }
        const saved = await downloadMatchedLinkedinPdf(profile, sourceTab.id);
        indeedResumeBatchState.processed += 1;
        if (saved) indeedResumeBatchState.saved += 1;
        else indeedResumeBatchState.failed += 1;
        updateSourceHeaderProgressUi();
      }
    } finally {
      const completed = { ...indeedResumeBatchState };
      indeedResumeBatchState = { ...indeedResumeBatchState, active: false };
      updateSourceHeaderProgressUi();

      let sourceTab = null;
      try { sourceTab = await chrome.tabs.get(Number(completed.sourceTabId)); } catch { sourceTab = null; }
      if (
        sourceTab?.id && isLinkedinPeopleSearchUrl(completed.sourcePageUrl) &&
        sourceTab.url !== completed.sourcePageUrl
      ) {
        await chrome.tabs.update(sourceTab.id, { url: completed.sourcePageUrl, active: true }).catch(() => {});
      }

      if (completed.saved) {
        notify(
          `${completed.saved} LinkedIn profile PDF${completed.saved === 1 ? "" : "s"} saved` +
          `${completed.failed ? `; ${completed.failed} unavailable` : ""}.`,
          completed.failed ? "error" : "",
        );
      } else if (completed.failed) {
        notify("LinkedIn profile PDFs were unavailable.", "error");
      }

      const pending = pendingSourcingContext;
      pendingSourcingContext = null;
      scheduleActiveSourcingSync(pending?.reason || "linkedin-resume-finished", 300);
    }
  })();
}

function startIndeedResumeBatch(profiles) {
  const queue = (profiles || []).filter((profile) => (
    profile?.source === "indeed" && Number(profile._candidateId)
  ));
  if (!queue.length || indeedResumeBatchState.active) return Promise.resolve();

  const first = queue[0];
  indeedResumeBatchState = {
    active: true,
    total: queue.length,
    processed: 0,
    saved: 0,
    failed: 0,
    sourceTabId: Number(first._sourceTabId || activeSourcingTabId),
    sourceWindowId: Number(first._sourceWindowId || activeSourcingWindowId),
    sourceContextKey: String(first._sourceContextKey || activeSourcingContextKey),
    sourcePageUrl: String(activeSourcingPageUrl || first.source_url || ""),
  };
  indeedResumeNavigationGrace = { sourceTabId: 0, sourcePageUrl: "", until: 0 };
  updateSourceHeaderProgressUi();

  return (async () => {
    try {
      for (let index = 0; index < queue.length; index += 1) {
        const profile = queue[index];
        let sourceTab = null;
        try {
          sourceTab = await chrome.tabs.get(Number(indeedResumeBatchState.sourceTabId));
        } catch {
          sourceTab = null;
        }
        const sourceIsCurrent = (
          platformForUrl(sourceTab?.url)?.key === "indeed" &&
          sameIndeedSearchContext(sourceTab?.url, indeedResumeBatchState.sourcePageUrl)
        );
        if (!sourceIsCurrent) {
          const remaining = queue.length - index;
          for (const skipped of queue.slice(index)) {
            const current = indeedLookupFor(skipped);
            indeedLookupState.set(skipped._selectionKey, {
              ...current,
              resume_status: "failed",
              resume_error: "Resume unavailable",
            });
            updateIndeedLookupProgressUi(skipped);
          }
          indeedResumeBatchState.processed += remaining;
          indeedResumeBatchState.failed += remaining;
          updateSourceHeaderProgressUi();
          break;
        }
        // Keep capture, server-side cover creation, and local save in this
        // awaited loop: the next candidate must not start until this one ends.
        const saved = await downloadMatchedIndeedResume(profile);
        indeedResumeBatchState.processed += 1;
        if (saved) indeedResumeBatchState.saved += 1;
        else indeedResumeBatchState.failed += 1;
        updateSourceHeaderProgressUi();
      }
    } finally {
      const completed = { ...indeedResumeBatchState };
      indeedResumeBatchState = {
        ...indeedResumeBatchState,
        active: false,
      };
      // Indeed can emit delayed URL/load events after its download menu has
      // completed. Keep those same-tab automation events from replacing the
      // stable results list with a temporary zero-card scan.
      indeedResumeNavigationGrace = {
        sourceTabId: Number(completed.sourceTabId),
        sourcePageUrl: String(completed.sourcePageUrl || ""),
        until: Date.now() + 2500,
      };
      updateSourceHeaderProgressUi();

      if (completed.saved) {
        notify(
          `${completed.saved} resume${completed.saved === 1 ? "" : "s"} saved` +
          `${completed.failed ? `; ${completed.failed} unavailable` : ""}.`,
          completed.failed ? "error" : "",
        );
      } else if (completed.failed) {
        notify("Resumes could not be saved.", "error");
      }

      let currentTab = null;
      try {
        [currentTab] = await chrome.tabs.query({ active: true, currentWindow: true });
      } catch {
        currentTab = null;
      }
      const stillOnResumeSource = (
        Number(currentTab?.id) === Number(completed.sourceTabId) &&
        platformForUrl(currentTab?.url)?.key === "indeed" &&
        sameIndeedSearchContext(currentTab?.url, completed.sourcePageUrl)
      );
      const pending = pendingSourcingContext;
      pendingSourcingContext = null;
      if (!stillOnResumeSource && (pending || currentTab)) {
        scheduleActiveSourcingSync(pending?.reason || "resume-finished", 100);
      }
    }
  })();
}

async function downloadMatchedIndeedResume(profile) {
  if (profile?.source !== "indeed") return false;
  const candidateId = Number(profile._candidateId);
  const current = indeedLookupFor(profile);
  if (!candidateId || current.resume || !hasCompleteIndeedContact(current)) return false;
  if (await hydrateStoredResume(profile)) return true;

  indeedLookupState.set(profile._selectionKey, {
    ...current,
    resume_status: "downloading",
    resume_error: "",
  });
  updateIndeedLookupProgressUi(profile);

  try {
    // Automatic capture stores the bytes returned by the proven MAIN-world
    // hook. Clear the older file-path tracker so a normal Indeed download does
    // not create a duplicate resume record in parallel.
    await sendExtensionMessage({ type: "RADIXSOL_CLEAR_ACTIVE_CANDIDATE" });

    const captured = await sendIndeedResumeMessage({
      type: "RADIXSOL_DOWNLOAD_INDEED_RESUME",
      index: profile.result_index,
      expectedName: profile.name,
    }, profile._sourceTabId);
    if (!captured?.ok) {
      const diagnostics = captured?.diagnostics?.length
        ? ` (${captured.diagnostics.join(", ")})`
        : "";
      throw new Error((captured?.error || "Indeed resume download could not be captured.") + diagnostics);
    }
    if (!captured.base64) throw new Error("Indeed returned no captured resume bytes.");

    const filename = `${profile.name || "candidate"} - resume.pdf`;
    const uploadStartedAt = Date.now() / 1000;
    let attached;
    try {
      attached = await api(`/candidates/${candidateId}/resume/from-browser`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content_base64: captured.base64,
          filename,
        }),
        timeout: 300000,
      });
    } catch (error) {
      if (error.message !== "The backend request timed out.") throw error;
      const recovered = await recoverStoredResume(candidateId, uploadStartedAt);
      if (!recovered) throw error;
      attached = { attached: true, resume: recovered, recovered_after_timeout: true };
    }
    if (!attached?.attached || !Number(attached?.resume?.id)) {
      throw new Error("The stored resume response was incomplete.");
    }

    const latest = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      ...latest,
      resume: attached.resume,
      resume_status: "stored",
      resume_error: "",
    });
    updateIndeedLookupProgressUi(profile);
    // The central copy is authoritative. A blocked local browser download must
    // not turn an already stored resume into a false failure state.
    await saveStoredResumeDownload(profile, candidateId, attached.resume).catch(() => {});
    return true;
  } catch (error) {
    const latest = indeedLookupFor(profile);
    indeedLookupState.set(profile._selectionKey, {
      ...latest,
      resume_status: "failed",
      resume_error: "Resume unavailable",
    });
    updateIndeedLookupProgressUi(profile);
    return false;
  }
}

async function openStoredResume(candidateId, resumeId) {
  if (indeedResumeBatchState.active) {
    throw new Error("Wait for the current resumes to finish saving.");
  }
  if (!candidateId || !resumeId) throw new Error("Stored resume was not found.");
  const blob = await fetchStoredResumeBlob(candidateId, resumeId);
  const url = URL.createObjectURL(blob);
  if (IS_EXTENSION) {
    try {
      await chrome.tabs.create({ url });
      releaseResumeBlobUrlLater(url);
    } catch (error) {
      URL.revokeObjectURL(url);
      throw error;
    }
  } else {
    window.open(url, "_blank", "noopener");
    releaseResumeBlobUrlLater(url);
  }
}

async function bulkImportIndeed(enrichContacts) {
  const profiles = selectedIndeedProfiles();
  if (!profiles.length) throw new Error(`Select at least one ${activeSourcingPlatform.label} profile.`);
  if (
    enrichContacts &&
    !confirm(`Import and look up ${profiles.length} selected profile${profiles.length === 1 ? "" : "s"}?`)
  ) {
    return;
  }

  const selectedJob = $("#indeedJobSelect")?.value;
  const jobId = selectedJob ? Number(selectedJob) : null;
  const progress = $("#indeedBulkProgress");
  let importedCount = 0;
  let existingCount = 0;
  let enrichedCount = 0;
  let failedCount = 0;

  for (let index = 0; index < profiles.length; index += 1) {
    const profile = profiles[index];
    if (progress) progress.textContent = `Processing ${index + 1} of ${profiles.length}: ${profile.name}`;
    try {
      const imported = await api("/candidates/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...profile, job_id: jobId, _selectionKey: undefined }),
      });
      if (imported.imported) importedCount += 1;
      else existingCount += 1;
      if (enrichContacts) {
        const enriched = await api(`/candidates/${imported.id}/contact-lookup`, {
          method: "POST",
          timeout: 60000,
        });
        if (enriched.status !== "error") enrichedCount += 1;
        else failedCount += 1;
      }
    } catch {
      failedCount += 1;
    }
  }

  activeJobId = jobId;
  notify(
    `${importedCount} imported, ${existingCount} already present` +
    `${enrichContacts ? `, ${enrichedCount} enriched` : ""}` +
    `${failedCount ? `, ${failedCount} failed` : ""}.`,
    failedCount ? "error" : "",
  );
  await go("candidates");
}

function showDraft(draft) {
  activeDraft = draft;
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="draftTitle">
      <h3 id="draftTitle">Outreach draft <span class="muted small">· review before approving</span></h3>
      <div class="muted small">To: ${escapeHtml((draft.to || []).join(", "))}</div>
      <input class="mt" aria-label="Subject" readonly value="${escapeHtml(draft.subject || "")}">
      <textarea class="mt" aria-label="Message body" readonly rows="10">${escapeHtml(draft.body || "")}</textarea>
      <div class="notice mt">${escapeHtml(draft.compliance || "")}</div>
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="close-modal">Close</button>
        <button type="button" class="btn ghost" data-action="copy-draft">Copy</button>
        <button type="button" class="btn teal" data-action="approve-draft" data-id="${Number(draft.outreach_id)}">Approve draft</button>
      </div>
      <p class="muted small">Approval records your review; it does not send the message.</p>
    </section>
  </div>`;
}

async function draftOutreach(candidateId) {
  const draft = await api("/outreach/draft", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ candidate_id: candidateId, job_id: activeJobId }),
    timeout: 60000,
  });
  showDraft(draft);
}

async function approveDraft(id) {
  await api(`/outreach/${id}/approve`, { method: "POST" });
  closeModal();
  notify("Draft approved and ready to send.");
  if (activeView === "candidates") await viewCandidates();
}

async function copyDraft() {
  if (!activeDraft) return;
  const text = `Subject: ${activeDraft.subject || ""}\n\n${activeDraft.body || ""}`;
  await navigator.clipboard.writeText(text);
  notify("Draft copied to the clipboard.");
}

async function showSmsComposer(candidateId, candidateName, phone) {
  if (!candidateId || !phone) throw new Error("A verified mobile number is required.");
  const [status, preview] = await Promise.all([
    api("/messaging/status"),
    api(`/candidates/${candidateId}/sms-preview?phone=${encodeURIComponent(phone)}`),
  ]);
  activeSmsContext = { candidateId, candidateName, phone: preview.phone };
  const missing = Array.isArray(preview.missing_fields) ? preview.missing_fields : [];
  const blocked = !status.enabled || !status.sender_configured || preview.opted_out ||
    preview.already_contacted || missing.length > 0 || !preview.message;
  const notices = [];
  if (!status.enabled || !status.sender_configured) notices.push("Twilio SMS is not configured on the backend.");
  if (missing.length) notices.push(`Candidate data is incomplete: ${missing.join(", ")}.`);
  if (preview.opted_out) notices.push("This candidate opted out of SMS.");
  if (preview.already_contacted) notices.push("This candidate has already received SMS outreach.");
  if (!status.reply_notifications_configured) notices.push("Reply email notifications are not fully configured.");
  $("#modalRoot").innerHTML = `<div class="modal" role="presentation">
    <section class="sheet" role="dialog" aria-modal="true" aria-labelledby="smsTitle">
      <h3 id="smsTitle">Send SMS to ${escapeHtml(candidateName || "candidate")}</h3>
      <div class="muted small">To: ${escapeHtml(preview.phone || phone)}</div>
      <label class="field-label mt" for="smsMessage">Message</label>
      <textarea id="smsMessage" rows="7" readonly>${escapeHtml(preview.message || "Message unavailable until all candidate fields are present.")}</textarea>
      <p class="muted small">The message uses the candidate's name, specialty, title, city, and state.</p>
      ${notices.map((item) => `<div class="notice mt">${escapeHtml(item)}</div>`).join("")}
      <div class="row modal-actions">
        <button type="button" class="btn ghost" data-action="close-modal">Cancel</button>
        <button type="button" class="btn teal" data-action="send-sms"${blocked ? " disabled" : ""}>Send with Twilio</button>
      </div>
    </section>
  </div>`;
}

async function composeSmsFromButton(button) {
  await showSmsComposer(
    Number(button.dataset.candidateId),
    button.dataset.candidateName || "Candidate",
    button.dataset.phone || "",
  );
}

async function sendCandidateSms() {
  if (!activeSmsContext) throw new Error("Candidate message context expired.");
  await api("/messaging/sms", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      candidate_id: activeSmsContext.candidateId,
      phone: activeSmsContext.phone,
      request_id: crypto.randomUUID(),
    }),
    timeout: 60000,
  });
  closeModal();
  activeSmsContext = null;
  notify("Message accepted by Twilio.");
  if (activeView === "candidates") await viewCandidates();
}

async function viewPipeline() {
  $("#title").textContent = "Pipeline";
  try {
    const suffix = activeJobId ? `?job_id=${encodeURIComponent(activeJobId)}` : "";
    const candidates = await api(`/candidates${suffix}`);
    $("#content").innerHTML = `<div class="card">
      <div class="row spread">
        <h3>Pipeline board</h3>
        <select id="jobSelect" class="field-auto">${jobOptions(true)}</select>
      </div>
      <div class="board-wrap"><div class="board">
        ${STAGES.map((stage) => {
          const inStage = candidates.filter((candidate) => candidate.stage === stage);
          return `<section class="column">
            <h4>${escapeHtml(stage)} (${inStage.length})</h4>
            ${inStage.map((candidate) => `<div class="mini"><strong>${escapeHtml(candidate.name)}</strong><div class="muted">${escapeHtml(candidate.location || "")}</div></div>`).join("")}
          </section>`;
        }).join("")}
      </div></div>
    </div>`;
  } catch (error) {
    setConnection();
    $("#content").innerHTML = backendError(error);
  }
}

async function viewDnc() {
  $("#title").textContent = "Do-Not-Contact";
  try {
    const list = await api("/dnc");
    $("#content").innerHTML = `
      <div class="card">
        <h3>Add a suppressed contact</h3>
        <div class="row">
          <input id="dncValue" class="grow" placeholder="Email or phone to suppress">
          <input id="dncReason" class="grow" placeholder="Reason (optional)">
          <button type="button" class="btn" data-action="add-dnc">Add</button>
        </div>
      </div>
      <div class="card">
        <h3>Suppressed contacts (${list.length})</h3>
        ${list.length
          ? list.map((entry) => `<div class="mini"><strong>${escapeHtml(entry.value)}</strong> <span class="muted">${escapeHtml(entry.reason || "")}</span></div>`).join("")
          : `<p class="muted">None yet. Suppressed emails and phones are removed from enrichment and blocked from outreach.</p>`}
      </div>`;
  } catch (error) {
    setConnection();
    $("#content").innerHTML = backendError(error);
  }
}

async function addDnc() {
  const value = $("#dncValue")?.value.trim();
  if (!value) throw new Error("Enter an email address or phone number.");
  await api("/dnc", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      value,
      reason: $("#dncReason")?.value.trim() || "",
    }),
  });
  notify("Contact added to the do-not-contact list.");
  await viewDnc();
}

function viewSettings() {
  $("#title").textContent = "Settings";
  $("#content").innerHTML = `<div class="card">
      <h3>Application</h3>
      <table class="settings-table"><tbody>
        <tr><td>Status</td><td>${backendHealth?.status === "ok" ? "Connected" : "Unavailable"}</td></tr>
        <tr><td>Version</td><td>${escapeHtml(backendHealth?.version || "")}</td></tr>
        <tr><td>Contact use</td><td>Human approval required</td></tr>
        <tr><td>Sending</td><td>Nothing is sent automatically</td></tr>
      </tbody></table>
    </div>`;
}

async function viewAnalytics() {
  $("#title").textContent = "Analytics";
  if (authConfig.enabled && !authSession?.extension_token) {
    $("#content").innerHTML = `<div class="card"><h3>Sign in required</h3><p class="muted">Sign in with your Healthcareboard email to view your enrichment activity.</p><button type="button" class="btn teal" data-action="login">Sign in</button></div>`;
    return;
  }
  const data = await api("/analytics/me");
  $("#content").innerHTML = `<div class="card"><h3>Your enrichment activity</h3><table class="settings-table"><tbody>
    <tr><td>Candidates enriched</td><td>${Number(data.candidates_enriched || 0)}</td></tr>
    <tr><td>Successful enrichments</td><td>${Number(data.successful_enrichments || 0)}</td></tr>
    <tr><td>Total attempts</td><td>${Number(data.enrichment_attempts || 0)}</td></tr>
  </tbody></table></div>`;
}

async function saveBackend() {
  if (!IS_EXTENSION) return;
  const value = normalizeBackendUrl($("#backendUrl")?.value.trim() || "");
  apiBase = value;
  await writeExtensionSetting(BACKEND_STORAGE_KEY, value);
  const health = await refreshHealth(true);
  if (health) await loadJobs();
  viewSettings();
}

async function retry() {
  const health = await refreshHealth(true);
  if (health) {
    await loadJobs();
    await go(activeView);
  }
}

const views = {
  candidates: viewCandidates,
  indeed: viewIndeed,
  add: viewAdd,
  pipeline: viewPipeline,
  dnc: viewDnc,
  analytics: viewAnalytics,
  settings: viewSettings,
};

async function go(view) {
  if (IS_EXTENSION && !privacyConsent) {
    activeView = "indeed";
    renderSourcingStatus(
      "Consent required",
      "Review the data-use notice before using Medhunt candidate workflows.",
      { retry: false },
    );
    showPrivacyConsent();
    return;
  }
  activeView = views[view] ? view : "candidates";
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.classList.toggle("active", link.dataset.view === activeView);
  });
  await views[activeView]();
}

document.addEventListener("keydown", (event) => {
  const activeFilter = event.target.closest?.('[role="tab"][data-action="filter-indeed-results"]');
  if (!activeFilter || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const filters = Array.from(activeFilter.closest('[role="tablist"]')?.querySelectorAll('[role="tab"]') || []);
  if (!filters.length) return;
  event.preventDefault();
  const current = filters.indexOf(activeFilter);
  const next = event.key === "Home"
    ? filters[0]
    : event.key === "End"
      ? filters.at(-1)
      : filters[(current + (event.key === "ArrowRight" ? 1 : -1) + filters.length) % filters.length];
  next?.click();
});

document.addEventListener("change", async (event) => {
  if (event.target.classList.contains("indeed-select")) {
    if (event.target.checked) indeedSelected.add(event.target.dataset.key);
    else indeedSelected.delete(event.target.dataset.key);
    updateIndeedSelectionUi();
    return;
  }
  if (event.target.id === "indeedSelectAll") {
    indeedSelected = event.target.checked
      ? new Set(indeedCandidates.map((profile) => profile._selectionKey))
      : new Set();
    document.querySelectorAll(".indeed-select").forEach((checkbox) => {
      checkbox.checked = event.target.checked;
    });
    updateIndeedSelectionUi();
    return;
  }
  if (!["jobSelect", "addJobSelect", "indeedJobSelect"].includes(event.target.id)) return;
  activeJobId = event.target.value ? Number(event.target.value) : null;
  if (event.target.id === "jobSelect") await go(activeView);
});

document.addEventListener("click", async (event) => {
  const nav = event.target.closest(".nav-link");
  if (nav) {
    await go(nav.dataset.view);
    return;
  }

  if (event.target.classList.contains("modal")) {
    closeModal();
    return;
  }

  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  const id = Number(button.dataset.id);
  const index = Number(button.dataset.index);

  if (action === "close-modal") {
    closeModal();
    return;
  }
  if (action === "navigate") {
    await go(button.dataset.view);
    return;
  }

  const actions = {
    "login": login,
    "request-login-code": requestLoginCode,
    "verify-login-code": verifyLoginCode,
    "check-device-approval": checkDeviceApproval,
    "revoke-device": () => revokeDevice(button),
    "logout": logout,
    "open-privacy": openPrivacyNotice,
    "decline-privacy": declinePrivacyConsent,
    "accept-privacy": acceptPrivacyConsent,
    "retry": retry,
    "refresh-indeed": scanIndeedCandidates,
    "open-indeed-result": () => openIndeedResult(index),
    "review-indeed-result": () => {
      const profile = indeedCandidates[index];
      if (!profile) throw new Error("That displayed candidate is no longer available.");
      showIndeedImport(profile);
    },
    "toggle-all-indeed": toggleAllIndeedCandidates,
    "lookup-indeed": lookupSelectedIndeedCandidates,
    "retry-failed-lookups": retryFailedLookups,
    "filter-indeed-results": () => filterIndeedResults(button.dataset.filter),
    "approve-identity": () => approveCandidateIdentity(
      Number(button.dataset.candidateId),
      button.dataset.profileKey,
      button.dataset.canonicalName,
    ),
    "open-resume": () => openStoredResume(
      Number(button.dataset.candidateId),
      Number(button.dataset.resumeId),
    ),
    "capture-linkedin-pdf": () => captureLinkedinPdf(index),
    "cancel-linkedin-pdf": () => cancelLinkedinPdf(index),
    "bulk-import-indeed": () => bulkImportIndeed(false),
    "bulk-enrich-indeed": () => bulkImportIndeed(true),
    "capture-indeed": captureIndeedProfile,
    "import-indeed": importIndeedProfile,
    "public-records": () => publicRecordFromButton(button),
    "public-records-refresh": () => publicRecordFromButton(button, true),
    "public-records-copy": copyPublicRecordResult,
    "public-records-back": reopenCaptureReview,
    "create-job": createJob,
    "submit-intake": submitIntake,
    "enrich": () => enrichCandidate(id),
    "enrich-all": enrichAll,
    "rank-all": rankAll,
    "move": () => moveCandidate(id),
    "draft": () => draftOutreach(id),
    "compose-sms": () => composeSmsFromButton(button),
    "send-sms": sendCandidateSms,
    "approve-draft": () => approveDraft(id),
    "copy-draft": copyDraft,
    "add-dnc": addDnc,
    "save-backend": saveBackend,
    "test-backend": () => refreshHealth(true),
  };

  if (actions[action]) await withBusy(button, actions[action]);
});

async function startExtensionWorkspace() {
  if (extensionWorkspaceStarted || extensionWorkspaceStarting || !privacyConsent) return;
  extensionWorkspaceStarting = true;
  try {
    await loadAuth();
    if (authConfig.enabled && authSession?.device_pending) {
      showPendingDeviceApproval();
      return;
    }
    if (authConfig.enabled && !authSession?.extension_token) {
      await login();
      return;
    }
    await refreshExtensionCredits();
    extensionWorkspaceStarted = true;
    activeView = "indeed";
    renderSourcingStatus(
      "Detecting candidate page",
      "The panel follows the active tab automatically.",
      { retry: false },
    );
    const servicePromise = refreshHealth().then(async (health) => {
      if (!health) return;
      try {
        await loadJobs();
      } catch {
        setConnection();
      }
      await processPendingResumeEvents();
    });
    await synchronizeActiveSourcingTab("startup");
    await servicePromise;
  } finally {
    extensionWorkspaceStarting = false;
  }
}

(async function initialize() {
  if (!IS_EXTENSION) {
    document.querySelector("[data-view='indeed']")?.classList.add("hidden");
  } else {
    document.body.classList.add("extension-shell");
  }
  await loadBackendConfig();
  if (IS_EXTENSION) {
    activeView = "indeed";
    privacyConsent = (await readChromeSetting(PRIVACY_CONSENT_KEY)) === true;
    if (!privacyConsent) {
      // Load the public auth configuration before showing first-run access.
      // When Healthcareboard auth is enabled, sign-in and the data-use
      // acknowledgement live in one compact entry screen.
      await loadAuth();
      renderSourcingStatus(
        "Review required",
        "Medhunt will not read or transmit candidate profile data until you accept the data-use notice.",
        { retry: false },
      );
      if (authConfig.enabled && authSession?.device_pending) {
        showPendingDeviceApproval();
      } else if (authConfig.enabled && !authSession?.extension_token) {
        await login({ requirePrivacyConsent: true });
      } else {
        showPrivacyConsent();
      }
      return;
    }
    await startExtensionWorkspace();
    return;
  }
  const health = await refreshHealth();
  if (health) {
    try { await loadJobs(); } catch { setConnection(); }
  }
  await go("candidates");
})();
