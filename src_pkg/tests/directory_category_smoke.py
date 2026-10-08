"""Offline checks for directory categories added to the Chrome package."""
from __future__ import annotations

from healthcare_directory_adapter_smoke import BROWSER, FRONTEND, _install_runtime, _message
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(BROWSER))
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))
        cases = [
            (
                "https://npino.com/dentists/122300000x-dentist/",
                '<h3><a href="/dentist/1003000167-dr.-julio-edgardo-escobar/">Dr. Julio Edgardo Escobar</a></h3><p>Dentist</p><p>NPI Number: 1003000167</p><p>Address: Los Angeles, CA</p>',
                "npino", "1003000167", "Dentist",
            ),
            (
                "https://npino.com/nurses/363la2100x-acute-care/",
                '<h3><a href="/nurse/1003008913-sandra-k-walker/">Sandra K Walker</a></h3><p>Nurse Practitioner - Acute Care</p><p>NPI Number: 1003008913</p><p>Address: Cleveland, OH</p>',
                "npino", "1003008913", "Nurse Practitioner - Acute Care",
            ),
            (
                "https://health.usnews.com/physician-assistants/california",
                '<article><h3><a href="/physician-assistants/jane-smith-12345">Jane Smith</a></h3></article>',
                "usnews", "physician-assistants/jane-smith-12345", "Physician Assistant",
            ),
            (
                "https://health.usnews.com/dentists/general-dentists/california",
                '<article><h3><a href="/dentists/john-dentist-67890">John Dentist</a></h3></article>',
                "usnews", "dentists/john-dentist-67890", "Dentist",
            ),
            (
                "https://doctor.webmd.com/results?specialty=dentist",
                '<li class="ep" data-npi="1234567890"><a class="prov-name" href="/doctor/jane-smith-abc-overview">Jane Smith</a><span class="prov-specialty">Dentist</span></li>',
                "webmd", "1234567890", "Dentist",
            ),
        ]
        for url, html, platform, source_id, specialty in cases:
            page = context.new_page()
            page.goto(url)
            page.set_content(f"<main>{html}</main>")
            _install_runtime(page)
            page.add_script_tag(path=str(FRONTEND / "healthcare-directory-content.js"))
            result = _message(page, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
            assert result["platform"] == platform, (url, result)
            assert result["count"] == 1, (url, result)
            profile = result["profiles"][0]
            assert profile["source_id"] == source_id, (url, profile)
            assert specialty in (profile["headline"], *profile["roles"]), (url, profile)
            page.close()
        browser.close()
        print(f"Verified {len(cases)} directory category captures")


if __name__ == "__main__":
    main()
