"""A hand-written card's own ask re-passes the producer's door and the check route.

store.create_thread files a `mine` card with a single `Done` option (terminal, resolved): the
one-button "Is this finished?" ask. validate_thread demanded two options of every choice ask, so
once a run was dispatched on that card, GET -> PUT of the card back (to annotate it) and the
needs-owner-check preflight both failed with "ask.kind is 'choice' requires at least 2 options,
got 1" for an ask Helm itself had written. The two-option floor is for a question a producer
asks; a card's own terminal ack is not one, and an in-flight card's incoming ask is ignored by
put_threads anyway. A producer's own one-option choice is still refused.

Run: python3 tests/test_hand_card_ask_roundtrip.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="helm-handask-")
os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ.pop("HELM_SENTRY_DSN", None)

from starlette.testclient import TestClient  # noqa: E402

import app.service as svc  # noqa: E402

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print("  FAIL %s %s" % (name, str(detail)[:400]))


CLIENT = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}


def api(method, path, payload=None):
    r = CLIENT.request(method, path, json=payload, headers=AUTH)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"raw": r.text}
    return r.status_code, body


def put(t):
    return api("PUT", "/api/triage/threads", {"threads": [t]})


print("hand card ask round trip")

code, made = api("POST", "/api/triage/threads/create", {
    "title": "Hand written card", "summary": "A card the owner wrote himself.", "category": "Test",
    "urgency": "whenever", "intent": "mine"})
check("create 201", code == 201, made)
tid = made.get("thread_id", "")

code, th = api("GET", "/api/triage/threads/%s" % tid)
opts = (th.get("ask") or {}).get("options") or []
check("the card is lit with the one Done option",
      len(opts) == 1 and opts[0].get("terminal") is True, th.get("ask"))

# working: the state the dispatcher leaves a card in, its stored ask untouched
import helm.store as store  # noqa: E402

con = store.db()
con.execute("UPDATE threads SET state='working' WHERE thread_id=?", (tid,))
con.commit()
code, th = api("GET", "/api/triage/threads/%s" % tid)
check("the card is working with its one-option ask",
      th.get("state") == "working" and len((th.get("ask") or {}).get("options") or []) == 1, th)

code, res = api("POST", "/api/triage/needs-owner-check", {"thread": th})
check("working: check route does not fail validation on option count",
      "at least 2 options" not in json.dumps(res), res)
code, res = put(th)
check("working: PUT of the fetched card is accepted",
      code == 200 and not res.get("rejected"), res)
code, after = api("GET", "/api/triage/threads/%s" % tid)
check("working: the stored ask is preserved by the PUT",
      after.get("state") == "working"
      and len((after.get("ask") or {}).get("options") or []) == 1, after.get("ask"))

# a producer's own one-option choice on a lit card is still refused
code, res = put({"thread_id": "t-one-opt", "title": "One option", "summary": "s " * 10,
                 "category": "Test", "urgency": "whenever", "state": "needs-you",
                 "sources": [{"source": "test", "id": "x", "note": ""}],
                 "ask": {"kind": "choice", "prompt": "Proceed with the change?",
                         "options": [{"label": "Do it", "value": "do", "effect": "It happens"}],
                         "context": "c " * 40}})
check("a producer's lit one-option choice is still refused",
      "at least 2 options" in json.dumps(res), res)

print("%d passed, %d failed" % (PASSED, FAILED))
sys.exit(1 if FAILED else 0)
