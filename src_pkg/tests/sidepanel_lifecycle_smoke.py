"""Offline side-panel lifecycle, race, sanitation, and responsive smoke test.

The production side-panel JavaScript runs with mocked Chrome APIs and a mocked
local backend.  No network or enrichment provider is contacted.  The same
scenario is exercised in installed Chrome and Edge engines.
"""
from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


FRONTEND = Path(__file__).parents[1] / "frontend"
BROWSERS = [
    ("chrome", Path("C:/Program Files/Google/Chrome/Application/chrome.exe")),
    ("edge", Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")),
]


MOCK_SCRIPT = r"""() => {
  const event = () => ({
    listeners: [],
    addListener(listener) { this.listeners.push(listener); },
    removeListener(listener) {
      this.listeners = this.listeners.filter((item) => item !== listener);
    },
  });
  const profile = (name, location, source, sourceId, sourceUrl, extra = {}) => ({
    name, location, source, source_id: sourceId, source_url: sourceUrl, ...extra,
  });
  const state = window.__panelTest = {
    activeId: 1,
    instanceId: `panel-${Date.now()}-${Math.random()}`,
    local: { medhuntProfileDataConsentV1: true },
    sentRuntime: [],
    scanCalls: {},
    fetchPaths: [],
    resumeRequests: [],
    pendingResumeCallbacks: [],
    tabs: {
      1: {
        id: 1, windowId: 10, active: true, status: 'complete',
        url: 'https://employers.indeed.com/smartsourcing?query=rn', delay: 5,
        response: {
          ok: true, expected_count: 5,
          profiles: [
            profile('Alice Morgan', 'Lives in Denver, CO', 'indeed', 'alice',
              'https://employers.indeed.com/smartsourcing?candidateId=alice',
              { roles: ['Clinical Nurse'] }),
            profile('Alice Morgan', 'Denver, CO', 'indeed', 'alice',
              'https://employers.indeed.com/smartsourcing?candidateId=alice',
              { employers: ['General Hospital'] }),
            profile('Recommended Jobs', 'Search', 'indeed', 'nav',
              'https://employers.indeed.com/smartsourcing'),
            profile('<img src=x onerror="window.__panelXss=true">', 'Austin, TX',
              'indeed', 'unsafe',
              'https://employers.indeed.com/smartsourcing?candidateId=unsafe'),
            profile('Wrong Host', 'Miami, FL', 'indeed', 'wrong',
              'https://example.test/profile/wrong'),
          ],
        },
      },
      2: {
        id: 2, windowId: 10, active: false, status: 'complete',
        url: 'https://www.linkedin.com/in/elana-marsh/', delay: 5,
        response: {
          ok: true, expected_count: 1,
          profiles: [profile(
            'Elana Marsh, BSN, RN \u00b7 2nd',
            'Philadelphia, Pennsylvania, United States \u00b7 Contact info',
            'linkedin', 'elana-marsh', 'https://www.linkedin.com/in/elana-marsh/',
            { roles: ['Registered Nurse'], employers: ['Veterans Affairs'] },
          )],
        },
      },
      3: {
        id: 3, windowId: 10, active: false, status: 'complete',
        url: 'https://www.linkedin.com/feed/', delay: 5,
        response: { ok: true, profiles: [] },
      },
      4: {
        id: 4, windowId: 10, active: false, status: 'complete',
        url: 'https://employers.indeed.com/smartsourcing?query=slow', delay: 850,
        response: {
          ok: true, expected_count: 1,
          profiles: [profile('Slow Candidate', 'Boston, MA', 'indeed', 'slow',
            'https://employers.indeed.com/smartsourcing?candidateId=slow')],
        },
      },
      5: {
        id: 5, windowId: 10, active: false, status: 'complete',
        url: 'https://www.linkedin.com/in/fast-candidate/', delay: 5,
        response: {
          ok: true, expected_count: 1,
          profiles: [profile('Fast Candidate', 'Seattle, WA', 'linkedin', 'fast-candidate',
            'https://www.linkedin.com/in/fast-candidate/')],
        },
      },
      6: {
        id: 6, windowId: 10, active: false, status: 'complete',
        url: 'https://employers.indeed.com/smartsourcing?query=resume-test', delay: 5,
        response: {
          ok: true, expected_count: 1,
          profiles: [profile('Riley Resume', 'Columbus, OH', 'indeed', 'riley-resume',
            'https://employers.indeed.com/smartsourcing?candidateId=riley-resume',
            { roles: ['Registered Nurse'] })],
        },
      },
      7: {
        id: 7, windowId: 10, active: false, status: 'complete',
        url: 'chrome://downloads/', delay: 0,
        response: { ok: true, profiles: [] },
      },
    },
  };
  window.__panelXss = false;
  window.__panelInstanceId = state.instanceId;

  const runtimeEvent = event();
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const responseFor = (tab) => ({
    ...clone(tab.response || { ok: true, profiles: [] }),
    page_url: tab.url,
    platform: tab.url.includes('linkedin.com') ? 'linkedin' : 'indeed',
  });

  window.chrome = {
    runtime: {
      lastError: null,
      onMessage: runtimeEvent,
      sendMessage(message, callback) {
        state.sentRuntime.push(clone(message));
        const response = message?.type === 'RADIXSOL_GET_PENDING_RESUME_EVENTS'
          ? { ok: true, events: [] }
          : { ok: true };
        queueMicrotask(() => callback?.(response));
      },
    },
    tabs: {
      async query(query) {
        if (query?.active) return state.tabs[state.activeId] ? [clone(state.tabs[state.activeId])] : [];
        return Object.values(state.tabs).map(clone);
      },
      async get(tabId) {
        if (!state.tabs[tabId]) throw new Error('No such tab');
        return clone(state.tabs[tabId]);
      },
      async update(tabId, changes) {
        if (!state.tabs[tabId]) throw new Error('No such tab');
        if (changes?.active) state.activate(tabId, false);
        Object.assign(state.tabs[tabId], changes || {});
        return clone(state.tabs[tabId]);
      },
      async create(options) { return { id: 99, ...options }; },
      sendMessage(tabId, message, callback) {
        const tab = state.tabs[tabId];
        const messageType = message?.original_type || message?.type;
        if (messageType === 'RADIXSOL_DOWNLOAD_INDEED_RESUME') {
          state.resumeRequests.push({ tabId, message: clone(message) });
          state.pendingResumeCallbacks.push(callback);
          return;
        }
        state.scanCalls[tabId] = (state.scanCalls[tabId] || 0) + 1;
        const delay = Number(tab?.delay) || 0;
        setTimeout(() => {
          if (!tab) {
            chrome.runtime.lastError = { message: 'No such tab' };
            callback(undefined);
            chrome.runtime.lastError = null;
            return;
          }
          callback(responseFor(tab));
        }, delay);
      },
    },
    scripting: { async executeScript() { return []; } },
    storage: {
      local: {
        get(keys, callback) {
          const output = {};
          for (const key of keys || []) {
            if (Object.prototype.hasOwnProperty.call(state.local, key)) output[key] = state.local[key];
          }
          queueMicrotask(() => callback(output));
        },
        set(values, callback) {
          Object.assign(state.local, values || {});
          queueMicrotask(() => callback?.());
        },
      },
    },
    downloads: { async download() { return 500; } },
  };

  state.emit = (message, sender = {}) => {
    for (const listener of [...runtimeEvent.listeners]) listener(message, sender, () => {});
  };
  state.activate = (tabId, emit = true) => {
    for (const tab of Object.values(state.tabs)) tab.active = Number(tab.id) === Number(tabId);
    state.activeId = Number(tabId);
    const tab = state.tabs[state.activeId];
    if (emit) state.emit({
      type: 'RADIXSOL_ACTIVE_TAB_CHANGED',
      tab_id: tab?.id || 0,
      window_id: tab?.windowId || 0,
      url: tab?.url || '',
      status: tab?.status || '',
      reason: 'activated',
    });
  };
  state.resolveNextResume = () => {
    const callback = state.pendingResumeCallbacks.shift();
    if (!callback) throw new Error('No pending resume request');
    callback({
      ok: true,
      adapter_revision: 'indeed-capture-v5',
      base64: 'JVBERi0xLjQKJSVFT0YK',
      contentType: 'application/pdf',
      candidate: 'Riley Resume',
    });
  };

  window.fetch = async (input, options = {}) => {
    const url = new URL(String(input), 'http://127.0.0.1');
    state.fetchPaths.push(url.pathname);
    let payload = {};
    if (url.pathname === '/health') {
      payload = { status: 'ok', mode: 'live', database: 'sqlite' };
    } else if (url.pathname === '/session') {
      payload = { status: 'ok' };
    } else if (url.pathname === '/jobs') {
      payload = [];
    } else if (url.pathname === '/candidates/import/batch') {
      const request = JSON.parse(options.body || '{}');
      const profiles = request.profiles || [];
      payload = {
        saved: profiles.length, imported: profiles.length, existing: 0,
        results: profiles.map((candidate, index) => ({
          id: 1000 + index,
          imported: true,
          candidate: { id: 1000 + index, enrich_status: 'pending', verification: {} },
        })),
      };
    } else if (url.pathname === '/contact-lookup/batch') {
      const request = JSON.parse(options.body || '{}');
      payload = {
        results: Object.fromEntries((request.candidate_ids || []).map((candidateId) => [
          String(candidateId),
          {
            status: 'found',
            emails: [],
            masked_emails: ['ri***********@example.test'],
            phones: ['+16145550123'],
            phone_contacts: [{ value: '+16145550123', kind: 'mobile' }],
            resume_required: true,
          },
        ])),
      };
    } else if (/^\/candidates\/\d+\/resume\/from-browser$/.test(url.pathname)) {
      payload = {
        attached: true,
        resume: {
          id: 9001,
          filename: 'Riley_Resume_resume.pdf',
          created: Date.now() / 1000,
        },
      };
    } else {
      return new Response(JSON.stringify({ detail: 'Unexpected offline test request' }), {
        status: 404, headers: { 'content-type': 'application/json' },
      });
    }
    return new Response(JSON.stringify(payload), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  };
}"""


def _wait_text(page: Page, text: str, timeout: int = 5_000) -> None:
    page.wait_for_function(
        "expected => document.body.innerText.includes(expected)", arg=text, timeout=timeout
    )


def _body_text(page: Page) -> str:
    return page.locator("body").inner_text()


def _run_browser(browser_type, executable: Path) -> dict:
    browser = browser_type.launch(headless=True, executable_path=str(executable))
    page = browser.new_page(viewport={"width": 420, "height": 760})

    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    index = re.sub(r"\s*<script\s+src=[^>]+></script>", "", index, flags=re.I)
    page.set_content(index)
    page.add_style_tag(path=str(FRONTEND / "styles.css"))
    page.evaluate(MOCK_SCRIPT)
    page.add_script_tag(path=str(FRONTEND / "profile-quality.js"))

    app_source = (FRONTEND / "app.js").read_text(encoding="utf-8")
    extension_check = (
        'const IS_EXTENSION = ["chrome-extension:", "moz-extension:"]'
        '.includes(location.protocol);'
    )
    assert extension_check in app_source, "The extension-mode test hook no longer matches app.js."
    app_source = app_source.replace(extension_check, "const IS_EXTENSION = true;", 1)
    page.add_script_tag(content=app_source)

    # Initial scan: valid card survives, duplicate/navigation/XSS/wrong-host
    # entries never reach the UI or automatic persistence.
    _wait_text(page, "Alice Morgan")
    initial_text = _body_text(page)
    assert "1 profiles ready" in initial_text, initial_text
    assert "4 irrelevant or duplicate profiles were skipped" in initial_text, initial_text
    assert page.locator(".capture-row").count() == 1, initial_text
    assert page.locator(".capture-identity strong").all_inner_texts() == ["Alice Morgan"]
    assert page.evaluate("() => window.__panelXss") is False
    assert page.locator(".source-brand-copy strong").inner_text() == "Medhunt"
    assert page.locator(".medhunt-mark").count() == 1
    assert page.locator(".radixsol-mark").count() == 0
    header_progress = page.locator('[data-testid="source-progress"]')
    header_progressbar = page.locator('[data-testid="source-progressbar"]')
    assert header_progress.get_attribute("data-mode") == "inactive"
    assert header_progress.get_attribute("aria-hidden") == "true"
    assert header_progressbar.get_attribute("role") is None
    instance_id = page.evaluate("() => window.__panelInstanceId")

    # Scan progress is rendered only in the persistent top header. Unknown
    # totals stay indeterminate instead of exposing a synthetic percentage.
    page.evaluate(
        """() => window.__panelTest.emit({
          type: 'RADIXSOL_PLATFORM_SCAN_PROGRESS', platform: 'indeed',
          found: 3, total: 0, preview: []
        }, { tab: { id: 1, windowId: 10, active: true } })"""
    )
    header_progress = page.locator('[data-testid="source-progress"]')
    assert header_progress.get_attribute("data-kind") == "scan"
    assert header_progress.get_attribute("data-mode") == "indeterminate"
    assert header_progress.get_attribute("aria-hidden") is None
    assert header_progressbar.get_attribute("role") == "progressbar"
    assert header_progressbar.get_attribute("aria-valuenow") is None
    assert page.locator('[role="progressbar"]').count() == 1
    assert "3 found" in page.locator("#sourceHeaderStatus").inner_text()

    page.evaluate(
        """() => window.__panelTest.emit({
          type: 'RADIXSOL_PLATFORM_SCAN_PROGRESS', platform: 'indeed',
          found: 17, total: 50, preview: []
        }, { tab: { id: 1, windowId: 10, active: true } })"""
    )
    assert header_progress.get_attribute("data-mode") == "determinate"
    assert header_progressbar.get_attribute("aria-valuemin") == "0"
    assert header_progressbar.get_attribute("aria-valuenow") == "17"
    assert header_progressbar.get_attribute("aria-valuemax") == "50"
    assert page.locator("#sourceHeaderProgressBar").evaluate(
        "element => element.style.width"
    ) == "34%"
    assert page.locator(".panel-rescan-button").is_disabled()

    # A completed refresh returns the reserved header strip to a non-ARIA
    # inactive state and restores the rescan control.
    page.evaluate("() => scanIndeedCandidates()")
    _wait_text(page, "Alice Morgan")
    assert header_progress.get_attribute("data-mode") == "inactive"
    assert header_progress.get_attribute("aria-hidden") == "true"
    assert header_progressbar.get_attribute("role") is None
    assert page.locator(".panel-rescan-button").is_enabled()

    # One persistent panel follows a different supported platform tab without
    # a close/reopen or a manual refresh.
    page.evaluate("() => window.__panelTest.activate(2)")
    _wait_text(page, "Elana Marsh")
    linkedin_text = _body_text(page)
    assert "Alice Morgan" not in linkedin_text, linkedin_text
    assert page.locator(".capture-identity strong").all_inner_texts() == ["Elana Marsh"]
    assert page.locator(".active-page-indicator").inner_text() == "LinkedIn"
    assert page.locator('[data-testid="source-progress"]').get_attribute("role") is None
    assert page.evaluate("() => window.__panelInstanceId") == instance_id

    # A same-platform event from an inactive tab must not mutate the current
    # list or schedule a delayed rescan.
    calls_before = page.evaluate(
        "() => Object.values(window.__panelTest.scanCalls).reduce((a, b) => a + b, 0)"
    )
    page.evaluate(
        """() => window.__panelTest.emit({
          type: 'RADIXSOL_PLATFORM_RESULTS_CHANGED', platform: 'linkedin', count: 0,
          page_url: 'https://www.linkedin.com/in/inactive-person/'
        }, { tab: { id: 5, windowId: 10 } })"""
    )
    page.wait_for_timeout(1_050)
    assert "Elana Marsh" in _body_text(page)
    calls_after = page.evaluate(
        "() => Object.values(window.__panelTest.scanCalls).reduce((a, b) => a + b, 0)"
    )
    assert calls_after == calls_before, (calls_before, calls_after)

    # Unsupported sub-routes clear stale cards and explain what the recruiter
    # should open next.
    page.evaluate("() => window.__panelTest.activate(3)")
    _wait_text(page, "No candidate profile on this page")
    unsupported_text = _body_text(page)
    assert "Elana Marsh" not in unsupported_text, unsupported_text
    assert page.locator(".capture-row").count() == 0
    assert "Open a LinkedIn People search or an individual profile" in unsupported_text

    # Force a real overlapping scan. The stale, slow response must lose to the
    # latest active-tab context.
    page.evaluate("() => window.__panelTest.activate(4)")
    page.wait_for_timeout(240)
    page.evaluate("() => window.__panelTest.activate(5)")
    _wait_text(page, "Fast Candidate", timeout=5_000)
    page.wait_for_timeout(1_050)
    race_text = _body_text(page)
    assert "Fast Candidate" in race_text, race_text
    assert "Slow Candidate" not in race_text, race_text
    assert page.locator(".active-page-indicator").inner_text() == "LinkedIn"

    # Side-panel layouts vary substantially between browsers and user zoom.
    # These widths cover compact, ordinary, and wide side-panel sizes.
    measurements = {}
    for width in (320, 420, 600):
      page.set_viewport_size({"width": width, "height": 700})
      page.wait_for_timeout(60)
      measurement = page.evaluate(
          """() => {
            const root = document.documentElement;
            const workflow = document.querySelector('.indeed-workflow');
            const lookup = document.querySelector('.lookup-button');
            const rescan = document.querySelector('.panel-rescan-button');
            const rect = workflow?.getBoundingClientRect();
            return {
              viewport: innerWidth,
              rootScrollWidth: root.scrollWidth,
              bodyScrollWidth: document.body.scrollWidth,
              workflowLeft: rect?.left ?? -99,
              workflowRight: rect?.right ?? 9999,
              lookupHeight: lookup?.getBoundingClientRect().height || 0,
              rescanWidth: rescan?.getBoundingClientRect().width || 0,
              visibleRows: document.querySelectorAll('.capture-row').length,
            };
          }"""
      )
      assert measurement["rootScrollWidth"] <= width + 1, measurement
      assert measurement["bodyScrollWidth"] <= width + 1, measurement
      assert measurement["workflowLeft"] >= -1, measurement
      assert measurement["workflowRight"] <= width + 1, measurement
      assert measurement["lookupHeight"] >= 44, measurement
      assert measurement["rescanWidth"] >= 36, measurement
      assert measurement["visibleRows"] == 1, measurement
      measurements[str(width)] = measurement

    # Automatic Indeed resume capture is deliberately slower than contact
    # lookup.  While it is pending, Indeed can emit same-tab navigation and
    # zero-result signals as it opens the exact profile/download controls.
    # Those transient events must not replace the completed result workbench.
    page.set_viewport_size({"width": 420, "height": 760})
    page.evaluate("() => window.__panelTest.activate(6)")
    _wait_text(page, "Riley Resume")
    checkbox = page.locator(".indeed-select")
    assert checkbox.count() == 1
    if not checkbox.is_checked():
        checkbox.check()
    page.evaluate("() => { window.confirm = () => true; }")
    page.locator('[data-action="lookup-indeed"]').click()
    page.wait_for_function(
        "() => window.__panelTest.pendingResumeCallbacks.length === 1",
        timeout=5_000,
    )

    baseline_names = page.locator(".capture-identity strong").all_inner_texts()
    assert baseline_names == ["Riley Resume"], (baseline_names, _body_text(page))
    assert page.locator(".result-workbench-head").count() == 1, _body_text(page)
    assert "+16145550123" in page.locator(".capture-result").inner_text()
    assert page.locator('[data-testid="source-header"]').get_attribute(
        "data-progress-kind"
    ) == "resume"
    assert page.locator('[data-testid="source-progressbar"]').get_attribute(
        "role"
    ) == "progressbar"
    resume_scan_calls = page.evaluate("() => window.__panelTest.scanCalls[6] || 0")

    # Chrome's toolbar Downloads bubble emits a duplicate focus/activation
    # context, and the full Downloads page temporarily becomes the active tab.
    # Both must leave the running resume workflow and its results untouched.
    page.evaluate(
        """() => {
          const state = window.__panelTest;
          const tab = state.tabs[6];
          state.emit({
            type: 'RADIXSOL_ACTIVE_TAB_CHANGED', tab_id: 6, window_id: 10,
            url: tab.url, status: 'complete', platform: 'indeed',
            reason: 'window-focused'
          });
          state.activate(7);
        }"""
    )
    page.wait_for_timeout(400)
    assert page.locator(".capture-identity strong").all_inner_texts() == baseline_names
    assert page.locator(".result-workbench-head").count() == 1
    assert page.evaluate("() => window.__panelTest.scanCalls[6] || 0") == resume_scan_calls
    assert page.evaluate("() => window.__panelTest.scanCalls[7] || 0") == 0

    page.evaluate(
        """() => {
          const state = window.__panelTest;
          const tab = state.tabs[6];
          state.emit({
            type: 'RADIXSOL_ACTIVE_TAB_CHANGED', tab_id: 6, window_id: 10,
            url: tab.url, status: 'complete', reason: 'loaded'
          });
          state.emit({
            type: 'RADIXSOL_PLATFORM_SCAN_PROGRESS', platform: 'indeed',
            found: 0, total: 0, preview: []
          }, { tab: { id: 6, windowId: 10, active: true } });
          state.emit({
            type: 'RADIXSOL_PLATFORM_RESULTS_CHANGED', platform: 'indeed', count: 0,
            page_url: tab.url
          }, { tab: { id: 6, windowId: 10, active: true } });
        }"""
    )
    page.wait_for_timeout(1_100)

    resume_text = _body_text(page)
    assert page.locator(".capture-identity strong").all_inner_texts() == baseline_names
    assert page.locator(".capture-row").count() == 1
    assert page.locator(".result-workbench-head").count() == 1
    assert page.locator(".capture-result").count() == 1
    assert "+16145550123" in page.locator(".capture-result").inner_text()
    assert "Resume in progress" in page.locator(".capture-result").inner_text()
    assert page.locator(".source-status").count() == 0
    assert page.locator(".scan-card").count() == 0
    assert "Loading candidate page" not in resume_text
    assert "No profiles found" not in resume_text
    assert page.locator('[data-testid="source-header"]').get_attribute(
        "data-progress-kind"
    ) == "resume"
    assert page.locator('[data-action="open-indeed-result"]').is_disabled()
    assert page.locator('[data-action="refresh-indeed"].inline-scan-action').is_disabled()
    assert page.evaluate("() => window.__panelTest.scanCalls[6] || 0") == resume_scan_calls

    page.evaluate("() => window.__panelTest.resolveNextResume()")
    _wait_text(page, "Open resume", timeout=5_000)
    assert page.locator(".capture-identity strong").all_inner_texts() == baseline_names
    assert page.locator(".result-workbench-head").count() == 1
    assert page.locator(".source-status").count() == 0

    # A Chrome download can remain in progress after the central resume is
    # stored. Closing chrome://downloads reports the source tab with reason
    # "removed"; Indeed may also leave a transient candidate drawer parameter.
    # Neither condition is a request to discard and rebuild the workbench.
    page.evaluate(
        """() => {
          const state = window.__panelTest;
          const tab = state.tabs[6];
          tab.url += '&candidateId=riley-resume&drawer=resume';
          state.activate(6, false);
          indeedResumeNavigationGrace = { sourceTabId: 0, sourcePageUrl: '', until: 0 };
          state.emit({
            type: 'RADIXSOL_ACTIVE_TAB_CHANGED', tab_id: 6, window_id: 10,
            url: tab.url, status: 'complete', platform: 'indeed', reason: 'removed'
          });
        }"""
    )
    page.wait_for_timeout(400)
    assert page.locator(".capture-identity strong").all_inner_texts() == baseline_names
    assert page.locator(".result-workbench-head").count() == 1
    assert page.evaluate("() => window.__panelTest.scanCalls[6] || 0") == resume_scan_calls
    page.evaluate("() => window.__panelTest.activate(6)")
    page.wait_for_timeout(400)
    assert page.locator(".capture-identity strong").all_inner_texts() == baseline_names
    assert page.locator(".result-workbench-head").count() == 1
    assert page.evaluate("() => window.__panelTest.scanCalls[6] || 0") == resume_scan_calls
    page.evaluate(
        """() => {
          const state = window.__panelTest;
          const tab = state.tabs[6];
          state.emit({
            type: 'RADIXSOL_ACTIVE_TAB_CHANGED', tab_id: 6, window_id: 10,
            url: tab.url, status: 'complete', platform: 'indeed',
            reason: 'window-focused'
          });
        }"""
    )
    page.wait_for_timeout(400)
    assert page.locator(".capture-identity strong").all_inner_texts() == baseline_names
    assert page.locator(".result-workbench-head").count() == 1
    assert page.evaluate("() => window.__panelTest.scanCalls[6] || 0") == resume_scan_calls

    # The short post-download protection is scoped to the original search.
    # A genuine new search in the same Indeed tab must replace the old result
    # workbench instead of being mistaken for download-drawer navigation.
    page.evaluate(
        """() => {
          const state = window.__panelTest;
          const tab = state.tabs[6];
          tab.url = 'https://employers.indeed.com/smartsourcing?query=manual-empty';
          tab.response = { ok: true, profiles: [] };
          state.emit({
            type: 'RADIXSOL_ACTIVE_TAB_CHANGED', tab_id: 6, window_id: 10,
            url: tab.url, status: 'complete', platform: 'indeed', reason: 'navigated'
          });
        }"""
    )
    _wait_text(page, "No candidate profiles found", timeout=5_000)
    assert page.locator(".capture-row").count() == 0

    # Guard the no-provider-call promise of this test itself.
    fetch_paths = page.evaluate("() => window.__panelTest.fetchPaths")
    assert fetch_paths.count("/contact-lookup/batch") == 1, fetch_paths
    assert any(re.fullmatch(r"/candidates/\d+/resume/from-browser", path) for path in fetch_paths), fetch_paths
    assert not any("enrich" in path for path in fetch_paths), fetch_paths
    scan_calls = page.evaluate("() => window.__panelTest.scanCalls")
    browser.close()
    return {
        "supported_switch": True,
        "unsupported_cleared": True,
        "inactive_ignored": True,
        "race_last_context_won": True,
        "resume_events_preserved_results": True,
        "scan_calls": scan_calls,
        "widths": list(measurements),
    }


def main() -> None:
    available = [(name, path) for name, path in BROWSERS if path.exists()]
    assert available, "Chrome or Edge is required for the side-panel lifecycle smoke test."
    results = {}
    with sync_playwright() as playwright:
        for name, executable in available:
            results[name] = _run_browser(playwright.chromium, executable)
    print({"sidepanel_lifecycle": "passed", "browsers": results})


if __name__ == "__main__":
    main()
