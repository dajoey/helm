"""the owner's Retry on a stalled or capped run: POST /api/triage/threads/{id}/retry.

Run: python3 tests/test_retry_dispatch.py

The dispatcher offers a thread when its newest owner-reply or instruction is newer than
dispatched_at and the thread has attempts left (store.pending_dispatch). A run that never
reported, or one that burned its budget, sits in 'working' with nothing to offer, and
nothing on a screen could move it. Retry records the owner's instruction (so there is new work
carrying his words) and restores the attempt budget (he is the one lifting the cap). Only a
thread in flight can be retried; the route is the owner's own tap, so the web role allows it.
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
TMP = tempfile.mkdtemp(prefix="helm-retry-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")

import app.service as svc  # noqa: E402
import app.web as web  # noqa: E402
from helm import store  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


con = store.db(os.environ["HELM_DB"])
ASK = {"kind": "choice", "prompt": "Grow the volume tonight?", "requires": ["decision"],
       "options": [{"label": "Grow it by 500 GB", "value": "grow", "effect": "A short outage at 02:00"},
                   {"label": "Leave it until Friday", "value": "wait", "effect": "Nothing changes yet"}]}


def lit(tid):
    store.put_threads(con, {"threads": [{"thread_id": tid, "title": "Disk " + tid, "summary": "The disk", "category": "Fleet",
                                         "urgency": "this-week", "state": "needs-you", "ask": ASK,
                                         "sources": [{"source": "test", "id": tid}]}]})


def offered(tid):
    return any(p["thread_id"] == tid for p in store.pending_dispatch(con)["pending"])


print("a run that never reported")
lit("t-stale")
store.reply(con, "t-stale", "grow")
check("the answer is offered to the dispatcher", offered("t-stale"))
reply_ts = con.execute("SELECT MAX(ts) FROM events WHERE thread_id='t-stale' AND kind='owner-reply'").fetchone()[0]
store.mark_dispatched(con, "t-stale", mode="start")
store.mark_dispatched(con, "t-stale", reply_ts=reply_ts, ok=True)
check("once dispatched it is not offered again", not offered("t-stale"))
out = store.retry_dispatch(con, "t-stale", by="owner")
check("retry answers ok", out.get("ok") and out.get("retried"), out)
check("after retry the dispatcher offers it again", offered("t-stale"))
ev = con.execute("SELECT kind, body, source FROM events WHERE thread_id='t-stale' ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
check("the retry is the owner's instruction on the thread", ev["kind"] == "instruction" and ev["source"] == "owner" and "run it again" in ev["body"], dict(ev))

print("\na run that burned its attempts")
lit("t-capped")
store.reply(con, "t-capped", "grow")
for _ in range(store.MAX_DISPATCH_ATTEMPTS):
    store.mark_dispatched(con, "t-capped", mode="start")
check("capped: not offered", not offered("t-capped"))
store.retry_dispatch(con, "t-capped", by="owner", note="Retry with the smaller volume")
check("retry lifts the cap: offered again", offered("t-capped"))
check("the attempt budget is fresh", con.execute("SELECT dispatch_attempts FROM threads WHERE thread_id='t-capped'").fetchone()[0] == 0)
check("the owner's own words ride with it", con.execute("SELECT body FROM events WHERE thread_id='t-capped' AND kind='instruction'").fetchone()[0]
      == "Retry with the smaller volume")

print("\nquiet runs: what ACTIVITY offers to retry")
lit("t-quiet")
store.reply(con, "t-quiet", "grow")
rts = con.execute("SELECT MAX(ts) FROM events WHERE thread_id='t-quiet' AND kind='owner-reply'").fetchone()[0]
store.mark_dispatched(con, "t-quiet", mode="start")
store.mark_dispatched(con, "t-quiet", reply_ts=rts, ok=True)
con.execute("UPDATE events SET ts=? WHERE thread_id='t-quiet'", (time.time() - store.RESULT_TIMEOUT_S - 130,))   # H6: silent since
con.execute("UPDATE events SET ts=? WHERE thread_id='t-quiet' AND kind='owner-reply'", (time.time() - store.RESULT_TIMEOUT_S - 120,))
con.execute("UPDATE threads SET dispatched_at=? WHERE thread_id='t-quiet'", (time.time() - store.RESULT_TIMEOUT_S - 60,))
con.commit()
q = {r["thread_id"]: r for r in store.quiet_runs(con)}
check("a run that never reported is listed, saying so", "t-quiet" in q and q["t-quiet"]["why"] == "never reported back", q)
lit("t-capped2")
store.reply(con, "t-capped2", "grow")
for _ in range(store.MAX_DISPATCH_ATTEMPTS):
    store.mark_dispatched(con, "t-capped2", mode="start")
q = {r["thread_id"]: r for r in store.quiet_runs(con)}
check("a run that gave up is listed with its attempts", "t-capped2" in q and q["t-capped2"]["why"].startswith("gave up after"), q)
store.retry_dispatch(con, "t-capped2", by="owner")
check("once retried it is no longer quiet", "t-capped2" not in {r["thread_id"] for r in store.quiet_runs(con)})
check("a healthy lit thread is never listed", "t-lit" not in {r["thread_id"] for r in store.quiet_runs(con)})

print("\nonly a thread in flight")
lit("t-lit")
try:
    store.retry_dispatch(con, "t-lit", by="owner")
    check("a lit thread is refused", False)
except store.StoreError as e:
    check("a lit thread is refused with a 409 that says why", e.status == 409 and "needs-you" in e.detail, e.detail)
con.close()

print("\nthe route")
client = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
con = store.db(os.environ["HELM_DB"])
lit("t-route")
store.reply(con, "t-route", "grow")
store.mark_dispatched(con, "t-route", mode="start")
con.close()
r = client.post("/api/triage/threads/t-route/retry", json={}, headers={**AUTH, "X-Helm-Actor": "owner"})
check("POST /retry -> 200", r.status_code == 200 and r.json().get("retried"), r.text)
r = client.post("/api/triage/threads/t-lit/retry", json={}, headers=AUTH)
check("POST /retry on a lit thread -> 409 {ok:false, detail}", r.status_code == 409 and r.json()["ok"] is False, r.text)
r = client.post("/api/triage/threads/t-nope/retry", json={}, headers=AUTH)
check("POST /retry on an unknown thread -> 404", r.status_code == 404, r.status_code)
check("/api/status lists the quiet runs", any(r["thread_id"] == "t-quiet" for r in client.get("/api/status", headers=AUTH).json().get("quiet_runs", [])))
check("the web role allows the owner's retry tap", web.ALLOW_POST.match("/api/triage/threads/t-route/retry") is not None)

print("\nwake now (a snoozed thread)")
con = store.db(os.environ["HELM_DB"])
lit("t-snz")
store.snooze(con, "t-snz", hours=24)
row = con.execute("SELECT state, wake_at FROM threads WHERE thread_id='t-snz'").fetchone()
check("snoozed: not lit, wake_at set", row["state"] != "needs-you" and row["wake_at"] > time.time(), dict(row))
con.close()
r = client.post("/api/triage/threads/t-snz/wake", json={}, headers={**AUTH, "X-Helm-Actor": "owner"})
check("POST /wake -> 200 woke", r.status_code == 200 and r.json().get("woke"), r.text)
t = client.get("/api/triage/threads/t-snz", headers=AUTH).json()
check("it is lit again with the same question", t["state"] == "needs-you" and t["ask"]["prompt"] == ASK["prompt"], (t["state"], t.get("ask")))
check("and says why it came back", any(e["kind"] == "bot-result" and e["source"] == "helm-wake" for e in t["events"]))
r = client.post("/api/triage/threads/t-snz/wake", json={}, headers=AUTH)
check("waking a thread that is not snoozed -> 409", r.status_code == 409 and r.json()["ok"] is False, r.text)
print("\na muted thread is neither retried nor woken")
con = store.db(os.environ["HELM_DB"])
lit("t-muted")
store.reply(con, "t-muted", "grow")
con.execute("UPDATE threads SET suppressed=1 WHERE thread_id='t-muted'")
con.commit()
try:
    store.retry_dispatch(con, "t-muted", by="owner")
    check("retry on a muted thread is refused", False)
except store.StoreError as e:
    check("retry on a muted thread is refused with 409", e.status == 409 and "muted" in e.detail, e.detail)
con.execute("UPDATE threads SET wake_at=?, state='waiting-external' WHERE thread_id='t-muted'", (time.time() + 3600,))
con.commit()
try:
    store.wake_now(con, "t-muted")
    check("wake on a muted thread is refused", False)
except store.StoreError as e:
    check("wake on a muted thread is refused with 409", e.status == 409 and "muted" in e.detail, e.detail)
con.close()
check("the web role allows the owner's wake tap", web.ALLOW_POST.match("/api/triage/threads/t-snz/wake") is not None)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
