"""Regression for the mixed individual/organization NPINO result page."""
from pathlib import Path

from playwright.sync_api import sync_playwright


SCRIPT = Path(__file__).parents[1] / "frontend" / "healthcare-directory-content.js"
QUALITY = Path(__file__).parents[1] / "frontend" / "profile-quality.js"
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")


def test_npino_twenty_cards_yield_nineteen_people():
    names = [
        "Ms. Ayumi E Belanger, PA-C", "Steve Gichuru, PA-C",
        "Mrs. Hana Dandona, RPA-C", "Christopher Holland",
        "Mrs. Meghan Elizabeth Brant, PAC", "Pamela Kimzey Donohue, SCD, PA-C",
        "Golden Valley Health Centers", "Ms. Colleen Elizabeth Brown, PA-C",
        "Mrs. Elizma Eksteen Mercier, PHYSICIAN ASSISTANT",
        "Marc A Downs, RPA-C", "Evan R Law, PA-C",
        "Laurel Elizabeth Lehman, P.A.", "Mrs. Chinyere Ngozi Eze, PA-C",
        "Joseph P Byrne, PAC", "Ms. Kristin M Orrico, RPA-C",
        "Mr. Jesse Thomas Johnson Ii, PA", "Abby Lynn Labrecque, PA-C",
        "Ms. Diane E Wheeler, MS, PA-C", "Melody Mukon, PA",
        "Maribel De Ponce, PA-C",
    ]
    cards = "".join(
        f'<div class="bg-white"><div><h3><a href="/nurse/{1003000000 + index}-{index}/">{name}</a></h3>'
        f'<p>Physician Assistant - Medical</p><p>NPI Number: {1003000000 + index}</p>'
        '<p>Address: 91 Branscomb Rd, Green Cove Springs, FL, 32043</p></div></div>'
        for index, name in enumerate(names)
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME))
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))
            page.goto("https://npino.com/nurses/363am0700x-medical/", wait_until="domcontentloaded")
            page.set_content(f"<html><body><main>{cards}</main></body></html>")
            page.evaluate("""() => { window.chrome = {runtime: {lastError:null, onMessage: {
              addListener: listener => { window.listener = listener; }}, sendMessage: () => {}}}; }""")
            page.add_script_tag(path=str(SCRIPT))
            result = page.evaluate("""() => new Promise(resolve => window.listener(
              {type:'RADIXSOL_LIST_PLATFORM_CANDIDATES'}, {}, resolve))""")
            assert result["count"] == 19, result
            assert "Ayumi E Belanger" in [profile["name"] for profile in result["profiles"]]
            assert "Golden Valley Health Centers" not in [profile["name"] for profile in result["profiles"]]
            page.add_script_tag(path=str(QUALITY))
            sanitized = page.evaluate("""profiles => window.RadixsolProfileQuality.sanitizeProfiles(
              profiles, {platform:'npino'})""", result["profiles"])
            assert len(sanitized["profiles"]) == 19, sanitized
        finally:
            browser.close()


def test_npino_detail_provides_professional_pdf_fields():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME))
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))
            page.goto("https://npino.com/nurse/1003001058-ms.-ayumi-e-belanger/", wait_until="domcontentloaded")
            page.set_content("""<main><h2>Ms. Ayumi E Belanger - 1003001058 Profile Details</h2>
              <table><tr><th>Nurse Name</th><td>Ms. Ayumi E Belanger</td></tr>
              <tr><th>Credential</th><td>PA-C</td></tr>
              <tr><th>Specialization</th><td>Physician Assistant - Medical</td></tr>
              <tr><th>Provider Entity Type</th><td>Individual</td></tr>
              <tr><th>Licence No.</th><td>PA12345</td></tr></table></main>""")
            page.evaluate("""() => { window.chrome = {runtime: {lastError:null, onMessage: {
              addListener: listener => { window.listener = listener; }}, sendMessage: () => {}}}; }""")
            page.add_script_tag(path=str(SCRIPT))
            result = page.evaluate("""() => new Promise(resolve => window.listener(
              {type:'RADIXSOL_CAPTURE_PLATFORM_PROFILE'}, {}, resolve))""")
            assert result["ok"] is True, result
            document = result["profile"]["profile_document"]
            assert document["specialties"] == ["Physician Assistant - Medical"]
            assert document["licenses"] == ["PA12345"]
            assert document["npi"] == "1003001058"
        finally:
            browser.close()


def test_usnews_profile_without_education_still_captures_pdf_fields():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME))
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))
            page.goto("https://health.usnews.com/physician-assistants/jane-smith-12345", wait_until="domcontentloaded")
            page.set_content("""<main><div class="hero"><h1>Jane Smith, PA-C</h1></div>
              <div data-tracking-placement="profile_header" data-tracking-campaign="specialty">Physician Assistant</div>
              <section id="overview"><h2>Overview</h2><p>Jane Smith is a physician assistant in California.</p></section>
              </main>""")
            page.evaluate("""() => { window.chrome = {runtime: {lastError:null, onMessage: {
              addListener: listener => { window.listener = listener; }}, sendMessage: () => {}}}; }""")
            page.add_script_tag(path=str(SCRIPT))
            result = page.evaluate("""() => new Promise(resolve => window.listener(
              {type:'RADIXSOL_CAPTURE_PLATFORM_PROFILE'}, {}, resolve))""")
            assert result["ok"] is True, result
            assert result["profile"]["profile_document"]["specialties"] == ["Physician Assistant"]
        finally:
            browser.close()
