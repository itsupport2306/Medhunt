"""Offline edge-case smoke tests for the Indeed/Vivian/ZipRecruiter adapters.

No backend or enrichment provider is contacted. These fixtures exercise the
DOM quality gates, deduplication, SPA empty transitions, and lazy-list scan.
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
          window.__radixsolSentMessages = [];
          window.chrome = {
            runtime: {
              lastError: null,
              onMessage: {
                addListener: (listener) => { window.__radixsolMessageListener = listener; }
              },
              sendMessage: (message, callback) => {
                window.__radixsolSentMessages.push(message);
                if (callback) callback({ ok: true });
              }
            },
            storage: { local: {
              set: async () => {}, remove: async () => {}, get: async () => ({})
            } }
          };
        }"""
    )


def _message(page, message, timeout=20_000):
    page.set_default_timeout(timeout)
    return page.evaluate(
        """(message) => new Promise((resolve, reject) => {
          const listener = window.__radixsolMessageListener;
          if (!listener) return reject(new Error('Content-script listener was not installed.'));
          let settled = false;
          const done = (value) => { if (!settled) { settled = true; resolve(value); } };
          listener(message, {}, done);
          setTimeout(() => {
            if (!settled) reject(new Error('Adapter response timed out.'));
          }, 19000);
        })""",
        message,
    )


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(BROWSER))
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))

        indeed = context.new_page()
        indeed.goto("https://employers.indeed.com/smartsourcing")
        indeed.set_content(
            """<main>
              <article data-cauto-id="MATCH_CARD_BASE-visible-1">
                <h2 data-cauto-id="candidate-name">Alice Morgan</h2>
                <p data-cauto-id="candidate-location">Denver, CO</p>
                <p data-cauto-id="candidate-job-title">Clinical Nurse</p>
              </article>
              <article data-cauto-id="MATCH_CARD_BASE-hidden-1" style="display:none">
                <h2 data-cauto-id="candidate-name">Hidden Person</h2>
                <p data-cauto-id="candidate-location">Miami, FL</p>
              </article>
              <article data-cauto-id="MATCH_CARD_BASE-hidden-2" aria-hidden="true">
                <h2 data-cauto-id="candidate-name">Aria Hidden</h2>
                <p data-cauto-id="candidate-location">Boston, MA</p>
              </article>
              <article data-cauto-id="MATCH_CARD_BASE-stale-1" data-radixsol-stale="true">
                <h2 data-cauto-id="candidate-name">Stale Candidate</h2>
                <p data-cauto-id="candidate-location">Austin, TX</p>
              </article>
              <article data-cauto-id="MATCH_CARD_BASE-visible-1">
                <h2 data-cauto-id="candidate-name">Alice Morgan</h2>
                <p data-cauto-id="candidate-location">Denver, CO</p>
              </article>
            </main>"""
        )
        _install_runtime(indeed)
        indeed.add_script_tag(path=str(FRONTEND / "indeed-content.js"))
        result = _message(indeed, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert result["count"] == 1, result
        assert result["profiles"][0]["name"] == "Alice Morgan", result
        assert result["profiles"][0]["roles"] == ["Clinical Nurse"], result
        assert result["profiles"][0]["employers"] == [], result

        # The observer must tell the side panel to clear candidates when an
        # SPA hides the final visible result without replacing the whole DOM.
        indeed.wait_for_timeout(1400)
        indeed.evaluate(
            """() => document.querySelectorAll(
              '[data-cauto-id="MATCH_CARD_BASE-visible-1"]'
            ).forEach((card) => card.setAttribute('aria-hidden', 'true'))"""
        )
        indeed.wait_for_timeout(1400)
        empty_events = indeed.evaluate(
            """() => window.__radixsolSentMessages.filter(
              (message) => message.type === 'RADIXSOL_PLATFORM_RESULTS_CHANGED'
            )"""
        )
        assert empty_events[-1]["count"] == 0, empty_events

        lazy = context.new_page()
        lazy.goto("https://employers.indeed.com/smartsourcing?lazy=1")
        lazy.set_content(
            """<div id="list" style="height:160px; overflow-y:auto">
              <article data-cauto-id="MATCH_CARD_BASE-lazy-1" style="height:100px">
                <h2 data-cauto-id="candidate-name">First Candidate</h2>
                <p data-cauto-id="candidate-location">Dallas, TX</p>
              </article>
              <div style="height:800px"></div>
            </div>
            <script>
              document.querySelector('#list').addEventListener('scroll', (event) => {
                if (event.currentTarget.scrollTop < 200 || document.querySelector('#lazy-two')) return;
                const card = document.createElement('article');
                card.id = 'lazy-two';
                card.setAttribute('data-cauto-id', 'MATCH_CARD_BASE-lazy-2');
                card.innerHTML = '<h2 data-cauto-id="candidate-name">Second Candidate</h2>' +
                  '<p data-cauto-id="candidate-location">Phoenix, AZ</p>';
                event.currentTarget.append(card);
              });
            </script>"""
        )
        _install_runtime(lazy)
        lazy.add_script_tag(path=str(FRONTEND / "indeed-content.js"))
        lazy_result = _message(
            lazy, {"type": "RADIXSOL_SCAN_PLATFORM_CANDIDATES"}, timeout=30_000
        )
        assert [profile["source_id"] for profile in lazy_result["profiles"]] == [
            "lazy-1", "lazy-2"
        ], lazy_result

        vivian = context.new_page()
        vivian.goto("https://www.vivian.com/talent-pool")
        vivian.set_content(
            """<main>
              <article data-qa="Candidate Card">
                <a href="/talent-pool/candidate/vivian-1">
                  <h3 data-qa="User Name">Zoë O'Neil</h3>
                </a>
                <div data-qa="Employer Chat Header">Respiratory Therapist</div>
                <dl><dt data-qa="Home location List Item DT">Home location</dt>
                  <dd data-qa="Home location List Item DD">Portland, OR</dd></dl>
              </article>
              <article data-qa="Candidate Card">
                <a href="/talent-pool/candidate/vivian-copy">
                  <h3 data-qa="User Name">Zoë O'Neil</h3>
                </a>
                <div data-qa="Employer Chat Header">Respiratory Therapist</div>
                <dl><dt data-qa="Home location List Item DT">Home location</dt>
                  <dd data-qa="Home location List Item DD">Portland, OR</dd></dl>
              </article>
              <article data-qa="Candidate Card"><h3 data-qa="User Name">Recommended Jobs</h3>
                <div data-qa="Employer Chat Header">Browse suggestions</div></article>
              <article data-qa="Candidate Card" aria-hidden="true">
                <h3 data-qa="User Name">Hidden Vivian</h3>
                <div data-qa="Employer Chat Header">Candidate</div>
              </article>
            </main>"""
        )
        _install_runtime(vivian)
        vivian.add_script_tag(path=str(FRONTEND / "platform-content.js"))
        vivian_result = _message(vivian, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert vivian_result["count"] == 1, vivian_result
        vivian_profile = vivian_result["profiles"][0]
        assert vivian_profile["name"] == "Zoë O'Neil", vivian_result
        assert vivian_profile["source_id"] == "vv_vivian-1", vivian_result
        assert vivian_profile["source_url"].endswith("/talent-pool/candidate/vivian-1")
        vivian.wait_for_timeout(1400)
        vivian.evaluate(
            """() => document.querySelectorAll('[data-qa="Candidate Card"]')
              .forEach((card) => card.setAttribute('aria-hidden', 'true'))"""
        )
        vivian.wait_for_timeout(1400)
        vivian_empty_events = vivian.evaluate(
            """() => window.__radixsolSentMessages.filter(
              (message) => message.type === 'RADIXSOL_PLATFORM_RESULTS_CHANGED'
            )"""
        )
        assert vivian_empty_events[-1]["count"] == 0, vivian_empty_events

        zip_page = context.new_page()
        zip_page.goto("https://www.ziprecruiter.com/emp/rdb/search")
        zip_page.set_content(
            """<main>
              <article><h2>Recommended Jobs</h2><a href="/jobs/nurse">View jobs</a></article>
              <article data-testid="candidate-card">
                <h2 data-testid="candidate-name">Jordan Rivera</h2>
                <p>Austin, TX</p><a id="candidate-link" href="/candidate/zr-edge-1">View profile</a>
              </article>
              <article data-testid="candidate-card" aria-hidden="true">
                <h2 data-testid="candidate-name">Invisible Candidate</h2>
                <p>Seattle, WA</p><a href="/candidate/zr-hidden">View profile</a>
              </article>
            </main>"""
        )
        _install_runtime(zip_page)
        zip_page.add_script_tag(path=str(FRONTEND / "platform-main.js"))
        zip_page.add_script_tag(path=str(FRONTEND / "platform-content.js"))
        zip_result = _message(zip_page, {"type": "RADIXSOL_LIST_PLATFORM_CANDIDATES"})
        assert zip_result["count"] == 1, zip_result
        assert zip_result["profiles"][0]["name"] == "Jordan Rivera", zip_result
        assert zip_result["profiles"][0]["source_id"] == "zr-edge-1", zip_result
        assert "Recommended Jobs" not in zip_result["profiles"][0]["notes"]
        zip_page.evaluate(
            """() => document.querySelector('#candidate-link').addEventListener('click', (event) => {
              event.preventDefault(); window.__radixsolCandidateOpened = true;
            })"""
        )
        opened = _message(zip_page, {"type": "RADIXSOL_OPEN_PLATFORM_CANDIDATE", "index": 0})
        assert opened["ok"] is True, opened
        assert zip_page.evaluate("() => window.__radixsolCandidateOpened") is True

        print({
            "indeed_visible": result["count"],
            "indeed_lazy": lazy_result["count"],
            "vivian": vivian_result["count"],
            "ziprecruiter": zip_result["count"],
            "empty_transition": empty_events[-1]["count"],
            "platform_empty_transition": vivian_empty_events[-1]["count"],
        })
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
