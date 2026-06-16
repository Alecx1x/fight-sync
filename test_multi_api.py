"""End-to-end multi-clip through the API + a browser check of the new UI:
stage multiple gameplay/facecam clips, render with bell transitions, verify, and
confirm the multi-file tiles + transition controls work in a real browser.
"""
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from media import probe
import test_multi as tm

BASE = "http://127.0.0.1:8765"


def login(s):
    pw = open("fightsync-secret.txt").read().strip()
    s.post(f"{BASE}/login", data={"password": pw, "next": "/"})


def stage(s, path, name):
    with open(path, "rb") as fh:
        r = s.post(f"{BASE}/api/upload",
                   files={"file": (Path(path).name, fh, "video/mp4")},
                   data={"name": name})
    return r.json()["path"]


def main():
    g0, f0 = tm.make_pair(0, 5.0)
    g1, f1 = tm.make_pair(1, 3.0)
    s = requests.Session(); s.trust_env = False
    login(s)

    print("== MULTI-CLIP VIA API ==")
    gps = [stage(s, g0, "gameplay"), stage(s, g1, "gameplay")]
    fps = [stage(s, f0, "facecam"), stage(s, f1, "facecam")]
    import json
    r = s.post(f"{BASE}/api/render", data={
        "gameplay_paths_json": json.dumps(gps),
        "facecam_paths_json": json.dumps(fps),
        "make_subtitles": "false", "intro": "false", "outro": "false",
        "transitions": "true", "transition_label": "ROUND", "bell": "true"})
    jid = r.json()["job_id"]
    for _ in range(180):
        st = s.get(f"{BASE}/api/status/{jid}").json()
        if st["status"] in ("done", "error"):
            break
        time.sleep(1)
    assert st["status"] == "done", f"render failed: {st.get('message')}"
    res = st["result"]
    print(f"  clips={res['clips']} offsets={[x['offset'] for x in res['segments']]}")
    assert res["clips"] == 2
    assert abs(res["segments"][0]["offset"] - 5.0) < 0.1
    assert abs(res["segments"][1]["offset"] - 3.0) < 0.1
    d = s.get(f"{BASE}/api/download/{jid}")
    out = tm.T / "api_multi.mp4"; out.write_bytes(d.content)
    print(f"  downloaded {probe(str(out)).duration:.1f}s, {len(d.content)} bytes")
    print("  PASS - two clips staged, synced, joined with bell transition\n")

    print("== EQUAL-COUNT VALIDATION ==")
    r = s.post(f"{BASE}/api/render", data={
        "gameplay_paths_json": json.dumps(gps),
        "facecam_paths_json": json.dumps(fps[:1])})
    print(f"  2 gameplay vs 1 facecam -> {r.status_code}")
    assert r.status_code == 400
    print("  PASS - mismatched counts rejected\n")

    print("== BROWSER: multi-file tiles + transitions ==")
    pw = open("fightsync-secret.txt").read().strip()
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True); pg = b.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text)
              if m.type == "error" and "favicon" not in m.text.lower() else None)
        pg.goto(BASE, wait_until="networkidle")
        pg.fill("input[name=password]", pw); pg.click("button"); pg.wait_for_load_state("networkidle")
        assert pg.locator("#transitions").count() == 1, "transitions control missing"
        # add two gameplay clips via the hidden picker
        pg.set_input_files("#dropG input[type=file]", [g0, g1])
        pg.wait_for_function("() => document.querySelectorAll('#dropG .items li').length === 2",
                             timeout=20000)
        items = pg.locator("#dropG .items li").count()
        print(f"  gameplay items after selecting 2 files: {items}")
        assert items == 2
        b.close()
    assert not errors, f"JS errors: {errors}"
    print("  PASS - multi-file selection renders an ordered list, no JS errors\n")
    print("MULTI-CLIP API + UI TESTS PASSED")


if __name__ == "__main__":
    main()
