"""Playwright drive: the composer sends through Helm's own /reply when there is no agent console.

Run: python3 tests/test_screens_composer_no_console.py

The home composer sent a note through the agent console (POST /api/conversations/<id>/note)
whenever the conversation payload did not say the console was missing ("none"/"unreachable").
A payload with no console field at all was read as console-backed. With the console retired
(a chat link, H1) the composer must fall back to Helm's own door, `/reply` with `__redirect__`
and the note, whenever the payload does not say a console merged it.

1. A working thread whose conversation payload carries no console field: Send posts
   /reply {value: "__redirect__", note} to Helm, never the console's /note, and the thread
   records his words.
(PR-B retired the console whole: no payload is console-backed any more, and its routes are 404.)
Home site, synthetic data (tools/screens/fixture.py).
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
from test_screens_drive import WIDE, Drive, api  # noqa: E402

passed = failed = 0
TID = "t-no-console-composer"
TEXT = "Use the smaller volume and skip the reboot"


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

        print("\n1 · a conversation payload with no console field")
        api(st.home, "/api/triage/threads", {"threads": [{
            "thread_id": TID, "title": "Grow the scratch volume", "summary": "The scratch volume is nearly full",
            "category": "Fleet", "urgency": "this-week", "state": "needs-you",
            "ask": {"kind": "choice", "prompt": "Grow or prune the scratch volume?", "requires": ["decision"],
                    "options": [{"label": "Grow the volume", "value": "grow", "effect": "A short outage"},
                                {"label": "Prune older files", "value": "prune", "effect": "Deletes 40 GB"}]},
            "sources": [{"source": "test", "id": TID}]}]}, "PUT")
        api(st.home, "/api/triage/threads/%s/reply" % TID, {"value": "grow"})

        def no_console(route, request):
            r = route.fetch()
            d = r.json()
            d.pop("console", None)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(d))

        posts = []
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you")
        d.page.on("request", lambda rq: posts.append((rq.url, rq.post_data or "")) if rq.method == "POST" else None)
        d.page.route("**/api/conversations/" + TID, no_console)
        d.page.evaluate("location.hash = '#/threads/%s'" % TID)
        d.wait(".conv-note")
        d.page.wait_for_timeout(1500)
        d.page.fill(".conv-note", TEXT)
        d.page.click(".conv-send")
        d.until("document.querySelector('.conv-note').value === ''", 15000)
        d.page.wait_for_timeout(1000)
        replies = [b for u, b in posts if u.endswith("/api/triage/threads/%s/reply" % TID)]
        console = [u for u, b in posts if "/api/conversations/%s/note" % TID in u]
        check("Send posted Helm's /reply", len(replies) == 1, posts)
        body = json.loads(replies[0]) if replies else {}
        check("... as __redirect__ with his note", body.get("value") == "__redirect__" and body.get("note") == TEXT, body)
        check("... and never the console's /note", not console, console)
        t = api(st.home, "/api/triage/threads/" + TID)
        notes = [e for e in t.get("events", []) if e.get("kind") == "owner-note"]
        check("the thread holds his words", any(TEXT in (e.get("body") or "") for e in notes), notes[-2:])
        check("no page errors", not d.errors, d.errors)
        d.close()

        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
