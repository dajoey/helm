"""Delayed notebook mutations cannot replace the route the user opened next."""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "screens"))
sys.path.insert(0, HERE)

import fixture
import shoot
from test_screens_drive import Drive
from playwright.sync_api import sync_playwright

passed = failed = 0
OTHER = "#/note/Reference%2FTicket%20numbers.md"


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:400] if detail else ""))


def main():
    with fixture.Stack(("home",), notes_modes={"home": "store"}) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        for name, route, start, pattern in (
            ("create today", "#/notebook", lambda d: d.page.click("[data-newday]"), "**/api/notes/daily/new"),
            ("append day", "#/day/2026-09-25", lambda d: (d.page.fill("#append-line", "delayed line"), d.page.click("[data-append]")), "**/api/day/append"),
            ("toggle note", "#/note/Contacts%2FPine%20Hollow%20portal.md", lambda d: d.page.click(".md .task-box >> nth=0"), "**/api/notes/toggle"),
        ):
            d = Drive(browser, st.home, (999, 1392), route)
            d.wait({ "#/notebook": ".nb-home", "#/day/2026-09-25": "#append-line" }.get(route, ".nb-note-view"))
            held = []
            d.page.route(pattern, lambda route: held.append(route))
            start(d)
            d.page.goto(st.home.web_url + "/" + OTHER, wait_until="domcontentloaded")
            d.wait(".nb-note-view .md h1")
            check(name + ": the mutation request reached the server and was held", len(held) == 1, len(held))
            if held:
                held[0].continue_()
            d.page.wait_for_timeout(600)
            state = d.eval("({hash:location.hash, title:document.querySelector('.nb-note-view .md h1')?.textContent || '', body:document.querySelector('#main').textContent.slice(0,500)})")
            check(name + ": the newer note route wins after the response lands", state["hash"].startswith("#/note/Reference") and state["title"] == "Ticket numbers", state)
            check(name + ": no stale failure or page error appears", "failed" not in state["body"].lower() and not d.errors, (state["body"][-200:], d.errors))
            d.close()
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
