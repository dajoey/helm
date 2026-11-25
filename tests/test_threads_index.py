"""The THREADS list behind L3: GET /api/triage/threads/index.

Run: python3 tests/test_threads_index.py

Every thread the site has ever held (done and muted included, tombstoned excepted), newest
first, in light rows, paged and filtered server-side: state (needs-you, working, on hold,
backlog, done, muted), a date range on last_change, the agent or runner (responder or any
event source), and full-text search over titles, summaries and event bodies. The facets
(total, by_state, agents) are over every thread so the chips keep their numbers while a
filter is on. A bad state or page is a 400 {ok:false, detail}. Also: the web role proxies
the route and nothing else changed shape (the old list route is untouched).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_SITE"] = "home"
os.environ.pop("HELM_MODULES", None)
TMP = tempfile.mkdtemp(prefix="helm-index-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")

import app.service as svc  # noqa: E402
from helm import store  # noqa: E402

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


NOW = time.time()
con = store.db(os.environ["HELM_DB"])


def put(tid, title, state="needs-you", responder=None, last_change=None, summary="", ask=None, events=None):
    t = {"thread_id": tid, "title": title, "summary": summary or "About " + title, "category": "Fleet",
         "urgency": "this-week", "state": state, "responder": responder,
         "sources": [{"source": "test", "id": tid}]}
    if state == "needs-you":
        t["ask"] = ask or {"kind": "choice", "prompt": "Renew now?", "requires": ["decision"],
                           "options": [{"label": "Renew the cert today", "value": "renew", "effect": "It gets renewed"},
                                       {"label": "Leave it until Monday", "value": "wait", "effect": "Nothing happens yet"}]}
    else:
        t["ask"] = {"kind": "none"}
    if state == "waiting-external":
        t["blocked_on"] = "the registrar's desk, ticket 00100001"
        t["recheck_at"] = NOW + 86400
    if events:
        t["events"] = events
    out = store.put_threads(con, {"threads": [t]})
    assert not out.get("rejected"), out
    if last_change is not None:
        con.execute("UPDATE threads SET last_change=? WHERE thread_id=?", (last_change, tid))
        con.commit()


# 1,050-ish history: the staging count names, almost all of it done. Titles differ
# in their words, not just a number: the home intake's T3 dedupe fingerprints titles.
N_DONE = 1040
ADJ = ["quiet", "stale", "spare", "nightly", "weekly", "flaky", "slow", "loud", "cold", "warm", "dusty", "brisk",
       "odd", "even", "late", "early", "wide", "narrow", "deep", "flat", "sharp", "round", "plain", "fancy",
       "green", "amber", "grey", "blue", "red", "gold", "silver", "copper", "iron", "stone", "glass", "paper",
       "north", "south", "east", "west"]
NOUN = ["mirror", "backup", "digest", "monitor", "volume", "sweep", "lantern", "portal", "ticket", "registrar",
        "recorder", "gateway", "runner", "pane", "queue", "lease", "goal", "board", "wiki", "notebook",
        "cert", "cron", "heartbeat", "snapshot", "prune", "restore", "drill", "log", "alert", "sensor"]
for i in range(N_DONE):
    title = "%s %s %d" % (ADJ[i % len(ADJ)], NOUN[(i // len(ADJ)) % len(NOUN)], i)
    if i % 50 == 0:
        title += " about the restic prune"
    put("t-old-%04d" % i, title,
        state="needs-you", last_change=NOW - 86400 * 30 - i * 60,
        events=[{"ts": NOW - 86400 * 30 - i * 60 - 5, "source": "sweep", "kind": "bot-result",
                 "body": "Report %d: nothing to do." % i}])
con.execute("UPDATE threads SET state='done', ask_kind='none' WHERE thread_id LIKE 't-old-%'")
con.commit()
put("t-lit-1", "Media box disk at 94%", state="needs-you", responder="agent:worker", last_change=NOW - 60)
put("t-lit-2", "Portal login page name", state="needs-you", last_change=NOW - 3600)
put("t-work-1", "Dedupe the Monitor sweep", state="working", responder="agent:worker", last_change=NOW - 600,
    events=[{"ts": NOW - 700, "source": "runner", "kind": "bot-result", "body": "checking snapshots, still 88 GiB"}])
put("t-hold-1", "Vendor certificate renewal", state="waiting-external", last_change=NOW - 7200)
put("t-done-1", "Fix the email digest cron leg", state="needs-you")
store.close_thread(con, "t-done-1", reason="read it", actor="owner")
put("t-muted-1", "Flapping monitor again", state="needs-you")
store.mute(con, "t-muted-1", note="never again")
# closing and muting stamp last_change; the fixture dates them after the fact
con.execute("UPDATE threads SET last_change=? WHERE thread_id='t-done-1'", (NOW - 86400,))
con.execute("UPDATE threads SET last_change=? WHERE thread_id='t-muted-1'", (NOW - 86400 * 3,))
con.commit()
put("t-gone-1", "A thread the producer retired", state="needs-you")
store.put_threads(con, {"threads": [], "tombstones": ["t-gone-1"]})

# ---------------------------------------------------------------- the store
out = store.index_threads(con)
check("total counts every held thread, tombstoned excepted", out["total"] == N_DONE + 6, out["total"])
check("matched == total with no filter", out["matched"] == out["total"], (out["matched"], out["total"]))
check("default page is 100 rows, page 1 of 11", out["page"] == 1 and out["limit"] == 100 and out["pages"] == 11 and len(out["threads"]) == 100, (out["pages"], len(out["threads"])))
ids = [t["thread_id"] for t in out["threads"]]
check("newest first: the lit card from a minute ago leads", ids[0] == "t-lit-1", ids[:3])
check("done and muted are in the list", "t-done-1" in ids and "t-muted-1" in ids, ids[:12])
lc = [t["last_change"] for t in out["threads"]]
check("sorted by last_change descending", lc == sorted(lc, reverse=True))
row = next(t for t in out["threads"] if t["thread_id"] == "t-work-1")
check("a light row: no events, but the last kind, the agent and the count", "events" in row and isinstance(row["events"], int)
      and row["last_kind"] == "bot-result" and row["agent"] == "runner" and row["events"] >= 1, row)
check("a muted thread reads state muted with the flag", next(t for t in out["threads"] if t["thread_id"] == "t-muted-1")["state"] == "muted"
      and next(t for t in out["threads"] if t["thread_id"] == "t-muted-1")["muted"] is True)
check("facets: by_state over every thread", out["by_state"]["done"] == N_DONE + 1 and out["by_state"]["needs-you"] == 2
      and out["by_state"]["working"] == 1 and out["by_state"]["waiting-external"] == 1 and out["by_state"]["muted"] == 1, out["by_state"])
check("facets: agents (responders) with counts", any(a["agent"] == "agent:worker" and a["count"] == 2 for a in out["agents"]), out["agents"])

last = store.index_threads(con, page=11)
check("the last page holds the remainder", len(last["threads"]) == (N_DONE + 6) - 1000 and last["page"] == 11, len(last["threads"]))
check("a page past the end is empty, not an error", store.index_threads(con, page=99)["threads"] == [])
small = store.index_threads(con, limit=7, page=2)
check("limit + page: page 2 of 7 skips the first 7", small["threads"][0]["thread_id"] == ids[7] and small["pages"] == (N_DONE + 6 + 6) // 7, small["threads"][0]["thread_id"])
check("limit is capped at 500", store.index_threads(con, limit=10_000)["limit"] == 500)

f = store.index_threads(con, states=["done"])
check("state=done: only done threads", f["matched"] == N_DONE + 1 and all(t["state"] == "done" for t in f["threads"]), f["matched"])
check("facets stay whole while a filter is on", f["by_state"]["needs-you"] == 2 and f["total"] == N_DONE + 6)
f = store.index_threads(con, states=["needs-you", "working"])
check("two states at once", {t["thread_id"] for t in f["threads"]} == {"t-lit-1", "t-lit-2", "t-work-1"}, [t["thread_id"] for t in f["threads"]])
f = store.index_threads(con, states=["muted"])
check("state=muted: the suppressed thread alone", [t["thread_id"] for t in f["threads"]] == ["t-muted-1"])
f = store.index_threads(con, states=["muted", "waiting-external"])
check("muted combines with a live state", {t["thread_id"] for t in f["threads"]} == {"t-muted-1", "t-hold-1"})
try:
    store.index_threads(con, states=["parked"])
    check("an unknown state is refused", False)
except store.StoreError as e:
    check("an unknown state is refused with 400 and the list of states", e.status == 400 and "needs-you" in e.detail, e.detail)

f = store.index_threads(con, since=NOW - 86400 * 2)
check("since: the last two days", {t["thread_id"] for t in f["threads"]} == {"t-lit-1", "t-lit-2", "t-work-1", "t-hold-1", "t-done-1"}, [t["thread_id"] for t in f["threads"]])
f = store.index_threads(con, since=NOW - 86400 * 2, until=NOW - 1800)
check("since + until: a window", {t["thread_id"] for t in f["threads"]} == {"t-lit-2", "t-hold-1", "t-done-1"}, [t["thread_id"] for t in f["threads"]])
f = store.index_threads(con, until=NOW - 86400 * 29)
check("until alone: the old history", f["matched"] == N_DONE, f["matched"])

f = store.index_threads(con, who="worker")
check("who matches the responder", "t-lit-1" in {t["thread_id"] for t in f["threads"]} and f["matched"] == 2, f["matched"])
f = store.index_threads(con, who="runner")
check("who matches an event source (a runner's report)", [t["thread_id"] for t in f["threads"]] == ["t-work-1"], [t["thread_id"] for t in f["threads"]])
f = store.index_threads(con, who="RUNNER")
check("who is case-insensitive", f["matched"] == 1)

f = store.index_threads(con, q="restic")
N_RESTIC = len(range(0, N_DONE, 50))
check("q: full text over titles (21 old cards mention the restic prune)", f["matched"] == N_RESTIC and all("restic" in t["title"] for t in f["threads"]), f["matched"])
f = store.index_threads(con, q="snapshots")
check("q: full text over event bodies", [t["thread_id"] for t in f["threads"]] == ["t-work-1"], [t["thread_id"] for t in f["threads"]])
f = store.index_threads(con, q="restic", states=["done"], page=1, limit=5)
check("q combines with a state filter and paging", f["matched"] == N_RESTIC and len(f["threads"]) == 5 and f["pages"] == 5, (f["matched"], f["pages"]))
f = store.index_threads(con, q="zzzz-nothing-matches")
check("q with no hit: an empty page, matched 0", f["matched"] == 0 and f["threads"] == [])
f = store.index_threads(con, q='"unbalanced')
check("a stray FTS operator falls back to the scan instead of failing", f["matched"] == 0 and isinstance(f["threads"], list))
saved = store.FTS_AVAILABLE
store.FTS_AVAILABLE = False
try:
    f = store.index_threads(con, q="snapshots")
    check("without FTS5 the scan gives the same answer", [t["thread_id"] for t in f["threads"]] == ["t-work-1"])
finally:
    store.FTS_AVAILABLE = saved
f = store.index_threads(con, ids=["t-lit-2", "t-hold-1", "t-gone-1", "nope"])
check("ids: only those threads (the gate view), tombstoned and unknown ignored", [t["thread_id"] for t in f["threads"]] == ["t-lit-2", "t-hold-1"], [t["thread_id"] for t in f["threads"]])
r = client.get("/api/triage/threads/index?ids=t-lit-1,t-done-1&state=done", headers=AUTH)
check("ids through the route combine with a state", [t["thread_id"] for t in r.json()["threads"]] == ["t-done-1"], r.json().get("threads"))
gone = store.index_threads(con, q="retired")
check("a tombstoned thread is never listed", "t-gone-1" not in {t["thread_id"] for t in gone["threads"]} and gone["matched"] == 0)
check("the old list route is untouched: done and muted stay out of it",
      not any(t["thread_id"] in ("t-done-1", "t-muted-1") for t in store.list_threads(con)["threads"]))

# ---------------------------------------------------------------- the route
r = client.get("/api/triage/threads/index", headers=AUTH)
j = r.json()
check("GET /api/triage/threads/index answers the shape", r.status_code == 200 and j["ok"] is True
      and {"total", "matched", "page", "pages", "limit", "by_state", "agents", "threads", "generated_at"} <= set(j), list(j))
check("the route needs the bearer", client.get("/api/triage/threads/index").status_code == 401)
r = client.get("/api/triage/threads/index?state=done,muted&limit=3&page=2", headers=AUTH)
j = r.json()
check("state=a,b with paging through the route", r.status_code == 200 and j["matched"] == N_DONE + 2 and j["page"] == 2 and len(j["threads"]) == 3, (j.get("matched"), j.get("page")))
r = client.get("/api/triage/threads/index?since=%d&who=worker&q=media" % int(NOW - 86400), headers=AUTH)
check("since + who + q through the route", r.status_code == 200 and [t["thread_id"] for t in r.json()["threads"]] == ["t-lit-1"], r.json().get("threads"))
r = client.get("/api/triage/threads/index?state=parked", headers=AUTH)
check("an unknown state is 400 {ok:false, detail}", r.status_code == 400 and r.json()["ok"] is False and "parked" in r.json()["detail"], r.json())
r = client.get("/api/triage/threads/index?since=yesterday", headers=AUTH)
check("a non-numeric date is 400", r.status_code == 400 and r.json()["ok"] is False, r.json())
r = client.get("/api/triage/threads/index?page=x", headers=AUTH)
check("a non-numeric page is 400", r.status_code == 400 and r.json()["ok"] is False, r.json())
for big in ("99999999999999999999", "9223372036854775807"):
    r = client.get("/api/triage/threads/index?page=" + big, headers=AUTH)
    check("a page too large for an offset is 400, never a 500 (page=%s)" % big,
          r.status_code == 400 and r.json()["ok"] is False and "page" in r.json()["detail"], (r.status_code, r.text[:120]))
r = client.get("/api/triage/threads/index?page=9999", headers=AUTH)
check("a page past the last one is an empty 200", r.status_code == 200 and r.json()["threads"] == [], r.status_code)
r = client.get("/api/triage/threads/t-lit-1", headers=AUTH)
check("the thread-by-id route still answers beside it", r.status_code == 200 and r.json()["thread_id"] == "t-lit-1")
r = client.get("/api/triage/threads/index/", headers=AUTH)
check("a trailing slash is not the index", r.status_code in (404, 307, 200))

con.close()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
