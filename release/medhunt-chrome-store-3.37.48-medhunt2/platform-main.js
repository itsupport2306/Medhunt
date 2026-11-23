(() => {
  "use strict";

  if (window.__medhuntPlatformMainLoaded) return;
  window.__medhuntPlatformMainLoaded = true;

  function plainText(value) {
    return value == null ? "" : String(value).trim();
  }

  function plainList(value, mapper = plainText) {
    return Array.isArray(value) ? value.map(mapper).filter(Boolean) : [];
  }

  function sanitizeZipCandidate(candidate) {
    if (!candidate?.encryptedJobseekerId) return null;
    return {
      encryptedJobseekerId: plainText(candidate.encryptedJobseekerId),
      name: plainText(candidate.name),
      location: plainText(candidate.location),
      yearsOfExperience: Number(candidate.yearsOfExperience) || 0,
      employment: plainList(candidate.employment, (item) => ({
        company: plainText(item?.company),
        position: plainText(item?.position),
        startDate: plainText(item?.startDate),
        endDate: plainText(item?.endDate),
      })),
      education: plainList(candidate.education, (item) => ({
        school: plainText(item?.school),
        degree: plainText(item?.degree),
      })),
      skills: plainList(candidate.skills),
      licenses: plainList(candidate.licenses),
      certifications: plainList(candidate.certifications),
    };
  }

  function zipCandidatesFromReact() {
    const selectors = [
      "section.relative.p-24.bg-white",
      "section.relative.p-24",
      "section.relative",
      "[class*='candidate-card']",
      "article",
    ];
    let cards = [];
    for (const selector of selectors) {
      try {
        cards = Array.from(document.querySelectorAll(selector));
      } catch {
        cards = [];
      }
      if (cards.length) break;
    }

    const output = [];
    const seen = new Set();
    for (const card of cards) {
      const reactKey = Object.keys(card).find((key) => key.startsWith("__reactFiber$"));
      if (!reactKey) continue;
      let fiber = card[reactKey];
      let depth = 0;
      while (fiber && depth < 120) {
        const candidate = fiber.memoizedProps?.candidate || fiber.pendingProps?.candidate;
        const sanitized = sanitizeZipCandidate(candidate);
        if (sanitized?.encryptedJobseekerId && !seen.has(sanitized.encryptedJobseekerId)) {
          seen.add(sanitized.encryptedJobseekerId);
          output.push(sanitized);
          break;
        }
        fiber = fiber.return;
        depth += 1;
      }
    }
    return output;
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.data?.type !== "MEDHUNT_PLATFORM_MAIN_REQUEST") return;
    const requestId = plainText(event.data.requestId);
    if (!requestId) return;
    let candidates = [];
    let error = "";
    try {
      if (/(^|\.)ziprecruiter\.com$/i.test(location.hostname)) {
        candidates = zipCandidatesFromReact();
      }
    } catch (caught) {
      error = plainText(caught?.message || caught);
    }
    window.postMessage({
      type: "MEDHUNT_PLATFORM_MAIN_RESPONSE",
      requestId,
      candidates,
      error,
    }, "*");
  });
})();
