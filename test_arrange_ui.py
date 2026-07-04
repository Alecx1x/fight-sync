"""Headless drive of round arrangement (server must be running).
Stages 2 rounds, then checks: reorder swaps the WHOLE pair, skip disables a
round in both lists and excludes it from the enabled set, no JS errors.
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765"
PW = open("fightsync-secret.txt", encoding="utf-8").read().strip()


def stage_two(pg, tile_name):
    """Pick the first two distinct options from a tile's saved-uploads dropdown."""
    sel = pg.query_selector(f".drop[data-name='{tile_name}'] select[data-saved]")
    vals = [v for v in sel.evaluate("e=>[...e.options].map(o=>o.value)") if v][:2]
    assert len(vals) >= 2, f"need 2 saved {tile_name} clips, found {len(vals)}"
    for v in vals:
        sel.select_option(value=v)
    return vals


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

        stage_two(pg, "gameplay")
        stage_two(pg, "facecam")
        g0 = pg.evaluate("window._state.gameplay.map(i=>i.path)")
        f0 = pg.evaluate("window._state.facecam.map(i=>i.path)")
        assert len(g0) == 2 and len(f0) == 2, f"staging failed: {len(g0)},{len(f0)}"
        print("  staged 2 rounds")

        # --- reorder: move gameplay round 1 down -> both lists swap pairwise ---
        pg.click("#dropG [data-items] li:nth-child(1) [data-dn]")
        g1 = pg.evaluate("window._state.gameplay.map(i=>i.path)")
        f1 = pg.evaluate("window._state.facecam.map(i=>i.path)")
        assert g1 == [g0[1], g0[0]], f"gameplay not reordered: {g1}"
        assert f1 == [f0[1], f0[0]], f"facecam did NOT follow the round swap: {f1}"
        print("  reorder swapped the whole round (both lists)")

        # --- skip: toggle round 2 off -> disabled in BOTH lists, excluded ---
        pg.click("#dropG [data-items] li:nth-child(2) .tg")
        en_g = pg.evaluate("enabledItems('gameplay').length")
        en_f = pg.evaluate("enabledItems('facecam').length")
        g_off = pg.evaluate("window._state.gameplay[1].enabled")
        f_off = pg.evaluate("window._state.facecam[1].enabled")
        row_off = pg.locator("#dropG [data-items] li:nth-child(2)").evaluate(
            "el=>el.classList.contains('off')")
        assert g_off is False and f_off is False, "skip didn't disable both clips"
        assert en_g == 1 and en_f == 1, f"enabled set wrong: {en_g},{en_f}"
        assert row_off, "skipped row not dimmed"
        print(f"  skip disabled round 2 in both lists; enabled now {en_g}+{en_f}")

        # re-enable restores it
        pg.click("#dropG [data-items] li:nth-child(2) .tg")
        assert pg.evaluate("enabledItems('gameplay').length") == 2, "re-enable failed"
        print("  re-enable restored the round")

        b.close()

    if errors:
        print("JS ERRORS:")
        for e in errors:
            print("   ", e)
        sys.exit(1)
    print("OK - round reorder + skip work end to end, no JS errors")


if __name__ == "__main__":
    main()
