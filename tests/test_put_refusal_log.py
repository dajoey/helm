"""A refused PUT /api/triage/threads names its batch and its caller, in the log and in the record.

Before: the api's only trace of a refused producer write was uvicorn's access line (a client ip) and
the client_error line, which carried the method, path, status and the FIRST rejected card's rule text,
and nothing else: not which thread ids were in the batch, not the User-Agent, not who the caller said
it was. Nine repeats of the PUT /api/triage/threads 400 card could not name the caller.

Now the one client_error line for a refused request also carries

    ... detail="<rule text>" client=<ip> ua="<User-Agent>" caller="<X-Caller>" [threads=<id,id,...>]

and the durable refused_requests row (14 days, survives a container recreation) carries the same
caller and the batch's thread ids, readable at GET /api/refusals (?thread_id= matches any id in the
batch). X-Caller is the caller's own label (curl and other-host callers have no other name); it is
logged, never trusted. Never the request body.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

os.environ["HELM_APP_TOKEN"] = "test-token"
TMP = tempfile.mkdtemp(prefix="helm-putlog-")
DB = os.path.join(TMP, "t.db")
os.environ["HELM_DB"] = DB
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ.pop("HELM_SENTRY_DSN", None)

import app.service as svc  # noqa: E402
from helm import store  # noqa: E402

client = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
LINE_RE = re.compile(r'^helm client_error: trace=(\S+) (\S+) (\S+) status=(\d+) kind=([a-z_]+) '
                     r'detail="([^"]*)" client=(\S+) ua="([^"]*)" caller="([^"]*)"(?: threads=(\S+))?$')

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def call(method, path, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        r = client.request(method, path, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("helm client_error:")]
    return r, lines, buf.getvalue()


def rows(where="", args=()):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM refused_requests " + where, args)]
    except sqlite3.OperationalError as exc:
        return "ERR " + str(exc)
    finally:
        con.close()


MARK = "BODY-MARKER-9c1e"
HDRS = {**AUTH, "User-Agent": "curl/8.5.0", "X-Caller": "host-a-cron"}

# --- the failing scenario: a producer batch the validator refuses outright
r, lines, raw = call("PUT", "/api/triage/threads", headers=HDRS, json={"threads": [
    {"thread_id": "t-bad-1", "title": "no summary " + MARK},
    {"thread_id": "t-bad-2", "title": "also bad"}]})
check("refused batch: 400", r.status_code == 400, f"{r.status_code} {r.text}")
check("refused batch: response body unchanged", r.json().get("rejected_count") == 2, r.text)
check("exactly one client_error line", len(lines) == 1, repr(lines))
m = LINE_RE.match(lines[0]) if len(lines) == 1 else None
check("line has the documented shape, caller fields included", m is not None, repr(lines))
if m:
    trace, meth, path, status, kind, detail, client_ip, ua, caller, threads = m.groups()
    check("status", status == "400" and kind == "validation_error", status)
    rule = r.json()["rejected"][0]["errors"][0]
    check("the failing rule text is in the line", rule[:40] in detail, f"{rule!r} / {detail!r}")
    check("client ip", client_ip == "testclient", client_ip)
    check("User-Agent", ua == "curl/8.5.0", ua)
    check("X-Caller", caller == "host-a-cron", caller)
    check("the batch's thread ids", threads == "t-bad-1,t-bad-2", threads)
check("the request body is not in stderr", MARK not in raw, raw)

# --- the durable record carries the same, and a thread-id filter finds any card of the batch
got = rows("WHERE route='/api/triage/threads' AND status=400")
check("one durable row", isinstance(got, list) and len(got) == 1, repr(got))
row = got[0] if isinstance(got, list) and got else {}
check("row: caller", row.get("caller") == "host-a-cron", repr(row))
check("row: batch ids", row.get("batch") == "t-bad-1,t-bad-2", repr(row))
check("row: first rejected id is the thread id", row.get("thread_id") == "t-bad-1", repr(row))
check("row: user agent", row.get("user_agent") == "curl/8.5.0", repr(row))
check("row: rule text in the detail", "thread" in (row.get("detail") or "") and "NOT stored" in row.get("detail", ""), repr(row))
check("row: no body text", MARK not in repr(row), repr(row))
for tid in ("t-bad-1", "t-bad-2"):
    r = client.get("/api/refusals?thread_id=" + tid, headers=AUTH)
    items = r.json().get("refusals", []) if r.status_code == 200 else []
    check(f"GET /api/refusals?thread_id={tid} finds the batch", len(items) == 1 and items[0]["caller"] == "host-a-cron", r.text)
r = client.get("/api/refusals?thread_id=t-bad", headers=AUTH)
check("a thread-id filter is not a prefix match", r.status_code == 200 and r.json()["refusals"] == [], r.text)

# --- no X-Caller: the field is empty, never absent; a body with no threads list still logs
r, lines, _ = call("PUT", "/api/triage/threads", headers=AUTH, json={"threads": "not-a-list"})
m = LINE_RE.match(lines[0]) if len(lines) == 1 else None
check("no X-Caller: line still matches, caller empty, no threads field",
      r.status_code == 400 and m is not None and m.group(9) == "" and m.group(10) is None, repr(lines))

# --- a long batch is bounded; a hostile header cannot split or forge a line
ids = ["t-many-%03d" % i for i in range(60)]
r, lines, raw = call("PUT", "/api/triage/threads", headers={**HDRS, "X-Caller": 'a"b\nhelm client_error: forged ' + "z" * 400},
                     json={"threads": [{"thread_id": i, "title": "bad"} for i in ids]})
m = LINE_RE.match(lines[0]) if len(lines) == 1 else None
check("hostile X-Caller: one line, same shape", r.status_code == 400 and m is not None and raw.count("\n") == 1, repr(raw[:300]))
if m:
    check("hostile X-Caller: bounded", len(m.group(9)) <= 200, str(len(m.group(9))))
    shown = m.group(10).split(",")
    check("a 60-card batch is cut, with the count kept", len(shown) <= 21 and shown[0] == "t-many-000"
          and shown[-1].startswith("+") , repr(shown[-3:]))
last = rows("ORDER BY id DESC LIMIT 1")[0]
check("row: long batch bounded", len(last["batch"]) <= 200 and len(last["caller"]) <= 200, repr({k: len(str(v)) for k, v in last.items()}))

# --- a 2xx PUT writes nothing; a refusal of another route carries the caller too
ok = {"thread_id": "t-ok-1", "title": "A card", "summary": "s " * 10, "category": "Test", "urgency": "this-week",
      "state": "needs-you",
      "ask": {"kind": "choice", "prompt": "Pick one", "requires": ["decision"], "options": [
          {"label": "Renew the cert and rotate the key", "value": "do", "effect": "It gets done"},
          {"label": "Leave it alone for now", "value": "no", "effect": "Nothing happens"}]},
      "sources": [{"source": "test", "id": "t-ok-1", "note": ""}]}
n = len(rows())
r, lines, _ = call("PUT", "/api/triage/threads", headers=HDRS, json={"threads": [ok]})
check("accepted PUT: 200, no line, no row", r.status_code == 200 and lines == [] and len(rows()) == n, f"{r.status_code} {lines}")
r, lines, _ = call("POST", "/api/triage/threads/t-nope-9/note", headers=HDRS, json={"actor": "x", "note": "n"})
m = LINE_RE.match(lines[0]) if len(lines) == 1 else None
check("a 404 on another route names client, ua and caller too",
      m is not None and m.group(8) == "curl/8.5.0" and m.group(9) == "host-a-cron" and m.group(10) is None, repr(lines))
r, lines, _ = call("GET", "/api/triage/threads")
m = LINE_RE.match(lines[0]) if len(lines) == 1 else None
check("a 401 names the client too", r.status_code == 401 and m is not None and m.group(7) == "testclient", repr(lines))

# --- the migration: an existing database (the table without the new columns) gains them, once
old = os.path.join(TMP, "old.db")
con = sqlite3.connect(old)
con.execute("CREATE TABLE refused_requests (id INTEGER PRIMARY KEY, ts REAL NOT NULL, method TEXT NOT NULL DEFAULT '',"
            " route TEXT NOT NULL DEFAULT '', thread_id TEXT NOT NULL DEFAULT '', status INTEGER NOT NULL DEFAULT 0,"
            " client TEXT NOT NULL DEFAULT '', user_agent TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '',"
            " login TEXT NOT NULL DEFAULT '', principal TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')")
import time  # noqa: E402
con.execute("INSERT INTO refused_requests (ts, method, route, thread_id, status) VALUES (?,?,?,?,?)",
            (time.time(), "POST", "/x", "t-kept", 404))
con.commit()
con.close()
con = store.db(old)
store.init(con)
store.init(con)
cols = {r["name"] for r in con.execute("PRAGMA table_info(refused_requests)")}
check("migration adds caller and batch", {"caller", "batch"} <= cols, repr(cols))
check("migration keeps the old row", [r["thread_id"] for r in store.list_refusals(con)] == ["t-kept"])
store.record_refusal(con, "PUT", "/api/triage/threads", "t-a", 400, caller="c1", batch="t-a,t-b")
check("the new row stores caller and batch",
      [(r["caller"], r["batch"]) for r in store.list_refusals(con, thread_id="t-b")] == [("c1", "t-a,t-b")])
con.close()

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
