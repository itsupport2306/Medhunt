(() => {
  "use strict";

  const ADAPTER_REVISION = "healthcare-directory-v13";
  const ADAPTER_REQUEST = "MEDHUNT_HEALTHCARE_DIRECTORY_V13_REQUEST";
  if (window.__medhuntHealthcareDirectoryAdapterRevision === ADAPTER_REVISION) return;
  window.__medhuntHealthcareDirectoryAdapterRevision = ADAPTER_REVISION;

  const host = location.hostname.toLowerCase();
  const matches = (root) => host === root || host.endsWith(`.${root}`);
  const PLATFORM = matches("npino.com")
    ? { key: "npino", label: "NPI No." }
    : matches("npiprofile.com")
      ? { key: "npiprofile", label: "NPI Profile" }
      : host === "eservices.nysed.gov"
        ? { key: "nysed", label: "NYSED" }
        : host === "health.usnews.com"
          ? { key: "usnews", label: "U.S. News Doctor Finder" }
          : matches("medifind.com")
            ? { key: "medifind", label: "MediFind" }
            : matches("commonspirit.org")
              ? { key: "commonspirit", label: "CommonSpirit Health" }
              : host === "providers.sharecare.com"
                ? { key: "sharecare", label: "Sharecare" }
                : host === "doctor.webmd.com"
                  ? { key: "webmd", label: "WebMD" }
                  : null;
  if (!PLATFORM) return;

  let lastProfiles = [];
  let lastElements = new Map();
  let scanInProgress = false;

  function all(selector, root = document) {
    try { return Array.from(root.querySelectorAll(selector)); } catch { return []; }
  }

  function clean(value, limit = 1000) {
    return String(value ?? "")
      .replace(/\u00a0/g, " ")
      .replace(/[\u0000-\u001f\u007f]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, limit);
  }

  function visibleText(element) {
    return clean(element?.innerText || element?.textContent || "", 4000);
  }

  function isVisible(element) {
    if (!element || !element.isConnected || element.hidden || element.getAttribute?.("aria-hidden") === "true") {
      return false;
    }
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function unique(values) {
    return [...new Set((values || []).map((value) => clean(value)).filter(Boolean))];
  }

  function absoluteUrl(value) {
    try {
      const url = new URL(value || location.href, location.href);
      url.hash = "";
      return url.href;
    } catch {
      return location.href;
    }
  }





  const PROVIDER_CREDENTIAL = /^(?:M\.?D\.?|D\.?O\.?|M\.?P\.?H\.?|Ph\.?D\.?|DNP|APRN(?:-(?:C|BC|CNP))?|APN|ANP|NP(?:-(?:C|BC))?|FNP(?:-(?:C|BC))?|PNP(?:-(?:AC|PC|C|BC))?|C\.?P\.?N\.?P(?:-(?:AC|PC|C|BC))?|ACNP(?:-BC)?|AGNP(?:-(?:C|BC))?|AGACNP(?:-BC)?|AGPCNP(?:-(?:C|BC))?|WHNP(?:-(?:C|BC))?|NNP(?:-(?:C|BC))?|PMHNP(?:-(?:C|BC))?|CRNP|CNP|CNM|CRNA|CNS|CCRN|PA-C|R\.?\s*N\.?(?:-BC)?|LPN|LVN|MSN|B\.?\s*S\.?\s*N\.?|BSNRN|LCCE|REGISTERED NURSE|NURSE PRACTITIONER|DDS|DMD|FACP|FACOG)\.?$/i;

  function providerCredentials(value) {
    const parts = clean(value, 240)
      .replace(/^(?:dr\.?|doctor)\s+/i, "")
      .split(",")
      .map((part) => clean(part, 60));
    return unique(parts.slice(1).filter((part) => PROVIDER_CREDENTIAL.test(part)));
  }

  function providerProfession(credentials) {
    const values = unique(credentials).join(" ");
    if (/\b(?:M\.?D\.?|D\.?O\.?)\b/i.test(values)) return "Physician";
    if (/\bCRNA\b/i.test(values)) return "Nurse Anesthetist";
    if (/\b(?:DNP|APRN(?:-(?:C|BC|CNP))?|NP(?:-(?:C|BC))?|FNP(?:-(?:C|BC))?|ACNP(?:-BC)?|AGACNP(?:-BC)?|AGPCNP(?:-(?:C|BC))?|PNP(?:-(?:AC|PC|C|BC))?|CPNP(?:-(?:AC|PC|C|BC))?|WHNP(?:-(?:C|BC))?|NNP(?:-(?:C|BC))?|PMHNP(?:-(?:C|BC))?|AGNP(?:-(?:C|BC))?|CRNP|CNP|CNM)\b/i.test(values)) {
      return "Nurse Practitioner";
    }
    if (/\bPA-C\b/i.test(values)) return "Physician Assistant";
    if (/\b(?:DDS|DMD)\b/i.test(values)) return "Dentist";
    if (/\b(?:RN(?:-BC)?|LPN|LVN|CNS|CCRN)\b/i.test(values)) return "Nurse";
    return "Healthcare Provider";
  }

  function cleanProviderName(value) {
    const parts = clean(value, 240)
      .replace(/^(?:dr\.?|doctor|mr\.?|mrs\.?|ms\.?)\s+/i, "")
      .replace(/\s*\((?:individual|person)\)\s*$/i, "")
      .split(",")
      .map((part) => clean(part, 180));
    while (parts.length > 1 && PROVIDER_CREDENTIAL.test(parts[parts.length - 1])) parts.pop();
    const words = (parts.join(", ") || "").split(/\s+/).filter(Boolean);
    while (words.length > 2 && PROVIDER_CREDENTIAL.test(words[words.length - 1])) words.pop();
    return words.join(" ").replace(/,+$/g, "").trim();
  }

  const NON_PERSON = /\b(?:associates?|center|clinic|company|corporation|department|group|health(?:care)?|hospital|institute|laborator(?:y|ies)|llc|medical practice|partners?|pharmacy|professional corporation|services?|specialists?\s+(?:of|pc\b)|university)\b/i;
  const NAVIGATION_NAME = /^(?:address|date of licensure|individual|license|licensee|name|npi|npi profile|profession|provider|search|status|view profile)$/i;

  function looksLikePersonName(value) {
    const name = cleanProviderName(value);
    if (!name || name.length < 3 || name.length > 120 || /\d|@|https?:/i.test(name)) return false;
    if (NAVIGATION_NAME.test(name) || NON_PERSON.test(name)) return false;
    const words = name.split(/\s+/).filter(Boolean);
    return words.length >= 2 && words.length <= 10 && words.every((word) => (
      /^[\p{L}\p{M}.'\u2019-]+$/u.test(word)
    ));
  }

  function locationFromAddress(value) {
    const address = clean(value, 500)
      .replace(/^address\s*:?\s*/i, "")
      .replace(/\b(?:phone|fax)\s*:.*$/i, "")
      .trim();
    const commaParts = address.split(",").map((part) => clean(part)).filter(Boolean);
    for (let index = commaParts.length - 1; index >= 1; index -= 1) {
      const state = commaParts[index].match(/^([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?$/i);
      if (state) return `${commaParts[index - 1]}, ${state[1].toUpperCase()}`;
    }
    const match = address.match(/\b([A-Za-z][A-Za-z.'\u2019-]*(?:\s+[A-Za-z][A-Za-z.'\u2019-]*){0,3})\s*,?\s+([A-Z]{2})\s+\d{5}(?:-\d{4})?\b/i);
    return match ? `${clean(match[1])}, ${match[2].toUpperCase()}` : "";
  }

  function npiFrom(value) {
    return clean(value, 2000).match(/\b(\d{10})\b/)?.[1] || "";
  }

  function smallestContainer(element, required) {
    let node = element;
    for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
      const text = visibleText(node);
      if (required.test(text) && text.length <= 3000) return node;
      if (node.matches?.("article, li, tr")) break;
    }
    return element;
  }

  function specialtyFromPage() {
    const taxonomy = location.pathname.match(/\/taxonomy\/code\/([A-Za-z0-9]+)/i)?.[1] || "";
    const headings = all("h1, h2").map(visibleText);
    const heading = headings.find((text) => taxonomy && text.toLowerCase().includes(taxonomy.toLowerCase())) || headings[0] || "";
    return clean(heading
      .replace(new RegExp(taxonomy, "ig"), "")
      .replace(/\b(?:providers?|doctors?|records?|in the u\.?s\.?|nationwide|iowa|pennsylvania)\b.*$/i, "")
      .replace(/^[\s:\-\u2013]+|[\s:\-\u2013]+$/g, ""), 180);
  }

  function profileNotes({ npi = "", license = "", profession = "", specialty = "", address = "", extra = "" }) {
    return unique([
      specialty && `Specialty: ${specialty}`,
      profession && `Profession: ${profession}`,
      npi && `NPI: ${npi}`,
      license && `License: ${license}`,
      address && `Address: ${address}`,
      extra,
    ]).join("\n").slice(0, 12000);
  }

  function jsonLdObjects(root = document) {
    const values = [];
    for (const script of all('script[type="application/ld+json"]', root)) {
      try {
        const pending = [JSON.parse(script.textContent || "null")];
        while (pending.length) {
          const value = pending.shift();
          if (Array.isArray(value)) {
            pending.push(...value);
            continue;
          }
          if (!value || typeof value !== "object") continue;
          values.push(value);
          if (Array.isArray(value["@graph"])) pending.push(...value["@graph"]);


          if (value.mainEntity && typeof value.mainEntity === "object") pending.push(value.mainEntity);

          if (Array.isArray(value.provider)) pending.push(...value.provider);
          else if (value.provider && typeof value.provider === "object") pending.push(value.provider);
        }
      } catch {

      }
    }
    return values;
  }

  function hasSchemaType(value, expected) {
    const types = Array.isArray(value?.["@type"]) ? value["@type"] : [value?.["@type"]];
    return types.some((type) => clean(type, 80).toLowerCase() === expected.toLowerCase());
  }

  function schemaName(value) {
    const raw = typeof value === "string" ? value : value?.name;
    const text = clean(raw, 180);
    if (!text) return "";


    try {
      const parsed = new URL(text);
      if (parsed.hostname.toLowerCase() === "schema.org") {
        const tail = decodeURIComponent(parsed.pathname.split("/").filter(Boolean).pop() || "");
        if (tail) return clean(tail.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " "), 180);
      }
    } catch { /* retain non-URL labels */ }
    return text;
  }

  function schemaNames(value) {
    const names = [];
    for (const item of schemaValues(value)) {
      if (typeof item === "string") {
        names.push(schemaName(item));
      } else if (Array.isArray(item?.name)) {
        names.push(...item.name.map((entry) => schemaName(entry)));
      } else if (item?.name) {
        names.push(schemaName(item));
      }
    }
    return unique(names);
  }

  function metaContent(selector) {
    return clean(document.querySelector(selector)?.getAttribute("content"), 4000);
  }

  function canonicalPageUrl() {
    return absoluteUrl(document.querySelector('link[rel="canonical"]')?.getAttribute("href") || location.href);
  }

  function sameProfileUrl(firstValue, secondValue) {
    try {
      const first = new URL(firstValue, location.href);
      const second = new URL(secondValue, location.href);
      return first.hostname.toLowerCase() === second.hostname.toLowerCase()
        && first.pathname.replace(/\/+$/, "").toLowerCase() === second.pathname.replace(/\/+$/, "").toLowerCase();
    } catch {
      return false;
    }
  }

  function valuesForHeading(label, itemSelector) {
    const title = heading(document, label);
    if (!title) return [];
    const container = title.parentElement;
    if (!container) return [];
    const selector = itemSelector || '[class*="credentials__credential-item"], .csh-aem-provider-details__list-item';
    return unique(all(selector, container).map(visibleText));
  }

  function postalAddress(value) {
    if (!value || typeof value !== "object") return "";
    const city = clean(value.addressLocality, 120);
    const state = clean(value.addressRegion, 80);
    const postalCode = clean(value.postalCode, 30);
    const cityRegion = [city, state].filter(Boolean).join(", ");
    return [clean(value.streetAddress, 240), [cityRegion, postalCode].filter(Boolean).join(" ")]
      .filter(Boolean)
      .join(", ");
  }

  function exactUsNewsProfilePath() {
    return /^\/(?:doctors|nurse-practitioners)\/[^/?#]+-\d+\/?$/i.test(location.pathname);
  }

  function exactMediFindProfilePath(value = location.pathname) {
    return /^\/doctors\/[^/?#]+\/\d+\/?$/i.test(value);
  }

  function exactCommonSpiritProfilePath(value = location.pathname) {
    return /^\/find-a-doctor\/[^/?#]+-\d+\/?$/i.test(value);
  }

  function exactSharecareProfilePath(value = location.pathname) {
    return /^\/doctor\/[^/?#]+\/?$/i.test(value);
  }

  function schemaValues(value) {
    return Array.isArray(value) ? value : [value];
  }

  function boardCertifications(description) {
    const match = clean(description, 4000).match(/\bboard certified in\s+(.+?)(?:\.|$)/i);
    if (!match) return [];
    return unique(match[1].split(/,|\band\b/i).map((value) => (
      value && `Board certified in ${clean(value, 180)}`
    )));
  }

  function medifindCard(physician) {
    const sourceUrl = absoluteUrl(physician?.url || location.href);
    let path = "";
    try { path = new URL(sourceUrl).pathname; } catch { /* absoluteUrl supplied a safe fallback. */ }
    const sourceId = path.match(/\/(\d+)\/?$/)?.[1] || "";
    return (sourceId && document.getElementById(`card_doctor_${sourceId}`))
      || all('a[data-link-type="doctor-name"][href*="/doctors/"]').find((link) => (
        absoluteUrl(link.getAttribute("href")) === sourceUrl
      ))?.closest('[id^="card_doctor_"], article, li')
      || (exactMediFindProfilePath() ? document : null);
  }

  function medifindProfileDocument(physician, root) {
    const sourceUrl = absoluteUrl(physician?.url || location.href);
    if (!exactMediFindProfilePath(new URL(sourceUrl).pathname)) return null;
    const exactPage = exactMediFindProfilePath() && sameProfileUrl(sourceUrl, location.href);
    const pageRoot = exactPage ? document : root;
    const rawName = exactPage
      ? visibleText(document.querySelector('[class*="doctors-single_name"] span, main h1'))
      : (physician?.name || visibleText(root?.querySelector?.("h1, h2, h3")));
    const name = cleanProviderName(rawName || physician?.name);
    if (!looksLikePersonName(name)) return null;
    const specialties = unique([
      ...(exactPage ? valuesForHeading("Specialties") : []),
      ...schemaNames(physician?.medicalSpecialty),
      visibleText(pageRoot?.querySelector?.(
        '[class*="doctors-single_specialties"], [class*="DoctorCard_header__specialties"]',
      )),
    ]);
    const specialty = specialties[0] || "";
    const hospitals = unique([
      ...(exactPage ? valuesForHeading("Hospital Affiliations") : []),
      ...schemaNames(physician?.hospitalAffiliation),
      ...all(
        '[class*="doctors-single_sub-name"], [class*="DoctorCard_body__alt-container__affiliation"]',
        pageRoot || document,
      ).map(visibleText),
    ]);
    const summary = clean(
      visibleText(pageRoot?.querySelector?.(
        '[class*="DoctorProfileOverview_biography"], [class*="DoctorCard_body__biography"]',
      )) || physician?.description,
      4000,
    );
    const education = exactPage ? unique([
      ...valuesForHeading("Graduate Institution").map((value) => `Graduate Institution — ${value}`),
      ...valuesForHeading("Residency").map((value) => `Residency — ${value}`),
      ...valuesForHeading("Fellowships").map((value) => `Fellowship — ${value}`),
    ]) : [];
    const licenses = exactPage ? valuesForHeading("Licenses") : [];
    const certifications = unique([
      ...(exactPage ? valuesForHeading("Board Certifications") : []),
      ...boardCertifications(summary),
    ]);
    const languages = exactPage ? valuesForHeading("Languages Spoken") : [];
    const address = postalAddress(physician?.address)
      || visibleText(pageRoot?.querySelector?.(
        '[class*="doctors-single_doctor-address"] [class*="CardAddress_card-address__content"], [class*="CardAddress_card-address__content"]',
      ));
    const locationValue = physician?.address && typeof physician.address === "object"
      ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
          .filter(Boolean).join(", ")
      : locationFromAddress(address);
    if (exactPage && !education.length && !licenses.length && !certifications.length) {
      return null;
    }
    return {
      kind: "public_professional_profile",
      source_label: "MediFind",
      source_url: sourceUrl,
      headline: specialty || "Physician",
      credentials: providerCredentials(rawName),
      summary,
      specialties,
      subspecialties: [],
      hospitals,
      education,
      licenses,
      certifications,
      languages,
      years_experience: summary.match(/\b(\d+\+?)\s+years?\b/i)?.[1] || "",
      npi: npiFrom(sourceUrl),
      address,
      location: locationValue,
    };
  }

  function medifindProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    let physicians = jsonLdObjects().filter((value) => hasSchemaType(value, "Physician"));
    if (exactMediFindProfilePath()) {
      physicians = physicians.filter((value) => sameProfileUrl(value?.url || "", location.href));
      if (!physicians.length) {
        const rawName = visibleText(document.querySelector('[class*="doctors-single_name"] span, main h1'));
        if (looksLikePersonName(cleanProviderName(rawName))) {
          physicians = [{ name: rawName, url: location.href }];
        }
      }
    }
    for (const physician of physicians) {
      const sourceUrl = absoluteUrl(physician?.url || location.href);
      let sourcePath = "";
      try { sourcePath = new URL(sourceUrl).pathname; } catch { /* Ignore invalid structured URLs. */ }
      if (!exactMediFindProfilePath(sourcePath)) continue;
      const sourceId = sourcePath.match(/\/(\d+)\/?$/)?.[1] || sourcePath.replace(/^\/+|\/+$/g, "");
      if (!sourceId || seen.has(sourceId)) continue;
      const root = medifindCard(physician) || document;
      const name = cleanProviderName(physician?.name || visibleText(root.querySelector?.("h1, h2, h3")));
      if (!looksLikePersonName(name)) continue;
      const documentProfile = medifindProfileDocument(physician, root);
      if (exactMediFindProfilePath() && !documentProfile) continue;
      const specialty = documentProfile?.specialties?.[0] || "";
      const hospitals = documentProfile?.hospitals || [];
      const certifications = documentProfile?.certifications || [];
      const extra = unique([
        ...hospitals.map((value) => `Employer: ${value}`),
        ...certifications.map((value) => `Certification: ${value}`),
        documentProfile?.summary,
      ]).join("\n");
      seen.add(sourceId);
      profiles.push({
        name,
        location: documentProfile?.location || "",
        headline: specialty || "Physician",
        specialty,
        specialties: specialty ? [specialty] : [],
        notes: profileNotes({
          profession: "Physician", specialty,
          address: documentProfile?.address || "", extra,
        }),
        roles: ["Physician"],
        employers: hospitals,
        schools: documentProfile?.education || [],
        licenses: documentProfile?.licenses || [],
        certifications,
        profile_document: documentProfile || undefined,
        source: PLATFORM.key,
        source_url: sourceUrl,
        source_id: sourceId,
        captured_at: new Date().toISOString(),
      });
      elements.set(sourceId, root === document ? document.documentElement : root);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function commonSpiritCard(link) {




    return link?.closest?.("[id^='profile-card-'], [id^='card-'], .csh-aem-result-card--profilewrap")
      || link?.closest?.(".csh-aem-result-card--provider")
      || link?.parentElement;
  }

  function commonSpiritAnalytics(card) {
    const element = card?.querySelector?.("[data-analytics]") || card?.closest?.("[data-analytics]");
    const raw = element?.getAttribute?.("data-analytics") || "";
    if (!raw) return {};
    try {
      return JSON.parse(raw);
    } catch {
      try {
        return JSON.parse(raw.replace(/&quot;/g, '"').replace(/&#39;/g, "'"));
      } catch {
        return {};
      }
    }
  }

  function commonSpiritProfileDocument(profile, card, physician = null) {
    const sourceUrl = absoluteUrl(profile.source_url || physician?.url || canonicalPageUrl());
    if (!exactCommonSpiritProfilePath(new URL(sourceUrl).pathname)) return null;
    const exactPage = exactCommonSpiritProfilePath() && sameProfileUrl(sourceUrl, canonicalPageUrl());
    const specialties = unique([
      ...(exactPage ? all("#specialties .csh-aem-provider-details__list-item").map(visibleText) : []),
      ...schemaNames(physician?.medicalSpecialty),
      ...(profile.specialties || []),
    ]);
    const specialty = specialties[0] || "";
    const hospitals = unique([
      ...(exactPage ? all("#medical_groups .csh-aem-medical-groups__list-item").map(visibleText) : []),
      ...(exactPage ? all('[class*="provider-hero__org-unit"], [class*="provider-hero__location-name"]').map(visibleText) : []),
      ...schemaNames(physician?.hospitalAffiliation),
      ...(profile.employers || []),
    ]);
    const education = unique([
      ...(exactPage ? all("#education .csh-aem-provider-details__list-item").map(visibleText) : []),
      ...schemaNames(physician?.alumni),
    ]);
    const certifications = unique([
      ...(exactPage ? all("#credentials .csh-aem-provider-details__list-item").map(visibleText) : []),
      ...schemaNames(physician?.hasCredential),
    ]);
    const languages = unique([
      ...(exactPage ? all("#languages .csh-aem-provider-details__list-item").map(visibleText) : []),
      ...schemaNames(physician?.knowsLanguage),
    ]);
    const summary = clean(
      (exactPage && visibleText(document.querySelector("#about .csh-aem-show-more-content-block__content")))
      || physician?.description
      || metaContent('meta[name="description"]')
      || profile.notes,
      4000,
    );
    const address = postalAddress(physician?.address)
      || (exactPage ? visibleText(document.querySelector('[class*="provider-hero__address"]')) : "")
      || profile.address
      || "";
    const locationValue = physician?.address && typeof physician.address === "object"
      ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
          .filter(Boolean).join(", ")
      : (profile.location || locationFromAddress(address));
    return {
      kind: "public_professional_profile",
      source_label: "CommonSpirit Health",
      source_url: sourceUrl,
      headline: profile.headline || specialty || "Healthcare Provider",
      credentials: profile.credentials || [],
      summary,
      specialties,
      subspecialties: [],
      hospitals,
      education,
      licenses: [],
      certifications,
      languages,
      years_experience: "",
      npi: profile.source_id || "",
      address,
      location: locationValue,
    };
  }

  function commonSpiritProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    if (exactCommonSpiritProfilePath()) {
      const sourceUrl = canonicalPageUrl();
      const sourcePath = new URL(sourceUrl).pathname;
      const sourceId = sourcePath.match(/-(\d+)\/?$/)?.[1] || "";
      let physician = jsonLdObjects()
        .filter((value) => hasSchemaType(value, "Physician"))
        .find((value) => !value?.url || sameProfileUrl(value.url, sourceUrl)) || null;
      const titleParts = clean(
        metaContent('meta[property="og:title"]') || document.title,
        500,
      ).split("|").map((value) => clean(value, 180));
      const rawName = visibleText(document.querySelector('[class*="provider-hero__title"], main h1'))
        || titleParts[0]
        || physician?.name
        || "";
      const name = cleanProviderName(rawName);
      if (sourceId && looksLikePersonName(name)) {
        physician ||= { name: rawName, url: sourceUrl };
        const credentials = providerCredentials(rawName);
        const profession = providerProfession(credentials);
        const specialties = unique([
          ...all('[class*="provider-hero"] ul.tags li.specialty').map(visibleText),
          ...all("#specialties .csh-aem-provider-details__list-item").map(visibleText),
          ...schemaNames(physician.medicalSpecialty),
          titleParts[1],
        ]);
        const address = postalAddress(physician.address)
          || visibleText(document.querySelector('[class*="provider-hero__address"]'));
        const titleLocation = /,\s*[A-Z]{2}$/i.test(titleParts[2] || "") ? titleParts[2] : "";
        const locationValue = physician.address && typeof physician.address === "object"
          ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
              .filter(Boolean).join(", ")
          : (locationFromAddress(address) || titleLocation);
        const employers = unique([
          ...all("#medical_groups .csh-aem-medical-groups__list-item").map(visibleText),
          ...all('[class*="provider-hero__org-unit"], [class*="provider-hero__location-name"]').map(visibleText),
          ...schemaNames(physician.hospitalAffiliation),
        ]);
        const baseProfile = {
          name,
          location: locationValue,
          headline: specialties[0] || profession,
          specialty: specialties[0] || "",
          specialties,
          credentials,
          address,
          roles: [profession],
          employers,
          schools: [],
          licenses: [],
          certifications: [],
          source: PLATFORM.key,
          source_url: sourceUrl,
          source_id: sourceId,
          captured_at: new Date().toISOString(),
        };
        const documentProfile = commonSpiritProfileDocument(baseProfile, document, physician);
        const extra = unique([
          ...documentProfile.hospitals.map((value) => `Employer: ${value}`),
          ...documentProfile.education.map((value) => `Education: ${value}`),
          ...documentProfile.certifications.map((value) => `Certification: ${value}`),
          ...documentProfile.languages.map((value) => `Language: ${value}`),
          documentProfile.summary,
        ]).join("\n");
        Object.assign(baseProfile, {
          employers: documentProfile.hospitals,
          schools: documentProfile.education,
          certifications: documentProfile.certifications,
          notes: profileNotes({
            npi: sourceId,
            profession,
            specialty: specialties[0] || "",
            address: documentProfile.address,
            extra,
          }),
          profile_document: documentProfile,
          _detail_capture_ready: Boolean(
            document.querySelector("#education, #credentials, #medical_groups")
            && document.querySelector('[class*="provider-hero__title"], main h1')
          ),
        });
        profiles.push(baseProfile);
        elements.set(sourceId, document.documentElement);
      }
      return { profiles, elements };
    }
    const links = all('a[href*="/find-a-doctor/"]');
    for (const link of links) {
      const sourceUrl = absoluteUrl(link.getAttribute("href"));
      let sourcePath = "";
      try { sourcePath = new URL(sourceUrl).pathname; } catch { continue; }
      if (!/^\/find-a-doctor\/[^/?#]+-\d+\/?$/i.test(sourcePath)) continue;
      const sourceId = sourcePath.match(/-(\d+)\/?$/)?.[1] || "";
      if (!sourceId || seen.has(sourceId)) continue;
      const card = commonSpiritCard(link);
      const analytics = commonSpiritAnalytics(card);
      const locationData = analytics.primaryLocation && typeof analytics.primaryLocation === "object"
        ? analytics.primaryLocation
        : {};
      const rawName = visibleText(link.querySelector("h1, h2, h3, h4, h5") || link);
      const name = cleanProviderName(rawName);
      if (!looksLikePersonName(name)) continue;
      const credentials = providerCredentials(rawName);
      const profession = providerProfession(credentials);
      const specialties = unique(all(
        ".csh-aem-result-card__specialties__item--secondary",
        card || document,
      ).map(visibleText));
      const cardHeading = visibleText(card?.querySelector?.(
        ".csh-aem-result-card--wrap h6, .csh-aem-result-card--wrap h5",
      ));
      const locationType = clean(locationData.locationType, 180);
      const inferredSpecialty = locationType && !/^medical group clinic$/i.test(locationType)
        ? locationType
        : clean(cardHeading.split(/\s+-\s+/)[0], 180);
      if (!specialties.length && inferredSpecialty) specialties.push(inferredSpecialty);
      const employers = unique(all(
        ".csh-aem-result-card__specialties__item--primary",
        card || document,
      ).map(visibleText));
      employers.push(...(Array.isArray(locationData.medicalGroups) ? locationData.medicalGroups : []));
      if (!employers.length && locationData.locationName) employers.push(locationData.locationName);
      if (!employers.length && cardHeading) employers.push(cardHeading);
      const address = visibleText(card?.querySelector?.(
        ".csh-aem-result-card__line--address, .csh-aem-result-card--wrap p",
      )) || clean(locationData.locationFullAddress, 500);
      const location = locationFromAddress(address);
      const specialty = specialties[0] || "";
      const extra = unique([
        ...unique(employers).map((value) => `Employer: ${value}`),
        locationData.locationType && `Location type: ${locationData.locationType}`,
        visibleText(card?.querySelector?.(
          ".csh-aem-result-card__location-type, .csh-aem-result-card__line:first-child",
        )),
      ]).join("\n");
      const profile = {
        name,
        location,
        headline: specialty || profession,
        specialty,
        specialties,
        credentials,
        address,
        notes: profileNotes({
          profession, specialty, address, extra,
        }),
        roles: [profession],
        employers: unique(employers),
        schools: [],
        licenses: [],
        certifications: [],
        source: PLATFORM.key,
        source_url: sourceUrl,
        source_id: sourceId,
        captured_at: new Date().toISOString(),
      };
      profile.profile_document = commonSpiritProfileDocument(profile, card) || undefined;
      seen.add(sourceId);
      profiles.push(profile);
      elements.set(sourceId, card || document.documentElement);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function sharecareSectionValues(labels) {
    const values = [];
    for (const label of labels) {
      const title = heading(document, label);
      if (!title) continue;
      const list = nextElementMatching(title, "ul, ol, dl");
      if (list) {
        const items = all("li, dd", list).map(visibleText);
        values.push(...(items.length ? items : [visibleText(list)]));
        continue;
      }
      const paragraph = nextElementMatching(title, "p");
      if (paragraph) values.push(visibleText(paragraph));
    }
    return unique(values);
  }

  function sharecareProfileDocument(physician, profile) {
    const sourceUrl = absoluteUrl(profile?.source_url || physician?.url || canonicalPageUrl());
    let source;
    try { source = new URL(sourceUrl); } catch { return null; }
    if (source.hostname.toLowerCase() !== "providers.sharecare.com"
      || !exactSharecareProfilePath(source.pathname)) return null;
    const exactPage = exactSharecareProfilePath()
      && sameProfileUrl(sourceUrl, canonicalPageUrl());
    const rawName = exactPage
      ? visibleText(document.querySelector("main h1, h1")) || physician?.name
      : physician?.name || profile?.name;
    const name = cleanProviderName(rawName);
    if (!looksLikePersonName(name)) return null;
    const specialties = unique([
      ...schemaNames(physician?.medicalSpecialty),
      ...sharecareSectionValues(["Specialties", "Specialty", "Areas of Expertise"]),
      ...(profile?.specialties || []),
    ]);
    const hospitals = unique([
      ...schemaNames(physician?.hospitalAffiliation),
      ...sharecareSectionValues(["Hospital Affiliations", "Affiliations"]),
      ...(profile?.employers || []),
    ]);
    const education = unique([
      ...schemaNames(physician?.alumni),
      ...schemaNames(physician?.education),
      ...sharecareSectionValues([
        "Education", "Education & Training", "Medical Education", "Medical School",
        "Residency", "Fellowship",
      ]),
    ]);
    const certifications = unique([
      ...schemaNames(physician?.hasCredential),
      ...sharecareSectionValues(["Board Certifications", "Certifications", "Credentials"]),
    ]);
    const licenses = sharecareSectionValues(["Licenses", "Medical Licenses", "State Licenses"]);
    const languages = unique([
      ...schemaNames(physician?.knowsLanguage),
      ...sharecareSectionValues(["Languages", "Languages Spoken"]),
    ]);
    const summary = clean(
      physician?.description
        || (exactPage && visibleText(document.querySelector(
          '[class*="biography"], [class*="Biography"], [class*="overview"], [class*="Overview"]',
        )))
        || (exactPage && metaContent('meta[name="description"]')),
      4000,
    );
    const address = postalAddress(physician?.address)
      || (exactPage && visibleText(document.querySelector(
        '[class*="address"], [itemprop="address"]',
      )))
      || profile?.address
      || "";
    const locationValue = physician?.address && typeof physician.address === "object"
      ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
          .filter(Boolean).join(", ")
      : (profile?.location || locationFromAddress(address));
    const schemaIdentifier = typeof physician?.identifier === "string"
      ? physician.identifier
      : physician?.identifier?.value || "";
    const npi = npiFrom(
      document.querySelector("[data-npi]")?.getAttribute("data-npi")
        || schemaIdentifier || profile?.source_id || "",
    );
    const credentials = profile?.credentials || providerCredentials(rawName);
    return {
      kind: "public_professional_profile",
      source_label: "Sharecare",
      source_url: sourceUrl,
      headline: specialties[0] || profile?.headline || providerProfession(credentials),
      credentials,
      summary,
      specialties,
      subspecialties: [],
      hospitals,
      education,
      licenses,
      certifications,
      languages,
      years_experience: summary.match(/\b(\d+\+?)\s+years?\b/i)?.[1] || "",
      npi,
      address,
      location: locationValue,
    };
  }

  function sharecareProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    const physicians = jsonLdObjects().filter((value) => hasSchemaType(value, "Physician"));
    if (exactSharecareProfilePath()) {
      const sourceUrl = canonicalPageUrl();
      let physician = physicians.find((value) => !value?.url || sameProfileUrl(value.url, sourceUrl)) || null;
      const rawName = visibleText(document.querySelector("main h1, h1"))
        || physician?.name
        || metaContent('meta[property="og:title"]')
        || document.title;
      const name = cleanProviderName(rawName);
      if (looksLikePersonName(name)) {
        physician ||= { name: rawName, url: sourceUrl };
        const address = postalAddress(physician.address);
        const specialty = schemaNames(physician.medicalSpecialty)[0] || "";
        const location = physician.address && typeof physician.address === "object"
          ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
              .filter(Boolean).join(", ")
          : locationFromAddress(address);
        const sourceId = npiFrom(
          document.querySelector("[data-npi]")?.getAttribute("data-npi")
            || (typeof physician.identifier === "string" ? physician.identifier : physician.identifier?.value),
        ) || new URL(sourceUrl).pathname.replace(/^\/doctor\//i, "").replace(/\/$/, "");
        const credentials = providerCredentials(rawName);
        const profession = providerProfession(credentials);
        const profile = {
          name,
          location,
          headline: specialty || profession,
          specialty,
          specialties: specialty ? [specialty] : [],
          credentials,
          address,
          roles: [profession],
          employers: schemaNames(physician.hospitalAffiliation),
          schools: schemaNames(physician.alumni),
          licenses: [],
          certifications: schemaNames(physician.hasCredential),
          source: PLATFORM.key,
          source_url: sourceUrl,
          source_id: sourceId,
          captured_at: new Date().toISOString(),
        };
        const documentProfile = sharecareProfileDocument(physician, profile);
        const extra = unique([
          ...documentProfile.hospitals.map((value) => `Hospital affiliation: ${value}`),
          ...documentProfile.education.map((value) => `Education: ${value}`),
          ...documentProfile.certifications.map((value) => `Certification: ${value}`),
          ...documentProfile.licenses.map((value) => `License: ${value}`),
          documentProfile.summary,
        ]).join("\n");
        Object.assign(profile, {
          employers: documentProfile.hospitals,
          schools: documentProfile.education,
          licenses: documentProfile.licenses,
          certifications: documentProfile.certifications,
          notes: profileNotes({
            profession,
            specialty: documentProfile.specialties[0] || specialty,
            address: documentProfile.address,
            extra,
          }),
          profile_document: documentProfile,
          _detail_capture_ready: Boolean(
            documentProfile.specialties.length || documentProfile.hospitals.length
              || documentProfile.education.length || documentProfile.certifications.length
              || documentProfile.licenses.length || documentProfile.summary,
          ),
        });
        profiles.push(profile);
        elements.set(sourceId, document.documentElement);
      }
      return { profiles, elements };
    }

    const byUrl = new Map(physicians.map((physician) => [
      absoluteUrl(physician.url || ""), physician,
    ]));
    const cards = all("article.ProviderCardAlternative");
    const linksByUrl = new Map();
    for (const link of all('a[href*="/doctor/"]')) {
      const url = absoluteUrl(link.getAttribute("href"));
      const current = linksByUrl.get(url);
      if (!current || looksLikePersonName(link.textContent)) linksByUrl.set(url, link);
    }
    const entries = cards.map((card) => {
      const link = card.querySelector('a[href*="/doctor/"]');
      const url = absoluteUrl(link?.getAttribute("href") || card.getAttribute("data-href") || "");
      return { card, link, physician: byUrl.get(url) || null };
    });
    const includedUrls = new Set(entries.map((entry) => absoluteUrl(
      entry.link?.getAttribute("href") || entry.card?.getAttribute("data-href") || "",
    )));



    for (const [url, link] of linksByUrl) {
      if (!url || includedUrls.has(url)) continue;
      const card = link.closest("article, li, [class*='ProviderCard'], [class*='provider-card'], [class*='result']");
      entries.push({ card, link, physician: byUrl.get(url) || null });
      includedUrls.add(url);
    }



    for (const physician of physicians) {
      const url = absoluteUrl(physician.url || "");
      if (!url || includedUrls.has(url)) continue;
      entries.push({ card: null, link: linksByUrl.get(url) || null, physician });
      includedUrls.add(url);
    }
    for (const entry of entries) {
      const card = entry.card;
      const link = entry.link;
      const sourceUrl = absoluteUrl(
        link?.getAttribute("href") || card?.getAttribute("data-href") || entry.physician?.url || "",
      );
      let source;
      try { source = new URL(sourceUrl); } catch { continue; }
      if (source.hostname.toLowerCase() !== "providers.sharecare.com"
        || !exactSharecareProfilePath(source.pathname)) continue;
      const physician = entry.physician || byUrl.get(sourceUrl) || null;
      const rawName = visibleText(card?.querySelector?.(
        ".ProviderCardAlternative-title, h2, h3, h4",
      )) || physician?.name || visibleText(link);
      const name = cleanProviderName(rawName);
      if (!looksLikePersonName(name)) continue;
      const sourceId = npiFrom(
        card?.getAttribute("data-npi")
          || (typeof physician?.identifier === "string" ? physician.identifier : physician?.identifier?.value),
      ) || clean(card?.getAttribute("data-pwid"), 80)
        || source.pathname.replace(/^\/doctor\//i, "").replace(/\/$/, "");
      if (!sourceId || seen.has(sourceId)) continue;
      const credentials = providerCredentials(rawName);
      const profession = providerProfession(credentials);
      const address = postalAddress(physician?.address)
        || visibleText(card?.querySelector?.(
          '.ProviderCardAlternative-location-button-content a[href*="google.com/maps"], [itemprop="address"]',
        ))
        || "";
      const location = physician?.address && typeof physician.address === "object"
        ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
            .filter(Boolean).join(", ")
        : locationFromAddress(address);
      const specialties = unique([
        visibleText(card?.querySelector?.(".ProviderCardAlternative-meta, [class*='specialty'], [class*='Specialty']")),
        ...schemaNames(physician?.medicalSpecialty),
        visibleText(document.querySelector('[data-qa-target="qa-southpaw-search"]')),
      ]);
      const hospitals = schemaNames(physician?.hospitalAffiliation);
      const specialty = specialties[0] || "";
      const profile = {
        name,
        location,
        headline: specialty || profession,
        specialty,
        specialties,
        credentials,
        address,
        notes: profileNotes({
          profession,
          specialty,
          address,
          extra: hospitals.map((value) => `Hospital affiliation: ${value}`).join("\n"),
        }),
        roles: [profession],
        employers: hospitals,
        schools: schemaNames(physician?.alumni),
        licenses: [],
        certifications: schemaNames(physician?.hasCredential),
        source: PLATFORM.key,
        source_url: sourceUrl,
        source_id: sourceId,
        captured_at: new Date().toISOString(),
      };
      profile.profile_document = sharecareProfileDocument(physician, profile) || undefined;
      seen.add(sourceId);
      profiles.push(profile);
      elements.set(sourceId, card || document.documentElement);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function heading(root, label) {
    const expected = clean(label, 120).toLowerCase();
    return all("h1, h2, h3, h4, dt", root).find((element) => (
      visibleText(element).toLowerCase() === expected
    )) || null;
  }

  function nextElementMatching(element, selector) {
    let node = element?.nextElementSibling || null;
    while (node) {
      if (node.matches?.(selector)) return node;
      if (node.matches?.("h1, h2, h3, h4")) return null;
      const nested = node.querySelector?.(selector);
      if (nested) return nested;
      node = node.nextElementSibling;
    }
    return null;
  }

  function definitionPairs(list) {
    if (!list) return [];
    const pairs = [];
    let term = "";
    for (const child of Array.from(list.children || [])) {
      if (child.matches?.("dt")) {
        term = visibleText(child);
      } else if (term && child.matches?.("dd")) {
        const detail = visibleText(child);
        if (detail) pairs.push({ term, detail });
      }
    }
    return pairs;
  }

  function pairsAfterHeading(root, label) {
    return definitionPairs(nextElementMatching(heading(root, label), "dl"));
  }

  function usNewsProfileDocument(physician = null) {
    if (!exactUsNewsProfilePath()) return null;
    const hero = document.querySelector('[data-test-id^="HeroGraphic"], .hero') || document;
    const rawName = visibleText(hero.querySelector("h1") || document.querySelector("main h1"));
    const name = cleanProviderName(physician?.name || rawName);
    if (!looksLikePersonName(name)) return null;

    const credentials = unique(
      (rawName.match(/\b(?:MD|DO|DNP|APRN|NP|PA-C|RN|DDS|DMD|MPH|PhD)\b/gi) || [])
        .map((value) => value.toUpperCase()),
    );
    const schemaSpecialty = schemaName(physician?.medicalSpecialty);
    const headerSpecialty = visibleText(document.querySelector(
      '[data-tracking-placement="profile_header"][data-tracking-campaign="specialty"]'
    ));
    const specialtyPairs = all("#overview dl").flatMap(definitionPairs);
    const specialties = unique([
      schemaSpecialty,
      headerSpecialty,
      ...specialtyPairs.filter(({ term }) => /^specialty$/i.test(term)).map(({ detail }) => detail),
    ]);
    const subspecialties = unique([
      ...specialtyPairs.filter(({ term }) => /^subspecialt/i.test(term)).map(({ detail }) => detail),
    ]);

    const medicalTraining = pairsAfterHeading(document.querySelector("#experience") || document, "Medical School & Residency");
    const credentialsAndLicenses = pairsAfterHeading(
      document.querySelector("#experience") || document,
      "Certifications & Licensure",
    );
    const education = unique(medicalTraining.map(({ term, detail }) => `${term} — ${detail}`));
    const licenses = unique(credentialsAndLicenses
      .filter(({ term, detail }) => /\bstate medical license\b/i.test(`${term} ${detail}`))
      .map(({ term, detail }) => `${term} — ${detail}`));
    const certifications = unique(credentialsAndLicenses
      .filter(({ term, detail }) => !/\bstate medical license\b/i.test(`${term} ${detail}`))
      .map(({ term, detail }) => `${term} — ${detail}`));

    const schemaAffiliations = Array.isArray(physician?.hospitalAffiliation)
      ? physician.hospitalAffiliation
      : [physician?.hospitalAffiliation];
    const hospitals = unique([
      ...schemaAffiliations.map(schemaName),
      ...all('#hospitals .hospital-name, #hospitals h3, #hospitals h4, #hospitals a[href*="/best-hospitals/"]')
        .map(visibleText),
      visibleText(document.querySelector('a[href="#hospitals"] strong')),
    ]).filter((value) => !/^hospitals?(?:\s*&\s*ascs?)?$/i.test(value));

    const overviewSection = document.querySelector("#overview");
    const overviewHeading = heading(overviewSection || document, "Overview");
    const overview = clean(visibleText(nextElementMatching(overviewHeading, "div, p")), 4000);
    const detailText = visibleText(overviewSection || document);
    const speaks = detailText.match(/\bSpeaks\s+([^\n]+?)(?=\s+Works at\b|$)/i)?.[1] || "";
    const languages = unique(speaks.split(/,|\band\b/i));
    const years = visibleText(hero).match(/\b(\d+\+?)\s+Years? of Experience\b/i)?.[1] || "";
    const schemaAddress = postalAddress(physician?.address);
    const address = schemaAddress || visibleText(document.querySelector('a[href="#location"]'));
    const locationValue = physician?.address && typeof physician.address === "object"
      ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
          .filter(Boolean).join(", ")
      : locationFromAddress(address);
    const npi = npiFrom(
      document.querySelector("[doctor_npi]")?.getAttribute("doctor_npi")
      || document.querySelector("[data-tracking-npi]")?.getAttribute("data-tracking-npi")
      || visibleText(document.querySelector("#experience")),
    );



    if (!education.length && !licenses.length && !certifications.length) return null;
    return {
      kind: "public_professional_profile",
      source_label: "U.S. News Doctor Finder",
      source_url: absoluteUrl(physician?.url || location.href),
      headline: [specialties[0], subspecialties[0]].filter(Boolean).join(" — "),
      credentials,
      summary: overview,
      specialties,
      subspecialties,
      hospitals,
      education,
      licenses,
      certifications,
      languages,
      years_experience: years,
      npi,
      address,
      location: locationValue,
    };
  }

  function usNewsProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    const cards = all('[data-test-id="DetailCardDoctor"]');
    const roots = cards.length ? cards : [document];

    for (const root of roots) {
      let physicians = jsonLdObjects(root).filter((value) => hasSchemaType(value, "Physician"));
      if (!physicians.length && root === document && exactUsNewsProfilePath()) {
        const heroName = visibleText(document.querySelector('[data-test-id^="HeroGraphic"] h1, .hero h1, main h1'));
        if (looksLikePersonName(cleanProviderName(heroName))) {
          physicians = [{ name: heroName, url: location.href }];
        }
      }
      for (const physician of physicians) {
        const name = cleanProviderName(physician.name);
        if (!looksLikePersonName(name)) continue;

        const sourceUrl = absoluteUrl(physician.url || root.querySelector?.(
          'a[href^="/doctors/"], a[href^="/nurse-practitioners/"]'
        )?.getAttribute("href") || location.href);
        let sourcePath = "";
        try { sourcePath = new URL(sourceUrl).pathname; } catch { /* absoluteUrl already supplied a safe fallback. */ }
        const profileDocument = root === document ? usNewsProfileDocument(physician) : null;
        const npi = npiFrom(
          root.querySelector?.("[doctor_npi]")?.getAttribute("doctor_npi")
          || root.querySelector?.("[data-tracking-npi]")?.getAttribute("data-tracking-npi")
          || profileDocument?.npi,
        );
        const sourceId = npi || sourcePath.replace(/^\/+|\/+$/g, "") || name.toLowerCase();
        if (seen.has(sourceId)) continue;

        const specialty = schemaName(physician.medicalSpecialty) || profileDocument?.specialties?.[0] || "";
        const address = postalAddress(physician.address) || profileDocument?.address || "";
        const locationValue = physician.address && typeof physician.address === "object"
          ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)]
              .filter(Boolean).join(", ")
          : (profileDocument?.location || locationFromAddress(address));
        const affiliations = Array.isArray(physician.hospitalAffiliation)
          ? physician.hospitalAffiliation
          : [physician.hospitalAffiliation];
        const employers = unique([
          ...affiliations.map(schemaName),
          ...(profileDocument?.hospitals || []),
        ]);
        const profession = /\/nurse-practitioners(?:\/|$)/i.test(sourcePath)
          ? "Nurse Practitioner"
          : "Physician";
        const extra = unique([
          ...employers.map((employer) => `Employer: ${employer}`),
          ...(profileDocument?.subspecialties || []).map((value) => `Subspecialty: ${value}`),
          ...(profileDocument?.education || []).map((value) => `Education: ${value}`),
          ...(profileDocument?.certifications || []).map((value) => `Certification: ${value}`),
          ...(profileDocument?.licenses || []).map((value) => `License: ${value}`),
          ...(profileDocument?.languages || []).map((value) => `Language: ${value}`),
          profileDocument?.years_experience && `Years of experience: ${profileDocument.years_experience}`,
          profileDocument?.summary,
          clean(physician.description, 1200),
        ]).join("\n");

        seen.add(sourceId);
        profiles.push({
          name,
          location: locationValue,
          headline: specialty || profession,
          notes: profileNotes({ npi, profession, specialty, address, extra }),
          roles: [profession],
          employers,
          schools: profileDocument?.education || [],
          licenses: profileDocument?.licenses || [],
          certifications: profileDocument?.certifications || [],
          profile_document: profileDocument || undefined,
          source: PLATFORM.key,
          source_url: sourceUrl,
          source_id: sourceId,
          captured_at: new Date().toISOString(),
        });
        elements.set(sourceId, root === document ? document.documentElement : root);
        if (profiles.length >= 100) return { profiles, elements };
      }
    }
    return { profiles, elements };
  }

  function npinoProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    for (const link of all("a[href]")) {
      const href = absoluteUrl(link.getAttribute("href"));
      let profileUrl;
      try { profileUrl = new URL(href); } catch { continue; }
      if (!/^(?:www\.)?npino\.com$/i.test(profileUrl.hostname)) continue;


      const npi = profileUrl.pathname.match(/^\/[a-z][a-z-]*\/(\d{10})(?:-[^/]*)?\/?$/i)?.[1];
      if (!npi || seen.has(npi)) continue;
      let card = null;
      for (let node = link; node; node = node.parentElement) {
        if (visibleText(node).length > 3000) break;


        const otherProfile = all("a[href]", node).some((anchor) => {
          const path = absoluteUrl(anchor.getAttribute("href"));
          const linkedNpi = path.match(/\/[a-z][a-z-]*\/(\d{10})(?:-[^/?#]*)?\/?(?:[?#]|$)/i)?.[1];
          return linkedNpi && linkedNpi !== npi;
        });
        if (otherProfile) break;
        const text = visibleText(node);
        const hasThisNpi = text.includes(npi) || /\bNPI Number\s*:/i.test(text);
        const hasPersonName = all("h1, h2, h3, h4, [itemprop='name'], .provider-name", node)
          .some((element) => looksLikePersonName(element.textContent));
        if (hasThisNpi && hasPersonName) {
          card = node;
          if (node.matches?.("article, li, tr, [class*='card'], [class*='result']")) break;
        }


        if (node === document.body) break;
      }
      if (!card) card = smallestContainer(link, /\bNPI Number\s*:/i);
      const nameLink = card.querySelector?.("h2 a[href], h3 a[href]") || link;
      const name = candidateName(card, nameLink, npi) || cleanProviderName(nameLink.textContent);
      if (!looksLikePersonName(name)) continue;
      const text = visibleText(card);
      const address = clean(text.match(/\bAddress\s*:\s*(.*?)(?=\s+(?:Phone|Fax)\s*:|$)/i)?.[1] || "", 500);
      const specialty = clean(card.querySelector?.("h3 + p, h2 + p")?.textContent || specialtyFromPage(), 180);
      seen.add(npi);
      profiles.push({
        name,
        location: locationFromAddress(address),
        headline: specialty,
        notes: profileNotes({ npi, specialty, address }),
        roles: specialty ? [specialty] : [],
        employers: [],
        schools: [],
        licenses: [],
        certifications: [],
        source: PLATFORM.key,
        source_url: href,
        source_id: npi,
        captured_at: new Date().toISOString(),
      });
      elements.set(npi, card);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function webmdSectionValues(labels) {
    const expected = labels.map((label) => label.toLowerCase());
    const values = [];
    for (const title of all("h1, h2, h3, h4, dt, [class*='heading'], [class*='title']")) {
      const text = visibleText(title).toLowerCase();
      if (!expected.some((label) => text === label || text.includes(label))) continue;
      const section = title.closest("section, article, [class*='section'], [class*='card']") || title.parentElement;
      if (!section) continue;
      values.push(...all("li, dd, p, a", section).filter((item) => item !== title).map(visibleText));
    }
    return unique(values.filter((value) => value.length < 300));
  }

  function webmdProfileDocument(physician, profile) {
    const sourceUrl = absoluteUrl(profile?.source_url || physician?.url || canonicalPageUrl());
    let source;
    try { source = new URL(sourceUrl); } catch { return null; }
    if (source.hostname.toLowerCase() !== "doctor.webmd.com"
      || !/^\/doctor\/[^/?#]+-overview\/?$/i.test(source.pathname)) return null;
    const exactPage = /^\/doctor\/[^/?#]+-overview\/?$/i.test(location.pathname)
      && sameProfileUrl(sourceUrl, canonicalPageUrl());
    const rawName = (exactPage && visibleText(document.querySelector("main h1, h1")))
      || physician?.name || profile?.name || metaContent('meta[property="og:title"]');
    const name = cleanProviderName(rawName);
    if (!looksLikePersonName(name)) return null;
    const credentials = providerCredentials(rawName);
    const specialties = unique([
      ...schemaNames(physician?.medicalSpecialty),
      ...webmdSectionValues(["Specialty", "Specialties", "Areas of Expertise"]),
      ...(profile?.specialties || []),
    ]);
    const hospitals = unique([
      ...schemaNames(physician?.hospitalAffiliation),
      ...webmdSectionValues(["Hospital Affiliations", "Affiliations"]),
    ]);
    const education = unique([
      ...schemaNames(physician?.alumni), ...schemaNames(physician?.education),
      ...webmdSectionValues(["Education & Training", "Education", "Medical School", "Residency", "Fellowship"]),
    ]);
    const credentialsAndLicenses = unique([
      ...schemaNames(physician?.hasCredential),
      ...webmdSectionValues(["Certifications", "License", "Licenses", "Certifications, License, & Education"]),
    ]);
    const licenses = credentialsAndLicenses.filter((value) => /\blicen[cs]e\b/i.test(value));
    const certifications = credentialsAndLicenses.filter((value) => !licenses.includes(value));
    const languages = unique([
      ...schemaNames(physician?.knowsLanguage), ...webmdSectionValues(["Languages Spoken", "Languages"]),
    ]);
    const summary = clean(physician?.description
      || (exactPage && visibleText(document.querySelector('[class*="biography"], [class*="overview"], [class*="about"]')))
      || (exactPage && metaContent('meta[name="description"]')), 4000);
    const address = postalAddress(physician?.address)
      || (exactPage && visibleText(document.querySelector('[itemprop="address"], address, [class*="address"]')))
      || profile?.address || "";
    const locationValue = physician?.address && typeof physician.address === "object"
      ? [clean(physician.address.addressLocality, 120), clean(physician.address.addressRegion, 80)].filter(Boolean).join(", ")
      : (profile?.location || locationFromAddress(address));
    if (!specialties.length && !hospitals.length && !education.length && !licenses.length
      && !certifications.length && !summary) return null;
    const specialty = specialties[0] || profile?.headline || providerProfession(credentials);
    return {
      kind: "public_professional_profile",
      source_label: "WebMD",
      source_url: sourceUrl,
      headline: specialty,
      summary,
      credentials,
      specialties,
      subspecialties: [],
      hospitals,
      education,
      licenses,
      certifications,
      languages,
      npi: npiFrom(document.querySelector("[data-npi]")?.getAttribute("data-npi") || profile?.source_id || ""),
      address,
      location: locationValue,
    };
  }

  function webmdProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    const profilePath = /^\/doctor\/[^/?#]+-overview\/?$/i.test(location.pathname);
    const roots = profilePath ? [document] : all("li.ep[data-npi], li.ep, article[class*='result'], .results-card-wrap");
    for (const root of roots) {
      const link = root.querySelector?.("a.prov-name[href*='/doctor/'], a[href*='/doctor/'][href*='overview']")
        || (profilePath ? null : null);
      const href = link?.getAttribute("href") || (profilePath ? location.href : "");
      const linkedUrl = absoluteUrl(href);
      let parsed;
      try { parsed = new URL(linkedUrl); } catch { continue; }
      if (parsed.hostname.toLowerCase() !== "doctor.webmd.com"
        || !/^\/doctor\/[^/?#]+-overview\/?$/i.test(parsed.pathname)) continue;
      const sourceUrl = `${parsed.origin}${parsed.pathname}`;
      const pathKey = parsed.pathname.replace(/\/+$/, "").toLowerCase();
      if (seen.has(pathKey)) continue;
      const physician = jsonLdObjects(root).find((value) => hasSchemaType(value, "Physician"))
        || jsonLdObjects().find((value) => hasSchemaType(value, "Physician") && sameProfileUrl(value.url || "", sourceUrl))
        || null;
      const rawName = (profilePath && visibleText(document.querySelector("main h1, h1")))
        || visibleText(link) || physician?.name || "";
      const name = cleanProviderName(rawName);
      if (!looksLikePersonName(name)) continue;
      const npi = npiFrom(root.getAttribute?.("data-npi") || physician?.identifier?.value || physician?.identifier || "");
      const sourceId = npi || parsed.pathname.replace(/^\/doctor\//i, "").replace(/-overview\/?$/i, "");
      if (!sourceId) continue;
      const cardText = visibleText(root);
      const specialty = clean(root.querySelector?.(".prov-specialty, [class*='specialty']")?.textContent
        || schemaNames(physician?.medicalSpecialty)[0] || specialtyFromPage(), 180);
      const imageAlt = clean(root.querySelector?.("img[alt]")?.getAttribute("alt") || "", 300);
      const providerLocation = clean(
        cardText.match(/\b([A-Za-z .'-]+,\s*[A-Z]{2})(?=\s*[-|])/i)?.[1]
          || imageAlt.match(/\b([A-Za-z .'-]+,\s*[A-Z]{2})\b/)?.[1]
          || "",
        180,
      );
      const credentials = providerCredentials(rawName);
      const documentProfile = profilePath ? webmdProfileDocument(physician, {
        name, source_url: sourceUrl, source_id: sourceId, specialty, headline: specialty,
      }) : null;
      const profile = {
        name,
        location: documentProfile?.location || providerLocation,
        headline: specialty || providerProfession(credentials),
        notes: profileNotes({
          npi, specialty, address: documentProfile?.address || "",
          extra: documentProfile?.summary || cardText.slice(0, 800),
        }),
        roles: [providerProfession(credentials)],
        employers: documentProfile?.hospitals || schemaNames(physician?.hospitalAffiliation),
        schools: documentProfile?.education || schemaNames(physician?.alumni),
        licenses: documentProfile?.licenses || [],
        certifications: documentProfile?.certifications || schemaNames(physician?.hasCredential),
        profile_document: documentProfile || undefined,
        source: PLATFORM.key,
        source_url: sourceUrl,
        source_id: sourceId,
        captured_at: new Date().toISOString(),
      };
      seen.add(pathKey);
      profiles.push(profile);
      elements.set(sourceId, root === document ? document.documentElement : root);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function candidateName(container, primaryLink, npi) {
    const candidates = [
      primaryLink?.textContent,
      ...all('[itemprop="name"], .provider-name, .name, h2, h3, h4, a[href]', container).map((element) => element.textContent),
      ...all("td", container).map((element) => element.textContent),
    ];
    return candidates
      .map((value) => cleanProviderName(String(value || "").replace(npi, "")))
      .find(looksLikePersonName) || "";
  }

  function npiProfileProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    const pageSpecialty = specialtyFromPage();
    for (const link of all('a[href*="/npi/"]')) {
      const href = absoluteUrl(link.getAttribute("href"));
      const npi = href.match(/\/npi\/(\d{10})(?:[/?#-]|$)/i)?.[1] || "";
      if (!npi || seen.has(npi)) continue;
      const card = link.closest("tr, article, li") || smallestContainer(link, new RegExp(`\\b${npi}\\b`));
      const cardText = visibleText(card);
      if (/\borganization\b/i.test(cardText) && !/\bindividual\b/i.test(cardText)) continue;
      const name = candidateName(card, link, npi);
      if (!name) continue;
      const location = locationFromAddress(cardText);
      seen.add(npi);
      profiles.push({
        name,
        location,
        headline: pageSpecialty,
        notes: profileNotes({ npi, specialty: pageSpecialty, extra: cardText.slice(0, 1200) }),
        roles: pageSpecialty ? [pageSpecialty] : [],
        employers: [],
        schools: [],
        licenses: [],
        certifications: [],
        source: PLATFORM.key,
        source_url: href,
        source_id: npi,
        captured_at: new Date().toISOString(),
      });
      elements.set(npi, card);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function nysedProfiles() {
    const profiles = [];
    const elements = new Map();
    const seen = new Set();
    const rows = all("#searchTable tbody tr").filter(isVisible);
    for (const row of rows) {
      const cells = all("td", row).map(visibleText);
      if (cells.length < 4) continue;
      const license = clean(cells[0], 120);
      const name = cleanProviderName(cells[1]);
      const profession = clean(cells[2], 180);
      const address = clean(cells[3], 500);
      const licensedOn = clean(cells[4], 100);
      if (!license || !looksLikePersonName(name)) continue;
      const sourceId = `${profession.toLowerCase()}:${license.toLowerCase()}`;
      if (seen.has(sourceId)) continue;
      seen.add(sourceId);
      profiles.push({
        name,
        location: locationFromAddress(address),
        headline: profession,
        notes: profileNotes({ license, profession, address, extra: licensedOn && `Date of licensure: ${licensedOn}` }),
        roles: profession ? [profession] : [],
        employers: [],
        schools: [],
        licenses: [license],
        certifications: [],
        source: PLATFORM.key,
        source_url: absoluteUrl(location.href),
        source_id: sourceId,
        captured_at: new Date().toISOString(),
      });
      elements.set(sourceId, row);
      if (profiles.length >= 100) break;
    }
    return { profiles, elements };
  }

  function scanSnapshot() {
    const result = PLATFORM.key === "npino"
      ? npinoProfiles()
      : PLATFORM.key === "npiprofile"
        ? npiProfileProfiles()
        : PLATFORM.key === "nysed"
          ? nysedProfiles()
          : PLATFORM.key === "medifind"
            ? medifindProfiles()
            : PLATFORM.key === "commonspirit"
              ? commonSpiritProfiles()
              : PLATFORM.key === "sharecare"
                ? sharecareProfiles()
                : PLATFORM.key === "webmd"
                  ? webmdProfiles()
                  : usNewsProfiles();
    lastProfiles = result.profiles;
    lastElements = result.elements;
    return {
      ok: true,
      platform: PLATFORM.key,
      platform_label: PLATFORM.label,
      profiles: result.profiles,
      count: result.profiles.length,
      expected_count: result.profiles.length,
      raw: { rows: result.profiles.length },
      page_url: location.href,
    };
  }

  async function progressiveScan() {
    scanInProgress = true;
    try {
      const result = scanSnapshot();
      chrome.runtime.sendMessage({
        type: "MEDHUNT_PLATFORM_SCAN_PROGRESS",
        platform: PLATFORM.key,
        found: result.count,
        total: result.expected_count,
        page_url: location.href,
      }, () => void chrome.runtime.lastError);
      return result;
    } finally {
      scanInProgress = false;
    }
  }

  function openCandidate(index) {
    const profile = lastProfiles[Number(index)];
    if (!profile) return { ok: false, error: "That displayed provider is no longer available." };
    const element = lastElements.get(profile.source_id);
    const link = element?.matches?.("a[href]") ? element : (PLATFORM.key === "npino"
      ? element?.querySelector?.("h2 a[href], h3 a[href]")
      : element?.querySelector?.(
        'a[href*="/npi/"], a[href*="/doctors/"], a[href*="/nurse-practitioners/"], a[href*="/find-a-doctor/"], a.prov-name, a[href*="-overview"], a.information'
      ));
    if (link) {
      link.scrollIntoView({ block: "center", behavior: "auto" });
      link.click();
      return { ok: true, source_url: profile.source_url };
    }
    return { ok: false, error: `${PLATFORM.label} did not expose a provider-details link.` };
  }

  function captureProfile() {
    const result = scanSnapshot();
    if (result.profiles.length !== 1) {
      return { ok: false, error: `Open one ${PLATFORM.label} provider result, then try Capture again.` };
    }
    if (result.profiles[0]?._detail_capture_ready === false) {
      return { ok: false, error: `${PLATFORM.label} is still loading the profile details.` };
    }
    return { ok: true, platform: PLATFORM.key, profile: result.profiles[0], page_url: location.href };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    const messageType = message?.type === ADAPTER_REQUEST ? message.original_type : message?.type;
    const respond = (payload) => sendResponse({ ...payload, adapter_revision: ADAPTER_REVISION });
    if (messageType === "MEDHUNT_PLATFORM_PING") {
      respond({ ok: true, platform: PLATFORM.key, label: PLATFORM.label, url: location.href });
      return false;
    }
    if (messageType === "MEDHUNT_CAPTURE_PLATFORM_PROFILE") {
      respond(captureProfile());
      return false;
    }
    if (messageType === "MEDHUNT_LIST_PLATFORM_CANDIDATES") {
      respond(scanSnapshot());
      return false;
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
  function detectResultsChange() {
    if (scanInProgress) return;
    clearTimeout(mutationTimer);
    mutationTimer = setTimeout(() => {
      const snapshot = scanSnapshot();
      const signature = snapshot.profiles.map((profile) => (
        `${profile.source_id}:${profile.profile_document ? JSON.stringify(profile.profile_document) : ""}`
      )).join("|");
      if (signature === lastSignature) return;
      const hadResults = Boolean(lastSignature);
      lastSignature = signature;
      if (!signature && !hadResults) return;
      chrome.runtime.sendMessage({
        type: "MEDHUNT_PLATFORM_RESULTS_CHANGED",
        platform: PLATFORM.key,
        count: snapshot.count,
        page_url: location.href,
      }, () => void chrome.runtime.lastError);
    }, 800);
  }

  new MutationObserver(detectResultsChange).observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["class", "hidden", "aria-hidden"],
  });
  addEventListener("popstate", detectResultsChange);
  detectResultsChange();
})();
