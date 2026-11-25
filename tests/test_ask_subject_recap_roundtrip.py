"""ask.subject and ask.recap survive the triage threads PUT/GET round trip.

The card rule no-subject-recap (and the needs-owner-check route) read ask.subject and ask.recap
off the thread the caller submits. The store kept them against the question but only handed them
back as catchup.subject / catchup.recap, never inside `ask`, so an agent that read a stored card
and re-submitted or re-checked it (the shape every client holds) found both fields gone and the
gate refused it. They now read back inside `ask` too, while that question is live, exactly as
ask.requires does.

Run: python3 tests/test_ask_subject_recap_roundtrip.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="helm-askrecap-")
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
CONTEXT = ("This card exists to prove the round trip. The producer names the thing and says what "
           "it is, and the store has to hand both back unchanged inside the ask.")
SUBJECT = "Round trip probe card"
RECAP = ("A probe card that proves the cold read survives a save. It needs a decision so the "
         "check route has a real question to look at.")


def card(tid, title, prompt="Which way should this go?", **ask_extra):
    ask = {"kind": "choice", "prompt": prompt, "options": OPTIONS, "context": CONTEXT,
           "requires": ["decision"]}
    ask.update(ask_extra)
    return {"thread_id": tid, "title": title, "summary": "s " * 10, "category": "Test",
            "urgency": "this-week", "state": "needs-you",
            "sources": [{"source": "test", "id": tid, "note": ""}], "ask": ask}


def put(t):
    return api("PUT", "/api/triage/threads", {"threads": [t]})


def get(tid):
    code, th = api("GET", "/api/triage/threads/%s" % tid)
    return th if code == 200 else None


print("ask.subject and ask.recap round trip")

# 1. PUT with both, GET reads both back inside ask
code, res = put(card("sr-1", "Subject and recap round trip", subject=SUBJECT, recap=RECAP))
check("PUT with subject and recap lands", code == 200 and res.get("created") == 1, (code, res))
th = get("sr-1")
check("GET reads ask.subject back", th is not None and th["ask"].get("subject") == SUBJECT, th and th["ask"])
check("GET reads ask.recap back", th is not None and th["ask"].get("recap") == RECAP, th and th["ask"])
check("catchup still carries them",
      th["catchup"].get("subject") == SUBJECT and th["catchup"].get("recap") == RECAP, th["catchup"])

# 2. the stored card, as read, passes the gate (the rule that read the dropped fields)
code, res = api("POST", "/api/triage/needs-owner-check", {"thread": th})
check("the stored card re-passes the check route",
      code == 200 and res.get("needs_you") is True and not res.get("failed_test"), (code, res))
check("the stored card does not fail the subject/recap rule", "no-subject-recap" not in str(res), res)

# 3. the list read carries them too
code, lst = api("GET", "/api/triage/threads")
row = next((t for t in lst["threads"] if t["thread_id"] == "sr-1"), None)
check("the threads list carries ask.subject and ask.recap",
      row is not None and row["ask"].get("subject") == SUBJECT and row["ask"].get("recap") == RECAP,
      row and row["ask"])

# 4. a resync that does not resend them keeps them (same question)
put(card("sr-1", "Subject and recap round trip"))
th = get("sr-1")
check("a resync of the same question without them keeps them",
      th["ask"].get("subject") == SUBJECT and th["ask"].get("recap") == RECAP, th["ask"])

# 5. sending new words replaces them
put(card("sr-1", "Subject and recap round trip", subject="A newer subject", recap=RECAP + " Said again."))
th = get("sr-1")
check("sending them again replaces them",
      th["ask"].get("subject") == "A newer subject" and th["ask"].get("recap", "").endswith("Said again."),
      th["ask"])

# 6. a different question does not inherit the old words
put(card("sr-1", "Subject and recap round trip", prompt="Now a different question entirely?"))
th = get("sr-1")
check("a new question without them does not inherit the old ones",
      not th["ask"].get("subject") and not th["ask"].get("recap"), th["ask"])

# 7. a card that never stated them reads back without the keys
code, res = put(card("sr-none", "Subject and recap absent"))
th = get("sr-none")
check("a card with neither reads back without the keys",
      code == 200 and th is not None and "subject" not in th["ask"] and "recap" not in th["ask"],
      (code, th and th["ask"]))

print("%d passed, %d failed" % (PASSED, FAILED))
sys.exit(1 if FAILED else 0)
