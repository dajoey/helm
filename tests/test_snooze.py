"""Adversarial verification of the Helm snooze contract.

Tests store.snooze() across durations, validation gates, event recording,
ask preservation, list hiding, and wake behaviour.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import store  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail else ""))


def setup_db():
    fd, path = tempfile.mkstemp(prefix="helm-snooze-", suffix=".db")
    os.close(fd)
    con = store.db(path)
    return con, path


def thread(tid, **kw):
    ask = kw.get("ask", {
        "kind": "choice",
        "prompt": "Pick an option",
        "options": [{"label": "Action A", "value": "a", "effect": "Do A"},
                    {"label": "Action B", "value": "b", "effect": "Do B"}]
    })
    if str(ask.get("kind")) in ("choice", "text"):
        ask = dict(ask, requires=ask.get("requires", ["decision"]))
    _DIG = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
            "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}
    t = {
        "thread_id": tid,
        "title": kw.get("title", "concern " + "".join(_DIG.get(c, c) for c in tid)),
        "summary": kw.get("summary", "a summary"),
        "category": kw.get("category", "Fleet"),
        "urgency": kw.get("urgency", "this-week"),
        "state": kw.get("state", "needs-you"),
        "ask": ask,
        "sources": kw.get("sources", [{"source": "tracker", "id": "card-1"}]),
    }
    if "events" in kw:
        t["events"] = kw["events"]
    if "first_seen" in kw:
        t["first_seen"] = kw["first_seen"]
    if t["state"] == "waiting-external":
        t["blocked_on"] = kw.get("blocked_on", "the vendor support desk")
    return t


def main():
    global PASS, FAIL
    con, path = setup_db()
    now = time.time()

    print("\nhappy path: snooze across standard durations")
    store.put_threads(con, {"threads": [
        thread("t-sn-1h", title="concern one h snooze"),
        thread("t-sn-1d", title="concern one d snooze"),
        thread("t-sn-1w", title="concern one w snooze"),
        thread("t-sn-1mo", title="concern one mo snooze"),
        thread("t-sn-frac", title="concern frac snooze"),
    ]})

    # 1h
    r1 = store.snooze(con, "t-sn-1h", hours=1)
    check("1h snooze accepted", r1.get("accepted") is True, r1)
    check("1h returns state=snoozed", r1.get("state") == "snoozed", r1)
    check("1h returns wake_at ~ now + 3600", abs(r1["wake_at"] - (now + 3600)) < 120, r1)
    check("1h returns hidden_until == wake_at", r1["hidden_until"] == r1["wake_at"], r1)
    check("1h returns ISO wakes string", isinstance(r1.get("wakes"), str) and len(r1["wakes"]) > 10, r1)

    row1 = con.execute("SELECT * FROM threads WHERE thread_id='t-sn-1h'").fetchone()
    check("stored state is waiting-external", row1["state"] == "waiting-external", row1["state"])
    check("stored wake_at matches", row1["wake_at"] == r1["wake_at"], row1["wake_at"])
    check("stored wake_note mentions 1h", "1h" in row1["wake_note"], row1["wake_note"])
    check("stored wake_ask preserved original choice",
          json.loads(row1["wake_ask"]).get("kind") == "choice", row1["wake_ask"])
    ev1 = con.execute("SELECT body FROM events WHERE thread_id='t-sn-1h' AND kind='state-change'").fetchall()
    check("state-change event recorded", len(ev1) == 1 and "snoozed by the owner for 1h" in ev1[0]["body"], ev1)

    # 1d, 1w, 1mo, 1.5h
    r_1d = store.snooze(con, "t-sn-1d", hours=24)
    check("1d ~ now + 86400", abs(r_1d["wake_at"] - (now + 86400)) < 120, r_1d)
    row_1d = con.execute("SELECT wake_note FROM threads WHERE thread_id='t-sn-1d'").fetchone()
    check("wake_note mentions 1d", "1d" in row_1d["wake_note"], row_1d["wake_note"])

    r_1w = store.snooze(con, "t-sn-1w", hours=168)
    check("1w ~ now + 7*86400", abs(r_1w["wake_at"] - (now + 7 * 86400)) < 120, r_1w)

    r_1mo = store.snooze(con, "t-sn-1mo", hours=720)
    check("1mo ~ now + 30*86400", abs(r_1mo["wake_at"] - (now + 30 * 86400)) < 120, r_1mo)

    r_frac = store.snooze(con, "t-sn-frac", hours="1.5")
    check("fractional hours accepted", abs(r_frac["wake_at"] - (now + 5400)) < 120, r_frac)

    print("\nsnooze with custom note")
    store.put_threads(con, {"threads": [thread("t-sn-note")]})
    r_note = store.snooze(con, "t-sn-note", hours=24, note="waiting on vendor reply")
    row_note = con.execute("SELECT wake_note FROM threads WHERE thread_id='t-sn-note'").fetchone()
    check("custom wake_note kept", row_note["wake_note"] == "waiting on vendor reply", row_note["wake_note"])
    ev_note = con.execute("SELECT body FROM events WHERE thread_id='t-sn-note' AND kind='state-change'").fetchall()
    check("event carries custom note", "waiting on vendor reply" in ev_note[0]["body"], ev_note)

    print("\nbackward compatibility: positional and direct date arguments")
    store.put_threads(con, {"threads": [
        thread("t-sn-pos"),
        thread("t-sn-iso"),
    ]})
    target_epoch = now + 4 * 86400
    r_pos = store.snooze(con, "t-sn-pos", target_epoch, "later")
    check("positional call accepted", abs(r_pos["wake_at"] - target_epoch) < 5, r_pos)

    iso_date = dt.datetime.fromtimestamp(now + 2 * 86400, store._LOCAL_TZ).isoformat()
    r_iso = store.snooze(con, "t-sn-iso", wake_at=iso_date)
    check("ISO date string accepted", abs(r_iso["wake_at"] - (now + 2 * 86400)) < 120, r_iso)

    print("\nre-snooze replaces wake date")
    store.put_threads(con, {"threads": [thread("t-sn-resnooze")]})
    store.snooze(con, "t-sn-resnooze", hours=1)
    store.snooze(con, "t-sn-resnooze", hours=168)
    row_re = con.execute("SELECT wake_at FROM threads WHERE thread_id='t-sn-resnooze'").fetchone()
    check("last snooze wins", abs(row_re["wake_at"] - (now + 168 * 3600)) < 120, row_re["wake_at"])

    print("\nre-snooze keeps the stashed question")
    store.put_threads(con, {"threads": [thread("t-sn-keep", title="wildcard cert renewal",
                                               ask={"kind": "choice", "prompt": "Renew the wildcard cert now or wait?",
                                                    "options": [{"label": "Renew now", "value": "now", "effect": "Renew"},
                                                                {"label": "Wait", "value": "wait", "effect": "Wait"}]})]})
    store.snooze(con, "t-sn-keep", hours=24)
    store.snooze(con, "t-sn-keep", hours=168)
    row_keep = con.execute("SELECT * FROM threads WHERE thread_id='t-sn-keep'").fetchone()
    stash_keep = json.loads(row_keep["wake_ask"])
    check("re-snooze keeps the choice kind", stash_keep.get("kind") == "choice", stash_keep)
    check("re-snooze keeps the original prompt",
          stash_keep.get("prompt") == "Renew the wildcard cert now or wait?", stash_keep)
    check("re-snooze keeps the options", [o.get("value") for o in stash_keep.get("options", [])] == ["now", "wait"], stash_keep)
    check("re-snooze still moves the wake date", abs(row_keep["wake_at"] - (now + 168 * 3600)) < 120, row_keep["wake_at"])
    con.execute("UPDATE threads SET wake_at=? WHERE thread_id='t-sn-keep'", (time.time() - 1,))
    con.commit()
    store.wake_due(con)
    woke = con.execute("SELECT state, ask_kind, ask_prompt, ask_options FROM threads WHERE thread_id='t-sn-keep'").fetchone()
    check("woken card is the original choice, not an ack",
          woke["state"] == "needs-you" and woke["ask_kind"] == "choice"
          and woke["ask_prompt"] == "Renew the wildcard cert now or wait?"
          and len(json.loads(woke["ask_options"])) == 2, dict(woke))

    print("\nwake marks the re-lit ask stale when a bot reported while it was snoozed")

    def wake_now(tid):
        con.execute("UPDATE threads SET wake_at=? WHERE thread_id=?", (time.time() - 1, tid))
        con.commit()
        store.wake_due(con)
        return store.get_thread(con, tid)["ask"]

    store.put_threads(con, {"threads": [thread("t-sn-stale", title="stale ask after progress")]})
    store.snooze(con, "t-sn-stale", hours=1)
    store.add_event(con, "t-sn-stale", time.time() + 5, "agent", "", "bot-result", "finished the work")
    con.commit()
    ask_stale = wake_now("t-sn-stale")
    check("re-lit ask is flagged stale", ask_stale.get("stale") is True, ask_stale)
    check("stale ask keeps its question", ask_stale.get("kind") == "choice" and len(ask_stale["options"]) == 2, ask_stale)

    store.put_threads(con, {"threads": [thread("t-sn-fresh", title="fresh ask after quiet snooze")]})
    store.snooze(con, "t-sn-fresh", hours=1)
    ask_fresh = wake_now("t-sn-fresh")
    check("no report while snoozed: ask not stale", not ask_fresh.get("stale"), ask_fresh)

    store.put_threads(con, {"threads": [thread("t-sn-before", title="report before the snooze")]})
    store.add_event(con, "t-sn-before", time.time() - 5, "agent", "", "bot-result", "progress before")
    con.commit()
    store.snooze(con, "t-sn-before", hours=1)
    ask_before = wake_now("t-sn-before")
    check("report from before the snooze: ask not stale", not ask_before.get("stale"), ask_before)

    store.put_threads(con, {"threads": [thread("t-sn-new", title="stale then asked afresh")]})
    store.snooze(con, "t-sn-new", hours=1)
    store.add_event(con, "t-sn-new", time.time() + 5, "agent", "", "bot-result", "moved on")
    con.commit()
    wake_now("t-sn-new")
    store.put_threads(con, {"threads": [thread("t-sn-new", title="stale then asked afresh",
                                               ask={"kind": "choice", "prompt": "A different question now?",
                                                    "options": [{"label": "Yes", "value": "y", "effect": "Do it"},
                                                                {"label": "No", "value": "n", "effect": "Skip"}]})]})
    ask_new = store.get_thread(con, "t-sn-new")["ask"]
    check("a new question is never stale", ask_new.get("prompt") == "A different question now?"
          and not ask_new.get("stale"), ask_new)

    print("\nre-snooze keeps a run's recheck mode")
    store.put_threads(con, {"threads": [thread("t-sn-rc")]})
    store.snooze(con, "t-sn-rc", hours=24)
    con.execute("UPDATE threads SET wake_mode='recheck', wake_set_at=?, wake_note='check the vendor ticket'"
                " WHERE thread_id='t-sn-rc'", (now - 100,))
    con.commit()
    store.snooze(con, "t-sn-rc", hours=48)
    row_rc = con.execute("SELECT wake_mode, wake_set_at, wake_note, wake_ask FROM threads WHERE thread_id='t-sn-rc'").fetchone()
    check("re-snooze keeps recheck mode", row_rc["wake_mode"] == "recheck", row_rc["wake_mode"])
    check("re-snooze keeps the run's set time", abs(row_rc["wake_set_at"] - (now - 100)) < 1, row_rc["wake_set_at"])
    check("re-snooze keeps the run's note", row_rc["wake_note"] == "check the vendor ticket", row_rc["wake_note"])
    check("recheck re-snooze keeps the stashed choice", json.loads(row_rc["wake_ask"]).get("kind") == "choice", row_rc["wake_ask"])

    print("\nre-snooze with a new note replaces the note")
    store.snooze(con, "t-sn-rc", hours=72, note="new reason")
    row_rc2 = con.execute("SELECT wake_note FROM threads WHERE thread_id='t-sn-rc'").fetchone()
    check("explicit note wins on re-snooze", row_rc2["wake_note"] == "new reason", row_rc2["wake_note"])

    print("\nvalidation gates")
    store.put_threads(con, {"threads": [thread("t-sn-val")]})

    def bad(kw, match=""):
        try:
            store.snooze(con, "t-sn-val", **kw)
            return False, "did not raise"
        except store.StoreError as e:
            if match and match not in e.detail:
                return False, f"detail mismatch: {e.detail}"
            return True, e.status
        except Exception as e:
            return False, f"unexpected exception: {e}"

    ok, s = bad({}, "required")
    check("missing hours/wake_at -> 400", ok and s == 400, s)

    ok, s = bad({"hours": "abc"}, "number")
    check("non-numeric hours -> 400", ok and s == 400, s)

    ok, s = bad({"hours": 0}, "positive")
    check("hours=0 -> 400", ok and s == 400, s)

    ok, s = bad({"hours": -5}, "positive")
    check("negative hours -> 400", ok and s == 400, s)

    ok, s = bad({"hours": 100000}, "more than")
    check("excessive hours -> 400", ok and s == 400, s)

    ok, s = bad({"wake_at_raw": "not-a-date"}, "required")
    check("garbage wake_at_raw -> 400", ok and s == 400, s)

    ok, s = bad({"wake_at_raw": now - 3600}, "in the past")
    check("wake_at in past -> 400", ok and s == 400, s)

    # done thread
    con.execute("UPDATE threads SET state='done' WHERE thread_id='t-sn-val'")
    con.commit()
    ok, s = bad({"hours": 1}, "closed")
    check("done thread -> 400 (reopening is a re-file)", ok and s == 400, s)

    # nonexistent thread
    try:
        store.snooze(con, "t-nonexistent", hours=1)
        check("nonexistent thread -> 404", False, "did not raise")
    except store.StoreError as e:
        check("nonexistent thread -> 404", e.status == 404, e.status)

    print("\nhiding and waking behaviour")
    store.put_threads(con, {"threads": [thread("t-sn-hide", ask={
        "kind": "choice",
        "prompt": "Restore question?",
        "options": [{"label": "Yes", "value": "y", "effect": ""},
                    {"label": "No", "value": "n", "effect": ""}]
    })]})
    store.snooze(con, "t-sn-hide", hours=24)

    list1 = store.list_threads(con)
    tids1 = {t["thread_id"] for t in list1["threads"]}
    check("snoozed thread hidden from default list", "t-sn-hide" not in tids1, tids1)

    list_snoozed = store.list_threads(con, include_snoozed=True)
    tids_s = {t["thread_id"] for t in list_snoozed["threads"]}
    check("snoozed thread visible with include_snoozed=True", "t-sn-hide" in tids_s, tids_s)

    # simulate time expiring
    con.execute("UPDATE threads SET wake_at=? WHERE thread_id='t-sn-hide'", (now - 10,))
    con.commit()

    list_woken = store.list_threads(con)
    check("woke counter reported 1", list_woken["woke"] >= 1, list_woken["woke"])
    row_woken = con.execute("SELECT * FROM threads WHERE thread_id='t-sn-hide'").fetchone()
    check("state restored to needs-you", row_woken["state"] == "needs-you", row_woken["state"])
    check("wake_at cleared to 0", row_woken["wake_at"] == 0, row_woken["wake_at"])
    check("preserved choice ask restored", row_woken["ask_kind"] == "choice", row_woken["ask_kind"])
    check("restored ask has options", "Yes" in row_woken["ask_options"], row_woken["ask_options"])
    ev_woken = con.execute("SELECT body FROM events WHERE thread_id='t-sn-hide' AND source='helm-wake'").fetchall()
    check("helm-wake event appended", len(ev_woken) >= 1, ev_woken)

    print("\nInvariant 5: a bot result must not cancel a snooze the owner set")
    # the owner's exact 2026-09-17 sequence: snooze at 21:21:09, the producer
    # re-posted the same ask at 21:22:01, and the card was lit again.
    store.put_threads(con, {"threads": [thread("t-sn-inv5", title="concern inv five deferral")]})
    snz = store.snooze(con, "t-sn-inv5", hours=24)
    check("snooze() reports state=snoozed to its caller", snz.get("state") == "snoozed", snz)
    row_pre = con.execute("SELECT * FROM threads WHERE thread_id='t-sn-inv5'").fetchone()
    wake_pre = row_pre["wake_at"]
    # snooze() stores 'waiting-external' + wake_at; wake_at is the fact that hides it.
    check("deferred before the result lands", wake_pre > time.time(), wake_pre)

    res = store.post_result(con, "t-sn-inv5", {
        "outcome": "needs-your-call",
        "state": "needs-you",
        "note": ("The task is paused again and needs your call. Whatever you tap or "
                 "write here goes straight back into it and it resumes."),
        "ask": {"kind": "choice",
                "prompt": "Build 0.1.58.0 is ready - after updating the plugin, does auto-market move stock?",
                "requires": ["decision"],
                "options": [{"label": "Yes, stock moves now", "value": "works",
                             "effect": "Closes the task as fixed and ships the build."},
                            {"label": "No, still stuck", "value": "still-stuck",
                             "effect": "Sends it back for another round of diagnosis."}]},
        "responder": "agent"})
    row_post = con.execute("SELECT * FROM threads WHERE thread_id='t-sn-inv5'").fetchone()
    check("result was accepted", res.get("accepted") is True, res)
    check("result reports snooze_kept", res.get("snooze_kept") is True, res)
    check("result echoes the state it wanted", res.get("state_requested") == "needs-you", res)
    check("thread is STILL deferred", row_post["wake_at"] > time.time(), row_post["wake_at"])
    check("wake_at survived untouched", row_post["wake_at"] == wake_pre,
          (row_post["wake_at"], wake_pre))
    check("card is not lit underneath", (row_post["ask_kind"] or "none") == "none",
          row_post["ask_kind"])
    check("thread stays out of the board", all(
        t["thread_id"] != "t-sn-inv5" for t in store.list_threads(con)["threads"]))
    check("the report is still in the history", any(
        "0.1.58.0" in (e["body"] or "") or "needs your call" in (e["body"] or "")
        for e in con.execute(
            "SELECT body FROM events WHERE thread_id='t-sn-inv5'").fetchall()))
    check("newest question rides along in wake_ask",
          "0.1.58.0" in (row_post["wake_ask"] or ""), row_post["wake_ask"])
    check("a new question re-opens what is owed", row_post["answer_applied"] == 0,
          row_post["answer_applied"])

    # ...and it is the NEWEST question that comes back, not the stale one.
    store.post_result(con, "t-sn-inv5", {
        "outcome": "needs-your-call", "state": "needs-you",
        "note": ("Superseded: build 0.1.59.0 replaces the one you were asked about earlier. "
                 "Same question, newer build; this is the one that should come back to you."),
        "ask": {"kind": "choice", "prompt": "Build 0.1.59.0 is ready - does auto-market move stock?",
                "requires": ["decision"],
                "options": [{"label": "Yes, stock moves now", "value": "works",
                             "effect": "Closes the task as fixed and ships the build."},
                            {"label": "No, still stuck", "value": "still-stuck",
                             "effect": "Sends it back for another round of diagnosis."}]},
        "responder": "agent"})
    con.execute("UPDATE threads SET wake_at=? WHERE thread_id='t-sn-inv5'", (time.time() - 5,))
    store.list_threads(con)
    row_woke5 = con.execute("SELECT * FROM threads WHERE thread_id='t-sn-inv5'").fetchone()
    check("wakes up lit again", row_woke5["state"] == "needs-you", row_woke5["state"])
    check("wakes with the NEWEST question", "0.1.59.0" in (row_woke5["ask_prompt"] or ""),
          row_woke5["ask_prompt"])

    print("\nInvariant 5: a parked thread with a wake date is defended the same way")
    store.put_threads(con, {"threads": [thread("t-sn-inv5b", title="concern inv five b deferral")]})
    store.snooze(con, "t-sn-inv5b", hours=6)
    store.post_result(con, "t-sn-inv5b", {
        "outcome": "waiting-until", "state": "waiting-external",
        "blocked_on": "The producer resumes the task on its own schedule",
        "recheck_at": time.time() + 3 * 86400,
        "note": ("Parked until the next scheduled resume; nothing is needed from you, and "
                 "the outcome will be reported back on this same card when it lands."),
        "responder": "agent"})
    row_b = con.execute("SELECT * FROM threads WHERE thread_id='t-sn-inv5b'").fetchone()
    check("a park result cannot cancel the snooze either", row_b["wake_at"] > time.time(),
          row_b["wake_at"])
    check("park result left wake_at alone", row_b["wake_at"] > 0, row_b["wake_at"])

    print("\nInvariant 5 does not block the legitimate doors")
    store.put_threads(con, {"threads": [thread("t-sn-inv5c", title="concern inv five c deferral")]})
    store.snooze(con, "t-sn-inv5c", hours=6)
    first = con.execute("SELECT wake_at FROM threads WHERE thread_id='t-sn-inv5c'").fetchone()[0]
    store.post_result(con, "t-sn-inv5c", {
        "outcome": "deferring-further", "state": "snoozed",
        "wake_at": time.time() + 48 * 3600,
        "note": ("Pushing this out further; there is nothing to look at until the vendor "
                 "replies, so the card should stay down until then rather than nag you."),
        "responder": "agent"})
    row_c = con.execute("SELECT * FROM threads WHERE thread_id='t-sn-inv5c'").fetchone()
    check("an explicit re-snooze may still move the date", row_c["wake_at"] > first,
          (row_c["wake_at"], first))

    store.put_threads(con, {"threads": [thread("t-sn-inv5d", title="concern inv five d deferral")]})
    store.snooze(con, "t-sn-inv5d", hours=6)
    store.reply(con, "t-sn-inv5d", "a")
    row_d = con.execute("SELECT * FROM threads WHERE thread_id='t-sn-inv5d'").fetchone()
    check("the owner's own reply still clears the snooze", row_d["wake_at"] == 0, row_d["wake_at"])
    check("...and the thread is visible while it is worked", any(
        t["thread_id"] == "t-sn-inv5d" for t in store.list_threads(con)["threads"]))
    check("...so no stale wake can ambush it later", (row_d["wake_ask"] or "") == "",
          row_d["wake_ask"])

    print("\nnaive dates follow the host zone's DST, not the offset at import")
    probe = (
        "import json; from helm import store; "
        "print(json.dumps([store._parse_wake_at(x) for x in "
        "('2026-12-01T09:00:00', '2026-07-01T09:00:00', '2026-12-01')]))")
    env = dict(os.environ, TZ="America/New_York")
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        got = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        got = None
    utc = dt.timezone.utc
    want = [dt.datetime(2026, 12, 1, 14, 0, tzinfo=utc).timestamp(),   # 09:00 EST
            dt.datetime(2026, 7, 1, 13, 0, tzinfo=utc).timestamp(),    # 09:00 EDT
            dt.datetime(2026, 12, 1, 5, 0, tzinfo=utc).timestamp()]    # midnight EST
    check("winter naive date is read as EST", got and got[0] == want[0], (got, out.stderr[-300:]))
    check("summer naive date is read as EDT", got and got[1] == want[1], got)
    check("date-only value lands on that local day", got and got[2] == want[2], got)

    con.close()
    if os.path.exists(path):
        os.remove(path)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
