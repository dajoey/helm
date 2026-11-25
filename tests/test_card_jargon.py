"""The bookkeeping-jargon card rule: an ask written in another system's internal vocabulary is refused.

Run: python3 tests/test_card_jargon.py   (from the repo root)

The failure: a goal reviewer filed a choice ask ("One frozen success rule for the loop guard cannot be
met as written; amend its window to the post-fix sessions?") whose every noun was bookkeeping the
reader had never seen: frozen success rule, window, post-fix, noticed sessions, sealed records,
round. Every validator passed it, and the card could not be answered until it was rewritten in plain
words. The rule `bookkeeping-jargon` (helm/card_rules.py) now reads the subject, recap, prompt,
context and every option for that vocabulary. In reject mode, which production sets, the PUT door,
the /result door and the check route refuse it with a sentence that names the vocabulary and says
what to write instead.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="helm-card-jargon-")
os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ["HELM_SITE"] = "home"
os.environ.pop("HELM_SENTRY_DSN", None)
os.environ["HELM_CARD_RULES"] = "bookkeeping-jargon=reject"

from starlette.testclient import TestClient  # noqa: E402

import app.service as svc  # noqa: E402
from helm import card_rules as cr  # noqa: E402

PASS = FAIL = 0
CLIENT = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
RULE = "bookkeeping-jargon"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:600] if detail != "" else ""))


def api(method, path, payload=None):
    r = CLIENT.request(method, path, json=payload, headers=AUTH)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"raw": r.text}
    return r.status_code, body


# The filed ask, as it reached the gate.
BAD_ASK = {
    "kind": "choice", "requires": "decision",
    "subject": "Loop guard goal",
    "recap": "The loop guard goal cannot finish on its own and needs a call from you.",
    "prompt": "One frozen success rule for the loop guard cannot be met as written; amend its window "
              "to the post-fix sessions?",
    "context": ("Why it stopped: Since the wording fix, five of seven noticed worker sessions answered "
                "in their docs. The frozen rule instead names the first three noticed after install "
                "- before the fix - and those docs are sealed records nobody may edit, so no round "
                "can satisfy it as written. Amend: the rule reads 'at least three post-fix noticed "
                "sessions answered'."),
    "options": [
        {"label": "Amend to post-fix sessions (Recommended)", "value": "amend",
         "effect": "The goal is filed as done."},
        {"label": "Keep the frozen wording", "value": "keep", "effect": "The goal is filed as abandoned."},
        {"label": "Wait for more sessions first", "value": "wait", "effect": "The goal stays open."}],
}
# The same question in plain words, as it was rewritten in the session.
PLAIN_ASK = {
    "kind": "choice", "requires": "decision",
    "subject": "Loop guard goal",
    "recap": "The loop guard is built and working, but an agent's checklist item for it cannot be met.",
    "prompt": "File the loop guard goal as done, even though one checklist item cannot be met?",
    "context": ("The loop guard is built and working. An agent's checklist item said the first 3 "
                "workers warned would reply; they did not, because the warning was badly worded. "
                "After the fix 5 of 7 did. Nothing changes on any machine; this only decides whether "
                "the goal is filed as done or abandoned."),
    "options": [
        {"label": "File it as done (Recommended)", "value": "done",
         "effect": "Nothing changes on any machine; the goal is filed as done."},
        {"label": "File it as abandoned", "value": "abandon",
         "effect": "Nothing changes on any machine; the goal is filed as abandoned."}],
}
_N = [0]


def card(ask):
    _N[0] += 1
    return {"thread_id": "cj-%03d" % _N[0], "title": "A jargon probe %s" % ("abcdefghij"[_N[0] % 10] * 3),
            "summary": "A probe card, never shown.", "category": "Test", "urgency": "whenever",
            "state": "needs-you", "responder": "agent", "ask": copy.deepcopy(ask)}


def fired(a):
    return cr.evaluate(a["kind"], a.get("prompt"), a.get("context"), a.get("options"),
                       a.get("subject"), a.get("recap"))


def mutated(**fields):
    a = copy.deepcopy(PLAIN_ASK)
    a.update(fields)
    return a


print("\n1. the filed ask is refused at every door, with a sentence that says what to write")
check("the rule exists", RULE in cr.RULE_IDS, cr.RULE_IDS)
check("the filed ask fires it", RULE in fired(BAD_ASK), fired(BAD_ASK))
check("the plain rewrite fires nothing", fired(PLAIN_ASK) == [], fired(PLAIN_ASK))

t = card(BAD_ASK)
code, res = api("POST", "/api/triage/needs-owner-check", {"thread": t})
detail = str(res.get("detail") or "")
check("check route: needs_you false, failed on validation",
      code == 200 and res.get("needs_you") is False and res.get("failed_test") == "validation", (code, res))
check("check route: the detail names the rule and the vocabulary", RULE in detail and "frozen" in detail
      and "success criteria" in detail and "sealed records" in detail, detail)
check("check route: the detail says what to write instead",
      "what was built" in detail and "plain words" in detail and "nothing changes" in detail.lower(), detail)
check("check route: the detail does not quote the card", "loop guard" not in detail.lower(), detail)

code, res = api("PUT", "/api/triage/threads", {"threads": [card(BAD_ASK)]})
body = json.dumps(res)
check("PUT door: refused (400 or rejected)", code == 400 or res.get("rejected_count") == 1, (code, res))
check("PUT door: the refusal names the rule", RULE in body, body)

tid = "cj-result"
code, res = api("PUT", "/api/triage/threads", {"threads": [dict(card(PLAIN_ASK), thread_id=tid,
                                                                   title="A result door probe")]})
check("result door setup: the plain ask is stored", code == 200 and res.get("created") == 1, (code, res))
api("POST", "/api/triage/threads/%s/reply" % tid, {"value": "done"})
note = "The run did the work and this is the long-enough report about it. " + "z" * 40
code, res = api("POST", "/api/triage/threads/%s/result" % tid,
                {"outcome": "asked", "state": "needs-you", "note": note, "ask": BAD_ASK})
check("result door: 400 naming the rule", code == 400 and RULE in json.dumps(res), (code, res))
code, res = api("POST", "/api/triage/threads/%s/result" % tid,
                {"outcome": "asked", "state": "needs-you", "note": note, "ask": PLAIN_ASK})
check("result door: the plain rewrite is accepted", code in (200, 202), (code, res))

code, res = api("POST", "/api/triage/needs-owner-check", {"thread": card(PLAIN_ASK)})
check("check route: the plain rewrite passes", code == 200 and res.get("needs_you") is True
      and res.get("warnings") == [], (code, res))

print("\n2. each piece of vocabulary fires on its own, in any field he reads")
TERMS = {
    "frozen rule": "The frozen rule says three workers must reply.",
    "frozen wording": "Keep the frozen wording?",
    "frozen success criterion": "Edit the frozen success criterion?",
    "rule that is frozen": "The checklist rule is now frozen.",
    "success criteria": "The success criteria name three workers.",
    "success criterion": "The one success criterion was missed.",
    "success rule": "The success rule cannot be met.",
    "sealed records": "Those docs are sealed records.",
    "sealed record": "That is a sealed record.",
    "post-fix": "Count the post-fix sessions.",
    "pre-fix": "Ignore the pre-fix sessions.",
    "post fix": "Count the post fix sessions.",
    "longitudinal": "A longitudinal check of workers.",
    "round 3": "Nothing could pass it in round 3.",
    "Round 12": "Round 12 failed.",
    "planning round": "The planning round ended.",
    "review round": "The review round ended.",
    "goal review": "The goal review stopped.",
    "goal reviewer": "The goal reviewer stopped.",
    "criterion id D1": "Criterion D1 failed.",
    "criterion id F12": "Check F12 failed.",
    "id then noun": "The D1 numbers were retuned.",
    "id then fails": "F3 fails on the new wording.",
    "noticed sessions": "Five of seven noticed sessions answered.",
}
for name, text in TERMS.items():
    for field in ("prompt", "context", "recap", "subject"):
        a = mutated(**{field: text})
        if field == "prompt":
            a["prompt"] = text + " Which do you pick?"
        if field == "recap":
            a["recap"] = text + " It needs you."
        if field == "context":
            a["context"] = PLAIN_ASK["context"] + " " + text
        got = fired(a)
        check("%s in the %s" % (name, field), got == [RULE], got)
    for where, edit in (("option label", lambda a: a["options"][1].update(label=text[:58])),
                        ("option effect", lambda a: a["options"][1].update(effect=text))):
        a = copy.deepcopy(PLAIN_ASK)
        edit(a)
        got = fired(a)
        check("%s in the %s" % (name, where), got == [RULE], got)

print("\n3. precision: ordinary words do not fire")
QUIET = [
    "The lake is frozen over, so the cabin trip is off.",
    "The frozen food order arrives Friday.",
    "A frozen process on the NAS needs a restart.",
    "The success of the backup depends on the key.",
    "The window is closed for the evening.",
    "Round up the three pending invoices.",
    "Press F5 to reload the page, or open the D drive.",
    "Vitamin D3 and the F150 are not on the checklist for this.",
    "The longest path is the slow one.",
    "A post-mortem of the outage is attached.",
    "The sealed envelope arrived at the office.",
    "It rounds to 3 workers.",
    "A fix was made after the failure; 5 of 7 workers replied.",
    "Review the goal of the project.",
    "The criteria for a good backup are on the page.",
]
for text in QUIET:
    a = mutated(context=PLAIN_ASK["context"] + " " + text)
    check("quiet: %s" % text[:60], RULE not in fired(a), fired(a))
ack = {"kind": "ack", "prompt": "The goal review finished; nothing to do."}
check("an ack is read too (it is a card he reads)", RULE in fired(ack), fired(ack))
check("no ask, no rule", cr.evaluate("none", "frozen success rule") == [])

print("\n4. modes: log warns without refusing, off is silent")
old = os.environ["HELM_CARD_RULES"]
os.environ["HELM_CARD_RULES"] = ""
try:
    check("unset means log", cr.modes()[RULE] == "log", cr.modes())
    code, res = api("POST", "/api/triage/needs-owner-check", {"thread": card(BAD_ASK)})
    check("log: the card still passes and carries the warning",
          code == 200 and res.get("needs_you") is True and res.get("warnings") == [RULE], (code, res))
    os.environ["HELM_CARD_RULES"] = "%s=off" % RULE
    code, res = api("POST", "/api/triage/needs-owner-check", {"thread": card(BAD_ASK)})
    check("off: no warning", code == 200 and res.get("needs_you") is True and res.get("warnings") == [],
          (code, res))
finally:
    os.environ["HELM_CARD_RULES"] = old

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
