"""Drive the flow demo (leash flow-web) and save screenshots to reports/screenshots/flow-web-*.png."""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "reports" / "screenshots"


def main(base: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1600, "height": 1000})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.request.post(base + "/api/reset")
        pg.goto(base)
        pg.wait_for_selector("#compile")
        pg.select_option("#who", "SCEN0004")
        pg.click("#compile")
        pg.wait_for_selector("#confirm")
        pg.screenshot(path=str(OUT / "flow-web-1-review.png"), full_page=True)
        pg.click("#confirm")
        pg.wait_for_selector("#next")
        pg.click("#next")                          # AU0035: an ordinary purchase
        pg.wait_for_selector(".chk")
        pg.screenshot(path=str(OUT / "flow-web-2-approve.png"), full_page=True)
        pg.check("#ap2")                           # from here on, the shop signs its carts (AP2)
        pg.click("#next")                          # AU0036: duplicate → the phone gets a push notification
        pg.wait_for_selector("#banner:not([hidden])")
        pg.wait_for_timeout(700)                   # let the slide-in finish
        pg.screenshot(path=str(OUT / "flow-web-2a-banner.png"), full_page=False)
        pg.click("#banner")                        # the system notification opens the purchase page
        pg.wait_for_selector("#app [data-res='decline']")
        pg.screenshot(path=str(OUT / "flow-web-2b-push.png"), full_page=False)
        pg.click("#app [data-res='decline']")
        pg.wait_for_timeout(300)
        pg.click("#back")
        pg.click("#next")                          # AU0037: "pre-authorised" text in the shop's product text
        pg.wait_for_timeout(400)
        pg.screenshot(path=str(OUT / "flow-web-3-injection.png"), full_page=True)
        pg.click(".chk:has-text('security.merchant_text_clean')")
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(OUT / "flow-web-5-wires.png"), full_page=True)
        pg.set_viewport_size({"width": 420, "height": 900})
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(OUT / "flow-web-4-phone.png"), full_page=False)
        b.close()
    print("page errors:", errors or "none")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8002")
    main(ap.parse_args().base)
