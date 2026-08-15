"""Capture dashboard screenshots using Playwright + system Edge (Chromium).
Saves PNGs into ./images with relative-path-friendly names for README embedding.
"""
import os, sys, time

from playwright.sync_api import sync_playwright

EDGE = r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
URL = "http://127.0.0.1:8123/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
os.makedirs(OUT, exist_ok=True)

SHOTS = [
    ("01-overview-full.png", "full"),
    ("02-summary-card.png", "#summary-card"),
    ("03-fund-card.png", None),   # first fund card, resolved at runtime
    ("04-manage-panel.png", "#mgr"),
]

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=EDGE,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900},
                                device_scale_factor=1.5)
        page.goto(URL, wait_until="load", timeout=30000)

        # wait for cards + ECharts canvases to render
        try:
            page.wait_for_selector("#main .card", timeout=15000)
        except Exception:
            print("WARN: no .card found")
        # allow async NAV load + chart draw
        time.sleep(4)

        # dismiss a possible sync overlay if it blocks
        for sel in ["#sync-enter", "#sync-btns button"]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    time.sleep(1)
            except Exception:
                pass

        canvas_count = len(page.query_selector_all("canvas"))
        card_count = len(page.query_selector_all("#main .card"))
        print(f"cards={card_count} canvases={canvas_count}")

        # 1) full page
        page.screenshot(path=os.path.join(OUT, "01-overview-full.png"),
                        full_page=True)
        print("saved overview")

        # 2) summary card
        sc = page.query_selector("#summary-card")
        if sc and sc.is_visible():
            sc.screenshot(path=os.path.join(OUT, "02-summary-card.png"))
            print("saved summary card")

        # 3) first real fund card (id starts with "card_")
        fc = page.query_selector('[id^="card_"]')
        if fc:
            fc.scroll_into_view_if_needed()
            time.sleep(1)
            fc.screenshot(path=os.path.join(OUT, "03-fund-card.png"))
            print("saved fund card")
        else:
            print("WARN: no fund card found")

        # 4) management panel
        btn = page.query_selector("#btn-mgr")
        if btn:
            btn.click()
            time.sleep(1.5)
            # expand the first trade group so the fee column is visible
            fold = page.query_selector(".tr-fold")
            if fold:
                fold.click()
                time.sleep(0.8)
            mgr = page.query_selector("#mgr")
            if mgr and mgr.is_visible():
                mgr.screenshot(path=os.path.join(OUT, "04-manage-panel.png"))
                print("saved manage panel")
            else:
                page.screenshot(path=os.path.join(OUT, "04-manage-panel.png"))
                print("saved manage panel (page fallback)")

        browser.close()
    print("DONE")

if __name__ == "__main__":
    main()
