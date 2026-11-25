"""tests/test_reply_note_integrity.py — Reply note integrity & provenance in pending dispatch.

Run: python3 tests/test_reply_note_integrity.py   (from repo root)

Covers:
1. Store level: reply(value, note="A") -> append_note("B") -> pending_dispatch item has reply_note == "A"
2. Store level: reply without note -> append_note("B") -> pending_dispatch item has reply_note == ""
3. Store level: provenance: normal reply has reply_note_source == "owner"
4. Store level: stall hand-off has instruction in reply_note and reply_note_source == "helm-stall"
5. Store level: multi-round reply preserves current round's note
6. HTTP route level: POST /reply -> POST /note -> GET /pending-dispatch has reply_note == "A", reply_note_source == "owner"
7. Non-interference: threads.last_note remains updated as display mirror
"""
from __future__ import annotations

import os
import sys
import tempfile

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


def test_store_reply_note_integrity():
    print("\n1. Store level: reply note preserved despite subsequent append_note")
    db_file = os.path.join(tempfile.mkdtemp(prefix="helm-test-note-"), "t.db")
    con = store.db(db_file)

    # 1.1 the owner replies with note A, then an agent appends note B
    lit(con, "t-note-1", "Note overwrite test")
    store.reply(con, "t-note-1", "first", note="the owner's original note A")
    store.append_note(con, "t-note-1", "Agent subsequent note B", actor="agent")

    # Thread row last_note display mirror should show Note B
    row = dict(con.execute("SELECT * FROM threads WHERE thread_id=?", ("t-note-1",)).fetchone())
    check("threads.last_note has display mirror Note B", row["last_note"] == "Agent subsequent note B", row["last_note"])

    # Pending dispatch must return the owner's note A, not Note B
    p = store.pending_dispatch(con)
    item = next((it for it in p["pending"] if it["thread_id"] == "t-note-1"), None)
    check("t-note-1 is pending dispatch", item is not None)
    if item:
        check("pending dispatch reply_note is Note A", item.get("reply_note") == "the owner's original note A", item.get("reply_note"))
        check("pending dispatch reply_note_source is owner", item.get("reply_note_source") == "owner", item.get("reply_note_source"))

    # 1.2 the owner replies with NO note, then an agent appends note B
    lit(con, "t-note-2", "Empty note test")
    store.reply(con, "t-note-2", "first", note="")
    store.append_note(con, "t-note-2", "Agent note on empty reply", actor="agent")

    p2 = store.pending_dispatch(con)
    item2 = next((it for it in p2["pending"] if it["thread_id"] == "t-note-2"), None)
    check("t-note-2 is pending dispatch", item2 is not None)
    if item2:
        check("pending dispatch reply_note is empty string", item2.get("reply_note") == "", item2.get("reply_note"))
        check("pending dispatch reply_note_source is owner", item2.get("reply_note_source") == "owner", item2.get("reply_note_source"))

    # 1.3 Multi-round: Round 1 note A, dispatched; Round 2 note C, followed by append_note D
    lit(con, "t-note-3", "Multi round test")
    store.reply(con, "t-note-3", "first", note="Round 1 Note")
    p3_r1 = store.pending_dispatch(con)
    it3_r1 = next(it for it in p3_r1["pending"] if it["thread_id"] == "t-note-3")
    store.mark_dispatched(con, "t-note-3", reply_ts=it3_r1["reply_ts"], ok=True, mode="start")
    store.mark_dispatched(con, "t-note-3", reply_ts=it3_r1["reply_ts"], ok=True, mode="reply")
    # Post result to re-ask the owner
    store.post_result(con, "t-note-3", {
        "outcome": "round-1-done", "state": "needs-you", "responder": "agent",
        "note": "Round 1 complete, please decide next step with all necessary details and context provided. " + "y" * 30,
        "ask": {"kind": "choice", "prompt": "Continue?", "options": [
            {"label": "Continue to step 2", "value": "step2", "effect": "Proceed"},
            {"label": "Stop now", "value": "stop", "effect": "Done"}
        ]}
    })
    # the owner replies in round 2 with note C
    store.reply(con, "t-note-3", "step2", note="Round 2 Note C")
    # Another note lands before dispatch
    store.append_note(con, "t-note-3", "Pre-dispatch status Note D", actor="monitor")

    p3_r2 = store.pending_dispatch(con)
    it3_r2 = next((it for it in p3_r2["pending"] if it["thread_id"] == "t-note-3"), None)
    check("t-note-3 round 2 is pending dispatch", it3_r2 is not None)
    if it3_r2:
        check("pending dispatch has Round 2 Note C", it3_r2.get("reply_note") == "Round 2 Note C", it3_r2.get("reply_note"))
        check("pending dispatch reply_note_source is owner", it3_r2.get("reply_note_source") == "owner", it3_r2.get("reply_note_source"))


def test_stall_hand_off_source():
    print("\n2. Store level: stall hand-off instruction and source")
    db_file = os.path.join(tempfile.mkdtemp(prefix="helm-test-stall-"), "t.db")
    con = store.db(db_file)

    lit(con, "s-stall-note", "Stall hand off note integrity test")
    store.post_result(con, "s-stall-note", {
        "outcome": "round-cap-review-unavailable",
        "state": "needs-you",
        "responder": "dispatcher-skeptic",
        "note": "Work stopped at round cap without second lane. " + "x" * 40,
        "ask": {"kind": "ack", "prompt": "Work stopped at round cap."}
    })
    # Another note lands after stall routing
    store.append_note(con, "s-stall-note", "Intervening agent note", actor="bot")

    p = store.pending_dispatch(con)
    item = next((it for it in p["pending"] if it["thread_id"] == "s-stall-note"), None)
    check("s-stall-note is pending dispatch", item is not None)
    if item:
        check("reply_note contains stall instruction", "Finish the work or name the blocker" in (item.get("reply_note") or ""), item.get("reply_note"))
        check("reply_note_source is helm-stall", item.get("reply_note_source") == "helm-stall", item.get("reply_note_source"))


def test_http_api_reply_note_integrity():
    print("\n3. HTTP API level: /reply followed by /note preserves reply_note in /pending-dispatch")
    from starlette.testclient import TestClient
    os.environ["HELM_APP_TOKEN"] = "test-token"
    tmp = tempfile.mkdtemp(prefix="helm-api-note-")
    os.environ["HELM_DB"] = os.path.join(tmp, "t.db")
    os.environ["HELM_ATTACH_DIR"] = os.path.join(tmp, "att")

    import app.service as svc
    client = TestClient(svc.app)
    auth = {"Authorization": "Bearer test-token"}

    # Create thread with non-terminal choice ask via API
    r_put = client.put("/api/triage/threads", json={"threads": [{
        "thread_id": "t-http-note-1", "title": "HTTP Note Integrity Test",
        "summary": "scratch test summary", "category": "Fleet",
        "urgency": "whenever", "state": "needs-you",
        "ask": {
            "kind": "choice",
            "prompt": "Which action should be taken?",
            "requires": ["decision"],
            "options": [
                {"label": "Run step one", "value": "step1", "effect": "Execute step 1"},
                {"label": "Run step two", "value": "step2", "effect": "Execute step 2"}
            ]
        }
    }]}, headers=auth)
    check("put thread 200", r_put.status_code == 200, r_put.text[:100])
    tid = "t-http-note-1"

    # the owner answers via HTTP with note A
    r_rep = client.post(f"/api/triage/threads/{tid}/reply", json={
        "value": "step1", "note": "the owner HTTP Note A"
    }, headers=auth)
    check("reply 202", r_rep.status_code == 202, r_rep.text[:100])

    # Intervening note posted via /note
    r_note = client.post(f"/api/triage/threads/{tid}/note", json={
        "note": "Intervening HTTP Note B", "actor": "agent"
    }, headers=auth)
    check("append note 202", r_note.status_code == 202, r_note.text[:100])

    # Query pending-dispatch via HTTP
    r_pend = client.get("/api/triage/pending-dispatch", headers=auth)
    check("pending-dispatch 200", r_pend.status_code == 200, r_pend.text[:100])
    items = r_pend.json().get("pending", [])
    item = next((it for it in items if it["thread_id"] == tid), None)
    check("thread is in HTTP pending-dispatch", item is not None)
    if item:
        check("HTTP pending-dispatch reply_note is Note A", item.get("reply_note") == "the owner HTTP Note A", item.get("reply_note"))
        check("HTTP pending-dispatch reply_note_source is owner", item.get("reply_note_source") == "owner", item.get("reply_note_source"))

    # Display mirror check via GET /threads
    r_thr = client.get("/api/triage/threads", headers=auth)
    thr_item = next((it for it in r_thr.json().get("threads", []) if it["thread_id"] == tid), None)
    check("threads list has thread", thr_item is not None)
    if thr_item:
        check("threads last_note has display mirror Note B", thr_item.get("last_note") == "Intervening HTTP Note B", thr_item.get("last_note"))


def main():
    test_store_reply_note_integrity()
    test_stall_hand_off_source()
    test_http_api_reply_note_integrity()
    print("=" * 60)
    print("%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
