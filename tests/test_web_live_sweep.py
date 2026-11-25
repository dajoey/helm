"""The web role after the 2026-09-26 home cutover, first live sweep: what the first hours on the
live app found, offline, against a fake api (the test_web_role.py pattern).

Run: python3 tests/test_web_live_sweep.py   (from the repo root)

1. Discovery: the page's own JavaScript errors reached nobody. POST /api/client-error takes
   one report, logs it (redacted, capped) and hands it to the error tracker as its own issue per
   message and file, so the the error tracker sweep cards each NEW one to an agent. Identical reports
   are sent once per window, and a storm is capped. The role's own unhandled exceptions
   answer a JSON 500, never a traceback, and go to the error tracker too (it had no SDK at all).
2. (retired with the home console: a console task with no Helm card made the stream ask the api
   every 3 s; there is no console view any more, so a 404 is simply the answer,
   tests/test_conversation_gone_stream.py.)
3. Nothing was compressed. JSON and the shell go out
   gzipped when the browser asks; event streams, attachments and media never are.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "live-sweep-test-token-7d2a"
os.environ["HELM_APP_TOKEN"] = TOKEN
os.environ["HELM_API_URL"] = "http://api.test"
os.environ["HELM_SITE"] = "home"
os.environ.pop("HELM_WEB_REQUIRE_AUTH_HEADER", None)
os.environ.pop("HELM_SENTRY_DSN", None)

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.web as web  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail else ""))


# ---------------------------------------------------------------- fakes
API_SEEN = []
HELD = "t-has-a-card"
BIG = {"threads": [{"thread_id": "t-%d" % i, "title": "a thread title " * 4, "state": "done"} for i in range(200)]}


async def api(request: Request):
    API_SEEN.append((request.method, request.url.path))
    if request.url.path == "/api/triage/threads":
        return JSONResponse(BIG)
    if request.url.path.startswith("/att/"):
        return Response(b"\x89PNG" + b"0" * 4000, media_type="image/png")
    return JSONResponse({"ok": True, "path": request.url.path})


web.UPSTREAM_TRANSPORT = httpx.ASGITransport(app=Starlette(routes=[Route("/{p:path}", api, methods=["GET", "POST"])]))


class FakeSdk:
    def __init__(self):
        self.messages, self.exceptions = [], []

    def capture_message(self, msg, level=None, scope=None, **kw):   # sentry_sdk 2.x: fingerprint/extras/tags as scope kwargs
        self.messages.append({"msg": msg, "level": level, "fingerprint": kw.get("fingerprint"),
                              "extras": dict(kw.get("extras") or {}), "tags": dict(kw.get("tags") or {})})

    def capture_exception(self, exc, scope=None, **kw):
        self.exceptions.append(exc)


AUTH = {"X-Forwarded-User": "owner"}
LINES = []
_orig_log = web._log
web._log = lambda m: (LINES.append(m), _orig_log(m))
c = TestClient(web.app, client=("127.0.0.1", 40000), raise_server_exceptions=False)

print("1. discovery: the page's errors and the role's own")
check("no DSN in env -> the web role's SDK is off", getattr(web, "sentry_sdk", "missing") is None, repr(getattr(web, "sentry_sdk", "missing")))
fake = FakeSdk()
web.sentry_sdk = fake
rep = {"kind": "error", "msg": "TypeError: x is undefined", "src": "https://helm.example.com/board.js", "line": 212, "col": 9,
       "stack": "TypeError: x is undefined\n    at render (https://helm.example.com/board.js:212:9)", "view": "#/board", "ua": "Mozilla/5.0 test"}
r = c.post("/api/client-error", json=rep, headers=AUTH)
check("a report is accepted (204)", r.status_code == 204, (r.status_code, r.text[:200]))
check("it is logged as one CLIENT-ERROR line with the login, view and place (note N1: the app's view logs the class, not the text)",
      any(l.startswith("CLIENT-ERROR") and "owner" in l and "#/board" in l and "board.js:212" in l and "TypeError" in l and "x is undefined" not in l for l in LINES), LINES[-3:])
check("it reaches the error tracker as the class name only (note N1: the app's fence)",
      len(fake.messages) == 1 and "TypeError" in fake.messages[0]["msg"] and "x is undefined" not in fake.messages[0]["msg"], fake.messages)
fp = fake.messages[0]["fingerprint"] if fake.messages else None
check("its fingerprint groups by message and file, not by the tab or the line",
      fp and fp[0] == "helm-web-client" and "board.js" in " ".join(fp) and "212" not in " ".join(fp), fp)
r = c.post("/api/client-error", json=rep, headers=AUTH)
check("the same report again inside the window is not re-sent", r.status_code == 204 and len(fake.messages) == 1, len(fake.messages))
for i in range(60):
    c.post("/api/client-error", json=dict(rep, msg="Error: storm %d" % i), headers=AUTH)
check("a storm is capped per minute", 1 < len(fake.messages) <= web.CLIENT_ERROR_MAX_PER_MIN + 1, len(fake.messages))
check("a storm is capped in the log too", sum(1 for l in LINES if l.startswith("CLIENT-ERROR")) <= web.CLIENT_ERROR_MAX_PER_MIN + 1,
      sum(1 for l in LINES if l.startswith("CLIENT-ERROR")))
r = c.post("/api/client-error", json=rep)
check("no login header -> 401, like every other path", r.status_code == 401, r.status_code)
r = c.post("/api/client-error", content=b"x" * (web.CLIENT_ERROR_MAX_BODY + 10), headers=dict(AUTH, **{"Content-Type": "application/json"}))
check("an oversized report is refused (413)", r.status_code == 413, r.status_code)
r = c.post("/api/client-error", content=b"not json", headers=dict(AUTH, **{"Content-Type": "application/json"}))
check("a report that is not JSON is refused (400)", r.status_code == 400, r.status_code)
n_api = len(API_SEEN)
c.post("/api/client-error", json=dict(rep, msg="Error: not proxied"), headers=AUTH)
check("a report never reaches the api role", len(API_SEEN) == n_api, API_SEEN[n_api:])
long = dict(rep, msg="E" * 3000, stack="s" * 10000)   # under the 16 KB body cap, over every field cap
web._client_err_reset()
n_lines = len(LINES)
c.post("/api/client-error", json=long, headers=AUTH)
new = [l for l in LINES[n_lines:] if l.startswith("CLIENT-ERROR")]
check("long fields are capped before they are logged", len(new) == 1 and len(new[0]) < 1200, [(len(l), l[:100]) for l in new])
check("...and before they reach the error tracker", len(fake.messages[-1]["msg"]) <= 400 and all(len(str(v)) <= 4100 for v in fake.messages[-1]["extras"].values()),
      [len(str(v)) for v in fake.messages[-1]["extras"].values()])

real_meta = web._meta


async def boom(request):
    raise RuntimeError("web role boom " + TOKEN)
web._meta = boom
r = c.get("/api/meta", headers=AUTH)
web._meta = real_meta
check("an unhandled error in the role answers a JSON 500", r.status_code == 500 and r.headers.get("content-type", "").startswith("application/json"), (r.status_code, r.text[:200]))
check("...that carries no traceback and no token", TOKEN not in r.text and "Traceback" not in r.text, r.text[:300])
check("...and reaches the error tracker", fake.exceptions and "web role boom" in str(fake.exceptions[-1]), fake.exceptions)
check("...and is logged, redacted", any(l.startswith("WEB-ERROR") and "[token]" in l and TOKEN not in l for l in LINES), LINES[-2:])

print("1b. review follow-ups (PR #9 round 1)")
web._client_err_reset()
n_lines = len(LINES)
c.post("/api/client-error", json=dict(rep, msg="Error: \x1b[31mred\x07 bell\x00nul " + TOKEN, src="https://h/x\x1b[2Jboard.js"), headers=AUTH)
new = [l for l in LINES[n_lines:] if l.startswith("CLIENT-ERROR")]
check("control characters never reach the log line", new and not any(ch in new[0] for ch in "\x1b\x07\x00"), [repr(l[:160]) for l in new])
fp = " ".join(fake.messages[-1]["fingerprint"] or [])
check("the the error tracker fingerprint never carries the token (note N1: the app's view's message is the class name, the src is still scrubbed)",
      TOKEN not in fp and "Error" in fp, fp)
check("...and the message too", TOKEN not in fake.messages[-1]["msg"], fake.messages[-1]["msg"])
import subprocess as _sp
probe = _sp.run([sys.executable, "-c", "import os,sys; sys.path.insert(0, os.getcwd()); os.environ['HELM_SENTRY_DSN']='http://k@127.0.0.1:9/1'; "
                 "import app.web as w; print(w.sentry_sdk.get_client().options.get('max_request_body_size'))"],
                capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                env=dict(os.environ))
check("with a DSN the SDK never attaches request bodies (replies, notes, reports)", probe.stdout.strip() == "never", (probe.stdout, probe.stderr[-300:]))
req = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "requirements.txt")).read()
import re as _re
m = _re.search(r"^starlette>=([0-9]+)\.([0-9]+)", req, _re.M)
check("requirements.txt floors Starlette at 1.5 (GZipMiddleware exclude_content_types)", m and (int(m.group(1)), int(m.group(2))) >= (1, 5), m and m.group(0))
sw = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "sw.js")).read()
check("the service worker's shell carries /errors.js", '"/errors.js"' in sw)
check("a .gz attachment is never compressed twice", "application/gzip" in web.GZIP_SKIP, web.GZIP_SKIP)

print("3. compression")
r = c.get("/api/triage/threads", headers=dict(AUTH, **{"Accept-Encoding": "gzip"}))
check("JSON goes out gzipped when the browser asks", r.headers.get("content-encoding") == "gzip", dict(r.headers))
check("...and decodes to the same body", r.json() == BIG)
raw = c.get("/api/triage/threads", headers=dict(AUTH, **{"Accept-Encoding": "identity"}))
check("...and not when it does not ask", raw.headers.get("content-encoding") is None and raw.json() == BIG, dict(raw.headers))
r = c.get("/", headers=dict(AUTH, **{"Accept-Encoding": "gzip"}))
check("the shell (index.html) is gzipped", r.headers.get("content-encoding") == "gzip", dict(r.headers))
r = c.get("/att/x.png", headers=dict(AUTH, **{"Accept-Encoding": "gzip"}))
check("an attachment is never recompressed", r.headers.get("content-encoding") is None, dict(r.headers))
check("...and still leaves sandboxed", "sandbox" in (r.headers.get("content-security-policy") or ""), dict(r.headers))
web.CONV_WINDOW_S, web.CONV_POLL_S = 0.0, 0.0
with c.stream("GET", "/api/conversations/%s/stream" % HELD, headers=dict(AUTH, **{"Accept-Encoding": "gzip"})) as s:
    first = next(s.iter_lines(), "")
    enc = s.headers.get("content-encoding")
check("an event stream is never compressed (it would buffer)", enc is None, enc)
check("...and still streams its frame", first.startswith("data: "), first[:80])

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
