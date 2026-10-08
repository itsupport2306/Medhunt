"use strict";

// Platform-independent guard for profiles returned by content scripts. Site
// adapters read the DOM; this layer prevents obvious navigation labels,
// malformed source URLs, stale cards, and duplicates from reaching storage.
(() => {
  const MAX_PROFILES = 100;
  const CREDENTIALS = new Set([
    "acls", "aprn", "bls", "bsn", "cna", "cnor", "crna", "cst", "dnp",
    "do", "dds", "dmd", "lpn", "lvn", "ma", "mba", "md", "ms", "msn", "np", "pa", "pac", "pals", "pccn", "phd",
    "phn", "physicianassistant", "rma", "rn", "rpac", "scd",
  ]);
  const NON_PERSON_LABELS = new Set([
    "add friend", "candidate", "candidates", "connect", "contact info",
    "facebook", "follow", "home", "linkedin", "loading", "message",
    "notifications", "people", "people you may know", "profile", "profiles",
    "recommended jobs", "registered nurse", "results", "search",
    "search results", "see all", "source profiles", "suggested for you",
  ]);
  const FACEBOOK_RESERVED = new Set([
    "about", "ads", "business", "events", "friends", "gaming", "groups",
    "help", "home", "jobs", "login", "marketplace", "messages",
    "notifications", "pages", "people", "photo", "photos", "reel", "reels",
    "search", "share", "stories", "watch",
  ]);

  function cleanText(value, limit = 500) {
    return String(value ?? "")
      .replace(/[\u0000-\u001f\u007f]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, limit);
  }

  function cleanNotes(value, limit = 12000) {
    return String(value ?? "")
      .replace(/\r\n?/g, "\n")
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]+/g, " ")
      .split("\n")
      .map((line) => line.replace(/\s+/g, " ").trim())
      .filter(Boolean)
      .join("\n")
      .slice(0, limit);
  }

  function credentialToken(value) {
    return cleanText(value, 30).replace(/[^A-Za-z]/g, "").toLowerCase();
  }

  function normalizeName(value) {
    let name = cleanText(value, 140)
      .replace(/^(?:dr\.?|doctor|mr\.?|mrs\.?|ms\.?|miss)\s+/i, "")
      .replace(/\s+(?:\u00b7|\u2022)\s*(?:1st|2nd|3rd\+?|out of network).*$/i, "")
      .replace(/\s*\([^()]{1,100}\)\s*/g, " ")
      .replace(/\s+[\u2013\u2014-]\s+(?:registered\s+nurse|licensed\s+practical\s+nurse|nurse|rn|lpn|lvn)\b.*$/i, "")
      .replace(/\s+/g, " ")
      .trim();

    const commaParts = name.split(",").map((part) => part.trim()).filter(Boolean);
    while (commaParts.length > 1 && CREDENTIALS.has(credentialToken(commaParts.at(-1)))) {
      commaParts.pop();
    }
    name = commaParts.join(" ");

    const words = name.split(/\s+/).filter(Boolean);
    while (words.length > 1) {
      const raw = words.at(-1).replace(/[.,]+$/g, "");
      if (raw !== raw.toUpperCase() || !CREDENTIALS.has(credentialToken(raw))) break;
      words.pop();
    }
    return words.join(" ")
      .replace(/^[^\p{L}]+|[^\p{L}.'\u2019\-]+$/gu, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function likelyPersonName(value) {
    const name = normalizeName(value);
    if (!name || name.length < 2 || name.length > 100) return false;
    if (/[\d@]|https?:|www\./i.test(name)) return false;
    if (NON_PERSON_LABELS.has(name.toLowerCase())) return false;
    const words = name.split(/\s+/).filter(Boolean);
    if (!words.length || words.length > 8) return false;
    if (!/[\p{L}]{2}/u.test(name)) return false;
    return words.every((word) => (
      /\p{L}/u.test(word) && !/[^\p{L}\p{M}.'\u2019\-]/u.test(word)
    ));
  }

  function normalizeLocation(value) {
    const location = cleanText(value, 180)
      .replace(/^(?:lives\s+in|located\s+in|current\s+city|location)\s*:?\s*/i, "")
      .replace(/\s+(?:\u00b7|\u2022)\s*contact\s+info.*$/i, "")
      .trim();
    if (/^(?:location|remote|profile captured|not provided|unknown)$/i.test(location)) return "";
    return location;
  }

  function platformHost(platform, hostname) {
    const host = hostname.toLowerCase();
    const roots = {
      indeed: "indeed.com",
      vivian: "vivian.com",
      ziprecruiter: "ziprecruiter.com",
      linkedin: "linkedin.com",
      facebook: "facebook.com",
      npino: "npino.com",
      nysed: "eservices.nysed.gov",
      npiprofile: "npiprofile.com",
      usnews: "health.usnews.com",
      medifind: "medifind.com",
      webmd: "doctor.webmd.com",
      commonspirit: "commonspirit.org",
      sharecare: "providers.sharecare.com",
    };
    const root = roots[platform];
    return Boolean(root && (host === root || host.endsWith(`.${root}`)));
  }

  function facebookProfilePath(url) {
    const path = url.pathname.replace(/^\/+|\/+$/g, "");
    if (/^profile\.php$/i.test(path)) return Boolean(url.searchParams.get("id"));
    const parts = path.split("/").filter(Boolean);
    if (!parts.length) return false;
    if (parts[0].toLowerCase() === "people") return parts.length >= 3;
    return !FACEBOOK_RESERVED.has(parts[0].toLowerCase());
  }

  function validProfileUrl(value, platform) {
    if (!value) return true;
    try {
      const url = new URL(value);
      if (!/^https?:$/i.test(url.protocol) || !platformHost(platform, url.hostname)) return false;
      if (platform === "linkedin") return /^\/in\/[^/?#]+\/?$/i.test(url.pathname);
      if (platform === "facebook") return facebookProfilePath(url);
      if (platform === "usnews") {
        return /^\/(?:doctors|nurse-practitioners|physician-assistants|dentists)(?:\/|$)/i.test(url.pathname);
      }
      if (platform === "npino") return /^\/[^/]+\/\d{10}(?:-[^/]*)?\/?$/i.test(url.pathname);
      if (platform === "medifind") return /^\/doctors\/[^/?#]+\/\d+\/?$/i.test(url.pathname);
      if (platform === "commonspirit") return /^\/find-a-doctor\/[^/?#]+-\d+\/?$/i.test(url.pathname);
      if (platform === "sharecare") return /^\/doctor\/[^/?#]+\/?$/i.test(url.pathname);
      if (platform === "webmd") return /^\/doctor\/[^/?#]+-overview\/?$/i.test(url.pathname);
      return true;
    } catch {
      return false;
    }
  }

  function textList(value, limit = 30) {
    if (!Array.isArray(value)) return [];
    const seen = new Set();
    const output = [];
    for (const item of value) {
      const text = cleanText(item, 180);
      const key = text.toLowerCase();
      if (!text || seen.has(key)) continue;
      seen.add(key);
      output.push(text);
      if (output.length >= limit) break;
    }
    return output;
  }

  function sanitizeProfileDocument(value, platform, sourceUrl) {
    if (
      !["usnews", "medifind", "commonspirit", "sharecare", "webmd", "npino"].includes(platform) || !value || typeof value !== "object"
      || value.kind !== "public_professional_profile"
    ) return null;
    const documentUrl = cleanText(value.source_url || sourceUrl, 1200);
    if (!validProfileUrl(documentUrl, platform)) return null;
    const credentials = textList(value.credentials, 12);
    const specialties = textList(value.specialties, 20);
    const subspecialties = textList(value.subspecialties, 20);
    const hospitals = textList(value.hospitals, 40);
    const education = textList(value.education, 40);
    const certifications = textList(value.certifications, 40);
    const licenses = textList(value.licenses, 75);
    const languages = textList(value.languages, 20);
    const summary = cleanNotes(value.summary, 4000);
    const meaningful = education.length || certifications.length || licenses.length
      || specialties.length || hospitals.length || summary;
    if (!meaningful) return null;
    return {
      kind: "public_professional_profile",
      source_label: cleanText(value.source_label, 100)
        || (platform === "medifind" ? "MediFind"
          : platform === "commonspirit" ? "CommonSpirit Health"
            : platform === "sharecare" ? "Sharecare" : "U.S. News Doctor Finder"),
      source_url: documentUrl,
      headline: cleanText(value.headline, 300),
      summary,
      credentials,
      specialties,
      subspecialties,
      hospitals,
      education,
      certifications,
      licenses,
      languages,
      years_experience: cleanText(value.years_experience, 60),
      npi: cleanText(value.npi, 30).replace(/\D/g, "").slice(0, 10),
      address: cleanText(value.address, 500),
      location: normalizeLocation(value.location),
    };
  }

  function stableSourceId(profile, sourceUrl) {
    const explicit = cleanText(profile?.source_id, 300);
    if (explicit) return explicit;
    try {
      const url = new URL(sourceUrl);
      return `${url.pathname}${url.search}`.replace(/^\/+|\/+$/g, "").slice(0, 300);
    } catch {
      return "";
    }
  }

  function sanitizeProfile(raw, context = {}) {
    if (!raw || typeof raw !== "object") return { profile: null, reason: "invalid" };
    const platform = cleanText(context.platform || raw.source, 30).toLowerCase();
    const name = normalizeName(raw.name);
    if (!likelyPersonName(name)) return { profile: null, reason: "invalid_name" };

    const rawSourceUrl = cleanText(raw.source_url, 1200);
    const pageUrl = cleanText(context.pageUrl, 1200);
    const sourceUrl = rawSourceUrl || (context.singleProfile ? pageUrl : "");
    if (sourceUrl && !validProfileUrl(sourceUrl, platform)) {
      return { profile: null, reason: "invalid_source" };
    }

    const profile = {
      ...raw,
      name,
      location: normalizeLocation(raw.location),
      hometown: normalizeLocation(raw.hometown),
      headline: cleanText(raw.headline, 240),
      source: platform,
      source_url: sourceUrl,
      source_id: stableSourceId(raw, sourceUrl),
      notes: cleanNotes(raw.notes, 12000),
      aliases: textList(raw.aliases),
      roles: textList(raw.roles),
      employers: textList(raw.employers),
      schools: textList(raw.schools),
      // Preserve source-declared specialties independently from generic
      // skills/roles. Healthcare and staffing adapters can expose either a
      // scalar `specialty` or bounded specialty arrays; keeping both here
      // lets the import contract carry them to the recruiting system for exact resolution.
      specialty: cleanText(raw.specialty, 240),
      specialties: textList([
        ...(Array.isArray(raw.specialties) ? raw.specialties : []),
        ...(Array.isArray(raw.subspecialties) ? raw.subspecialties : []),
      ], 20),
      subspecialties: textList(raw.subspecialties, 20),
      skills: textList(raw.skills, 100),
      licenses: textList(raw.licenses, 100),
      certifications: textList(raw.certifications, 100),
      profile_document: sanitizeProfileDocument(raw.profile_document, platform, sourceUrl),
    };
    return { profile, reason: "" };
  }

  function profileKey(profile) {
    if (profile.source_id) return `id:${profile.source_id.toLowerCase()}`;
    if (profile.source_url) return `url:${profile.source_url.toLowerCase()}`;
    return `person:${profile.name.toLowerCase()}|${profile.location.toLowerCase()}`;
  }

  function richness(profile) {
    return [
      profile.location, profile.hometown, profile.headline, profile.source_url,
      profile.notes, ...(profile.roles || []), ...(profile.employers || []),
      ...(profile.schools || []),
    ].reduce((score, value) => score + (cleanText(value, 12000).length ? 1 : 0), 0);
  }

  function mergeProfiles(left, right) {
    const primary = richness(right) > richness(left) ? right : left;
    const secondary = primary === right ? left : right;
    const merged = { ...secondary, ...primary };
    for (const field of [
      "aliases", "roles", "employers", "schools", "specialties", "subspecialties",
      "skills", "licenses", "certifications",
    ]) {
      merged[field] = textList([...(left[field] || []), ...(right[field] || [])], 100);
    }
    if ((secondary.notes || "").length > (primary.notes || "").length) merged.notes = secondary.notes;
    return merged;
  }

  function sanitizeProfiles(values, context = {}) {
    const profiles = new Map();
    const skipped = { invalid: 0, invalid_name: 0, invalid_source: 0, duplicate: 0 };
    for (const raw of Array.isArray(values) ? values : []) {
      const { profile, reason } = sanitizeProfile(raw, context);
      if (!profile) {
        skipped[reason] = (skipped[reason] || 0) + 1;
        continue;
      }
      const key = profileKey(profile);
      if (profiles.has(key)) {
        skipped.duplicate += 1;
        profiles.set(key, mergeProfiles(profiles.get(key), profile));
      } else {
        profiles.set(key, profile);
      }
      if (profiles.size >= MAX_PROFILES) break;
    }
    const skippedCount = Object.values(skipped).reduce((total, count) => total + count, 0);
    return { profiles: [...profiles.values()], skipped, skippedCount };
  }

  globalThis.RadixsolProfileQuality = Object.freeze({
    cleanText,
    normalizeName,
    likelyPersonName,
    normalizeLocation,
    validProfileUrl,
    sanitizeProfile,
    sanitizeProfiles,
  });
})();
