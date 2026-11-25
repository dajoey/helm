"""ask.requires survives the triage threads PUT/GET round trip.

The four-test's T1 evidence (ask.requires) used to be door evidence only: the PUT accepted
it, the gate read it off the submitted body, and the store dropped it, so the card read back
without it and re-checking a stored card against POST /api/triage/needs-owner-check failed
T1-only-he-can. The markers now ride in the row's ask_meta next to subject and recap, so they
belong to the QUESTION: they read back while that question is live and are gone with it.

Run: python3 tests/test_ask_requires_roundtrip.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="helm-askreq-")
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


OPTIONS = [{"label": "Do the thing fully", "value": "do", "effect": "It gets done"},
           {"label": "Skip it for now", "value": "no", "effect": "Nothing happens"}]
CONTEXT = ("This card exists to prove the round trip. The producer states which only-he-can "
           "capability the next step needs, and the store has to hand it back unchanged.")


def card(tid, title, prompt="Which way should this go?", **ask_extra):
    ask = {"kind": "choice", "prompt": prompt, "options": OPTIONS, "context": CONTEXT,
           "subject": "Round trip", "recap": "A probe card for the requires round trip."}
    ask.update(ask_extra)
    return {"thread_id": tid, "title": title, "summary": "s " * 10, "category": "Test",
            "urgency": "this-week", "state": "needs-you",
            "sources": [{"source": "test", "id": tid, "note": ""}], "ask": ask}


def put(t):
    return api("PUT", "/api/triage/threads", {"threads": [t]})


def get(tid):
    code, th = api("GET", "/api/triage/threads/%s" % tid)
    return th if code == 200 else None


print("ask.requires round trip")

# 1. list form: PUT, GET, and the stored card re-passes the gate
code, res = put(card("rq-list", "Requires list form round trip", requires=["decision"]))
check("PUT with requires lands", code == 200 and res.get("created") == 1, (code, res))
th = get("rq-list")
check("GET reads ask.requires back (list form)",
      th is not None and th["ask"].get("requires") == ["decision"], th and th["ask"])
code, res = api("POST", "/api/triage/needs-owner-check", {"thread": th})
check("the stored card re-passes the gate (T1 not refused)",
      code == 200 and res.get("failed_test") != "T1-only-he-can", (code, res))

# 2. string form reads back normalized to the list
code, res = put(card("rq-str", "Requires string form round trip", requires="Decision"))
th = get("rq-str")
check("string form is stored and reads back as a list",
      code == 200 and th is not None and th["ask"].get("requires") == ["decision"],
      (code, th and th["ask"]))

# 3. several markers keep their order and drop repeats
put(card("rq-many", "Requires several markers round trip", requires=["funds", "decision", "funds"]))
th = get("rq-many")
check("several markers read back in order, deduplicated",
      th is not None and th["ask"].get("requires") == ["funds", "decision"], th and th["ask"])

# 4. the list read carries it too
code, lst = api("GET", "/api/triage/threads")
row = next((t for t in lst["threads"] if t["thread_id"] == "rq-list"), None)
check("the threads list carries ask.requires",
      row is not None and row["ask"].get("requires") == ["decision"], row and row["ask"])

# 5. a resync that does not resend requires keeps it (same question)
put(card("rq-list", "Requires list form round trip"))
th = get("rq-list")
check("a resync of the same question without requires keeps it",
      th["ask"].get("requires") == ["decision"], th["ask"])

# 6. a resync that sends a different marker replaces it
put(card("rq-list", "Requires list form round trip", requires=["credential"]))
th = get("rq-list")
check("sending requires again replaces it", th["ask"].get("requires") == ["credential"], th["ask"])

# 7. a different question does not inherit the old markers
put(card("rq-list", "Requires list form round trip", prompt="Now a different question entirely?"))
th = get("rq-list")
check("a new question without requires does not inherit the old ones",
      not th["ask"].get("requires"), th["ask"])

# 8. a card that never stated requires reads back without the key (an ack is exempt from T1)
ack = card("rq-ack", "Requires absent on an ack")
ack["ask"] = {"kind": "ack", "prompt": "Read this note.", "context": CONTEXT}
code, res = put(ack)
th = get("rq-ack")
check("a card with no requires reads back without the key",
      code == 200 and th is not None and not th["ask"].get("requires"), (code, th and th["ask"]))

# 9. the door still refuses a requires-less new choice and an unknown marker
code, res = put(card("rq-none", "Requires missing is still refused"))
check("a new choice card with no requires is still refused at the door",
      code == 400 and "T1-only-he-can" in str(res), (code, res))
code, res = put(card("rq-bad", "Requires unknown marker is still refused", requires=["whim"]))
check("an unknown marker is still a 400", code == 400, (code, res))

print("%d passed, %d failed" % (PASSED, FAILED))
sys.exit(1 if FAILED else 0)
