"""Playwright drives for what the first hours of the live app found (2026-09-26 home cutover).

Run: python3 tests/test_screens_live_sweep.py

1. The page reports its own errors: an uncaught error and an unhandled rejection each reach
   POST /api/client-error once, with the view; a network failure is not reported.
(2-5 drove the home console's board and its tasks with no card; the console is retired.)
Home site, synthetic data (tools/screens/fixture.py).
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
NO_CARD = "t-queued-tasks-20260925-rotate-media-box-logs"   # a queued task the fixture holds no Helm card for


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def watch(d):
    """Every request the page makes from now on: (method, path, status or None)."""
    seen = []
    d.page.on("request", lambda r: seen.append([r.method, r.url.split("//", 1)[-1].split("/", 1)[-1].split("?")[0], None, r]))

    def done(resp):
        for row in reversed(seen):
            if row[3] is resp.request and row[2] is None:
                row[2] = resp.status
                return
    d.page.on("response", done)
    return seen


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())

        print("\n1 · the page reports its own errors")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you")
        seen = watch(d)
        d.eval("setTimeout(() => { throw new TypeError('drive-boom-uncaught') }, 0)")
        d.eval("setTimeout(() => { Promise.reject(new Error('drive-boom-rejected')) }, 0)")
        d.eval("setTimeout(() => { Promise.reject(new TypeError('Failed to fetch')) }, 0)")
        d.eval("setTimeout(() => { throw new TypeError('drive-boom-uncaught') }, 10)")   # the same one again
        d.page.wait_for_timeout(1500)
        posts = [r for r in seen if r[0] == "POST" and r[1] == "api/client-error"]
        bodies = [r[3].post_data or "" for r in posts]
        check("an uncaught error is reported once", sum("drive-boom-uncaught" in b for b in bodies) == 1, bodies)
        check("an unhandled rejection is reported", any("drive-boom-rejected" in b and '"rejection"' in b for b in bodies), bodies)
        check("the report says which view it happened on", any('"view":"#/needs-you"' in b for b in bodies), bodies)
        check("a network failure is not reported", not any("Failed to fetch" in b for b in bodies), bodies)
        check("each report is accepted (204)", posts and all(r[2] == 204 for r in posts), [(r[1], r[2]) for r in posts])
        d.close()

        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
