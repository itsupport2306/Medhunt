"""Offline browser smoke test for Indeed, Vivian, ZipRecruiter, LinkedIn, and Facebook adapters.

No backend or provider API is contacted. The browser routes both hostnames to
small local fixtures, loads the content scripts, and asks each adapter for its
currently displayed profiles.
"""
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
                addListener: (listener) => { window.__radixsolMessageListener = listener; },
                removeListener: (listener) => {
                  if (window.__radixsolMessageListener === listener) {
                    window.__radixsolMessageListener = null;
                  }
                }
              },
              sendMessage: (_message, callback) => { if (callback) callback({ ok: true }); }
            }
          };
        }"""
    )


def _message(page, message):
    return page.evaluate(
        """(message) => new Promise((resolve, reject) => {
          const listener = window.__radixsolMessageListener;
          if (!listener) return reject(new Error('Content-script listener was not installed.'));
          const asyncResponse = listener(message, {}, resolve);
          if (asyncResponse !== true) setTimeout(() => reject(new Error('No asynchronous response.')), 1000);
        })""",
        message,
    )


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(BROWSER),
        )
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))

        syntax_page = context.new_page()
        for filename in (
            "app.js", "background.js", "indeed-content.js", "linkedin-content.js",
            "facebook-content.js", "platform-content.js", "healthcare-directory-content.js",
        ):
            source = (FRONTEND / filename).read_text(encoding="utf-8")
            assert syntax_page.evaluate("source => { new Function(source); return true; }", source)
        syntax_page.close()

        indeed = context.new_page()
        indeed.goto("https://employers.indeed.com/smartsourcing?candidateId=shared-drawer-id")
        indeed.set_content(
            """<main data-candidate-id="shared-drawer-id">
              <article data-cauto-id="MATCH_CARD_BASE-indeed-1">
                <h2 data-cauto-id="candidate-name">Alex Morgan</h2>
                <p data-cauto-id="candidate-location">Atlanta, GA</p>
                <p data-cauto-id="candidate-job-title">Registered Nurse at Example Medical Center</p>
              </article>
              <article data-cauto-id="MATCH_CARD_BASE-indeed-2">
                <h2 data-cauto-id="candidate-name">Bailey Reed</h2>
                <p data-cauto-id="candidate-location">Louisville, KY</p>
                <p data-cauto-id="candidate-job-title">Dialysis Registered Nurse</p>
              </article>
            </main>"""
        )
        _install_runtime(indeed)
        indeed.add_script_tag(path=str(FRONTEND / "indeed-content.js"))
        indeed_result = _message(indeed, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert indeed_result["ok"] is True
        assert len(indeed_result["profiles"]) == 2, indeed_result
        assert [profile["source_id"] for profile in indeed_result["profiles"]] == [
            "indeed-1", "indeed-2",
        ]
        assert indeed_result["profiles"][0]["roles"] == ["Registered Nurse"]
        assert indeed_result["profiles"][0]["employers"] == ["Example Medical Center"]
        assert indeed_result["profiles"][0]["schools"] == []
        assert "Headline: Registered Nurse at Example Medical Center" in indeed_result["profiles"][0]["notes"]
        assert "Role: Registered Nurse" in indeed_result["profiles"][0]["notes"]
        assert "Employer: Example Medical Center" in indeed_result["profiles"][0]["notes"]

        indeed_profile = context.new_page()
        indeed_profile.goto("https://employers.indeed.com/smartsourcing?candidateId=indeed-profile-1")
        indeed_profile.set_content(
            """<aside role="dialog" data-testid="candidate-profile" data-candidate-id="indeed-profile-1">
              <h1 data-cauto-id="candidate-name">Casey Jordan</h1>
              <p data-cauto-id="candidate-location">Austin, TX</p>
              <p data-cauto-id="candidate-job-title">Clinical Nurse at Mercy Hospital</p>
              <h2>Work Experience</h2>
              <div>ICU Nurse</div><div>Previous Medical Center</div><div>2018 - 2022</div>
              <div>Provided acute bedside care to a diverse patient population.</div>
              <h2>Education</h2>
              <div>BSN</div><div>Example University</div><div>2014 - 2018</div>
              <h2>Skills</h2><div>Critical care nursing and patient education</div>
            </aside>"""
        )
        _install_runtime(indeed_profile)
        indeed_profile.add_script_tag(path=str(FRONTEND / "indeed-content.js"))
        indeed_profile_result = _message(
            indeed_profile, {"type": "RADIXSOL_CAPTURE_PLATFORM_PROFILE"},
        )
        captured_indeed = indeed_profile_result["profile"]
        assert captured_indeed["roles"] == ["Clinical Nurse", "ICU Nurse"]
        assert captured_indeed["employers"] == ["Mercy Hospital", "Previous Medical Center"]
        assert captured_indeed["schools"] == ["Example University"]
        assert "Role: ICU Nurse" in captured_indeed["notes"]
        assert "Employer: Previous Medical Center" in captured_indeed["notes"]
        assert "School: Example University" in captured_indeed["notes"]

        vivian = context.new_page()
        vivian.goto("https://www.vivian.com/talent-pool")
        vivian.set_content(
            """<article data-qa="Candidate Card">
              <h3 data-qa="User Name">Jane Doe</h3>
              <div data-qa="Employer Chat Header">Registered Nurse</div>
              <dl>
                <dt data-qa="Home location List Item DT">Home location</dt>
                <dd data-qa="Home location List Item DD">Portland, OR</dd>
                <dt data-qa="Discipline List Item DT">Discipline</dt>
                <dd data-qa="Discipline List Item DD">Registered Nurse</dd>
                <dt data-qa="Specialty List Item DT">Specialty</dt>
                <dd data-qa="Specialty List Item DD">ICU (7 years)</dd>
                <dt data-qa="License List Item DT">License</dt>
                <dd data-qa="License List Item DD">RN - Oregon</dd>
                <dt data-qa="Recent experience List Item DT">Recent experience</dt>
                <dd data-qa="Recent experience List Item DD">Example Medical Center</dd>
              </dl>
            </article>"""
        )
        _install_runtime(vivian)
        vivian.add_script_tag(path=str(FRONTEND / "platform-content.js"))
        vivian_result = _message(vivian, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert vivian_result["platform"] == "vivian"
        assert vivian_result["profiles"][0]["name"] == "Jane Doe"
        assert "Employer: Example Medical Center" in vivian_result["profiles"][0]["notes"]
        assert "Specialty: ICU" in vivian_result["profiles"][0]["notes"]

        zip_page = context.new_page()
        zip_page.goto("https://www.ziprecruiter.com/emp/rdb/search")
        zip_page.set_content('<section id="candidate" class="relative p-24 bg-white"></section>')
        _install_runtime(zip_page)
        zip_page.evaluate(
            """() => {
              document.querySelector('#candidate')['__reactFiber$radixsol'] = {
                memoizedProps: { candidate: {
                  encryptedJobseekerId: 'zr-test-1', name: 'John Smith', location: 'Austin, TX',
                  yearsOfExperience: 5,
                  employment: [{ company: 'Example Hospital', position: 'Registered Nurse' }],
                  education: [{ school: 'Example University', degree: 'BSN' }],
                  skills: ['ICU'], licenses: ['RN']
                } },
                return: null
              };
            }"""
        )
        zip_page.add_script_tag(path=str(FRONTEND / "platform-main.js"))
        zip_page.add_script_tag(path=str(FRONTEND / "platform-content.js"))
        zip_result = _message(zip_page, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert zip_result["platform"] == "ziprecruiter"
        assert zip_result["profiles"][0]["source_id"] == "zr-test-1"
        assert "Employer: Example Hospital" in zip_result["profiles"][0]["notes"]

        linkedin = context.new_page()
        linkedin.goto("https://www.linkedin.com/in/avinash-patel-b902343a4/")
        linkedin.set_content(
            """<!doctype html><html><head><title>Avinash Patel | LinkedIn</title></head><body><main>
              <section data-view-name="profile-card">
                <div>Avinash Patel</div>
                <div>Registered Nurse at Dr. AMIT DORKAR MULTISPECIALITY HOSPITAL, MIRAJ</div>
                <div>India · Contact info</div>
                <button aria-label="More actions">More</button>
                <div role="menu"><button role="menuitem">Save to PDF</button></div>
              </section>
              <section><h2>Experience</h2><div>Registered Nurse at Dr. AMIT DORKAR MULTISPECIALITY HOSPITAL</div><div>Staff Nurse at Previous Care Center</div></section>
              <section><h2>Education</h2><div>Gujarat University</div></section>
            </main></body></html>"""
        )
        _install_runtime(linkedin)
        linkedin.add_script_tag(path=str(FRONTEND / "linkedin-content.js"))
        linkedin_result = _message(linkedin, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert linkedin_result["platform"] == "linkedin"
        assert linkedin_result["profiles"][0]["name"] == "Avinash Patel"
        assert linkedin_result["profiles"][0]["source_id"] == "avinash-patel-b902343a4"
        assert linkedin_result["profiles"][0]["location"] == "India"
        assert linkedin_result["profiles"][0]["headline"].startswith("Registered Nurse")
        assert "Dr. AMIT DORKAR" in linkedin_result["profiles"][0]["notes"]
        assert "Staff Nurse" in linkedin_result["profiles"][0]["roles"]
        assert "Previous Care Center" in linkedin_result["profiles"][0]["employers"]
        assert linkedin_result["profiles"][0]["schools"] == ["Gujarat University"]
        assert "Role: Staff Nurse" in linkedin_result["profiles"][0]["notes"]
        assert "Employer: Previous Care Center" in linkedin_result["profiles"][0]["notes"]
        assert "School: Gujarat University" in linkedin_result["profiles"][0]["notes"]
        linkedin_guide = _message(linkedin, {"type": "RADIXSOL_GUIDE_LINKEDIN_PDF"})
        assert linkedin_guide["more_button_found"] is True
        linkedin_auto_pdf = _message(linkedin, {"type": "RADIXSOL_AUTO_LINKEDIN_PDF"})
        assert linkedin_auto_pdf["ok"] is True, linkedin_auto_pdf
        assert linkedin_auto_pdf["action"] == "save_to_pdf"

        linkedin_location = context.new_page()
        linkedin_location.goto("https://www.linkedin.com/in/elana-marsh-rn/")
        linkedin_location.set_content(
            """<!doctype html><html><head><title>Elana Marsh, BSN, RN | LinkedIn</title></head><body><main>
              <section data-view-name="profile-card">
                <h1>Elana Marsh, BSN, RN</h1><span>She/Her · 2nd</span>
                <div class="text-body-medium break-words">Registered Nurse at the U.S. Department of Veterans Affairs</div>
                <div class="text-body-small inline t-black--light break-words">Elana Marsh, BSN, RN She/Her · 2nd · She/Her</div>
                <div class="pv-text-details__left-panel mt2">
                  <span class="text-body-small inline t-black--light break-words">Philadelphia, Pennsylvania, United States</span>
                  <span><span>·</span><a id="top-card-text-details-contact-info" href="/in/elana-marsh-rn/overlay/contact-info/">Contact info</a></span>
                </div>
              </section>
            </main></body></html>"""
        )
        _install_runtime(linkedin_location)
        linkedin_location.add_script_tag(path=str(FRONTEND / "linkedin-content.js"))
        linkedin_location_result = _message(
            linkedin_location,
            {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert linkedin_location_result["profiles"][0]["name"] == "Elana Marsh"
        assert linkedin_location_result["profiles"][0]["location"] == (
            "Philadelphia, Pennsylvania, United States"
        ), linkedin_location_result
        assert "Role: Registered Nurse" in linkedin_location_result["profiles"][0]["notes"]
        assert (
            "Employer: the U.S. Department of Veterans Affairs"
            in linkedin_location_result["profiles"][0]["notes"]
        )

        linkedin_people = context.new_page()
        linkedin_people.goto(
            "https://www.linkedin.com/search/results/people/?keywords=registered%20nurse"
        )
        linkedin_people.set_content(
            """<!doctype html><html><body><main class="search-results-container">
              <ul role="list">
                <li class="reusable-search__result-container">
                  <a href="/in/reana-ramos-rn/"><img alt="Reana Ramos"></a>
                  <a class="entity-result__title-text" href="/in/reana-ramos-rn/">
                    <span aria-hidden="true">Reana Ramos Â· 2nd</span>
                  </a>
                  <div class="entity-result__primary-subtitle">BSN, RN, PHN</div>
                  <div class="entity-result__secondary-subtitle">Lodi, California, United States</div>
                  <div class="entity-result__summary">Current: Registered Nurse at Adventist Health</div>
                  <div class="entity-result__summary mutual">
                    <a href="/in/nakul-jain/"><img alt="Nakul Jain"></a>
                    <span>Nakul Jain is a mutual connection</span>
                  </div><button>Connect</button>
                </li>
                <li class="reusable-search__result-container">
                  <a class="entity-result__title-text" aria-label="View Elana Marsh, BSN, RN's profile"
                     href="https://www.linkedin.com/in/elana-marsh-rn/?miniProfileUrn=abc">
                    <span aria-hidden="true">Elana Marsh, BSN, RN â€¢ 2nd</span>
                  </a>
                  <div class="entity-result__primary-subtitle">Registered Nurse at the U.S. Department of Veterans Affairs</div>
                  <div class="entity-result__secondary-subtitle">Philadelphia, Pennsylvania, United States</div>
                  <div class="entity-result__summary">Current: Registered Nurse at U.S. Department of Veterans Affairs</div>
                  <button>Follow</button>
                </li>
                <div class="generic-result-row">
                  <a href="/in/maryann-liu-rn/"><span aria-hidden="true">Maryann Liu Â· 2nd</span></a>
                  <div data-radixsol-field="headline">Clinical Nurse at MSH</div>
                  <div data-radixsol-field="location">New York, New York, United States</div>
                  <div>Current: Registered Nurse at The Mount Sinai Hospital</div>
                  <div class="mutual">
                    <a href="/in/ankit-kumar/"><img alt="ANKIT KUMAR"></a>
                    <span>ANKIT KUMAR is a mutual connection</span>
                  </div><button>Connect</button>
                </div>
                <li data-chameleon-result-urn="urn:li:member:lum-tankem-rn">
                  <a href="/in/lum-tankem-rn/"><span aria-hidden="true">Lum Tankem Â· 2nd</span></a>
                  <div class="entity-result__primary-subtitle">Registered Nurse/ Healthcare Coordinator</div>
                  <div class="entity-result__secondary-subtitle">Dallas, Texas, United States</div>
                  <button>Connect</button>
                </li>
                <article>
                  <a href="/in/no-location-nurse/"><span>No Location Nurse Â· 3rd+</span></a>
                  <div class="entity-result__primary-subtitle">ICU Registered Nurse at Example Health</div>
                  <button>Connect</button>
                </article>
                <li style="display:none" class="reusable-search__result-container">
                  <a href="/in/hidden-candidate/"><span>Hidden Candidate</span></a>
                  <div class="entity-result__primary-subtitle">Registered Nurse</div>
                  <div class="entity-result__secondary-subtitle">Miami, Florida, United States</div>
                </li>
              </ul>
            </main></body></html>"""
        )
        _install_runtime(linkedin_people)
        linkedin_people.add_script_tag(path=str(FRONTEND / "linkedin-content.js"))
        linkedin_people_result = _message(
            linkedin_people,
            {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"},
        )
        assert linkedin_people_result["ok"] is True, linkedin_people_result
        assert linkedin_people_result["count"] == 5, linkedin_people_result
        assert linkedin_people_result["expected_count"] == 5
        assert [profile["name"] for profile in linkedin_people_result["profiles"]] == [
            "Reana Ramos", "Elana Marsh", "Maryann Liu", "Lum Tankem", "No Location Nurse",
        ], linkedin_people_result
        assert [profile["location"] for profile in linkedin_people_result["profiles"]] == [
            "Lodi, California, United States",
            "Philadelphia, Pennsylvania, United States",
            "New York, New York, United States",
            "Dallas, Texas, United States",
            "",
        ]
        assert [profile["headline"] for profile in linkedin_people_result["profiles"]] == [
            "BSN, RN, PHN",
            "Registered Nurse at the U.S. Department of Veterans Affairs",
            "Clinical Nurse at MSH",
            "Registered Nurse/ Healthcare Coordinator",
            "ICU Registered Nurse at Example Health",
        ]
        assert [profile["source_id"] for profile in linkedin_people_result["profiles"]] == [
            "reana-ramos-rn", "elana-marsh-rn", "maryann-liu-rn", "lum-tankem-rn",
            "no-location-nurse",
        ]
        assert [profile["source_url"] for profile in linkedin_people_result["profiles"]] == [
            "https://www.linkedin.com/in/reana-ramos-rn/",
            "https://www.linkedin.com/in/elana-marsh-rn/",
            "https://www.linkedin.com/in/maryann-liu-rn/",
            "https://www.linkedin.com/in/lum-tankem-rn/",
            "https://www.linkedin.com/in/no-location-nurse/",
        ]
        assert (
            "Employer: Adventist Health"
            in linkedin_people_result["profiles"][0]["notes"]
        )
        assert linkedin_people_result["profiles"][0]["roles"] == ["Registered Nurse"]
        assert linkedin_people_result["profiles"][0]["employers"] == ["Adventist Health"]
        assert linkedin_people_result["profiles"][0]["schools"] == []
        assert (
            "Current: Registered Nurse at The Mount Sinai Hospital"
            in linkedin_people_result["profiles"][2]["notes"]
        )
        assert linkedin_people_result["scan_limit"] == 100
        assert linkedin_people_result["limit_reached"] is False
        # LinkedIn virtualizes rows after scrolling. A quiet/current-DOM read
        # must retain candidates accumulated earlier for the same search URL.
        linkedin_people.evaluate(
            """() => Array.from(document.querySelectorAll(
              'li.reusable-search__result-container'
            )).slice(0, 2).forEach((element) => element.remove())"""
        )
        linkedin_people_retained = _message(
            linkedin_people,
            {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert linkedin_people_retained["count"] == 5, linkedin_people_retained
        linkedin_people_refreshed = _message(
            linkedin_people,
            {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"},
        )
        assert [profile["name"] for profile in linkedin_people_refreshed["profiles"]] == [
            "Maryann Liu", "Lum Tankem", "No Location Nurse",
        ], linkedin_people_refreshed
        linkedin_people.evaluate(
            """() => document.addEventListener('click', (event) => event.preventDefault(), true)"""
        )
        linkedin_opened = _message(
            linkedin_people,
            {"type": "RADIXSOL_OPEN_PLATFORM_CANDIDATE", "index": 0},
        )
        assert linkedin_opened == {
            "ok": True,
            "source_url": "https://www.linkedin.com/in/maryann-liu-rn/",
            "adapter_revision": "linkedin-capture-v4",
        }

        facebook = context.new_page()
        facebook.goto("https://www.facebook.com/jane.doe.rn")
        facebook.set_content(
            """<div role="main">
              <h1>Jane Doe</h1>
              <div>Intro</div>
              <div>Registered Nurse at Example Medical Center</div>
              <div>Lives in Portland, Oregon</div>
              <div>Studied at Example University</div>
            </div>"""
        )
        _install_runtime(facebook)
        facebook.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        facebook_result = _message(facebook, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert facebook_result["platform"] == "facebook"
        assert facebook_result["profiles"][0]["name"] == "Jane Doe"
        assert facebook_result["profiles"][0]["location"] == "Portland, Oregon"
        assert facebook_result["profiles"][0]["headline"] == "Registered Nurse"
        assert "Employer: Example Medical Center" in facebook_result["profiles"][0]["notes"]
        assert "School: Example University" in facebook_result["profiles"][0]["notes"]
        assert facebook_result["profiles"][0]["roles"] == ["Registered Nurse"]
        assert facebook_result["profiles"][0]["employers"] == ["Example Medical Center"]
        assert facebook_result["profiles"][0]["schools"] == ["Example University"]

        # Current Facebook variants do not always expose the profile name as
        # an h1. The browser title may only be a notification badge plus
        # "Facebook", while the visible profile header and Intro facts remain
        # available in the rendered main region.
        facebook_modern = context.new_page()
        facebook_modern.goto("https://www.facebook.com/ian.lee.rn/?sk=about")
        facebook_modern.set_content(
            """<!doctype html><html><head><title>(3) Facebook</title></head><body>
              <header role="banner">
                <h1 aria-label="Facebook">Facebook</h1>
                <span>Notifications</span><span>(3) Facebook</span>
              </header>
              <main role="main">
                <section data-pagelet="ProfileCover">
                  <img alt="Ian Lee's cover photo">
                  <a href="/ian.lee.rn"><span dir="auto">Ian Lee (Registered Nurse)</span></a>
                  <span>72 friends</span><button>Add friend</button><button>Message</button>
                </section>
                <section data-pagelet="ProfileTilesFeed_0">
                  <h2>Intro</h2>
                  <div><span dir="auto">I just love my job. I'm dedicated for helping people in need and just love everyone.</span></div>
                  <div><span aria-label="Current city">Boston, MA</span></div>
                  <div><span aria-label="Workplace">People's Choice Healthcare Solutions</span></div>
                  <div><span aria-label="College">Hayat College of Health Management Sciences &amp; Institute of Nursing</span></div>
                </section>
                <section><h2>Posts</h2><article>Unrelated timeline content</article></section>
              </main>
            </body></html>"""
        )
        _install_runtime(facebook_modern)
        facebook_modern.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        facebook_modern_result = _message(
            facebook_modern,
            {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        modern_profile = facebook_modern_result["profiles"][0]
        assert modern_profile["name"] == "Ian Lee", facebook_modern_result
        assert modern_profile["headline"] == "Registered Nurse"
        assert modern_profile["location"] == "Boston, MA"
        assert modern_profile["source_id"] == "ian.lee.rn"
        assert modern_profile["source_url"] == "https://www.facebook.com/ian.lee.rn"
        assert "Bio: I just love my job." in modern_profile["notes"]
        assert "Employer: People's Choice Healthcare Solutions" in modern_profile["notes"]
        assert (
            "School: Hayat College of Health Management Sciences & Institute of Nursing"
            in modern_profile["notes"]
        )
        assert "(3) Facebook" not in modern_profile["name"]

        # Facebook can place the profile cover before role=main and omit both
        # semantic headings and a self-profile link. Recover the visible name
        # next to the Friends/actions row and honor labelled international
        # locations rather than applying a US-only location grammar.
        facebook_live = context.new_page()
        facebook_live.goto("https://www.facebook.com/people/Jane-Soria/100091234567890/")
        facebook_live.set_content(
            """<!doctype html><html><head><title>(3) Facebook</title></head><body>
              <header role="banner">
                <h1 aria-label="Facebook">Facebook</h1>
                <nav><span>Home</span><span>Friends</span><span>Notifications</span></nav>
              </header>
              <section id="cover">
                <div dir="auto">Jane Soria (Registered Nurse)</div>
                <div>2.5K friends</div>
                <button>Add friend</button><button>Message</button>
                <div>RN ✨</div><div>Daddy's Little Princess</div>
              </section>
              <main role="main">
                <section><h2>Personal details</h2>
                  <div aria-label="Current city">Lives in Paris, France</div>
                  <div aria-label="Hometown">From Paris, France</div>
                </section>
              </main>
            </body></html>"""
        )
        _install_runtime(facebook_live)
        facebook_live.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        facebook_live_result = _message(
            facebook_live,
            {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        live_profile = facebook_live_result["profiles"][0]
        assert facebook_live_result["adapter_revision"] == "facebook-profile-v8"
        assert live_profile["name"] == "Jane Soria", facebook_live_result
        assert live_profile["headline"] == "Registered Nurse", facebook_live_result
        assert live_profile["location"] == "Paris, France", facebook_live_result
        assert live_profile["hometown"] == "Paris, France", facebook_live_result
        assert live_profile["source_id"] == "id:100091234567890"
        assert live_profile["source_url"] == (
            "https://www.facebook.com/profile.php?id=100091234567890"
        )
        facebook_live_v6 = _message(facebook_live, {
            "type": "RADIXSOL_FACEBOOK_V8_REQUEST",
            "original_type": "RADIXSOL_LIST_PLATFORM_CANDIDATES",
        })
        assert facebook_live_v6["adapter_revision"] == "facebook-profile-v8"
        assert facebook_live_v6["profiles"][0]["name"] == "Jane Soria"

        # Facebook may render the primary name and a nested alternate-name
        # fragment as separate nodes. Parentheses, aliases, credentials and
        # healthcare titles are context only; none may become lookup identity.
        facebook_name_shapes = [
            (
                "https://www.facebook.com/rhonda.hampton",
                """<main role="main"><section aria-label="Profile header">
                  <h1>Rhonda Hampton <span>(Rhonda Hampton)</span></h1>
                  <a href="/rhonda.hampton">184 friends</a><button>Add friend</button>
                  <div>Lives in Wichita Falls, Texas</div>
                </section></main>""",
                "Rhonda Hampton", "Wichita Falls, Texas", "",
            ),
            (
                "https://www.facebook.com/princess.rubia",
                """<main role="main"><section aria-label="Profile header">
                  <h1>PrincessJulana Rubia <span>(Intet)</span></h1>
                  <a href="/princess.rubia">1.6K friends</a><button>Add friend</button>
                  <div>Lives in Digos</div>
                </section></main>""",
                "PrincessJulana Rubia", "Digos", "Alternate name: Intet",
            ),
            (
                "https://www.facebook.com/kathy.shaiken",
                """<main role="main"><section aria-label="Profile header">
                  <h1>Kathy Shaiken RN</h1>
                  <a href="/kathy.shaiken">30 friends</a><button>Add friend</button>
                  <div>Lives in West Jefferson, NC</div>
                </section></main>""",
                "Kathy Shaiken", "West Jefferson, NC", "Professional descriptor: RN",
            ),
            (
                "https://www.facebook.com/maudelinee.nurse",
                """<main role="main"><section aria-label="Profile header">
                  <h1>MaudelineE Infirmière-Registered Nurse</h1>
                  <a href="/maudelinee.nurse">30 friends</a><button>Add friend</button>
                </section></main>""",
                "MaudelineE", "", "Professional descriptor: Infirmière-Registered Nurse",
            ),
            (
                "https://www.facebook.com/ideh.villu",
                """<main role="main"><section aria-label="Profile header">
                  <h1>Ideh Villu</h1>
                  <a href="/ideh.villu">557 friends</a><button>Add friend</button>
                </section></main>""",
                "Ideh Villu", "", "",
            ),
        ]
        for url, markup, expected_name, expected_location, expected_note in facebook_name_shapes:
            shaped = context.new_page()
            shaped.goto(url)
            shaped.set_content(markup)
            _install_runtime(shaped)
            shaped.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
            shaped_result = _message(
                shaped, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
            )
            assert shaped_result["ok"] is True, shaped_result
            shaped_profile = shaped_result["profiles"][0]
            assert shaped_profile["name"] == expected_name, shaped_result
            assert shaped_profile["location"] == expected_location, shaped_result
            if expected_note:
                assert expected_note in shaped_profile["notes"], shaped_result

        # On SPA navigation, the URL changes before Facebook replaces the old
        # cover DOM. The adapter must report loading (zero profiles) rather
        # than pairing Kathy Shaiken with Kathy Sirianni's source URL.
        facebook_spa = context.new_page()
        facebook_spa.goto("https://www.facebook.com/kathy.shaiken")
        facebook_spa.set_content(
            """<main role="main"><section id="profile" aria-label="Profile header">
              <h1 id="profile-name">Kathy Shaiken RN</h1>
              <a id="profile-link" href="/kathy.shaiken">30 friends</a>
              <button>Add friend</button><div id="profile-location">Lives in West Jefferson, NC</div>
            </section></main>"""
        )
        _install_runtime(facebook_spa)
        facebook_spa.evaluate(
            """() => {
              window.__radixsolRuntimeMessages = [];
              chrome.runtime.sendMessage = (message, callback) => {
                window.__radixsolRuntimeMessages.push(message);
                if (callback) callback({ok: true});
              };
            }"""
        )
        facebook_spa.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        first_spa = _message(
            facebook_spa, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert first_spa["profiles"][0]["name"] == "Kathy Shaiken", first_spa
        facebook_spa.evaluate(
            """() => history.pushState({}, '', '/kathy.sirianni')"""
        )
        facebook_spa.wait_for_timeout(550)
        stale_spa = _message(
            facebook_spa, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert stale_spa["ok"] is False, stale_spa
        assert stale_spa["error_code"] == "FACEBOOK_PROFILE_LOADING", stale_spa
        assert stale_spa["profiles"] == [], stale_spa
        route_messages = facebook_spa.evaluate("() => window.__radixsolRuntimeMessages")
        assert any(message.get("identity_changed") is True for message in route_messages)
        facebook_spa.evaluate(
            """() => {
              document.querySelector('#profile-name').textContent =
                'Kathy Sirianni (Kathy Scott Sirianni)';
              document.querySelector('#profile-link').href = '/kathy.sirianni';
              document.querySelector('#profile-link').textContent = '145 friends';
              document.querySelector('#profile-location').textContent =
                'Lives in Indian Land, SC';
            }"""
        )
        facebook_spa.wait_for_timeout(100)
        current_spa = _message(
            facebook_spa, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert current_spa["ok"] is True, current_spa
        assert current_spa["profiles"][0]["name"] == "Kathy Sirianni", current_spa
        assert current_spa["profiles"][0]["source_id"] == "kathy.sirianni", current_spa
        assert current_spa["profiles"][0]["location"] == "Indian Land, SC", current_spa
        assert "Alternate name: Kathy Scott Sirianni" in current_spa["profiles"][0]["notes"]

        # Current city and hometown are separate identity inputs. Parentheses
        # remain name context, and a semantic aria label may wrap visible
        # `Lives in` / `From` text without duplicating either prefix.
        facebook_hometown = context.new_page()
        facebook_hometown.goto("https://www.facebook.com/elizabeth.cruz")
        facebook_hometown.set_content(
            """<main role="main"><section aria-label="Profile header">
              <h1>Elizabeth Cruz <span>(Liz)</span></h1>
              <a href="/elizabeth.cruz">251 friends</a><button>Add friend</button>
              <div aria-label="Current city">Lives in Virginia Beach, Virginia</div>
              <div aria-label="Hometown">From Wichita, Kansas</div>
            </section></main>"""
        )
        _install_runtime(facebook_hometown)
        facebook_hometown.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        hometown_result = _message(
            facebook_hometown, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        hometown_profile = hometown_result["profiles"][0]
        assert hometown_profile["name"] == "Elizabeth Cruz", hometown_result
        assert hometown_profile["location"] == "Virginia Beach, Virginia", hometown_result
        assert hometown_profile["hometown"] == "Wichita, Kansas", hometown_result
        assert "Alternate name: Liz" in hometown_profile["notes"], hometown_result
        assert "Hometown: Wichita, Kansas" in hometown_profile["notes"], hometown_result

        # `From` alone is not the person's current location. Keep it available
        # solely as a later fallback instead of silently promoting it.
        facebook_from_only = context.new_page()
        facebook_from_only.goto("https://www.facebook.com/from.only.person")
        facebook_from_only.set_content(
            """<main role="main"><section aria-label="Profile header">
              <h1>From Only Person</h1>
              <a href="/from.only.person">50 friends</a><button>Add friend</button>
              <div aria-label="Hometown">From Digos</div>
            </section></main>"""
        )
        _install_runtime(facebook_from_only)
        facebook_from_only.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        from_only_result = _message(
            facebook_from_only, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        from_only_profile = from_only_result["profiles"][0]
        assert from_only_profile["location"] == "", from_only_result
        assert from_only_profile["hometown"] == "Digos", from_only_result

        # A Facebook SPA navigation can leave the preceding People search
        # mounted alongside the opened profile. The adapter must scope facts
        # to the active profile main and must not capture Jane (or Singapore)
        # from the stale search-results main. This shape mirrors the inspected
        # DOM supplied with the Ma Nii Shaa profile open.
        facebook_two_mains = context.new_page()
        facebook_two_mains.goto("https://www.facebook.com/manisha.gahatraj.754")
        facebook_two_mains.set_content(
            """<!doctype html><html><head><title>(3) Facebook</title></head><body>
              <header role="banner"><h1 aria-label="Facebook">Facebook</h1></header>
              <div role="main" aria-label="Search results">
                <div role="feed">
                  <div role="article">
                    <a role="presentation" href="/profile.php?id=100082971725801">
                      Mary Jane Francisco (Registered Nurse)
                    </a>
                    <span>Lives in Singapore</span><div role="button">Add friend</div>
                  </div>
                  <div role="article">
                    <a role="presentation" href="/janecristy.gumatasoria">
                      Jane Soria (Registered Nurse)
                    </a>
                    <span>Lives in Paris, France</span><div role="button">Add friend</div>
                  </div>
                </div>
              </div>
              <div role="main">
                <section aria-label="Profile header">
                  <div aria-label="View profile cover photo"></div>
                  <div role="button"><span dir="auto">Ma Nii Shaa (Registered Nurse)</span></div>
                  <a href="/manisha.gahatraj.754/friends_all/">116 friends</a>
                  <div role="button">Add friend</div><div role="button">Message</div>
                  <nav role="tablist">
                    <a role="tab" aria-selected="true" href="/manisha.gahatraj.754/">All</a>
                    <a role="tab" aria-selected="false" href="/manisha.gahatraj.754/about">About</a>
                  </nav>
                </section>
                <section aria-labelledby="personal-details-heading">
                  <h2 id="personal-details-heading">Personal details</h2>
                  <div role="list" aria-label="Personal details">
                    <div role="listitem"><span>Lives in London, United Kingdom</span></div>
                    <div role="listitem"><span>From Kathmandu, Nepal</span></div>
                    <div role="listitem"><span>Single</span></div>
                  </div>
                </section>
              </div>
              <div role="dialog" aria-label="Notifications">
                <h2>Notifications</h2><span>Welcome to Facebook!</span>
              </div>
              <script>window.moduleName = 'ProfileCometLockedProfilePopover';</script>
              <div hidden>ProfileCometLockedProfilePopover</div>
            </body></html>"""
        )
        _install_runtime(facebook_two_mains)
        facebook_two_mains.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        two_mains_result = _message(
            facebook_two_mains,
            {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert two_mains_result["ok"] is True, two_mains_result
        two_mains_profile = two_mains_result["profiles"][0]
        assert two_mains_profile["name"] == "Ma Nii Shaa", two_mains_result
        assert two_mains_profile["headline"] == "Registered Nurse", two_mains_result
        assert two_mains_profile["location"] == "London, United Kingdom", two_mains_result
        assert two_mains_profile["hometown"] == "Kathmandu, Nepal", two_mains_result
        assert two_mains_profile["source_id"] == "manisha.gahatraj.754"
        assert "Jane Soria" not in two_mains_profile["notes"]
        assert "Paris, France" not in two_mains_profile["notes"]
        assert "Singapore" not in two_mains_profile["notes"]
        assert "ProfileCometLockedProfilePopover" not in two_mains_profile["notes"]

        # Locked profiles are deliberately outside the product scope. A
        # visible Facebook lock notice must prevent capture and enrichment,
        # while generic hidden JS/module strings (tested above) must not.
        facebook_locked = context.new_page()
        facebook_locked.goto("https://www.facebook.com/locked.nurse")
        facebook_locked.set_content(
            """<!doctype html><html><head><title>(3) Facebook</title></head><body>
              <header role="banner"><h1 aria-label="Facebook">Facebook</h1></header>
              <main role="main">
                <section aria-label="Profile header">
                  <div role="button"><span dir="auto">Locked Nurse (Registered Nurse)</span></div>
                  <div role="button">Add friend</div><div role="button">Message</div>
                  <div role="status" aria-label="This profile is locked">
                    <span>This profile is locked</span>
                  </div>
                  <div>Lives in Miami, Florida</div>
                </section>
              </main>
            </body></html>"""
        )
        _install_runtime(facebook_locked)
        facebook_locked.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        locked_result = _message(
            facebook_locked,
            {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert locked_result["ok"] is False, locked_result
        assert locked_result["error_code"] == "FACEBOOK_PROFILE_LOCKED"
        assert locked_result["profiles"] == []
        assert locked_result["count"] == 0
        assert locked_result["expected_count"] == 0

        facebook_locked_aria = context.new_page()
        facebook_locked_aria.goto("https://www.facebook.com/profile.php?id=987654321")
        facebook_locked_aria.set_content(
            """<!doctype html><html><body><main role="main">
              <h1>Another Locked Nurse</h1>
              <div role="button">Add friend</div>
              <div role="status" aria-label="Profile locked"></div>
            </main></body></html>"""
        )
        _install_runtime(facebook_locked_aria)
        facebook_locked_aria.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        locked_aria_result = _message(
            facebook_locked_aria,
            {"type": "RADIXSOL_CAPTURE_PLATFORM_PROFILE"},
        )
        assert locked_aria_result["ok"] is False, locked_aria_result
        assert locked_aria_result["error_code"] == "FACEBOOK_PROFILE_LOCKED"
        assert locked_aria_result["profiles"] == []

        facebook_profile_php = context.new_page()
        facebook_profile_php.goto("https://www.facebook.com/profile.php?id=123456789&sk=about")
        facebook_profile_php.set_content(
            """<main><h1>Profile PHP Person (Registered Nurse)</h1>
              <div>100 friends</div><button>Add friend</button>
              <div>Lives in Toronto, Canada</div></main>"""
        )
        _install_runtime(facebook_profile_php)
        facebook_profile_php.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        profile_php_result = _message(
            facebook_profile_php,
            {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert profile_php_result["profiles"][0]["name"] == "Profile PHP Person"
        assert profile_php_result["profiles"][0]["source_id"] == "id:123456789"

        facebook_name_pending = context.new_page()
        facebook_name_pending.goto("https://www.facebook.com/jane.pending")
        facebook_name_pending.set_content(
            """<header role="banner"><h1>Facebook</h1><span>Notifications</span></header>
              <main role="main"><div>Loading profile…</div></main>"""
        )
        _install_runtime(facebook_name_pending)
        facebook_name_pending.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        name_pending_result = _message(
            facebook_name_pending,
            {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert name_pending_result["ok"] is False
        assert name_pending_result["error_code"] == "FACEBOOK_PROFILE_LOADING"

        facebook_reserved = context.new_page()
        facebook_reserved.goto("https://www.facebook.com/groups/registerednurses")
        facebook_reserved.set_content(
            """<main role="main"><h1>Registered Nurses</h1><div>Boston, MA</div></main>"""
        )
        _install_runtime(facebook_reserved)
        facebook_reserved.add_script_tag(path=str(FRONTEND / "facebook-content.js"))
        facebook_reserved_result = _message(
            facebook_reserved,
            {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"},
        )
        assert facebook_reserved_result["ok"] is False
        assert facebook_reserved_result["error_code"] == "FACEBOOK_PROFILE_URL_REQUIRED"

        print({
            "indeed": len(indeed_result["profiles"]),
            "vivian": len(vivian_result["profiles"]),
            "ziprecruiter": len(zip_result["profiles"]),
            "linkedin": len(linkedin_result["profiles"]),
            "linkedin_people": len(linkedin_people_result["profiles"]),
            "facebook": len(facebook_result["profiles"]),
            "facebook_modern": len(facebook_modern_result["profiles"]),
            "facebook_live": len(facebook_live_result["profiles"]),
        })
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
