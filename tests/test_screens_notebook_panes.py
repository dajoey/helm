"""Notebook properties appear only when they carry actual context."""
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


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:400] if detail else ""))


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        for route, selector, expected in (
            ("#/notebook", ".nb-home", False),
            ("#/day/2026-09-25", ".dayhead", False),
            ("#/topic/Contacts%2FPine%20Hollow%20portal.md", ".ctx", False),
            ("#/note/Contacts%2FPine%20Hollow%20portal.md", ".nb-note-view", True),
        ):
            d = Drive(browser, st.home, (999, 1392), route)
            d.wait(selector)
            state = d.eval("({display:getComputedStyle(document.querySelector('#pane-props')).display, connected:!!document.querySelector('#props-body .hm-conn'), mainWidth:Math.round(document.querySelector('#pane-main').getBoundingClientRect().width)})")
            visible = state["display"] != "none"
            check(route + ": properties visibility matches its content", visible == expected, state)
            # the note's Connected pane (Helm.connected) is the right pane's content now; the old #aside stays hidden
            check(route + ": the context state and width agree", state["connected"] == expected and (expected or state["mainWidth"] > 600), state)
            d.close()
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
