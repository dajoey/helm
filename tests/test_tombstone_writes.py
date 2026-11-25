"""A tombstoned thread is gone for writers as well as readers.

Before: get_thread / the lists filter tombstoned=0, but put_threads' update branch matched the
tombstoned row by id and answered `updated: 1` for a card no reader could see, and post_result lit
`needs-you` + an ask on the still-tombstoned row (an invisible lit card). A producer that re-parked
on the same id got a false success on every tick and the owner never saw the card.

Now: a PUT of a retired id is REJECTED (a retired card stays retired; the producer must mint a new
id), and a result that would leave a tombstoned thread in any state but done is refused with 409.
A settling result (state done) on a retired thread is still accepted: the pump reports the outcome
of a task that finished after the card was retired.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

os.environ["HELM_APP_TOKEN"] = "test-token"
TMP = tempfile.mkdtemp(prefix="helm-tomb-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ.pop("HELM_SENTRY_DSN", None)

import app.service as svc

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


def thread(tid, title="Retired once, asked again"):
    return {
        "thread_id": tid, "title": title, "summary": "s " * 10,
        "category": "Test", "urgency": "this-week", "state": "needs-you",
        "ask": {"kind": "choice", "prompt": "Pick one", "requires": ["decision"],
                "options": [
                    {"label": "Renew the cert and rotate the key", "value": "do", "effect": "It gets done"},
                    {"label": "Leave it alone for now", "value": "no", "effect": "Nothing happens"}]},
        "sources": [{"source": "test", "id": tid, "note": ""}],
    }


def put(threads=(), tombstones=()):
    return client.put("/api/triage/threads",
                      json={"threads": list(threads), "tombstones": list(tombstones)}, headers=AUTH)


def row(tid):
    con = svc._db()
    try:
        return con.execute("SELECT state, ask_kind, tombstoned FROM threads WHERE thread_id=?", (tid,)).fetchone()
    finally:
        con.close()


def events(tid):
    con = svc._db()
    try:
        return con.execute("SELECT COUNT(*) n FROM events WHERE thread_id=?", (tid,)).fetchone()["n"]
    finally:
        con.close()


NOTE = "Task ran to the end; the report is here so the card does not lie about what happened."

# --- (a) a PUT of a retired id -------------------------------------------------------------------
TID = "t-tomb-put"
check("create", put([thread(TID)]).status_code == 200)
check("retire", put(tombstones=[TID]).status_code == 200)
r0 = row(TID)
check("retired row is tombstoned and done", r0["tombstoned"] == 1 and r0["state"] == "done", dict(r0))
check("a retired thread reads as gone", client.get(f"/api/triage/threads/{TID}", headers=AUTH).status_code == 404)

r = put([thread(TID)])
body = r.json()
check("PUT of a retired id is not a success", body.get("ok") is False and body.get("updated") == 0
      and body.get("accepted") == 0, str(body)[:300])
check("PUT of a retired id is refused (400), not 200", r.status_code == 400, r.status_code)
rej = body.get("rejected") or []
check("the rejection names the thread and says it is retired",
      len(rej) == 1 and rej[0].get("thread_id") == TID
      and any("retired" in e for e in rej[0].get("errors", [])), str(rej)[:300])
r1 = row(TID)
check("the retired row is untouched", r1["tombstoned"] == 1 and r1["state"] == "done" and r1["ask_kind"] == r0["ask_kind"],
      dict(r1))

# the rest of a batch is still stored (partial success) and a live id is unaffected
r = put([thread(TID), thread("t-tomb-live")])
body = r.json()
check("a retired id costs only itself in a mixed batch",
      body.get("created") == 1 and body.get("rejected_count") == 1
      and row("t-tomb-live")["state"] == "needs-you", str(body)[:300])
check("live thread resync still updates", put([thread("t-tomb-live", "Edited title")]).json().get("updated") == 1)

# --- (b) a result on a retired thread ------------------------------------------------------------
TID2 = "t-tomb-result"
put([thread(TID2)])
put(tombstones=[TID2])
r = client.post(f"/api/triage/threads/{TID2}/claims", json={"executor_id": "exec-11", "runtime": "agent"}, headers=AUTH)
check("claim on a retired thread is still 201", r.status_code == 201, r.text[:200])
cid = r.json().get("claim_id")
ev0 = events(TID2)
r_before = row(TID2)

lit = {"outcome": "asked", "state": "needs-you", "note": NOTE,
       "ask": {"kind": "choice", "prompt": "Which way should the run go?", "requires": ["decision"],
               "options": [
                   {"label": "Renew the cert and rotate the key", "value": "do", "effect": "It gets done"},
                   {"label": "Leave it alone for now", "value": "no", "effect": "Nothing happens"}]}}
r = client.post(f"/api/triage/threads/{TID2}/claims/{cid}/result", json=lit, headers=AUTH)
check("a lighting result on a retired thread is refused (409)", r.status_code == 409, f"{r.status_code} {r.text[:300]}")
check("the refusal says the thread is retired", "retired" in r.text, r.text[:300])
r2 = row(TID2)
check("no invisible needs-you row", r2["tombstoned"] == 1 and r2["state"] == "done"
      and r2["ask_kind"] == r_before["ask_kind"], (dict(r2), dict(r_before)))
check("a refused result writes no event", events(TID2) == ev0, (events(TID2), ev0))

# the claim was not consumed by the refusal; a settling result (even one that sends an ack along,
# which a done thread settles without lighting) goes through and leaves the row unlit
ack = {"outcome": "finished", "state": "done", "note": NOTE,
       "ask": {"kind": "ack", "prompt": "Done - read the report", "options": [
           {"label": "Got it", "value": "ok", "effect": "Settles this"}]}}
r = client.post(f"/api/triage/threads/{TID2}/claims/{cid}/result", json=ack, headers=AUTH)
check("a settling result on a retired thread is still accepted (202)", r.status_code == 202, f"{r.status_code} {r.text[:300]}")
r3 = row(TID2)
check("settled row stays tombstoned, done and unlit",
      r3["tombstoned"] == 1 and r3["state"] == "done" and r3["ask_kind"] == "none", dict(r3))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
