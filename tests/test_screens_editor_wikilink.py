"""Playwright drive of Ctrl+click on a [[wikilink]] in the note editor (home site, notebook store).

Run: python3 tests/test_screens_editor_wikilink.py

Ctrl+click used to look the name up in the editor's autocomplete (12 candidates, newest first), so a
link to a note whose name many newer notes also contain said "no note called X yet" while the read
view linked it. It asks the server's link resolver now. Synthetic data (tools/screens/fixture.py).
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


def put(home, rel, text):
    return api(home, "/api/notes/note", {"rel": rel, "text": text, "create": True}, method="PUT")


def main():
    with fixture.Stack(("home",), notes_modes={"home": "store"}) as st, sync_playwright() as p:
        home = st.home
        put(home, "Reference/Plan.md", "# Plan\n\nthe note the link names.\n")
        for i in range(14):
            put(home, "Contacts/Plan %d.md" % i, "# Plan %d\n" % i)
        put(home, "Reference/Links.md", "# Links\n\n[[Plan]] and [[No Such Page Anywhere]]\n")
        items = api(home, "/api/notes/complete?q=Plan&kind=all").get("items", [])
        check("setup: the autocomplete's candidates for 'Plan' leave out Reference/Plan.md",
              len(items) == 12 and all(i["rel"] != "Reference/Plan.md" for i in items), [i["rel"] for i in items])

        browser = p.chromium.launch()
        d = Drive(browser, home, WIDE, "#/edit/Reference%2FLinks.md")
        try:
            d.wait("#editor-host .cm-editor")
            d.wait(".cm-wikilink")
            d.page.click(".cm-wikilink >> nth=0", modifiers=["Control"])
            h = d.wait_hash(lambda h: "Links.md" not in h)
            check("Ctrl+click on [[Plan]] opens Reference/Plan.md", h.endswith("Reference%2FPlan.md") or h.endswith("Reference/Plan.md"), h)

            d.page.goto(home.web_url + "/#/edit/Reference%2FLinks.md", wait_until="domcontentloaded")
            d.wait("#editor-host .cm-editor")
            d.wait(".cm-wikilink >> nth=1")
            d.page.click(".cm-wikilink >> nth=1", modifiers=["Control"])
            check("Ctrl+click on a name no note has says so", d.until("document.body.innerText.includes('no note called No Such Page Anywhere yet')", 5000))
            check("...and stays on the note", "Links.md" in d.hash(), d.hash())
            check("no page errors", not d.errors, d.errors[:3])
        finally:
            d.close()
        browser.close()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
