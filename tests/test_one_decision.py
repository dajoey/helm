"""One card, one decision: the ask gate refuses a card that bundles several.

Run: python3 tests/test_one_decision.py   (from the repo root)

The failure (2026-10-03): a `choice` ask whose prompt announced "Five open calls ... the
one to tap", whose buttons answered ONE of them, and whose context held the other four as
numbered lines, each ending "Recommended: ...", under "reply by number on any you want
changed". A tap answers one question; the rest could only be answered as free text. Every
validator passed it. The rule was prose only (Playbooks/Talking To the owner section 6).

The gate now refuses four deterministic tells, at the producer's door (PUT), the bot's
door (result) and the self-check route, with one message that says what to do.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="helm-onedec-")
os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ.pop("HELM_SENTRY_DSN", None)

from starlette.testclient import TestClient  # noqa: E402

import app.service as svc  # noqa: E402
from helm import store  # noqa: E402

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print("  FAIL %s %s" % (name, str(detail)[:500]))


CLIENT = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
NOTE = "The run did the work and this is the long-enough report about it. " + "z" * 40
MSG = "more than one decision"


def api(method, path, payload=None):
    r = CLIENT.request(method, path, json=payload, headers=AUTH)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"raw": r.text}
    return r.status_code, body


CAPPED_PROMPT = ("Five open calls from the roof survey on one card, each with what I "
               "would do - the one to tap: move the scaffold hire to the smaller crew so the two "
               "rooms that are unusable this weekend get done tonight?")
CAPPED_CONTEXT = (
    "The roof survey found six items. Tap one below. The first four numbered "
    "lines need your word - reply by number on any you want changed; lines five and six "
    "are my calls, listed so nothing changes silently.\n"
    "1 Scaffold hire: the quote is 1,700, about a week. Recommended: approve it today.\n"
    "2 The cheaper weekday crew rate. Recommended: approve it.\n"
    "3 Two quotes name a material nobody stocks. Recommended: switch both to "
    "the standard sheet.\n"
    "4 The eight spare tiles stay in the shed until Monday. Recommended for Monday: use "
    "one box only if the ridge is dry.\n"
    "5 Decided, mine, reversible: the gutter clean moves onto the same visit.\n"
    "6 Decided, mine, reversible: leak checks pin to the surveyor's report.")
CAPPED_OPTIONS = [
    {"label": "the smaller crew takes the hire (Recommended)", "value": "smaller",
     "effect": "The scaffold hire moves to the smaller crew from tonight"},
    {"label": "The hire stays as it is", "value": "stay",
     "effect": "Nothing moves and the two rooms stay unusable"},
    {"label": "Trial: one room first", "value": "trial",
     "effect": "One room is done first and I report how it went"}]

GOOD_CONTEXT = ("The relay cert expires Friday. Renewing costs nothing but restarts the "
                "relay for a minute; letting it lapse stops mail to the site.")
GOOD_OPTIONS = [
    {"label": "Renew the cert today", "value": "renew", "effect": "I renew it now"},
    {"label": "Let it expire", "value": "expire", "effect": "nothing is renewed"}]

_n = [0]
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
         "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
         "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
         "xray", "yankee", "zulu"]


def title(prefix):
    """A title unique after T3's digit-stripping fingerprint: spell the counter."""
    _n[0] += 1
    n = _n[0]
    return "%s %s %s" % (prefix, WORDS[n % 26], WORDS[(n // 26 + 3 * n) % 26]), n


def card(prompt, options, context, kind="choice"):
    name, n = title("one decision probe")
    ask = {"kind": kind, "prompt": prompt, "context": context,
           "requires": ["decision"]}
    if kind == "choice":
        ask["options"] = options
    return {"thread_id": "od-%d" % n, "title": name,
            "summary": "a summary of the probe", "category": "Fleet",
            "urgency": "this-week", "state": "needs-you", "ask": ask}


def put(t):
    return api("PUT", "/api/triage/threads", {"threads": [t]})


def refused_at_put(t):
    code, res = put(t)
    errs = json.dumps(res)
    return (code == 400 or res.get("rejected_count") == 1), errs


def refused_at_result(prompt, options, context):
    name, n = title("bot door probe")
    tid = "od-bot-%d" % n
    code, res = put({"thread_id": tid, "title": name,
                     "summary": "s", "category": "Fleet", "urgency": "this-week",
                     "state": "needs-you",
                     "ask": {"kind": "choice", "prompt": "Chase it?", "context": GOOD_CONTEXT,
                             "requires": ["decision"], "options": GOOD_OPTIONS}})
    api("POST", "/api/triage/threads/%s/reply" % tid, {"value": "renew"})
    code, res = api("POST", "/api/triage/threads/%s/result" % tid, {
        "outcome": "asked", "state": "needs-you", "note": NOTE,
        "ask": {"kind": "choice", "prompt": prompt, "context": context,
                "requires": ["decision"], "options": options}})
    return code, json.dumps(res)


print("the assistant card, refused at every door")
t = card(CAPPED_PROMPT, CAPPED_OPTIONS, CAPPED_CONTEXT)
bad, errs = refused_at_put(t)
check("producer's door (PUT) refuses it", bad, errs)
check("with the plain one-decision message", MSG in errs and "own card" in errs, errs)
check("naming the tells that hit", "count" in errs and "by number" in errs
      and "recommended" in errs, errs)
code, res = refused_at_result(CAPPED_PROMPT, CAPPED_OPTIONS, CAPPED_CONTEXT)
check("bot's door (result) refuses it with a 400", code == 400 and MSG in res, (code, res))
code, res = api("POST", "/api/triage/needs-owner-check", {"thread": t})
check("the self-check route says needs_you:false and why (200, the answer is data)",
      code == 200 and res.get("needs_you") is False and MSG in res.get("detail", ""),
      (code, res))
check("the probe stored nothing", store.db(os.environ["HELM_DB"]).execute(
    "SELECT COUNT(*) FROM threads WHERE thread_id=?", (t["thread_id"],)).fetchone()[0] == 0)

print("each tell refuses alone")
SINGLE = "Renew the relay cert now or let it lapse?"
tells = [
    ("a count of decisions in the prompt",
     "Three open decisions on the relay cert: which one to tap first?", GOOD_CONTEXT, "count"),
    ("a digit count of questions",
     "I have 4 questions about the relay cert, so which do we start with?", GOOD_CONTEXT, "count"),
    ("answers routed 'by number'", SINGLE,
     GOOD_CONTEXT + " Tap one below, and reply by number on anything else you want changed.",
     "by number"),
    ("answer by line", SINGLE,
     GOOD_CONTEXT + " Please answer on the other points by line in the note.", "by number"),
    ("two recommended numbered lines", SINGLE,
     GOOD_CONTEXT + "\n1 The scaffold hire is unbudgeted. Recommended: approve it.\n"
     "2 An agent runs an unsupported setting. Recommended: switch it.", "recommended"),
    ("two question sentences in the prompt",
     "Renew the relay cert now? And should the site mail relay move to the new host?",
     GOOD_CONTEXT, "question"),
]
for name, prompt, context, tell in tells:
    t = card(prompt, GOOD_OPTIONS, context)
    bad, errs = refused_at_put(t)
    check("refused at PUT: " + name, bad and MSG in errs, errs)
    check("and the message names the tell (%s): %s" % (tell, name), tell in errs, errs)
    code, res = refused_at_result(prompt, GOOD_OPTIONS, context)
    check("refused at the bot's door: " + name, code == 400 and MSG in res, (code, res))

print("single-decision cards still go through")
single = card(
    "Which relay cert plan do we take?",
    [{"label": "Renew the cert today", "value": "renew", "effect": "I renew it now (Recommended)"},
     {"label": "Let it expire", "value": "expire", "effect": "nothing is renewed"},
     {"label": "Renew and rotate the key", "value": "rotate", "effect": "I renew and rotate the key"},
     {"label": "Move to a new CA", "value": "ca", "effect": "I file for a cert from the new CA"}],
    "The relay cert expires Friday; four ways to handle it.\n"
    "1. Renew today: costs nothing, restarts the relay for a minute (Recommended).\n"
    "2. Let it expire: stops mail to the site.\n"
    "3. Renew and rotate the key: renew plus a new signing key.\n"
    "4. Move to a new CA: a longer job and a new vendor.")
bad, errs = refused_at_put(single)
check("one decision, four options, numbered paragraphs, ONE Recommended: accepted",
      not bad, errs)
verdict = card(
    "Did the fixes land on your phone?",
    [{"label": "All 3 work", "value": "ok", "effect": "I close the thread"},
     {"label": "Some don't", "value": "some", "effect": "I reopen it and ask which"},
     {"label": "Haven't played it yet", "value": "later", "effect": "I leave it open"}],
    "Three fixes shipped to the app today and you need to try them. The fixes:\n"
    "1. The settings page no longer shows the old tag.\n"
    "2. The label bar stays small.\n"
    "3. A missing note shows a plain page.")
bad, errs = refused_at_put(verdict)
check("the verdict card with a numbered list of fixes (no per-line recommended): accepted",
      not bad, errs)
bad, errs = refused_at_put(card("Read this and close it.", None, "", kind="ack"))
check("a plain ack is accepted", not bad, errs)
bad, errs = refused_at_put(card("Which cert did you mean?", None, GOOD_CONTEXT, kind="text"))
check("a text ask is accepted", not bad, errs)
code, res = api("POST", "/api/triage/needs-owner-check", {"thread": single})
check("the self-check route passes the single-decision card",
      code == 200 and res.get("needs_you") is True, (code, res))

print("the check is callable on its own")
check("one_decision_errors is empty for a clean card",
      store.one_decision_errors("choice", SINGLE, GOOD_OPTIONS, GOOD_CONTEXT) == [])
check("and ignores asks that are not choices",
      store.one_decision_errors("text", CAPPED_PROMPT, [], CAPPED_CONTEXT) == [])

print("\n%d passed, %d failed" % (PASSED, FAILED))
sys.exit(1 if FAILED else 0)
