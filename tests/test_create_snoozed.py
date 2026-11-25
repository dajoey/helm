"""A card can be created already snoozed: POST /api/triage/threads/create with `wake_at`.

A producer files a card for the owner that carries a `when`. With no create-time
snooze it had to create the card (lit: needs-you, listed, a phone poll sees it) and then call
POST .../snooze a second later, so a poll landing in that gap buzzed the owner for a card that should
not surface until its time. `wake_at` (epoch seconds or ISO-8601, the same field the snooze route
takes) on the create body now writes the card hidden in the same insert: it is never in the plain
list, not even for one read, and it wakes exactly as a snoozed card does, with its Done ask.

Run: python3 tests/test_create_snoozed.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="helm-createsnooze-")
os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ.pop("HELM_SENTRY_DSN", None)

from starlette.testclient import TestClient  # noqa: E402

import app.service as svc  # noqa: E402
import helm.store as store  # noqa: E402

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


def listed(tid, snoozed=False):
    _, body = api("GET", "/api/triage/threads" + ("?include_snoozed=1" if snoozed else ""))
    rows = body.get("threads", body) if isinstance(body, dict) else body
    return [t for t in rows if t.get("thread_id") == tid]


def card(title, **kw):
    d = {"title": title, "summary": "A card filed from a chat session.", "category": "Test",
         "urgency": "whenever", "intent": "mine"}
    d.update(kw)
    return d


print("create already snoozed")
now = time.time()
wake = now + 3 * 3600

code, made = api("POST", "/api/triage/threads/create", card("Chase the invoice later", wake_at=wake))
check("create with wake_at is 201", code == 201, made)
tid = made.get("thread_id", "")
check("the create response says snoozed with the wake time",
      made.get("state") == "snoozed" and abs(float(made.get("wake_at") or 0) - wake) < 1
      and abs(float(made.get("hidden_until") or 0) - wake) < 1, made)
check("born snoozed: not in the plain list on the first read", listed(tid) == [], listed(tid))
shown = listed(tid, snoozed=True)
check("visible with include_snoozed, wearing wake_at in the future",
      len(shown) == 1 and float(shown[0].get("wake_at") or 0) > now, shown)

con = store.db()
row = con.execute("SELECT * FROM threads WHERE thread_id=?", (tid,)).fetchone()
check("stored hidden and not lit: no live ask, wake_at set",
      row["wake_at"] > now and row["ask_kind"] == "none" and row["state"] == "waiting-external",
      dict(row))
check("the question it will wake with is stashed",
      '"Done"' in (row["wake_ask"] or "") or "done" in (row["wake_ask"] or ""), row["wake_ask"])
ev = [e["body"] for e in con.execute("SELECT body FROM events WHERE thread_id=?", (tid,))]
check("history says it was snoozed until a time", any(b.startswith("snoozed until") for b in ev), ev)

print("wakes like any snoozed card")
con.execute("UPDATE threads SET wake_at=? WHERE thread_id=?", (time.time() - 5, tid))
con.commit()
store.wake_due(con)
lit = listed(tid)
check("after its time the card is back in the plain list, lit with Done",
      len(lit) == 1 and lit[0].get("state") == "needs-you"
      and (lit[0].get("ask") or {}).get("kind") == "choice", lit)

print("ISO date, note, and plain create")
iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now + 86400))
code, made = api("POST", "/api/triage/threads/create",
                 card("Back tomorrow", wake_at=iso, wake_note="Back tomorrow, as asked"))
check("ISO-8601 wake_at is accepted and hidden",
      code == 201 and made.get("state") == "snoozed" and listed(made.get("thread_id")) == [], made)
con = store.db()
row = con.execute("SELECT wake_note FROM threads WHERE thread_id=?", (made.get("thread_id"),)).fetchone()
check("wake_note is kept", row and "Back tomorrow, as asked" in row["wake_note"], row and dict(row))

code, made = api("POST", "/api/triage/threads/create", card("No wake given"))
check("without wake_at the card is lit as before",
      code == 201 and made.get("state") == "needs-you" and len(listed(made.get("thread_id"))) == 1, made)
code, made = api("POST", "/api/triage/threads/create", card("Empty wake", wake_at=""))
check("an empty wake_at is the same as none", code == 201 and made.get("state") == "needs-you", made)

print("refusals write nothing")
before = len(store.db().execute("SELECT 1 FROM threads").fetchall())
for name, extra in (
        ("past", {"wake_at": now - 60}),
        ("garbage", {"wake_at": "next tuesdayish"}),
        ("over 400 days", {"wake_at": now + 500 * 86400}),
        ("hand-off to a run", {"intent": "agent", "instruction": "do it", "wake_at": wake}),
        ("object", {"wake_at": {"a": 1}})):
    code, res = api("POST", "/api/triage/threads/create", card("Refused " + name, **extra))
    check("%s is a 400" % name, code == 400, (code, res))
after = len(store.db().execute("SELECT 1 FROM threads").fetchall())
check("none of the refused creates left a card", after == before, (before, after))

print("%d passed, %d failed" % (PASSED, FAILED))
sys.exit(1 if FAILED else 0)
