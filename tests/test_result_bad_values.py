"""POST /api/triage/threads/:id/result: a value no database can hold is a 400 or is cleaned, never a 500.

A report whose text carries a lone UTF-16 surrogate (a Windows script that cut a string in the
middle of an emoji), a `responder` that is not a string, or a snooze `wake_at` of NaN used to
raise inside post_result: a 500 that pages the error tracker and costs the client its report.

Run: python3 tests/test_result_bad_values.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ.pop("HELM_MODULES", None)
TMP = tempfile.mkdtemp(prefix="helm-result-bad-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ["HELM_DISPATCHER_HB_FILE"] = os.path.join(TMP, "hb.json")

import app.service as svc  # noqa: E402

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


NOTE = "Checked the meter history and the last three reads; the numbers agree with the contact's figure. " * 2
ASK = {"kind": "ack", "prompt": "Read this?"}
n = 0


def fresh():
    global n
    n += 1
    tid = "rbv-%d" % n
    t = {"thread_id": tid, "title": "Twin Rivers meter read", "summary": "s", "category": "Contacts",
         "urgency": "this-week", "state": "needs-you", "ask": ASK,
         "sources": [{"source": "case-watch", "id": tid}]}
    r = client.put("/api/triage/threads", json={"threads": [t]}, headers=AUTH)
    assert r.status_code == 200, r.text
    return tid


def post(tid, **over):
    body = {"outcome": "done", "state": "needs-you", "note": NOTE, "ask": ASK}
    body.update(over)
    # json.dumps writes a lone surrogate as a \udXXX escape, the way a Windows client's body arrives
    return client.post("/api/triage/threads/%s/result" % tid, content=json.dumps(body), headers=JSON_H)


def thread(tid):
    return client.get("/api/triage/threads/" + tid, headers=AUTH)


print("1. a lone surrogate in a text field is not a 500, and the report still lands")
for field in ("note", "outcome", "answers", "ask.prompt", "ask.context"):
    tid = fresh()
    if field == "note":
        r = post(tid, note=NOTE + " cut in the middle of an emoji \ud83d")
    elif field == "outcome":
        r = post(tid, outcome="done\ud83d")
    elif field == "answers":
        r = post(tid, answers="The figure agrees. \ud83d")
    else:
        r = post(tid, ask=dict(ASK, **{field.split(".")[1]: "Read this? \ud83d"}))
    check("%s: not a 500" % field, r.status_code < 500, (r.status_code, r.text[:200]))
    check("%s: accepted (202), the report is kept" % field, r.status_code == 202, (r.status_code, r.text[:200]))
    check("%s: the thread still reads" % field, thread(tid).status_code == 200)
tid = fresh()
r = post(tid, note=NOTE + " tail \ud83d end")
body = thread(tid).text
check("the text around the surrogate survives", r.status_code == 202 and "tail" in body and " end" in body, r.text[:200])
tid = fresh()
r = post(tid, note=NOTE + " pair 😀 ok")
check("a valid surrogate pair (an emoji) is untouched", r.status_code == 202 and "\U0001F600" in thread(tid).text,
      r.text[:200])
tid = fresh()
r = post(tid, state="waiting-external", blocked_on="waiting on \ud83d", wake_at=None)
check("waiting-external blocked_on with a surrogate is not a 500", r.status_code < 500, (r.status_code, r.text[:200]))

print("1b. a draft whose display text carries a lone surrogate is not a 500")
from helm.store import draft_sha  # noqa: E402


def draft(**view):
    spec = {"to": "member@example.org", "body": "hello"}
    v = dict(title="Reply", text="hello there", detail="", fields=[{"label": "To", "value": "member"}])
    v.update(view)
    return {"version": 1, "dest": "mail", "spec": spec, "sha": draft_sha("mail", spec),
            "button": "Send", "view": v, "checked_at": ""}


for label, view in (("view.text", dict(text="hi \ud83d")), ("view.title", dict(title="T\ud83d")),
                    ("view.detail", dict(detail="d\ud83d")),
                    ("view.fields label", dict(fields=[{"label": "a\ud83d", "value": "v"}])),
                    ("view.fields value", dict(fields=[{"label": "a", "value": "v\ud83d"}]))):
    tid = fresh()
    dr = draft(**view)
    r = post(tid, draft=dr)
    check("draft %s: not a 500" % label, r.status_code < 500, (r.status_code, r.text[:200]))
    check("draft %s: the thread still reads" % label, thread(tid).status_code == 200)
tid = fresh()
dr = draft()
dr["button"] = "Send \ud83d"
r = post(tid, draft=dr)
check("draft button with a surrogate: not a 500", r.status_code < 500, (r.status_code, r.text[:200]))
tid = fresh()
r = post(tid, draft=draft(text="hi \ud83d there"))
check("a surrogate in draft text is cleaned and the report kept", r.status_code == 202, (r.status_code, r.text[:200]))
tid = fresh()
r = post(tid, draft=draft(text="clean 😀 text"))
check("a clean draft is still accepted", r.status_code == 202, (r.status_code, r.text[:200]))

print("2. responder: anything but a string is a 400, never a 500")
for label, val in (("list", [1]), ("object", {"a": 1}), ("huge int", 10 ** 30), ("lone surrogate", "ex\ud83d")):
    tid = fresh()
    r = post(tid, responder=val)
    check("responder %s: not a 500" % label, r.status_code < 500, (r.status_code, r.text[:200]))
tid = fresh()
r = post(tid, responder=[1])
check("responder list is a 400 that names the field", r.status_code == 400 and "responder" in r.text, r.text[:200])
tid = fresh()
r = post(tid, responder="agent")
check("a normal responder is still accepted", r.status_code == 202, r.text[:200])

print("3. a snooze wake_at that is not a date (NaN) is a 400, never a 500")
tid = fresh()
r = client.post("/api/triage/threads/%s/result" % tid,
                content=json.dumps(dict(outcome="deferred", state="snoozed", note=NOTE, wake_at=float("nan"))),
                headers=JSON_H)
check("wake_at NaN: 400", r.status_code == 400, (r.status_code, r.text[:200]))
tid = fresh()
r = post(tid, state="snoozed", wake_at="nan")
check("wake_at 'nan': 400", r.status_code == 400, (r.status_code, r.text[:200]))
tid = fresh()
r = post(tid, state="snoozed", wake_at=time.time() + 86400)
check("a real wake_at still snoozes", r.status_code == 202, (r.status_code, r.text[:200]))

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
