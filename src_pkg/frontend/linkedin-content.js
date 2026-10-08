(() => {
  "use strict";

  const ADAPTER_REVISION = "linkedin-capture-v4";
  const ADAPTER_REQUEST = "RADIXSOL_LINKEDIN_V2_REQUEST";
  if (window.__radixsolLinkedinAdapterRevision === ADAPTER_REVISION) return;
  window.__radixsolLinkedinAdapterRevision = ADAPTER_REVISION;
  window.__radixsolLinkedinCaptureLoaded = true;

  const PLATFORM = { key: "linkedin", label: "LinkedIn" };
  const RESULT_CARD_SELECTOR = [
    "li.reusable-search__result-container",
    ".reusable-search__result-container",
    "[data-view-name='search-entity-result-universal-template']",
    "[data-view-name='people-search-result']",
    "[data-chameleon-result-urn]",
    "[data-entity-urn*='urn:li:fsd_profile']",
  ].join(",");
  const PROFESSIONAL_CREDENTIAL = /^(?:RN|LPN|LVN|APRN|NP|CNP|FNP|FNP-C|AGNP|AGNP-C|CRNA|CNS|PA-C|MD|DO|DDS|DMD|PharmD|RPh|PT|DPT|OT|OTR|OTR\/L|SLP|CCC-SLP|CNA|CST|CNOR|PCCN|PHN|BSN|MSN|DNP|ADN|ASN|AAS|BScN|MBA|MPH|MHA|PhD|EdD|PMP|SHRM-CP|SHRM-SCP)$/i;
  const CREDENTIAL_SEQUENCE = /^(?:(?:RN|LPN|LVN|APRN|NP|CNP|FNP(?:-C)?|AGNP(?:-C)?|CRNA|CNS|PA-C|MD|DO|DDS|DMD|PharmD|RPh|PT|DPT|OT|OTR(?:\/L)?|SLP|CCC-SLP|CNA|CST|CNOR|PCCN|PHN|BSN|MSN|DNP|ADN|ASN|AAS|BScN|MBA|MPH|MHA|PhD|EdD|PMP|SHRM-CP|SHRM-SCP)\s*(?:[,/]|[\u00b7\u2022])?\s*)+$/i;

  function clean(value) {
    return String(value ?? "")
      .replace(/\u00a0/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function isVisible(element) {
    if (!element?.isConnected) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity || 1) > 0 && rect.width >= 4 && rect.height >= 4;
  }

  async function trustedClick(element, options = {}) {
    if (!isVisible(element)) return Promise.resolve({ ok: false, error: "The LinkedIn action is not visible." });
    if (options.scroll !== false) {
      element.scrollIntoView({ block: "center", inline: "center", behavior: "auto" });
      // LinkedIn's hydrated profile header can replace its action buttons
      // after a scroll. Measure the live element only after layout settles.
      await pause(100);
    }
    if (!isVisible(element)) {
      return { ok: false, error: "The LinkedIn action changed while the profile was loading." };
    }
    try { element.focus({ preventScroll: true }); } catch { /* no-op */ }
    const rect = element.getBoundingClientRect();
    const x = Math.round(rect.left + (rect.width / 2));
    const y = Math.round(rect.top + (rect.height / 2));
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(
        { type: "RADIXSOL_TRUSTED_LINKEDIN_CLICK", x, y },
        (response) => {
          if (chrome.runtime.lastError) {
            resolve({ ok: false, error: chrome.runtime.lastError.message });
          } else {
            resolve(response || { ok: false, error: "The LinkedIn action did not respond." });
          }
        },
      );
    });
  }

  const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

  function unique(values) {
    const seen = new Set();
    return (values || []).map(clean).filter((value) => {
      const key = value.toLowerCase();
      if (!value || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function textLines(root = document) {
    return String(root?.innerText || root?.textContent || "")
      .split(/[\r\n]+/)
      .map(clean)
      .filter(Boolean);
  }

  function looksLikePersonName(value) {
    const name = clean(value)
      .replace(/^\(\d+\)\s*/, "")
      .replace(/\s+[^\s]*\s*(?:1st|2nd|3rd)(?:\s+degree)?\b.*$/i, "")
      .replace(/\s*[·•]\s*(?:1st|2nd|3rd)(?:\s+degree)?\b.*$/i, "")
      .replace(/\s*[·•]\s*verified\b.*$/i, "")
      .trim();
    const words = name.split(/\s+/).filter(Boolean);
    return name.length >= 3 && name.length <= 90 && words.length >= 2 && words.length <= 8 &&
      /^[\p{L}\p{M} .,'’\-]+$/u.test(name) &&
      !CREDENTIAL_SEQUENCE.test(name) &&
      !/\b(?:linkedin|profile|registered nurse|recruiter|engineer|manager|contact info|connections?|followers?|university|hospital|people|jobs?|posts?|newsletter|company|school)\b/i.test(name) &&
      !/^(?:view|open|follow|connect|message|more|see all|show all)\b/i.test(name);
  }

  function stripProfessionalCredentials(value) {
    const parts = clean(value).split(",").map(clean);
    while (parts.length > 1 && PROFESSIONAL_CREDENTIAL.test(parts[parts.length - 1])) parts.pop();
    let result = parts.join(", ");
    const trailing = result.split(/\s+/);
    while (trailing.length > 1 && PROFESSIONAL_CREDENTIAL.test(trailing[trailing.length - 1].replace(/,$/, ""))) {
      trailing.pop();
    }
    return trailing.join(" ").replace(/[,\s]+$/, "");
  }

  function normalizedName(value) {
    const cleaned = stripProfessionalCredentials(clean(value)
      .replace(/^\(\d+\)\s*/, "")
      .replace(/\s*\([^()]{1,100}\)\s*/g, " ")
      .replace(/\s+[^\s]*\s*(?:1st|2nd|3rd)(?:\s+degree)?\b.*$/i, "")
      .replace(/\s*\|\s*LinkedIn.*$/i, "")
      .replace(/\s+(?:she\s*\/\s*her|he\s*\/\s*him|they\s*\/\s*them)\b.*$/i, "")
      .replace(/\s*[·•]\s*(?:1st|2nd|3rd)(?:\s+degree)?\b.*$/i, "")
      .replace(/\s*[·•]\s*verified\b.*$/i, "")
      .trim());
    if (looksLikePersonName(cleaned)) return cleaned;
    const beforeRole = cleaned.split(/\s+[-–—]\s+/)[0]?.trim() || "";
    return looksLikePersonName(beforeRole) ? beforeRole : "";
  }

  function metadataValue(selectors) {
    for (const selector of selectors) {
      const value = clean(document.querySelector(selector)?.getAttribute("content"));
      if (value) return value;
    }
    return "";
  }

  function profileIdentity(value) {
    try {
      const parsed = new URL(value, location.origin);
      if (
        parsed.protocol !== "https:" ||
        !(parsed.hostname === "linkedin.com" || parsed.hostname.endsWith(".linkedin.com"))
      ) return null;
      const match = parsed.pathname.match(/^\/in\/([^/?#]+)/i);
      if (!match) return null;
      const slug = clean(decodeURIComponent(match[1]));
      if (!slug || slug.length > 180 || /[\s\\/?#]/.test(slug) || /^(?:undefined|null|me)$/i.test(slug)) return null;
      return {
        id: clean(slug).toLowerCase(),
        url: `${parsed.origin}/in/${encodeURIComponent(slug)}/`,
      };
    } catch {
      return null;
    }
  }

  function normalizedProfileUrl() {
    return profileIdentity(location.href)?.url || "";
  }

  function sourceId() {
    return profileIdentity(location.href)?.id || "";
  }

  function jsonLdPerson() {
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(script.textContent || "null");
        const roots = Array.isArray(parsed) ? parsed : [parsed];
        const values = roots.flatMap((value) => [
          value,
          ...(Array.isArray(value?.["@graph"]) ? value["@graph"] : []),
        ]);
        const person = values.find((value) => {
          const type = value?.["@type"];
          return type === "Person" || (Array.isArray(type) && type.includes("Person"));
        });
        if (person) return person;
      } catch {
        // Ignore malformed or unrelated structured data.
      }
    }
    return {};
  }

  function firstText(selectors, root = document) {
    for (const selector of selectors) {
      let elements = [];
      try { elements = Array.from(root.querySelectorAll(selector)); } catch {}
      for (const element of elements) {
        const value = clean(element.innerText || element.textContent);
        if (value) return value;
      }
    }
    return "";
  }

  function profileHeader() {
    const main = document.querySelector("main") || document;
    const selectors = [
      "h1.text-heading-xlarge",
      ".text-heading-xlarge",
      ".pv-text-details__left-panel h1",
      "[data-view-name='profile-card'] h1",
      "[data-view-name='profile-card'] h2",
      "[data-view-name='profile-card'] [class*='name']",
      ".artdeco-entity-lockup__title",
      "h1",
    ];
    let heading = null;
    for (const selector of selectors) {
      heading = Array.from(main.querySelectorAll(selector))
        .find((element) => normalizedName(element.innerText || element.textContent));
      if (heading) break;
    }
    const section = heading?.closest("section") || heading?.parentElement?.parentElement || main;
    return { main, heading, section };
  }

  function metadataName(structured = {}) {
    const candidates = [
      structured.name,
      metadataValue(["meta[property='og:title']", "meta[name='twitter:title']"]),
      document.title,
    ];
    return candidates.map(normalizedName).find(Boolean) || "";
  }

  function profileLines(main, section) {
    const local = textLines(section);
    return local.length >= 2 ? local : textLines(main).slice(0, 120);
  }

  function headlineFromLines(lines, name) {
    const nameIndex = lines.findIndex((line) => normalizedName(line) === name);
    if (nameIndex < 0) return "";
    return lines.slice(nameIndex + 1, nameIndex + 9).find((line) => (
      line.length >= 3 && line.length <= 220 &&
      !normalizedName(line) &&
      !/^(?:1st|2nd|3rd|message|more|connect|follow|contact info)$/i.test(line) &&
      !/\b(?:contact info|connections?|followers?|verification|verify now|profile views|university)\b/i.test(line)
    )) || "";
  }

  function normalizeLocation(value) {
    return clean(value)
      .replace(/^location\s*:\s*/i, "")
      .replace(/\s*[·•]\s*contact info\b.*$/i, "")
      .trim();
  }

  function usableLocation(value, name, headline, { trusted = false } = {}) {
    const candidate = normalizeLocation(value);
    if (!candidate || candidate.length > 120) return "";

    const lower = candidate.toLowerCase();
    const normalizedCandidateName = clean(name).toLowerCase();
    const normalizedHeadline = clean(headline).toLowerCase();
    if (
      /^[·•]$/.test(candidate) ||
      lower === normalizedCandidateName ||
      lower === normalizedHeadline ||
      (normalizedCandidateName && lower.includes(normalizedCandidateName)) ||
      /\b(?:1st|2nd|3rd)(?:\s+degree)?\b/i.test(candidate) ||
      /\b(?:she\s*\/\s*her|he\s*\/\s*him|they\s*\/\s*them|followers?|connections?|contact info|mutual connection)\b/i.test(candidate) ||
      /^(?:follow|message|more|connect|open to work)$/i.test(candidate) ||
      /\b(?:registered nurse|licensed practical nurse|engineer|developer|recruiter|manager|director|specialist|coordinator|consultant|analyst|technician|physician|therapist)\b/i.test(candidate) ||
      /\s+at\s+\S/i.test(candidate) ||
      /\b(?:incorporated|inc\.?|llc|ltd\.?|corp(?:oration)?|company|hospital|health|healthcare|medical|care center|clinic|group|associates|network|foundation|partners|staffing|solutions?|services?|department of|university|college)\b/i.test(candidate)
    ) return "";

    // Values adjacent to LinkedIn's Contact info control and structured
    // address values are authoritative. Generic class selectors must still
    // look location-like so pronouns, degree badges and employers cannot win.
    if (trusted) return candidate;
    return (
      /,/.test(candidate) ||
      /\b(?:greater|area|metropolitan|united states|united kingdom|canada|india|australia)\b/i.test(candidate) ||
      /\b[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$/.test(candidate)
    ) ? candidate : "";
  }

  function locationFromLines(lines, name, headline) {
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const match = line.match(/^(.{2,120}?)\s*[·•]\s*Contact info\b/i);
      if (match) {
        const candidate = usableLocation(match[1], name, headline, { trusted: true });
        if (candidate) return candidate;
      }

      if (/^contact info$/i.test(line)) {
        for (let previous = index - 1; previous >= Math.max(0, index - 3); previous -= 1) {
          if (/^[·•]$/.test(lines[previous])) continue;
          const candidate = usableLocation(lines[previous], name, headline, { trusted: true });
          if (candidate) return candidate;
        }
      }
    }
    return "";
  }

  function locationNearContactInfo(section, name, headline) {
    const controls = Array.from(section.querySelectorAll([
      "#top-card-text-details-contact-info",
      "a[href*='/overlay/contact-info']",
      "a[href*='contact-info']",
      "[data-control-name*='contact']",
    ].join(",")));

    for (const control of controls) {
      let node = control;
      for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
        const previous = usableLocation(
          node.previousElementSibling?.innerText || node.previousElementSibling?.textContent,
          name,
          headline,
          { trusted: true },
        );
        if (previous) return previous;

        const nearby = locationFromLines(textLines(node.parentElement), name, headline);
        if (nearby) return nearby;
        if (node === section) break;
      }
    }
    return "";
  }

  function structuredLocation(structured, name, headline) {
    const address = structured?.address;
    if (!address) return "";
    if (typeof address === "string") {
      return usableLocation(address, name, headline, { trusted: true });
    }
    const country = typeof address.addressCountry === "object"
      ? address.addressCountry?.name
      : address.addressCountry;
    return usableLocation(unique([
      address.addressLocality,
      address.addressRegion,
      country,
    ]).join(", "), name, headline, { trusted: true });
  }

  function sectionNotes(main) {
    const wanted = /^(about|experience|education|skills|licenses\s*&?\s*certifications|certifications|volunteering)$/i;
    const notes = [];
    for (const section of main.querySelectorAll("section")) {
      const heading = firstText(["h2", "h3"], section);
      if (!wanted.test(heading)) continue;
      const text = clean(section.innerText || section.textContent);
      if (!text || text.length < heading.length + 3) continue;
      notes.push(`${heading}:\n${text.slice(0, 3500)}`);
    }
    return unique(notes).join("\n\n").slice(0, 16000);
  }

  function sectionContext(main) {
    const roles = [];
    const employers = [];
    const schools = [];
    const dateLine = /^(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+)?(?:19|20)\d{2}\b/i;
    const schoolWords = /\b(?:university|college|school|academy|institute|polytechnic|conservatory)\b/i;
    const nonSignal = /^(?:(?:experience|education|show all|see more|present)|(?:full-time|part-time|contract|self-employed|freelance|internship)(?:\s*[\u00b7\u2022].*)?)$/i;

    const meaningful = (value) => {
      const text = clean(value).replace(/\s*[\u00b7\u2022]\s*(?:full-time|part-time|contract|self-employed|freelance|internship).*$/i, "");
      if (!text || text.length > 220 || nonSignal.test(text) || dateLine.test(text)) return "";
      return text;
    };

    for (const section of main.querySelectorAll("section")) {
      const heading = firstText(["h2", "h3"], section);
      if (!/^(?:experience|education)$/i.test(heading)) continue;
      const lines = textLines(section).filter((line) => clean(line).toLowerCase() !== heading.toLowerCase());

      if (/^experience$/i.test(heading)) {
        for (const line of lines) {
          const match = clean(line).match(/^(?:current\s*:\s*)?(.{2,120}?)\s+at\s+(.{2,180})$/i);
          if (!match) continue;
          const role = meaningful(match[1]);
          const employer = meaningful(match[2]);
          if (role) roles.push(role);
          if (employer) employers.push(employer);
        }

        // Current LinkedIn profile entries expose their primary and secondary
        // labels separately. Use only those semantic pairs; arbitrary prose in
        // an Experience description must never become identity context.
        for (const entry of section.querySelectorAll([
          "li.artdeco-list__item",
          ".pvs-list__item--line-separated",
          "[data-view-name='profile-component-entity']",
        ].join(","))) {
          const role = meaningful(firstText([
            ".t-bold span[aria-hidden='true']",
            ".t-bold",
            "[data-field='title']",
          ], entry));
          const employer = meaningful(firstText([
            ".t-normal span[aria-hidden='true']",
            ".t-normal",
            "[data-field='company']",
          ], entry));
          if (role && employer && role.toLowerCase() !== employer.toLowerCase()) {
            roles.push(role);
            employers.push(employer);
          }
        }
      } else {
        for (const entry of section.querySelectorAll([
          "li.artdeco-list__item",
          ".pvs-list__item--line-separated",
          "[data-view-name='profile-component-entity']",
        ].join(","))) {
          const school = meaningful(firstText([
            ".t-bold span[aria-hidden='true']",
            ".t-bold",
            "[data-field='school']",
          ], entry));
          if (school) schools.push(school);
        }
        for (const line of lines) {
          const school = meaningful(line.replace(/^school\s*:\s*/i, ""));
          if (school && schoolWords.test(school)) schools.push(school);
        }
      }
    }

    return {
      roles: unique(roles).slice(0, 12),
      employers: unique(employers).slice(0, 12),
      schools: unique(schools).slice(0, 8),
    };
  }

  function readProfile() {
    const url = normalizedProfileUrl();
    const id = sourceId();
    if (!url || !id) return null;

    const structured = jsonLdPerson();
    const { main, heading, section } = profileHeader();
    const name = normalizedName(heading?.innerText || heading?.textContent) || metadataName(structured);
    if (!name) return null;

    const lines = profileLines(main, section);

    const headline = firstText([
      ".text-body-medium.break-words",
      ".text-body-medium",
      "[data-view-name='profile-card'] [class*='headline']",
      "[data-generated-suggestion-target] + div",
    ], section) || clean(structured.jobTitle) || headlineFromLines(lines, name);

    const selectorLocations = [];
    for (const selector of [
      ".pv-text-details__left-panel.mt2 .text-body-small.inline.t-black--light.break-words",
      ".pv-top-card .pv-text-details__left-panel .text-body-small.break-words",
      "[data-view-name='profile-card'] [class*='location']",
      ".top-card-layout__first-subline",
      ".text-body-small.inline.t-black--light.break-words",
      ".text-body-small.t-black--light.break-words",
    ]) {
      let elements = [];
      try { elements = Array.from(section.querySelectorAll(selector)); } catch {}
      for (const element of elements) {
        const candidate = usableLocation(element.innerText || element.textContent, name, headline);
        if (candidate) selectorLocations.push(candidate);
      }
    }

    const fallbackCandidates = Array.from(section.querySelectorAll("span, div"))
      .map((element) => usableLocation(element.innerText || element.textContent, name, headline))
      .filter(Boolean);
    const locationText = locationNearContactInfo(section, name, headline) ||
      locationFromLines(lines, name, headline) ||
      selectorLocations[0] ||
      structuredLocation(structured, name, headline) ||
      fallbackCandidates[0] || "";

    const details = sectionNotes(main);
    const capturedContext = sectionContext(main);
    const headlineContext = roleAndEmployer(headline);
    const roles = unique([headlineContext.role, ...capturedContext.roles]).slice(0, 12);
    const employers = unique([headlineContext.employer, ...capturedContext.employers]).slice(0, 12);
    const schools = capturedContext.schools.slice(0, 8);
    const notes = unique([
      `LinkedIn profile: ${url}`,
      headline ? `Headline: ${headline}` : "",
      ...roles.map((value) => `Role: ${value}`),
      ...employers.map((value) => `Employer: ${value}`),
      ...schools.map((value) => `School: ${value}`),
      locationText ? `Location: ${locationText}` : "",
      details,
    ]).join("\n\n").slice(0, 20000);

    return {
      name,
      location: locationText,
      headline,
      source: PLATFORM.key,
      source_url: url,
      source_id: id,
      notes,
      roles,
      employers,
      schools,
      result_index: 0,
      captured_at: new Date().toISOString(),
    };
  }

  function isDisplayed(element) {
    if (!element?.isConnected) return false;
    if (element.hidden || element.closest?.("[hidden], [inert], [aria-hidden='true']")) return false;
    const style = getComputedStyle(element);
    if (
      style.display === "none" ||
      style.visibility === "hidden" ||
      style.visibility === "collapse" ||
      Number(style.opacity) === 0
    ) return false;
    return element.getClientRects().length > 0;
  }

  function peopleResultPage() {
    return /^\/search\/results\/people(?:\/|$)/i.test(location.pathname);
  }

  function broadResultPage() {
    return /^\/search\/results\/all(?:\/|$)/i.test(location.pathname) ||
      /^\/search\/results\/?$/i.test(location.pathname) ||
      /^\/search\/?$/i.test(location.pathname);
  }

  function resultPage() {
    return peopleResultPage() || broadResultPage();
  }

  function linkIdentity(link) {
    return profileIdentity(link?.getAttribute?.("href") || link?.href || "");
  }

  function escapedRegex(value) {
    return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function profileLinkLabel(link) {
    const values = [
      clean(link?.getAttribute?.("aria-label"))
        .replace(/^(?:view|open)\s+/i, "")
        .replace(/[\u2019']s\s+profile.*$/i, "")
        .replace(/\s+profile$/i, ""),
      ...textLines(link),
      clean(link?.querySelector?.("img")?.getAttribute("alt")),
    ];
    return values.map(normalizedName).find(Boolean) || "";
  }

  function isMutualConnectionLink(link) {
    const boundary = link?.closest?.(RESULT_CARD_SELECTOR);
    const linkedName = profileLinkLabel(link);
    let element = link;
    for (let depth = 0; element && element !== boundary && depth < 5; depth += 1) {
      if (depth > 0 && element.matches?.("main, li, [role='listitem']")) break;
      const text = clean(element.innerText || element.textContent);
      if (text.length <= 320 && /\bmutual connections?\b/i.test(text)) {
        const linkedPersonIsSubject = linkedName && textLines(element).some((line) => (
          new RegExp(`^${escapedRegex(linkedName)}\\s+is\\s+(?:a\\s+)?mutual connections?\\b`, "i").test(line)
        ));
        if (linkedPersonIsSubject) return true;

        // Some LinkedIn variants show only "2 mutual connections" next to
        // avatar links. Accept that small local region only when it contains
        // no candidate location/current-role evidence.
        const includesCandidateEvidence = textLines(element).some((line) => (
          resultLocationCandidate(line) || /^(?:current|past)\s*:/i.test(line)
        )) || Boolean(element.querySelector?.(
          ".entity-result__primary-subtitle, .entity-result__secondary-subtitle, [class*='location']",
        ));
        if (!includesCandidateEvidence) return true;
        return false;
      }
      element = element.parentElement;
    }
    return false;
  }

  function identitiesWithin(element) {
    return new Set(
      Array.from(element?.querySelectorAll?.("a[href*='/in/']") || [])
        .filter((link) => !isMutualConnectionLink(link))
        .map(linkIdentity)
        .filter(Boolean)
        .map((identity) => identity.id),
    );
  }

  function resultLocationCandidate(value, name = "", headline = "", options = {}) {
    const candidate = usableLocation(value, name, headline, options);
    if (!candidate) return "";
    if (/^(?:current|past)\s*:/i.test(candidate)) return "";
    if (/^(?:(?:RN|LPN|LVN|APRN|NP|CNP|FNP|CRNA|CNS|PA-C|MD|DO|DDS|DMD|PharmD|RPh|PT|DPT|OT|OTR(?:\/L)?|SLP|CCC-SLP|CNA|CST|CNOR|BSN|MSN|DNP|ADN|ASN|AAS|BScN|MBA|MPH|MHA|PhD|EdD|PHN|PMP|SHRM-CP|SHRM-SCP)\s*(?:[,/]|[\u00b7\u2022])?\s*)+$/i.test(candidate)) return "";
    return candidate;
  }

  function explicitPeopleEvidence(card, link) {
    const semanticRoot = card?.closest?.([
      "[data-view-name]",
      "[data-entity-urn]",
      "[data-chameleon-result-urn]",
      "li.reusable-search__result-container",
    ].join(",")) || card;
    const viewName = clean(semanticRoot?.getAttribute?.("data-view-name"));
    const urn = clean([
      semanticRoot?.getAttribute?.("data-entity-urn"),
      semanticRoot?.getAttribute?.("data-chameleon-result-urn"),
    ].filter(Boolean).join(" "));
    if (/\b(?:people|person|profile)\b/i.test(viewName)) return true;
    if (/urn:li:(?:fsd_profile|member|profile):?/i.test(urn)) return true;

    const section = card?.closest?.("section");
    const sectionHeading = firstText(["h1", "h2", "h3"], section || document.createElement("div"));
    if (/^people(?:\s+results?)?$/i.test(sectionHeading)) return true;

    // LinkedIn's universal search uses this semantic result structure for a
    // person even when data-view-name itself remains generic. Post-author and
    // article links use different actor classes and must not satisfy it.
    return Boolean(
      card?.matches?.("li.reusable-search__result-container, .reusable-search__result-container") &&
      link?.closest?.(".entity-result__title-text, .entity-result__title-line, [class*='entity-result__title']") &&
      card.querySelector?.(".entity-result__primary-subtitle, [class*='entity-result__primary-subtitle']"),
    );
  }

  function subjectLinkEvidence(link, card) {
    if (!link || !card || !isDisplayed(link)) return false;
    if (link.closest?.([
      ".entity-result__title-text",
      ".entity-result__title-line",
      "[class*='entity-result__title']",
      "[data-view-name='people-search-result']",
      "[data-view-name*='profile']",
      "h2",
      "h3",
    ].join(","))) return true;
    if (/\b(?:view|open)\s+.+(?:'s|\u2019s)\s+profile\b/i.test(clean(link.getAttribute?.("aria-label")))) return true;
    return peopleResultPage() && Boolean(profileLinkLabel(link));
  }

  function plausibleResultCard(card, link, identity) {
    if (!card || !identity || !isDisplayed(card) || !isDisplayed(link)) return false;
    const identities = identitiesWithin(card);
    if (identities.size !== 1 || !identities.has(identity.id)) return false;
    if (broadResultPage() && !explicitPeopleEvidence(card, link)) return false;
    if (broadResultPage() && card.matches?.("article") && !/\bpeople\b/i.test(clean(card.getAttribute?.("data-view-name")))) {
      return false;
    }

    const name = nameFromResultCard(card, identity);
    if (!name || !subjectLinkEvidence(link, card)) return false;
    const lines = textLines(card);
    const hasSemanticDetail = Boolean(card.querySelector?.([
      ".entity-result__primary-subtitle",
      ".entity-result__secondary-subtitle",
      "[class*='entity-result__primary-subtitle']",
      "[class*='entity-result__secondary-subtitle']",
      "[data-radixsol-field='headline']",
      "[data-radixsol-field='location']",
      "[class*='location']",
    ].join(","))) || lines.some((line) => (
      resultLocationCandidate(line, name) ||
      /^(?:current|past)\s*:/i.test(line) ||
      /\b(?:nurse|engineer|developer|manager|director|recruiter|specialist|coordinator|consultant|analyst|technician|physician|therapist)\b/i.test(line)
    ));
    if (!hasSemanticDetail) return false;

    const cardText = clean(card.innerText || card.textContent);
    if (broadResultPage() && /\b(?:like|comment|repost|send)\b.*\b(?:like|comment|repost|send)\b/i.test(cardText)) {
      return false;
    }
    return cardText.length <= 5000;
  }

  function resultCardFor(link) {
    const identity = linkIdentity(link);
    if (!identity) return null;
    const explicit = link.closest(RESULT_CARD_SELECTOR);
    if (explicit && plausibleResultCard(explicit, link, identity)) return explicit;

    const main = link.closest("main") || document.querySelector("main") || document.body;
    let element = link.parentElement;
    while (element && element !== main.parentElement) {
      if (isDisplayed(element)) {
        const identities = identitiesWithin(element);
        const lines = textLines(element);
        const hasCandidateDetail = lines.some((line) => (
          resultLocationCandidate(line) ||
          /^(?:current|past)\s*:/i.test(line) ||
          /\b(?:nurse|engineer|developer|manager|director|recruiter|specialist|coordinator|consultant|analyst|technician|physician|therapist|at\s+.+)$/i.test(line)
        ));
        if (
          identities.size === 1 &&
          lines.length >= 2 &&
          hasCandidateDetail &&
          lines.join(" ").length <= 3500 &&
          plausibleResultCard(element, link, identity)
        ) {
          return element;
        }
        if (identities.size > 1) break;
      }
      if (element === main) break;
      element = element.parentElement;
    }
    return null;
  }

  function nameFromResultCard(card, identity) {
    const candidates = [];
    for (const link of card.querySelectorAll("a[href*='/in/']")) {
      if (linkIdentity(link)?.id !== identity.id) continue;
      const label = clean(link.getAttribute("aria-label"))
        .replace(/^(?:view|open)\s+/i, "")
        .replace(/[\u2019']s\s+profile.*$/i, "")
        .replace(/\s+profile$/i, "");
      if (label) candidates.push(label);
      candidates.push(...textLines(link));
    }
    for (const selector of [
      ".entity-result__title-text",
      ".entity-result__title-line",
      "[class*='entity-result__title']",
      "[data-view-name*='search-result'] h2",
      "h2", "h3",
    ]) {
      for (const element of card.querySelectorAll(selector)) candidates.push(...textLines(element));
    }
    candidates.push(...textLines(card).slice(0, 5));
    for (const candidate of candidates) {
      const name = normalizedName(candidate);
      if (name && !/^(?:linkedin member|view profile|profile)$/i.test(name)) return name;
    }
    return "";
  }

  function resultCardLocation(card, name, headline) {
    const trustedSelectors = [
      ".entity-result__secondary-subtitle",
      "[class*='entity-result__secondary-subtitle']",
      "[data-view-name*='search-result'] [class*='location']",
      "[data-view-name='people-search-result'] [class*='location']",
      "[data-radixsol-field='location']",
    ];
    for (const selector of trustedSelectors) {
      for (const element of card.querySelectorAll(selector)) {
        const candidate = resultLocationCandidate(element.innerText || element.textContent, name, headline, { trusted: true });
        if (candidate) return candidate;
      }
    }

    const genericSelectors = [
      ".t-14.t-normal.t-black--light",
      ".t-black--light",
      "[class*='secondary-subtitle']",
    ];
    for (const selector of genericSelectors) {
      for (const element of card.querySelectorAll(selector)) {
        const candidate = resultLocationCandidate(element.innerText || element.textContent, name, headline);
        if (candidate) return candidate;
      }
    }

    const lines = textLines(card);
    const nameIndex = lines.findIndex((line) => normalizedName(line) === name);
    const start = nameIndex >= 0 ? nameIndex + 1 : 0;
    for (const line of lines.slice(start, start + 10)) {
      const candidate = resultLocationCandidate(line, name, headline);
      if (candidate) return candidate;
    }
    return "";
  }

  function usableResultHeadline(value, name, locationText) {
    const candidate = clean(value).replace(/^headline\s*:\s*/i, "");
    if (!candidate || candidate.length > 240) return "";
    if (normalizedName(candidate) === name || normalizeLocation(candidate) === normalizeLocation(locationText)) return "";
    if (/^(?:current|past)\s*:|^(?:connect|follow|message|more)$/i.test(candidate)) return "";
    if (/\b(?:mutual connection|followers?|connections?)\b/i.test(candidate)) return "";
    return candidate;
  }

  function resultCardHeadline(card, name, locationText) {
    for (const selector of [
      ".entity-result__primary-subtitle",
      "[class*='entity-result__primary-subtitle']",
      "[data-view-name*='search-result'] [class*='headline']",
      "[data-radixsol-field='headline']",
    ]) {
      for (const element of card.querySelectorAll(selector)) {
        const candidate = usableResultHeadline(element.innerText || element.textContent, name, locationText);
        if (candidate) return candidate;
      }
    }

    const lines = textLines(card);
    const nameIndex = lines.findIndex((line) => normalizedName(line) === name);
    const start = nameIndex >= 0 ? nameIndex + 1 : 0;
    for (const line of lines.slice(start, start + 7)) {
      const candidate = usableResultHeadline(line, name, locationText);
      if (!candidate || resultLocationCandidate(candidate, name, "")) continue;
      return candidate;
    }
    return "";
  }

  function currentResultDetail(card) {
    for (const line of textLines(card)) {
      const match = line.match(/^current\s*:\s*(.+)$/i);
      if (match?.[1]) return clean(match[1]);
    }
    return "";
  }

  function roleAndEmployer(value) {
    const candidate = clean(value).replace(/^(?:current|past)\s*:\s*/i, "");
    if (!candidate) return { role: "", employer: "" };
    const parts = candidate.split(/\s+at\s+/i);
    if (parts.length < 2) {
      // A no-"at" headline is a role only when it looks occupational. This
      // keeps a bare company such as "Acme Health, Inc." from being copied
      // into either role or employer while retaining titles like "ICU RN".
      if (
        CREDENTIAL_SEQUENCE.test(candidate) ||
        /\b(?:incorporated|inc\.?|llc|ltd\.?|corp(?:oration)?|company|hospital|healthcare|medical center|clinic|solutions?|services?)\b/i.test(candidate)
      ) return { role: "", employer: "" };
      return { role: candidate, employer: "" };
    }
    const role = clean(parts.shift());
    const employer = clean(parts.join(" at "));
    return {
      role: CREDENTIAL_SEQUENCE.test(role) ? "" : role,
      employer: employer === role ? "" : employer,
    };
  }

  function readSearchResult(card, identity, index) {
    const name = nameFromResultCard(card, identity);
    if (!name) return null;

    // Headline and location influence one another's rejection rules, so first
    // use the semantic LinkedIn fields and then fill either missing value from
    // the ordered visible lines.
    let headline = resultCardHeadline(card, name, "");
    let locationText = resultCardLocation(card, name, headline);
    if (!headline) headline = resultCardHeadline(card, name, locationText);
    if (!locationText) locationText = resultCardLocation(card, name, headline);

    const current = currentResultDetail(card);
    const currentParts = roleAndEmployer(current);
    const headlineParts = roleAndEmployer(headline);
    const role = currentParts.role || headlineParts.role;
    const employer = currentParts.employer || headlineParts.employer;
    const roles = unique([role]).slice(0, 12);
    const employers = unique([employer]).slice(0, 12);
    const notes = unique([
      `LinkedIn profile: ${identity.url}`,
      headline ? `Headline: ${headline}` : "",
      current ? `Current: ${current}` : "",
      ...roles.map((value) => `Role: ${value}`),
      ...employers.map((value) => `Employer: ${value}`),
      locationText ? `Location: ${locationText}` : "",
    ]).join("\n\n").slice(0, 20000);

    return {
      name,
      location: locationText,
      headline,
      source: PLATFORM.key,
      source_url: identity.url,
      source_id: identity.id,
      notes,
      roles,
      employers,
      schools: [],
      result_index: index,
      captured_at: new Date().toISOString(),
    };
  }

  let lastProfiles = [];
  let lastElements = new Map();
  let scanInProgress = false;
  let accumulatedSearchKey = "";
  let accumulatedSearchProfiles = new Map();
  let accumulatedSearchElements = new Map();

  function currentSearchKey() {
    return resultPage() ? `${location.pathname}${location.search}` : "";
  }

  function visibleSearchResults() {
    if (!resultPage()) return { profiles: [], elements: new Map() };
    const main = document.querySelector("main") || document.body;
    const visibleProfiles = [];
    const seen = new Set();
    const visibleElements = new Map();
    for (const link of main.querySelectorAll("a[href*='/in/']")) {
      if (!isDisplayed(link) || isMutualConnectionLink(link)) continue;
      const identity = linkIdentity(link);
      if (!identity || seen.has(identity.id)) continue;
      const card = resultCardFor(link);
      if (!card) continue;
      const profile = readSearchResult(card, identity, visibleProfiles.length);
      if (!profile) continue;
      seen.add(identity.id);
      visibleProfiles.push(profile);
      visibleElements.set(identity.id, card);
    }
    return { profiles: visibleProfiles, elements: visibleElements };
  }

  function readSearchResults() {
    const visible = visibleSearchResults();
    const visibleProfiles = visible.profiles;
    const visibleElements = visible.elements;

    const searchKey = currentSearchKey();
    if (searchKey !== accumulatedSearchKey) {
      accumulatedSearchKey = searchKey;
      accumulatedSearchProfiles = new Map();
      accumulatedSearchElements = new Map();
    }
    for (const profile of visibleProfiles) {
      accumulatedSearchProfiles.set(candidateKey(profile), profile);
      accumulatedSearchElements.set(profile.source_id, visibleElements.get(profile.source_id));
    }
    const profiles = [...accumulatedSearchProfiles.values()].slice(0, 100)
      .map((profile, index) => ({ ...profile, result_index: index }));
    lastProfiles = profiles;
    lastElements = new Map(profiles.map((profile) => [
      profile.source_id,
      accumulatedSearchElements.get(profile.source_id),
    ]));
    return profiles;
  }

  function candidateKey(profile) {
    return profile.source_id || `${profile.name.toLowerCase()}|${profile.location.toLowerCase()}`;
  }

  function searchScrollContainer() {
    const first = [...lastElements.values()].find((element) => element?.isConnected) ||
      document.querySelector(RESULT_CARD_SELECTOR);
    let element = first?.parentElement;
    while (element && element !== document.body) {
      const style = getComputedStyle(element);
      if (/(auto|scroll)/.test(style.overflowY) && element.scrollHeight > element.clientHeight + 80) return element;
      element = element.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

  function reportScanProgress(profiles) {
    chrome.runtime.sendMessage({
      type: "RADIXSOL_PLATFORM_SCAN_PROGRESS",
      platform: PLATFORM.key,
      found: profiles.length,
      total: profiles.length,
      preview: profiles.slice(-5).map(({ name, location, headline }) => ({ name, location, headline })),
    }, () => void chrome.runtime.lastError);
  }

  async function progressiveSearchScan() {
    if (!resultPage()) return snapshot();
    if (scanInProgress) return { ok: false, error: "A LinkedIn candidate scan is already running." };
    scanInProgress = true;
    // An explicit scan is a fresh census. LIST/mutation reads preserve the
    // accumulated virtualized set, but Refresh must be able to remove stale
    // rows even when LinkedIn keeps the same search URL.
    accumulatedSearchKey = currentSearchKey();
    accumulatedSearchProfiles = new Map();
    accumulatedSearchElements = new Map();
    lastProfiles = [];
    lastElements = new Map();
    const captured = new Map();
    const capturedElements = new Map();
    let container = null;
    let originalTop = 0;

    const merge = () => {
      const current = readSearchResults();
      for (const profile of current) {
        const key = candidateKey(profile);
        if (!captured.has(key)) captured.set(key, profile);
        const element = lastElements.get(profile.source_id);
        if (element) capturedElements.set(key, element);
      }
      reportScanProgress([...captured.values()]);
    };

    try {
      merge();
      container = searchScrollContainer();
      originalTop = Number(container?.scrollTop) || 0;
      if (typeof container?.scrollTo === "function") container.scrollTo({ top: 0, behavior: "auto" });
      else if (container) container.scrollTop = 0;
      // LinkedIn virtualizes People results, so give the top cards time to
      // mount before beginning the downward accumulation pass.
      await new Promise((resolve) => setTimeout(resolve, 320));
      merge();
      let stableAtBottom = 0;
      for (let step = 0; step < 24 && captured.size < 100; step += 1) {
        const viewport = Math.max(500, Number(container?.clientHeight) || window.innerHeight || 800);
        const maxTop = Math.max(0, Number(container?.scrollHeight || 0) - viewport);
        const currentTop = Number(container?.scrollTop) || 0;
        if (maxTop <= 0) break;
        const nextTop = Math.min(maxTop, currentTop + Math.round(viewport * 0.82));
        if (typeof container?.scrollTo === "function") container.scrollTo({ top: nextTop, behavior: "auto" });
        else if (container) container.scrollTop = nextTop;
        const before = captured.size;
        await new Promise((resolve) => setTimeout(resolve, 380));
        merge();
        const updatedMax = Math.max(0, Number(container?.scrollHeight || 0) - viewport);
        const atBottom = nextTop >= updatedMax - 3;
        stableAtBottom = atBottom && captured.size === before ? stableAtBottom + 1 : 0;
        if (stableAtBottom >= 3) break;
      }

      const profiles = [...captured.values()].slice(0, 100)
        .map((profile, index) => ({ ...profile, result_index: index }));
      accumulatedSearchKey = currentSearchKey();
      accumulatedSearchProfiles = new Map(profiles.map((profile) => [candidateKey(profile), profile]));
      accumulatedSearchElements = new Map(profiles.map((profile) => [
        profile.source_id,
        capturedElements.get(candidateKey(profile)),
      ]));
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
        expected_count: profiles.length,
        truncated: captured.size >= 100,
        scan_limit: 100,
        limit_reached: profiles.length >= 100,
        raw: {
          cards: profiles.length,
          names: profiles.length,
          scan_limit: 100,
          truncated: captured.size >= 100,
          limit_reached: profiles.length >= 100,
        },
        page_url: location.href,
      };
    } finally {
      if (container) container.scrollTop = originalTop;
      scanInProgress = false;
    }
  }

  function openCandidate(index) {
    const profile = lastProfiles[Number(index)];
    if (!profile) return { ok: false, error: "That displayed LinkedIn candidate is no longer available." };
    const card = lastElements.get(profile.source_id);
    const link = Array.from(card?.querySelectorAll?.("a[href*='/in/']") || [])
      .find((candidate) => linkIdentity(candidate)?.id === profile.source_id);
    if (link?.isConnected) {
      link.scrollIntoView({ block: "center", behavior: "auto" });
      link.click();
      return { ok: true, source_url: profile.source_url };
    }
    location.href = profile.source_url;
    return { ok: true, source_url: profile.source_url };
  }

  function snapshot() {
    const profile = readProfile();
    const profiles = profile ? [profile] : readSearchResults();
    if (!profiles.length) {
      return {
        ok: false,
        platform: PLATFORM.key,
        error: "Open a complete LinkedIn /in/ profile or a People search with loaded candidate cards.",
      };
    }
    if (profile) {
      lastProfiles = profiles;
      lastElements = new Map();
    }
    const scanLimitReached = !profile && profiles.length >= 100;
    return {
      ok: true,
      platform: PLATFORM.key,
      platform_label: PLATFORM.label,
      profiles,
      count: profiles.length,
      expected_count: profiles.length,
      scan_limit: 100,
      limit_reached: scanLimitReached,
      raw: {
        cards: profiles.length,
        names: profiles.length,
        scan_limit: 100,
        limit_reached: scanLimitReached,
      },
      page_url: profile?.source_url || location.href,
    };
  }

  function guidePdfDownload() {
    const buttons = Array.from(document.querySelectorAll("button"));
    const more = buttons.find((button) => {
      const label = clean(button.getAttribute("aria-label"));
      const text = clean(button.innerText || button.textContent);
      return /more actions/i.test(label) || /^more$/i.test(text);
    });
    if (!more) {
      return {
        ok: true,
        more_button_found: false,
        message: "Open LinkedIn's More menu and choose Save to PDF.",
      };
    }
    more.scrollIntoView({ block: "center", behavior: "smooth" });
    const previousOutline = more.style.outline;
    const previousOffset = more.style.outlineOffset;
    more.style.outline = "3px solid #7432ed";
    more.style.outlineOffset = "3px";
    setTimeout(() => {
      more.style.outline = previousOutline;
      more.style.outlineOffset = previousOffset;
    }, 15000);
    return {
      ok: true,
      more_button_found: true,
      message: "The More button is highlighted. Open it and choose Save to PDF.",
    };
  }

  function linkedinMoreButton() {
    const buttons = Array.from(document.querySelectorAll("main button, button")).filter(isVisible);
    const candidates = buttons.filter((button) => {
      const label = clean(button.getAttribute("aria-label"));
      const text = clean(button.innerText || button.textContent);
      return /^(?:more|more actions)$/i.test(label) || /^more$/i.test(text);
    });
    const heading = profileHeader().heading;
    const headingRect = isVisible(heading) ? heading.getBoundingClientRect() : null;
    return candidates.sort((left, right) => {
      const score = (button) => {
        const label = clean(button.getAttribute("aria-label"));
        const rect = button.getBoundingClientRect();
        let value = 0;
        if (/^more$/i.test(label)) value += 100;
        else if (/^more actions$/i.test(label)) value += 90;
        if (button.hasAttribute("aria-expanded")) value += 30;
        if (headingRect && Math.abs(rect.top - headingRect.top) < 500) value += 20;
        if (button.closest("main")) value += 10;
        return value - (rect.top / 10000);
      };
      return score(right) - score(left);
    })[0] || null;
  }

  function saveToPdfAction() {
    const selectors = [
      "[role='menuitem']",
      "[role='option']",
      "[role='button']",
      "[role='menu'] button",
      "[role='menu'] a",
      "button",
      "a",
      "[tabindex='0']",
      ".artdeco-dropdown__item",
    ].join(",");
    return Array.from(document.querySelectorAll(selectors)).find((element) => {
      const labels = [
        clean(element.getAttribute("aria-label")),
        clean(element.getAttribute("title")),
        clean(element.innerText || element.textContent),
      ].filter(Boolean);
      return isVisible(element) && labels.some((text) => (
        /^(?:save(?: profile)? (?:to|as) pdf|download(?: profile)?(?: as)? pdf)$/i.test(text)
      ));
    }) || null;
  }

  async function waitForLinkedinMoreButton(timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    let more = null;
    while (Date.now() < deadline && !more) {
      more = linkedinMoreButton();
      if (!more) await pause(150);
    }
    return more;
  }

  async function automaticPdfDownload() {
    if (!normalizedProfileUrl()) {
      return { ok: false, error: "Open the candidate's exact LinkedIn profile first." };
    }
    // A prior attempt can leave the popover open. Use its action directly
    // instead of toggling the More button and accidentally closing it.
    let action = saveToPdfAction();
    const more = action ? null : await waitForLinkedinMoreButton();
    if (!action && !more) return { ok: false, error: "LinkedIn's More action is unavailable on this profile." };
    if (!action) {
      let opened = await trustedClick(more);
      if (!opened?.ok) {
        // The header may have re-rendered during the first scroll. Resolve its
        // current button once more rather than clicking stale coordinates.
        const replacement = await waitForLinkedinMoreButton(3000);
        if (!replacement || replacement === more) return opened;
        opened = await trustedClick(replacement);
      }
      if (!opened?.ok) return opened;
    }

    const deadline = Date.now() + 10000;
    while (Date.now() < deadline && !action) {
      await pause(120);
      action = saveToPdfAction();
    }
    if (!action) {
      return { ok: false, error: "LinkedIn did not offer Save to PDF for this profile." };
    }
    // Popover items are already visible. Scrolling one can dismiss LinkedIn's
    // detached popover before the trusted click reaches it.
    const clicked = await trustedClick(action, { scroll: false });
    return clicked?.ok
      ? { ok: true, action: "save_to_pdf" }
      : { ok: false, error: clicked?.error || "LinkedIn's Save to PDF action could not be selected." };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    const messageType = message?.type === ADAPTER_REQUEST ? message.original_type : message?.type;
    const respond = (payload) => sendResponse({ ...payload, adapter_revision: ADAPTER_REVISION });
    if (messageType === "RADIXSOL_PLATFORM_PING") {
      respond({ ok: true, platform: PLATFORM.key, label: PLATFORM.label, url: location.href });
      return false;
    }
    if (messageType === "RADIXSOL_CAPTURE_PLATFORM_PROFILE") {
      const result = snapshot();
      respond(result.ok ? { ...result, profile: result.profiles[0] } : result);
      return false;
    }
    if (messageType === "RADIXSOL_LIST_PLATFORM_CANDIDATES") {
      const result = snapshot();
      respond(result);
      return false;
    }
    if (messageType === "RADIXSOL_SCAN_PLATFORM_CANDIDATES") {
      if (!resultPage()) {
        const result = snapshot();
        if (result.ok) {
          chrome.runtime.sendMessage({
            type: "RADIXSOL_PLATFORM_SCAN_PROGRESS",
            platform: PLATFORM.key,
            found: result.profiles.length,
            total: result.profiles.length,
            preview: result.profiles.map(({ name, location, headline }) => ({ name, location, headline })),
          }, () => void chrome.runtime.lastError);
        }
        respond(result);
        return false;
      }
      progressiveSearchScan().then(respond).catch((error) => {
        respond({ ok: false, platform: PLATFORM.key, error: String(error?.message || error) });
      });
      return true;
    }
    if (messageType === "RADIXSOL_OPEN_PLATFORM_CANDIDATE") {
      if (resultPage()) respond(openCandidate(message.index));
      else {
        window.scrollTo({ top: 0, behavior: "smooth" });
        respond({ ok: true, source_url: normalizedProfileUrl() });
      }
      return false;
    }
    if (messageType === "RADIXSOL_GUIDE_LINKEDIN_PDF") {
      respond(guidePdfDownload());
      return false;
    }
    if (messageType === "RADIXSOL_AUTO_LINKEDIN_PDF") {
      automaticPdfDownload().then(respond).catch((error) => {
        respond({ ok: false, error: String(error?.message || error) });
      });
      return true;
    }
    return false;
  });

  let mutationTimer = null;
  let lastSignature = null;
  let observedUrl = location.href;

  function changeSnapshot() {
    const isSearch = resultPage();
    const profiles = isSearch
      ? visibleSearchResults().profiles
      : [readProfile()].filter(Boolean);
    return { profiles, view: isSearch ? "search" : "profile", resultPage: isSearch };
  }

  function emitResultsChanged() {
    if (scanInProgress) {
      scheduleResultsChanged(500);
      return;
    }
    const state = changeSnapshot();
    const signature = JSON.stringify({
      page: `${location.pathname}${location.search}${location.hash}`,
      view: state.view,
      profiles: state.profiles.map((profile) => ({
        id: profile.source_id,
        url: profile.source_url,
        name: profile.name,
        location: profile.location,
        headline: profile.headline,
        roles: profile.roles,
        employers: profile.employers,
        schools: profile.schools,
      })),
    });
    if (signature === lastSignature) return;
    lastSignature = signature;
    try {
      chrome.runtime.sendMessage({
        type: "RADIXSOL_PLATFORM_RESULTS_CHANGED",
        platform: PLATFORM.key,
        view: state.view,
        result_page: state.resultPage,
        page_url: location.href,
        count: state.profiles.length,
        empty: state.profiles.length === 0,
      }, () => void chrome.runtime.lastError);
    } catch {
      // The extension can be reloaded while a LinkedIn tab remains open.
    }
  }

  function scheduleResultsChanged(delay = 500) {
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(emitResultsChanged, delay);
  }

  new MutationObserver(() => scheduleResultsChanged()).observe(document.documentElement, {
    attributes: true,
    attributeFilter: [
      "aria-hidden", "class", "data-chameleon-result-urn", "data-entity-urn",
      "data-view-name", "hidden", "href", "style",
    ],
    characterData: true,
    childList: true,
    subtree: true,
  });
  window.addEventListener("popstate", () => scheduleResultsChanged(100));
  window.addEventListener("hashchange", () => scheduleResultsChanged(100));
  setInterval(() => {
    if (location.href === observedUrl) return;
    observedUrl = location.href;
    scheduleResultsChanged(100);
  }, 750);
  // Emit the initial state too. The side panel may already be open when the
  // adapter is injected, so waiting for a later DOM mutation leaves it stale.
  scheduleResultsChanged(50);
})();
