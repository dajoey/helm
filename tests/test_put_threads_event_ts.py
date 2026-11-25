"""PUT /api/triage/threads: an event `ts` that is not a usable timestamp rejects that thread, never a 500.

An ISO string used to reach float() in add_event after earlier threads of the payload were
written but before the commit, so the ValueError (a 500) rolled back the whole batch.
Now validate_thread names the bad `ts` as a per-thread error and the other threads are stored.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

os.environ["HELM_APP_TOKEN"] = "test-token"  # force: a persisted real token must never leak into tests
TMP = tempfile.mkdtemp(prefix="helm-evts-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")

import app.service as svc

client = TestClient(svc.app, raise_server_exceptions=False)
AUTH = {"Authorization": "Bearer test-token"}
JSON_H = dict(AUTH, **{"Content-Type": "application/json"})

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def thread(tid, events):
    t = {"thread_id": tid, "title": "A thing " + tid, "summary": "s", "category": "ops",
         "urgency": "whenever", "state": "backlog", "sources": []}
    if events is not None:
        t["events"] = events
    return t


def put(threads):
    # json.dumps, not json=: it also emits a bare NaN, which a producer's Python would send
    return client.put("/api/triage/threads", content=json.dumps({"threads": threads}), headers=JSON_H)


def stored(tid):
    return client.get("/api/triage/threads/%s" % tid, headers=AUTH).status_code == 200


good = {"ts": 1759572000.5, "source": "x", "kind": "update", "body": "ok"}

print("a bad ts rejects its thread; the other threads in the payload are stored")
for label, bad in [("iso string", "2026-10-04T10:00:00Z"), ("junk string", "not-a-number"),
                   ("list", [1]), ("dict", {"a": 1}), ("true", True),
                   ("nan", float("nan")), ("infinity", float("inf")), ("negative", -5),
                   ("milliseconds", 1759572000000), ("huge int", 10 ** 400),
                   ("huge numeric string", "1" + "0" * 400)]:
    slug = label.replace(" ", "-")
    tid_ok, tid_bad = "ok-" + slug, "bad-" + slug
    r = put([thread(tid_ok, [good]),
             thread(tid_bad, [{"ts": bad, "source": "x", "kind": "update", "body": "hello"}])])
    check("%s: not a 500" % label, r.status_code < 500, r.status_code)
    if r.status_code >= 500:
        continue
    out = r.json()
    rej = out.get("rejected") or []
    check("%s: the bad thread is rejected" % label,
          len(rej) == 1 and rej[0].get("thread_id") == tid_bad, out)
    check("%s: the error names ts" % label,
          bool(rej) and any("ts" in e for e in rej[0].get("errors", [])), out)
    check("%s: the good thread is stored" % label, stored(tid_ok))
    check("%s: the bad thread is not stored" % label, not stored(tid_bad))

print("usable ts values, and absent ts, still store")
for label, ts in [("float", 1759572000.5), ("int", 1759572000), ("numeric string", "1759572000"),
                  ("absent", None), ("empty string", ""), ("zero", 0)]:
    ev = {"source": "x", "kind": "update", "body": "e-" + label}
    if label != "absent":
        ev["ts"] = ts
    r = put([thread("good-" + label.replace(" ", "-"), [ev])])
    check("%s: accepted" % label, r.status_code == 200 and r.json().get("accepted") == 1,
          "%s %s" % (r.status_code, r.text[:200]))

print("events that are not a list are an error, not a crash; non-object entries are still skipped")
r = put([thread("ev-int", 5)])
check("events=5 is a 400, not a 500", r.status_code == 400, r.status_code)
r = put([thread("ev-skip", ["junk", 3, good])])
check("non-object entries skipped", r.status_code == 200 and r.json().get("accepted") == 1,
      "%s %s" % (r.status_code, r.text[:200]))

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
