(() => {
  "use strict";

  const ADAPTER_REVISION = "platform-capture-v2";
  const ADAPTER_REQUEST = "MEDHUNT_PLATFORM_V2_REQUEST";
  if (window.__medhuntPlatformAdapterRevision === ADAPTER_REVISION) return;
  window.__medhuntPlatformAdapterRevision = ADAPTER_REVISION;
  window.__medhuntPlatformCaptureLoaded = true;

  const host = location.hostname.toLowerCase();
  const PLATFORM = host === "vivian.com" || host.endsWith(".vivian.com")
    ? { key: "vivian", label: "Vivian" }
    : host === "ziprecruiter.com" || host.endsWith(".ziprecruiter.com")
      ? { key: "ziprecruiter", label: "ZipRecruiter" }
      : null;
  if (!PLATFORM) return;

  let scanInProgress = false;
  let lastProfiles = [];
  let lastElements = new Map();

  function all(selector, root = document) {
    try { return Array.from(root.querySelectorAll(selector)); } catch { return []; }
  }

  function clean(value) {
    return String(value ?? "")
      .replace(/\u00a0/g, " ")
      .replace(/\s+/g, " ")
      .replace(/^(--|—)\s*/, "")
      .replace(/not specified/gi, "")
      .trim();
  }

  function visibleText(element) {
    return clean(element?.innerText || element?.textContent || "");
  }

  function isVisible(element) {
    if (!element || !element.isConnected) return false;
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE) {
      if (
        node.hidden ||
        node.inert ||
        node.getAttribute?.("aria-hidden") === "true" ||
        /^(?:closed|inactive|exited|stale)$/i.test(node.getAttribute?.("data-state") || "") ||
        /^(?:stale|removed)$/i.test(node.getAttribute?.("data-status") || "") ||
        node.getAttribute?.("data-stale") === "true" ||
        node.getAttribute?.("data-medhunt-stale") === "true"
      ) return false;
      const style = getComputedStyle(node);
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        Number(style.opacity) === 0
      ) return false;
      node = node.parentElement;
    }
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0;
  }

  const NON_PERSON_LABELS = /^(?:recommended jobs?|suggested jobs?|job matches?|people you may know|search results?|candidate(?:s)?|talent pool|view profile|learn more|show more|load more)$/i;

  function looksLikePersonName(value) {
    const name = clean(value).replace(/\s*[|\u00b7\u2022]\s*.*$/, "").trim();
    if (
      name.length < 3 ||
      name.length > 100 ||
      NON_PERSON_LABELS.test(name) ||
      /\d|@|https?:|\b(?:click|apply|hiring|recommended|jobs?|candidates?|results?)\b/i.test(name)
    ) return false;
    const tokens = name.split(/\s+/).filter(Boolean);
    return tokens.length >= 2 && tokens.length <= 12 && tokens.every((token) => (
      /^[\p{L}\p{M}.'\u2019-]+,?$/u.test(token)
    ));
  }

  function normalized(value) {
    return clean(value)
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  function textLines(element) {
    const seen = new Set();
    return String(element?.innerText || element?.textContent || "")
      .split(/\r?\n/)
      .map(clean)
      .filter((line) => {
        const key = line.toLowerCase();
        if (!line || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }

  function hash(value) {
    let result = 5381;
    for (const character of String(value || "")) {
      result = (((result << 5) + result) + character.charCodeAt(0)) >>> 0;
    }
    return result.toString(36);
  }

  function unique(values) {
    return [...new Set((values || []).map(clean).filter(Boolean))];
  }

  function absoluteUrl(value) {
    try { return new URL(value || location.href, location.origin).href; } catch { return location.href; }
  }

  function candidateLink(card) {
    return all("a[href]", card).find((link) => {
      const href = link.getAttribute("href") || "";
      return /(?:candidate|jobseeker|resume|profile|encryptedJobseekerId)/i.test(href) ||
        /\/talent(?:-pool)?\/[^/?#]+/i.test(href);
    }) || null;
  }

  function taggedNotes(profile) {
    const lines = [profile.name, profile.location, profile.headline];
    if (profile.specialties?.length) {
      lines.push(`Specialty: ${unique(profile.specialties).join(", ")}`);
    }
    for (const job of profile.employment || []) {
      if (job.position) lines.push(`Role: ${job.position}`);
      if (job.company) lines.push(`Employer: ${job.company}`);
      const dates = [job.startDate, job.endDate].filter(Boolean).join(" - ");
      if (dates) lines.push(`Dates: ${dates}`);
    }
    for (const education of profile.education || []) {
      if (education.degree) lines.push(`Degree: ${education.degree}`);
      if (education.school) lines.push(`School: ${education.school}`);
    }
    if (profile.skills?.length) lines.push(`Skills: ${unique(profile.skills).join(", ")}`);
    if (profile.licenses?.length) lines.push(`Licenses: ${unique(profile.licenses).join(", ")}`);
    if (profile.certifications?.length) {
      lines.push(`Certifications: ${unique(profile.certifications).join(", ")}`);
    }
    if (profile.yearsOfExperience) lines.push(`Experience: ${profile.yearsOfExperience} years`);
    return unique(lines).join("\n").slice(0, 12000);
  }

  function vivianProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    const cards = all('[data-qa="Candidate Card"]').filter(isVisible);
    for (const card of cards) {
      const name = clean(card.querySelector('[data-qa="User Name"]')?.textContent);
      if (!looksLikePersonName(name)) continue;
      const header = clean(card.querySelector('[data-qa="Employer Chat Header"]')?.textContent);
      const fields = {};
      for (const term of all('dt[data-qa$="List Item DT"]', card)) {
        const key = clean((term.getAttribute("data-qa") || "").replace(/ List Item DT$/, ""));
        if (!key) continue;
        fields[key] = clean(card.querySelector(`dd[data-qa="${CSS.escape(key)} List Item DD"]`)?.textContent);
      }
      const locationText = fields["Home location"] || "";
      const discipline = fields.Discipline || "";
      const specialtyRaw = fields.Specialty || "";
      const specialty = specialtyRaw.replace(/\s*\(\d+\s*years?\)\s*/i, "").trim();
      const years = Number(specialtyRaw.match(/\((\d+)\s*years?\)/i)?.[1]) || 0;
      const secondSpecialty = fields["Specialty 2"] || "";
      const license = fields.License || "";
      const certifications = all('dd[data-qa="Certifications List Item DD"]', card)
        .flatMap((element) => clean(element.textContent).split(","))
        .map(clean)
        .filter(Boolean);
      const educationText = fields.Education || "";
      const recentExperience = fields["Recent experience"] || "";
      const headline = header || [discipline, specialty].filter(Boolean).join(" / ");
      if (!name || (!locationText && !headline)) continue;

      const link = candidateLink(card);
      const sourceUrl = link ? absoluteUrl(link.getAttribute("href")) : location.href;
      let explicitId = clean(
        card.getAttribute("data-candidate-id") ||
        card.getAttribute("data-user-id") ||
        card.getAttribute("data-profile-id"),
      );
      if (!explicitId && link) {
        try {
          const url = new URL(sourceUrl);
          explicitId =
            url.searchParams.get("candidateId") ||
            url.searchParams.get("candidate_id") ||
            url.searchParams.get("userId") ||
            url.pathname.split("/").filter(Boolean).pop() ||
            "";
        } catch {}
      }
      const identity = [normalized(name), normalized(locationText), normalized(discipline), normalized(specialty)].join("|");
      const sourceId = explicitId ? `vv_${explicitId}` : `vv_${hash(identity)}`;
      const identityKey = `${normalized(name)}|${normalized(locationText)}`;
      if (seen.has(sourceId) || seen.has(identityKey)) continue;
      seen.add(sourceId);
      seen.add(identityKey);
      const profile = {
        name,
        location: locationText,
        headline,
        source: PLATFORM.key,
        source_url: sourceUrl,
        source_id: sourceId,
        employment: headline ? [{ company: recentExperience, position: headline }] : [],
        education: educationText ? [{ school: "", degree: educationText }] : [],
        skills: unique([specialty, secondSpecialty]),
        specialties: unique([specialty, secondSpecialty]),
        licenses: unique([license]),
        certifications: unique(certifications),
        yearsOfExperience: years,
      };
      profile.notes = taggedNotes(profile);
      profile.result_index = profiles.length;
      profiles.push(profile);
      elements.set(sourceId, card);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements, cardCount: cards.length };
  }

  function requestMainCandidates(timeoutMs = 1800) {
    return new Promise((resolve) => {
      const requestId = `medhunt_${Date.now()}_${Math.random().toString(36).slice(2)}`;
      let finished = false;
      const finish = (value) => {
        if (finished) return;
        finished = true;
        window.removeEventListener("message", receive);
        resolve(value);
      };
      const receive = (event) => {
        if (
          event.source === window &&
          event.data?.type === "MEDHUNT_PLATFORM_MAIN_RESPONSE" &&
          event.data.requestId === requestId
        ) finish(Array.isArray(event.data.candidates) ? event.data.candidates : []);
      };
      window.addEventListener("message", receive);
      window.postMessage({ type: "MEDHUNT_PLATFORM_MAIN_REQUEST", requestId }, "*");
      setTimeout(() => finish([]), timeoutMs);
    });
  }

  function zipDomFallback() {
    const cards = Array.from(new Set(all([
      "section.relative.p-24.bg-white",
      "section.relative.p-24",
      "[class*='candidate-card']",
      "[data-testid*='candidate-card']",
      "[data-testid='candidate']",
      "[data-qa='Candidate Card']",
      "[data-qa*='candidate-card']",
      "[data-candidate-id]",
      "article",
    ].join(",")))).filter(isVisible);
    const candidates = [];
    const candidateCards = [];
    const seen = new Set();
    for (const card of cards) {
      const lines = textLines(card).filter((line) => line.length <= 180);
      const link = candidateLink(card);
      const explicitSemanticCard = card.matches?.(
        "[class*='candidate-card'], [data-testid*='candidate-card'], [data-testid='candidate'], " +
        "[data-qa='Candidate Card'], [data-qa*='candidate-card'], [data-candidate-id]"
      );



      if (!link && !explicitSemanticCard) continue;

      const preferredNameElements = [
        ...all("[data-testid*='candidate-name'], [data-qa*='name'], [class*='candidate-name'], h1, h2, h3", card),
        ...(link ? [link] : []),
      ];
      const name = preferredNameElements
        .filter(isVisible)
        .map((element) => clean(element.textContent).split("\n")[0])
        .find(looksLikePersonName) || lines.find(looksLikePersonName) || "";
      if (!looksLikePersonName(name)) continue;
      const locationText = lines.find((line) => /,\s*[A-Z]{2}(?:\s+\d{5})?\b/.test(line)) || "";
      const url = link ? absoluteUrl(link.getAttribute("href")) : location.href;
      let sourceId = clean(card.getAttribute("data-candidate-id"));
      try {
        const parsed = new URL(url);
        sourceId = sourceId ||
          parsed.searchParams.get("encryptedJobseekerId") ||
          parsed.searchParams.get("candidateId") ||
          parsed.searchParams.get("candidate_id") ||
          "";
        if (!sourceId && link) {
          const pathMatch = parsed.pathname.match(/\/(?:candidate|jobseeker|resume|profile)\/([^/?#]+)/i);
          sourceId = pathMatch?.[1] || "";
        }
      } catch {}
      sourceId = sourceId || `zr_${hash([normalized(name), normalized(locationText), normalized(url)].join("|"))}`;
      if (seen.has(sourceId)) continue;
      seen.add(sourceId);
      candidateCards.push(card);
      candidates.push({
        encryptedJobseekerId: sourceId,
        name,
        location: locationText,
        employment: [],
        education: [],
        skills: [],
        licenses: [],
        certifications: [],
        _element: card,
        _url: url,
        _lines: lines,
      });
    }
    return { candidates, cards: candidateCards };
  }

  function zipElementForCandidate(candidate, fallback, sourceId, name, locationText) {
    if (candidate?._element?.isConnected && isVisible(candidate._element)) return candidate._element;
    const exact = fallback.candidates.find((item) => (
      clean(item.encryptedJobseekerId) === sourceId && item._element?.isConnected
    ));
    if (exact?._element) return exact._element;

    const identity = `${normalized(name)}|${normalized(locationText)}`;
    const identityMatch = fallback.candidates.find((item) => (
      `${normalized(item.name)}|${normalized(item.location)}` === identity && item._element?.isConnected
    ));
    if (identityMatch?._element) return identityMatch._element;

    return fallback.cards.find((card) => {
      const hrefs = all("a[href]", card).map((link) => link.getAttribute("href") || "");
      return card.getAttribute("data-candidate-id") === sourceId ||
        hrefs.some((href) => href.includes(encodeURIComponent(sourceId)) || href.includes(sourceId));
    }) || null;
  }

  async function zipProfiles() {
    let candidates = await requestMainCandidates();
    const fallback = zipDomFallback();
    if (!candidates.length) candidates = fallback.candidates;
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    for (const candidate of candidates) {
      const sourceId = clean(candidate.encryptedJobseekerId);
      const name = clean(candidate.name);
      if (!sourceId || !looksLikePersonName(name) || seen.has(sourceId)) continue;
      seen.add(sourceId);
      const employment = Array.isArray(candidate.employment) ? candidate.employment : [];
      const current = employment[0] || {};
      const sourceUrl = candidate._url || (() => {
        const url = new URL(location.href);
        url.searchParams.set("encryptedJobseekerId", sourceId);
        return url.href;
      })();
      const profile = {
        name,
        location: clean(candidate.location),
        headline: clean(current.position),
        source: PLATFORM.key,
        source_url: sourceUrl,
        source_id: sourceId,
        employment,
        education: Array.isArray(candidate.education) ? candidate.education : [],
        skills: Array.isArray(candidate.skills) ? candidate.skills : [],
        licenses: Array.isArray(candidate.licenses) ? candidate.licenses : [],
        certifications: Array.isArray(candidate.certifications) ? candidate.certifications : [],
        yearsOfExperience: Number(candidate.yearsOfExperience) || 0,
      };
      profile.notes = candidate._lines?.length
        ? unique([taggedNotes(profile), ...candidate._lines]).join("\n").slice(0, 12000)
        : taggedNotes(profile);
      profile.result_index = profiles.length;
      profiles.push(profile);
      const element = zipElementForCandidate(
        candidate,
        fallback,
        sourceId,
        name,
        profile.location,
      );
      if (element) elements.set(sourceId, element);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements, cardCount: Math.max(fallback.cards.length, profiles.length) };
  }

  async function scanSnapshot() {
    const result = PLATFORM.key === "vivian" ? vivianProfiles() : await zipProfiles();
    lastProfiles = result.profiles;
    lastElements = result.elements;
    return {
      ok: true,
      platform: PLATFORM.key,
      platform_label: PLATFORM.label,
      profiles: result.profiles,
      count: result.profiles.length,
      expected_count: expectedCount(result.profiles.length, result.cardCount),
      raw: { cards: result.cardCount, names: result.profiles.length },
      page_url: location.href,
    };
  }

  function expectedCount(captured, cards) {
    const pageText = visibleText(document.body).slice(0, 50000);
    const range = pageText.match(/\b(\d{1,3})\s*[-–]\s*(\d{1,3})\s+of\s+[\d,]+/i);
    const explicit = pageText.match(/\b([\d,]{1,5})\s+candidates?\b/i);
    const rangeCount = range ? Math.max(0, Number(range[2]) - Number(range[1]) + 1) : 0;
    const explicitCount = explicit ? Number(explicit[1].replace(/,/g, "")) : 0;
    return Math.min(100, Math.max(captured, cards, rangeCount, explicitCount));
  }

  function candidateKey(profile) {
    return clean(profile?.source_id) || `${normalized(profile?.name)}|${normalized(profile?.location)}`;
  }

  function scrollContainer() {
    const firstCard = PLATFORM.key === "vivian"
      ? all('[data-qa="Candidate Card"]').find(isVisible)
      : all([
          "section.relative.p-24.bg-white",
          "section.relative.p-24",
          "[class*='candidate-card']",
          "[data-testid*='candidate-card']",
          "[data-testid='candidate']",
          "[data-qa='Candidate Card']",
          "[data-qa*='candidate-card']",
          "[data-candidate-id]",
          "article",
        ].join(",")).find((card) => (
          isVisible(card) && (candidateLink(card) || card.matches?.(
            "[class*='candidate-card'], [data-testid*='candidate-card'], [data-testid='candidate'], " +
            "[data-qa='Candidate Card'], [data-qa*='candidate-card'], [data-candidate-id]"
          ))
        ));
    let element = firstCard?.parentElement;
    while (element && element !== document.body) {
      const style = getComputedStyle(element);
      if (/(auto|scroll)/.test(style.overflowY) && element.scrollHeight > element.clientHeight + 80) return element;
      element = element.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

  function reportProgress(found, total, profiles) {
    chrome.runtime.sendMessage({
      type: "MEDHUNT_PLATFORM_SCAN_PROGRESS",
      platform: PLATFORM.key,
      found,
      total,
      preview: profiles.slice(-5).map(({ name, location, headline }) => ({ name, location, headline })),
    }, () => void chrome.runtime.lastError);
  }

  async function progressiveScan() {
    if (scanInProgress) return { ok: false, error: "A candidate scan is already running." };
    scanInProgress = true;
    const captured = new Map();
    const capturedElements = new Map();
    const container = scrollContainer();
    const originalTop = Number(container?.scrollTop) || 0;
    let expected = 0;
    const merge = async () => {
      const snapshot = await scanSnapshot();
      expected = Math.max(expected, Number(snapshot.expected_count) || 0);
      for (const profile of snapshot.profiles) {
        const key = candidateKey(profile);
        if (!captured.has(key)) captured.set(key, profile);
        const element = lastElements.get(profile.source_id);
        if (element?.isConnected) capturedElements.set(key, element);
      }
      reportProgress(captured.size, expected || captured.size, [...captured.values()]);
    };

    const setTop = (top) => {
      if (typeof container?.scrollTo === "function") container.scrollTo({ top, behavior: "auto" });
      else if (container) container.scrollTop = top;
    };

    try {
      if (originalTop > 0) {
        setTop(0);
        await new Promise((resolve) => setTimeout(resolve, 220));
      }
      await merge();
      let bottomStableRounds = 0;
      let previousCount = captured.size;
      let previousHeight = Number(container?.scrollHeight) || 0;
      for (let step = 0; step < 36 && bottomStableRounds < 4 && captured.size < 100; step += 1) {
        const clientHeight = Math.max(1, Number(container?.clientHeight) || window.innerHeight || 720);
        const beforeHeight = Number(container?.scrollHeight) || clientHeight;
        const maxTop = Math.max(0, beforeHeight - clientHeight);
        const currentTop = Number(container?.scrollTop) || 0;
        const stride = Math.max(320, Math.floor(clientHeight * 0.82));
        const nextTop = Math.min(maxTop, currentTop + stride);
        setTop(nextTop);
        await new Promise((resolve) => setTimeout(resolve, PLATFORM.key === "ziprecruiter" ? 350 : 260));
        await merge();

        const afterHeight = Number(container?.scrollHeight) || beforeHeight;
        const afterMaxTop = Math.max(0, afterHeight - clientHeight);
        const afterTop = Number(container?.scrollTop) || nextTop;
        const atBottom = afterTop >= afterMaxTop - 4;
        const unchanged = captured.size === previousCount && afterHeight === previousHeight;
        bottomStableRounds = atBottom && unchanged ? bottomStableRounds + 1 : 0;
        previousCount = captured.size;
        previousHeight = afterHeight;
      }
      const profiles = [...captured.values()].slice(0, 100).map((profile, index) => ({ ...profile, result_index: index }));
      lastProfiles = profiles;
      lastElements = new Map(profiles.map((profile) => [
        profile.source_id,
        capturedElements.get(candidateKey(profile)),
      ]));
      return {
        ok: true,
        platform: PLATFORM.key,
        platform_label: PLATFORM.label,
        profiles,
        count: profiles.length,
        expected_count: Math.max(expected, profiles.length),
        raw: { cards: profiles.length, names: profiles.length },
        page_url: location.href,
      };
    } finally {
      setTop(originalTop);
      scanInProgress = false;
      observeChanges();
    }
  }

  function openCandidate(index) {
    const profile = lastProfiles[Number(index)];
    if (!profile) return { ok: false, error: "That displayed candidate is no longer available." };
    const element = lastElements.get(profile.source_id);
    const link = element?.matches?.("a[href]") ? element : element?.querySelector?.("a[href]");
    if (link) {
      link.scrollIntoView({ block: "center", behavior: "auto" });
      link.click();
      return { ok: true, source_url: absoluteUrl(link.getAttribute("href")) };
    }
    if (element) {
      element.scrollIntoView({ block: "center", behavior: "auto" });
      element.click();
      return { ok: true, source_url: profile.source_url };
    }
    if (profile.source_url && profile.source_url !== location.href) {
      location.href = profile.source_url;
      return { ok: true, source_url: profile.source_url };
    }
    return { ok: false, error: `${PLATFORM.label} did not expose a clickable candidate card.` };
  }

  async function captureProfile() {
    const snapshot = await scanSnapshot();
    const currentId = new URL(location.href).searchParams.get("encryptedJobseekerId");
    const profile = snapshot.profiles.find((item) => item.source_id === currentId) ||
      (snapshot.profiles.length === 1 ? snapshot.profiles[0] : null);
    if (!profile) {
      return { ok: false, error: `Open one ${PLATFORM.label} candidate profile, then try Capture again.` };
    }
    return { ok: true, platform: PLATFORM.key, profile: { ...profile, captured_at: new Date().toISOString() } };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    const messageType = message?.type === ADAPTER_REQUEST ? message.original_type : message?.type;
    const respond = (payload) => sendResponse({ ...payload, adapter_revision: ADAPTER_REVISION });
    if (messageType === "MEDHUNT_PLATFORM_PING") {
      respond({ ok: true, platform: PLATFORM.key, label: PLATFORM.label, url: location.href });
      return false;
    }
    if (messageType === "MEDHUNT_CAPTURE_PLATFORM_PROFILE") {
      captureProfile().then(respond).catch((error) => respond({ ok: false, error: String(error?.message || error) }));
      return true;
    }
    if (messageType === "MEDHUNT_LIST_PLATFORM_CANDIDATES") {
      scanSnapshot().then(respond).catch((error) => respond({ ok: false, error: String(error?.message || error) }));
      return true;
    }
    if (messageType === "MEDHUNT_SCAN_PLATFORM_CANDIDATES") {
      progressiveScan().then(respond).catch((error) => respond({ ok: false, error: String(error?.message || error) }));
      return true;
    }
    if (messageType === "MEDHUNT_OPEN_PLATFORM_CANDIDATE") {
      respond(openCandidate(message.index));
      return false;
    }
    return false;
  });

  let mutationTimer = null;
  let lastSignature = "";
  const observeChanges = () => {
    if (scanInProgress) return;
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(async () => {
      const snapshot = await scanSnapshot().catch(() => null);
      const signature = snapshot?.profiles?.map(candidateKey).join("|") || "";
      if (signature === lastSignature) return;
      const hadResults = Boolean(lastSignature);
      lastSignature = signature;
      if (!signature && !hadResults) return;
      chrome.runtime.sendMessage({
        type: "MEDHUNT_PLATFORM_RESULTS_CHANGED",
        platform: PLATFORM.key,
        count: snapshot?.profiles?.length || 0,
        page_url: location.href,
      }, () => void chrome.runtime.lastError);
    }, 1200);
  };
  new MutationObserver(observeChanges).observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    characterData: true,
    attributeFilter: [
      "aria-hidden", "hidden", "style", "class", "data-state", "data-status", "data-stale",
      "data-candidate-id", "data-qa",
    ],
  });
  addEventListener("popstate", observeChanges);
  addEventListener("hashchange", observeChanges);
  let observedResultsUrl = location.href;
  setInterval(() => {
    if (location.href === observedResultsUrl) return;
    observedResultsUrl = location.href;
    lastSignature = "";
    observeChanges();
  }, 1500);
  observeChanges();
})();
