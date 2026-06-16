"""Headless browser smoke test (uses installed Chrome, no browser download).
Verifies the page initializes without JS errors and the channel row renders +
live-polls. Requires the server running on 127.0.0.1:8765.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"


def main():
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" and "favicon" not in m.text.lower()
                and "404" not in m.text else None)

        page.goto(BASE, wait_until="networkidle")
        # core sections present
        assert page.locator("#channelsSection").count() == 1, "channels section missing"
        assert page.locator("#dropG").count() == 1
        assert page.locator("#cropModal").count() == 1
        print("  page loaded, sections present")

        # add a reliably-live channel through the UI and watch the badge update
        page.fill("#chanUrl", "https://www.youtube.com/@LofiGirl")
        page.click("#chanAdd")
        page.wait_for_selector(".chan", timeout=15000)
        # wait for live poll to resolve the badge
        page.wait_for_function(
            "() => { const b=document.querySelector('.chan [data-badge]');"
            "return b && (b.textContent==='live' || b.textContent==='offline'); }",
            timeout=30000,
        )
        badge = page.locator(".chan [data-badge]").first.text_content()
        cap_disabled = page.locator(".chan [data-cap]").first.is_disabled()
        print(f"  channel rendered; badge='{badge}', capture disabled={cap_disabled}")
        assert badge == "live", f"expected live badge, got '{badge}'"
        assert cap_disabled is False, "capture button should be enabled when live"

        # clean up the test channel via its × button
        page.click(".chan [data-x]")
        page.wait_for_timeout(800)
        browser.close()

    if errors:
        print("  JS ERRORS:")
        for e in errors:
            print("   -", e)
        sys.exit("UI TEST FAILED: console/page errors")
    print("UI SMOKE TEST PASSED (no JS errors, live badge + capture enabled)")


if __name__ == "__main__":
    main()
