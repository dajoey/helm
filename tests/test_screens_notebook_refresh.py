"""Playwright drive: the notebook's refresh() is ordered against navigate() and against typing.

Run: python3 tests/test_screens_notebook_refresh.py

refresh() (the 120 s poll, the refresh button, R) took snapshotInputs() before its awaits and put
it back after render(): text typed into an empty append box after the snapshot was not in it, and
render() replaced the box, so the text was gone. And loadView() assigned S.view after its awaits
with no check that the route was still the one it started on: a poll that began on the day view
and landed after the user opened a note left S.view as the day payload under a note route, noteView
threw, and the toast read "refresh failed".

1. Typing into the day page's append box while a refresh is in flight survives the refresh.
2. A refresh's day payload that lands after the user opened a note does not replace the note's view:
   the note is still drawn and no "refresh failed" toast shows.
3. A refresh with nothing else going on still lands.
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
HELD = "Contacts/Pine Hollow portal.md"
TYPED = "typed while the refresh was in flight"


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


class Hold:
    """Hold the next request whose path matches, until release() (so a test orders the responses)."""

    def __init__(self, d, path):
        self.pending = []
        self.armed = True
        d.ctx.route("**/" + path + "*", self._on)

    def _on(self, route):
        if self.armed:
            self.armed = False
            self.pending.append(route)
        else:
            route.continue_()

    def release(self):
        for r in self.pending:
            r.continue_()
        self.pending = []


def today(d):
    return d.eval("(() => { const x = new Date(); return x.getFullYear() + '-' + String(x.getMonth() + 1).padStart(2, '0') + '-' + String(x.getDate()).padStart(2, '0'); })()")


def open_day(browser, st):
    d = Drive(browser, st.home, WIDE, "#/notebook")
    d.wait("#main .view")
    d.eval("location.hash = '#/day/%s'" % today(d))
    d.wait("#append-line")
    return d


def main():
    with fixture.Stack(("home",), notes_modes={"home": "store"}) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())

        print("\n1 · typing during a refresh survives it")
        d = open_day(browser, st)
        hold = Hold(d, "api/notes/folders")
        d.page.click("[data-nbrefresh]")
        d.page.wait_for_timeout(300)
        d.page.click("#append-line")
        d.page.keyboard.type(TYPED)
        hold.release()
        d.page.wait_for_timeout(1500)
        v = d.eval("(document.getElementById('append-line') || {}).value")
        check("the refresh landed", d.eval("!!document.querySelector('#main .view')"))
        check("the text typed after it started is still in the append box", v == TYPED, v)
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\n2 · a late day payload does not replace the note the user opened")
        d = open_day(browser, st)
        hold = Hold(d, "api/day")
        d.page.click("[data-nbrefresh]")
        d.page.wait_for_timeout(500)
        d.eval("location.hash = '#/note/%s'" % HELD.replace("/", "%2F").replace(" ", "%20"))
        d.wait("#main .md")
        d.page.wait_for_timeout(500)
        hold.release()
        d.page.wait_for_timeout(1500)
        toast = d.eval("document.getElementById('toast').textContent")
        check("no 'refresh failed' toast", "refresh failed" not in toast, toast)
        check("the note is still on screen", d.eval("!!document.querySelector('#main .md') && !document.querySelector('#main .daygrid')"),
              d.eval("document.querySelector('#main').textContent.slice(0, 200)"))
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\n3 · a quiet refresh still lands")
        d = open_day(browser, st)
        d.page.click("[data-nbrefresh]")
        d.page.wait_for_timeout(1500)
        check("the day page is drawn", d.eval("!!document.querySelector('#main .daygrid')"))
        check("no page errors", not d.errors, d.errors)
        d.close()

        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
