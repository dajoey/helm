"""The read API and identity, at both sites.

Run: python3 tests/test_read_api.py

GET /api/triage/threads/{id}, /api/triage/events?since=&until=&thread=&kind=&limit=,
/api/triage/search?q=&limit= (FTS5, kept in sync on write), /api/meta's shape, and the
tokens table + CLI: a per-caller token works, its principal is recorded on the events
it writes, revoking it stops it, and the legacy HELM_APP_TOKEN keeps working.
"""
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ.pop("HELM_MODULES", None)
TMP = tempfile.mkdtemp(prefix="helm-read-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")

import app.service as svc  # noqa: E402
from helm import store, tokens  # noqa: E402

client = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def put(tid, title, summary="", ask=None):
    t = {"thread_id": tid, "title": title, "summary": summary, "category": "Contacts",
         "urgency": "this-week", "state": "needs-you",
         "ask": ask or {"kind": "choice", "prompt": "Renew now?", "requires": ["decision"],
                        "options": [{"label": "Renew the cert today", "value": "renew", "effect": "It gets renewed"},
                                    {"label": "Leave it until Monday", "value": "wait", "effect": "Nothing happens yet"}]},
         "sources": [{"source": "test", "id": tid}]}
    return client.put("/api/triage/threads", json={"threads": [t]}, headers=AUTH)


# --- E2 meta shape
m = client.get("/api/meta").json()
for k in ("ok", "service", "version", "time", "auth", "modules", "live", "needs_you"):
    check("meta has %s" % k, k in m, m)
check("meta service is helm, live is true-ish (a count)", m["service"] == "helm" and isinstance(m["live"], int), m)

# --- E1 thread by id
r = put("r-1", "Pine Hollow PayTerm driver flapping", "The terminal reboots twice a day.")
check("fixture landed", r.status_code == 200 and r.json()["created"] == 1, r.json())
r = client.get("/api/triage/threads/r-1", headers=AUTH)
t = r.json()
check("GET thread by id", r.status_code == 200 and t["thread_id"] == "r-1" and t["title"].startswith("Pine Hollow"), t)
check("thread carries its events", isinstance(t["events"], list) and any(e["kind"] == "bot-ask" for e in t["events"]), t["events"])
check("ask + options present", t["ask"]["kind"] == "choice" and len(t["ask"]["options"]) == 2, t["ask"])
r = client.get("/api/triage/threads/nope", headers=AUTH)
check("unknown id is 404 {ok:false}", r.status_code == 404 and r.json()["ok"] is False, r.json())
r = client.get("/api/triage/threads/r-1")
check("thread by id needs the bearer", r.status_code == 401)
client.put("/api/triage/threads", json={"threads": [], "tombstones": ["r-1"]}, headers=AUTH)
r = client.get("/api/triage/threads/r-1", headers=AUTH)
check("a tombstoned thread reads as gone", r.status_code == 404, r.status_code)

# --- E1 events
t0 = time.time()
put("r-2", "Twin Rivers meter read, ticket 00100002", "Reads are stale since Tuesday.")
client.post("/api/triage/threads/r-2/reply", json={"value": "renew", "note": "Do the renewal, and tell them first."}, headers=AUTH)
client.post("/api/triage/threads/r-2/note", json={"note": "Chased the vendor by phone.", "actor": "owner"}, headers=AUTH)
put("r-3", "Backup job keeps failing on host-b", "The nightly rsync exits 23.")
r = client.get("/api/triage/events?since=%f" % (t0 - 1), headers=AUTH)
ev = r.json()
check("events since", r.status_code == 200 and ev["count"] >= 4 and ev["events"], ev)
check("events are ordered by ts", [e["ts"] for e in ev["events"]] == sorted(e["ts"] for e in ev["events"]))
check("events carry structured fields", all(k in ev["events"][0] for k in ("event_id", "ts", "source", "kind", "body", "thread_id", "title", "state")), ev["events"][0])
r = client.get("/api/triage/events?thread=r-2&kind=owner-note", headers=AUTH).json()
check("filter by thread + kind", r["count"] == 1 and r["events"][0]["body"].startswith("Do the renewal"), r)
r = client.get("/api/triage/events?thread=r-2&kind=note", headers=AUTH).json()
check("a /note event is kind 'note'", r["count"] == 1 and "vendor" in r["events"][0]["body"], r)
r = client.get("/api/triage/events?since=%f&until=%f" % (t0 - 1, t0 - 0.5), headers=AUTH).json()
check("until bounds the range", r["count"] == 0, r)
r = client.get("/api/triage/events?limit=2", headers=AUTH).json()
check("limit caps", r["count"] == 2, r)
r = client.get("/api/triage/events?since=abc", headers=AUTH)
check("a bad since is 400", r.status_code == 400 and r.json()["ok"] is False, r.json())

# --- E1 search (FTS5, in sync on write)
check("FTS5 is available in this SQLite", store.FTS_AVAILABLE is True, store.FTS_AVAILABLE)
r = client.get("/api/triage/search?q=twin+rivers", headers=AUTH).json()
check("search by title", r["count"] == 1 and r["threads"][0]["thread_id"] == "r-2", r)
r = client.get("/api/triage/search?q=rsync", headers=AUTH).json()
check("search by summary", r["count"] == 1 and r["threads"][0]["thread_id"] == "r-3", r)
r = client.get("/api/triage/search?q=chased+vendor+phone", headers=AUTH).json()
check("search by event body (a note)", r["count"] == 1 and r["threads"][0]["thread_id"] == "r-2", r)
r = client.get("/api/triage/search?q=renewal+tell", headers=AUTH).json()
check("search by a reply note, every token required", r["count"] == 1, r)
r = client.get("/api/triage/search?q=renewal+zebra", headers=AUTH).json()
check("a token that matches nothing empties the result", r["count"] == 0, r)
r = client.get("/api/triage/search?q=", headers=AUTH).json()
check("empty query is empty", r["count"] == 0 and r["threads"] == [], r)
r = client.get("/api/triage/search?q=twi", headers=AUTH).json()
check("prefix matching", r["count"] == 1, r)
r = client.get("/api/triage/search?q=%22unbalanced", headers=AUTH)
check("a stray quote does not 500", r.status_code == 200, r.status_code)
r = client.get("/api/triage/search?q=stale&limit=1", headers=AUTH).json()
check("limit honoured", r["count"] <= 1, r)
client.post("/api/triage/threads/r-3/note", json={"note": "Root cause: the NAS share went read-only overnight."}, headers=AUTH)
r = client.get("/api/triage/search?q=nas+share+read-only", headers=AUTH).json()
check("the index follows a write", r["count"] == 1 and r["threads"][0]["thread_id"] == "r-3", r)
put("r-3", "Backup job keeps failing on host-b", "The nightly rsync exits 23; the share is read-only.")
r = client.get("/api/triage/search?q=nightly+exits", headers=AUTH).json()
check("a resync re-indexes the thread text without duplicates", r["count"] == 1, r)
con = store.db(os.environ["HELM_DB"])
n_thread_rows = con.execute("SELECT COUNT(*) FROM search_fts WHERE thread_id='r-3' AND kind='thread'").fetchone()[0]
check("one thread row per thread in the index", n_thread_rows == 1, n_thread_rows)
con.execute("DELETE FROM search_fts")
con.commit()
con.close()
con = store.db(os.environ["HELM_DB"])   # a re-open rebuilds an empty index (adoption on open)
n = con.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
con.close()
check("an empty index is rebuilt on open", n > 0, n)
r = client.get("/api/triage/search?q=twin", headers=AUTH).json()
check("and searches again", r["count"] == 1, r)
# The fallback path (no FTS5) gives the same answer for the same query.
scan = store.search_threads.__wrapped__ if hasattr(store.search_threads, "__wrapped__") else None
saved = store.FTS_AVAILABLE
store.FTS_AVAILABLE = False
con = store.db(os.environ["HELM_DB"])
ids = [t["thread_id"] for t in store.search_threads(con, "nas share")]
con.close()
store.FTS_AVAILABLE = saved
check("the Python fallback finds the same thread", ids == ["r-3"], ids)

# --- E3 tokens
con = store.db(os.environ["HELM_DB"])
tok = tokens.add(con, "mobile")
con.close()
check("a minted token is long and never the legacy one", len(tok) > 30 and tok != "test-token")
A2 = {"Authorization": "Bearer " + tok}
r = client.get("/api/triage/threads", headers=A2)
check("a per-caller token authenticates", r.status_code == 200, r.status_code)
r = client.post("/api/triage/threads/r-3/note", json={"note": "Filed from the phone."}, headers=A2)
check("and writes", r.status_code == 202, r.json())
ev = client.get("/api/triage/events?thread=r-3&kind=note", headers=AUTH).json()["events"]
mine = [e for e in ev if "phone" in e["body"]]
check("its principal is recorded on the event", mine and mine[0]["source_id"] == "mobile", mine)
legacy = [e for e in ev if "NAS share" in e["body"]]
check("the legacy token records no principal", legacy and legacy[0]["source_id"] == "", legacy)
con = store.db(os.environ["HELM_DB"])
rows = tokens.list_tokens(con)
check("list shows the principal, live, last used", rows[0]["principal"] == "mobile" and rows[0]["revoked"] is None
      and rows[0]["last_used"], rows)
check("revoke", tokens.revoke(con, "mobile") is True)
check("revoking twice is False", tokens.revoke(con, "mobile") is False)
con.close()
r = client.get("/api/triage/threads", headers=A2)
check("a revoked token is refused", r.status_code == 401, r.status_code)
r = client.get("/api/triage/threads", headers=AUTH)
check("the legacy HELM_APP_TOKEN keeps working", r.status_code == 200)
r = client.get("/api/triage/threads", headers={"Authorization": "Bearer " + tok[:-1] + "x"})
check("a near-miss token is refused", r.status_code == 401)
# the auth gate's cache is warm now; a token minted by the CLI in another process must work at once
con = store.db(os.environ["HELM_DB"])
fresh = tokens.add(con, "producer")
con.close()
r = client.get("/api/triage/threads", headers={"Authorization": "Bearer " + fresh})
check("a token minted after the cache warmed is good on its first use (no 15 s window)", r.status_code == 200, r.status_code)
r = client.post("/api/triage/threads/r-3/reply", json={"note": "no value at all"}, headers=AUTH)
check("a reply without a value is a 400 with the error shape, not a 500",
      r.status_code == 400 and r.json()["ok"] is False and "value" in r.json()["detail"], (r.status_code, r.text[:120]))
r = client.get("/api/triage/suppressions", headers=AUTH)
check("suppressions carries the earlier app's count", r.status_code == 200 and r.json()["count"] == len(r.json()["suppressions"]), r.text[:120])
con = store.db(os.environ["HELM_DB"])
try:
    tokens.add(con, "bad principal!")
    check("a bad principal is refused", False)
except ValueError:
    check("a bad principal is refused", True)
con.close()

# the CLI
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = tokens.main(["add", "producer"])
minted = out.getvalue().strip()
check("CLI add prints the token once, on stdout", rc == 0 and len(minted) > 30 and "shown once" in err.getvalue(), (rc, minted[:5]))
r = client.get("/api/triage/threads", headers={"Authorization": "Bearer " + minted})
check("the CLI's token works", r.status_code == 200)
out = io.StringIO()
with redirect_stdout(out):
    rc = tokens.main(["list"])
check("CLI list names both principals", rc == 0 and "mobile" in out.getvalue() and "producer" in out.getvalue(), out.getvalue())
out = io.StringIO()
with redirect_stdout(out):
    rc = tokens.main(["revoke", "producer"])
check("CLI revoke", rc == 0 and "revoked producer" in out.getvalue(), out.getvalue())
r = client.get("/api/triage/threads", headers={"Authorization": "Bearer " + minted})
check("revoked via CLI is refused", r.status_code == 401)
err = io.StringIO()
with redirect_stderr(err):
    rc = tokens.main(["frob"])
check("CLI usage on a bad verb", rc == 2)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
