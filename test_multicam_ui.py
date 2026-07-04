"""Headless multicam UI flow (server running). Uploads gameplay + primary
facecam + a second angle via the file pickers, confirms the angles section +
list, syncs the angle (waveform -> Use -> Done), syncs primary, adds cuts in
verify, and checks the multicam state is populated. No JS errors.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from media import FFMPEG

BASE = "http://127.0.0.1:8765"
PW = open("fightsync-secret.txt", encoding="utf-8").read().strip()


def build(mp4, src, freq):
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"{src}=size=320x240:rate=30:duration=8",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(mp4),
    ], check=True)


def upload(pg, tile_sel, path):
    with pg.expect_file_chooser() as fc:
        pg.click(f"{tile_sel} [data-browse]")
    fc.value.set_files(str(path))


def wave_use_and_verify(pg):
    """Drag the facecam waveform a bit and click 'Use this alignment'."""
    pg.wait_for_function(
        "() => { const e=document.getElementById('waveOff');"
        "return e && /offset/.test(e.textContent) && !/loading/.test(e.textContent); }",
        timeout=30000)
    cv = pg.locator("#waveCanvas"); cv.scroll_into_view_if_needed()
    box = cv.bounding_box(); y = box["y"] + box["height"] * 0.75
    x0 = box["x"] + box["width"] * 0.4
    pg.mouse.move(x0, y); pg.mouse.down(); pg.mouse.move(x0 + 60, y, steps=6); pg.mouse.up()
    pg.click("#waveUse")
    pg.wait_for_selector("#syncResult", state="visible", timeout=5000)


def main():
    T = Path(tempfile.mkdtemp(prefix="fs_mcui_"))
    build(T / "g.mp4", "testsrc", 300)
    build(T / "f.mp4", "testsrc2", 300)
    build(T / "a.mp4", "smptebars", 500)

    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page()
        pg.on("pageerror", lambda e: errors.append("PAGEERROR: " + str(e)))
        pg.on("console", lambda m: errors.append("CONSOLE: " + m.text)
              if m.type == "error" and "favicon" not in m.text.lower()
              and "404" not in m.text else None)
        pg.goto(BASE + "/login", wait_until="domcontentloaded")
        pg.fill("input[name=password]", PW); pg.click("button, input[type=submit]")
        pg.wait_for_load_state("networkidle")

        upload(pg, "#dropG", T / "g.mp4")
        upload(pg, "#dropF", T / "f.mp4")
        pg.wait_for_function("() => window._state.gameplay.length===1 && window._state.facecam.length===1")
        pg.wait_for_selector("#multicamSection", state="visible", timeout=10000)
        print("  primary pair staged; multicam section visible")

        # add a 2nd angle
        with pg.expect_file_chooser() as fc:
            pg.click("#addAngle")
        fc.value.set_files(str(T / "a.mp4"))
        pg.wait_for_function("() => window._state.angles.length===1", timeout=15000)
        print("  angle added:", pg.evaluate("window._state.angles[0].name"))

        # sync the angle: click its Sync, align, Use, Done
        pg.click("#angleList li:nth-child(1) .anglesync")
        wave_use_and_verify(pg)
        assert pg.locator("#btnDoneAngle").is_visible(), "Done-with-angle button not shown"
        pg.click("#btnDoneAngle")
        pg.wait_for_function("() => window._state.angles[0].synced === true", timeout=5000)
        ang_off = pg.evaluate("window._state.angles[0].offset")
        print(f"  angle synced, offset={ang_off:.2f}s")

        # sync the primary (back to main) + enter verify
        wave_use_and_verify(pg)
        pg.wait_for_selector("#cutBlock", state="visible", timeout=5000)

        # scrub master to ~middle, add a cut to the angle, then back to main
        pg.evaluate("masterSeek((pv.dur||10)*0.5)")
        # cut buttons: 'Main' then 'Angle 2'
        btns = pg.locator("#cutButtons button")
        assert btns.count() == 2, f"expected 2 cut buttons, got {btns.count()}"
        btns.nth(1).click()                       # cut to the angle
        pg.evaluate("masterSeek((pv.dur||10)*0.7)")
        btns.nth(0).click()                       # cut back to main
        cuts = pg.evaluate("window._state.cuts")
        print(f"  cuts: {[(round(c['t'],1), c['angle']) for c in cuts]}")
        assert len(cuts) == 2 and cuts[0]["angle"] == 1 and cuts[1]["angle"] == 0, cuts
        b.close()

    if errors:
        print("JS ERRORS:")
        for e in errors: print("   ", e)
        sys.exit(1)
    print("OK - multicam UI: upload angle, per-angle sync, cuts all work")


if __name__ == "__main__":
    main()
