"""4xx answers are logged and counted (wiki Projects/Helm Error Observability,
component 8).

Before: a request the API refused (401, 400, 404, 405, 409) left nothing but uvicorn's access
line, so nobody could tell WHY a caller was refused without the caller's own logs. Now every
4xx writes one greppable stderr line,

    helm client_error: trace=<id> <METHOD> <path> status=<n> kind=<kind> detail="<why>"

and counts in helm_client_errors_total{method,path,status,kind}. The line never carries the
Authorization value, the site token or the request body, and the metric labels stay bounded
by the route table and a fixed set of kinds.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

os.environ["HELM_APP_TOKEN"] = "test-token"  # force: a persisted real token must never leak into tests
TMP = tempfile.mkdtemp(prefix="helm-4xx-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ.pop("HELM_SENTRY_DSN", None)

import app.service as svc  # noqa: E402

client = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
# The caller fields (client, ua, caller, threads) follow the detail: tests/test_put_refusal_log.py.
LINE_RE = re.compile(r'^helm client_error: trace=(\S+) (\S+) (\S+) status=(\d+) kind=([a-z_]+) detail="([^"]*)"'
                     r' client=\S+ ua="[^"]*" caller="[^"]*"(?: threads=\S+)?$')

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def call(method, path, **kw):
    """(response, [client_error lines]) for one request."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        r = client.request(method, path, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("helm client_error:")]
    return r, lines, buf.getvalue()


def one_line(name, r, lines, status, kind, method, path):
    check(f"{name}: status {status}", r.status_code == status, str(r.status_code))
    check(f"{name}: exactly one client_error line", len(lines) == 1, repr(lines))
    if len(lines) != 1:
        return None
    m = LINE_RE.match(lines[0])
    check(f"{name}: line is the documented shape", m is not None, lines[0])
    if not m:
        return None
    trace, meth, p, st, k, detail = m.groups()
    check(f"{name}: trace matches X-Trace-ID", trace == r.headers.get("x-trace-id"),
          f"{trace} vs {r.headers.get('x-trace-id')}")
    check(f"{name}: method", meth == method, meth)
    check(f"{name}: path", p == path, p)
    check(f"{name}: status in line", st == str(status), st)
    check(f"{name}: kind", k == kind, k)
    check(f"{name}: detail bounded", len(detail) <= 200, str(len(detail)))
    return detail


def put_thread(tid):
    return client.put("/api/triage/threads", headers=AUTH, json={"threads": [{
        "thread_id": tid, "title": "A card", "summary": "s " * 10,
        "category": "Test", "urgency": "this-week", "state": "needs-you",
        "ask": {"kind": "choice", "prompt": "Pick one", "requires": ["decision"], "options": [
            {"label": "Renew the cert and rotate the key", "value": "do", "effect": "It gets done"},
            {"label": "Leave it alone for now", "value": "no", "effect": "Nothing happens"}]},
        "sources": [{"source": "test", "id": tid, "note": ""}],
    }]})


# --- 401, no Authorization header at all
r, lines, _ = call("GET", "/api/triage/threads")
d = one_line("401 missing", r, lines, 401, "unauthorized", "GET", "/api/triage/threads")
check("401 missing: detail says the header is missing", d == "missing Authorization header", repr(d))
check("401 missing: response body unchanged", r.json() == {"ok": False, "detail": "unauthorized"}, r.text)

# --- 401, a bearer that matches nothing: the token value never reaches the log
SECRET = "secret-invalid-token-12345"
r, lines, raw = call("GET", "/api/triage/threads", headers={"Authorization": "Bearer " + SECRET})
d = one_line("401 invalid", r, lines, 401, "unauthorized", "GET", "/api/triage/threads")
check("401 invalid: detail says the token matched no caller", d == "Authorization token matches no caller", repr(d))
check("401 invalid: the bearer value is not in stderr", SECRET not in raw, raw)
r, lines, _ = call("GET", "/api/triage/threads", headers={"Authorization": "Basic " + SECRET})
check("401 basic: not-Bearer detail, no value", len(lines) == 1 and 'detail="Authorization header is not Bearer"'
      in lines[0] and SECRET not in lines[0], repr(lines))

# --- a detail that quotes a bearer value (an exception echoing a header) is redacted
check("bearer text in a detail is redacted",
      svc._log_text("upstream said: Bearer abc.def-123 was refused") == "upstream said: Bearer [REDACTED] was refused")

# --- 405 on an open route
r, lines, _ = call("POST", "/api/meta")
one_line("405", r, lines, 405, "method_not_allowed", "POST", "/api/meta")

# --- 400, a malformed PUT: the reason is logged, the payload is not
MARK = "PAYLOAD-BODY-MARKER-7f3a"
r, lines, raw = call("PUT", "/api/triage/threads", headers=AUTH,
                     json={"threads": [{"title": "missing-id " + MARK, "summary": MARK}]})
d = one_line("400 put", r, lines, 400, "validation_error", "PUT", "/api/triage/threads")
check("400 put: detail names the failed rule", d is not None and "thread_id" in d, repr(d))
check("400 put: the request body is not in stderr", MARK not in raw, raw)
check("400 put: response body still intact for the client",
      r.json().get("rejected_count") == 1 and "rejected" in r.json(), r.text)

# --- 400 from a StoreError through _rej
r, lines, _ = call("POST", "/api/triage/threads/create", headers=AUTH, json={})
if r.status_code == 400:
    d = one_line("400 _rej", r, lines, 400, "validation_error", "POST", "/api/triage/threads/create")
    check("400 _rej: detail is the StoreError's", d is not None and d == r.json()["detail"][:200], repr(d))
else:
    check("400 _rej: empty create is refused 400", False, f"{r.status_code} {r.text}")

# --- 404, no such thread
r, lines, _ = call("POST", "/api/triage/threads/t-nope/claims", headers=AUTH,
                   json={"executor_id": "lane-test", "runtime": "agent"})
d = one_line("404", r, lines, 404, "not_found", "POST", "/api/triage/threads/t-nope/claims")
check("404: detail", d == "no such thread: t-nope", repr(d))

# --- 404, an unrouted path: Starlette's plain-text body becomes the detail
r, lines, _ = call("GET", "/api/nowhere", headers=AUTH)
d = one_line("404 unrouted", r, lines, 404, "not_found", "GET", "/api/nowhere")
check("404 unrouted: plain-text detail", d == "Not Found", repr(d))

# --- 409, a second live claim
check("seed thread stored", put_thread("t-4xx-1").status_code == 200)
r = client.post("/api/triage/threads/t-4xx-1/claims", headers=AUTH,
                json={"executor_id": "lane-test", "runtime": "agent"})
check("first claim 201", r.status_code == 201, r.text)
r, lines, _ = call("POST", "/api/triage/threads/t-4xx-1/claims", headers=AUTH,
                   json={"executor_id": "lane-test", "runtime": "agent"})
d = one_line("409", r, lines, 409, "conflict", "POST", "/api/triage/threads/t-4xx-1/claims")
check("409: detail", d is not None and "already claimed" in d, repr(d))
check("409: body extras still reach the client", "claim_id" in r.json(), r.text)

# --- the caller's own X-Trace-ID is the trace in the line
r, lines, _ = call("GET", "/api/triage/threads", headers={"X-Trace-ID": "tr-caller-42"})
check("caller trace id is logged", len(lines) == 1 and " trace=tr-caller-42 " in lines[0], repr(lines))

# --- the site token in a path is redacted; a newline in a path cannot split the line
r, lines, raw = call("GET", "/api/triage/threads/test-token/nothing-here", headers=AUTH)
check("token in path: one line", len(lines) == 1, repr(lines))
check("token in path: redacted", "test-token" not in raw and "[REDACTED]" in raw, raw)
r, lines, raw = call("GET", "/api/x%0Afake%20helm%20client_error:%20injected", headers=AUTH)
check("newline in path: one line only", len(lines) == 1 and raw.count("\n") == 1, repr(raw))
r, lines, raw = call("GET", "/api/triage/threads", headers={"X-Trace-ID": "a\"b c"})
check("hostile trace id cannot break the shape", len(lines) == 1 and LINE_RE.match(lines[0]) is not None,
      repr(lines))

# --- a long detail is cut at 200 chars
r, lines, _ = call("GET", "/api/" + "p" * 400, headers=AUTH)
m = LINE_RE.match(lines[0]) if len(lines) == 1 else None
check("long path bounded", m is not None and len(m.group(3)) <= 200, repr(lines))

# --- a 2xx writes nothing
r, lines, _ = call("GET", "/api/triage/threads", headers=AUTH)
check("2xx: no client_error line", r.status_code == 200 and lines == [], repr(lines))

# --- the counter: bounded labels, one series per (method, route, status, kind)
r = client.get("/metrics")
text = r.text
check("metrics: HELP line", "# TYPE helm_client_errors_total counter" in text, text[:400])
check("metrics: 401 series counts every unauthorized GET",
      'helm_client_errors_total{method="GET",path="/api/triage/threads",status="401",kind="unauthorized"} 5' in text,
      "\n".join(ln for ln in text.splitlines() if ln.startswith("helm_client_errors_total")))
check("metrics: path params collapse to :id",
      'helm_client_errors_total{method="POST",path="/api/triage/threads/:id/claims",status="409",kind="conflict"} 1'
      in text)
check("metrics: unrouted paths share one label",
      'path="/<unmatched>",status="404",kind="not_found"' in text and "p" * 50 not in text)
check("metrics: no detail text in labels", "already claimed" not in text and "no caller" not in text)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
