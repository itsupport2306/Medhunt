"""Offline UI regression checks for the Medhunt candidate workbench.

The production side-panel code is rendered with mocked browser and backend APIs.
No enrichment provider or network service is contacted.
"""
from __future__ import annotations

import re
import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from sidepanel_lifecycle_smoke import BROWSERS, FRONTEND, MOCK_SCRIPT


def _load_panel(page: Page) -> None:
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    index = re.sub(r"\s*<script\s+src=[^>]+></script>", "", index, flags=re.I)
    page.set_content(index)
    page.add_style_tag(path=str(FRONTEND / "styles.css"))
    page.evaluate(MOCK_SCRIPT)
    page.add_script_tag(path=str(FRONTEND / "profile-quality.js"))
    source = (FRONTEND / "app.js").read_text(encoding="utf-8")
    extension_check = (
        'const IS_EXTENSION = ["chrome-extension:", "moz-extension:"]'
        ".includes(location.protocol);"
    )
    assert extension_check in source
    if 'const DEFAULT_BACKEND = "https://medhunt1.onrender.com";' in source:
        # Packaged UI geometry tests model a valid persisted sign-in. The
        # separate hosted-auth test below deliberately starts without one.
        page.evaluate(
            """() => { window.__panelTest.local.medhuntHealthBoardSession = {
              extension_token: 'smoke-test-token', user: { name: 'Test Recruiter' }
            }; }"""
        )
    page.add_script_tag(content=source.replace(extension_check, "const IS_EXTENSION = true;", 1))
    page.wait_for_selector(".capture-row")


def _run_hosted_auth_gate(browser_type, executable: Path) -> dict:
    """A hosted extension must keep login open and pause profile scanning."""
    browser = browser_type.launch(headless=True, executable_path=str(executable))
    page = browser.new_page(viewport={"width": 420, "height": 760})
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    index = re.sub(r"\s*<script\s+src=[^>]+></script>", "", index, flags=re.I)
    page.set_content(index)
    page.add_style_tag(path=str(FRONTEND / "styles.css"))
    page.evaluate(MOCK_SCRIPT)
    page.add_script_tag(path=str(FRONTEND / "profile-quality.js"))

    source = (FRONTEND / "app.js").read_text(encoding="utf-8")
    extension_check = (
        'const IS_EXTENSION = ["chrome-extension:", "moz-extension:"]'
        ".includes(location.protocol);"
    )
    local_backend = 'const DEFAULT_BACKEND = "http://127.0.0.1:8091";'
    hosted_backend = 'const DEFAULT_BACKEND = "https://medhunt1.onrender.com";'
    assert extension_check in source
    source = source.replace(extension_check, "const IS_EXTENSION = true;", 1)
    if local_backend in source:
        assert source.count(local_backend) == 1
        source = source.replace(local_backend, hosted_backend, 1)
    else:
        assert source.count(hosted_backend) == 1
    page.add_script_tag(content=source)
    page.wait_for_selector("#medhuntLoginEmail")
    page.wait_for_timeout(150)

    assert page.locator("#medhuntLoginEmail").is_visible()
    state = page.evaluate("() => window.__panelTest")
    assert "/auth/config" in state["fetchPaths"]
    assert "/health" not in state["fetchPaths"], state["fetchPaths"]
    assert state["scanCalls"] == {}, state["scanCalls"]
    browser.close()
    return {"login_visible": True, "health_checks_before_login": 0, "scans_before_login": 0}


def _seed_profiles(page: Page, count: int = 50) -> None:
    page.evaluate(
        """count => {
          const longName = "Alexandria-Marguerite O'Callaghan-Santamaría, DNP, APRN, RN, Clinical Nurse Specialist";
          const longLocation = "Northwest Washington metropolitan region, District of Columbia, United States";
          const profiles = Array.from({ length: count }, (_, index) => ({
            name: index === 0 ? longName : `Candidate ${String(index + 1).padStart(2, '0')} Example`,
            location: index === 0 ? longLocation : `${index % 2 ? 'Philadelphia' : 'Denver'}, ${index % 2 ? 'Pennsylvania' : 'Colorado'}, United States`,
            headline: index === 0
              ? "Critical Care Registered Nurse and patient safety educator with trauma, telemetry, and clinical leadership experience"
              : `${index % 3 ? 'Registered Nurse' : 'Clinical Nurse Educator'} at Regional Health Network`,
            roles: [index % 3 ? 'Registered Nurse' : 'Clinical Nurse Educator'],
            employers: ['Regional Health Network'],
            source: 'linkedin',
            source_id: `candidate-${index + 1}`,
            source_url: `https://www.linkedin.com/in/candidate-${index + 1}/`,
            result_index: index,
            _selectionKey: `id:candidate-${index + 1}`,
            _candidateId: 3000 + index,
            _sourceTabId: 2,
          }));
          activeSourcingPlatform = SOURCING_PLATFORMS.linkedin;
          activePageIndicatorLabel = 'LinkedIn';
          indeedCandidates = profiles;
          indeedLookupProfiles = [];
          indeedLookupState = new Map();
          indeedLookupScope = new Set();
          indeedLookupSummary = null;
          indeedResultFilter = 'all';
          indeedSelected = new Set(profiles.map(profile => profile._selectionKey));
          skippedProfileCount = 2;
          indeedScanState = { phase: 'captured', found: count, total: count };
          renderIndeedProfiles();
        }""",
        count,
    )


def _seed_mixed_results(page: Page) -> dict:
    return page.evaluate(
        """() => {
          indeedLookupProfiles = indeedCandidates.slice();
          indeedLookupScope = new Set(indeedCandidates.map(profile => profile._selectionKey));
          indeedLookupState = new Map();
          const summary = { total: indeedCandidates.length, processed: indeedCandidates.length, matched: 0, no_match: 0, errors: 0 };
          for (let index = 0; index < indeedCandidates.length; index += 1) {
            const profile = indeedCandidates[index];
            if (index % 10 === 0) {
              indeedLookupState.set(profile._selectionKey, { status: 'failed', emails: [], phones: [] });
              summary.errors += 1;
            } else if (index % 3 === 0) {
              const phones = ['+12125550100', '+12125550101', '+12125550102', '+12125550103'];
              indeedLookupState.set(profile._selectionKey, {
                status: 'found',
                emails: [
                  `candidate.${index}.personal@example.com`,
                  `candidate.${index}.clinical.recruiting.address@regional-health-network.example`,
                ],
                phones,
                phone_contacts: phones.map((value, phoneIndex) => ({ value, kind: phoneIndex === 0 ? 'mobile' : 'other' })),
                resume_required: false,
              });
              summary.matched += 1;
            } else {
              indeedLookupState.set(profile._selectionKey, { status: 'not_found', emails: [], phones: [] });
              summary.no_match += 1;
            }
          }
          indeedLookupSummary = summary;
          indeedResultFilter = 'all';
          indeedScanState = { phase: 'results', found: summary.total, total: summary.total };
          renderIndeedProfiles();
          return summary;
        }"""
    )


def _assert_inactive_header_progress(page: Page) -> None:
    """An idle header keeps its layout slot without exposing a fake progressbar."""
    header = page.locator('[data-testid="source-header"]')
    progress = page.locator('[data-testid="source-progress"]')
    progressbar = page.locator('[data-testid="source-progressbar"]')
    assert header.get_attribute("aria-busy") == "false"
    assert header.get_attribute("data-progress-kind") == "none"
    assert progress.get_attribute("data-kind") == "none"
    assert progress.get_attribute("data-mode") == "inactive"
    assert progress.get_attribute("aria-hidden") == "true"
    assert progressbar.get_attribute("role") is None


def _assert_header_progress(
    page: Page,
    *,
    kind: str,
    mode: str,
    current: int | None,
    total: int | None,
) -> None:
    """Assert the single public progress surface lives inside the top header."""
    header = page.locator('[data-testid="source-header"]')
    progress = page.locator('[data-testid="source-progress"]')
    progressbar = page.locator('[data-testid="source-progressbar"]')
    assert header.get_attribute("aria-busy") == "true"
    assert header.get_attribute("data-progress-kind") == kind
    assert progress.get_attribute("data-kind") == kind
    assert progress.get_attribute("data-mode") == mode
    assert progress.get_attribute("aria-hidden") is None
    assert progressbar.get_attribute("role") == "progressbar"
    assert page.locator('[role="progressbar"]').count() == 1
    assert progressbar.evaluate("element => element.closest('[data-testid=source-progress]') !== null")
    assert page.locator(".panel-rescan-button").is_disabled()
    assert page.locator(".panel-rescan-button").get_attribute("aria-disabled") == "true"
    if mode == "determinate":
        assert progressbar.get_attribute("aria-valuemin") == "0"
        assert progressbar.get_attribute("aria-valuenow") == str(current)
        assert progressbar.get_attribute("aria-valuemax") == str(total)
    else:
        assert progressbar.get_attribute("aria-valuemin") is None
        assert progressbar.get_attribute("aria-valuenow") is None
        assert progressbar.get_attribute("aria-valuemax") is None


def _layout(page: Page) -> dict:
    return page.evaluate(
        """() => {
          const list = document.querySelector('#indeedCandidateList');
          const header = document.querySelector('[data-testid="source-header"]');
          const dock = document.querySelector('[data-testid="action-dock"]');
          const row = document.querySelector('.capture-row:not([hidden])');
          const primary = row?.querySelector('.capture-row-primary');
          const avatar = row?.querySelector('.capture-avatar');
          const checkboxControl = row?.querySelector('.candidate-select-control');
          const identity = row?.querySelector('.capture-identity');
          const interactive = [...document.querySelectorAll('button:not([hidden]), input:not([hidden])')]
            .filter(element => {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.display !== 'none' && style.visibility !== 'hidden' && rect.width && rect.height;
            });
          return {
            width: innerWidth,
            rootScrollWidth: document.documentElement.scrollWidth,
            bodyScrollWidth: document.body.scrollWidth,
            listScrollHeight: list?.scrollHeight || 0,
            listClientHeight: list?.clientHeight || 0,
            headerBackground: header ? getComputedStyle(header).backgroundImage : '',
            headerBackgroundColor: header ? getComputedStyle(header).backgroundColor : '',
            dockVisible: Boolean(dock && dock.getBoundingClientRect().height),
            rowDisplay: row ? getComputedStyle(row).display : '',
            rowBorderStyle: row ? getComputedStyle(row).borderStyle : '',
            rowRadius: row ? getComputedStyle(row).borderRadius : '',
            primaryDisplay: primary ? getComputedStyle(primary).display : '',
            avatarRadius: avatar ? getComputedStyle(avatar).borderRadius : '',
            checkboxTargetHeight: checkboxControl?.getBoundingClientRect().height || 0,
            primaryItemsAligned: Boolean(primary && avatar && checkboxControl && identity) &&
              Math.abs(avatar.getBoundingClientRect().top - checkboxControl.getBoundingClientRect().top) < 1 &&
              Math.abs(avatar.getBoundingClientRect().top - identity.getBoundingClientRect().top) < 1,
            tooNarrowControls: interactive.filter(element => {
              if (element.closest('.result-summary')) return false;
              const rect = element.getBoundingClientRect();
              return rect.left < -1 || rect.right > innerWidth + 1;
            }).length,
          };
        }"""
    )


def _run_browser(browser_type, executable: Path) -> dict:
    browser = browser_type.launch(headless=True, executable_path=str(executable))
    page = browser.new_page(viewport={"width": 420, "height": 760})
    _load_panel(page)
    _seed_profiles(page)

    assert "50 profiles ready" in page.locator("body").inner_text()
    assert "candidates captured" not in page.locator("body").inner_text().lower()
    assert page.locator(".capture-row").count() == 50
    assert page.locator(".candidate-select-control").count() == 50
    page.evaluate("""() => {
      rememberEnrichedProfile(indeedCandidates[0]);
      indeedSelected = new Set();
      toggleAllIndeedCandidates();
      renderIndeedProfiles();
    }""")
    assert page.locator(".capture-row.previously-enriched").count() == 1
    assert page.locator(".already-enriched-badge").inner_text() == "Already enriched"
    assert page.locator(".capture-row.previously-enriched input").is_disabled()
    assert page.evaluate("() => !indeedSelected.has(indeedCandidates[0]._selectionKey)")
    page.evaluate("""() => {
      enrichedProfileKeys.clear();
      indeedSelected = new Set(indeedCandidates.map(profile => profile._selectionKey));
      renderIndeedProfiles();
    }""")
    assert page.locator(".source-brand-copy strong").inner_text() == "Medhunt"
    assert page.locator(".medhunt-mark").count() == 1
    assert page.locator(".radixsol-mark").count() == 0
    assert page.locator(".medhunt-mark").evaluate("el => getComputedStyle(el).borderRadius") != "50%"
    _assert_inactive_header_progress(page)

    # Health can resolve after the profile queue has rendered. Updating the
    # connection state must refresh the visible header instead of leaving a
    # stale "Service offline" label behind.
    page.evaluate("() => { backendHealth = null; updateSourceHeaderServiceStatus(); }")
    assert page.locator("#sourceHeaderStatus").inner_text() == "Service offline"
    page.evaluate(
        "() => { backendHealth = { status: 'ok' }; updateSourceHeaderServiceStatus(); }"
    )
    assert page.locator("#sourceHeaderStatus").inner_text() == "Ready to find contacts"

    # The profile queue stays visually consistent with the compact reference
    # workflow; filtering belongs in the source site rather than above cards.
    assert page.locator("#candidateSearch").count() == 0
    assert page.locator("#candidateFilterEmpty").count() == 0

    captured_layouts = {}
    for width, height in ((320, 700), (420, 760), (600, 900)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(50)
        layout = _layout(page)
        assert layout["rootScrollWidth"] <= width + 1, layout
        assert layout["bodyScrollWidth"] <= width + 1, layout
        assert layout["tooNarrowControls"] == 0, layout
        assert layout["dockVisible"], layout
        assert layout["rowDisplay"] == "flex", layout
        assert layout["rowBorderStyle"] == "solid", layout
        assert layout["primaryDisplay"] == "grid", layout
        assert layout["primaryItemsAligned"], layout
        assert layout["checkboxTargetHeight"] >= 44, layout
        # The restrained shell uses one solid brand color; decorative gradients
        # and glow effects should not compete with workflow progress.
        assert layout["headerBackground"] == "none", layout
        assert layout["headerBackgroundColor"] not in {"", "rgba(0, 0, 0, 0)"}, layout
        assert layout["avatarRadius"] != "50%", layout
        assert layout["listScrollHeight"] > layout["listClientHeight"], layout
        columns = page.locator(".capture-row").nth(1).evaluate(
            "el => Math.round(el.getBoundingClientRect().top) === Math.round(document.querySelector('.capture-row').getBoundingClientRect().top)"
        )
        assert columns is False, (width, columns)
        page.locator("#indeedCandidateList").evaluate("el => { el.scrollTop = el.scrollHeight; }")
        page.wait_for_timeout(30)
        last_visible = page.locator(".capture-row").last.evaluate(
            """el => {
              const list = document.querySelector('#indeedCandidateList').getBoundingClientRect();
              const row = el.getBoundingClientRect();
              return row.bottom <= list.bottom + 1 && row.top >= list.top - 1;
            }"""
        )
        assert last_visible, (width, layout)
        captured_layouts[str(width)] = layout

    # An unknown scan total is genuinely indeterminate: it must never expose
    # the former synthetic 8% as an accessible numeric value.
    page.evaluate(
        """() => {
          indeedScanState = { phase: 'scanning', found: 3, total: 0 };
          renderIndeedScanning();
        }"""
    )
    _assert_header_progress(
        page, kind="scan", mode="indeterminate", current=None, total=None
    )
    assert "3 found" in page.locator("#sourceHeaderStatus").inner_text()
    assert "total is still being determined" in (
        page.locator('[data-testid="source-progressbar"]').get_attribute("aria-valuetext") or ""
    )

    # Determinate scan progress uses candidate units, while the visual fill is
    # a derived percentage.
    page.evaluate(
        """() => {
          indeedScanState = { phase: 'scanning', found: 17, total: 50 };
          renderIndeedScanning();
        }"""
    )
    _assert_header_progress(page, kind="scan", mode="determinate", current=17, total=50)
    assert page.locator("#sourceHeaderProgressBar").evaluate("element => element.style.width") == "34%"
    assert "17 of 50" in page.locator("#sourceHeaderStatus").inner_text()

    # Lookup progress belongs to the same header surface. There must not be a
    # second body-level progressbar competing with it for visual or AT output.
    _seed_profiles(page, 2)
    page.evaluate(
        """() => {
          indeedLookupProfiles = indeedCandidates.slice();
          indeedLookupScope = new Set(indeedCandidates.map(profile => profile._selectionKey));
          indeedSelected = new Set(indeedCandidates.map(profile => profile._selectionKey));
          indeedLookupSummary = { total: 2, processed: 1, matched: 1, no_match: 0, errors: 0 };
          indeedLookupState = new Map(indeedCandidates.map((profile, index) => [
            profile._selectionKey,
            index === 0
              ? { status: 'found', emails: ['one@example.test'], phones: [] }
              : { status: 'looking_up', emails: [], phones: [] },
          ]));
          indeedScanState = { phase: 'lookup', found: 2, total: 2 };
          renderIndeedProfiles();
        }"""
    )
    _assert_header_progress(page, kind="lookup", mode="determinate", current=1, total=2)
    assert page.locator("#sourceHeaderProgressBar").evaluate("element => element.style.width") == "50%"
    assert "1 of 2" in page.locator("#sourceHeaderStatus").inner_text()
    assert "1 contact found" in (
        page.locator('[data-testid="source-progressbar"]').get_attribute("aria-valuetext") or ""
    )
    page.set_viewport_size({"width": 320, "height": 700})
    lookup_header_bounds = page.locator('[data-testid="source-header"]').evaluate(
        """header => {
          const progress = header.querySelector('[data-testid=source-progress]').getBoundingClientRect();
          const headerRect = header.getBoundingClientRect();
          return { left: progress.left, right: progress.right, headerLeft: headerRect.left, headerRight: headerRect.right };
        }"""
    )
    assert lookup_header_bounds["left"] >= lookup_header_bounds["headerLeft"] - 1
    assert lookup_header_bounds["right"] <= lookup_header_bounds["headerRight"] + 1

    _seed_profiles(page)
    summary = _seed_mixed_results(page)
    _assert_inactive_header_progress(page)
    assert page.locator("[role=tab]").count() == 4
    assert page.locator('[role=tab][data-filter="all"]').get_attribute("aria-selected") == "true"
    assert page.locator(".capture-row").count() == 50
    assert "Ready" in page.locator(".result-summary").inner_text()
    assert "Unavailable" in page.locator(".result-summary").inner_text()

    matched_tab = page.locator('[role=tab][data-filter="matched"]')
    matched_tab.click()
    assert page.locator(".capture-row").count() == summary["matched"]
    assert matched_tab.get_attribute("aria-selected") == "true"
    assert page.evaluate("() => document.activeElement?.dataset?.filter") == "matched"
    page.keyboard.press("ArrowRight")
    assert page.evaluate("() => document.activeElement?.dataset?.filter") == "no_match"
    assert page.locator('[role=tab][data-filter="no_match"]').get_attribute("aria-selected") == "true"
    assert page.locator(".capture-row").count() == summary["no_match"]

    results_layouts = {}
    page.locator('[role=tab][data-filter="all"]').click()
    for width, height in ((320, 700), (420, 760), (600, 900)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(50)
        layout = _layout(page)
        assert layout["rootScrollWidth"] <= width + 1, layout
        assert layout["bodyScrollWidth"] <= width + 1, layout
        assert layout["tooNarrowControls"] == 0, layout
        assert page.locator(".result-summary").evaluate("el => el.scrollWidth >= el.clientWidth")
        results_layouts[str(width)] = layout

    # The extension-side workflow must no longer render the old purple visual system.
    rendered_colors = page.evaluate(
        """() => [...document.querySelectorAll('[data-testid="source-header"], .capture-row, .capture-avatar, .summary-tile, .lookup-button')]
          .flatMap(element => {
            const style = getComputedStyle(element);
            return [style.color, style.backgroundColor, style.borderColor];
          })"""
    )
    forbidden = {"rgb(116, 50, 237)", "rgb(126, 52, 238)", "rgb(128, 53, 241)"}
    assert forbidden.isdisjoint(rendered_colors), rendered_colors

    browser.close()
    return {
        "profiles": 50,
        "summary": summary,
        "captured_widths": list(captured_layouts),
        "result_widths": list(results_layouts),
        "keyboard_filters": True,
        "search_filter": True,
    }


def main() -> None:
    global FRONTEND
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend", type=Path, default=FRONTEND)
    args = parser.parse_args()
    FRONTEND = args.frontend.resolve()
    available = [(name, path) for name, path in BROWSERS if path.exists()]
    assert available, "Chrome or Edge is required for the workbench UI smoke test."
    output = {}
    with sync_playwright() as playwright:
        for name, executable in available:
            output[name] = {
                "workbench": _run_browser(playwright.chromium, executable),
                "hosted_auth_gate": _run_hosted_auth_gate(playwright.chromium, executable),
            }
    print({"frontend_workbench": "passed", "browsers": output})


if __name__ == "__main__":
    main()
