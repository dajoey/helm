"""One ordered event list per conversation: helm/conversation.py and the api
role's GET /api/triage/threads/{id}/conversation.

Run: python3 tests/test_conversation.py

The merge types a Helm thread's events for the timeline (the owner's replies and notes and the
agent's reports are messages; state changes, claims and parks are system lines; a past ask
is its prompt line), keeps the live ask out of the timeline (the composer draws it: nothing
drawn twice), adds a task's ## Progress and live run block when there is one, orders
everything by time and drops duplicates: a note the owner wrote on the thread and the same note
mirrored into the task's progress are one message. The api route serves the Helm side with
`console: "none"`, 404 for an unknown thread.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ.pop("HELM_MODULES", None)
TMP = tempfile.mkdtemp(prefix="helm-conv-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")

import app.service as svc  # noqa: E402
from helm import conversation as conv  # noqa: E402
from helm import store  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


NOW = time.time()
ASK = {"kind": "choice", "prompt": "Prune or grow?", "requires": ["decision"],
       "options": [{"label": "Prune older than 90 days", "value": "prune", "effect": "Deletes 180 GB"},
                   {"label": "Grow the volume", "value": "grow", "effect": "A short outage"}]}


def ev(i, kind, body, source="bot", dt=0):
    return {"event_id": "e%d" % i, "ts": NOW - 1000 + i * 10 + dt, "kind": kind, "body": body, "source": source}


print("typing the Helm side")
lit = {"thread_id": "t-1", "state": "needs-you", "ask": ASK, "events": [
    ev(1, "filed", "Filed by the disk sweep", "sweep"),
    ev(2, "bot-ask", "ASKED YOU (choice): Old question\nwith options text", "bot"),
    ev(3, "owner-reply", "grow", "owner"),
    ev(4, "bot-result", "Grew it by 500 GB; the volume is at 61%.", "agent:worker"),
    ev(5, "owner-note", "Thanks, keep an eye on it", "owner"),
    ev(6, "state-change", "parked on the owner: needs a decision", "helm"),
    ev(7, "bot-ask", "ASKED YOU (choice): Prune or grow?", "bot"),
]}
evs = conv.helm_events(lit)
by_id = {e["id"]: e for e in evs}
check("the live ask (newest bot-ask on a lit card) stays out of the timeline", "e7" not in by_id, list(by_id))
check("an older ask shows its prompt line only", by_id["e2"]["type"] == "ask" and by_id["e2"]["body"] == "Old question", by_id["e2"])
check("an answer reads as the option's label", by_id["e3"]["type"] == "message" and by_id["e3"]["author"] == "You"
      and by_id["e3"]["body"] == "You answered: Grow the volume", by_id["e3"])
check("a bot result is the agent's report, a message", by_id["e4"]["type"] == "message" and by_id["e4"]["kind"] == "report"
      and by_id["e4"]["author"] == "agent:worker", by_id["e4"])
check("the owner's note is his message (no delivery stamp on Helm's side)", by_id["e5"]["type"] == "message" and by_id["e5"]["kind"] == "note" and "delivered" not in by_id["e5"], by_id["e5"])
check("a state change is a labelled system line", by_id["e6"]["type"] == "system" and by_id["e6"]["body"].startswith("state · parked"), by_id["e6"])
check("a filed event is a labelled system line", by_id["e1"]["body"].startswith("filed · "), by_id["e1"])
done = dict(lit, state="done")
check("once the card is not lit, the last ask is history too", any(e["id"] == "e7" for e in conv.helm_events(done)))
check("a reply whose option is gone still reads", conv.helm_events({"state": "done", "events": [ev(1, "owner-reply", "wait_a_week", "owner")]})[0]["body"]
      == "You answered: wait a week")
settled = {"state": "done", "ask": {"kind": "none"}, "events": [
    {"ts": NOW - 50, "kind": "bot-ask", "source": "bot", "body": "ASKED YOU (choice): Fix the cron leg now?\n- Fix it now -- The leg is patched\n- Leave it until Monday -- No digest this weekend"},
    {"ts": NOW - 40, "kind": "owner-reply", "source": "owner", "body": "fix"},
    {"ts": NOW - 30, "kind": "owner-reply", "source": "owner", "body": "wait"}]}
got = [e["body"] for e in conv.helm_events(settled) if e["type"] == "message"]
check("a settled card's answer reads by the label the question listed", got[0] == "You answered: Fix it now", got)
check("a value no label holds reads as its own words", got[1] == "You answered: wait", got)
special = {"state": "done", "ask": {"kind": "none"}, "events": [
    {"ts": NOW - 50, "kind": "bot-ask", "source": "bot", "body": "ASKED YOU (choice): What next?\n- Make it a task -- A worker picks it up\n- Redirect it -- Send it elsewhere"},
    {"ts": NOW - 40, "kind": "owner-reply", "source": "owner", "body": "__task__"},
    {"ts": NOW - 30, "kind": "owner-reply", "source": "owner", "body": "__redirect__"}]}
got = [e["body"] for e in conv.helm_events(special) if e["type"] == "message"]
check("Helm's own answer values keep their fixed wording", got == ["You answered: handed it over", "You answered: with a note"], got)
att = conv.helm_events({"state": "working", "events": [{"ts": NOW, "kind": "agent-attachment", "source": "agent:worker",
                                                         "body": "FILE ATTACHED -- OPEN IT BEFORE YOU ANSWER: disk-usage.png (image/png, 2.4 KB)\nThe volume over a week\n/api/triage/threads/t/attachments/a1"}]})[0]
check("an attachment reads as a file, not an instruction to an agent", att["type"] == "message" and att["body"] == "📎 disk-usage.png (2.4 KB)\nThe volume over a week", att)
check("an empty non-ask event is skipped", conv.helm_events({"state": "done", "events": [ev(1, "note", "", "owner")]}) == [])

# A machine's hand-off keeps the owner-reply / owner-note kinds with the machine's source
# (store.handoff_source); the timeline must not write it up as the owner's own.
for who in ("api", "helm-app-error-watch"):
    hand = conv.helm_events({"state": "working", "events": [
        {"ts": NOW - 20, "kind": "owner-reply", "source": who, "body": "__task__"},
        {"ts": NOW - 19.999, "kind": "owner-note", "source": who, "body": "Re-arm it and report."}]})
    check("a hand-off's reply from %s is authored by it" % who, hand[0]["author"] == who and hand[0]["kind"] == "reply"
          and hand[0]["body"] == "%s answered: handed it over" % who, hand[0])
    check("a hand-off's note from %s is authored by it" % who, hand[1]["author"] == who and hand[1]["kind"] == "note"
          and hand[1]["body"] == "Re-arm it and report.", hand[1])
# A relay that answers on the owner's word reads by its readable name.
relay = conv.helm_events({"state": "done", "ask": {"kind": "none"}, "events": [
    {"ts": NOW - 50, "kind": "bot-ask", "source": "bot", "body": "ASKED YOU (choice): Prune or grow?\n- Grow the volume -- A short outage\n- Prune -- Deletes 180 GB"},
    {"ts": NOW - 40, "kind": "owner-reply", "source": "relay-mcp", "body": "grow"},
    {"ts": NOW - 39.999, "kind": "owner-note", "source": "relay-mcp", "body": "On the owner's word."}]})
msgs = [e for e in relay if e["type"] == "message"]
check("a relay-mcp answer is authored 'Relay'", msgs[0]["author"] == "Relay", msgs[0])
check("... and reads 'Relay answered: <label>'", msgs[0]["body"] == "Relay answered: Grow the volume", msgs[0])
check("a relay-mcp note is authored 'Relay'", msgs[1]["author"] == "Relay" and msgs[1]["body"] == "On the owner's word.", msgs[1])
check("the raw source stays on the event", msgs[0]["source"] == "relay-mcp", msgs[0])
# The same relay under the other spelling of its principal.
relay = [e for e in conv.helm_events({"state": "working", "ask": {"kind": "none"}, "events": [
    {"ts": NOW - 50, "kind": "bot-ask", "source": "bot", "body": "ASKED YOU (choice): Prune or grow?\n- Grow the volume -- A short outage\n- Prune -- Deletes 180 GB"},
    {"ts": NOW - 40, "kind": "owner-reply", "source": "relay", "body": "grow"},
    {"ts": NOW - 39.999, "kind": "owner-note", "source": "relay", "body": "He said: the first one."}]}) if e["type"] == "message"]
check("a relay answer is authored 'Relay'", relay[0]["author"] == "Relay", relay[0])
check("... and reads 'Relay answered: <label>'", relay[0]["body"] == "Relay answered: Grow the volume", relay[0])
check("a relay note is authored 'Relay'", relay[1]["author"] == "Relay" and relay[1]["body"] == "He said: the first one.", relay[1])
check("the raw source stays `relay` on the event", relay[0]["source"] == "relay", relay[0])
for who in ("owner", "Owner", ""):
    mine = conv.helm_events({"state": "working", "events": [
        {"ts": NOW - 20, "kind": "owner-reply", "source": who, "body": "__task__"},
        {"ts": NOW - 19.999, "kind": "owner-note", "source": who, "body": "Do it."}]})
    check("source %r stays the owner's reply" % who, mine[0]["author"] == "You" and mine[0]["body"].startswith("You answered: "), mine[0])
    check("source %r stays the owner's note" % who, mine[1]["author"] == "You", mine[1])
sess = conv.helm_events({"state": "working", "events": [{"ts": NOW - 20, "kind": "owner-reply", "source": "session", "body": "__task__"}]})
check("a chat session's answer on his word keeps reading as his", sess[0]["author"] == "You", sess[0])

print("\nmerging a task's side")
thread = {"thread_id": "t-2", "state": "working", "events": [
    ev(1, "state-change", "claimed by the worker seat", "helm"),
    ev(2, "owner-note", "Keep the window short", "owner"),
]}
progress = [{"ts": NOW - 1000 + 5, "text": "Read 200 alerts; 61 are one monitor flapping."},
            {"ts": NOW - 1000 + 25, "text": "owner via Helm note: Keep the window short"},
            {"ts": NOW - 1000 + 25, "text": "Commit abc123: window 180 s."},
            {"ts": NOW - 1000 + 25, "text": "Commit abc123: window 180 s."}]
run = {"ts": NOW - 5, "pane_id": "sj-a1", "runner": "pi worker", "status": "working", "tool_calls": ["edit_file"], "output_tail": ["4 passed"]}
m = conv.merge(thread, progress, run)
kinds = [(e["type"], e.get("kind")) for e in m]
check("everything in time order", [e["ts"] for e in m] == sorted(e["ts"] for e in m), [e["ts"] for e in m])
check("the note and its progress mirror are one message", sum(1 for e in m if e["type"] == "message" and "window short" in e["body"]) == 1, kinds)
check("the Helm copy of the note is the one kept", any(e["id"] == "e2" for e in m))
check("identical progress lines in one second are one line", sum(1 for e in m if "Commit abc123" in e.get("body", "")) == 1)
check("progress lines are system lines", any(e["type"] == "system" and e["kind"] == "progress" and "flapping" in e["body"] for e in m))
check("the run block is last, with its tool calls and tail", m[-1]["type"] == "run" and m[-1]["tool_calls"] == ["edit_file"] and m[-1]["output_tail"] == ["4 passed"], m[-1])
check("ids are unique", len({e["id"] for e in m}) == len(m))
far = [{"ts": NOW - 1000 + 20 + 3600, "text": "owner via Helm note: Keep the window short"}]
check("the same words an hour later are a new message", sum(1 for e in conv.merge(thread, far) if "window short" in e["body"]) == 2)
check("extra events (control overlays) merge in by time", conv.merge(thread, extra=[{"id": "x1", "ts": NOW - 1000 + 15, "type": "system", "body": "Withdrawn"}])[1]["id"] == "x1")

print("\nthe payload")
p = conv.payload(lit)
check("it has the conversation's shape", all(k in p for k in ("thread_id", "task_id", "task_status", "thread_state", "events",
                                                                           "pending_notes", "ask", "review_state", "properties", "generated_at")), sorted(p))
check("thread_state and the live ask come from the thread", p["thread_state"] == "needs-you" and p["ask"] == ASK)
check("console says none merged it", p["console"] == "none")
check("no task: task fields are empty", p["task_id"] is None and p["task_status"] is None and p["properties"] == {})
p = conv.payload(dict(lit, state="working"))
check("a card that is not lit has no live ask", p["ask"] is None)
check("no key names the retired console", not [k for k in p if "agent" in k.lower()], sorted(p))
p = conv.payload(None, task={"id": "tasks-1", "status": "todo", "thread_id": "rk-tasks-1"}, progress=[{"ts": NOW, "text": "Queued."}])
check("a task with no Helm thread yet still has a conversation", p["thread_id"] == "rk-tasks-1" and p["task_status"] == "todo"
      and p["thread_state"] is None and len(p["events"]) == 1, p)

print("\nthe api route")
con = store.db(os.environ["HELM_DB"])
store.put_threads(con, {"threads": [{"thread_id": "t-conv", "title": "Disk at 94%", "summary": "The media box disk", "category": "Fleet",
                                     "urgency": "this-week", "state": "needs-you", "ask": ASK, "sources": [{"source": "test", "id": "1"}],
                                     "events": [{"ts": NOW - 60, "source": "sweep", "kind": "bot-result", "body": "Disk at 94%."}]}]})
con.close()
client = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
r = client.get("/api/triage/threads/t-conv/conversation", headers=AUTH)
j = r.json()
check("200 with the Helm side", r.status_code == 200 and j["thread_id"] == "t-conv" and j["console"] == "none", r.text[:200])
check("the report is in the timeline", any(e["type"] == "message" and e["kind"] == "report" and "94%" in e["body"] for e in j["events"]), j["events"])
check("the lit ask is the payload's, not a timeline row", j["ask"] and j["ask"]["prompt"] == "Prune or grow?"
      and not any(e["type"] == "ask" and "Prune or grow" in e["body"] for e in j["events"]))
client.post("/api/triage/threads/t-conv/reply", json={"value": "grow"}, headers=AUTH)
j = client.get("/api/triage/threads/t-conv/conversation", headers=AUTH).json()
check("after the answer: the state moved and the answer is the owner's message", j["thread_state"] != "needs-you" and j["ask"] is None
      and any(e["type"] == "message" and e["body"] == "You answered: Grow the volume" for e in j["events"]), (j["thread_state"], j["events"]))
r = client.get("/api/triage/threads/t-nope/conversation", headers=AUTH)
check("an unknown thread is 404", r.status_code == 404 and r.json()["ok"] is False)
r = client.get("/api/triage/threads/t-conv/conversation")
check("the route wants the bearer", r.status_code == 401, r.status_code)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
