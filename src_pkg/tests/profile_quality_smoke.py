"""Offline adversarial tests for the side-panel profile quality boundary.

This test executes the exact browser JavaScript used by the extension in both
Chrome and Edge.  It does not start the backend and cannot spend enrichment
credits.
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


FRONTEND = Path(__file__).parents[1] / "frontend"
BROWSERS = [
    ("chrome", Path("C:/Program Files/Google/Chrome/Application/chrome.exe")),
    ("edge", Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")),
]


def _exercise(browser_type, executable: Path) -> dict:
    browser = browser_type.launch(headless=True, executable_path=str(executable))
    page = browser.new_page()
    page.goto("about:blank")
    page.add_script_tag(path=str(FRONTEND / "profile-quality.js"))
    result = page.evaluate(
        r"""() => {
          const quality = globalThis.RadixsolProfileQuality;
          window.__radixsolXssExecuted = false;

          const linkedin = quality.sanitizeProfiles([
            {
              name: 'Elana Marsh, BSN, RN \u00b7 2nd',
              location: 'Philadelphia, Pennsylvania, United States \u00b7 Contact info',
              source: 'linkedin',
              source_id: 'elana-marsh',
              source_url: 'https://www.linkedin.com/in/elana-marsh/',
              roles: ['Registered Nurse', 'registered nurse'],
              notes: 'Role: Registered Nurse\nEmployer: Veterans Affairs'
            },
            {
              name: 'Elana Marsh, BSN, RN',
              location: 'Philadelphia, Pennsylvania, United States',
              source: 'linkedin',
              source_id: 'elana-marsh',
              source_url: 'https://www.linkedin.com/in/elana-marsh/',
              employers: ['Veterans Affairs'],
              schools: ['Drexel University']
            },
            {
              name: 'Recommended Jobs', location: 'Denver, CO', source: 'linkedin',
              source_url: 'https://www.linkedin.com/in/recommended-jobs/'
            },
            {
              name: '<img src=x onerror="window.__radixsolXssExecuted=true">',
              location: 'Austin, TX', source: 'linkedin',
              source_url: 'https://www.linkedin.com/in/unsafe/'
            },
            {
              name: 'Wrong Host', location: 'Miami, FL', source: 'linkedin',
              source_url: 'https://example.test/in/wrong-host/'
            },
            null,
          ], { platform: 'linkedin' });

          const indeed = quality.sanitizeProfiles([
            {
              name: 'Alice Morgan', location: 'Lives in Denver, CO', source: 'indeed',
              source_url: 'https://employers.indeed.com/smartsourcing?candidateId=alice'
            },
            {
              name: 'Alice Morgan', location: 'Denver, CO', source: 'indeed',
              source_url: 'https://employers.indeed.com/smartsourcing?candidateId=alice',
              roles: ['Clinical Nurse']
            },
            {
              name: 'Search Results', location: 'Search', source: 'indeed',
              source_url: 'https://employers.indeed.com/smartsourcing'
            },
          ], { platform: 'indeed' });

          const vivian = quality.sanitizeProfiles([
            {
              name: "Zo\u00eb O'Neil", location: 'Portland, OR', source: 'vivian',
              source_url: 'https://www.vivian.com/talent-pool/candidate/zoe'
            },
          ], { platform: 'vivian' });
          const facebook = quality.sanitizeProfiles([
            {
              name: 'Elizabeth Cruz (Liz)', location: 'Virginia Beach, VA', source: 'facebook',
              source_url: 'https://www.facebook.com/elizabeth.cruz'
            },
            {
              name: 'PrincessJulana Rubia (Intet)', location: 'Digos', source: 'facebook',
              source_url: 'https://www.facebook.com/people/PrincessJulana-Rubia/1000123456789/'
            },
          ], { platform: 'facebook', singleProfile: true });
          const usnews = quality.sanitizeProfiles([
            {
              name: 'Ryan E. Longman', location: 'New York, NY', source: 'usnews',
              source_id: '1689794356',
              source_url: 'https://health.usnews.com/doctors/ryan-longman-601896',
              roles: ['Physician'], employers: ['NewYork-Presbyterian Hospital'],
              notes: 'Specialty: Obstetrics & Gynecology\nNPI: 1689794356',
              profile_document: {
                kind: 'public_professional_profile',
                source_label: 'U.S. News Doctor Finder',
                source_url: 'https://health.usnews.com/doctors/ryan-longman-601896',
                specialties: ['Obstetrics & Gynecology'],
                education: ['Columbia University — Medical School'],
                licenses: ['NY State Medical License — Active'],
                npi: '1689794356'
              }
            },
            {
              name: 'Wrong Host', location: 'New York, NY', source: 'usnews',
              source_url: 'https://example.test/doctors/wrong-host-1'
            },
            {
              name: 'Wrong Section', location: 'New York, NY', source: 'usnews',
              source_url: 'https://health.usnews.com/best-hospitals/wrong-section'
            },
          ], { platform: 'usnews' });
          const sharecare = quality.sanitizeProfiles([
            {
              name: 'Raja Flores', location: 'New York, NY', source: 'sharecare',
              source_id: '1306821244',
              source_url: 'https://providers.sharecare.com/doctor/dr-raja-flores',
              specialty: 'Cardiothoracic Surgery', specialties: ['Cardiothoracic Surgery'],
              profile_document: {
                kind: 'public_professional_profile', source_label: 'Sharecare',
                source_url: 'https://providers.sharecare.com/doctor/dr-raja-flores',
                specialties: ['Cardiothoracic Surgery'],
                hospitals: ['Mount Sinai Morningside']
              }
            },
            {
              name: 'Wrong Sharecare Path', source: 'sharecare',
              source_url: 'https://providers.sharecare.com/find-a-doctor/search'
            }
          ], { platform: 'sharecare' });

          return {
            apiFrozen: Object.isFrozen(quality),
            linkedin,
            indeed,
            vivian,
            facebook,
            usnews,
            sharecare,
            facebookUrls: {
              profileId: quality.validProfileUrl(
                'https://www.facebook.com/profile.php?id=12345', 'facebook'),
              people: quality.validProfileUrl(
                'https://www.facebook.com/people/Jane-Doe/12345/', 'facebook'),
              groups: quality.validProfileUrl(
                'https://www.facebook.com/groups/nurses', 'facebook'),
              marketplace: quality.validProfileUrl(
                'https://www.facebook.com/marketplace', 'facebook'),
            },
            normalized: [
              quality.normalizeName('Silvia Lopez-Clarke, CST, BSN, RN'),
              quality.normalizeName('Rhonda Hampton (Rhonda Hampton)'),
              quality.normalizeName('Kathy Shaiken RN'),
            ],
            xssExecuted: window.__radixsolXssExecuted,
          };
        }"""
    )
    browser.close()
    return result


def main() -> None:
    available = [(name, path) for name, path in BROWSERS if path.exists()]
    assert available, "Chrome or Edge is required for the profile-quality smoke test."

    summaries = {}
    with sync_playwright() as playwright:
        for name, executable in available:
            result = _exercise(playwright.chromium, executable)
            assert result["apiFrozen"] is True, result
            assert result["xssExecuted"] is False, result

            linkedin = result["linkedin"]
            assert len(linkedin["profiles"]) == 1, result
            profile = linkedin["profiles"][0]
            assert profile["name"] == "Elana Marsh", result
            assert profile["location"] == "Philadelphia, Pennsylvania, United States", result
            assert profile["roles"] == ["Registered Nurse"], result
            assert profile["employers"] == ["Veterans Affairs"], result
            assert profile["schools"] == ["Drexel University"], result
            assert profile["notes"] == (
                "Role: Registered Nurse\nEmployer: Veterans Affairs"
            ), result
            assert linkedin["skipped"] == {
                "invalid": 1,
                "invalid_name": 2,
                "invalid_source": 1,
                "duplicate": 1,
            }, result

            indeed = result["indeed"]
            assert len(indeed["profiles"]) == 1, result
            assert indeed["profiles"][0]["location"] == "Denver, CO", result
            assert indeed["profiles"][0]["roles"] == ["Clinical Nurse"], result
            assert indeed["skipped"]["duplicate"] == 1, result
            assert indeed["skipped"]["invalid_name"] == 1, result

            assert [item["name"] for item in result["vivian"]["profiles"]] == [
                "Zoë O'Neil",
            ], result
            assert [item["name"] for item in result["facebook"]["profiles"]] == [
                "Elizabeth Cruz", "PrincessJulana Rubia",
            ], result
            assert len(result["usnews"]["profiles"]) == 1, result
            assert result["usnews"]["profiles"][0]["source_id"] == "1689794356", result
            assert result["usnews"]["profiles"][0]["profile_document"]["npi"] == "1689794356", result
            assert result["usnews"]["profiles"][0]["profile_document"]["education"] == [
                "Columbia University — Medical School"
            ], result
            assert result["usnews"]["skipped"]["invalid_source"] == 2, result
            assert len(result["sharecare"]["profiles"]) == 1, result
            assert result["sharecare"]["profiles"][0]["source"] == "sharecare", result
            assert result["sharecare"]["profiles"][0]["profile_document"]["source_label"] == "Sharecare", result
            assert result["sharecare"]["skipped"]["invalid_source"] == 1, result
            assert result["facebookUrls"] == {
                "profileId": True,
                "people": True,
                "groups": False,
                "marketplace": False,
            }, result
            assert result["normalized"] == [
                "Silvia Lopez-Clarke", "Rhonda Hampton", "Kathy Shaiken",
            ], result
            summaries[name] = {
                "accepted": len(linkedin["profiles"]) + len(indeed["profiles"])
                + len(result["vivian"]["profiles"]) + len(result["facebook"]["profiles"])
                + len(result["usnews"]["profiles"]) + len(result["sharecare"]["profiles"]),
                "skipped": linkedin["skippedCount"] + indeed["skippedCount"],
            }

    print({"profile_quality": "passed", "browsers": summaries})


if __name__ == "__main__":
    main()
