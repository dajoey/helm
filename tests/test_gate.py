"""The needs-owner gate: a card reaches the owner only if the tested rules say it needs him now.

Run: python3 tests/test_gate.py   (from the repo root)

the owner, 2026-09-24, in session: "Plan it live in Helm", then "Approve, build it now", then
"Full plan + build the missing re-check job". Rules live in helm/gate_rules.py
(written for calibration; held-out: 0 of 51
needed cards held back, 10 of 39 could-wait cards kept away). Wiki: Projects/Decision Bench 2026-09.

The failure this is written from: 1,243 of 1,250 asks came through post_result, where no
four-test runs, and a "Work completed ... Tap to close" ack lit needs-you every time
("why are you telling me?", "stop sending this card", "WHY DID YOU SEND ME THIS POINTLESS
CARD?").
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

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


def result(con, tid, prompt, kind="ack", note=LONG, options=None):
    ask = {"kind": kind, "prompt": prompt}
    if options:
        ask["options"] = options
    return store.post_result(con, tid, {"outcome": "reported", "state": "needs-you",
                                        "note": note, "ask": ask})


def row(con, tid):
    return dict(con.execute("SELECT * FROM threads WHERE thread_id=?", (tid,)).fetchone())


def gate_events(con, tid):
    return [dict(e) for e in con.execute(
        "SELECT * FROM events WHERE thread_id=? AND source='helm-gate' ORDER BY ts", (tid,))]


def run():
    os.environ.pop("HELM_GATE", None)
    con = store.db(os.path.join(tempfile.mkdtemp(prefix="helm-gate-"), "t.db"))

    print("\na status card outside a test cycle is settled by the gate, not shown")
    lit(con, "g-status", "disk cleanup on the media box")
    out = result(con, "g-status", "Work completed: cleanup finished and verified. Tap to close.")
    r = row(con, "g-status")
    check("it is settled (done), not lit", r["state"] == "done", r["state"])
    check("the response says the gate decided", (out.get("gate") or {}).get("decision") == "drop", out)
    ev = gate_events(con, "g-status")
    check("a visible gate event says why", ev and "settled by gate" in ev[-1]["body"], ev)
    check("the report itself is still in the history",
          con.execute("SELECT COUNT(*) FROM events WHERE thread_id='g-status' AND kind='bot-result'")
          .fetchone()[0] == 1)
    check("the question it held is kept for a reopen", json.loads(r["gate_hold_ask"] or "{}").get("kind") == "ack",
          r["gate_hold_ask"])

    print("\na status card inside a test cycle is held until the build is on testing")
    lit(con, "g-hold", "LazyMarketCompanion routing fix")
    store.post_result(con, "g-hold", {"outcome": "shipped", "state": "waiting-external", "note":
                                      "Shipped LMC 0.1.46.0 to the testing channel for in-game verification. "
                                      + "z" * 40, "blocked_on": "the testing channel publish job"})
    out = result(con, "g-hold", "Work completed: routing fix done in code and pushed (0.1.46.0). Tap to close.")
    r = row(con, "g-hold")
    check("it is held (waiting-external), not lit", r["state"] == "waiting-external", r["state"])
    check("the hold names the build it waits for", "0.1.46.0" in r["gate_cmd"] and
          r["gate_cmd"].startswith("helm-gate:build"), r["gate_cmd"])
    check("and says it is held by the gate", "held by gate" in r["blocked_on"], r["blocked_on"])
    check("it is rechecked within the hour", 0 < r["recheck_at"] - time.time() <= store.GATE_RECHECK_S + 5,
          r["recheck_at"] - time.time())
    check("no ask is left lit underneath", r["ask_kind"] == "none", r["ask_kind"])
    check("the response says hold", (out.get("gate") or {}).get("decision") == "hold", out)

    print("\nreal questions and build-ready test cards still reach him")
    lit(con, "g-real", "Object-store subscription renewal")
    result(con, "g-real", "Renew the object-store plan, drop it entirely, or add usage credits?",
           kind="choice", options=[{"label": "Keep the subscription", "value": "keep",
                                    "effect": "Nothing changes; the sub renews"},
                                   {"label": "Drop it entirely", "value": "drop",
                                    "effect": "I cancel the subscription at renewal"}])
    check("a funds decision is lit", row(con, "g-real")["state"] == "needs-you")
    check("with no gate event", not gate_events(con, "g-real"))
    lit(con, "g-ready", "LMC vendoring")
    result(con, "g-ready", "LMC 0.1.12.0 is live on testing - verify the vendoring in game and tap to close.")
    check("a build that is live on testing is sent", row(con, "g-ready")["state"] == "needs-you")

    print("\nre-asks are dropped")
    lit(con, "g-reask", "BossMod AI range")
    result(con, "g-reask", "Still the same question: BossMod's AI stops 20 yalms from your target - want to set it?",
           kind="choice", options=[{"label": "Set it to 3 myself", "value": "self", "effect": "You change it"},
                                   {"label": "Have me dig further", "value": "dig", "effect": "I keep looking"}])
    check("the re-ask is settled", row(con, "g-reask")["state"] == "done", row(con, "g-reask")["state"])

    print("\na hold with no version to check fails open: it is shown")
    lit(con, "g-nover", "Gluttony movement")
    store.post_result(con, "g-nover", {"outcome": "note", "state": "waiting-external", "note":
                                       "Movement work continues in game testing next round. " + "q" * 40,
                                       "blocked_on": "the next movement build from the agent"})
    result(con, "g-nover", "Work completed: completed. Tap to close.",
           note="Testing notes recorded for the in-game movement check. " + "w" * 40)
    check("no version -> lit, never held blind", row(con, "g-nover")["state"] == "needs-you",
          row(con, "g-nover")["state"])

    print("\nexemptions: only hand cards are never gated (the retired orchestrator's mirror prefix is gated like any card)")
    check("the exemption list is the hand-card prefix alone", store.GATE_EXEMPT_PREFIXES == (store.HAND_ID_PREFIX,), store.GATE_EXEMPT_PREFIXES)
    lit(con, "t-queued-tasks-x", "A parked task waiting on the owner")
    result(con, "t-queued-tasks-x", "Work completed: completed. Tap to close.")
    check("a former mirror id is gated like any card", row(con, "t-queued-tasks-x")["state"] != "needs-you",
          row(con, "t-queued-tasks-x")["state"])

    print("\nthe kill switch turns it all off")
    os.environ["HELM_GATE"] = "off"
    lit(con, "g-off", "another cleanup")
    result(con, "g-off", "Work completed: cleanup finished and verified. Tap to close.")
    check("HELM_GATE=off -> lit as before", row(con, "g-off")["state"] == "needs-you")
    os.environ.pop("HELM_GATE")

    print("\nthe re-check releases a hold when the build appears")
    out = store.apply_recheck(con, "g-hold", True, "LazyMarketCompanion testing 0.1.46.0")
    r = row(con, "g-hold")
    check("released -> lit with the question it held", r["state"] == "needs-you" and r["ask_kind"] == "ack"
          and "0.1.46.0" in r["ask_prompt"], (r["state"], r["ask_kind"], r["ask_prompt"]))
    check("and says so", out.get("outcome") == "released", out)
    check("the park fields are cleared", r["gate_cmd"] == "" and r["recheck_at"] == 0, (r["gate_cmd"], r["recheck_at"]))

    lit(con, "g-hold2", "LMC second fix")
    store.post_result(con, "g-hold2", {"outcome": "shipped", "state": "waiting-external", "note":
                                       "Shipped LMC 0.1.47.0 to testing for in-game verification. " + "z" * 40,
                                       "blocked_on": "the testing channel publish job"})
    result(con, "g-hold2", "Work completed: fix pushed (0.1.47.0). Tap to close.")
    out = store.apply_recheck(con, "g-hold2", False, "testing shows 0.1.46.0")
    r = row(con, "g-hold2")
    check("still not on testing -> stays held", r["state"] == "waiting-external", r["state"])
    check("looked at again within the hour", 0 < r["recheck_at"] - time.time() <= store.GATE_RECHECK_S + 5)
    con.execute("UPDATE threads SET parked_since=? WHERE thread_id='g-hold2'", (time.time() - 8 * 86400,))
    out = store.apply_recheck(con, "g-hold2", False, "testing shows 0.1.46.0")
    r = row(con, "g-hold2")
    check("held 7+ days -> shown anyway with the original question", r["state"] == "needs-you"
          and "0.1.47.0" in r["ask_prompt"], (r["state"], r["ask_prompt"]))
    lit(con, "g-hold3", "LMC third fix")
    store.post_result(con, "g-hold3", {"outcome": "shipped", "state": "waiting-external", "note":
                                       "Shipped LMC 0.1.48.0 to testing for in-game verification. " + "z" * 40,
                                       "blocked_on": "the testing channel publish job"})
    result(con, "g-hold3", "Work completed: fix pushed (0.1.48.0). Tap to close.")
    store.apply_recheck(con, "g-hold3", None, "could not read the testing channel")
    check("a check that could not run fails open (shown)", row(con, "g-hold3")["state"] == "needs-you")

    print("\nreopen brings back the original question and logs a gate miss")
    out = store.reopen_thread(con, "g-status", note="I did want this")
    r = row(con, "g-status")
    check("reopened with the original ack, not the generic text ask",
          r["state"] == "needs-you" and r["ask_kind"] == "ack" and "cleanup finished" in r["ask_prompt"],
          (r["ask_kind"], r["ask_prompt"]))
    check("a gate-miss event is recorded", any("gate miss" in e["body"] for e in gate_events(con, "g-status")),
          gate_events(con, "g-status"))
    lit(con, "g-hold4", "LMC fourth fix")
    store.post_result(con, "g-hold4", {"outcome": "shipped", "state": "waiting-external", "note":
                                       "Shipped LMC 0.1.49.0 to testing for in-game verification. " + "z" * 40,
                                       "blocked_on": "the testing channel publish job"})
    result(con, "g-hold4", "Work completed: fix pushed (0.1.49.0). Tap to close.")
    store.reopen_thread(con, "g-hold4")
    check("a gate-held thread can be pulled back too", row(con, "g-hold4")["state"] == "needs-you")

    print("\nthe gate log lists what it kept away, for the held-by-gate view")
    log = store.gate_log(con, days=14)
    ids = {x["thread_id"] for x in log}
    check("settled, held and missed threads are all listed", {"g-status", "g-reask", "g-hold2"} <= ids, ids)
    check("each entry carries the decision and reason", all(x.get("decision") and x.get("reason") for x in log), log[:2])

    store.put_threads(con, {"threads": [], "tombstones": ["g-reask"]})
    check("a removed (tombstoned) thread is not counted in the gate log",
          "g-reask" not in {x["thread_id"] for x in store.gate_log(con, days=14)})

    print("\ndue re-checks are handed out as data for the sweeper")
    lit(con, "g-hold5", "LMC fifth fix")
    store.post_result(con, "g-hold5", {"outcome": "shipped", "state": "waiting-external", "note":
                                       "Shipped LMC 0.1.50.0 to testing for in-game verification. " + "z" * 40,
                                       "blocked_on": "the testing channel publish job"})
    result(con, "g-hold5", "Work completed: fix pushed (0.1.50.0). Tap to close.")
    con.execute("UPDATE threads SET recheck_at=? WHERE thread_id='g-hold5'", (time.time() - 5,))
    due = store.due_rechecks(con)
    d = next((x for x in due if x["thread_id"] == "g-hold5"), None)
    check("the due list carries the gate command and the card text to resolve the plugin",
          d and d["gate_cmd"].startswith("helm-gate:build") and "hint" in d, d)

    print("\nthe producer's door: a NEW status card PUT straight onto the board is gated too")
    store.put_threads(con, {"threads": [{
        "thread_id": "g-put", "title": "backup check finished", "summary": "nightly backup verified",
        "category": "Fleet", "urgency": "whenever", "state": "needs-you",
        "ask": {"kind": "ack", "prompt": "Work completed: backups verified. Tap to close."}}]})
    r = row(con, "g-put")
    check("settled on arrival, with the gate's reason", r["state"] == "done" and gate_events(con, "g-put"),
          (r["state"], gate_events(con, "g-put")))
    check("and reopenable with its question", json.loads(r["gate_hold_ask"] or "{}").get("kind") == "ack")
    store.put_threads(con, {"threads": [{
        "thread_id": "g-put-real", "title": "renew the domain", "summary": "example.com renews in 5 days",
        "category": "Fleet", "urgency": "this-week", "state": "needs-you",
        "ask": {"kind": "choice", "prompt": "Renew example.com for 1 year or 3 years?", "requires": ["decision"],
                "options": [{"label": "Renew for one year", "value": "1y", "effect": "Charges about 12 dollars"},
                            {"label": "Renew for three years", "value": "3y", "effect": "Charges about 36 dollars"}]}}]})
    check("a real question PUT onto the board is lit as before", row(con, "g-put-real")["state"] == "needs-you")

    return FAIL == 0


def run_api():
    """The three routes the sweeper and the held-by-gate view use (skipped where starlette is absent)."""
    try:
        from starlette.testclient import TestClient
    except ImportError:
        print("\n(api checks skipped: no starlette here; they run in the helm image)")
        return True
    tmp = tempfile.mkdtemp(prefix="helm-gate-api-")
    os.environ.update({"HELM_APP_TOKEN": "test-token", "HELM_DB": os.path.join(tmp, "t.db"),
                       "HELM_ATTACH_DIR": os.path.join(tmp, "att")})
    import app.service as svc
    svc.DB_PATH = os.environ["HELM_DB"]
    client = TestClient(svc.app)
    auth = {"Authorization": "Bearer test-token"}
    con = store.db(os.environ["HELM_DB"])
    print("\napi: rechecks, recheck verdicts and the gate log")
    lit(con, "a-hold", "LMC api fix")
    store.post_result(con, "a-hold", {"outcome": "shipped", "state": "waiting-external", "note":
                                      "Shipped LMC 0.1.51.0 to testing for in-game verification. " + "z" * 40,
                                      "blocked_on": "the testing channel publish job"})
    result(con, "a-hold", "Work completed: fix pushed (0.1.51.0). Tap to close.")
    con.execute("UPDATE threads SET recheck_at=? WHERE thread_id='a-hold'", (time.time() - 5,))
    con.commit()
    r = client.get("/api/triage/rechecks", headers=auth)
    check("GET /rechecks lists the due gate hold", r.status_code == 200 and
          any(d["thread_id"] == "a-hold" for d in r.json()["due"]), r.text[:200])
    check("and it needs the token", client.get("/api/triage/rechecks").status_code == 401)
    r = client.post("/api/triage/threads/a-hold/recheck", headers=auth, json={"passed": "yes"})
    check("a non-boolean verdict is refused", r.status_code == 400, r.text)
    r = client.post("/api/triage/threads/a-hold/recheck", headers=auth,
                    json={"passed": True, "detail": "LazyMarketCompanion testing 0.1.51.0"})
    check("POST /recheck releases it", r.status_code == 202 and r.json().get("outcome") == "released", r.text)
    r = client.get("/api/triage/gate?days=14", headers=auth)
    check("GET /gate lists it with its decision", r.status_code == 200 and
          any(e["thread_id"] == "a-hold" and e["decision"] == "released" for e in r.json()["entries"]), r.text[:300])
    return True


if __name__ == "__main__":
    ok = run()
    run_api()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(0 if ok and FAIL == 0 else 1)
