"""tests/test_stall_route.py — Route stalled runs to an agent instead of the owner.

Run: python3 tests/test_stall_route.py   (from repo root)

Covers:
1. round-cap-review-unavailable ack result from dispatcher-skeptic routes to agent (state working, instruction event, pending dispatch)
2. other stall hand-backs (round-cap-duplicate, partial-capped, reported-by-dispatcher, degraded no-action)
3. non-stalls still light or settle (skeptic escalation choice lights needs-you, ready settles done)
4. loop guard: second stall within 7 days lights for the owner with "an agent already tried once: <first stall>"
5. kill switch: HELM_GATE_OFF=stall or HELM_GATE=off preserves needs-you behavior
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import store  # noqa: E402

PASS = FAIL = 0
LONG = "The run did the work and this is the long-enough report about it. " + "y" * 40


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail else ""))


def lit(con, tid, title, prompt="Pick one?", kind="choice"):
    ask = {"kind": kind, "prompt": prompt, "requires": ["decision"]}
    if kind == "choice":
        ask["options"] = [{"label": "Do the first thing now", "value": "first",
                           "effect": "I do the first thing and report back"},
                          {"label": "Leave it for later", "value": "later",
                           "effect": "Nothing happens until you ask again"}]
    store.put_threads(con, {"threads": [{
        "thread_id": tid, "title": title, "summary": "a summary", "category": "Fleet",
        "urgency": "this-week", "state": "needs-you", "ask": ask}]})
    store.reply(con, tid, "first" if kind == "choice" else "ack")


def row(con, tid):
    return dict(con.execute("SELECT * FROM threads WHERE thread_id=?", (tid,)).fetchone())


def stall_events(con, tid):
    return [dict(e) for e in con.execute(
        "SELECT * FROM events WHERE thread_id=? AND source='helm-stall' ORDER BY ts", (tid,))]


def run():
    os.environ.pop("HELM_GATE", None)
    os.environ.pop("HELM_GATE_OFF", None)
    con = store.db(os.path.join(tempfile.mkdtemp(prefix="helm-stall-"), "t.db"))

    print("\n1. round-cap-review-unavailable routes to an agent instead of the owner")
    lit(con, "s-roundcap", "Investigate high memory usage on host")
    store.post_result(con, "s-roundcap", {
        "outcome": "round-cap-review-unavailable",
        "state": "needs-you",
        "responder": "dispatcher-skeptic",
        "note": "This thread hit the executor round cap and the second-lane review could not run or produced no usable verdict (review harness timeout). Everything the rounds produced is preserved in the history above; acknowledging settles the thread, and any reply reopens it. " + "x" * 40,
        "ask": {
            "kind": "ack",
            "prompt": "Work stopped at the round cap — the second-lane review could not run. Acknowledging settles this thread; reply anything to reopen it."
        }
    })
    r = row(con, "s-roundcap")
    check("state is working instead of needs-you", r["state"] == "working", r["state"])
    check("ask_kind is cleared to none (not lit on board)", r["ask_kind"] == "none", r["ask_kind"])
    sev = stall_events(con, "s-roundcap")
    check("instruction event recorded with source helm-stall", bool(sev and sev[0]["kind"] == "instruction"), sev)
    if sev:
        check("instruction names outcome and note snippet", "round-cap-review-unavailable" in sev[0]["body"] and "Finish the work or name the blocker" in sev[0]["body"], sev[0]["body"])
    check("last_note is updated with instruction", "Finish the work or name the blocker" in r["last_note"], r["last_note"])
    p = store.pending_dispatch(con)
    pending_ids = [item["thread_id"] for item in p["pending"]]
    check("thread is queued in pending_dispatch", "s-roundcap" in pending_ids, pending_ids)
    if "s-roundcap" in pending_ids:
        item = next(it for it in p["pending"] if it["thread_id"] == "s-roundcap")
        check("pending dispatch item has reply_value __task__", item["reply_value"] == store.HAND_TASK_VALUE, item["reply_value"])
        check("pending dispatch item has instruction in reply_note", "Finish the work or name the blocker" in item["reply_note"], item["reply_note"])

    print("\n2. other dispatcher stall sources route to an agent too")
    # round-cap-duplicate
    lit(con, "s-dupe", "Dupe round cap test")
    store.post_result(con, "s-dupe", {
        "outcome": "round-cap-duplicate",
        "state": "needs-you",
        "responder": "dispatcher-skeptic",
        "note": "This thread hit the round cap, and the second-lane review wanted to escalate — but an ask covering this same concern is already open on thread t-other. " + "y" * 40,
        "ask": {"kind": "ack", "prompt": "Round cap reached; the same concern is already being asked about on another thread."}
    })
    check("round-cap-duplicate routes to working", row(con, "s-dupe")["state"] == "working")

    # partial-capped
    lit(con, "s-partcap", "Partial capped run test")
    store.post_result(con, "s-partcap", {
        "outcome": "partial-capped",
        "state": "needs-you",
        "responder": "agent",
        "note": "The run hit a cap before filing its own report; this is where it got to. " + "z" * 40,
        "ask": {"kind": "ack", "prompt": "The run hit a cap before filing its own report; this is where it got to. Acknowledging settles this thread."}
    })
    check("partial-capped routes to working", row(con, "s-partcap")["state"] == "working")

    # reported-by-dispatcher
    lit(con, "s-repdisp", "Reported by dispatcher test")
    store.post_result(con, "s-repdisp", {
        "outcome": "reported-by-dispatcher",
        "state": "needs-you",
        "responder": "agent",
        "note": "The run finished without filing its own report; this is its final message. " + "w" * 40,
        "ask": {"kind": "ack", "prompt": "The run finished without filing its own report; this is its final message. Acknowledging settles this thread."}
    })
    check("reported-by-dispatcher routes to working", row(con, "s-repdisp")["state"] == "working")

    # degraded no-action hand-back
    lit(con, "s-degraded", "Degraded no action test")
    store.post_result(con, "s-degraded", {
        "outcome": "note-only",
        "state": "needs-you",
        "responder": "agent",
        "note": "The executor judged no action was owed, but the settle verdict was malformed. " + "v" * 40,
        "ask": {"kind": "ack", "prompt": "The run judged no action was owed, but its settle verdict was malformed so it could not close this itself. Acknowledging settles the thread."}
    })
    check("degraded no-action routes to working", row(con, "s-degraded")["state"] == "working")

    print("\n3. non-stalls are NOT rerouted")
    # skeptic escalation choice
    lit(con, "s-escalate", "Real escalation choice")
    store.post_result(con, "s-escalate", {
        "outcome": "needs-owner",
        "state": "needs-you",
        "responder": "dispatcher-skeptic",
        "note": "The executor rounds hit the round cap and the second-lane review could not finish this without you. " + "e" * 40,
        "ask": {
            "kind": "choice",
            "prompt": "Pick direction for stalled work?",
            "options": [{"label": "Grant bonus round", "value": "bonus", "effect": "Run another round"},
                        {"label": "Close thread", "value": "close", "effect": "Close it"}]
        }
    })
    check("skeptic escalation choice stays needs-you", row(con, "s-escalate")["state"] == "needs-you")

    # ready result settles done
    lit(con, "s-ready", "Ready verified done")
    store.post_result(con, "s-ready", {
        "outcome": "ready",
        "state": "done",
        "no_action": True,
        "responder": "dispatcher-skeptic",
        "note": "The second-lane review verified that the artifact plainly satisfies the decision made. " + "r" * 40
    })
    check("ready result settles done", row(con, "s-ready")["state"] == "done")

    print("\n4. loop guard: second stall within 7 days lights for the owner")
    lit(con, "s-loop", "Loop guard test")
    # First stall: routes to agent
    store.post_result(con, "s-loop", {
        "outcome": "round-cap-review-unavailable",
        "state": "needs-you",
        "responder": "dispatcher-skeptic",
        "note": "First stall: hit round cap review unavailable. " + "1" * 40,
        "ask": {"kind": "ack", "prompt": "Work stopped at the round cap."}
    })
    check("first stall routed to working", row(con, "s-loop")["state"] == "working")

    # Second stall on same thread: lights for the owner with warning
    store.post_result(con, "s-loop", {
        "outcome": "round-cap-review-unavailable",
        "state": "needs-you",
        "responder": "dispatcher-skeptic",
        "note": "Second stall: hit round cap again after agent attempted work. " + "2" * 40,
        "ask": {"kind": "ack", "prompt": "Work stopped at the round cap."}
    })
    r_loop = row(con, "s-loop")
    check("second stall lights needs-you for the owner", r_loop["state"] == "needs-you", r_loop["state"])
    check("note contains 'an agent already tried once:'", "an agent already tried once:" in r_loop["last_note"], r_loop["last_note"])
    check("note references first stall info", "round-cap-review-unavailable" in r_loop["last_note"], r_loop["last_note"])

    print("\n5. kill switch preserves old needs-you behavior")
    lit(con, "s-kill", "Kill switch test")
    os.environ["HELM_GATE_OFF"] = "stall"
    store.post_result(con, "s-kill", {
        "outcome": "round-cap-review-unavailable",
        "state": "needs-you",
        "responder": "dispatcher-skeptic",
        "note": "Killed stall route: should light needs-you immediately. " + "k" * 40,
        "ask": {"kind": "ack", "prompt": "Work stopped at the round cap."}
    })
    check("HELM_GATE_OFF=stall lights needs-you", row(con, "s-kill")["state"] == "needs-you")
    os.environ.pop("HELM_GATE_OFF", None)

    lit(con, "s-kill2", "Full kill switch test")
    os.environ["HELM_GATE"] = "off"
    store.post_result(con, "s-kill2", {
        "outcome": "round-cap-review-unavailable",
        "state": "needs-you",
        "responder": "dispatcher-skeptic",
        "note": "Killed gate: should light needs-you immediately. " + "k" * 40,
        "ask": {"kind": "ack", "prompt": "Work stopped at the round cap."}
    })
    check("HELM_GATE=off lights needs-you", row(con, "s-kill2")["state"] == "needs-you")
    os.environ.pop("HELM_GATE", None)

    return FAIL == 0


if __name__ == "__main__":
    ok = run()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(0 if ok and FAIL == 0 else 1)
