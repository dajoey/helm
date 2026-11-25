"""A refused request leaves a durable record of WHO sent it.

Before: the only evidence of a 4xx was uvicorn's access line and the stderr client_error line,
both in the container's stdout, which dies with the container. A deploy recreates helm-app-api
several times a day, so a caller that got a 404 for a thread Helm does not hold could not be
named an hour later (the note-404 card was settled "one-off" four times for that reason).

Now every authenticated 4xx also writes one row to `refused_requests` in the data volume's
database: method, route, thread id from the path, status, client ip, User-Agent, the body's
`actor`, the Authentik login and the token principal, and a timestamp. The table keeps 14 days
and a bounded row count; it survives an api restart; GET /api/refusals reads it (bearer only,
not proxied by the web role). A request that succeeds writes no row, and the answer a refused
caller gets does not change.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

os.environ["HELM_APP_TOKEN"] = "test-token"
TMP = tempfile.mkdtemp(prefix="helm-refusal-")
DB = os.path.join(TMP, "t.db")
os.environ["HELM_DB"] = DB
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ.pop("HELM_SENTRY_DSN", None)

import app.service as svc  # noqa: E402
from helm import store  # noqa: E402

client = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def rows(where="", args=()):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM refused_requests " + where, args)]
    except sqlite3.OperationalError as exc:
        return "ERR " + str(exc)
    finally:
        con.close()


def put_thread(tid):
    return client.put("/api/triage/threads", headers=AUTH, json={"threads": [{
        "thread_id": tid, "title": "A card", "summary": "s " * 10,
        "category": "Test", "urgency": "this-week", "state": "needs-you",
        "ask": {"kind": "choice", "prompt": "Pick one", "requires": ["decision"], "options": [
            {"label": "Renew the cert and rotate the key", "value": "do", "effect": "It gets done"},
            {"label": "Leave it alone for now", "value": "no", "effect": "Nothing happens"}]},
        "sources": [{"source": "test", "id": tid, "note": ""}],
    }]})


# --- the failing scenario: a note to a thread Helm does not hold
t0 = time.time()
r = client.post("/api/triage/threads/t-nope-1/note", headers={**AUTH, "User-Agent": "producer-pump/9"},
                json={"actor": "pump-x", "note": "NOTE-BODY-MARKER-31c"})
check("unknown thread: still a 404 with the same body", r.status_code == 404 and r.json().get("ok") is False, r.text)
got = rows()
check("a row was written", isinstance(got, list) and len(got) == 1, repr(got))
row = got[0] if isinstance(got, list) and got else {}
check("row: method", row.get("method") == "POST", repr(row))
check("row: route is the bounded template label", row.get("route") == "/api/triage/threads/:id/note", repr(row))
check("row: thread id from the path", row.get("thread_id") == "t-nope-1", repr(row))
check("row: status", row.get("status") == 404, repr(row))
check("row: client ip", bool(row.get("client")), repr(row))
check("row: body actor", row.get("actor") == "pump-x", repr(row))
check("row: user agent", row.get("user_agent") == "producer-pump/9", repr(row))
check("row: timestamp", t0 - 1 <= (row.get("ts") or 0) <= time.time() + 1, repr(row))
check("row: the note text is not kept", "NOTE-BODY-MARKER-31c" not in repr(row), repr(row))
check("row: no Authorization value", "test-token" not in repr(row), repr(row))

# --- a valid thread writes no row
put_thread("t-real-1")
before = len(rows())
r = client.post("/api/triage/threads/t-real-1/note", headers=AUTH, json={"actor": "pump-x", "note": "hello"})
check("valid thread: accepted", r.status_code == 202, r.text)
check("valid thread: no new row", len(rows()) == before, repr(rows()))
r = client.get("/api/triage/threads/t-real-1", headers=AUTH)
check("2xx GET: no new row", r.status_code == 200 and len(rows()) == before)

# --- the other actor key, the login header, and a body that is not an object
r = client.post("/api/triage/threads/t-nope-2/note", headers={**AUTH, "X-Helm-Actor": "someone"},
                json={"author": "auth-y", "note": "n"})
r2 = client.post("/api/triage/threads/t-nope-3/note", headers=AUTH, content=b"not json")
got = {x["thread_id"]: x for x in rows()}
check("author key is kept as the actor", got.get("t-nope-2", {}).get("actor") == "auth-y", repr(got.get("t-nope-2")))
check("Authentik login is kept", got.get("t-nope-2", {}).get("login") == "someone", repr(got.get("t-nope-2")))
check("a 400 is recorded too, with no actor", r2.status_code == 400 and got.get("t-nope-3", {}).get("status") == 400
      and got.get("t-nope-3", {}).get("actor") == "", repr(got.get("t-nope-3")))

# --- unauthenticated scanner probes leave no row (they are counted by method only)
n = len(rows())
client.get("/.aws/credentials")
client.get("/wp-login.php")
check("unauthenticated probe of an unrouted path writes no row", len(rows()) == n, repr(rows()))

# --- a 401 on a ROUTED path (wrong token, no header) writes no row: anyone who can reach the
# port could otherwise send 5000 bad-token requests and evict every real refusal record
n = len(rows())
for hdrs in ({"Authorization": "Bearer wrong"}, {}, {"Authorization": "Basic abc"}):
    for _ in range(3):
        rr = client.post("/api/triage/threads/t-x0/note", headers=hdrs, json={"actor": "scanner", "note": "n"})
        check("unauthenticated routed request: 401", rr.status_code == 401, rr.text)
check("a 401 on a routed path (wrong token / no header) writes no row", len(rows()) == n, repr(rows()[n:]))
client.get("/api/refusals")
check("an unauthenticated read of /api/refusals writes no row", len(rows()) == n, repr(rows()[n:]))

# --- the legacy token ("" principal) is authenticated: its refusals still record
rr = client.post("/api/triage/threads/t-legacy-1/note", headers=AUTH, json={"actor": "legacy", "note": "n"})
check("legacy-token refusal is still recorded",
      rr.status_code == 404 and len(rows("WHERE thread_id='t-legacy-1'")) == 1, repr(rows("WHERE thread_id='t-legacy-1'")))

# --- values are bounded and a long unknown path cannot grow the route column
r = client.post("/api/triage/threads/" + "x" * 5000 + "/note", headers={**AUTH, "User-Agent": "u" * 5000},
                json={"actor": "a" * 5000})
last = rows("ORDER BY id DESC LIMIT 1")[0]
check("long values are cut", all(len(str(last[k])) <= 300 for k in ("thread_id", "actor", "user_agent", "route")), repr({k: len(str(v)) for k, v in last.items()}))

# --- an unrouted path: the route label is the one bounded `/<unmatched>`, so the row itself must
# say which path the caller asked for or nobody can name the caller's mistake after the container
# is recreated (a second site's `GET /<unmatched> 404` card could not be diagnosed for that reason)
r = client.get("/api/some/route/nobody/serves?q=SECRET-QUERY-31c&x=1", headers={**AUTH, "User-Agent": "client-script/3"})
got = rows("WHERE route='/<unmatched>' ORDER BY id DESC LIMIT 1")
row = got[0] if isinstance(got, list) and got else {}
check("unrouted path: still a 404", r.status_code == 404, r.text)
check("unrouted path: a row was written under /<unmatched>", row.get("status") == 404, repr(got))
check("unrouted path: the row names the path", "/api/some/route/nobody/serves" in (row.get("detail") or ""), repr(row))
check("unrouted path: the query string is not kept", "SECRET-QUERY-31c" not in repr(row), repr(row))
check("unrouted path: the caller is still named", row.get("user_agent") == "client-script/3", repr(row))
r = client.get("/api/refusals", headers=AUTH)
check("unrouted path: GET /api/refusals shows the path",
      any("/api/some/route/nobody/serves" in (x.get("detail") or "") for x in r.json().get("refusals", [])), r.text[:400])
client.get("/z" * 400, headers=AUTH)
last = rows("WHERE route='/<unmatched>' ORDER BY id DESC LIMIT 1")[0]
check("unrouted path: a long path is cut", len(last["detail"]) <= 300, str(len(last["detail"])))
# a routed refusal keeps its own detail untouched
r = client.post("/api/triage/threads/t-nope-6/note", headers=AUTH, json={"actor": "z", "note": "n"})
check("routed refusal: detail is not given a path", "(path " not in (rows("WHERE thread_id='t-nope-6'")[0]["detail"] or ""),
      repr(rows("WHERE thread_id='t-nope-6'")))

# --- it reads back over the api, newest first, filtered
r = client.get("/api/refusals", headers=AUTH)
check("GET /api/refusals: 200", r.status_code == 200, r.text)
body = r.json() if r.status_code == 200 else {}
items = body.get("refusals", [])
check("GET /api/refusals: newest first", items and items[0]["id"] > items[-1]["id"], repr(items[:2]))
r = client.get("/api/refusals?thread_id=t-nope-1", headers=AUTH)
check("GET /api/refusals?thread_id= filters", r.status_code == 200 and [x["actor"] for x in r.json()["refusals"]] == ["pump-x"], r.text)
r = client.get("/api/refusals?since=%s" % (time.time() + 60), headers=AUTH)
check("GET /api/refusals?since= filters", r.status_code == 200 and r.json()["refusals"] == [], r.text)
check("GET /api/refusals needs the bearer", client.get("/api/refusals").status_code == 401)

# --- survives a restart (a new app instance over the same file) and migrates idempotently
client2 = TestClient(svc.build_app())
r = client2.get("/api/refusals?thread_id=t-nope-1", headers=AUTH)
check("readable by a fresh app over the same database", r.status_code == 200 and len(r.json()["refusals"]) == 1, r.text)
con = store.db(DB)
store.init(con)
store.init(con)
check("init is idempotent and keeps the rows", con.execute("SELECT COUNT(*) FROM refused_requests").fetchone()[0] == len(rows()))
con.close()

# --- retention: 14 days, and a row cap
con = store.db(DB)
old = time.time() - 15 * 86400
con.execute("INSERT INTO refused_requests (ts, method, route, thread_id, status) VALUES (?,?,?,?,?)",
            (old, "POST", "/api/triage/threads/:id/note", "t-old", 404))
con.commit()
con.close()
r = client.get("/api/refusals?thread_id=t-old", headers=AUTH)
check("a row past 14 days is not returned even before the next write prunes it",
      r.status_code == 200 and r.json()["refusals"] == [], r.text)
client.post("/api/triage/threads/t-nope-4/note", headers=AUTH, json={"actor": "z"})
check("a row older than 14 days is pruned on the next write", rows("WHERE thread_id='t-old'") == [], repr(rows("WHERE thread_id='t-old'")))
check("a recent row is kept", len(rows("WHERE thread_id='t-nope-1'")) == 1)

con = store.db(DB)
cap = store.REFUSALS_MAX
con.executemany("INSERT INTO refused_requests (ts, method, route, thread_id, status) VALUES (?,?,?,?,?)",
                [(time.time(), "POST", "/r", "t-fill", 404)] * (cap + 50))
con.commit()
con.close()
client.post("/api/triage/threads/t-nope-5/note", headers=AUTH, json={"actor": "z"})
n = len(rows())
check("the table is capped at REFUSALS_MAX", n <= cap, str(n))
check("the cap drops the oldest, keeps the newest", len(rows("WHERE thread_id='t-nope-5'")) == 1)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
