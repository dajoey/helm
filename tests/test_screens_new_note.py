"""Playwright drive of the notebook's new-note dialog on the home site (notebook store).

Run: python3 tests/test_screens_new_note.py

Since the notebook organizer (PR #28) the home folder list (GET /api/notes/folders `folders`) leaves
out a folder with no notes: there is nothing in it to read. The new-note dialog is where a note is
filed, so it offers every folder a note may go in (`allowed`), a brand-new empty one included, and
the first note lands there. Home site in store mode, synthetic data (tools/screens/fixture.py).
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "screens"))
sys.path.insert(0, HERE)

if os.environ.get("HELM_SKIP_DRIVES") == "1":
    print("  FAIL the drives were skipped (HELM_SKIP_DRIVES=1): this suite never passes quietly")
    print("0 passed, 1 failed")
    sys.exit(1)

from playwright.sync_api import sync_playwright  # noqa: E402

import fixture  # noqa: E402
from test_screens_drive import WIDE, Drive, api  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def main():
    with fixture.Stack(("home",), notes_modes={"home": "store"}) as st, sync_playwright() as p:
        home = st.home
        made = api(home, "/api/notes/folder", {"name": "Garden"})
        check("a new home folder is made through the api", made.get("created") is True, made)
        fo = api(home, "/api/notes/folders")
        listed = [f["folder"] for f in fo.get("folders", [])]
        check("the empty folder is not listed to browse (nothing in it to read)", "Garden" not in listed, listed)
        check("it is listed as a folder a note may be filed in", "Garden" in (fo.get("allowed") or []), fo.get("allowed"))

        browser = p.chromium.launch()
        d = Drive(browser, home, WIDE, "#/notebook")
        try:
            d.wait("#main .view")
            d.page.keyboard.press("Alt+n")
            d.wait("#nn-folder")
            d.until("document.querySelectorAll('#nn-folder option').length > 0", timeout=3000)
            opts = d.eval("[...document.querySelectorAll('#nn-folder option')].map(o => o.value)")
            check("the new-note dialog offers the empty folder", "Garden" in opts, opts)
            check("...and the folders that hold notes", {"Contacts", "Reference"} <= set(opts), opts)
            check("...but not the daily folders (the day's note has its own route)",
                  not any(o.startswith("Daily") for o in opts), opts)
            if "Garden" in opts:
                d.page.fill("#nn-title", "Tomato beds")
                d.page.select_option("#nn-folder", "Garden")
                d.page.click("#nn-go")
                h = d.wait_hash(lambda h: "Garden" in h)
                check("the first note is filed in the new folder", "Garden%2FTomato%20beds.md" in h or "Garden/Tomato beds.md" in h, h)
                fo = api(home, "/api/notes/folders")
                check("...and the folder is listed to browse now",
                      any(f["folder"] == "Garden" and f["count"] == 1 for f in fo["folders"]), fo["folders"])
            check("no page errors", not d.errors, d.errors[:3])
        finally:
            d.close()

        # The boot race behind a flaky CI: Alt+n pressed while the boot fetch of the folders is still
        # in flight. The dialog must wait for the folders, not render an empty folder select.
        held, state = [], {"open": False}

        def hold_folders(route):
            if state["open"]:
                route.continue_()
            else:
                held.append(route)

        d = Drive(browser, home, WIDE, "#/notebook", before_goto=lambda ctx: ctx.route("**/api/notes/folders", hold_folders))
        try:
            d.wait("#main .view")
            d.page.keyboard.press("Alt+n")
            d.page.wait_for_timeout(700)   # the dialog gets its chance to build while the folders are held
            state["open"] = True
            for r in held:
                r.continue_()
            d.wait("#nn-folder")
            d.until("document.querySelectorAll('#nn-folder option').length > 0", timeout=3000)
            opts = d.eval("[...document.querySelectorAll('#nn-folder option')].map(o => o.value)")
            check("Alt+n before the folders have loaded: the dialog still offers the empty folder", "Garden" in opts, opts)
            check("...and the folders that hold notes (slow boot)", {"Contacts", "Reference"} <= set(opts), opts)
            check("no page errors (slow boot)", not d.errors, d.errors[:3])
        finally:
            d.close()
            browser.close()
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
