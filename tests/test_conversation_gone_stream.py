"""The conversation stream for a thread the site does not hold stops asking the api.

Run: python3 tests/test_conversation_gone_stream.py   (from the repo root)

The work api's error watcher filed `GET /api/triage/threads/:id/conversation 404` (8 in 10
minutes). A tab opened on a thread that is gone (deleted, or a stale link) opens the push stream
(/api/conversations/<id>/stream) at the same moment as its first read. The page then closes
its EventSource, but the web role's stream loop never noticed: it kept reading the api for its
whole 55 s window, a 404 every read, and it logged an error for each.

1. A stream opened on a missing thread answers 404 itself (an EventSource that gets a non-200
   does not retry), after asking the api once.
2. A stream that was running when the thread went away ends on the api's 404 instead of
   reading it again and again.
3. A thread the api holds still streams its frame, and a stream still rides out an api that is
   down (502 on the first read is not "gone").
Work site, synthetic data.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "gone-stream-test-token-5b1c"
os.environ["HELM_APP_TOKEN"] = TOKEN
os.environ["HELM_API_URL"] = "http://api.test"
os.environ.pop("HELM_WEB_REQUIRE_AUTH_HEADER", None)
os.environ.pop("HELM_SENTRY_DSN", None)

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.web as web  # noqa: E402

PASS = FAIL = 0
GONE = "t-gone-for-good"
HELD = "t-held-here"
DOWN = "t-api-down"
VANISHING = "t-vanishes-mid-stream"
SEEN = []
VANISH_AFTER = [1]   # the vanishing thread answers 200 this many times, then 404


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail else ""))


def thread_payload(tid):
    return {"thread_id": tid, "task_id": None, "task_status": None, "thread_state": "needs-you", "ask": None,
            "events": [], "pending_notes": [], "properties": {}, "review_state": None}


async def api(request: Request):
    path = request.url.path
    SEEN.append(path)
    if path == "/api/triage/threads/%s/conversation" % HELD:
        return JSONResponse(thread_payload(HELD))
    if path == "/api/triage/threads/%s/conversation" % DOWN:
        return JSONResponse({"detail": "boom"}, status_code=500)
    if path == "/api/triage/threads/%s/conversation" % VANISHING:
        if VANISH_AFTER[0] > 0:
            VANISH_AFTER[0] -= 1
            return JSONResponse(thread_payload(VANISHING))
        return JSONResponse({"detail": "no such thread"}, status_code=404)
    return JSONResponse({"ok": False, "detail": "no such thread"}, status_code=404)


web.UPSTREAM_TRANSPORT = httpx.ASGITransport(app=Starlette(routes=[Route("/{p:path}", api, methods=["GET", "POST"])]))
AUTH = {"X-Forwarded-User": "owner"}
c = TestClient(web.app, client=("127.0.0.1", 40000), raise_server_exceptions=False)
LINES = []
_orig_log = web._log
web._log = lambda m: (LINES.append(m), _orig_log(m))

# the stream's poll runs fast, so a loop that keeps asking shows itself inside the window
web.CONV_POLL_S = 0.02
web.CONV_WINDOW_S = 1.0


def conv_reads(tid):
    return sum(1 for p in SEEN if p == "/api/triage/threads/%s/conversation" % tid)


print("1. a stream opened on a missing thread")
SEEN.clear()
t0 = time.monotonic()
r = c.get("/api/conversations/%s/stream" % GONE, headers=AUTH)
took = time.monotonic() - t0
check("it answers 404, so the browser's EventSource gives up", r.status_code == 404, (r.status_code, r.text[:120]))
check("it answers at once, not after the stream window", took < 0.5, took)
check("the api was asked once", conv_reads(GONE) == 1, conv_reads(GONE))
check("a gone thread is not an error line in the log", not any("CONV-STREAM-ERROR" in l and GONE in l for l in LINES), LINES[-3:])

print("2. a thread that goes away while it streams")
SEEN.clear()
VANISH_AFTER[0] = 1
t0 = time.monotonic()
with c.stream("GET", "/api/conversations/%s/stream" % VANISHING, headers=AUTH) as s:
    body = "".join(s.iter_text())
took = time.monotonic() - t0
check("its first frame is delivered", body.startswith("data: "), body[:80])
check("the stream ends on the 404 instead of riding out the window", took < 0.6, took)
check("the api is not asked again and again", conv_reads(VANISHING) <= 3, conv_reads(VANISHING))
check("no error line for it", not any("CONV-STREAM-ERROR" in l and VANISHING in l for l in LINES), LINES[-3:])

print("3. the streams that should keep going")
SEEN.clear()
with c.stream("GET", "/api/conversations/%s/stream" % HELD, headers=AUTH) as s:
    first = next(s.iter_lines(), "")
check("a thread the api holds streams its frame", first.startswith("data: ") and HELD in first, first[:100])
SEEN.clear()
t0 = time.monotonic()
with c.stream("GET", "/api/conversations/%s/stream" % DOWN, headers=AUTH) as s:
    status = s.status_code
    body = "".join(s.iter_text())
took = time.monotonic() - t0
check("an api that is down is not 'gone': the stream opens and keeps trying", status == 200, status)
check("...for its whole window", took >= 0.9, took)
check("...and the failure is logged", any("CONV-STREAM-ERROR" in l and DOWN in l for l in LINES), LINES[-3:])

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
