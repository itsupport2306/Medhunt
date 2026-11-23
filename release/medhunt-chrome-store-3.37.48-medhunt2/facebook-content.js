(() => {
  "use strict";

  const ADAPTER_REVISION = "facebook-profile-v8";
  const ADAPTER_REQUEST = "MEDHUNT_FACEBOOK_V8_REQUEST";
  if (window.__medhuntFacebookAdapterRevision === ADAPTER_REVISION) return;
  if (window.__medhuntFacebookMessageListener) {
    try {
      chrome.runtime.onMessage.removeListener(window.__medhuntFacebookMessageListener);
    } catch {

    }
  }
  if (window.__medhuntFacebookObserver) {
    try { window.__medhuntFacebookObserver.disconnect(); } catch { /* no-op */ }
  }
  if (window.__medhuntFacebookMutationTimer) {
    clearTimeout(window.__medhuntFacebookMutationTimer);
  }
  if (window.__medhuntFacebookRouteTimer) {
    clearInterval(window.__medhuntFacebookRouteTimer);
  }
  window.__medhuntFacebookCaptureLoaded = true;
  window.__medhuntFacebookAdapterRevision = ADAPTER_REVISION;

  const PLATFORM = { key: "facebook", label: "Facebook" };

  const RESERVED_ROUTES = new Set([
    "watch", "marketplace", "gaming", "groups", "events", "pages", "ads",
    "business", "help", "settings", "notifications", "messages", "friends",
    "bookmarks", "memories", "search", "stories", "reels", "reel", "live",
    "fundraisers", "offers", "jobs", "login", "recover", "policies", "privacy",
    "terms", "cookies", "photo.php", "video.php", "share.php", "sharer.php",
    "story.php", "permalink.php", "share", "public", "dialog", "home.php",
    "profile.php", "places", "photo",
  ]);
  const UI_LABELS = new Set([
    "facebook", "home", "friends", "photos", "videos", "reels", "posts",
    "about", "intro", "more", "message", "follow", "add friend", "search",
    "notifications", "professional dashboard", "edit profile", "see more",
    "all", "check-ins", "personal details", "contact info", "cover photo",
  ]);
  const PROFILE_SIGNAL_RE = /^(?:(?:\d[\d,.]*|\d+(?:\.\d+)?[KMB])\s+(?:friends?|followers?|following)|add friend|edit profile|message|follow)$/i;
  const LOCKED_PROFILE_RE = /^(?:this\s+)?profile\s+is\s+locked$|^(?:profile\s+locked|locked\s+profile)$|^you(?:'|\u2019)?ve\s+locked\s+your\s+profile$|^[\p{L}][\p{L}'\u2019 .-]{0,100}\s+(?:has\s+)?locked\s+(?:his|her|their)\s+profile$/iu;
  const ROLE_WORDS = /\b(?:rn|lpn|lvn|nurse|nursing|physician|doctor|therapist|clinician|practitioner|assistant|specialist|coordinator|recruiter|manager|director|engineer|technician|educator|faculty|professor|consultant|administrator)\b/i;
  const CREDENTIALS = new Set([
    "aa", "aas", "adn", "anp", "aprn", "as", "asn", "ba", "bba", "bsc",
    "bs", "bsn", "ccrn", "cma", "cna", "cnm", "cnor", "cns", "cpa",
    "cst", "dds", "dnp", "do", "dpt", "emt", "esq", "fnp", "fnpbc",
    "fnp-c", "jd", "lcsw", "lmsw", "lpn", "lvn", "ma", "mba", "md",
    "mha", "mph", "ms", "msc", "msn", "np", "nrcma", "od", "pa",
    "pccn", "pharmd", "phd", "phn", "pmhnp", "pmhnpbc", "psyd", "rn", "rnc",
    "rnbc", "rnfa",
  ]);
  const HEALTH_ROLE_SOURCE = [
    "licensed\\s+(?:registered|practical|vocational)\\s+nurse",
    "registered\\s+nurse", "practical\\s+nurse", "vocational\\s+nurse",
    "nurse\\s+practitioner", "family\\s+nurse\\s+practitioner",
    "certified\\s+(?:registered\\s+)?nurse\\s+anesthetist",
    "certified\\s+nursing\\s+assistant", "nursing\\s+assistant",
    "clinical\\s+nurse\\s+specialist", "staff\\s+nurse", "charge\\s+nurse",
    "travel\\s+nurse", "school\\s+nurse", "home\\s+health\\s+nurse",
    "registered\\s+nursing", "nursing", "nurse",
    "infirmi[eè]re(?:\\s+auxiliaire)?", "infirmier(?:\\s+auxiliaire)?",
  ].join("|");
  const HEALTH_ROLE_CHUNK_RE = new RegExp(
    `^(?:${HEALTH_ROLE_SOURCE})(?:\\s*[-/|,&]\\s*(?:${HEALTH_ROLE_SOURCE}))*$`, "iu",
  );
  const HEALTH_ROLE_SUFFIX_RE = new RegExp(
    `(?:\\s+|,\\s*|\\|\\s*|[–—]\\s*)((?:${HEALTH_ROLE_SOURCE})(?:\\s*[-/|,&]\\s*(?:${HEALTH_ROLE_SOURCE}))*)\\s*$`,
    "iu",
  );
  const COMPANY_WORDS = /\b(?:health|healthcare|hospital|clinic|medical|care|solutions|services|center|centre|system|group|staffing|agency|inc\.?|llc|corp(?:oration)?|company|associates|department|practice)\b/i;
  const SCHOOL_WORDS = /\b(?:university|college|school|academy|institute|polytechnic)\b/i;
  const US_STATE = "(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming)";
  const US_LOCATION_RE = new RegExp(`^[\\p{L}][\\p{L} .'-]{0,70},\\s*${US_STATE}(?:,\\s*(?:United States|USA|US))?$`, "iu");
  const FACT_SECTION_RE = /^(?:intro|about|personal details|details about|places lived|work(?: and education)?|education|contact and basic info)$/i;
  const FACT_ARIA_RE = /(?:current city|hometown|location|workplace|employer|college|school|education|personal details|places lived|profile intro)/i;
  const CONTAMINATED_REGION_SELECTOR = [
    "article", "[role='article']", "[role='feed']", "[role='dialog']",
    "[aria-label*='comment' i]", "[aria-label='Posts' i]",
    "[data-pagelet*='FeedUnit']", "[data-pagelet*='TimelineFeed']",
    "[data-pagelet*='ProfileTimeline']", "[data-pagelet*='Posts']",
    "[data-ad-rendering-role*='story']", "[data-ad-rendering-role*='comment']",
  ].join(",");
  const PAGE_TEXT_RE = /^(?:page transparency|manage page|edit page info|facebook page|create a page)$/i;
  const PAGE_CATEGORY_RE = /^page\\s*[\\u00b7|]\\s*.{2,100}$/i;

  function clean(value) {
    return String(value ?? "")
      .replace(/\u00a0/g, " ")
      .replace(/[\u200b-\u200d\ufeff]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function unique(values, limit = 60) {
    const output = [];
    const seen = new Set();
    for (const value of values || []) {
      const cleaned = clean(value);
      const key = cleaned.toLowerCase();
      if (!cleaned || seen.has(key)) continue;
      seen.add(key);
      output.push(cleaned);
      if (output.length >= limit) break;
    }
    return output;
  }

  function facebookIdentity(value = location.href) {
    try {
      const parsed = new URL(value);
      const host = parsed.hostname.toLowerCase();
      if (!(host === "facebook.com" || host.endsWith(".facebook.com"))) return null;
      const path = parsed.pathname.replace(/\/+$/, "") || "/";
      const lowerPath = path.toLowerCase();
      if (lowerPath === "/profile.php" && parsed.searchParams.get("id")) {
        const id = clean(parsed.searchParams.get("id"));
        if (!/^[a-z0-9._-]+$/i.test(id)) return null;
        return {
          sourceId: `id:${id}`,
          url: `https://www.facebook.com/profile.php?id=${encodeURIComponent(id)}`,
        };
      }

      const segments = path.split("/").filter(Boolean).map((part) => decodeURIComponent(part));
      if (!segments.length) return null;
      const first = clean(segments[0]).toLowerCase();



      if (first === "people") {
        if (segments.length >= 3 && /^\d+$/.test(segments.at(-1) || "")) {
          const id = segments.at(-1);
          return {
            sourceId: `id:${id}`,
            url: `https://www.facebook.com/profile.php?id=${encodeURIComponent(id)}`,
          };
        }
        return null;
      }
      if (!first || RESERVED_ROUTES.has(first)) return null;
      if (!/^[a-z0-9._-]+$/i.test(segments[0])) return null;
      return {
        sourceId: first,
        url: `https://www.facebook.com/${encodeURIComponent(segments[0])}`,
      };
    } catch {
      return null;
    }
  }

  function isVisible(element) {
    if (!(element instanceof Element)) return false;
    if (element.closest('[hidden], [aria-hidden="true"]')) return false;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    return element.getClientRects().length > 0;
  }

  function sameIdentityHref(element, identity) {
    const href = element?.getAttribute?.("href");
    if (!href || !identity) return false;
    try {
      const linked = facebookIdentity(new URL(href, location.href).href);
      return Boolean(linked && linked.sourceId === identity.sourceId);
    } catch {
      return false;
    }
  }

  function mainRegions() {
    return Array.from(document.querySelectorAll('[role="main"], main'))
      .filter(isVisible);
  }

  function isSearchResultsRegion(element) {
    return /^(?:search|people)\s+results$/i.test(clean(element?.getAttribute?.("aria-label")));
  }

  function profileRegion(identity) {
    const regions = mainRegions();
    const scored = regions.map((element, index) => {
      let score = 0;
      if (isSearchResultsRegion(element)) score -= 1000;
      if (element.querySelector('[role="tablist"], [role="tab"]')) score += 120;
      if (Array.from(element.querySelectorAll('a[href]')).some((link) => sameIdentityHref(link, identity))) {
        score += 180;
      }
      if (element.querySelector('[aria-label*="profile cover" i], [aria-label*="profile header" i]')) {
        score += 70;
      }
      if (Array.from(element.querySelectorAll('button, [role="button"], a')).some((node) => {
        const value = clean(node.innerText || node.textContent || node.getAttribute("aria-label"));
        return PROFILE_SIGNAL_RE.test(value) || /^add friend\b/i.test(value);
      })) score += 45;
      return { element, index, score };
    });
    scored.sort((left, right) => right.score - left.score || right.index - left.index);
    const best = scored[0];
    return best && best.score > -500 ? best.element : null;
  }

  function visibleLockedProfile(region) {
    if (!region) return false;
    for (const element of region.querySelectorAll('[role="status"], [aria-label], h1, h2, h3, span, div')) {
      if (!isVisible(element)) continue;
      const values = [
        clean(element.getAttribute("aria-label")),
        clean(element.innerText || element.textContent),
      ];
      for (const value of values) {
        if (value && value.length <= 160 && LOCKED_PROFILE_RE.test(value)) return true;
      }
    }
    return false;
  }

  function isContaminatedElement(element) {
    if (!(element instanceof Element)) return false;
    return Boolean(element.closest(CONTAMINATED_REGION_SELECTOR));
  }

  function visibleCompactValues(root, selector, limit = 120) {
    const values = [];
    if (!root) return values;
    for (const element of root.querySelectorAll(selector)) {
      if (values.length >= limit || !isVisible(element) || isContaminatedElement(element)) continue;
      const text = clean(element.innerText || element.textContent || element.getAttribute("aria-label"));
      const label = clean(element.getAttribute("aria-label"));
      if (text && text.length <= 180) values.push(text);
      if (label && label.length <= 180 && !sameLooseText(label, text)) values.push(label);
    }
    return unique(values, limit);
  }

  function profileSurfaceKind(identity, region) {
    if (!identity || !region) return "loading";
    if (visibleLockedProfile(region)) return "locked";

    let personalScore = 0;
    let pageScore = 0;
    const values = visibleCompactValues(
      region,
      "h1, h2, h3, [role='heading'], button, [role='button'], a, span, [aria-label]",
    );
    for (const value of values) {
      if (/^(?:add friend|friends)$/i.test(value)
          || /^(?:\d[\d,.]*|\d+(?:\.\d+)?[KMB])\s+friends?$/i.test(value)) {
        personalScore += 5;
      } else if (/^(?:professional mode|professional dashboard)$/i.test(value)) {
        personalScore += 3;
      }
      if (PAGE_TEXT_RE.test(value) || PAGE_CATEGORY_RE.test(value)) pageScore += 6;
      if (/^(?:\d[\d,.]*|\d+(?:\.\d+)?[KMB])\s+likes?$/i.test(value)) pageScore += 4;
      if (/^like$/i.test(value)) pageScore += 3;
      if (/^(?:call now|book now|visit website|get quote|send whatsapp)$/i.test(value)) pageScore += 3;
    }

    const ogType = clean(document.querySelector('meta[property="og:type"]')?.content).toLowerCase();
    const regionLabel = clean(region.getAttribute?.("aria-label"));
    if (ogType === "profile") personalScore += 6;
    if (region.querySelector("[data-pagelet*='ProfileCover'], [data-pagelet*='ProfileHeader'], [aria-label*='profile header' i]")) {
      personalScore += 2;
    }
    if (Array.from(region.querySelectorAll("a[href]")).some((link) => sameIdentityHref(link, identity))) {
      personalScore += 2;
    }
    if (region.querySelector("[aria-label*='page transparency' i], [aria-label*='manage page' i]")) {
      pageScore += 7;
    }
    if (/\bfacebook page\b/i.test(regionLabel)) pageScore += 6;
    if (/^(?:business\.business|business|place|product)$/i.test(ogType)) pageScore += 4;




    if (pageScore >= 6 && personalScore < 5) return "page";
    return "personal";
  }

  function linesFrom(root) {
    return unique(String(root?.innerText || root?.textContent || "")
      .split(/\r?\n/)
      .map(clean)
      .filter((line) => line && line.length <= 300), 160);
  }

  function leafSegments(root) {
    if (!root) return [];
    const values = [];
    const selector = [
      "h1", "h2", "h3", "[role='heading']", "[dir='auto']", "a", "span",
      "[aria-label*='city' i]", "[aria-label*='work' i]",
      "[aria-label*='school' i]", "[aria-label*='college' i]",
    ].join(",");
    for (const element of root.querySelectorAll(selector)) {
      if (!isVisible(element)) continue;
      const text = clean(element.innerText || element.textContent);
      if (text && text.length <= 300) values.push(text);
      const label = clean(element.getAttribute("aria-label"));
      if (label && label.length <= 300 && label.toLowerCase() !== text.toLowerCase()) {
        if (text && /^(?:current city|hometown|location|workplace|employer|college|school|education)$/i.test(label)) {
          values.push(`${label}: ${text}`);
        } else {
          values.push(label);
        }
      }
    }
    return unique(values, 200);
  }

  function credentialChunk(value) {
    const tokens = clean(value).split(/[\s/&,]+/).filter(Boolean);
    return tokens.length > 0 && tokens.every((token) => (
      CREDENTIALS.has(token.toLowerCase().replace(/[^a-z0-9-]/g, ""))
    ));
  }

  function sameLooseText(left, right) {
    const key = (value) => clean(value).toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
    return Boolean(key(left) && key(left) === key(right));
  }

  function splitHeaderName(value) {
    let raw = clean(value)
      .replace(/^\(\d+\)\s*/, "")
      .replace(/\s*[|\-]\s*Facebook\s*$/i, "")
      .replace(/\s+on Facebook\s*$/i, "")
      .replace(/\s*[·|]\s*(?:1st|2nd|3rd|following)\s*$/i, "")
      .replace(/^(?:dr|mr|mrs|ms|miss|prof)\.?\s+/i, "");
    const descriptors = [];
    const alternateNames = [];
    if (/^\([^()]+\)$/.test(raw)) {
      return { name: "", headline: "", alternateNames: [], descriptors: [] };
    }
    let parenthetical = raw.match(/^(.*?)\s*\(([^()]{1,100})\)\s*$/);
    while (parenthetical && clean(parenthetical[1])) {
      const inside = clean(parenthetical[2]);
      raw = clean(parenthetical[1]);
      if (HEALTH_ROLE_CHUNK_RE.test(inside) || credentialChunk(inside) || ROLE_WORDS.test(inside)) {
        descriptors.push(inside);
      } else if (!sameLooseText(raw, inside)) {
        alternateNames.push(inside);
      }
      parenthetical = raw.match(/^(.*?)\s*\(([^()]{1,100})\)\s*$/);
    }

    const commaParts = raw.split(",").map(clean);
    if (commaParts.length > 1) {
      const kept = [commaParts[0]];
      for (const part of commaParts.slice(1)) {
        if (credentialChunk(part) || HEALTH_ROLE_CHUNK_RE.test(part)) descriptors.push(part);
        else if (part) kept.push(part);
      }
      raw = clean(kept.join(" "));
    }

    let roleMatch = raw.match(HEALTH_ROLE_SUFFIX_RE);
    while (roleMatch) {
      const role = clean(roleMatch[1]);
      const base = clean(raw.slice(0, roleMatch.index).replace(/[,|–—-]+$/, ""));
      const baseWords = base.match(/[\p{L}][\p{L}'’.-]*/gu) || [];
      const ambiguous = /^(?:nurse|nursing)$/i.test(role);
      if (baseWords.length < 2 && ambiguous) break;
      descriptors.push(role);
      raw = base;
      roleMatch = raw.match(HEALTH_ROLE_SUFFIX_RE);
    }

    const tokens = raw.split(/\s+/).filter(Boolean);
    while (tokens.length > 1 && credentialChunk(tokens.at(-1))) {
      descriptors.unshift(tokens.pop());
    }
    raw = clean(tokens.join(" "));
    return {
      name: raw,
      headline: unique(descriptors, 8).join(" · "),
      alternateNames: unique(alternateNames, 5),
      descriptors: unique(descriptors, 8),
    };
  }

  function plausibleName(value) {
    const { name } = splitHeaderName(value);
    const lower = name.toLowerCase();
    if (!name || name.length > 120 || UI_LABELS.has(lower)) return false;
    if (/^(?:\(\d+\)\s*)?facebook$/i.test(name)) return false;
    if (/\b(?:notification|friend request|people you may know)\b/i.test(name)) return false;
    if (/\d{2,}/.test(name) || /[@#]|https?:|www\./i.test(name)) return false;
    if (/\b(?:friends?|followers?|following)\b/i.test(name)) return false;
    const words = name.match(/[\p{L}][\p{L}'’-]*/gu) || [];
    return words.length >= 1 && words.length <= 8;
  }

  const sameIdentityLink = sameIdentityHref;

  function profileSignals(root = document.body) {
    if (!root) return [];
    const signals = [];
    for (const element of root.querySelectorAll("button, [role='button'], a, span, div")) {
      if (signals.length >= 50) break;
      if (!isVisible(element)) continue;
      const value = clean(element.innerText || element.textContent);
      if (value.length <= 50 && PROFILE_SIGNAL_RE.test(value)) signals.push(element);
    }
    return signals;
  }

  function bodyLineHeader(root = document.body) {
    const lines = String(root?.innerText || root?.textContent || "")
      .split(/\r?\n/)
      .map(clean)
      .filter(Boolean);
    const signalIndexes = [];
    lines.forEach((line, index) => {
      if (PROFILE_SIGNAL_RE.test(line)) signalIndexes.push(index);
    });
    for (const signalIndex of signalIndexes) {


      for (let offset = 1; offset <= 8 && signalIndex - offset >= 0; offset += 1) {
        const value = lines[signalIndex - offset];
        if (PROFILE_SIGNAL_RE.test(value)) continue;
        if (plausibleName(value)) return value;
      }
    }
    return "";
  }

  function chromePenalty(element) {
    if (!element) return 0;
    if (element.closest("[data-pagelet*='ProfileCover'], [data-pagelet*='ProfileHeader']")) return 0;
    return element.closest("[role='banner'], [role='navigation'], nav") ? 220 : 0;
  }

  function signalProximityScore(element, signals) {
    if (!element || !signals.length) return 0;
    const rect = element.getBoundingClientRect();
    let best = 0;
    for (const signal of signals) {
      const signalRect = signal.getBoundingClientRect();
      const distance = Math.abs(
        (rect.top + rect.height / 2) - (signalRect.top + signalRect.height / 2)
      );
      if (distance <= 260) best = Math.max(best, 120 - distance / 3);
      if (element.parentElement && element.parentElement.contains(signal)) best = Math.max(best, 135);
    }
    return best;
  }

  function profileHeader(root, identity) {
    const candidates = [];
    const signals = profileSignals(root);
    const elements = root.querySelectorAll([
      "h1", "[role='heading'][aria-level='1']", "[role='heading']", "h2", "h3",
      "a[href]", "[dir='auto']",
    ].join(","));
    for (const element of elements) {
      if (!isVisible(element)) continue;
      const value = clean(element.innerText || element.textContent);
      if (!plausibleName(value)) continue;
      let score = 0;
      if (element.matches("h1")) score += 120;
      if (element.matches("[role='heading'][aria-level='1']")) score += 110;
      else if (element.matches("[role='heading'], h2, h3")) score += 45;
      if (sameIdentityLink(element.closest("a[href]") || element, identity)) score += 100;
      if (element.closest("[data-pagelet*='ProfileCover'], [data-pagelet*='ProfileHeader']")) score += 70;
      if (ROLE_WORDS.test(value.match(/\(([^()]*)\)\s*$/)?.[1] || "")) score += 45;
      if (element.matches("[dir='auto']")) score += 5;
      score += signalProximityScore(element, signals);
      score -= chromePenalty(element);
      const rect = element.getBoundingClientRect();
      if (rect.top >= 0 && rect.top < Math.max(innerHeight * 1.5, 1200)) score += 15;
      candidates.push({ element, value, score });
    }
    const nearbyLine = bodyLineHeader(root);
    if (nearbyLine) {
      const parentheticalRole = nearbyLine.match(/\(([^()]*)\)\s*$/)?.[1] || "";
      candidates.push({
        element: null,
        value: nearbyLine,
        score: 165 + (ROLE_WORDS.test(parentheticalRole) ? 45 : 0),
      });
    }
    const metadata = clean(document.querySelector('meta[property="og:title"]')?.content);
    if (metadata && plausibleName(metadata)) {
      candidates.push({ element: null, value: metadata, score: 80 });
    }
    const title = clean(document.title);
    if (/\bFacebook\s*$/i.test(title) && plausibleName(title)) {
      candidates.push({ element: null, value: title, score: 70 });
    }
    candidates.sort((left, right) => right.score - left.score);
    const best = candidates[0];
    if (best) return { ...splitHeaderName(best.value), element: best.element };
    return { name: "", headline: "", element: null };
  }

  function boundedSectionRoot(heading, root) {
    if (!heading || !root || isContaminatedElement(heading)) return null;
    const semantic = heading.closest([
      "[data-pagelet*='ProfileTile']", "section", "[role='region']", "[role='list']",
      "[aria-label*='personal details' i]", "[aria-label*='places lived' i]",
    ].join(","));
    if (semantic && root.contains(semantic) && !isContaminatedElement(semantic)) {
      const text = clean(semantic.innerText || semantic.textContent);
      if (text.length <= 6000) return semantic;
    }

    let chosen = null;
    let current = heading.parentElement;
    while (current && current !== root) {
      if (isContaminatedElement(current)) break;
      const text = clean(current.innerText || current.textContent);
      if (text.length > 4000) break;
      const majorHeadings = Array.from(current.querySelectorAll("h1, h2, h3, [role='heading']"))
        .filter(isVisible)
        .map((element) => clean(element.innerText || element.textContent))
        .filter((value) => FACT_SECTION_RE.test(value) || /^(?:posts|photos|friends|reels|videos)$/i.test(value));
      if (majorHeadings.length > 1) break;
      chosen = current;
      current = current.parentElement;
    }
    return chosen;
  }

  function factRoots(root, headerElement) {
    const roots = [];
    const add = (element) => {
      if (!element || element === root || !root.contains(element)
          || isContaminatedElement(element) || roots.includes(element)) return;
      roots.push(element);
    };

    for (const element of root.querySelectorAll([
      "[data-pagelet*='ProfileCover']", "[data-pagelet*='ProfileHeader']",
      "[aria-label*='profile header' i]", "[role='list'][aria-label*='personal details' i]",
      "[role='list'][aria-label*='places lived' i]",
    ].join(","))) add(element);

    if (headerElement) {
      const headerRoot = headerElement.closest([
        "[data-pagelet*='ProfileCover']", "[data-pagelet*='ProfileHeader']",
        "[aria-label*='profile header' i]", "section",
      ].join(","));
      add(headerRoot);
      const parentText = clean(headerElement.parentElement?.innerText || headerElement.parentElement?.textContent);
      if (parentText.length <= 2500) add(headerElement.parentElement);
    }

    for (const heading of root.querySelectorAll("h1, h2, h3, [role='heading'], [dir='auto']")) {
      if (!isVisible(heading) || !FACT_SECTION_RE.test(clean(heading.innerText || heading.textContent))) continue;
      add(boundedSectionRoot(heading, root));
    }
    for (const element of root.querySelectorAll("[aria-label]")) {
      if (FACT_ARIA_RE.test(clean(element.getAttribute("aria-label")))) add(element);
    }
    return roots;
  }

  function recognizedFactLine(value) {
    const line = clean(value);
    return /^(?:(?:Lives in|Current city|Location|From|Hometown|Works|Worked|Workplace|Employer|Studied at|Went to|College|School|Education|Bio)\s*:?\s*.+|Current:\s*.+)$/i.test(line);
  }

  function boundedFactValues(root, headerElement) {
    const areas = factRoots(root, headerElement);
    const values = areas.flatMap((area) => [
      ...linesFrom(area),
      ...leafSegments(area),
    ]);
    const insideArea = (element) => areas.some((area) => area === element || area.contains(element));

    for (const element of root.querySelectorAll("div, span, li, p, a, [aria-label]")) {
      if (!isVisible(element) || isContaminatedElement(element)) continue;
      const text = clean(element.innerText || element.textContent);
      const label = clean(element.getAttribute("aria-label"));
      if (label && FACT_ARIA_RE.test(label) && text && text.length <= 300) {
        values.push(`${label}: ${text}`);
      }
      if (!text || text.length > 300) continue;
      if (recognizedFactLine(text)) {
        values.push(text);
        continue;
      }



      if (/^.{2,100}\s+at\s+.{2,140}$/i.test(text)
          && (insideArea(element) || element.parentElement === root)) {
        values.push(text);
      }
    }
    return unique(values, 240);
  }

  function normalizeLocation(value) {
    return clean(value)



      .replace(/^(?:(?:Lives in|Current city|Location|Hometown|From)\s*:?\s*)+/i, "")
      .replace(/\s*[·|]\s*.*$/, "")
      .trim();
  }

  function looksLikeExplicitLocation(value) {
    const line = normalizeLocation(value);
    if (!line || line.length > 120) return false;
    if (!/[\p{L}]/u.test(line) || /[@#]|https?:|www\./i.test(line)) return false;
    if (/\b(?:works?|studied|school|college|university|hospital|healthcare|current:)\b/i.test(line)) return false;
    return true;
  }

  function looksLikeLocation(value) {
    const line = normalizeLocation(value);
    if (!line || line.length > 120) return false;
    if (/\b(?:works?|studied|school|college|university|hospital|healthcare|current:)\b/i.test(line)) return false;
    return US_LOCATION_RE.test(line)
      || /^[\p{L}][\p{L} .'-]{1,70},\s*[\p{L}][\p{L} .'-]{1,60}$/iu.test(line)
      || /^[\p{L}][\p{L} .'-]{1,70},\s*[\p{L}][\p{L} .'-]{1,60},\s*(?:United States|Canada|India|United Kingdom|Australia)$/iu.test(line);
  }

  function normalizeAriaFact(value) {
    return clean(value)
      .replace(/^(?:Current city|Hometown|Workplace|Employer|College|School|Education)\s*:?\s*/i, "");
  }

  function extractFacts(values, header) {
    const currentLocations = [];
    const hometowns = [];
    const roles = [];
    const employers = [];
    const schools = [];
    const bios = [];
    const details = [];
    if (header.headline) roles.push(header.headline);

    for (const original of unique(values, 240)) {
      const line = clean(original);
      if (!line || line === header.name || line === `${header.name} (${header.headline})`) continue;
      if (UI_LABELS.has(line.toLowerCase()) || /^\d[\d,.]*\s+(?:friends?|followers?|following)$/i.test(line)) continue;

      let match = line.match(/^(?:Lives in|Current city|Location)\s*:?\s*(.+)$/i);
      if (match && looksLikeExplicitLocation(match[1])) {
        currentLocations.push(normalizeLocation(match[1]));
        details.push(line);
        continue;
      }
      match = line.match(/^(?:From|Hometown)\s*:?\s*(.+)$/i);
      if (match && looksLikeExplicitLocation(match[1])) {
        hometowns.push(normalizeLocation(match[1]));
        details.push(line);
        continue;
      }
      if (looksLikeLocation(line)) {
        currentLocations.push(normalizeLocation(line));
        details.push(line);
        continue;
      }
      match = line.match(/^(?:Works|Worked|Workplace|Employer)(?:\s+at)?\s*:?\s+(.+)$/i);
      if (match) {
        employers.push(normalizeAriaFact(match[1]));
        details.push(line);
        continue;
      }
      match = line.match(/^Current:\s*(.{2,100}?)\s+at\s+(.{2,140})$/i);
      if (!match) match = line.match(/^(?:Former\s+)?(.{2,100}?)\s+at\s+(.{2,140})$/i);
      if (match && !/^(?:lives|studied|went|works|worked)$/i.test(match[1])) {
        roles.push(match[1]);
        employers.push(match[2]);
        details.push(line);
        continue;
      }
      match = line.match(/^(?:Studied at|Went to|College|School|Education)\s*:?\s*(.+)$/i);
      if (match) {
        schools.push(normalizeAriaFact(match[1]));
        details.push(line);
        continue;
      }
      if (SCHOOL_WORDS.test(line) && line.length <= 180) {
        schools.push(line);
        details.push(line);
        continue;
      }
      if (COMPANY_WORDS.test(line) && line.length <= 160 && !ROLE_WORDS.test(line)) {
        employers.push(line);
        details.push(line);
        continue;
      }
      match = line.match(/^Bio\s*:?\s*(.+)$/i);
      if (match && match[1]) {
        bios.push(match[1]);
        details.push(line);
        continue;
      }
      if (line.length >= 20 && line.length <= 300
          && /(?:\bI\b|\bI'm\b|\bmy\b|\bpassionate\b|\bdedicated\b|[.!?]$)/i.test(line)
          && !/\b(?:friend request|mutual friend|people you may know)\b/i.test(line)) {
        bios.push(line);
        details.push(line);
        continue;
      }
      if (/^(?:From|Followed by|Joined Facebook|Professional mode|Digital creator)\b/i.test(line)) {
        details.push(line);
      }
    }
    return {
      location: currentLocations[0] || "",
      hometown: hometowns[0] || "",
      headline: roles[0] || (employers[0] ? `Works at ${employers[0]}` : ""),
      roles: unique(roles, 8),
      employers: unique(employers, 8),
      schools: unique(schools, 8),
      bios: unique(bios, 5),
      details: unique(details, 40),
    };
  }

  function readProfile(identity = facebookIdentity(), suppliedRegion = null) {
    if (!identity) return null;



    const root = suppliedRegion || profileRegion(identity) || document.body;
    let header = profileHeader(root, identity);
    if (!header.name && root !== document.body) {


      header = profileHeader(document.body, identity);
    }
    if (!header.name) return null;
    const visibleIdentityEvidence = profileSignals(document.body).length > 0
      || Array.from(document.querySelectorAll("a[href]"))
        .some((link) => isVisible(link) && sameIdentityHref(link, identity));
    if (!header.element && !visibleIdentityEvidence) return null;
    const bindingRoots = Array.from(new Set([
      header.element?.closest?.("[data-pagelet*='ProfileCover'], [data-pagelet*='ProfileHeader'], [aria-label*='profile header' i]"),
      root.querySelector?.("[role='tablist']"),
    ].filter(Boolean)));
    const boundIdentities = unique(bindingRoots.flatMap((area) => (
      Array.from(area.querySelectorAll("a[href]"))
        .map((link) => facebookIdentity(new URL(link.getAttribute("href"), location.href).href)?.sourceId || "")
    )), 20);
    const routeBound = boundIdentities.includes(identity.sourceId);
    if (boundIdentities.length && !routeBound) return null;
    const previous = window.__medhuntFacebookLastProfile;
    if (
      previous && previous.source_id !== identity.sourceId
      && sameLooseText(previous.name, header.name) && !routeBound
    ) return null;



    const values = boundedFactValues(root, header.element);
    const facts = extractFacts(values, header);
    const noteLines = [
      `Facebook profile: ${identity.url}`,
      facts.headline ? `Headline: ${facts.headline}` : "",
      facts.location ? `Location: ${facts.location}` : "",
      facts.hometown ? `Hometown: ${facts.hometown}` : "",
      ...facts.bios.map((value) => `Bio: ${value}`),
      ...facts.roles.map((value) => `Role: ${value}`),
      ...facts.employers.map((value) => `Employer: ${value}`),
      ...facts.schools.map((value) => `School: ${value}`),
      ...(header.alternateNames || []).map((value) => `Alternate name: ${value}`),
      ...(header.descriptors || []).map((value) => `Professional descriptor: ${value}`),
      ...facts.details.map((value) => `Visible detail: ${value}`),
    ].filter(Boolean);
    const profile = {
      name: header.name,
      location: facts.location,
      hometown: facts.hometown,
      headline: facts.headline,
      source: PLATFORM.key,
      source_url: identity.url,
      source_id: identity.sourceId,
      notes: unique(noteLines, 100).join("\n").slice(0, 20000),
      roles: facts.roles.slice(0, 12),
      employers: facts.employers.slice(0, 12),
      schools: facts.schools.slice(0, 10),
      alternate_names: (header.alternateNames || []).slice(0, 8),
      result_index: 0,
      captured_at: new Date().toISOString(),
    };
    window.__medhuntFacebookLastProfile = {
      source_id: profile.source_id,
      name: profile.name,
    };
    return profile;
  }

  function snapshot() {
    const identity = facebookIdentity();
    if (!identity) {
      return {
        ok: false,
        platform: PLATFORM.key,
        error_code: "FACEBOOK_PROFILE_URL_REQUIRED",
        error: "This Facebook URL is not an individual profile. Open a profile.php, /people/, or personal vanity profile URL.",
      };
    }
    const region = profileRegion(identity) || document.body;
    if (visibleLockedProfile(region)) {
      return {
        ok: false,
        platform: PLATFORM.key,
        error_code: "FACEBOOK_PROFILE_LOCKED",
        error: "This Facebook profile is locked. Medhunt did not capture, save, or enrich it.",
        profiles: [],
        count: 0,
        expected_count: 0,
        page_url: identity.url,
        raw: { cards: 0, names: 0, locked: 1 },
      };
    }
    if (profileSurfaceKind(identity, region) === "page") {
      return {
        ok: false,
        platform: PLATFORM.key,
        error_code: "FACEBOOK_PAGE_UNSUPPORTED",
        error: "This is a Facebook Page, not an individual personal profile. Nothing was captured.",
        profiles: [],
        count: 0,
        expected_count: 0,
        page_url: identity.url,
        raw: { cards: 0, names: 0, page: 1 },
      };
    }
    const profile = readProfile(identity, region);
    if (!profile) {
      return {
        ok: false,
        platform: PLATFORM.key,
        error_code: "FACEBOOK_PROFILE_LOADING",
        error: "Facebook is still loading the current profile identity. No profile was captured.",
        profiles: [],
        count: 0,
        expected_count: 0,
        page_url: identity.url,
      };
    }
    return {
      ok: true,
      platform: PLATFORM.key,
      platform_label: PLATFORM.label,
      profiles: [profile],
      count: 1,
      expected_count: 1,
      raw: { cards: 1, names: 1 },
      page_url: profile.source_url,
    };
  }

  const messageListener = (message, _sender, sendResponse) => {


    const messageType = message?.type === ADAPTER_REQUEST
      ? message?.original_type
      : message?.type;
    if (messageType === "MEDHUNT_PLATFORM_PING") {
      const identity = facebookIdentity();
      const region = identity ? (profileRegion(identity) || document.body) : null;
      const profileKind = identity ? profileSurfaceKind(identity, region) : "unsupported";
      sendResponse({
        ok: true,
        platform: PLATFORM.key,
        label: PLATFORM.label,
        adapter_revision: ADAPTER_REVISION,
        supported_profile: Boolean(identity && profileKind === "personal"),
        profile_kind: profileKind,
        url: location.href,
      });
      return false;
    }
    if (messageType === "MEDHUNT_CAPTURE_PLATFORM_PROFILE") {
      const result = snapshot();
      sendResponse(result.ok
        ? { ...result, adapter_revision: ADAPTER_REVISION, profile: result.profiles[0] }
        : { ...result, adapter_revision: ADAPTER_REVISION });
      return false;
    }
    if (["MEDHUNT_LIST_PLATFORM_CANDIDATES", "MEDHUNT_SCAN_PLATFORM_CANDIDATES"].includes(messageType)) {
      const result = snapshot();
      if (result.ok && messageType === "MEDHUNT_SCAN_PLATFORM_CANDIDATES") {
        chrome.runtime.sendMessage({
          type: "MEDHUNT_PLATFORM_SCAN_PROGRESS",
          platform: PLATFORM.key,
          found: 1,
          total: 1,
          preview: result.profiles.map(({ name, location, headline }) => ({ name, location, headline })),
        }, () => void chrome.runtime.lastError);
      }
      sendResponse({ ...result, adapter_revision: ADAPTER_REVISION });
      return false;
    }
    if (messageType === "MEDHUNT_OPEN_PLATFORM_CANDIDATE") {
      window.scrollTo({ top: 0, behavior: "smooth" });
      sendResponse({
        ok: true,
        adapter_revision: ADAPTER_REVISION,
        source_url: facebookIdentity()?.url || location.href,
      });
      return false;
    }
    return false;
  };
  window.__medhuntFacebookMessageListener = messageListener;
  chrome.runtime.onMessage.addListener(messageListener);

  function observedProfileState() {
    const identity = facebookIdentity();
    if (!identity) {
      return {
        signature: `unsupported|${location.href}`,
        count: 0,
        error_code: "FACEBOOK_PROFILE_URL_REQUIRED",
        page_url: location.href,
      };
    }
    const region = profileRegion(identity) || document.body;
    const kind = profileSurfaceKind(identity, region);
    if (kind === "locked") {
      return {
        signature: `locked|${identity.sourceId}`,
        count: 0,
        locked: true,
        error_code: "FACEBOOK_PROFILE_LOCKED",
        source_id: identity.sourceId,
        page_url: identity.url,
      };
    }
    if (kind === "page") {
      return {
        signature: `page|${identity.sourceId}`,
        count: 0,
        page: true,
        error_code: "FACEBOOK_PAGE_UNSUPPORTED",
        source_id: identity.sourceId,
        page_url: identity.url,
      };
    }
    const profile = readProfile(identity, region);
    if (!profile) {
      return {
        signature: `loading|${identity.sourceId}`,
        count: 0,
        error_code: "FACEBOOK_PROFILE_LOADING",
        source_id: identity.sourceId,
        page_url: identity.url,
      };
    }
    return {
      signature: JSON.stringify([
        profile.source_id, profile.name, profile.location, profile.hometown,
        profile.headline, profile.roles, profile.employers, profile.schools,
        profile.alternate_names,
      ]),
      count: 1,
      source_id: profile.source_id,
      page_url: profile.source_url,
    };
  }

  let lastSignature = "";
  function emitObservedState(force = false, extra = {}) {
    const state = observedProfileState();
    if (!force && state.signature === lastSignature) return;
    lastSignature = state.signature;
    chrome.runtime.sendMessage({
      type: "MEDHUNT_PLATFORM_RESULTS_CHANGED",
      platform: PLATFORM.key,
      adapter_revision: ADAPTER_REVISION,
      count: state.count,
      locked: Boolean(state.locked),
      page: Boolean(state.page),
      error_code: state.error_code || "",
      source_id: state.source_id || "",
      page_url: state.page_url || location.href,
      ...extra,
    }, () => void chrome.runtime.lastError);
  }

  const observer = new MutationObserver(() => {
    clearTimeout(window.__medhuntFacebookMutationTimer);
    window.__medhuntFacebookMutationTimer = setTimeout(() => {
      emitObservedState(false);
    }, 500);
  });
  window.__medhuntFacebookObserver = observer;
  observer.observe(document.documentElement, {
    childList: true, subtree: true, characterData: true, attributes: true,
    attributeFilter: [
      "href", "aria-label", "aria-selected", "aria-hidden", "role", "title",
      "data-pagelet",
    ],
  });



  emitObservedState(true, { initial: true });

  let observedIdentity = facebookIdentity()?.sourceId || "";
  window.__medhuntFacebookRouteTimer = setInterval(() => {
    const current = facebookIdentity();
    const currentId = current?.sourceId || "";
    if (currentId === observedIdentity) return;
    observedIdentity = currentId;
    lastSignature = "";
    chrome.runtime.sendMessage({
      type: "MEDHUNT_PLATFORM_RESULTS_CHANGED",
      platform: PLATFORM.key,
      count: 0,
      identity_changed: true,
      source_id: currentId,
      page_url: current?.url || location.href,
    }, () => void chrome.runtime.lastError);
    clearTimeout(window.__medhuntFacebookMutationTimer);
    window.__medhuntFacebookMutationTimer = setTimeout(() => {
      emitObservedState(false);
    }, 100);
  }, 400);
})();
