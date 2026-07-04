"""Headless drive of the sound-wave sync aligner (server must be running).
Logs in, stages a real gameplay+facecam pair via the saved-uploads dropdowns,
then checks: no JS errors, the waveform canvas loads, dragging the facecam lane
changes pv.offset, and 'Use this alignment' advances to the verify phase.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"
PW = open("fightsync-secret.txt", encoding="utf-8").read().strip()


def main():
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page()
        pg.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))
        pg.on("console", lambda m: errors.append("CONSOLE: " + m.text)
              if m.type == "error" and "favicon" not in m.text.lower()
              and "404" not in m.text else None)

        pg.goto(BASE + "/login", wait_until="domcontentloaded")
        pg.fill("input[name=password]", PW)
        pg.click("button, input[type=submit]")
        pg.wait_for_load_state("networkidle")
        assert pg.locator("#waveCanvas").count() == 1, "wave canvas missing from DOM"
        print("  logged in; #waveCanvas present")

        # pick a gameplay + facecam option from the saved-uploads dropdowns
        sels = pg.query_selector_all("select[data-saved]")
        staged = {}
        for s in sels:
            name = s.evaluate("e=>e.closest('.drop').dataset.name")
            opts = s.evaluate("e=>[...e.options].map(o=>o.value)")
            val = next((v for v in opts if v), None)
            if val and name in ("gameplay", "facecam"):
                s.select_option(value=val)
                staged[name] = val
        assert "gameplay" in staged and "facecam" in staged, f"could not stage pair: {staged}"
        print(f"  staged gameplay + facecam from library")

        # preview section + waveform should appear and load
        pg.wait_for_selector("#previewSection", state="visible", timeout=10000)
        pg.wait_for_function(
            "() => { const e=document.getElementById('waveOff');"
            "return e && /offset/.test(e.textContent) && !/loading/.test(e.textContent); }",
            timeout=30000)
        print("  waveform loaded:", pg.locator("#waveOff").text_content())

        # drag the lower (facecam) lane horizontally -> offset must change
        cv = pg.locator("#waveCanvas")
        cv.scroll_into_view_if_needed()
        box = cv.bounding_box()
        y_lower = box["y"] + box["height"] * 0.75
        x0 = box["x"] + box["width"] * 0.4
        pg.mouse.move(x0, y_lower)
        pg.mouse.down()
        pg.mouse.move(x0 + 120, y_lower, steps=8)
        pg.mouse.up()
        off_after = pg.locator("#waveOff").text_content()
        print("  after drag:", off_after)
        assert "+0.00s" not in off_after, "drag did not change offset"

        # 'Use this alignment' -> verify phase
        pg.click("#waveUse")
        pg.wait_for_selector("#syncResult", state="visible", timeout=5000)
        res = pg.locator("#syncResult").text_content()
        assert "sound-wave" in res, f"verify message unexpected: {res}"
        # mark-phase wave panel should now be hidden
        assert not pg.locator("#waveWrap").is_visible(), "wave panel still visible in verify"
        print("  entered verify:", res[:60].encode("ascii", "replace").decode(), "...")

        # ---- trim handles (verify phase) ----
        tcv = pg.locator("#trimCanvas")
        tcv.scroll_into_view_if_needed()
        tb = tcv.bounding_box()
        ymid = tb["y"] + tb["height"] / 2
        # grab the IN (left) handle and drag it ~30% inward
        pg.mouse.move(tb["x"] + 4, ymid)
        pg.mouse.down()
        pg.mouse.move(tb["x"] + tb["width"] * 0.3, ymid, steps=8)
        pg.mouse.up()
        trim_in = pg.evaluate("window._pv.trimIn")
        trim_dur = pg.evaluate("window._pv.dur")
        print(f"  trimIn after drag: {trim_in:.1f}s of {trim_dur:.1f}s composite")
        assert trim_in > 1.0, "trim IN handle did not move"
        read = pg.locator("#trimRead").text_content()
        assert "keep" in read.lower(), f"trim readout not updated: {read}"
        # reset restores full range
        pg.click("#trimReset")
        assert pg.evaluate("window._pv.trimIn") == 0, "Keep-all reset failed"
        print("  trim readout:", read.encode("ascii", "replace").decode(), "| reset OK")

        b.close()

    if errors:
        print("JS ERRORS:")
        for e in errors:
            print("   ", e)
        sys.exit(1)
    print("OK - sound-wave aligner works end to end, no JS errors")


if __name__ == "__main__":
    main()
