"""Existing Helm images still load under the image rules (finding 5).

Run: python3 tests/test_screens_images.py

The fixture starts the real api and web roles with a throwaway threads DB. A thread is seeded at run time with one
image attachment (a real PNG through the api's attach route), then the thread page is opened in a real browser:

  the attachment's bytes load from /api/triage/threads/<id>/attachments/<att> with 200 (same origin)
  the card visual is drawn by web/conversation.js and the image is not broken
  every request the page makes is same-origin (no outside origin at all)
  the shell document carries Content-Security-Policy: img-src 'self' data: blob:

Every value is made up.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
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
from test_screens_drive import Drive  # noqa: E402

passed = failed = 0
WIDE = (999, 1392)
TID = "t-images-ok1"


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def api(site, path, body=None, method="GET"):
    req = urllib.request.Request(site.api_url + path,
                                 data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": "Bearer " + fixture.TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode() or "{}")


# A 1x1 white PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        w = st.home
        api(w, "/api/triage/threads", {"threads": [{"thread_id": TID, "title": "Card visual thread", "summary": "Visuals",
                                                    "category": "Contacts", "urgency": "whenever", "state": "working",
                                                    "events": [{"ts": 1, "source": "agent", "kind": "update", "body": "made a chart"}]}]}, "PUT")
        att = api(w, "/api/triage/threads/%s/attach" % TID,
                  {"filename": "chart.png", "mime": "image/png", "uploader": "owner", "caption": "the chart",
                   "content_base64": base64.b64encode(PNG).decode()}, "POST")
        check("precondition: the attachment took", isinstance(att, dict) and att.get("att_id"), att)

        browser = p.chromium.launch(**shoot.launch_args())
        requests_log: list = []
        responses_log: list = []
        d = Drive(browser, w, WIDE, "#/threads/" + TID)
        d.page.on("request", lambda r: requests_log.append(r.url))
        d.page.on("response", lambda r: responses_log.append((r.url, r.status)))
        d.wait(".conv-visuals .visual-img", timeout=20000)
        d.page.wait_for_timeout(400)

        att_url = w.web_url + "/api/triage/threads/%s/attachments/%s" % (TID, att["att_id"])
        hits = [(u, s) for u, s in responses_log if u.split("?")[0] == att_url]
        check("the attachment bytes load from the attachments route with 200", hits and hits[0][1] == 200, hits)
        img = d.eval("(() => { const i = document.querySelector('.conv-visuals .visual-img'); return i ? {src: i.getAttribute('src'), w: i.naturalWidth, complete: i.complete} : null; })()")
        check("the card visual is drawn and the image decoded (not broken)", img and img["src"] == "/api/triage/threads/%s/attachments/%s" % (TID, att["att_id"]) and img["w"] == 1 and img["complete"], img)
        origins = set()
        for u in requests_log:
            m = urllib.parse.urlsplit(u)
            origins.add((m.scheme, m.netloc))
        check("every request the page makes is same-origin", origins <= {(urllib.parse.urlsplit(w.web_url).scheme, urllib.parse.urlsplit(w.web_url).netloc)}, origins)
        r = d.page.goto(w.web_url + "/")      # the shell itself (a different document, so the response comes back)
        check("the shell carries the image Content-Security-Policy", r.headers.get("content-security-policy") == "img-src 'self' data: blob:", r.headers.get("content-security-policy"))
        check("no page errors", d.errors == [], d.errors)
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
