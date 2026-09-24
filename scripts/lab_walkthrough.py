"""Drive the five lab pages (leash lab <part>) and save screenshots to reports/screenshots/lab-*.png.
Start them first: for p in permanent mandate apply shop-text respond; do uv run leash lab $p & done"""
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "reports" / "screenshots"
H = "http://127.0.0.1"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1600, "height": 1000})
        pg.on("pageerror", lambda e: errors.append(f"{pg.url}: {e}"))

        pg.goto(f"{H}:8101/")                                  # 1 · permanent rules from the profile
        pg.wait_for_selector(".robj")
        pg.select_option("#who", "CU0005")
        pg.wait_for_timeout(600)
        pg.screenshot(path=str(OUT / "lab-1-permanent.png"), full_page=True)

        pg.goto(f"{H}:8102/")                                  # 2 · a mandate
        pg.wait_for_selector("#rules .robj")
        pg.screenshot(path=str(OUT / "lab-2-mandate.png"), full_page=True)

        pg.goto(f"{H}:8103/")                                  # 3 · applying rules (SCEN0004, the US seller)
        pg.wait_for_selector("#out .chk")
        pg.select_option("#pi", "3")
        pg.wait_for_timeout(900)
        pg.screenshot(path=str(OUT / "lab-3-apply.png"), full_page=True)

        pg.goto(f"{H}:8104/")                                  # 4 · the shop's text (an attack example)
        pg.wait_for_selector("#out .panel")
        pg.select_option("#ex", "attacks:0")
        pg.wait_for_timeout(600)
        pg.screenshot(path=str(OUT / "lab-4-shop-text.png"), full_page=True)

        pg.goto(f"{H}:8105/")                                  # 5 · customer response
        pg.wait_for_selector("#start")
        pg.click("#start")
        pg.wait_for_selector("#banner:not([hidden])")
        pg.wait_for_timeout(600)
        pg.click("#banner")
        pg.click("#app [data-res='decline']")
        pg.wait_for_timeout(300)
        pg.click("#next")                                      # the injected CHF 520 order: flagged
        pg.wait_for_timeout(400)
        pg.click("#controls [data-mode='block']")
        pg.wait_for_timeout(300)
        pg.click("#retry")                                     # same shop again: now declined by the block
        pg.wait_for_timeout(400)
        pg.screenshot(path=str(OUT / "lab-5-respond.png"), full_page=True)
        b.close()
    print("page errors:", errors or "none")


if __name__ == "__main__":
    main()
