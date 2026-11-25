"""View transitions never leave two active pages or depend on animation completion."""
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
        for name, reduced, hidden in (("ordinary", "no-preference", False), ("reduced-motion", "reduce", False), ("hidden-document", "no-preference", True)):
            before = None
            if hidden:
                def before(ctx):
                    ctx.add_init_script("Object.defineProperty(document, 'hidden', {configurable: true, get: () => true})")
            d = Drive(browser, st.home, (999, 1392), "#/notebook", reduced_motion=reduced, before_goto=before)
            d.wait(".nb-home")
            d.eval("Helm.showMain('search')")
            d.page.wait_for_timeout(450)
            state = d.eval("({visible:[...document.querySelectorAll('#pane-main > .main-view:not(.hidden)')].map(n=>n.id), leaving:[...document.querySelectorAll('.view-leaving')].length, main:document.body.dataset.main})")
            check(name + ": exactly the destination view stays visible", state["visible"] == ["view-search"] and state["main"] == "search", state)
            check(name + ": no outgoing view or animation is left behind", state["leaving"] == 0, state)
            if name == "ordinary":
                d.eval("for (const n of ['notebook', 'search', 'notebook', 'search']) Helm.showMain(n)")
                d.page.wait_for_timeout(450)
                rapid = d.eval("({visible:[...document.querySelectorAll('#pane-main > .main-view:not(.hidden)')].map(n=>n.id), opacity:getComputedStyle(document.querySelector('#view-search')).opacity, leaving:[...document.querySelectorAll('.view-leaving')].length})")
                check("rapid A-B-A-B: the destination is visible and fully opaque", rapid["visible"] == ["view-search"] and rapid["opacity"] == "1", rapid)
                check("rapid A-B-A-B: every interrupted animation is cleaned up", rapid["leaving"] == 0, rapid)
                # Real hash navigation superseding an in-flight transition (R6): the rapid block
                # left search showing; go to the notebook by hash, then to activity while the
                # notebook's 160/180 ms transition is still in flight.
                d.eval("location.hash = '#/notebook'")
                mid = d.until("document.querySelector('#view-notebook.view-entering') !== null", timeout=5000)
                check("hash navigation A->B: B's transition is in flight", mid, d.errors[:3])
                mid_at_switch = d.eval("(() => { const mid = document.querySelector('#view-notebook.view-entering') !== null; location.hash = '#/activity'; return mid; })()")
                check("hash supersession mid-transition: B was still in flight when C superseded it", mid_at_switch, mid_at_switch)
                d.page.wait_for_timeout(450)
                sup = d.eval("({visible:[...document.querySelectorAll('#pane-main > .main-view:not(.hidden)')].map(n=>n.id), opacity:getComputedStyle(document.querySelector('#view-activity')).opacity, leaving:[...document.querySelectorAll('.view-leaving')].length, main:document.body.dataset.main})")
                check("hash supersession mid-transition: exactly the destination view stays visible", sup["visible"] == ["view-activity"] and sup["main"] == "activity", sup)
                check("hash supersession mid-transition: the destination is fully opaque", sup["opacity"] == "1", sup)
                check("hash supersession mid-transition: no outgoing view is left behind", sup["leaving"] == 0, sup)
                check("hash supersession mid-transition: no page error was raised", not d.errors, d.errors[:3])
            d.close()
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
