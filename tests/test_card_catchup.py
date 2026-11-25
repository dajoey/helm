"""A card says when its question was actually put to the owner, and a resync that changes nothing says nothing.

The phone and the web both key "is this new?" on the thread's `last_change`. last_change moves on
dispatch bookkeeping, labels, and every producer resync, so a card that had been waiting for days
announced itself again as if it were fresh, and nothing on the card said how old the question was.

What the store now says:
  * `catchup.lit_at`        when the CURRENT question (kind, prompt, options) was first put. It
                            moves only when the question itself changes, never on a resync, a
                            context edit or a dispatch touch.
  * `catchup.checked_at`    the last time an agent reported on the thread (>= lit_at), or lit_at.
  * `catchup.reports_since` how many agent reports landed since the question was put.
Absolute times only: the reader works out "3 days ago", so the card can never lie about age.

And: a needs-you resync that changes nothing leaves last_change alone.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HELM_SITE"] = "home"

from helm import store  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def ask(prompt="Promote it, or keep it on testing?", context="The testing build has run for a week.", **extra):
    a = {"kind": "choice", "prompt": prompt, "requires": ["decision"], "context": context,
         "options": [{"label": "Promote it", "value": "promote", "effect": "production gets the build"},
                     {"label": "Keep it on testing", "value": "keep", "effect": "nothing changes"}]}
    a.update(extra)
    return a


def thread(tid="cc-1", **over):
    t = {"thread_id": tid, "title": "Promote the build", "summary": "a build is ready", "category": "Fleet",
         "urgency": "this-week", "state": "needs-you", "ask": ask()}
    t.update(over)
    return t


def get(con, tid="cc-1"):
    return store.get_thread(con, tid)


def run():
    con = store.db(os.path.join(tempfile.mkdtemp(prefix="helm-catchup-"), "t.db"))

    print("\na fresh needs-you card knows when its question was put")
    t0 = time.time()
    store.put_threads(con, {"threads": [thread()]})
    t1 = time.time()
    c = get(con)["catchup"]
    check("lit_at is when it was put", t0 - 0.01 <= c["lit_at"] <= t1 + 0.01, c)
    check("checked_at falls back to lit_at when no agent has reported", c["checked_at"] == c["lit_at"], c)
    check("no reports yet", c["reports_since"] == 0, c)

    print("\na resync that changes nothing changes nothing")
    lit = c["lit_at"]
    last = get(con)["last_change"]
    time.sleep(0.05)
    store.put_threads(con, {"threads": [thread()]})
    after = get(con)
    check("lit_at does not move", after["catchup"]["lit_at"] == lit, after["catchup"])
    check("last_change does not move either (it is what the phone keys on)", after["last_change"] == last,
          (after["last_change"], last))

    print("\na context edit is not a new question")
    time.sleep(0.05)
    store.put_threads(con, {"threads": [thread(ask=ask(context="A different amount of detail."))]})
    after = get(con)
    check("lit_at does not move", after["catchup"]["lit_at"] == lit, after["catchup"])
    check("the new context is stored", after["ask"]["context"] == "A different amount of detail.")
    check("a real change still moves last_change", after["last_change"] > last)

    print("\na different question is a new question")
    time.sleep(0.05)
    store.put_threads(con, {"threads": [thread(ask=ask(prompt="Promote it now, or wait for the next build?"))]})
    after = get(con)
    check("lit_at moves forward", after["catchup"]["lit_at"] > lit, after["catchup"])
    lit2 = after["catchup"]["lit_at"]

    print("\nagent reports since the question was put are counted and dated")
    time.sleep(0.05)
    store.add_event(con, "cc-1", time.time(), "agent", "", "bot-result", "checked: still waiting on the play test")
    time.sleep(0.02)
    store.add_event(con, "cc-1", time.time(), "agent", "", "bot-result", "checked again: still waiting")
    c = get(con)["catchup"]
    check("two reports since", c["reports_since"] == 2, c)
    check("checked_at is the newest report", c["checked_at"] > lit2, c)
    check("lit_at is untouched by reports", c["lit_at"] == lit2, c)

    print("\nthe same question put back after being emptied is not a new question")
    asked_before = get(con)["catchup"]
    time.sleep(0.05)
    store.snooze(con, "cc-1", hours=24)
    snoozed = get(con)
    check("a snoozed card holds no live question", snoozed["catchup"]["lit_at"] == 0, snoozed["catchup"])
    time.sleep(0.05)
    store.wake_now(con, "cc-1")
    woke = get(con)["catchup"]
    check("it wakes with the age it had", woke["lit_at"] == asked_before["lit_at"], (woke, asked_before))
    check("and is not counted as asked again", woke["times_asked"] == asked_before["times_asked"], woke)
    # a gate hold, a recheck and the pump's re-light all empty the ask and restore it unchanged
    row = con.execute("SELECT ask_kind, ask_prompt, ask_options FROM threads WHERE thread_id='cc-1'").fetchone()
    con.execute("UPDATE threads SET ask_kind='none', ask_prompt='', ask_options='[]' WHERE thread_id='cc-1'")
    time.sleep(0.05)
    con.execute("UPDATE threads SET ask_kind=?, ask_prompt=?, ask_options=? WHERE thread_id='cc-1'", tuple(row))
    check("emptied and restored: same age", get(con)["catchup"]["lit_at"] == asked_before["lit_at"])
    time.sleep(0.05)
    store.put_threads(con, {"threads": [thread(ask=ask(prompt="Is the build good enough to promote?"))]})
    check("a different question after all that is new", get(con)["catchup"]["lit_at"] > asked_before["lit_at"])

    print("\na resync that carries events is a change")
    last = get(con)["last_change"]
    time.sleep(0.05)
    t = thread(ask=ask(prompt="Is the build good enough to promote?"))
    t["events"] = [{"source": "agent", "kind": "update", "body": "a new fact landed"}]
    store.put_threads(con, {"threads": [t]})
    check("last_change moves", get(con)["last_change"] > last)

    print("\nan upgraded store adopts cards it already holds")
    con.execute("UPDATE threads SET ask_lit_at=0, ask_lit_key='' WHERE thread_id='cc-1'")
    store.init(con)
    adopted = get(con)["catchup"]["lit_at"]
    newest_ask = con.execute("SELECT MAX(ts) FROM events WHERE thread_id='cc-1' AND kind='bot-ask'").fetchone()[0]
    check("lit_at is the newest ask event", abs(adopted - newest_ask) < 1e-6, (adopted, newest_ask))

    print("\nadoption counts from the FIRST time the same question was put, not the latest re-ask")
    store.put_threads(con, {"threads": [thread(tid="cc-3", title="Retire the old relay host")]})
    first_ts = con.execute("SELECT MIN(ts) FROM events WHERE thread_id='cc-3' AND kind='bot-ask'").fetchone()[0]
    time.sleep(0.05)
    store.put_threads(con, {"threads": [thread(tid="cc-3", title="Retire the old relay host", ask=ask(context="Re-sent with more detail, same question."))]})
    n_asks = con.execute("SELECT COUNT(*) FROM events WHERE thread_id='cc-3' AND kind='bot-ask'").fetchone()[0]
    con.execute("UPDATE threads SET ask_lit_at=0, ask_lit_key='' WHERE thread_id='cc-3'")
    store.init(con)
    c3 = get(con, "cc-3")["catchup"]
    check("two ask events for one question", n_asks == 2, n_asks)
    check("lit_at is the first of them", abs(c3["lit_at"] - first_ts) < 1e-6, (c3, first_ts))
    check("times_asked says two", c3["times_asked"] == 2, c3)
    check("opened_at is the thread's first_seen", c3["opened_at"] == get(con, "cc-3")["first_seen"], c3)

    print("\nadoption also reads a snoozed card's held question, so it wakes with its real age")
    store.put_threads(con, {"threads": [thread(tid="cc-4", title="Rotate the backup key")]})
    real = con.execute("SELECT MIN(ts) FROM events WHERE thread_id='cc-4' AND kind='bot-ask'").fetchone()[0]
    store.snooze(con, "cc-4", hours=24)
    con.execute("UPDATE threads SET ask_lit_at=0, ask_lit_key='' WHERE thread_id='cc-4'")
    store.init(con)
    time.sleep(0.05)
    store.wake_now(con, "cc-4")
    check("a card snoozed before the upgrade wakes with its original age",
          abs(get(con, "cc-4")["catchup"]["lit_at"] - real) < 1e-6, (get(con, "cc-4")["catchup"], real))

    print("\nthe owner's last touch is his own, not an agent filing as him")
    check("none yet on a card he never touched", get(con, "cc-3")["catchup"]["owner_last_at"] == 0)
    snoozed_touch = get(con, "cc-4")["catchup"]["owner_last_at"]
    check("snoozing was his touch", snoozed_touch > 0, snoozed_touch)
    time.sleep(0.05)
    store.reply(con, "cc-4", "promote", note="Go for it, I tested it last night.")
    check("his reply is a later touch", get(con, "cc-4")["catchup"]["owner_last_at"] > snoozed_touch)

    print("\nthreads with no live question carry no age")
    store.put_threads(con, {"threads": [thread(tid="cc-2", title="Closed earlier", state="done", ask={"kind": "none"})]})
    c = get(con, "cc-2")["catchup"]
    check("lit_at is 0", c["lit_at"] == 0, c)

    print("\nreview follow-ups")
    # 1 a leftover non-object wake_ask on a settled row must not make every open of the DB raise
    store.put_threads(con, {"threads": [thread(tid="cc-5", title="Settled with odd leftovers", state="done", ask={"kind": "none"})]})
    con.execute("UPDATE threads SET wake_ask='\"just a string\"', ask_lit_at=0 WHERE thread_id='cc-5'")
    try:
        store.init(con)
        check("a non-object wake_ask does not break init", True)
    except Exception as e:  # noqa: BLE001
        check("a non-object wake_ask does not break init", False, repr(e))

    # 2 a canned question re-asked after the card settled is a new question
    store.put_threads(con, {"threads": [thread(tid="cc-6", title="Reopen me twice")]})
    store.close_thread(con, "cc-6", reason="done with it", actor="owner")
    store.reopen_thread(con, "cc-6", note="again")
    first = get(con, "cc-6")["catchup"]["lit_at"]
    check("a reopen lights a question", first > 0, first)
    time.sleep(0.05)
    store.close_thread(con, "cc-6", reason="done again", actor="owner")
    time.sleep(0.05)
    store.reopen_thread(con, "cc-6", note="again, again")
    second = get(con, "cc-6")["catchup"]["lit_at"]
    check("the second reopen carries its own age, not the first one's", second > first, (first, second))

    # 3 a whitespace-only prompt change is a change the card can see
    store.put_threads(con, {"threads": [thread(tid="cc-7", title="Whitespace card")]})
    lc = get(con, "cc-7")["last_change"]
    time.sleep(0.05)
    store.put_threads(con, {"threads": [thread(tid="cc-7", title="Whitespace card", ask=ask(prompt="Promote it, or keep it on testing?  "))]})
    check("last_change and lit_at agree about a changed prompt",
          (get(con, "cc-7")["last_change"] > lc) == (get(con, "cc-7")["catchup"]["lit_at"] > lc), get(con, "cc-7")["catchup"])

    # 4 a recap-only resync refreshes an open card
    store.put_threads(con, {"threads": [thread(tid="cc-8", title="Recap only card")]})
    lc = get(con, "cc-8")["last_change"]
    time.sleep(0.05)
    store.put_threads(con, {"threads": [thread(tid="cc-8", title="Recap only card", ask=ask(subject="Recap card", recap="Now it has a recap."))]})
    check("new words on the same question move last_change", get(con, "cc-8")["last_change"] > lc)

    # 5 a multi-line prompt still counts its asks
    mp = "Promote it,\nor keep it on testing?"
    store.put_threads(con, {"threads": [thread(tid="cc-9", title="Multi line card", ask=ask(prompt=mp))]})
    store.put_threads(con, {"threads": [thread(tid="cc-9", title="Multi line card", ask=ask(prompt=mp, context="More detail, same question, sent again."))]})
    check("times_asked reads 2 for a multi-line prompt", get(con, "cc-9")["catchup"]["times_asked"] == 2, get(con, "cc-9")["catchup"])

    # 6 a bot close through /result forgets the question too
    store.put_threads(con, {"threads": [thread(tid="cc-10", title="Bot closed card")]})
    check("lit before the close", bool(con.execute("SELECT ask_lit_key FROM threads WHERE thread_id='cc-10'").fetchone()[0]))
    store.reply(con, "cc-10", "promote")
    note = "I promoted the build and verified production serves it, here is the long enough report. " + "z" * 40
    store.post_result(con, "cc-10", {"outcome": "done", "state": "done", "note": note, "answer_applied": True, "nothing_owed": True,
                                     "answers": "Promoted as you asked and production serves the new build now."})
    st = con.execute("SELECT state, ask_lit_key FROM threads WHERE thread_id='cc-10'").fetchone()
    check("closed by a bot result: the key is cleared", st["state"] == "done" and st["ask_lit_key"] == "", dict(st))

    con.close()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
