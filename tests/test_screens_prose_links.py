"""Prose links at the sites that must not change (web/app.js appendRefText, note N8, step 14).

Run: python3 tests/test_screens_prose_links.py

The prose renderer (appendRefText) is gated: a [[wikilink]] renders only where the notebook module
is on, and a thread id only when the thread list holds it. This suite pins the rendered output of
three strings as constants captured from the current code, and runs the same three strings in the
app; every value is made up.
"""
from __future__ import annotations

import json
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


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {str(detail)[:500]}")


# The constants: what the renderer drew at capture time (see the module docstring for the basis).
# 2026-11 (Helm shell): a reference is an object chip (web/objects.js): the type's glyph (drawn by glyphs.js: held here as <svg/> so
# this pin is about the link, not the artwork), the label in its own span, the type colour class and the peek's data attributes.
WIKI = '<a class="hm-ochip t-note body-link body-ref" href="#/note/Page.md" data-peek="note" data-peek-id="Page.md" data-peek-label="Page"><svg/><span class="hm-ochip-t">Page</span></a>'
CARD_ID_OFF = "t_abcd1234"     # a card id is not a link: the app holds no board
THREAD = '<a class="hm-ochip t-ask body-link body-ref" href="#/threads/t-media-box-disk" data-peek="ask" data-peek-id="t-media-box-disk" data-peek-label="t-media-box-disk"><svg/><span class="hm-ochip-t">t-media-box-disk</span></a>'

STRINGS = ["[[Page]]", "t_abcd1234", "t-media-box-disk"]
WANT = {"[[Page]]": WIKI, "t_abcd1234": CARD_ID_OFF, "t-media-box-disk": THREAD}
RENDER = "(s => { const box = document.createElement('div'); window.appendRefText(s, box); return box.innerHTML.replace(/<svg[\\s\\S]*?<\\/svg>/g, '<svg/>'); })"
N_THREADS = "(() => (typeof state !== 'undefined' && Array.isArray(state.threads)) ? state.threads.length : -1)"

HEALTH = {"gateway": {"status": "ok", "state": "running"}, "dashboard": {"status": "ok", "state": "running"}}


def rendered(d):
    for _ in range(60):
        if d.page.evaluate(N_THREADS) > 0:
            break
        d.page.wait_for_timeout(500)
    return {s: d.page.evaluate(RENDER, s) for s in STRINGS}


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())

        print("\nthe app")
        d = Drive(browser, st.home, WIDE, "#/threads")
        got = rendered(d)
        for s in STRINGS:
            check("%s renders exactly as captured" % s, got[s] == WANT[s], got[s])
        check("no page error", not d.errors, d.errors)
        d.close()


    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
