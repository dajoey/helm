"""A bearer-only caller is recorded as itself on reply/close/backlog, never as the owner (home),
once HELM_BEARER_ACTOR_STRICT is on. Off (the default), a mobile app keeps working.

Run: python3 tests/test_shared_token_actor.py   (from the repo root)

Part 1 is the exact Android request (shared bearer, `{value}` reply, `{reason}` close, no
actor header): with the switch off it still succeeds as the owner. Part 2 turns the switch on: the
shared HELM_APP_TOKEN has many holders, so a missing actor, or a `owner` claim with no signed-in
login behind it, is recorded as the caller (the per-caller token's principal, else "api").
the owner's own paths stay the owner: the web role's X-Helm-Actor, and a per-caller token whose
principal is owner.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

os.environ["HELM_APP_TOKEN"] = "test-token"
TMP = tempfile.mkdtemp(prefix="helm-actor-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")

import app.service as svc  # noqa: E402
from helm import actions, site, store, tokens  # noqa: E402

client = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
LOGIN = dict(AUTH)
LOGIN["X-Helm-Actor"] = "owner"

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


_con = svc._db()
_tok = {p: tokens.add(_con, p) for p in ("log-watch", "owner")}
_con.close()


def bearer(p):
    return {"Authorization": "Bearer " + _tok[p]}


def lit(tid, kind="choice"):
    ask = {"kind": kind, "prompt": "Pick one", "requires": ["decision"]}
    if kind == "choice":
        ask["options"] = [{"label": "Renew the cert and rotate the key", "value": "do", "effect": "Done"},
                          {"label": "Leave it alone for now", "value": "no", "effect": "Nothing"}]
    r = client.put("/api/triage/threads", json={"threads": [{
        "thread_id": tid, "title": "Rotate the relay cert " + tid, "summary": "s " * 10,
        "category": "Test", "urgency": "this-week", "state": "needs-you", "ask": ask,
        "sources": [{"source": "test", "id": tid, "note": ""}]}]}, headers=AUTH)
    assert r.status_code == 200, r.text


def ev(tid, kind):
    evs = [e for e in client.get("/api/triage/threads/" + tid, headers=AUTH).json()["events"] if e["kind"] == kind]
    return evs[-1] if evs else {}


def post(path, body, headers=AUTH):
    return client.post(path, json=body, headers=headers)


# --- part 1: switch off (the default). The mobile app: shared bearer alone, no actor anywhere.
os.environ.pop("HELM_BEARER_ACTOR_STRICT", None)
lit("a1")
r = post("/api/triage/threads/a1/reply", {"value": "do"})
check("android reply {value} is accepted", r.status_code == 202, r.text[:200])
check("... and is the owner's tap", ev("a1", "owner-reply").get("source") == "owner", ev("a1", "owner-reply"))
lit("a2", kind="ack")
r = post("/api/triage/threads/a2/close", {"reason": "handled it myself"})
check("android close {reason} is accepted", r.status_code == 202, (r.status_code, r.text[:200]))
check("... as the owner", ev("a2", "state-change").get("source") == "owner", ev("a2", "state-change"))
lit("a3")
post("/api/triage/threads/a3/backlog", {"reason": "not now"})
check("backlog with no actor keeps the site default", ev("a3", "backlog").get("source") == "owner", ev("a3", "backlog"))
lit("a4")
post("/api/triage/threads/a4/reply", {"value": "do", "by": "owner"})
check("an explicit by:owner is still the owner with the switch off", ev("a4", "owner-reply").get("source") == "owner",
      ev("a4", "owner-reply"))
check("door_actor off: nothing substituted", actions.door_actor(None, None, "api") == (None, False))

# --- part 2: switch on
os.environ["HELM_BEARER_ACTOR_STRICT"] = "1"
lit("r1")
post("/api/triage/threads/r1/reply", {"value": "do", "note": "done"})
check("shared token, no actor: the reply is api's", ev("r1", "owner-reply").get("source") == "api", ev("r1", "owner-reply"))
check("... and so is its note", ev("r1", "owner-note").get("source") == "api", ev("r1", "owner-note"))

lit("r2")
post("/api/triage/threads/r2/reply", {"value": "do"}, bearer("log-watch"))
check("per-caller token, no actor: the reply is its principal's",
      ev("r2", "owner-reply").get("source") == "log-watch", ev("r2", "owner-reply"))

lit("r3")
post("/api/triage/threads/r3/reply", {"value": "do"}, LOGIN)
check("a forwarded login stays the owner", ev("r3", "owner-reply").get("source") == "owner", ev("r3", "owner-reply"))

lit("r4")
post("/api/triage/threads/r4/reply", {"value": "do", "by": "owner"})
check("shared token claiming by:owner is api's", ev("r4", "owner-reply").get("source") == "api", ev("r4", "owner-reply"))
lit("r5")
post("/api/triage/threads/r5/reply", {"value": "do", "actor": "owner"})
check("... and actor:owner too", ev("r5", "owner-reply").get("source") == "api", ev("r5", "owner-reply"))
lit("r6")
post("/api/triage/threads/r6/reply", {"value": "do", "by": "owner"}, bearer("log-watch"))
check("a per-caller token claiming owner is its principal's",
      ev("r6", "owner-reply").get("source") == "log-watch", ev("r6", "owner-reply"))

lit("r7")
post("/api/triage/threads/r7/reply", {"value": "do"}, bearer("owner"))
check("a per-caller token whose principal is owner is the owner", ev("r7", "owner-reply").get("source") == "owner",
      ev("r7", "owner-reply"))

lit("r8")
post("/api/triage/threads/r8/reply", {"value": "do", "by": "owner"}, LOGIN)
check("login plus by:owner is the owner", ev("r8", "owner-reply").get("source") == "owner", ev("r8", "owner-reply"))

# --- hand-off keeps its rule
lit("h1")
post("/api/triage/threads/h1/reply", {"value": "__task__", "note": "Re-arm it and report.", "by": "owner"})
check("a bearer-only __task__ claiming owner is api's", ev("h1", "owner-note").get("source") == "api",
      ev("h1", "owner-note"))

# --- close: only the owner closes at home, so the caller's own name is refused
lit("c1", kind="ack")
r = post("/api/triage/threads/c1/close", {"reason": "done"})
check("shared token close with no actor is refused", r.status_code == 400 and "api" in r.text, (r.status_code, r.text[:200]))
check("... and the card stays open", client.get("/api/triage/threads/c1", headers=AUTH).json()["state"] == "needs-you")
r = post("/api/triage/threads/c1/close", {"reason": "done", "actor": "owner"})
check("shared token close claiming owner is refused", r.status_code == 400, (r.status_code, r.text[:200]))
r = post("/api/triage/threads/c1/close", {"reason": "done"}, LOGIN)
check("a forwarded login closes", r.status_code == 202, (r.status_code, r.text[:200]))
check("... as the owner", ev("c1", "state-change").get("source") == "owner", ev("c1", "state-change"))
lit("c2", kind="ack")
r = post("/api/triage/threads/c2/close", {"reason": "done"}, bearer("owner"))
check("a per-caller token whose principal is owner closes", r.status_code == 202, (r.status_code, r.text[:200]))

# --- park / backlog default their source to the caller
lit("b1")
post("/api/triage/threads/b1/backlog", {"reason": "not now"})
check("backlog with no actor is the caller's", ev("b1", "backlog").get("source") == "api", ev("b1", "backlog"))
post("/api/triage/threads/b1/unbacklog", {"note": "now"}, bearer("log-watch"))
check("unbacklog by a per-caller token is its principal's", ev("b1", "unbacklog").get("source") == "log-watch",
      ev("b1", "unbacklog"))
lit("b2")
post("/api/triage/threads/b2/backlog", {"reason": "not now"}, LOGIN)
check("backlog from the web role is the owner's", ev("b2", "backlog").get("source") == "owner", ev("b2", "backlog"))

# --- part 3: switch off, but a principal listed in HELM_BEARER_STRICT_PRINCIPALS is always
# recorded as itself (Relay answers on the owner's word; its answers must never read as his tap).
os.environ.pop("HELM_BEARER_ACTOR_STRICT", None)
os.environ["HELM_BEARER_STRICT_PRINCIPALS"] = " relay-mcp , other-relay "
_con = svc._db()
_tok["relay-mcp"] = tokens.add(_con, "relay-mcp")
_con.close()

lit("p1")
r = post("/api/triage/threads/p1/reply", {"value": "do", "note": "on the owner's word"}, bearer("relay-mcp"))
check("listed principal: the reply is accepted", r.status_code == 202, r.text[:200])
check("... and recorded as the principal, not the owner", ev("p1", "owner-reply").get("source") == "relay-mcp",
      ev("p1", "owner-reply"))
check("... and so is its note", ev("p1", "owner-note").get("source") == "relay-mcp", ev("p1", "owner-note"))
check("... and the card moved on", client.get("/api/triage/threads/p1", headers=AUTH).json()["state"] != "needs-you")
lit("p2")
post("/api/triage/threads/p2/reply", {"value": "do", "by": "owner"}, bearer("relay-mcp"))
check("listed principal claiming by:owner is still itself", ev("p2", "owner-reply").get("source") == "relay-mcp",
      ev("p2", "owner-reply"))
lit("p3")
post("/api/triage/threads/p3/reply", {"value": "do"}, bearer("log-watch"))
check("an unlisted principal keeps the site default (the owner)", ev("p3", "owner-reply").get("source") == "owner",
      ev("p3", "owner-reply"))
lit("p4")
post("/api/triage/threads/p4/reply", {"value": "do"})
check("the shared token (mobile) keeps the site default (the owner)", ev("p4", "owner-reply").get("source") == "owner",
      ev("p4", "owner-reply"))
lit("p5", kind="ack")
r = post("/api/triage/threads/p5/close", {"reason": "done"}, bearer("relay-mcp"))
check("a listed principal cannot close a home card", r.status_code == 400, (r.status_code, r.text[:200]))
# A strict caller that NAMES a non-owner actor (a future client edit) is still recorded as
# itself: the name is the caller's own claim, the event source is the token's principal.
lit("n1")
r = post("/api/triage/threads/n1/reply", {"value": "do", "by": "bot", "note": "named"}, bearer("relay-mcp"))
check("listed principal naming by:bot is accepted", r.status_code == 202, r.text[:200])
check("... the reply is the principal's, never the owner's", ev("n1", "owner-reply").get("source") == "relay-mcp", ev("n1", "owner-reply"))
check("... and so is its note", ev("n1", "owner-note").get("source") == "relay-mcp", ev("n1", "owner-note"))
lit("n2")
post("/api/triage/threads/n2/reply", {"value": "do", "actor": "Relay", "note": "named"}, bearer("relay-mcp"))
check("listed principal naming actor:'Relay' is the principal's reply",
      ev("n2", "owner-reply").get("source") == "relay-mcp", ev("n2", "owner-reply"))
check("... and note", ev("n2", "owner-note").get("source") == "relay-mcp", ev("n2", "owner-note"))
lit("n3")
post("/api/triage/threads/n3/reply", {"value": "do", "by": "bot"}, bearer("log-watch"))
check("an unlisted principal naming bot keeps the site default source (switch off)",
      ev("n3", "owner-reply").get("source") == "owner", ev("n3", "owner-reply"))
lit("n4")
post("/api/triage/threads/n4/reply", {"value": "do", "by": "bot"}, LOGIN)
check("a signed-in login naming bot keeps its say (no substitution)", ev("n4", "owner-reply").get("source") == "owner",
      ev("n4", "owner-reply"))
# The global switch: any strict caller that names a non-owner actor.
os.environ["HELM_BEARER_ACTOR_STRICT"] = "1"
lit("n5")
post("/api/triage/threads/n5/reply", {"value": "do", "by": "bot", "note": "n"})
check("global strict: the shared token naming bot is api's, not the owner's", ev("n5", "owner-reply").get("source") == "api",
      ev("n5", "owner-reply"))
check("... and its note", ev("n5", "owner-note").get("source") == "api", ev("n5", "owner-note"))
lit("n6")
post("/api/triage/threads/n6/reply", {"value": "do", "by": "bot"}, bearer("owner"))
check("global strict: the owner principal naming bot stays the owner's source", ev("n6", "owner-reply").get("source") == "owner",
      ev("n6", "owner-reply"))
os.environ.pop("HELM_BEARER_ACTOR_STRICT", None)
check("door_actor: listed caller substitutes", actions.door_actor(None, None, "relay-mcp") == ("relay-mcp", True))
check("door_actor: unlisted caller does not", actions.door_actor(None, None, "log-watch") == (None, False))
os.environ.pop("HELM_BEARER_STRICT_PRINCIPALS", None)

check("an explicit owner is the owner", actions.door_actor("owner", None, "api") == ("owner", False))

# --- part 4, work: a principal listed in HELM_BEARER_STRICT_PRINCIPALS (Relay's the reference app
# connector, `relay`) is recorded as itself, not as a nameless chat session and never as the owner,
# on an answer and on a hand-off it files. Every other work caller keeps the session rule.
os.environ["HELM_BEARER_STRICT_PRINCIPALS"] = "relay"
_con = svc._db()
_tok["relay"] = tokens.add(_con, "relay")
_con.close()
lit("w1")
r = post("/api/triage/threads/w1/reply", {"value": "do", "note": "on the owner's word"}, bearer("relay"))
check("a listed principal's answer is accepted", r.status_code == 202, r.text[:200])
check("... recorded as the principal", ev("w1", "owner-reply").get("source") == "relay", ev("w1", "owner-reply"))
check("... and so is its note", ev("w1", "owner-note").get("source") == "relay", ev("w1", "owner-note"))
lit("w2")
post("/api/triage/threads/w2/reply", {"value": "do", "by": "owner"}, bearer("relay"))
check("a listed principal claiming by:owner is still itself", ev("w2", "owner-reply").get("source") == "relay",
      ev("w2", "owner-reply"))
lit("w4")
post("/api/triage/threads/w4/reply", {"value": "do", "by": "owner"})
check("the shared token's explicit owner is still the owner", ev("w4", "owner-reply").get("source") == "owner",
      ev("w4", "owner-reply"))
r = post("/api/triage/threads/create", {"title": "[Relay] Check the relay cert", "summary": "From Relay.",
                                         "category": "Test", "urgency": "whenever", "intent": "agent",
                                         "instruction": "Read the cert dates and report."}, bearer("relay"))
check("a listed principal's hand-off is filed", r.status_code == 201, r.text[:200])
htid = (r.json() or {}).get("thread_id") or ""
check("... its hand-off answer is the principal's", ev(htid, "owner-reply").get("source") == "relay",
      ev(htid, "owner-reply"))
check("... and its brief", ev(htid, "owner-note").get("source") == "relay", ev(htid, "owner-note"))
check("door_source names the listed caller", actions.door_source(None, "relay", "session") == "relay")
check("door_source leaves an unlisted caller alone", actions.door_source(None, "log-watch", "session") is None)
check("a signed-in login is never substituted", actions.door_source("owner", "relay", "session") is None)
os.environ.pop("HELM_BEARER_STRICT_PRINCIPALS", None)
site.reset()

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
