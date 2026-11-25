"""The Day timeline and the store readers against a throwaway thread store
built from Helm's own DDL.

Ported from the reference suite's tests/test_day.py (55 checks), setup and imports only:
`helm_client` is `notebook.helm_adapter`, the store is `helm.store` on HELM_DB
(no private copy of a store.py file is loaded), and the Recorder fixture URL is
a synthetic host. 54 of the 55 port. The one that does not, and why:

  - "read-only URI refuses a write": the reference app opened triage.db through a
    `mode=ro` SQLite URI because it was a second process. The adapter is the
    same process as the store and opens it through helm.store.db(), which is
    read-write by design. It is replaced
    by a check that the adapter's connection IS the store's (a write through
    helm.store is visible to the adapter's readers at once).

The two loader checks ("Helm's store module loaded under a private name",
"the store module was not pointed at the real database") are kept with the
same intent: the adapter uses helm.store, on the scratch path.

Extra checks at the end cover the structured-field timeline (reply_kind
'close', helm-gate / helm-recheck sources, a web login as the source, the
`bot-ask` and `note` kinds) and the `changed` list.
"""
# temp-ok: the DB and folder under test are fixtures this module creates and deletes
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_day_")
DB = os.path.join(TMP, "triage.db")
os.environ["HELM_NOTES_PROFILE"] = "fixed"
os.environ["HELM_NOTES_MODE"] = "folder"
os.environ["HELM_NOTES_ROOT"] = os.path.join(TMP, "vault")
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = DB
os.environ["HELM_DATA"] = TMP

from helm import store  # noqa: E402
from notebook import day, helm_adapter, notes  # noqa: E402

D = dt.date(2026, 9, 22)
START, END = day.day_bounds(D)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def at(hh, mm=0):
    return START + hh * 3600 + mm * 60


def seed(store):
    import sqlite3
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    store.init(con)
    cols = [r[1] for r in con.execute("PRAGMA table_info(threads)")]

    def thread(tid, title, category="Tooling and docs", state="needs-you", urgency="now", first_seen=None,
               links=None, ask_options=None, sources=None, tombstoned=0):
        con.execute(
            "INSERT INTO threads (thread_id, title, summary, category, urgency, state, ask_kind, ask_prompt,"
            " ask_options, sources, first_seen, last_change, tombstoned) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, title, "summary of " + tid, category, urgency, state, "choice" if ask_options else "none", "",
             json.dumps(ask_options or []), json.dumps(sources or []), first_seen or at(9), first_seen or at(9),
             tombstoned))
        if links and "links" in cols:
            con.execute("UPDATE threads SET links=? WHERE thread_id=?", (json.dumps(links), tid))

    def ev(tid, hh, mm, kind, body, source="owner", source_id="", reply_kind=""):
        store.add_event(con, tid, at(hh, mm), source, source_id, kind, body, reply_kind=reply_kind)

    thread("hand", "A card you wrote", first_seen=at(9, 0))
    ev("hand", 9, 0, "state-change", "you wrote this card yourself")
    thread("bot", "Filed by a sweep", first_seen=at(9, 2), sources=[{"source": "email-sweep", "id": "x"}])
    ev("bot", 9, 2, "state-change", "filed", source="")
    thread("old", "Seen yesterday", first_seen=START - 3600, ask_options=[{"value": "ship", "label": "Ship it", "terminal": True}])
    ev("old", 10, 15, "owner-reply", "ship")
    ev("old", 10, 15, "owner-note", "a note riding along " + "x" * 400)
    ev("old", 10, 16, "state-change", "settled by choosing 'Ship it'")
    ev("old", 10, 16, "state-change", "write-back: []")
    ev("old", 11, 0, "update", "producer resync, noise")
    thread("woke", "Came back from snooze", first_seen=START - 86400)
    ev("woke", 9, 1, "bot-result", "snooze brought it back", source="helm-wake")
    ev("woke", 13, 30, "bot-result", "the run's report", source="agent", source_id="run-1")
    ev("woke", 13, 31, "failure-detail", "Attempted: something; got: nothing", source="call_recorder")
    thread("call", "Call needs a listen", category="Calls", first_seen=at(11, 6),
           links=[{"label": "Recorder #1188", "url": "https://meetings.example.com/recording/1188"}])
    ev("call", 11, 6, "state-change", "filed")
    thread("tomorrow", "Filed after midnight", first_seen=END + 60)
    ev("tomorrow", 24, 1, "owner-note", "not today")
    thread("ghost", "Tombstoned", first_seen=at(12), tombstoned=1)
    ev("ghost", 12, 0, "owner-note", "should not appear")
    ev("old", 15, 0, "park", "parked because " + "y" * 90)
    ev("old", 15, 5, "unpark", "moving again")
    ev("hand", 16, 0, "backlog", "not now")
    ev("hand", 16, 5, "unbacklog", "now")
    ev("hand", 17, 0, "owner-reply", "__redirect__")
    ev("hand", 17, 0, "owner-note", "do this instead")
    ev("hand", 17, 30, "owner-reply", "__task__")
    ev("hand", 17, 45, "owner-reply", "ack")
    ev("bot", 18, 0, "bot-result", "gave up after 3 tries", source="helm-gave-up")
    # A burst on a thread whose options Helm has already cleared, settled with a
    # label Helm double-quoted because it holds an apostrophe.
    ev("woke", 14, 0, "owner-reply", "snooze_again")
    ev("woke", 14, 0, "owner-note", "pushing it a week")
    ev("woke", 14, 0, "state-change", "settled by choosing \"Leave it, I'll write it myself\"")
    ev("hand", 17, 45, "state-change", 'settled by acknowledging: the ask was only ever "read this"')
    ev("bot", 9, 30, "state-change", "dispatch failed: rc=-1 the executor never started")
    for i in (1, 2, 3):
        thread("dup%d" % i, "Same failure card", first_seen=START - 7200)
        ev("dup%d" % i, 11, 20 + i, "state-change", "closed by you: swept")
        ev("dup%d" % i, 11, 20 + i, "state-change", "write-back: []")
    # Structured-field rows (the store writes these shapes today).
    thread("web", "Closed from the web", first_seen=START - 7200)
    ev("web", 19, 0, "state-change", "closed by you: done from the web", source="owner", reply_kind="close")
    thread("gated", "Held by the gate", first_seen=START - 7200)
    ev("gated", 19, 10, "state-change", "held: not for the owner yet", source="helm-gate")
    ev("gated", 19, 11, "bot-ask", "ask: choice: pick one", source="helm-gate")
    thread("recheck", "Parked and rechecked", first_seen=START - 7200)
    ev("recheck", 19, 20, "state-change", "recheck #1: still blocked on the vendor. Next look 2026-09-29 09:00.",
       source="helm-recheck")
    ev("recheck", 19, 40, "note", "a plain note from the app", source="owner")
    ev("recheck", 19, 50, "note", "a note from a producer", source="bridge")
    con.commit()
    con.close()
    return "links" in cols


def run() -> int:
    check("Helm's store module is the adapter's store", helm_adapter._store is store and hasattr(store, "init"))
    check("the store is pointed at the scratch database", "helm_test_day" in DB and store.DB_PATH == DB)
    has_links = seed(store)

    hcon = helm_adapter.store(DB)
    # Replaces the reference's read-only URI check: the adapter's connection is the store's own.
    store.add_event(hcon, "hand", at(8, 0), "owner", "", "note", "written through helm.store")
    hcon.commit()
    check("the adapter reads the same store helm.store writes (no second copy, no read-only URI)",
          any(e["body"] == "written through helm.store" for e in helm_adapter._events_of(hcon, "hand")))
    hcon.execute("DELETE FROM events WHERE body='written through helm.store'")
    hcon.commit()

    ts = helm_adapter.store_threads(hcon)
    check("store_threads skips tombstones", sorted(t["thread_id"] for t in ts) == ["bot", "call", "dup1", "dup2", "dup3", "gated", "hand", "old", "recheck", "tomorrow", "web", "woke"], sorted(t["thread_id"] for t in ts))
    t = helm_adapter.store_thread(hcon, "old")
    check("store_thread carries events and parsed options",
          t and len(t["events"]) >= 6 and t["ask"]["options"][0]["label"] == "Ship it", t and t["ask"])
    check("store_thread returns None for unknown", helm_adapter.store_thread(hcon, "nope") is None)

    seen = helm_adapter.store_threads_first_seen_between(hcon, START, END)
    check("first_seen window excludes yesterday, tomorrow and tombstones",
          sorted(x["thread_id"] for x in seen) == ["bot", "call", "hand"], sorted(x["thread_id"] for x in seen))
    hand = next(x for x in seen if x["thread_id"] == "hand")
    check("first event body rides along for the 'you wrote it' label", hand["first_body"].startswith("you wrote this card"))
    bot = next(x for x in seen if x["thread_id"] == "bot")
    check("a first event with no source leaves first_source empty and keeps the sources column",
          bot["first_source"] == "" and bot["sources"][0]["source"] == "email-sweep", (bot["first_source"], bot["sources"]))

    tl = day.build_timeline(hcon, START, END)
    kinds = [(dt.datetime.fromtimestamp(e["ts"]).strftime("%H:%M"), e["kind"], e["thread_id"]) for e in tl]
    check("timeline is sorted by time", [e["ts"] for e in tl] == sorted(e["ts"] for e in tl))
    check("nothing from the ghost or tomorrow", all(e["thread_id"] not in ("ghost", "tomorrow") for e in tl), kinds)
    check("filed entries for the three seen today", [e["thread_id"] for e in tl if e["kind"] == "filed"] == ["hand", "bot", "call"], kinds)
    labels = {(e["thread_id"], e["kind"]): e for e in tl}
    check("your own card says you wrote it", labels[("hand", "filed")]["label"] == "you wrote it")
    check("a swept card names its filer", labels[("bot", "filed")]["label"] == "filed by email-sweep", labels[("bot", "filed")]["label"])
    check("update events are dropped", not any(e["kind"] == "update" for e in tl))
    check("write-back and creation echoes are dropped",
          not any("write-back" in (e.get("detail") or "") for e in tl) and
          not any(e["kind"] == "state" and "wrote this card" in (e.get("detail") or "") for e in tl), kinds)
    old_rows = [e for e in tl if e["thread_id"] == "old"]
    old_settled = [e for e in old_rows if e["kind"] == "settled"]
    check("an answer, its note and the settle Helm records for one click are one row",
          len(old_settled) == 1 and [p["label"] for p in old_settled[0]["parts"]] == ["you answered", "you noted", "you settled it"],
          [(e["kind"], e["label"]) for e in old_rows])
    check("a terminal reply shows the option label once, on the settle line",
          old_settled and old_settled[0]["detail"] == "Ship it" and "Ship it" not in old_settled[0]["more"], old_settled)
    check("the note rides along, excerpted", old_settled and len(old_settled[0]["more"]) == 1
          and len(old_settled[0]["more"][0]) <= day.EXCERPT and old_settled[0]["more"][0].endswith("…"),
          old_settled and [len(m) for m in old_settled[0]["more"]])
    check("no answered or noted row survives on that thread", not any(e["kind"] in ("answered", "noted") for e in old_rows), [e["kind"] for e in old_rows])
    all_labels = {e["label"] for e in tl}
    check("redirect, task and ack replies get their own words",
          all_labels >= {"you answered with a note", "you handed it over", "you acknowledged it"}, sorted(all_labels))
    hand_ack = [e for e in tl if e["thread_id"] == "hand" and e["label"] == "you acknowledged it"]
    check("an acknowledgement and Helm's settle for it are one row with no canned tail",
          len(hand_ack) == 1 and hand_ack[0]["kind"] == "settled" and hand_ack[0]["detail"] == ""
          and [p["label"] for p in hand_ack[0]["parts"]] == ["you acknowledged it"], hand_ack)
    redirect = [e for e in tl if e["thread_id"] == "hand" and e["label"] == "you answered with a note"]
    check("a redirect carries its note", redirect and redirect[0]["more"] == ["do this instead"], redirect)
    woke_you = [e for e in tl if e["thread_id"] == "woke" and e["kind"] == "settled"]
    check("a double-quoted label is parsed", woke_you and woke_you[0]["detail"] == "Leave it, I'll write it myself", woke_you)
    check("an answer whose wording is gone is dropped under the settle line",
          woke_you and woke_you[0]["more"] == ["pushing it a week"] and not woke_you[0]["raw"], woke_you and woke_you[0]["more"])
    check("option_label humanizes a lost id and says so", day.option_label("[]", "known-i-did-that-on-purpose") == ("known i did that on purpose", False))
    check("option_label finds a label", day.option_label('[{"value":"a","label":"Ship it"}]', "a") == ("Ship it", True))
    check("settled_detail handles both quote styles and colons",
          day.settled_detail("settled by choosing 'Done'") == "Done" and day.settled_detail('settled by choosing "It\'s fine"') == "It's fine"
          and day.settled_detail("closed by you: reason here") == "reason here" and day.settled_detail("closed by you") == "")
    ev = {"ts": 1.0, "thread_id": "s", "title": "t", "category": "c", "state": "done", "urgency": "whenever"}
    s_close = day.timeline_entry(dict(ev, kind="state-change", source="session",
                                      body="closed from a chat session: posted to the case"))
    s_settle = day.timeline_entry(dict(ev, kind="state-change", source="session",
                                       body="settled from a chat session by choosing 'I sent it'"))
    s_reply = day.timeline_entry(dict(ev, kind="owner-reply", source="session", body="sent",
                                      ask_options='[{"value":"sent","label":"I sent it"}]'))
    check("a chat session's close never reads as yours",
          s_close["label"] == "a chat session closed it" and s_close["detail"] == "posted to the case"
          and s_settle["label"] == "a chat session closed it" and s_settle["detail"] == "I sent it"
          and s_reply["label"] == "a chat session answered", (s_close, s_settle, s_reply))
    # An agent's own close, and an old row's wording: both read as a run's close, by reply_kind
    # and by the body alone, never as yours.
    for src, wording in [("bot", "closed by the agent")]:
        by_kind = day.timeline_entry(dict(ev, kind="state-change", source=src, reply_kind="close",
                                          body=wording + ": applied the second option"))
        by_body = day.timeline_entry(dict(ev, kind="state-change", source="", body=wording + ": applied the second option"))
        check("%r reads as a run's close, by reply_kind and by the body alone" % wording,
              all(r["kind"] == "settled" and r["label"] == "a run closed it" and r["actor"] == "bot"
                  and r["detail"] == "applied the second option" for r in (by_kind, by_body)), (by_kind, by_body))
    check("snooze wake is its own kind", ("woke", "woke") in labels, kinds)
    check("a run's report is 'run'", ("woke", "run") in labels, kinds)
    check("gave-up is 'capped'", ("bot", "capped") in labels, kinds)
    check("bursts never cross actors: the run's report and the failure detail stay two rows",
          ("woke", "run") in labels and ("woke", "detail") in labels, kinds)
    check("a producer's detail names who added what",
          labels.get(("woke", "detail"), {}).get("label") == "call_recorder added the failure detail", labels.get(("woke", "detail"), {}).get("label"))
    check("a filing and the detail filed with it are one row", labels[("bot", "filed")]["parts"][0]["label"] == "filed by email-sweep" and labels[("bot", "filed")]["more"] == ["filed"], labels[("bot", "filed")])
    dups = [e for e in tl if e["title"] == "Same failure card"]
    check("identical closes in a sweep fold into one row with a count",
          len(dups) == 1 and dups[0]["count"] == 3 and dups[0]["thread_ids"] == ["dup1", "dup2", "dup3"]
          and dups[0]["label"] == "you closed it" and dups[0]["detail"] == "swept", dups)
    check("a dispatch failure is its own kind", labels.get(("bot", "dispatch"), {}).get("label") == "the run could not start"
          and labels.get(("bot", "dispatch"), {}).get("detail") == "rc=-1 the executor never started", kinds)
    check("settled state-change is 'settled'", ("old", "settled") in labels, kinds)
    check("five minutes apart is two rows: park and unpark", ("old", "park") in labels and ("old", "unpark") in labels, kinds)
    check("backlog and unbacklog", ("hand", "backlog") in labels and ("hand", "unbacklog") in labels, kinds)
    check("every row links to a card and carries a count and its parts", all(e["thread_id"] and e["title"] and e["count"] >= 1 and e["parts"] for e in tl))
    check("no row still carries its working timestamp", not any("last_ts" in e for e in tl))

    calls = day.calls_of_day(hcon, START, END)
    check("calls of the day come from the Calls category only", [c["thread_id"] for c in calls] == ["call"], calls)
    if has_links:
        check("the Recorder link is parsed", calls and calls[0]["recording"] == {"url": "https://meetings.example.com/recording/1188", "n": 1188, "label": "Recorder #1188"}, calls)
    else:
        check("threads table has no links column in this Helm; Recorder parse skipped", True)

    # Carry-over and assemble, with a small folder.
    vault = os.environ["HELM_NOTES_ROOT"]
    for rel, text in {
        "Daily/2026/2026-09/2026-09-19.md": "- [ ] friday leftover\n- [x] friday done\n",
        "Daily/2026/2026-09/2026-09-22.md": "# Monday\n\n- [ ] today open\n\nSee [[Some topic]].\n",
        "Projects/Ongoing/Some topic.md": "# Some topic\n",
    }.items():
        p = os.path.join(vault, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    os.utime(os.path.join(vault, "Projects", "Ongoing", "Some topic.md"), (at(15), at(15)))
    ncon = notes.db()
    notes.rebuild(ncon, force=True)
    carry = day.carry_over(ncon, D)
    check("carry-over skips the weekend to Friday's note", carry and carry["date"] == "2026-09-19" and [t["text"] for t in carry["tasks"]] == ["friday leftover"], carry)
    check("carry-over is None past the window", day.carry_over(ncon, dt.date(2026, 10, 30)) is None)
    nxt = day.carry_over(ncon, dt.date(2026, 9, 23))
    check("carry-over for the next day reads Monday's open line", nxt and nxt["date"] == "2026-09-22" and [t["text"] for t in nxt["tasks"]] == ["today open"], nxt)

    a = day.assemble(D, ncon, hcon, lambda body: "<p>%d chars</p>" % len(body))
    check("assemble names the note and renders through the callback", a["rel"] == "Daily/2026/2026-09/2026-09-22.md" and a["exists"] and a["html"].endswith("chars</p>"))
    check("assemble counts open tasks", a["open_tasks"] == 1 and a["tasks"][0]["text"] == "today open")
    check("assemble carries the timeline and calls", len(a["timeline"]) == len(tl) and len(a["calls"]) == 1)
    check("assemble lists linked topic notes", a["mentions"] == [{"rel": "Projects/Ongoing/Some topic.md", "title": "Some topic"}], a["mentions"])
    check("assemble knows prev, next and today", a["prev"] == "2026-09-21" and a["next"] == "2026-09-23" and a["today"] is False)
    b = day.assemble(dt.date(2026, 9, 24), ncon, hcon, lambda body: body)
    check("a day with no note assembles empty", b["exists"] is False and b["html"] == "" and b["tasks"] == [])

    # --- the structured fields ---------------------------------------------------------
    check("a close is known by reply_kind, and a web login reads as you",
          labels.get(("web", "settled"), {}).get("label") == "you closed it"
          and labels.get(("web", "settled"), {}).get("detail") == "done from the web", labels.get(("web", "settled")))
    check("the gate's hold and its released ask are known by source and kind",
          labels.get(("gated", "gate"), {}).get("label") == "the gate held it"
          and ("gated", "asked") in labels and labels[("gated", "asked")]["label"] == "the gate released a question", kinds)
    check("a recheck is known by its source", labels.get(("recheck", "recheck"), {}).get("label") == "a recheck came due")
    check("a plain note kind says who noted",
          ("recheck", "noted") in labels and labels[("recheck", "noted")]["label"] == "you noted"
          and any(e["thread_id"] == "recheck" and e["label"] == "bridge added a note" for e in tl), kinds)
    web_login = day.timeline_entry(dict(ev, kind="state-change", source="owner",
                                        body="settled from a chat session by choosing 'Done'"))
    check("the source outranks the body's wording: a login's settle is yours",
          web_login["label"] == "you settled it" and web_login["actor"] == "you", web_login)
    check("assemble lists the notes changed that day, the day's own note excluded",
          [n["rel"] for n in a["changed"]] == ["Projects/Ongoing/Some topic.md"], a["changed"])

    # --- a bare-string sources column (work store, 2026-09-28) -------------------------------------
    # Work cards filed before the unification hold bare strings ("sf:00000001"). A bare string is the
    # source's own id, not its filer (topic.ref_values reads it the same way); GET /api/day 500'd on it.
    hcon.execute(
        "INSERT INTO threads (thread_id, title, summary, category, urgency, state, ask_kind, ask_prompt,"
        " ask_options, sources, first_seen, last_change, tombstoned) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
        ("legacy", "Filed with string sources", "s", "Tooling and docs", "now", "needs-you", "none", "", "[]",
         json.dumps(["crm:00000001", "tickets:ABC-1"]), at(20), at(20)))
    store.add_event(hcon, "legacy", at(20), "", "", "state-change", "filed")
    hcon.commit()
    try:
        tl2, err = day.build_timeline(hcon, START, END), ""
    except Exception as e:  # noqa: BLE001 - the check reports the crash
        tl2, err = [], "%s: %s" % (type(e).__name__, e)
    legacy = [e for e in tl2 if e["thread_id"] == "legacy" and e["kind"] == "filed"]
    check("a card whose sources are bare strings still gets a timeline row", not err and len(legacy) == 1, err or legacy)
    check("...and a bare string is not read as its filer", legacy and legacy[0]["label"] == "filed", legacy)
    check("a dict source after a bare string still names the filer",
          day._filed_label({"sources": ["sf:1", None, {"source": "email-sweep", "id": "x"}]}) == "filed by email-sweep")

    # --- day bounds on the two DST days (2026-10-04 bug hunt) ---------------------------------------
    # The window ends at the next LOCAL midnight, not start + 24 h: the day is 25 h long when clocks
    # go back and 23 h when they go forward, and consecutive days must abut with no gap or overlap.
    import time
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        s, e = day.day_bounds(dt.date(2026, 11, 1))
        check("the fall-back day spans 25 h", (e - s) / 3600 == 25, (e - s) / 3600)
        s, e = day.day_bounds(dt.date(2026, 3, 8))
        check("the spring-forward day spans 23 h", (e - s) / 3600 == 23, (e - s) / 3600)
        check("an ordinary day spans 24 h", (lambda b: b[1] - b[0])(day.day_bounds(dt.date(2026, 9, 22))) == 86400)
        abut = all(day.day_bounds(d0)[1] == day.day_bounds(d0 + dt.timedelta(days=1))[0]
                   for d0 in (dt.date(2026, 10, 31), dt.date(2026, 11, 1), dt.date(2026, 3, 7), dt.date(2026, 3, 8)))
        check("consecutive days' bounds abut exactly across both DST changes", abut)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()

    ncon.close()
    hcon.close()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
