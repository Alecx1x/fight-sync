"""Headless test of the new 'Choose video file' button (server must be running).
Clicks the button, picks a real file via the OS chooser, and confirms it uploads
and appears as a gameplay item — i.e. no path typing needed.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from media import FFMPEG

BASE = "http://127.0.0.1:8765"
PW = open("fightsync-secret.txt", encoding="utf-8").read().strip()


def main():
    T = Path(tempfile.mkdtemp(prefix="fs_up_"))
    clip = T / "mygameplay.mp4"
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(clip),
    ], check=True)

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

        assert pg.locator("#dropG [data-browse]").count() == 1, "Choose-file button missing"
        # click the button -> OS file chooser opens -> pick our file
        with pg.expect_file_chooser() as fc_info:
            pg.click("#dropG [data-browse]")
        fc_info.value.set_files(str(clip))

        # it should upload and show up as a gameplay item
        pg.wait_for_function(
            "() => window._state && window._state.gameplay.length === 1", timeout=20000)
        item = pg.evaluate("window._state.gameplay[0]")
        print(f"  uploaded + listed: name='{item['name']}', path under recordings: "
              f"{'recordings' in item['path']}")
        assert "recordings" in item["path"], f"file not staged server-side: {item['path']}"
        assert pg.locator("#dropG [data-items] li").count() == 1, "item row not rendered"
        b.close()

    if errors:
        print("JS ERRORS:")
        for e in errors:
            print("   ", e)
        sys.exit(1)
    print("OK - Choose-file button uploads a picked video (no path typing)")


if __name__ == "__main__":
    main()
