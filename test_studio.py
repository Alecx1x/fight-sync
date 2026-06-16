"""Form Studio tests: backend track + burn-in render over HTTP, and a real
browser test that draws a marker on the canvas. Requires the server running.
"""
import subprocess
import time
import urllib.parse
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from media import FFMPEG, probe

T = Path(__file__).parent / "_test"
T.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8765"


def build_base():
    out = T / "studio_base.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4",
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(out)],
        check=True,
    )
    return str(out)


def api_tests(base):
    s = requests.Session(); s.trust_env = False
    s.post(f"{BASE}/login", data={"password": open("fightsync-secret.txt").read().strip(), "next": "/"})
    print("== STUDIO BACKEND (track + render) ==")
    fd = {"path": base, "start": "0.3", "dur": "2.0",
          "nx": "0.4", "ny": "0.4", "nw": "0.2", "nh": "0.2"}
    track = s.post(f"{BASE}/api/studio/track", data=fd).json()["track"]
    print(f"  track points: {len(track)}")
    assert len(track) > 1, "track too short"

    import json
    anns = [
        {"type": "circle", "color": "red", "nx": .5, "ny": .5, "nr": .15,
         "start": 0.0, "end": 4.0, "label": "GUARD"},
        {"type": "zone", "color": "green", "nx": .4, "ny": .4, "nw": .2, "nh": .2,
         "start": 0.3, "end": 2.3, "track": track},
    ]
    r = s.post(f"{BASE}/api/studio/render",
               data={"path": base, "annotations": json.dumps(anns)})
    jid = r.json()["job_id"]
    for _ in range(120):
        st = s.get(f"{BASE}/api/status/{jid}").json()
        if st["status"] in ("done", "error"):
            break
        time.sleep(1)
    assert st["status"] == "done", f"render failed: {st.get('message')}"
    d = s.get(f"{BASE}/api/download/{jid}")
    out = T / "studio_out.mp4"
    out.write_bytes(d.content)
    info = probe(str(out))
    print(f"  exported: {info.width}x{info.height}, {info.duration:.1f}s, {len(d.content)} bytes")
    assert info.width == 640 and info.height == 360
    print("  PASS - tracked + static markers burned in via studio API\n")


def browser_test(base):
    print("== STUDIO PAGE (browser) ==")
    errors = []
    url = f"{BASE}/studio?path=" + urllib.parse.quote(base)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text)
              if m.type == "error" and "favicon" not in m.text.lower() else None)
        pg.goto(BASE, wait_until="networkidle")    # log in first (auth gate)
        pg.fill("input[name=password]", open("fightsync-secret.txt").read().strip())
        pg.click("button"); pg.wait_for_load_state("networkidle")
        pg.goto(url, wait_until="networkidle")
        pg.wait_for_function("() => document.getElementById('vid').readyState >= 1", timeout=15000)
        assert pg.locator("#tools").count() == 1

        # select the circle tool and draw a circle on the canvas
        pg.click("button[data-tool='circle']")
        tool = pg.evaluate("window._fs.state.tool")
        ov = pg.locator("#ov").bounding_box()
        print(f"  tool after click: {tool}; canvas box: "
              f"{round(ov['width'])}x{round(ov['height'])} at "
              f"{round(ov['x'])},{round(ov['y'])}")
        cx, cy = ov["x"] + ov["width"] / 2, ov["y"] + ov["height"] / 2
        pg.mouse.move(cx, cy); pg.mouse.down()
        pg.mouse.move(cx + 50, cy + 10); pg.mouse.up()
        pg.wait_for_timeout(400)
        nmk = pg.evaluate("window._fs.state.markers.length")
        rows = pg.locator(".mrow").count()
        print(f"  markers after drawing a circle: state={nmk}, rows={rows}")
        assert nmk == 1, "drawing a circle did not create a marker"
        b.close()
    if errors:
        print("  JS ERRORS:", errors)
        raise SystemExit("studio page had JS errors")
    print("  PASS - studio page loads, tool draws a marker, no JS errors\n")


def main():
    base = build_base()
    api_tests(base)
    browser_test(base)
    print("STUDIO TESTS PASSED")


if __name__ == "__main__":
    main()
