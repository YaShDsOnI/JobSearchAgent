"""Browser smoke test of the candidate workspace (run by `python tools/ci.py`, stage ui-smoke).

    python tools/ui_smoke.py --base http://127.0.0.1:4173 --api http://127.0.0.1:8000 --out DIR

Drives the main flow with headless Chromium against a running, freshly seeded deployment:
sign up, upload a CV, accept suggested facts, add work authorization, set the location
ladder, turn the agent on, match, open a job, draft and approve a CV, prepare the review
package, and visit every view at desktop and phone widths. Fails on any uncaught page error
or a missing screen. Screenshots go to --out. Uses only sanitized, invented data.
"""

import argparse
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

CV_TEXT = (
    "Asha Example\nasha.example@example.test | +91 98765 43210\n"
    "Data analyst with 3 years of experience with SQL.\nSkills: SQL, Python, Excel, Tableau\n"
    "B.Tech in Computer Science, Example Institute of Technology\n"
)
VIEWS = ("overview", "jobs", "profile", "preferences", "cvs", "applications", "tracker", "sources")


def wait_for(url: str, seconds: int = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except OSError:  # Connection refused, timeout, or HTTP error while starting.
            time.sleep(0.5)
    raise SystemExit(f"not reachable: {url}")


def shot(page: Page, out: Path, name: str) -> None:
    page.screenshot(path=str(out / f"{name}.png"), full_page=True)


def run(base: str, out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    cv = out / "cv.txt"
    cv.write_text(CV_TEXT, encoding="utf-8")
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.goto(base)
        page.get_by_role("button", name="Create a new account").click()
        page.get_by_label("Email").fill(f"ui-smoke-{int(time.time())}@example.test")
        page.get_by_label("Password").fill("ui-smoke-password-123456")
        page.get_by_role("button", name="Create account").click()
        page.get_by_role("heading", name="Overview").wait_for()
        shot(page, out, "01-overview")

        page.get_by_role("link", name="Profile").click()
        page.locator("input[type=file]").set_input_files(str(cv))
        page.get_by_text("Suggested facts").wait_for()
        while page.get_by_role("button", name="This is true").count():
            page.get_by_role("button", name="This is true").first.click()
        page.get_by_role("combobox", name="Fact type to add").select_option("work_authorization")
        page.get_by_role("button", name="Add fact").click()
        card = page.locator("fieldset").last
        card.get_by_label("Countries (ISO codes, comma-separated)").fill("IN")
        card.get_by_label("Authorization status").select_option("authorized")
        page.get_by_role("button", name="Save new version").click()
        page.get_by_text("Saved a new profile version").wait_for()
        shot(page, out, "02-profile")

        page.get_by_role("link", name="Preferences").click()
        page.get_by_label("Target roles (comma-separated)").fill("Data Analyst")
        page.get_by_label("Add a city").fill("Vadodara")
        page.get_by_role("button", name="Add city").click()
        page.get_by_role("button", name="Save preferences").click()
        page.get_by_text("Saved. Matches will be refreshed").wait_for()
        shot(page, out, "03-preferences")

        page.get_by_role("link", name="Overview").click()
        page.get_by_role("button", name="Turn the agent on").click()
        page.get_by_role("button", name="Turn the agent off").wait_for()
        page.get_by_role("button", name="Match now").click()
        page.get_by_text("Matched").wait_for()
        shot(page, out, "04-overview-matched")

        page.get_by_role("link", name="Jobs").click()
        page.locator(".job-row").first.wait_for()
        page.locator(".job-row").first.click()
        page.get_by_role("heading", name="Eligibility").wait_for()
        shot(page, out, "05-job")
        page.get_by_role("button", name="Draft a factual CV").click()
        page.get_by_text("Check every claim before approving").wait_for()
        page.get_by_role("button", name="Approve this CV").click()
        page.get_by_role("button", name="Prepare the application").click()
        page.get_by_text("Application review package").wait_for()
        shot(page, out, "06-application")

        for width, label in ((1280, "desktop"), (390, "phone")):
            page.set_viewport_size({"width": width, "height": 844})
            for view in VIEWS:
                page.goto(f"{base}/#/{view}")
                page.locator("main h1").wait_for()
                page.wait_for_load_state("networkidle")
                shot(page, out, f"view-{label}-{view}")
        browser.close()
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--api", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    wait_for(f"{args.api}/api/v1/health/live")
    wait_for(args.base)
    errors = run(args.base.rstrip("/"), args.out)
    if errors:
        print("\n".join(errors))
        return 1
    print(f"ui smoke passed; screenshots in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
