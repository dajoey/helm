"""The store's event clock never runs backwards on a thread.

reply() dates its reply, note and settle events a millisecond apart so they stay in
order, and post_result() does the same for a report and its gate events. Two calls
within the same millisecond (a fast CI runner; a result landing on the heels of the
tap that queued it) used to interleave by wall clock: the owner's note, dated a
millisecond into the future, outdated the report that answered it, so the answer
gate demanded the answer again and refused the next result. Seen once on GitHub
Actions (run 36183650248, test_answer_gate "a later result on the same thread needs
no answers"; measured locally at -0.14 ms in the worst of 30 runs).

The clock is frozen here: every call sees the SAME time.time(), which is worse than
any runner, and the order must still hold.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HELM_SITE"] = "home"   # the answer gate is a home rule

from helm import store  # noqa: E402

PASS = FAIL = 0
NOTE = "I went and did the thing, and here is the long-enough report about it. " + "z" * 40
ASK = {"kind": "choice", "prompt": "Send the draft, or hold it?", "requires": ["decision"],
       "options": [{"label": "Send the draft", "value": "send", "effect": "it goes out"},
                   {"label": "Hold the draft", "value": "hold", "effect": "nothing goes out"}]}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def status(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return 202
    except store.StoreError as e:
        return e.status


def run():
    con = store.db(os.path.join(tempfile.mkdtemp(prefix="helm-clock-"), "t.db"))
    frozen = time.time()
    real_time = time.time
    time.time = lambda: frozen          # helm.store reads the same `time` module
    try:
        print("\nfrozen clock: a report posted in the same instant as the tap still comes after its note")
        store.put_threads(con, {"threads": [{
            "thread_id": "c-1", "title": "concern clock one", "summary": "a summary", "category": "Fleet",
            "urgency": "this-week", "state": "needs-you", "ask": dict(ASK)}]})
        store.reply(con, "c-1", "send", note="Send it, but why did the first attempt bounce?")
        note_ts = con.execute("SELECT ts FROM events WHERE thread_id='c-1' AND kind='owner-note'").fetchone()[0]
        check("the note is dated after the reply it rides with", note_ts > frozen, note_ts - frozen)
        body = {"outcome": "sent", "state": "needs-you", "note": NOTE,
                "ask": {"kind": "ack", "prompt": "Read the report and close this."}}
        check("a report with no answer is still refused (the gate itself is untouched)",
              status(store.post_result, con, "c-1", body) == 400)
        check("the answer lands", status(store.post_result, con, "c-1", dict(
            body, answers="It bounced because the recipient domain rejected the old relay IP.")) == 202)
        report_ts = con.execute("SELECT MAX(ts) FROM events WHERE thread_id='c-1' AND kind='bot-result'").fetchone()[0]
        check("the report is dated after the note it answers, though the wall clock never moved",
              report_ts > note_ts, (report_ts - note_ts) * 1000)
        store.reply(con, "c-1", "ack")
        check("a later report on the same thread needs no answers (the question was consumed)",
              status(store.post_result, con, "c-1", {"outcome": "checked", "state": "waiting-external",
                                                     "note": NOTE, "blocked_on": "the relay vendor's confirmation"}) == 202)
        ts = [r[0] for r in con.execute("SELECT ts FROM events WHERE thread_id='c-1' ORDER BY rowid")]
        # Within one call a report and the ask it raises share an instant (the seed's own
        # stamping); across calls the clock only moves forward.
        check("no event on the thread is dated before the one written before it",
              all(b >= a for a, b in zip(ts, ts[1:])), [round((t - frozen) * 1000, 3) for t in ts])
        ack_ts = con.execute("SELECT MAX(ts) FROM events WHERE thread_id='c-1' AND kind='owner-reply'").fetchone()[0]
        last_report = con.execute("SELECT MAX(ts) FROM events WHERE thread_id='c-1' AND kind='bot-result'").fetchone()[0]
        check("the ack is dated after the report it acknowledges, and the last report after the ack",
              ack_ts > report_ts and last_report > ack_ts, (report_ts - frozen, ack_ts - frozen, last_report - frozen))
        check("and none of them is dated more than a few milliseconds ahead of the clock",
              max(ts) - frozen < 0.05, max(ts) - frozen)

        print("\nthe clock is only ever nudged forward, never back, and only when it has to be")
        con.execute("DELETE FROM events WHERE thread_id='c-1'")
        check("with no events the wall clock is used as it is", store._after_latest(con, "c-1", frozen) == frozen)
        store.add_event(con, "c-1", frozen - 10, "bot", "", "bot-result", "an old report")
        check("a clock ahead of the latest event is used as it is", store._after_latest(con, "c-1", frozen) == frozen)
        store.add_event(con, "c-1", frozen + 0.004, "helm-gate", "", "state-change", "a gate note from the same instant")
        check("a clock behind the latest event lands one millisecond after it",
              abs(store._after_latest(con, "c-1", frozen) - (frozen + 0.005)) < 1e-6)   # epoch-sized floats
    finally:
        time.time = real_time
    con.close()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
