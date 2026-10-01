async function enableActionClick() {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

const TAB_CONTEXT_EVENT = "MEDHUNT_ACTIVE_TAB_CHANGED";
const tabUpdateTimers = new Map();
const linkedinDownloadClaims = new Map();
const PENDING_RESUME_KEY = "medhuntPendingResumeEvents";

async function queueResumeEvent(event) {
  const eventId = String(event.event_id || `${event.platform || "indeed"}:${event.candidateId}:${Date.now()}`);
  const stored = await chrome.storage.session.get([PENDING_RESUME_KEY]);
  const pending = Array.isArray(stored[PENDING_RESUME_KEY]) ? stored[PENDING_RESUME_KEY] : [];
  const next = [...pending.filter((item) => item?.event_id !== eventId), {
    ...event,
    event_id: eventId,
    queued_at: Date.now(),
  }].slice(-10);
  await chrome.storage.session.set({ [PENDING_RESUME_KEY]: next });
  await chrome.runtime.sendMessage(next.at(-1)).catch(() => {});
}

async function acknowledgeResumeEvent(eventId) {
  const stored = await chrome.storage.session.get([PENDING_RESUME_KEY]);
  const pending = Array.isArray(stored[PENDING_RESUME_KEY]) ? stored[PENDING_RESUME_KEY] : [];
  await chrome.storage.session.set({
    [PENDING_RESUME_KEY]: pending.filter((item) => item?.event_id !== String(eventId || "")),
  });
}

function sourcingPlatformForUrl(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    const matches = (root) => host === root || host.endsWith(`.${root}`);
    if (matches("indeed.com")) return "indeed";
    if (matches("vivian.com")) return "vivian";
    if (matches("ziprecruiter.com")) return "ziprecruiter";
    if (matches("linkedin.com")) return "linkedin";
    if (matches("facebook.com")) return "facebook";
    if (matches("npino.com")) return "npino";
    if (host === "eservices.nysed.gov") return "nysed";
    if (matches("npiprofile.com")) return "npiprofile";
    if (matches("medifind.com") && /^\/(?:doctors|specialty)(?:\/|$)/i.test(url.pathname)) {
      return "medifind";
    }
    if (matches("commonspirit.org") && /^\/(?:search|find-a-(?:doctor|location))(?:\/|$)/i.test(url.pathname)) {
      return "commonspirit";
    }
    if (host === "providers.sharecare.com" && /^\/(?:find-a-doctor|doctor)(?:\/|$)/i.test(url.pathname)) {
      return "sharecare";
    }
    if (host === "doctor.webmd.com" && (
      /^\/results(?:\/|$)/i.test(url.pathname)
      || /^\/providers\/specialty(?:\/|$)/i.test(url.pathname)
      || /^\/doctor\/[^/]+-overview\/?$/i.test(url.pathname)
    )) return "webmd";
    if (
      host === "health.usnews.com" &&
      /^\/(?:doctors|nurse-practitioners)(?:\/|$)/i.test(url.pathname)
    ) return "usnews";
  } catch {

  }
  return "";
}

function tabContext(tab, reason = "updated") {
  return {
    type: TAB_CONTEXT_EVENT,
    tab_id: Number(tab?.id) || 0,
    window_id: Number(tab?.windowId) || 0,
    url: String(tab?.url || ""),
    status: String(tab?.status || ""),
    platform: sourcingPlatformForUrl(tab?.url || ""),
    reason,
    sent_at: Date.now(),
  };
}

async function broadcastTabContext(tab, reason) {
  if (!tab?.id) return;
  await chrome.runtime.sendMessage(tabContext(tab, reason)).catch(() => {});
}

async function broadcastActiveTab(windowId, reason) {
  const query = { active: true };
  if (Number.isInteger(windowId) && windowId >= 0) query.windowId = windowId;
  else query.currentWindow = true;
  const [tab] = await chrome.tabs.query(query);
  if (tab) await broadcastTabContext(tab, reason);
}

chrome.runtime.onInstalled.addListener(() => {
  enableActionClick().catch(console.error);
});

chrome.runtime.onStartup.addListener(() => {
  enableActionClick().catch(console.error);
});

chrome.action.onClicked.addListener(async (tab) => {
  try {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  } catch {

  }
});

chrome.tabs.onActivated?.addListener(({ tabId }) => {
  chrome.tabs.get(tabId)
    .then((tab) => broadcastTabContext(tab, "activated"))
    .catch(() => {});
});

chrome.tabs.onUpdated?.addListener((tabId, changeInfo, tab) => {
  if (!tab?.active || (!changeInfo.url && changeInfo.status !== "complete")) return;
  clearTimeout(tabUpdateTimers.get(tabId));
  tabUpdateTimers.set(tabId, setTimeout(() => {
    tabUpdateTimers.delete(tabId);
    chrome.tabs.get(tabId)
      .then((current) => broadcastTabContext(current, changeInfo.url ? "navigated" : "loaded"))
      .catch(() => {});
  }, changeInfo.status === "complete" ? 120 : 250));
});

chrome.tabs.onRemoved?.addListener((tabId, removeInfo) => {
  clearTimeout(tabUpdateTimers.get(tabId));
  tabUpdateTimers.delete(tabId);
  setTimeout(() => {
    broadcastActiveTab(removeInfo?.windowId, "removed").catch(() => {});
  }, 80);
});

chrome.windows?.onFocusChanged?.addListener((windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) return;
  broadcastActiveTab(windowId, "window-focused").catch(() => {});
});

enableActionClick().catch(console.error);

async function dispatchTrustedIndeedClick(tabId, x, y) {
  const target = { tabId };
  const point = { x, y, button: "left" };
  let attached = false;
  try {
    await chrome.debugger.attach(target, "1.3");
    attached = true;
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseMoved",
      ...point,
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mousePressed",
      ...point,
      clickCount: 1,
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseReleased",
      ...point,
      clickCount: 1,
    });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error?.message || error) };
  } finally {
    if (attached) await chrome.debugger.detach(target).catch(() => {});
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "MEDHUNT_GET_ACTIVE_TAB_CONTEXT") {
    chrome.tabs.query({ active: true, currentWindow: true })
      .then(([tab]) => sendResponse(tab ? tabContext(tab, "requested") : {
        type: TAB_CONTEXT_EVENT,
        tab_id: 0,
        window_id: 0,
        url: "",
        status: "",
        platform: "",
        reason: "requested",
        sent_at: Date.now(),
      }))
      .catch(() => sendResponse({
        type: TAB_CONTEXT_EVENT,
        tab_id: 0,
        window_id: 0,
        url: "",
        status: "",
        platform: "",
        reason: "requested",
        sent_at: Date.now(),
      }));
    return true;
  }
  if (message?.type === "MEDHUNT_GET_PENDING_RESUME_EVENTS") {
    chrome.storage.session.get([PENDING_RESUME_KEY])
      .then((stored) => sendResponse({
        ok: true,
        events: Array.isArray(stored[PENDING_RESUME_KEY]) ? stored[PENDING_RESUME_KEY] : [],
      }))
      .catch(() => sendResponse({ ok: true, events: [] }));
    return true;
  }
  if (message?.type === "MEDHUNT_ACK_RESUME_EVENT") {
    acknowledgeResumeEvent(message.event_id)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "MEDHUNT_TRUSTED_INDEED_CLICK") {
    const tabId = Number(sender?.tab?.id);
    const tabUrl = String(sender?.tab?.url || "");
    const x = Number(message.x);
    const y = Number(message.y);
    if (
      !tabId ||
      !/(^|\.)indeed\.com$/i.test((() => {
        try { return new URL(tabUrl).hostname; } catch { return ""; }
      })()) ||
      !Number.isFinite(x) || !Number.isFinite(y) ||
      x < 0 || y < 0 || x > 10000 || y > 10000
    ) {
      sendResponse({ ok: false, error: "Trusted clicks are restricted to visible Indeed content." });
      return false;
    }
    dispatchTrustedIndeedClick(tabId, x, y).then(sendResponse);
    return true;
  }
  if (message?.type === "MEDHUNT_TRUSTED_LINKEDIN_CLICK") {
    const tabId = Number(sender?.tab?.id);
    const tabUrl = String(sender?.tab?.url || "");
    const x = Number(message.x);
    const y = Number(message.y);
    let parsed = null;
    try { parsed = new URL(tabUrl); } catch { parsed = null; }
    const host = String(parsed?.hostname || "").toLowerCase();
    if (
      !tabId ||
      !(host === "linkedin.com" || host.endsWith(".linkedin.com")) ||
      !/^\/in\/[^/?#]+\/?$/i.test(String(parsed?.pathname || "")) ||
      !Number.isFinite(x) || !Number.isFinite(y) ||
      x < 0 || y < 0 || x > 10000 || y > 10000
    ) {
      sendResponse({ ok: false, error: "Trusted clicks are restricted to an open LinkedIn profile." });
      return false;
    }
    dispatchTrustedIndeedClick(tabId, x, y).then(sendResponse);
    return true;
  }
  if (message?.type === "MEDHUNT_SET_ACTIVE_CANDIDATE") {
    chrome.storage.session.set({
      medhuntResumeCandidate: {
        candidateId: Number(message.candidateId),
        name: String(message.name || ""),
        sourceId: String(message.sourceId || ""),
        expiresAt: Date.now() + (10 * 60 * 1000),
      },
    }).then(() => sendResponse({ ok: true })).catch((error) => {
      sendResponse({ ok: false, error: String(error) });
    });
    return true;
  }
  if (message?.type === "MEDHUNT_CLEAR_ACTIVE_CANDIDATE") {
    chrome.storage.session.remove(["medhuntResumeCandidate"])
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "MEDHUNT_ARM_LINKEDIN_PDF_CAPTURE") {
    const tabId = Number(message.tabId);
    const candidateId = Number(message.candidateId);
    const sourceUrl = String(message.sourceUrl || "");
    const sourceSlug = linkedinProfileSlug(sourceUrl);
    if (!tabId || !candidateId || !sourceSlug) {
      sendResponse({ ok: false, error: "A saved candidate and an open LinkedIn profile are required." });
      return false;
    }
    chrome.tabs.get(tabId).then(async (tab) => {
      if (linkedinProfileSlug(tab?.url || "") !== sourceSlug) {
        throw new Error("The selected browser tab does not show this candidate's exact LinkedIn profile.");
      }
      await chrome.storage.session.set({
        medhuntLinkedinPdfCapture: {
          candidateId,
          name: String(message.name || "LinkedIn candidate"),
          sourceUrl,
          sourceSlug,
          tabId,
          downloadId: 0,
          armedAt: Date.now(),
          expiresAt: Date.now() + (5 * 60 * 1000),
        },
      });
      sendResponse({ ok: true, expires_in_seconds: 300 });
    }).catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "MEDHUNT_DISARM_LINKEDIN_PDF_CAPTURE") {
    chrome.storage.session.remove(["medhuntLinkedinPdfCapture"])
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  return false;
});

function isIndeedResumeDownload(download) {
  const origin = [download.url, download.finalUrl, download.referrer]
    .filter(Boolean)
    .join(" ");
  return /(^|[./])indeed\.com(?=[:/ ])/i.test(origin) &&
    /\.pdf(?:$|[?#])/i.test(download.filename || download.url || "");
}

function linkedinProfileSlug(value) {
  try {
    const parsed = new URL(value);
    if (!(parsed.hostname === "linkedin.com" || parsed.hostname.endsWith(".linkedin.com"))) return "";
    const match = parsed.pathname.match(/^\/in\/([^/?#]+)\/?$/i);
    if (!match) return "";
    try {
      return decodeURIComponent(match[1]).trim().toLowerCase();
    } catch {
      return match[1].trim().toLowerCase();
    }
  } catch {
    return "";
  }
}

function isPdfDownload(download) {
  const value = [download?.filename, download?.url, download?.finalUrl]
    .filter(Boolean)
    .join(" ");
  return /application\/pdf/i.test(download?.mime || "") || /\.pdf(?:$|[?# ])/i.test(value);
}

function hasDownloadTabId(download) {
  return Number.isInteger(download?.tabId) && Number(download.tabId) >= 0;
}

function parsedUrl(value) {
  try { return new URL(value); } catch { return null; }
}

async function matchesArmedLinkedinPdf(download, capture) {
  const expectedSlug = String(capture?.sourceSlug || linkedinProfileSlug(capture?.sourceUrl || ""));
  const armedTabId = Number(capture?.tabId);
  if (
    !expectedSlug || !armedTabId ||
    Number(capture?.expiresAt) < Date.now() ||
    !isPdfDownload(download)
  ) return false;



  let armedTab;
  try {
    armedTab = await chrome.tabs.get(armedTabId);
  } catch {
    return false;
  }
  if (linkedinProfileSlug(armedTab?.url || "") !== expectedSlug) return false;



  if (hasDownloadTabId(download) && Number(download.tabId) !== armedTabId) return false;

  const origins = [download?.url, download?.finalUrl, download?.referrer]
    .filter(Boolean)
    .map(parsedUrl)
    .filter(Boolean);
  const profileOrigins = origins
    .map((origin) => linkedinProfileSlug(origin.href))
    .filter(Boolean);
  if (profileOrigins.some((slug) => slug !== expectedSlug)) return false;

  if (hasDownloadTabId(download)) return true;





  const hasLinkedinOrigin = origins.some((origin) => {
    const host = origin.hostname.toLowerCase();
    return host === "linkedin.com" || host.endsWith(".linkedin.com") || host.endsWith(".licdn.com");
  });
  if (hasLinkedinOrigin) return true;
  return origins.length > 0 && origins.every((origin) => !/^https?:$/i.test(origin.protocol));
}



chrome.downloads.onCreated.addListener((download) => {
  const linkedinClaim = (async () => {
    const session = await chrome.storage.session.get(["medhuntLinkedinPdfCapture"]);
    const linkedin = session.medhuntLinkedinPdfCapture;
    if (linkedin?.candidateId && Number(linkedin.expiresAt) >= Date.now()) {
      if (await matchesArmedLinkedinPdf(download, linkedin)) {
        await chrome.storage.session.set({
          medhuntLinkedinPdfCapture: {
            ...linkedin,
            downloadId: Number(download.id),
            downloadStartedAt: Date.now(),
          },
        });
        await chrome.runtime.sendMessage({
          type: "MEDHUNT_LINKEDIN_PDF_CAPTURE_STARTED",
          candidateId: Number(linkedin.candidateId),
        }).catch(() => {});
        return true;
      }
    }
    return false;
  })().catch(() => false);
  linkedinDownloadClaims.set(Number(download.id), linkedinClaim);
  linkedinClaim.finally(() => linkedinDownloadClaims.delete(Number(download.id)));

  (async () => {
    await linkedinClaim;
    const capture = await chrome.storage.local.get(["medhuntResumeCapturing"]);
    if (!capture.medhuntResumeCapturing) return;
    const url = download.finalUrl || download.url || "";
    if (!url) return;
    await chrome.storage.local.set({
      medhuntLastResumeDownload: {
        id: download.id,
        url,
        mime: download.mime || "",
        filename: download.filename || "",
        createdAt: Date.now(),
      },
    });
  })().catch(() => {});
});

chrome.downloads.onChanged.addListener((delta) => {
  if (!["complete", "interrupted"].includes(delta.state?.current)) return;
  (async () => {



    const pendingClaim = linkedinDownloadClaims.get(Number(delta.id));
    if (pendingClaim) await pendingClaim;
    const session = await chrome.storage.session.get(["medhuntLinkedinPdfCapture"]);
    const linkedin = session.medhuntLinkedinPdfCapture;
    if (linkedin?.candidateId && Number(linkedin.downloadId) === Number(delta.id)) {
      if (delta.state.current === "interrupted") {
        await chrome.runtime.sendMessage({
          type: "MEDHUNT_LINKEDIN_PDF_CAPTURE_FAILED",
          candidateId: Number(linkedin.candidateId),
          error: delta.error?.current || "The LinkedIn PDF download was interrupted.",
        }).catch(() => {});
        await chrome.storage.session.remove(["medhuntLinkedinPdfCapture"]);
        return;
      }
      const [download] = await chrome.downloads.search({ id: delta.id });
      if (download && await matchesArmedLinkedinPdf(download, linkedin)) {
        await queueResumeEvent({
          type: "MEDHUNT_RESUME_DOWNLOADED",
          platform: "linkedin",
          candidateId: Number(linkedin.candidateId),
          candidateName: linkedin.name,
          path: download.filename,
          filename: String(download.filename || "").split(/[\\/]/).pop() || "linkedin-profile.pdf",
        });
      } else {
        await chrome.runtime.sendMessage({
          type: "MEDHUNT_LINKEDIN_PDF_CAPTURE_FAILED",
          candidateId: Number(linkedin.candidateId),
          error: "The PDF did not complete from the exact armed LinkedIn profile and tab.",
        }).catch(() => {});
      }
      await chrome.storage.session.remove(["medhuntLinkedinPdfCapture"]);
      return;
    }

    if (delta.state.current !== "complete") return;
    const [download] = await chrome.downloads.search({ id: delta.id });
    if (!download || !isIndeedResumeDownload(download)) return;
    const stored = await chrome.storage.session.get(["medhuntResumeCandidate"]);
    const candidate = stored.medhuntResumeCandidate;
    if (!candidate?.candidateId || Number(candidate.expiresAt) < Date.now()) return;
    await queueResumeEvent({
      type: "MEDHUNT_RESUME_DOWNLOADED",
      platform: "indeed",
      candidateId: Number(candidate.candidateId),
      candidateName: candidate.name,
      path: download.filename,
      filename: String(download.filename || "").split(/[\\/]/).pop() || "resume.pdf",
    });
    await chrome.storage.session.remove(["medhuntResumeCandidate"]);
  })().catch(console.error);
});
