"""Bodies the thread action routes (reply/close/reopen/mute) must not answer 500 for.

An extra key the store function does not take, a body that is not an object, a bad
attachment and a non-text reason are a 202 (extra key, dropped) or a 400, never a 500.
"""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

os.environ["HELM_APP_TOKEN"] = "test-token"  # force: a persisted real token must never leak into tests
TMP = tempfile.mkdtemp(prefix="helm-badbodies-")
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

_n = [0]
WORDS = ['zebra', 'walrus', 'kestrel', 'marmot', 'ibis', 'lemur', 'narwhal', 'ocelot', 'pangolin', 'quokka', 'tapir', 'vole', 'yak', 'gecko', 'heron', 'jackal', 'newt', 'osprey', 'puffin', 'rhea', 'stoat', 'toucan', 'urial', 'viper', 'wombat', 'xerus', 'civet', 'dingo', 'egret', 'fossa', 'gannet', 'hyrax', 'impala', 'jerboa', 'kudu', 'loris', 'mole', 'nutria', 'okapi', 'pika', 'quail', 'raven', 'skink', 'takin', 'uakari', 'vicuna', 'wren', 'addax', 'bongo', 'coati', 'dhole', 'eland', 'finch', 'gaur', 'hoopoe']
def seed():
    _n[0] += 1
    tid = "bb-%d" % _n[0]
    w = WORDS[_n[0] % len(WORDS)]   # distinct wording per thread, or the he-doesnt-know gate refuses the repeat
    body = {"threads": [{
        "thread_id": tid, "title": "Feed the %s colony schedule %d" % (w, _n[0]), "summary": "Rota for the %s enclosure keepers" % w,
        "category": "Test", "urgency": "this-week", "state": "needs-you",
        "ask": {"kind": "choice", "prompt": "Which %s rota do we run" % w, "requires": ["decision"],
                "context": "The %s enclosure keepers need a rota before the first of the month and nobody has picked one." % w,
                "options": [
                    {"label": "Renew the cert and rotate the key", "value": "do", "effect": "It gets done"},
                    {"label": "Leave it alone for now", "value": "no", "effect": "Nothing happens"},
                ]},
        "sources": [{"source": "test", "id": tid, "note": ""}],
    }]}
    r = client.put("/api/triage/threads", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return tid

def post(tid, action, raw):
    return client.post("/api/triage/threads/%s/%s" % (tid, action), content=json.dumps(raw), headers=JSON_H)

def state(tid):
    return client.get("/api/triage/threads/" + tid, headers=AUTH).json().get("state")

def att_files():
    out = []
    for root, _d, files in os.walk(os.environ["HELM_ATTACH_DIR"]):
        out += files
    return out

# --- an extra key is dropped (202) and the thread ends as the same call without it
for action, body, plain in [
    ("close", {"note": "done"}, {}),
    ("close", {"note": "x", "force": True}, {"force": True}),
    ("close", {"reason": "ok", "blocked_on": "x", "recheck_days": 2}, {"reason": "ok"}),
    ("mute", {"reason": "noise"}, {}),
    ("reply", {"value": "do", "reason": "x"}, {"value": "do"}),
    ("reply", {"value": "do", "hours": 3}, {"value": "do"}),
]:
    t1, t2 = seed(), seed()
    r = post(t1, action, body)
    check("%s %s is 202" % (action, body), r.status_code == 202, "%s %s" % (r.status_code, r.text[:160]))
    post(t2, action, plain)
    check("%s %s ends as without the extra key" % (action, body), state(t1) == state(t2),
          "%s vs %s" % (state(t1), state(t2)))

t = seed()
post(t, "close", {})
r = post(t, "reopen", {"reason": "oops"})
check("reopen with an extra key is 202", r.status_code == 202, "%s %s" % (r.status_code, r.text[:160]))
check("reopen with an extra key reopens", state(t) == "needs-you", str(state(t)))

# --- a body that is not an object is a 400
for action, raw in [("close", None), ("close", [1]), ("reply", [1]), ("reply", None), ("mute", None),
                    ("reopen", [1]), ("close", "text"), ("mute", 5)]:
    r = post(seed(), action, raw)
    check("%s %r is 400 object body required" % (action, raw),
          r.status_code == 400 and "object body required" in r.text, "%s %s" % (r.status_code, r.text[:160]))

# --- bad attachments are a 400 and write nothing
for label, att in [
    ("non-dict item", 1),
    ("not base64", {"name": "a.txt", "content_base64": "!!!notbase64"}),
    ("bad padding", {"name": "a.txt", "content_base64": "abc"}),
    ("non-string content", {"name": "a.txt", "content_base64": 5}),
]:
    r = post(seed(), "reply", {"value": "do", "attachments": [att]})
    check("attachment %s is 400" % label, r.status_code == 400, "%s %s" % (r.status_code, r.text[:160]))
check("no attachment bytes written for a bad one", att_files() == [], str(att_files()))

# --- a non-text reason or note is a 400
r = post(seed(), "close", {"reason": {"x": 1}})
check("close with an object reason is 400", r.status_code == 400, "%s %s" % (r.status_code, r.text[:160]))
t = seed()
post(t, "close", {})
r = post(t, "reopen", {"note": {"x": 1}})
check("reopen with an object note is 400", r.status_code == 400, "%s %s" % (r.status_code, r.text[:160]))

# --- pins: what live clients send keeps working
t = seed()
r = post(t, "close", {"actor": "owner", "reason": "not now"})
check("close actor+reason still 202 and done", r.status_code == 202 and state(t) == "done", r.text[:160])
t = seed()
r = post(t, "reply", {"value": "do", "note": "n", "by": "owner"})
check("reply value+note+by still 202", r.status_code == 202 and r.json().get("accepted") is True, r.text[:160])
t = seed()
r = post(t, "reply", {"value": "do", "attachments": [
    {"name": "a.txt", "mime": "text/plain", "content_base64": base64.b64encode(b"hello").decode()}]})
check("reply with a valid attachment is 202", r.status_code == 202, "%s %s" % (r.status_code, r.text[:160]))
check("the valid attachment's bytes land on disk", len(att_files()) == 1, str(att_files()))
t = seed()
r = post(t, "reply", {"value": "__task__", "note": "do the thing", "by": "some-agent"})
check("hand-off reply is 202", r.status_code == 202, r.text[:160])
ev = client.get("/api/triage/threads/" + t, headers=AUTH).json().get("events", [])
check("hand-off keeps its own source, not owner",
      any(e.get("body") == "__task__" and e.get("source") == "some-agent" for e in ev),
      str([(e.get("source"), e.get("body")) for e in ev][-4:]))

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
