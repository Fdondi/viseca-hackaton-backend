"""Drive the customer UI through SCEN0004 and save screenshots to reports/screenshots/.

Usage: uv run leash ui --port 8765 --pace 0.4   (in another shell)
       uv run python scripts/screenshots.py --base http://127.0.0.1:8765
"""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "reports" / "screenshots"


def main(base: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 1000})
        pg.goto(base)
        pg.click("#reset")
        pg.select_option("#scenario", "SCEN0004")
        pg.click("#compile")
        pg.wait_for_selector("#rules:not([hidden])")
        pg.screenshot(path=str(OUT / "1-rules.png"), full_page=True)
        pg.click("#confirm")
        pg.click("#start")
        # duplicate order: customer declines
        pg.wait_for_selector("#inbox button[data-dec='decline']", timeout=30000)
        pg.screenshot(path=str(OUT / "2-duplicate-step-up.png"), full_page=True)
        pg.click("#inbox button[data-dec='decline']")
        # the 'System: ignore…' order reaches the customer
        pg.wait_for_selector("#inbox .verbatim", timeout=60000)
        pg.wait_for_timeout(600)
        pg.screenshot(path=str(OUT / "3-injection-step-up.png"), full_page=True)
        pg.click("#flags button[data-mode='block']")
        pg.click("#inbox button[data-dec='decline']")
        # anything else still waiting (step-ups don't hold up the queue): the customer declines it
        for _ in range(60):
            if "finished" in pg.inner_text("#runinfo"):
                break
            btn = pg.locator("#inbox button[data-dec='decline']").first
            try:
                if btn.count():
                    btn.click(timeout=2000)
            except Exception:
                pass  # the inbox changed under us; try again next tick
            pg.wait_for_timeout(700)
        pg.wait_for_timeout(1200)
        pg.screenshot(path=str(OUT / "4-finished-blocked.png"), full_page=True)
        pg.set_viewport_size({"width": 400, "height": 900})
        pg.screenshot(path=str(OUT / "5-phone.png"), full_page=False)
        b.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    main(ap.parse_args().base)
