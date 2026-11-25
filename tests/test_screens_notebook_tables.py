"""Playwright drive: a notebook table keeps its short columns readable and scrolls when it is too wide.

Run: python3 tests/test_screens_notebook_tables.py

`.md{overflow-wrap:anywhere}` is inherited by table cells. `anywhere` feeds soft-wrap points into
the browser's min-content width, so under the default auto table layout every cell's minimum
became one character: the column with long prose took the width and hostname / host:port columns
were crushed to one fragment per line (Network/Caddy Routes, at any width).

1. A table shaped like Caddy Routes (short hostname, host:port, long prose): no cell is narrower
   than 90px, and a hostname or backend is on one line, at 999x1392 and at 412x915.
2. A table wider than the page scrolls inside itself (scrollWidth > clientWidth) and the page
   itself does not overflow sideways.
3. A small key/value table is unchanged: fits, no scroll.
4. Prose outside tables still wraps `anywhere` (the change is scoped to cells).
Work site, synthetic data (tools/screens/fixture.py).
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
from test_screens_drive import PHONE, WIDE, Drive  # noqa: E402

passed = failed = 0
NOTE = "#/note/Reference%2FRoute%20table.md"

# per table: its rendered cells' widths and line counts, and its scroll container's sizes
MEASURE = """() => [...document.querySelectorAll('#main .md table')].map(t => ({
  head: t.rows[0].cells[0].textContent.trim(),
  minCell: Math.min(...[...t.querySelectorAll('td,th')].map(c => c.getBoundingClientRect().width)),
  split: [...t.querySelectorAll('td,th')].filter(c => !/\\s/.test(c.textContent.trim())).filter(c => {
    const r = document.createRange(); r.selectNodeContents(c);
    return new Set([...r.getClientRects()].map(q => Math.round(q.top))).size > 1;
  }).map(c => c.textContent.trim()),
  scrolls: t.scrollWidth > t.clientWidth + 1,
  right: t.getBoundingClientRect().right,
}))"""


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        for label, size in (("999x1392", WIDE), ("412x915", PHONE)):
            print(f"\n{label} · tables in a note")
            d = Drive(browser, st.home, size, NOTE)
            d.wait("#main .md table")
            d.page.wait_for_timeout(300)
            tabs = {t["head"]: t for t in d.eval(MEASURE)}
            routes, wide, small = tabs.get("Hostname"), tabs.get("Container"), tabs.get("key")
            check("all three tables rendered", bool(routes and wide and small), list(tabs))
            if not (routes and wide and small):
                d.close()
                continue
            check("route table: no cell narrower than 90px", routes["minCell"] >= 90, routes)
            check("route table: hostnames and backends on one line", not routes["split"], routes)
            check("wide table: no token broken across lines", not wide["split"], wide)
            vw = size[0]
            check("page does not overflow sideways",
                  d.eval("document.documentElement.scrollWidth") <= vw + 1, d.eval("document.documentElement.scrollWidth"))
            check("every table stays inside the viewport", all(t["right"] <= vw + 1 for t in tabs.values()), tabs)
            check("key/value table: fits, no scroll", not small["scrolls"], small)
            if vw < 500:
                check("wide table scrolls horizontally on the phone", wide["scrolls"], wide)
            check("prose outside tables still wraps anywhere",
                  d.eval("getComputedStyle(document.querySelector('#main .md p')).overflowWrap") == "anywhere")
            check("no page errors", not d.errors, d.errors)
            d.close()
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
