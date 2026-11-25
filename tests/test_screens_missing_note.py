"""Playwright drive: a notebook page open on a note the site does not hold stops asking for it.

Run: python3 tests/test_screens_missing_note.py

The work api's error watcher filed `GET /api/notes/note 404`. The only caller is the notebook
view's loadView(); a tab left open on a note that is gone (deleted, moved without a redirect, or
a stale deep link) asked for it again on every 120 s refresh, and got the same 404. A note that is
not there is not going to appear because the page asked again.

1. Open a missing note: the page says so, and across three refresh ticks it asks for the note no
   more than the one time the open itself needed (it was one per tick, forever).
2. R / the refresh button (a deliberate refresh) still asks again.
3. A note that exists is still re-read on every tick.
Synthetic data (tools/screens/fixture.py).
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
import shoot  # noqa: E402
from test_screens_drive import WIDE, Drive  # noqa: E402

passed = failed = 0
MISSING = "Contacts/No such note.md"
HELD = "Contacts/Pine Hollow portal.md"
TICK_MS = 121_000   # notebook/app.js REFRESH_MS is 120 s


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def watch(d):
    """Every response the page gets from now on: (method, path, status, rel)."""
    seen = []

    def on(r):
        path, _, query = r.url.split("//", 1)[-1].split("/", 1)[-1].partition("?")
        seen.append((r.request.method, path, r.status, query))
    d.page.on("response", on)
    return seen


def note_reads(seen):
    return [s for s in seen if s[0] == "GET" and s[1] == "api/notes/note"]


def tick(d, n=3):
    for _ in range(n):
        d.page.clock.run_for(TICK_MS)
        d.page.wait_for_timeout(500)


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())

        print("\n1 · a note the site does not hold: said once, not polled")
        d = Drive(browser, st.home, WIDE, "#/notebook")
        d.wait("#main .view")
        d.page.clock.install()   # the 120 s refresh runs on the drive's clock from here
        seen = watch(d)
        d.page.evaluate("location.hash = '#/note/%s'" % MISSING.replace("/", "%2F").replace(" ", "%20"))
        d.page.wait_for_timeout(1500)
        text = d.eval("document.querySelector('#main').textContent")
        check("the page says the note is not here, with a way back", "not here" in text and "Back to the notebook" in text, text[:200])
        n0 = len(note_reads(seen))
        check("the open itself asked once", n0 == 1, n0)
        tick(d)
        n1 = len(note_reads(seen))
        check("three refresh ticks: no further read of the missing note", n1 == n0, (n0, n1))
        check("no page errors", not d.errors, d.errors)

        print("\n2 · a deliberate refresh asks again")
        d.eval("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
        d.page.keyboard.press("r")
        for _ in range(50):   # poll up to 5 s: a loaded runner answers late, not never
            if len(note_reads(seen)) > n1:
                break
            d.page.wait_for_timeout(100)
        n2 = len(note_reads(seen))
        check("R reads the note again", n2 > n1, (n1, n2))
        d.close()

        print("\n3 · a note that exists is still re-read on every tick")
        d = Drive(browser, st.home, WIDE, "#/notebook")
        d.wait("#main .view")
        d.page.clock.install()
        seen = watch(d)
        d.page.evaluate("location.hash = '#/note/%s'" % HELD.replace("/", "%2F").replace(" ", "%20"))
        d.page.wait_for_timeout(1500)
        n0 = len(note_reads(seen))
        tick(d)
        n1 = len(note_reads(seen))
        check("three ticks read the held note three more times", n1 - n0 >= 3, (n0, n1))
        check("no 404 on it", all(s[2] == 200 for s in note_reads(seen)), note_reads(seen))
        check("no page errors", not d.errors, d.errors)
        d.close()

        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
