"""First load answers no 404 for the one fixed Helm design ().

Run: python3 tests/test_screens_first_load.py

Loads every section cold through the fixture at 999 x 1392 and 411 x 915 and records every
response. None may be a 404, and no retired theme/SNES script, sheet, sprite or sound may load.
Then asks the web role for every file in the service worker's exact required SHELL set, so a
file that exists on disk but is not served is caught too.

One transient is injected on purpose: on the 999-wide board load the fake dashboard answers 503
to exactly one card-log read (the runner really produced one on 2026-11-21, among 75 answers).
A transient 5xx gets one re-read after a beat; only a refusal that persists fails the page.
404s are never retried — a missing route is the whole point of this suite.

The fixture's service worker script fetch is refused 401 (a worker's script request carries none
of the browser's extra headers), so a 401 for /sw.js alone is the fixture, not the app.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

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
from test_screens_drive import HEADERS  # noqa: E402
from test_sw_shell import required_shell, shell_list  # noqa: E402

passed = failed = 0
SECTIONS = ["#/", "#/home", "#/chat", "#/board", "#/manage", "#/activity"]
SIZES = [(999, 1392), (411, 915)]


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def retired_url(url: str) -> bool:
    path = url.split("?", 1)[0]
    return path.startswith("/themes/") or (path.startswith("/snes/") and path != "/snes/icon-32.png")


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        base = st.home.web_url
        for size in SIZES:
            print("\nfirst load, fixed Helm design, %d wide" % size[0])
            for route in SECTIONS:
                ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, extra_http_headers=HEADERS, locale="en-US",
                                          timezone_id="America/New_York")
                page = ctx.new_page()
                seen = []
                page.on("response", lambda r: seen.append((r.status, r.url.replace(base, ""))))
                page.goto(base + "/" + route, wait_until="load")
                page.wait_for_timeout(2500)
                bad = [(s, u) for s, u in seen if s == 404]
                other = [(s, u) for s, u in seen if s >= 400 and s != 404 and not (s == 401 and u == "/sw.js")]
                for s, u in [x for x in other if x[0] in (502, 503, 504)]:
                    time.sleep(1.5)     # a beat, then one re-read: a transient upstream refusal is not a broken route
                    try:
                        with urllib.request.urlopen(urllib.request.Request(base + u, headers=HEADERS), timeout=20) as r:
                            code = r.status
                    except urllib.error.HTTPError as e:
                        code = e.code
                    if code < 400:
                        other.remove((s, u))
                        print("  re-read %s -> %d (transient %d absorbed)" % (u, code, s))
                retired = [(s, u) for s, u in seen if retired_url(u)]
                check("%s: %d responses, no 404" % (route, len(seen)), len(seen) > 20 and not bad, bad or len(seen))
                check("%s: nothing else refused either" % route, not other, other)
                check("%s: no retired theme, SNES, sprite or sound request" % route, not retired, retired)
                check("%s: the fixed identity survives first paint" % route,
                      page.evaluate("document.documentElement.dataset.theme") == "helm"
                      and page.evaluate("!document.querySelector('#btn-theme, #btn-sound')"))
                ctx.close()
        print("\nevery file the service worker caches is served")
        shell = shell_list(browser)
        required = required_shell()
        refused = []
        for u in shell:
            try:
                urllib.request.urlopen(urllib.request.Request(base + u, headers=HEADERS), timeout=20).read(1)
            except urllib.error.HTTPError as e:
                refused.append((e.code, u))
        check("all %d required SHELL entries answer 200 from the web role" % len(required),
              len(shell) == len(required) and set(shell) == set(required) and not refused,
              {"refused": refused, "actual_count": len(shell), "required_count": len(required)})
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
