"""The card-shape rules at the needs-owner gate (helm/card_rules.py), in log mode.

Seven lines of Playbooks/Talking To the owner that the gate did not read: a time word, machine text (a
commit id, a path, a task id), no option marked (Recommended), no subject or recap, a bare-verb
option label, an empty context, another system's bookkeeping words (tests/test_card_jargon.py). A live probe of POST /api/triage/needs-owner-check on 2026-10-14
answered "all four tests pass" for a card breaking each of them.

Now each rule has a mode from HELM_CARD_RULES (off | log | reject, default log). In log mode:
  * the check route answers `warnings: [rule ids]` and its decision does not change;
  * every door (check, PUT, /result) records one row per rule per question: rule id, thread id,
    principal, author, time, and a hash of the question -- never card text;
  * GET /api/triage/card-rules counts them per rule, per principal and per author for a window.
Reject mode refuses the card with a sentence that names the rule and never quotes the card; off
mode neither warns nor records.

The bad cards are the probe's synthetic variants of one good card (scratchpad gate_probe.py).
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="helm-card-rules-")
DB = os.path.join(TMP, "t.db")
os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_DB"] = DB
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ["HELM_SITE"] = "home"
os.environ.pop("HELM_SENTRY_DSN", None)
os.environ.pop("HELM_CARD_RULES", None)

from starlette.testclient import TestClient  # noqa: E402

import app.service as svc  # noqa: E402
from helm import card_rules as cr  # noqa: E402
from helm import store  # noqa: E402
from helm import tokens  # noqa: E402

PASS = FAIL = 0
CLIENT = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:600] if detail != "" else ""))


# --- the cards -------------------------------------------------------------------------------------
GOOD_ASK = {
    "kind": "choice", "requires": "decision",
    "subject": "Vault write access",
    "recap": "The backup job needs write access to the vault. Only you can grant it.",
    "prompt": "Should the backup job get write access to the vault?",
    "context": ("1. Granting write access lets the nightly backup finish; it fails each night now.\n"
                "2. Keeping read-only means backups stay incomplete."),
    "options": [{"label": "Grant write access (Recommended)", "value": "grant",
                 "effect": "The nightly backup gets write access and completes."},
                {"label": "Keep it read-only", "value": "keep",
                 "effect": "Backups stay incomplete until another route is found."}],
}
_N = [0]


def card(mutate=None, title="Vault write access for the backup job"):
    _N[0] += 1
    a = copy.deepcopy(GOOD_ASK)
    if mutate:
        mutate(a)
    return {"thread_id": "cr-%03d" % _N[0], "title": "%s %d" % (title, _N[0]),
            "summary": "A probe card, never shown.", "category": "Test", "urgency": "whenever",
            "state": "needs-you", "responder": "agent", "ask": a}


def _pop(*keys):
    def f(a):
        for k in keys:
            a.pop(k, None)
    return f


# One bad card per rule: the probe's variants. Each must fire exactly its own rule.
BAD = {
    "time-word": lambda a: a.update(recap="The backup failed yesterday and needs write access. Only you can grant it."),
    "machine-text": lambda a: a.update(context=a["context"] + "\nCommit 508f17a changed it; see /srv/app/ops/x.py."),
    "no-recommended": lambda a: a["options"][0].update(label="Grant write access"),
    "no-subject-recap": _pop("subject", "recap"),
    "bare-verb-label": lambda a: a["options"][0].update(label="Go ahead (Recommended)"),
    "empty-context": lambda a: a.update(context=""),
}
# Text the bad cards carry that must never reach the record.
CARD_TEXT = ["yesterday", "508f17a", "/srv/app", "Go ahead", "Vault write access", "backup",
             "Grant write access", "read-only"]


def fired(a):
    return cr.evaluate(a["kind"], a.get("prompt"), a.get("context"), a.get("options"),
                       a.get("subject"), a.get("recap"))


def api(method, path, payload=None, headers=None):
    r = CLIENT.request(method, path, json=payload, headers=headers or AUTH)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"raw": r.text}
    return r.status_code, body


def gate(t, headers=None):
    return api("POST", "/api/triage/needs-owner-check", {"thread": t}, headers)


def rows(where="", args=()):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM card_rule_warnings " + where, args)]
    finally:
        con.close()


def decision(res):
    return (res.get("needs_you"), res.get("failed_test"), res.get("detail"))


def with_modes(value, fn):
    old = os.environ.get("HELM_CARD_RULES")
    if value is None:
        os.environ.pop("HELM_CARD_RULES", None)
    else:
        os.environ["HELM_CARD_RULES"] = value
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("HELM_CARD_RULES", None)
        else:
            os.environ["HELM_CARD_RULES"] = old


def run():
    print("\n1. each rule fires on its bad card and stays quiet on the good one")
    check("the good card breaks no rule", fired(GOOD_ASK) == [], fired(GOOD_ASK))
    for rule, mutate in BAD.items():
        a = copy.deepcopy(GOOD_ASK)
        mutate(a)
        check("%s fires on its bad card, and only it" % rule, fired(a) == [rule], fired(a))
    check("every rule is pinned to a line of the standard",
          all("Talking To the owner line" in cr.PINS[r] for r in cr.RULE_IDS), cr.PINS)

    print("\n2. precision: what the standard allows does not fire")
    quiet = {
        "Approve / Deny / Amend on a yes-or-no (lines 92-93)":
            lambda a: a.update(options=[{"label": "Approve (Recommended)", "value": "y", "effect": "it ships"},
                                        {"label": "Deny", "value": "n", "effect": "it does not ship"},
                                        {"label": "Amend", "value": "a", "effect": "you say what changes"}]),
        "a cancel beside the real alternative (section 5)":
            lambda a: a["options"].append({"label": "None - close this", "value": "none", "effect": "nothing changes"}),
        "a true retry-or-abandon (line 154)":
            lambda a: a.update(options=[{"label": "Retry (Recommended)", "value": "r", "effect": "it runs again"},
                                        {"label": "Cancel it", "value": "c", "effect": "it stops"}]),
        "a verb that names its thing ('Keep it read-only')":
            lambda a: a["options"][1].update(label="Keep it read-only"),
        "a machine token in an option value (line 139)":
            lambda a: a["options"][0].update(value="tasks-20261014-x-508f17a"),
        "a URL is a link, not a path":
            lambda a: a.update(context=a["context"] + "\nThe page is https://helm.example.com/#/note/Hosts/x.md now."),
        "a time word in the context (rule 2 reads subject, recap, prompt, options)":
            lambda a: a.update(context=a["context"] + "\nIt failed again last night."),
        "a version number and a date are not commit ids":
            lambda a: a.update(context=a["context"] + "\nBuild 0.1.5.0 shipped 2026-10-14; 1500 files."),
        "Node.js is a product, not a file":
            lambda a: a.update(context=a["context"] + "\nThe job runs on Node.js."),
    }
    for null in ("Not now", "Not yet", "Not for now", "Not just yet", "Not right now", "Not this time", "Later", "None", "Nothing", "Do nothing", "Leave it",
                 "Skip this check and close the task", "Cancel this task"):
        quiet["the null option %r beside a named one (section 5)" % null] = (
            lambda a, n=null: a["options"].append({"label": n, "value": "null", "effect": "nothing changes"}))
    quiet["'Not now' as the abandon of a retry-or-abandon"] = (
        lambda a: a.update(options=[{"label": "Retry (Recommended)", "value": "r", "effect": "it runs again"},
                                    {"label": "Not now", "value": "n", "effect": "it waits"}]))
    for name, mutate in quiet.items():
        a = copy.deepcopy(GOOD_ASK)
        mutate(a)
        check("quiet: " + name, fired(a) == [], fired(a))
    ack = {"kind": "ack", "prompt": "The nightly backup finished on its own; nothing to do."}
    check("an ack needs no subject, recap or context (Freshness rule 4)", fired(ack) == [], fired(ack))
    noisy = {
        "Retry / Cancel / Change the plan (the original failure card)":
            lambda a: a.update(options=[{"label": "Retry it", "value": "r", "effect": "x"},
                                        {"label": "Cancel it", "value": "c", "effect": "y"},
                                        {"label": "Change the plan (Recommended)", "value": "p", "effect": "z"}]),
        "'Run it anyway'": lambda a: a["options"][0].update(label="Run it anyway (Recommended)"),
        "'Deploy the fix'": lambda a: a["options"][0].update(label="Deploy the fix (Recommended)"),
        "'Now'": lambda a: a["options"][0].update(label="Now (Recommended)"),
        "'Do it now'": lambda a: a["options"][0].update(label="Do it now (Recommended)"),
        "'Fix it now'": lambda a: a["options"][0].update(label="Fix it now (Recommended)"),
        "'Change the plan now'": lambda a: a["options"][0].update(label="Change the plan now (Recommended)"),
        "'Do it now, not later' ('not' negates the defer, it is no cancel)":
            lambda a: a["options"][0].update(label="Do it now, not later (Recommended)"),
        "'Not later' alone": lambda a: a["options"][0].update(label="Not later (Recommended)"),
        "'Not for later' alone": lambda a: a["options"][0].update(label="Not for later (Recommended)"),
        "'Do not skip' ('not' negates the verb, it is no cancel)":
            lambda a: a["options"][1].update(label="Do not skip"),
        "'Do not stop' beside a named option": lambda a: a["options"][1].update(label="Do not stop"),
        "'Fix it, not later' beside a named option": lambda a: a["options"][1].update(label="Fix it, not later"),
    }
    for name, mutate in noisy.items():
        a = copy.deepcopy(GOOD_ASK)
        mutate(a)
        check("fires: " + name, "bare-verb-label" in fired(a), fired(a))
    for word in ("today", "tonight", "tomorrow", "this morning", "last week", "3 days ago", "recently", "just now"):
        a = copy.deepcopy(GOOD_ASK)
        a["options"][1]["effect"] = "Backups stay incomplete %s." % word
        check("time word in an option effect: %r" % word, fired(a) == ["time-word"], fired(a))
    for text in ("t-queued-tasks-20261010-foo-01", "tasks-20261010-foo-01", "C:\\Scripts\\File-IssueCard.ps1",
                 "~/scripts/helm-thread-put.py", "web/app.js", "store.py", "a1b2c3d4e5f6"):
        a = copy.deepcopy(GOOD_ASK)
        a["prompt"] = "Should the backup job (%s) get write access to the vault?" % text
        check("machine text in the prompt: %r" % text, fired(a) == ["machine-text"], fired(a))

    big = ("a.b-" * 5000) + "/" + ("x." * 3000)
    t0 = time.time()
    cr.evaluate("choice", big, big, [{"label": big, "effect": big}], big, big)
    check("a 20 KB field is evaluated in well under a second (fields are clipped)",
          time.time() - t0 < 0.5, time.time() - t0)

    print("\n3. the check route: log mode answers warnings, and the decision never changes")
    for rule, mutate in BAD.items():
        t = card(mutate)
        code_off, res_off = with_modes("*=off", lambda: gate(copy.deepcopy(t)))
        code, res = gate(t)
        check("%s: the route answers 200" % rule, code == 200 and code_off == 200, (code, res))
        check("%s: warnings name the rule" % rule, res.get("warnings") == [rule], res)
        check("%s: the decision is the one the rules-off gate gives" % rule,
              decision(res) == decision(res_off) and res_off.get("warnings") == [], (res, res_off))
        check("%s: the card still passes the gate" % rule, res.get("needs_you") is True, res)
    t = card()
    code, res = gate(t)
    check("the good card: needs_you, no warnings", code == 200 and res.get("needs_you") is True
          and res.get("warnings") == [], res)
    t = card(lambda a: (a.update(context=""), a["options"][0].update(label="X" * 61)))
    code, res = gate(t)
    check("a card the validator refuses still carries its warnings",
          res.get("needs_you") is False and res.get("failed_test") == "validation"
          and res.get("warnings") == ["empty-context"], res)
    check("the check route lands no thread", store.get_thread(store.db(DB), t["thread_id"]) is None)

    print("\n4. the record: rule id, thread, principal, author, time -- never card text")
    t = card(BAD["time-word"])
    before = time.time()
    gate(t)
    got = rows("WHERE thread_id=? AND door='check'", (t["thread_id"],))
    check("one row for the rule that fired", [r["rule"] for r in got] == ["time-word"], got)
    r0 = got[0] if got else {}
    check("the row carries the mode, the author and a time", r0.get("mode") == "log"
          and r0.get("author") == "agent" and r0.get("ts", 0) >= before - 1, r0)
    gate(t)
    check("the same question checked again adds no row",
          len(rows("WHERE thread_id=? AND door='check'", (t["thread_id"],))) == 1)
    con = store.db(DB)
    tok = tokens.add(con, "pump-test")
    con.close()
    t = card(BAD["empty-context"])
    gate(t, headers={"Authorization": "Bearer " + tok})
    got = rows("WHERE thread_id=?", (t["thread_id"],))
    check("the token principal is recorded", got and got[0]["principal"] == "pump-test", got)
    t = card()
    gate(t)
    got = rows("WHERE thread_id=?", (t["thread_id"],))
    check("a clean ask records one 'none' row (the denominator)", [r["rule"] for r in got] == ["none"], got)
    dump = json.dumps(rows())
    leaked = [s for s in CARD_TEXT if s.lower() in dump.lower()]
    check("no card text anywhere in the record", not leaked, leaked)

    print("\n5. the PUT door and the /result door record what they store, once per question")
    con = store.db(DB)
    t = card(BAD["bare-verb-label"], title="A put door card")
    res = store.put_threads(con, {"threads": [t]})
    check("log mode: the bad card is stored", res.get("created") == 1 and not res.get("rejected"), res)
    check("one put row for its rule", [r["rule"] for r in rows("WHERE thread_id=? AND door='put'",
                                                                 (t["thread_id"],))] == ["bare-verb-label"])
    store.put_threads(con, {"threads": [copy.deepcopy(t)]})
    check("a resync of the same question adds nothing",
          len(rows("WHERE thread_id=? AND door='put'", (t["thread_id"],))) == 1)
    t2 = copy.deepcopy(t)
    t2["ask"]["prompt"] = "Should the backup job get write access to the vault this month?"
    store.put_threads(con, {"threads": [t2]})
    check("a changed question is recorded again",
          len(rows("WHERE thread_id=? AND door='put'", (t["thread_id"],))) == 2)
    t3 = card(BAD["empty-context"], title="A card the four-test refuses")
    t3["ask"]["requires"] = []
    res = store.put_threads(con, {"threads": [t3]})
    check("a card the gate refuses is not recorded at the put door",
          res.get("rejected") and not rows("WHERE thread_id=? AND door='put'", (t3["thread_id"],)), res)
    body = {"outcome": "checked", "state": "needs-you", "actor": "worker-x",
            "note": "I checked the backup job and the vault, and this is the report about both. " * 2,
            "ask": dict(copy.deepcopy(GOOD_ASK), context="")}
    store.post_result(con, t["thread_id"], body)
    got = rows("WHERE thread_id=? AND door='result'", (t["thread_id"],))
    check("the result door records its rule", [r["rule"] for r in got] == ["empty-context"], got)
    check("the result door's author comes from the body", got and got[0]["author"] == "worker-x", got)
    con.close()

    con = store.db(DB)
    bad_id = card(BAD["empty-context"], title="A card with an id that is a list")
    bad_id["thread_id"] = ["x"]
    good = card(BAD["empty-context"], title="A good card beside it")
    res = store.put_threads(con, {"threads": [good, bad_id]})
    check("a batch with an unhashable bad thread id still stores the good card (partial success)",
          res.get("created") == 1 and res.get("rejected_count") == 1, res)
    check("and records the good card", [r["rule"] for r in rows("WHERE thread_id=? AND door='put'",
                                                                   (good["thread_id"],))] == ["empty-context"])
    con.close()
    code, res = api("PUT", "/api/triage/threads", {"threads": [card(title="Through the api"),
                                                              dict(card(title="Bad id"), thread_id={"a": 1})]})
    check("the same batch through the api is not a 500", code == 200 and res.get("created") == 1, (code, res))
    con = store.db(DB)
    t = card(BAD["time-word"], title="An id with a newline")
    t["thread_id"] = "cr-nl\ncard-rule log door=check thread=forged rule=time-word"
    gate(t)
    got = rows("WHERE rule='time-word' AND thread_id LIKE 'cr-nl%'")
    check("a raw thread id is recorded as one printable token (no forged log line)",
          got and "\n" not in got[0]["thread_id"] and " " not in got[0]["thread_id"], got)
    t = card(BAD["time-word"], title="An id with a unicode line break")
    t["thread_id"] = "cr-ls\u2028forged\x85line"
    gate(t)
    got = rows("WHERE rule='time-word' AND thread_id LIKE 'cr-ls%'")
    check("a unicode line break in a raw id is replaced too (printable ASCII only)",
          got and len(got[0]["thread_id"].splitlines()) == 1 and got[0]["thread_id"].isascii(), got)
    con.close()

    print("\n6. off and reject modes")

    def off_case():
        t = card(BAD["bare-verb-label"])
        code, res = gate(t)
        return res, rows("WHERE thread_id=?", (t["thread_id"],))
    res, got = with_modes("bare-verb-label=off", off_case)
    check("off: no warning", res.get("warnings") == [] and res.get("needs_you") is True, res)
    check("off: no row but the denominator", [r["rule"] for r in got] == ["none"], got)

    def reject_case():
        t = card(lambda a: (BAD["time-word"](a), BAD["empty-context"](a)))
        code, res = gate(t)
        con = store.db(DB)
        try:
            put = store.put_threads(con, {"threads": [copy.deepcopy(t)]})
        finally:
            con.close()
        return t, res, put
    t, res, put = with_modes("time-word=reject", reject_case)
    check("reject: the check route refuses the card", res.get("needs_you") is False
          and res.get("failed_test") == "validation" and "card rule time-word" in str(res.get("detail")), res)
    check("reject: the refusal names the rule and the standard, not the card",
          "Talking To the owner" in str(res.get("detail")) and "yesterday" not in str(res.get("detail")), res)
    check("reject: the other rule still only warns", res.get("warnings") == ["empty-context"], res)
    check("reject: the PUT refuses it too", put.get("rejected") and put.get("created") == 0, put)
    got = rows("WHERE thread_id=? AND door='check' AND rule='time-word'", (t["thread_id"],))
    check("reject: the check row says reject", got and got[0]["mode"] == "reject", got)
    m = with_modes("time-word=REJECT, *=off ,bogus=log,empty-context=loud", cr.modes)
    check("config: case-insensitive, '*' sets the default, bad entries are skipped",
          m["time-word"] == "reject" and m["empty-context"] == "off" and m["no-recommended"] == "off", m)
    check("config: unset means log for every rule", with_modes(None, cr.modes) == {r: "log" for r in cr.RULE_IDS})

    print("\n7. GET /api/triage/card-rules: counts per rule, principal and author for a window")
    code, res = api("GET", "/api/triage/card-rules?door=all")
    check("200 with counts", code == 200 and res.get("ok") is True and res.get("asks", 0) > 0, (code, res))
    check("every rule has a count", set(res.get("by_rule", {})) == set(cr.RULE_IDS), res.get("by_rule"))
    check("per principal", "pump-test" in res.get("by_principal", {}) and "" in res.get("by_principal", {}),
          res.get("by_principal"))
    check("per author", res.get("by_author", {}).get("agent", {}).get("asks_warned", 0) > 0, res.get("by_author"))
    check("modes and pins ride along", res.get("modes", {}).get("time-word") == "log"
          and "Talking To the owner" in res.get("pins", {}).get("time-word", ""), res.get("modes"))
    check("asks_warned <= asks", res.get("asks_warned", 0) <= res.get("asks", 0), res)
    code, res = api("GET", "/api/triage/card-rules")
    check("the default window counts the filing doors only (put, result)",
          code == 200 and set(res.get("by_door", {})) <= {"put", "result"} and res.get("doors") == ["put", "result"],
          res.get("by_door"))
    check("the put door's bare verb is counted", res.get("by_rule", {}).get("bare-verb-label", 0) >= 1, res.get("by_rule"))
    code, res = api("GET", "/api/triage/card-rules?door=check&since=%d" % (time.time() + 60))
    check("a window in the future is empty", code == 200 and res.get("asks") == 0, res)
    code, res = api("GET", "/api/triage/card-rules?door=nope")
    check("an unknown door is a 400", code == 400, (code, res))
    code, res = api("GET", "/api/triage/card-rules?since=yesterday")
    check("a non-numeric window is a 400", code == 400, (code, res))
    for q in ("since=nan", "until=inf", "since=1e400"):
        code, res = api("GET", "/api/triage/card-rules?" + q)
        check("a non-finite window (%s) is a 400, not a 500" % q, code == 400, (code, res))
    r = CLIENT.get("/api/triage/card-rules")
    check("the stats need the bearer", r.status_code == 401, r.status_code)

    print("\n8. the stats count what reached the board: refusals in, held / muted / settled out")
    note = "I checked the backup job and the vault, and this is the report about both. " * 2
    con = store.db(DB)

    def put_rows(tid, door="put"):
        return rows("WHERE thread_id=? AND door=? AND rule!='none'", (tid, door))

    def stats_rule(rule):
        return store.card_rule_stats(con, 0, time.time() + 60)["by_rule"][rule]

    # a refused PUT leaves one reject row at its door, and only for the rule that refused it
    t = card(lambda a: (BAD["time-word"](a), BAD["empty-context"](a)), title="A refused put")
    before = stats_rule("time-word")
    res = with_modes("time-word=reject", lambda: store.put_threads(con, {"threads": [copy.deepcopy(t)]}))
    check("reject: the PUT refuses the card", res.get("rejected") and res.get("created") == 0, res)
    got = put_rows(t["thread_id"])
    check("reject: the refused PUT writes one row, mode reject, for the rule that refused it",
          [(r["rule"], r["mode"]) for r in got] == [("time-word", "reject")], got)
    with_modes("time-word=reject", lambda: store.put_threads(con, {"threads": [copy.deepcopy(t)]}))
    check("reject: the producer retrying the same card adds nothing", len(put_rows(t["thread_id"])) == 1)
    check("reject: the default stats window counts the refusal", stats_rule("time-word") == before + 1,
          (before, stats_rule("time-word")))
    check("reject: the refused card never reached the board", store.get_thread(con, t["thread_id"]) is None)
    check("reject: no card text in the refusal row",
          not [s for s in CARD_TEXT if s.lower() in json.dumps(got).lower()], got)

    # a refused /result leaves one reject row, and it survives the refusal
    t = card(title="A refused result")
    store.put_threads(con, {"threads": [t]})
    bad_ask = dict(copy.deepcopy(GOOD_ASK), recap="The backup failed yesterday and needs write access.")
    refused = None
    try:
        with_modes("time-word=reject", lambda: store.post_result(
            con, t["thread_id"], {"outcome": "checked", "state": "needs-you", "actor": "worker-x",
                                  "note": note, "ask": bad_ask}))
    except store.StoreError as e:
        refused = e
    check("reject: the result is refused with the rule's sentence",
          refused is not None and "card rule time-word" in str(refused), refused)
    got = put_rows(t["thread_id"], "result")
    check("reject: the refused result writes one row, mode reject, committed",
          [(r["rule"], r["mode"], r["author"]) for r in got] == [("time-word", "reject", "worker-x")], got)

    # a card that is muted on arrival never reached the owner
    first = card(BAD["bare-verb-label"], title="A card to mute")
    store.put_threads(con, {"threads": [first]})
    store.mute(con, first["thread_id"], "probe")
    again = card(BAD["bare-verb-label"], title="A card to mute")
    res = store.put_threads(con, {"threads": [again]})
    check("a card the mute rule retires on arrival is suppressed", res.get("suppressed_on_arrival") == 1, res)
    check("muted on arrival: no put-door row", not rows("WHERE thread_id=?", (again["thread_id"],)))

    # a resync of a done thread stores nothing of the new question
    done = card(title="A thread the owner closed")
    store.put_threads(con, {"threads": [done]})
    con.execute("UPDATE threads SET state='done', ask_kind='none' WHERE thread_id=?", (done["thread_id"],))
    con.commit()
    resync = copy.deepcopy(done)
    BAD["bare-verb-label"](resync["ask"])
    res = store.put_threads(con, {"threads": [resync]})
    check("a resync of a done thread is preserved done", res.get("preserved_done") == 1, res)
    check("resync of a done thread: no put-door row for the question it dropped",
          not rows("WHERE thread_id=? AND door='put' AND rule='bare-verb-label'", (done["thread_id"],)))

    # a card the needs-owner gate settles on arrival is not shown
    settled = {"thread_id": "cr-gate-settled", "title": "Cleanup on the media box", "summary": "s",
               "category": "Fleet", "urgency": "whenever", "state": "needs-you", "responder": "agent",
               "ask": {"kind": "ack", "requires": "decision",
                       "prompt": "Work completed: cleanup finished yesterday and verified. Tap to close."}}
    res = store.put_threads(con, {"threads": [settled]})
    srow = store.get_thread(con, "cr-gate-settled")
    gated = bool(res.get("created")) and srow is not None and srow["state"] != "needs-you"
    check("the gate settles or holds the status card at the put door (probe is valid)", gated, (res, srow))
    check("gate-held at put: no put-door row", not rows("WHERE thread_id='cr-gate-settled'"))

    # the same at the result door
    lit = card(title="Result door gate probe")
    store.put_threads(con, {"threads": [lit]})
    store.reply(con, lit["thread_id"], "grant")
    out = store.post_result(con, lit["thread_id"], {
        "outcome": "reported", "state": "needs-you", "note": note, "actor": "worker-y",
        "ask": {"kind": "ack", "prompt": "Work completed: cleanup finished yesterday and verified. Tap to close."}})
    check("the gate settles the result's status card (probe is valid)", (out.get("gate") or {}).get("decision") == "drop", out)
    check("gate-settled at result: no result-door row", not put_rows(lit["thread_id"], "result"))

    # and a result that does light the card is still counted
    lit2 = card(title="Result door lit probe")
    store.put_threads(con, {"threads": [lit2]})
    store.post_result(con, lit2["thread_id"], {"outcome": "checked", "state": "needs-you", "note": note,
                                               "actor": "worker-z", "ask": dict(copy.deepcopy(GOOD_ASK), context="")})
    check("a lit result is counted at its door", [r["rule"] for r in put_rows(lit2["thread_id"], "result")]
          == ["empty-context"])
    con.close()

    print("\n9. no-subject-recap in reject mode: the one rule that qualified on the measured week")
    mode = "no-subject-recap=reject"
    for name, drop in (("subject and recap", ("subject", "recap")), ("subject", ("subject",)),
                       ("recap", ("recap",))):
        t = card(_pop(*drop), title="A card without " + name)
        code, res = with_modes(mode, lambda: gate(copy.deepcopy(t)))
        check("check route: a card without %s is refused, naming the rule" % name,
              code == 200 and res.get("needs_you") is False and res.get("failed_test") == "validation"
              and "card rule no-subject-recap" in str(res.get("detail")), (code, res))
        con = store.db(DB)
        put = with_modes(mode, lambda: store.put_threads(con, {"threads": [copy.deepcopy(t)]}))
        check("PUT: a card without %s is rejected and never stored" % name,
              put.get("rejected") and put.get("created") == 0
              and store.get_thread(con, t["thread_id"]) is None, put)
        got = rows("WHERE thread_id=? AND door='put' AND rule='no-subject-recap'", (t["thread_id"],))
        check("PUT: one refusal row, mode reject", [r["mode"] for r in got] == ["reject"], got)
        con.close()
    t = card(title="A card with both")
    code, res = with_modes(mode, lambda: gate(copy.deepcopy(t)))
    check("a card with subject and recap passes", res.get("needs_you") is True and res.get("warnings") == [], res)
    code, res = with_modes(mode, lambda: gate(card(BAD["empty-context"])))
    check("the other rules stay log beside it", res.get("needs_you") is True
          and res.get("warnings") == ["empty-context"], res)
    ack = card(title="An ack without subject or recap")
    ack["ask"] = {"kind": "ack", "prompt": "The nightly backup finished on its own; nothing to do."}
    code, res = with_modes(mode, lambda: gate(ack))
    check("an ack needs no subject or recap even in reject mode", "no-subject-recap" not in str(res), res)
    con = store.db(DB)
    t = card(title="A result without subject or recap")
    store.put_threads(con, {"threads": [t]})
    bad_ask = _pop("subject", "recap")
    ask = copy.deepcopy(GOOD_ASK)
    bad_ask(ask)
    refused = None
    try:
        with_modes(mode, lambda: store.post_result(
            con, t["thread_id"], {"outcome": "checked", "state": "needs-you", "actor": "worker-x",
                                  "note": note, "ask": ask}))
    except store.StoreError as e:
        refused = e
    check("/result: refused with 400 and the rule's sentence",
          refused is not None and refused.status == 400 and "card rule no-subject-recap" in str(refused), refused)
    got = rows("WHERE thread_id=? AND door='result' AND rule='no-subject-recap'", (t["thread_id"],))
    check("/result: one refusal row, mode reject", [(r["mode"], r["author"]) for r in got]
          == [("reject", "worker-x")], got)
    con.close()

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
