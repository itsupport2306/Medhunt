"""Offline browser smoke test for Medhunt's public healthcare-directory adapters."""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


FRONTEND = Path(__file__).parents[1] / "frontend"
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
BROWSER = Path(os.environ.get("RADIXSOL_BROWSER_EXECUTABLE", str(CHROME)))


def _install_runtime(page):
    page.evaluate(
        """() => {
          window.__radixsolMessageListener = null;
          window.chrome = {
            runtime: {
              lastError: null,
              onMessage: {
                addListener: listener => { window.__radixsolMessageListener = listener; }
              },
              sendMessage: (_message, callback) => { if (callback) callback({ ok: true }); }
            }
          };
        }"""
    )


def _message(page, message):
    return page.evaluate(
        """message => new Promise((resolve, reject) => {
          const listener = window.__radixsolMessageListener;
          if (!listener) return reject(new Error('Content-script listener was not installed.'));
          const asyncResponse = listener(message, {}, resolve);
          if (asyncResponse !== true && asyncResponse !== false) {
            setTimeout(() => reject(new Error('No response.')), 1000);
          }
        })""",
        message,
    )


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(BROWSER))
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))

        syntax = context.new_page()
        source = (FRONTEND / "healthcare-directory-content.js").read_text(encoding="utf-8")
        assert syntax.evaluate("source => { new Function(source); return true; }", source)
        app_source = (FRONTEND / "app.js").read_text(encoding="utf-8")
        assert syntax.evaluate("source => { new Function(source); return true; }", app_source)
        syntax.close()

        npino = context.new_page()
        npino.goto("https://npino.com/lookup/208800000x-urology/pa/")
        npino.set_content(
            """<main>
              <h1>Urology Doctors in Pennsylvania</h1>
              <div class="provider-card"><div><h3><a href="/npi/1093058315-patrick-theodore-gomella/">Patrick Theodore Gomella, MD, MPH</a></h3>
                <p>Urology</p><p>NPI Number: <a href="/npi/1093058315-patrick-theodore-gomella/">1093058315</a></p>
                <p>Address: 1245 Highland Ave Ste 302, Abington, PA, 19001-3724</p><p>Phone: 215-555-0100</p></div></div>
              <div class="provider-card"><div><h3><a href="/npi/1609865971-urology-specialists/">Urology Specialists Of The Lehigh Valley Pc</a></h3>
                <p>Urology</p><p>NPI Number: 1609865971</p><p>Address: Allentown, PA, 18106</p></div></div>
            </main>"""
        )
        _install_runtime(npino)
        npino.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        npino_result = _message(npino, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert npino_result["platform"] == "npino", npino_result
        assert npino_result["adapter_revision"] == "healthcare-directory-v9"
        assert npino_result["count"] == 1, npino_result
        assert npino_result["profiles"][0]["name"] == "Patrick Theodore Gomella"
        assert npino_result["profiles"][0]["source_id"] == "1093058315"
        assert npino_result["profiles"][0]["location"] == "Abington, PA"
        assert npino_result["profiles"][0]["roles"] == ["Urology"]

        npi_profile = context.new_page()
        npi_profile.goto("https://npiprofile.com/taxonomy/code/207V00000X/state/ia")
        npi_profile.set_content(
            """<main><h1>207V00000X - Obstetrics &amp; Gynecology Providers in Iowa</h1>
              <table><tbody>
                <tr><td><a href="/npi/1023944683">1023944683</a></td><td>Madeline M. Shaw, MD</td>
                  <td>Individual</td><td>200 Hawkins Dr, Iowa City, IA 52242-1009</td></tr>
                <tr><td><a href="/npi/1999999999">1999999999</a></td><td>Example Women's Health LLC</td>
                  <td>Organization</td><td>Des Moines, IA 50309</td></tr>
              </tbody></table>
            </main>"""
        )
        _install_runtime(npi_profile)
        npi_profile.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        npi_profile_result = _message(npi_profile, {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"})
        assert npi_profile_result["platform"] == "npiprofile", npi_profile_result
        assert npi_profile_result["count"] == 1, npi_profile_result
        assert npi_profile_result["profiles"][0]["name"] == "Madeline M. Shaw"
        assert npi_profile_result["profiles"][0]["source_id"] == "1023944683"
        assert npi_profile_result["profiles"][0]["location"] == "Iowa City, IA"
        assert npi_profile_result["profiles"][0]["headline"] == "Obstetrics & Gynecology"

        nysed = context.new_page()
        nysed.goto("https://eservices.nysed.gov/professions/verification-search")
        nysed.set_content(
            """<main><table id="searchTable"><tbody>
              <tr><td><a class="information" href="#">012345</a></td><td>Alexandra Rivera, MD</td>
                <td>Medicine</td><td>Albany, NY 12234</td><td>01/02/2014</td></tr>
              <tr><td><a class="information" href="#">099999</a></td><td>Capital Medical Group LLC</td>
                <td>Medicine</td><td>Albany, NY 12234</td><td>03/04/2015</td></tr>
            </tbody></table></main>"""
        )
        _install_runtime(nysed)
        nysed.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        nysed_result = _message(nysed, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert nysed_result["platform"] == "nysed", nysed_result
        assert nysed_result["count"] == 1, nysed_result
        assert nysed_result["profiles"][0]["name"] == "Alexandra Rivera"
        assert nysed_result["profiles"][0]["source_id"] == "medicine:012345"
        assert nysed_result["profiles"][0]["location"] == "Albany, NY"
        assert nysed_result["profiles"][0]["licenses"] == ["012345"]
        assert "Date of licensure: 01/02/2014" in nysed_result["profiles"][0]["notes"]

        usnews = context.new_page()
        usnews.goto("https://health.usnews.com/doctors/obstetrician-gynecologists")
        usnews.set_content(
            """<main><ol>
              <li><div data-test-id="DetailCardDoctor">
                <script type="application/ld+json">{
                  "@context":"https://schema.org", "@type":"LocalBusiness",
                  "name":"Dr. Ryan E. Longman MD", "telephone":"1-240-981-4709"
                }</script>
                <script type="application/ld+json">{
                  "@context":"http://schema.org", "@type":"Physician",
                  "name":"Ryan E. Longman",
                  "description":"Dr. Ryan Longman is an obstetrician-gynecologist in New York.",
                  "url":"https://health.usnews.com/doctors/ryan-longman-601896",
                  "address":{"@type":"PostalAddress","addressLocality":"New York","addressRegion":"NY","postalCode":"10038","streetAddress":"156 William St"},
                  "medicalSpecialty":{"@type":"MedicalSpecialty","name":"Obstetrics & Gynecology"},
                  "telephone":"(646) 962-2620",
                  "hospitalAffiliation":{"@type":"Hospital","name":"NewYork-Presbyterian Hospital-Columbia and Cornell"}
                }</script>
                <a href="https://health.usnews.com/doctors/ryan-longman-601896"><h3>Dr. Ryan E. Longman MD</h3></a>
                <button data-tracking-npi="1689794356">Book Appointment</button>
              </div></li>
              <li><div data-test-id="DetailCardDoctor">
                <script type="application/ld+json">{
                  "@context":"http://schema.org", "@type":"Physician",
                  "name":"Julie A. Abbott",
                  "url":"https://health.usnews.com/nurse-practitioners/julie-abbott-1953734",
                  "address":{"@type":"PostalAddress","addressLocality":"Coos Bay","addressRegion":"OR","postalCode":"97420","streetAddress":"1750 Thompson Rd"},
                  "medicalSpecialty":{"@type":"MedicalSpecialty","name":"Women's Health Nurse Practitioner"},
                  "hospitalAffiliation":{"@type":"Hospital","name":"Bay Area Hospital"}
                }</script>
                <a href="/nurse-practitioners/julie-abbott-1953734">Julie Abbott NP</a>
                <span data-tracking-npi="1609881028"></span>
              </div></li>
            </ol></main>"""
        )
        _install_runtime(usnews)
        usnews.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        usnews_result = _message(usnews, {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"})
        assert usnews_result["platform"] == "usnews", usnews_result
        assert usnews_result["platform_label"] == "U.S. News Doctor Finder"
        assert usnews_result["count"] == 2, usnews_result
        physician, nurse_practitioner = usnews_result["profiles"]
        assert physician["name"] == "Ryan E. Longman"
        assert physician["source_id"] == "1689794356"
        assert physician["location"] == "New York, NY"
        assert physician["headline"] == "Obstetrics & Gynecology"
        assert physician["roles"] == ["Physician"]
        assert physician["employers"] == ["NewYork-Presbyterian Hospital-Columbia and Cornell"]
        assert "Specialty: Obstetrics & Gynecology" in physician["notes"]
        assert "Profession: Physician" in physician["notes"]
        assert "NPI: 1689794356" in physician["notes"]
        assert "Employer: NewYork-Presbyterian Hospital-Columbia and Cornell" in physician["notes"]
        assert "1-240-981-4709" not in physician["notes"]
        assert "(646) 962-2620" not in physician["notes"]
        opened = _message(usnews, {"type": "RADIXSOL_OPEN_PLATFORM_CANDIDATE", "index": 0})
        assert opened["ok"] is True, opened
        assert opened["source_url"] == "https://health.usnews.com/doctors/ryan-longman-601896", opened
        assert nurse_practitioner["source_id"] == "1609881028"
        assert nurse_practitioner["roles"] == ["Nurse Practitioner"]

        usnews_profile = context.new_page()
        usnews_profile.goto("https://health.usnews.com/doctors/ryan-longman-601896")
        usnews_profile.set_content(
            """<main><div class="hero" data-test-id="HeroGraphic_with headshot">
                <h1>Dr. Ryan E. Longman MD</h1>
                <p data-tracking-placement="profile_header" data-tracking-campaign="specialty">Obstetrics &amp; Gynecology</p>
                <a href="#hospitals"><strong>NewYork-Presbyterian Hospital</strong></a>
                <span>18+ Years of Experience</span>
                <button doctor_npi="1689794356">Save</button>
              </div>
              <section id="overview"><h2>Overview</h2>
                <div>Dr. Ryan Longman is an obstetrician-gynecologist in New York.</div>
                <div>Speaks <strong>English</strong> Works at <strong>NewYork-Presbyterian Hospital</strong></div>
                <dl><dt>Specialty</dt><dd>Obstetrics &amp; Gynecology</dd></dl>
                <dl><dt>Subspecialties</dt><dd>Maternal &amp; Fetal Medicine</dd></dl>
              </section>
              <section id="hospitals"><h2>Hospitals &amp; ASCs</h2>
                <a href="/best-hospitals/area/ny/new-york-presbyterian-123"><h3>NewYork-Presbyterian Hospital</h3></a>
              </section>
              <section id="experience"><h2>Education &amp; Training</h2>
                <h3>Medical School &amp; Residency</h3><dl>
                  <dt>New York Presbyterian Hospital</dt><dd>Residency, Obstetrics and Gynecology, 2003-2007</dd>
                  <dt>Columbia University Vagelos College of Physicians and Surgeons</dt><dd>Medical School</dd>
                </dl>
                <h3>Certifications &amp; Licensure</h3><dl>
                  <dt>American Board of Obstetrics and Gynecology (ABMS)</dt><dd>Certified in Obstetrics &amp; Gynecology</dd>
                  <dt>NY State Medical License</dt><dd>Active through 2028</dd>
                </dl>
                <dl><dt>Provider NPI</dt><dd>1689794356</dd></dl>
              </section>
            </main>"""
        )
        _install_runtime(usnews_profile)
        usnews_profile.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        usnews_profile_result = _message(
            usnews_profile, {"type": "RADIXSOL_CAPTURE_PLATFORM_PROFILE"}
        )
        assert usnews_profile_result["ok"] is True, usnews_profile_result
        assert usnews_profile_result["profile"]["source_id"] == "1689794356"
        profile = usnews_profile_result["profile"]
        assert profile["schools"] == [
            "New York Presbyterian Hospital — Residency, Obstetrics and Gynecology, 2003-2007",
            "Columbia University Vagelos College of Physicians and Surgeons — Medical School",
        ]
        assert profile["licenses"] == ["NY State Medical License — Active through 2028"]
        assert profile["certifications"] == [
            "American Board of Obstetrics and Gynecology (ABMS) — Certified in Obstetrics & Gynecology"
        ]
        document_profile = profile["profile_document"]
        assert document_profile["kind"] == "public_professional_profile"
        assert document_profile["credentials"] == ["MD"]
        assert document_profile["subspecialties"] == ["Maternal & Fetal Medicine"]
        assert document_profile["hospitals"] == ["NewYork-Presbyterian Hospital"]
        assert document_profile["languages"] == ["English"]
        assert document_profile["years_experience"] == "18+"
        assert document_profile["npi"] == "1689794356"

        medifind = context.new_page()
        medifind.goto("https://www.medifind.com/specialty/thoracic-surgery")
        medifind.set_content(
            """<main><h1>Best Thoracic Surgeons Near Me</h1>
              <script type="application/ld+json">{
                "@context":"https://schema.org", "@type":"Physician",
                "name":"Brian E. Louie",
                "description":"Dr. Louie treats chest conditions. Dr. Louie is board certified in American Board Of Surgery.",
                "url":"https://www.medifind.com/doctors/brian-e-louie/10650877",
                "address":{"@type":"PostalAddress","addressLocality":"Seattle","addressRegion":"WA","postalCode":"98104","streetAddress":"1101 Madison Street, Suite 900","addressCountry":"US"},
                "medicalSpecialty":{"@type":"MedicalSpecialty","name":"Thoracic Surgery"},
                "telephone":"206-215-6800",
                "hospitalAffiliation":{"@type":"Hospital","name":"Swedish Medical Center"}
              }</script>
              <div id="card_doctor_10650877">
                <a data-link-type="doctor-name" href="/doctors/brian-e-louie/10650877"><h3>Dr. Brian E. Louie</h3></a>
                <div class="DoctorCard_header__specialties__fixture">Thoracic Surgery</div>
                <h4 class="DoctorCard_body__alt-container__affiliation__fixture">Swedish Thoracic Surgery - First Hill</h4>
                <div class="CardAddress_card-address__content__fixture">1101 Madison Street, Suite 900, Seattle, WA</div>
              </div>
            </main>"""
        )
        _install_runtime(medifind)
        medifind.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        medifind_result = _message(medifind, {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"})
        assert medifind_result["platform"] == "medifind", medifind_result
        assert medifind_result["platform_label"] == "MediFind"
        assert medifind_result["adapter_revision"] == "healthcare-directory-v9"
        assert medifind_result["count"] == 1, medifind_result
        medifind_profile = medifind_result["profiles"][0]
        assert medifind_profile["name"] == "Brian E. Louie"
        assert medifind_profile["source_id"] == "10650877"
        assert medifind_profile["location"] == "Seattle, WA"
        assert medifind_profile["specialties"] == ["Thoracic Surgery"]
        assert "Swedish Medical Center" in medifind_profile["employers"]
        assert "206-215-6800" not in medifind_profile["notes"]
        assert medifind_profile["profile_document"]["source_label"] == "MediFind"
        assert medifind_profile["profile_document"]["certifications"] == [
            "Board certified in American Board Of Surgery"
        ]

        actual_medifind_profile_count = 0
        medifind_profile_snapshot = FRONTEND.parents[1] / "medifind_profile.txt"
        if medifind_profile_snapshot.is_file():
            actual_medifind_profile = context.new_page()
            actual_medifind_profile.goto(
                "https://www.medifind.com/doctors/brian-e-louie/10650877"
            )
            actual_medifind_profile.set_content(
                medifind_profile_snapshot.read_text(encoding="utf-8")
            )
            _install_runtime(actual_medifind_profile)
            actual_medifind_profile.add_script_tag(
                path=str(FRONTEND / "healthcare-directory-content.js")
            )
            actual_medifind_result = _message(
                actual_medifind_profile,
                {"type": "RADIXSOL_CAPTURE_PLATFORM_PROFILE"},
            )
            assert actual_medifind_result["ok"] is True, actual_medifind_result
            actual_profile = actual_medifind_result["profile"]
            assert actual_profile["name"] == "Brian E. Louie", actual_profile
            assert actual_profile["source_id"] == "10650877", actual_profile
            assert actual_profile["specialties"] == ["Thoracic Surgery"], actual_profile
            assert "Surgery in WA" in actual_profile["licenses"], actual_profile
            assert "American Board Of Surgery" in actual_profile["certifications"], actual_profile
            assert any("University Of Toronto" in value for value in actual_profile["schools"])
            assert "Swedish Medical Center" in actual_profile["employers"], actual_profile
            assert actual_profile["profile_document"]["languages"] == ["English"]
            actual_medifind_profile_count = 1
            actual_medifind_profile.close()

        commonspirit = context.new_page()
        commonspirit.goto("https://providers.commonspirit.org/search?search=primary-care")
        commonspirit.set_content(
            """<main><div id="results-list">
              <div id="card-39c1d894-7371-48b5-97b1-a993c3781986" class="csh-aem-result-card csh-aem-result-card--provider">
                <a class="csh-aem-result-card__link" href="/find-a-doctor/clara-zee-1407550627?searchType=taxonomies&amp;slug=primary-care"><h3 class="csh-aem-result-card__title">Clara Zee, DO</h3></a>
                <ul class="csh-aem-result-card__specialties"><li class="csh-aem-result-card__specialties__item csh-aem-result-card__specialties__item--primary">Baylor St. Luke's Medical Group</li></ul>
                <ul class="csh-aem-result-card__specialties"><li class="csh-aem-result-card__specialties__item csh-aem-result-card__specialties__item--secondary">Family Medicine</li><li class="csh-aem-result-card__specialties__item csh-aem-result-card__specialties__item--secondary">Primary Care</li></ul>
                <div class="csh-aem-result-card__line csh-aem-result-card__line--address"><span>6769 Lake Woodlands Drive, Suite E, The Woodlands, TX 77382</span></div>
                <a href="tel:281-555-0100">281-555-0100</a>
              </div>
              <div id="card-renee-sayer" class="csh-aem-result-card csh-aem-result-card--provider">
                <a class="csh-aem-result-card__link" href="/find-a-doctor/renee-sayer-1234567890"><h3 class="csh-aem-result-card__title">Renee Sayer, APRN-C, DNP, FNP-C, PMHNP-C</h3></a>
                <ul class="csh-aem-result-card__specialties"><li class="csh-aem-result-card__specialties__item csh-aem-result-card__specialties__item--primary">CHI Health Clinic</li></ul>
                <ul class="csh-aem-result-card__specialties"><li class="csh-aem-result-card__specialties__item csh-aem-result-card__specialties__item--secondary">Family Medicine</li><li class="csh-aem-result-card__specialties__item csh-aem-result-card__specialties__item--secondary">Primary Care</li></ul>
                <div class="csh-aem-result-card__line csh-aem-result-card__line--address"><span>1721 Colfax St, Schuyler, NE 68661</span></div>
              </div>
            </div></main>"""
        )
        _install_runtime(commonspirit)
        commonspirit.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        commonspirit_result = _message(commonspirit, {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"})
        assert commonspirit_result["platform"] == "commonspirit", commonspirit_result
        assert commonspirit_result["platform_label"] == "CommonSpirit Health"
        assert commonspirit_result["adapter_revision"] == "healthcare-directory-v9"
        assert commonspirit_result["count"] == 2, commonspirit_result
        commonspirit_profile = next(
            profile for profile in commonspirit_result["profiles"]
            if profile["source_id"] == "1407550627"
        )
        assert commonspirit_profile["name"] == "Clara Zee"
        assert commonspirit_profile["source_id"] == "1407550627"
        assert commonspirit_profile["location"] == "The Woodlands, TX"
        assert commonspirit_profile["specialties"] == ["Family Medicine", "Primary Care"]
        assert "Baylor St. Luke's Medical Group" in commonspirit_profile["employers"]
        assert "281-555-0100" not in commonspirit_profile["notes"]
        assert commonspirit_profile["profile_document"]["source_label"] == "CommonSpirit Health"
        renee_profile = next(
            profile for profile in commonspirit_result["profiles"]
            if profile["source_id"] == "1234567890"
        )
        assert renee_profile["name"] == "Renee Sayer"
        assert renee_profile["roles"] == ["Nurse Practitioner"]
        assert renee_profile["credentials"] == ["APRN-C", "DNP", "FNP-C", "PMHNP-C"]
        assert renee_profile["specialties"] == ["Family Medicine", "Primary Care"]
        assert "CHI Health Clinic" in renee_profile["employers"]

        actual_commonspirit_count = 0
        actual_snapshot = FRONTEND.parents[1] / "Commonspirit.txt"
        if actual_snapshot.is_file():
            actual_commonspirit = context.new_page()
            actual_commonspirit.goto(
                "https://providers.commonspirit.org/search?search=primary-care"
            )
            actual_commonspirit.set_content(actual_snapshot.read_text(encoding="utf-8"))
            _install_runtime(actual_commonspirit)
            actual_commonspirit.add_script_tag(
                path=str(FRONTEND / "healthcare-directory-content.js")
            )
            actual_result = _message(
                actual_commonspirit,
                {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"},
            )
            assert actual_result["platform"] == "commonspirit", actual_result
            assert actual_result["count"] >= 1, actual_result
            actual_commonspirit_count = actual_result["count"]
            actual_commonspirit.close()

        actual_commonspirit_profile_count = 0
        commonspirit_profile_snapshot = FRONTEND.parents[1] / "Commonspirit_profilr.txt"
        if commonspirit_profile_snapshot.is_file():
            actual_commonspirit_profile = context.new_page()
            actual_commonspirit_profile.goto(
                "https://www.commonspirit.org/find-a-doctor/soheila-hedayati-1265667067"
            )
            actual_commonspirit_profile.set_content(
                commonspirit_profile_snapshot.read_text(encoding="utf-8")
            )
            _install_runtime(actual_commonspirit_profile)
            actual_commonspirit_profile.add_script_tag(
                path=str(FRONTEND / "healthcare-directory-content.js")
            )
            actual_commonspirit_result = _message(
                actual_commonspirit_profile,
                {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"},
            )
            assert actual_commonspirit_result["ok"] is True, actual_commonspirit_result
            assert actual_commonspirit_result["count"] == 1, actual_commonspirit_result
            actual_profile = actual_commonspirit_result["profiles"][0]
            assert actual_profile["name"] == "Soheila Hedayati", actual_profile
            assert actual_profile["source_id"] == "1265667067", actual_profile
            assert actual_profile["credentials"] == ["MD"], actual_profile
            assert actual_profile["specialties"] == ["Internal Medicine"], actual_profile
            assert actual_profile["location"] == "Seattle, WA", actual_profile
            assert "board-certified" in actual_profile["profile_document"]["summary"]
            actual_commonspirit_profile_count = 1
            actual_commonspirit_profile.close()

        commonspirit_full_profile = context.new_page()
        commonspirit_full_profile.goto(
            "https://www.commonspirit.org/find-a-doctor/soheila-hedayati-1265667067"
        )
        commonspirit_full_profile.set_content(
            """<html><head><link rel="canonical" href="https://www.commonspirit.org/find-a-doctor/soheila-hedayati-1265667067"></head><body><main>
              <h1 class="csh-aem-provider-hero__title">Soheila Hedayati, MD</h1>
              <div class="csh-aem-provider-hero__org-unit">Virginia Mason Medical Center</div>
              <div class="csh-aem-provider-hero__address">1100 Ninth Avenue, Seattle, WA 98101</div>
              <section id="about"><div class="csh-aem-show-more-content-block__content">Full public professional biography.</div></section>
              <section id="specialties"><div class="csh-aem-provider-details__list-item">Internal Medicine</div><div class="csh-aem-provider-details__list-item">Primary Care</div></section>
              <section id="credentials"><div class="csh-aem-provider-details__list-item">American Board of Internal Medicine</div><div class="csh-aem-provider-details__list-item">American Board of Geriatric Medicine</div></section>
              <section id="education"><div class="csh-aem-provider-details__list-item"><strong>Medical School:</strong> University of Vienna, Austria, 1998</div><div class="csh-aem-provider-details__list-item"><strong>Residency:</strong> Lutheran Medical Center, 2008</div></section>
              <section id="medical_groups"><div class="csh-aem-medical-groups__list-item">Virginia Mason Medical Center</div><div class="csh-aem-medical-groups__list-item">Rainier Health Network</div></section>
              <section id="languages"><div class="csh-aem-provider-details__list-item">English</div><div class="csh-aem-provider-details__list-item">German</div></section>
              <script type="application/ld+json">{
                "@type":"Physician", "name":"Soheila Hedayati, MD",
                "url":"https://www.commonspirit.org/find-a-doctor/soheila-hedayati-1265667067",
                "medicalSpecialty":{"name":["Internal Medicine","Primary Care"]},
                "hospitalAffiliation":[{"name":"Virginia Mason Medical Center"}],
                "knowsLanguage":[{"name":"English"},{"name":"German"}],
                "telephone":"206-555-0100"
              }</script>
            </main></body></html>"""
        )
        _install_runtime(commonspirit_full_profile)
        commonspirit_full_profile.add_script_tag(
            path=str(FRONTEND / "healthcare-directory-content.js")
        )
        commonspirit_full_result = _message(
            commonspirit_full_profile,
            {"type": "RADIXSOL_CAPTURE_PLATFORM_PROFILE"},
        )
        assert commonspirit_full_result["ok"] is True, commonspirit_full_result
        full_document = commonspirit_full_result["profile"]["profile_document"]
        assert full_document["specialties"] == ["Internal Medicine", "Primary Care"]
        assert "Rainier Health Network" in full_document["hospitals"]
        assert any("University of Vienna" in value for value in full_document["education"])
        assert full_document["certifications"] == [
            "American Board of Internal Medicine",
            "American Board of Geriatric Medicine",
        ]
        assert full_document["languages"] == ["English", "German"]
        assert "206-555-0100" not in commonspirit_full_result["profile"]["notes"]
        commonspirit_full_profile.close()

        sharecare = context.new_page()
        sharecare.goto("https://providers.sharecare.com/find-a-doctor/specialty/cardiothoracic-surgery")
        sharecare.set_content(
            """<html><head><link rel="canonical" href="https://providers.sharecare.com/find-a-doctor/specialty/cardiothoracic-surgery"></head><body><main>
              <div data-qa-target="qa-southpaw-search">Cardiothoracic Surgery</div>
              <article class="ProviderCardAlternative" data-pwid="YJNDQ" data-npi="1306821244" data-href="/doctor/dr-raja-flores">
                <a class="ProviderCardAlternative-header" href="/doctor/dr-raja-flores">
                  <h3 class="ProviderCardAlternative-title">Dr. Raja Flores, MD</h3>
                  <div class="ProviderCardAlternative-meta"><span>Cardiothoracic Surgery</span></div>
                </a>
                <div class="ProviderCardAlternative-location-button-content"><a href="https://www.google.com/maps/dir/?api=1">1470 Madison Ave # 3333 New York, NY 10029</a></div>
                <a href="tel:(212) 257-0031">(212) 257-0031</a>
              </article>
              <script type="application/ld+json">{"@context":"https://schema.org","@type":"SearchResultsPage","provider":[{"@type":"Physician","name":"Dr. Raja Flores, MD","url":"https://providers.sharecare.com/doctor/dr-raja-flores","medicalSpecialty":{"@type":"MedicalSpecialty","name":"Cardiothoracic Surgery"},"address":{"@type":"PostalAddress","streetAddress":"1470 Madison Ave # 3333","addressLocality":"New York","addressRegion":"NY","postalCode":"10029"},"hospitalAffiliation":{"@type":"Hospital","name":"Mount Sinai Morningside"}}]}</script>
            </main></body></html>"""
        )
        _install_runtime(sharecare)
        sharecare.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        sharecare_result = _message(sharecare, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert sharecare_result["platform"] == "sharecare", sharecare_result
        assert sharecare_result["platform_label"] == "Sharecare"
        assert sharecare_result["adapter_revision"] == "healthcare-directory-v9"
        assert sharecare_result["count"] == 1, sharecare_result
        sharecare_candidate = sharecare_result["profiles"][0]
        assert sharecare_candidate["name"] == "Raja Flores"
        assert sharecare_candidate["source_id"] == "1306821244"
        assert sharecare_candidate["location"] == "New York, NY"
        assert sharecare_candidate["specialties"] == ["Cardiothoracic Surgery"]
        assert sharecare_candidate["employers"] == ["Mount Sinai Morningside"]
        assert "212) 257-0031" not in sharecare_candidate["notes"]
        sharecare.close()

        sharecare_profile = context.new_page()
        sharecare_profile.goto("https://providers.sharecare.com/doctor/dr-raja-flores")
        sharecare_profile.set_content(
            """<html><head><link rel="canonical" href="https://providers.sharecare.com/doctor/dr-raja-flores"></head><body><main>
              <h1>Dr. Raja Flores, MD</h1>
              <h2>Specialties</h2><ul><li>Cardiothoracic Surgery</li></ul>
              <h2>Education &amp; Training</h2><ul><li>Albert Einstein College of Medicine</li></ul>
              <h2>Board Certifications</h2><ul><li>American Board of Thoracic Surgery</li></ul>
              <h2>Licenses</h2><ul><li>New York State Medical License</li></ul>
              <script type="application/ld+json">{"@context":"https://schema.org","@type":"Physician","name":"Dr. Raja Flores, MD","url":"https://providers.sharecare.com/doctor/dr-raja-flores","description":"Public professional overview for Dr. Flores.","medicalSpecialty":{"@type":"MedicalSpecialty","name":"Cardiothoracic Surgery"},"hospitalAffiliation":{"@type":"Hospital","name":"Mount Sinai Morningside"},"alumni":{"@type":"CollegeOrUniversity","name":"Albert Einstein College of Medicine"},"hasCredential":{"@type":"EducationalOccupationalCredential","name":"American Board of Thoracic Surgery"},"address":{"@type":"PostalAddress","addressLocality":"New York","addressRegion":"NY"},"identifier":"1306821244"}</script>
            </main></body></html>"""
        )
        _install_runtime(sharecare_profile)
        sharecare_profile.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
        sharecare_capture = _message(sharecare_profile, {
            "type": "RADIXSOL_HEALTHCARE_DIRECTORY_V9_REQUEST",
            "original_type": "RADIXSOL_CAPTURE_PLATFORM_PROFILE",
        })
        assert sharecare_capture["ok"] is True, sharecare_capture
        assert sharecare_capture["platform"] == "sharecare"
        assert sharecare_capture["profile"]["source_id"] == "1306821244"
        sharecare_document = sharecare_capture["profile"]["profile_document"]
        assert sharecare_document["source_label"] == "Sharecare"
        assert sharecare_document["specialties"] == ["Cardiothoracic Surgery"]
        assert sharecare_document["hospitals"] == ["Mount Sinai Morningside"]
        assert sharecare_document["education"] == ["Albert Einstein College of Medicine"]
        assert sharecare_document["certifications"] == ["American Board of Thoracic Surgery"]
        assert sharecare_document["licenses"] == ["New York State Medical License"]
        sharecare_profile.close()

        actual_sharecare_count = 0
        sharecare_snapshot = FRONTEND.parents[1] / "Sharecare.txt"
        if sharecare_snapshot.is_file():
            actual_sharecare = context.new_page()
            actual_sharecare.goto(
                "https://providers.sharecare.com/find-a-doctor/specialty/cardiothoracic-surgery"
            )
            actual_sharecare.set_content(sharecare_snapshot.read_text(encoding="utf-8"))
            _install_runtime(actual_sharecare)
            actual_sharecare.add_script_tag(
                path=str(FRONTEND / "healthcare-directory-content.js")
            )
            actual_sharecare_result = _message(
                actual_sharecare,
                {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"},
            )
            assert actual_sharecare_result["platform"] == "sharecare", actual_sharecare_result
            assert actual_sharecare_result["count"] > 0, actual_sharecare_result
            assert all(
                candidate["source"] == "sharecare"
                and candidate["source_url"].startswith("https://providers.sharecare.com/doctor/")
                for candidate in actual_sharecare_result["profiles"]
            )
            actual_sharecare_count = actual_sharecare_result["count"]
            actual_sharecare.close()

        browser.close()
        print({
            "npino": npino_result["count"],
            "npiprofile": npi_profile_result["count"],
            "nysed": nysed_result["count"],
            "usnews": usnews_result["count"],
            "usnews_profile": 1,
            "medifind": medifind_result["count"],
            "medifind_profile": actual_medifind_profile_count,
            "commonspirit": commonspirit_result["count"],
            "commonspirit_snapshot": actual_commonspirit_count,
            "commonspirit_profile": actual_commonspirit_profile_count,
            "sharecare": sharecare_result["count"],
            "sharecare_profile": 1,
            "sharecare_snapshot": actual_sharecare_count,
        })


if __name__ == "__main__":
    main()
