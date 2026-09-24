"""Drive the interactive demo (leash demo-web) and save screenshots to reports/screenshots/demo-web-*.png."""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "reports" / "screenshots"


def main(base: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1500, "height": 1000})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.goto(base)
        pg.wait_for_selector(".person")
        pg.click(".person[data-sid='SCEN0004']")
        pg.screenshot(path=str(OUT / "demo-web-1-pick.png"), full_page=True)
        pg.click("#go")
        pg.wait_for_selector("#send")

        def send():
            pg.click("#send")
            pg.wait_for_timeout(400)

        send()                                     # AU0035 as in the file: approve
        send()                                     # AU0036 duplicate: asks the customer
        pg.click("#answer button[data-res='decline']")
        pg.wait_for_timeout(300)
        send()                                     # AU0037 'pre-authorised' text: declined, shop flagged
        pg.click("button[data-preset='inject']")   # AU0038 (a clean, familiar US seller) with a hidden instruction added
        pg.wait_for_timeout(200)
        pg.screenshot(path=str(OUT / "demo-web-2-edited.png"), full_page=True)
        send()
        pg.screenshot(path=str(OUT / "demo-web-3-answer.png"), full_page=True)
        pg.click("#answer button[data-res='approve']")
        pg.wait_for_timeout(300)
        pg.click("button[data-preset='lookalike']")  # AU0039 is already a lookalike; make a second one
        send()
        pg.set_viewport_size({"width": 400, "height": 900})
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(OUT / "demo-web-4-phone.png"), full_page=False)
        b.close()
    print("page errors:", errors or "none")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    main(ap.parse_args().base)
