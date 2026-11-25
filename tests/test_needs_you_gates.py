"""Store gates that were only tested through the retired dispatcher suites.

The dispatcher (dispatcher/tick.py and friends) was retired on 2026-09-14 and
removed from this repo on 2026-09-25, together with the suites that drove it
(test_ladder, test_p7_resilience, test_p5b_kanban, test_dispatcher,
test_p4_producers). Those suites also carried checks of STORE gates that no
other suite covers. They are kept here, unchanged in substance, and run
against the store and the API directly (TestClient, no loopback server, no
executor):

  1. the needs-you four-test (contract P3b §1): T1..T4 probes and controls,
     the producer's door, licensed resync, the self-check route
  2. bare asks (contract P3b §4): the banned phrase, statement-not-ask, the
     direction-demand rewrite, and a sweep over every stored ask
  3. the terminal no-action verdict at the store gate (contract P7 §2)
  4. one object model (contract P5b): a result or a claim write-back updates
     the originating thread and never adds one

One fix on the way over: the self-check route probe posted a one-option
choice, which the store has refused since the 2-option rule landed; it now
carries two options, so it tests the four-test and not the option count.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="helm-nygates-")
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
        print("  FAIL %s %s" % (name, str(detail)[:400]))


CLIENT = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}


def api(method, path, payload=None):
    r = CLIENT.request(method, path, json=payload, headers=AUTH)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"raw": r.text}
    return r.status_code, body


def put_thread(tid, title, ask_kind="choice", prompt="Pick one", options=None,
               requires=None):
    t = {"thread_id": tid, "title": title, "summary": "s " * 10,
         "category": "Test", "urgency": "this-week", "state": "needs-you",
         "sources": [{"source": "test", "id": tid, "note": ""}]}
    if ask_kind:
        ask = {"kind": ask_kind, "prompt": prompt}
        # P3b §1.5: helper-created choice/text asks carry the T1 marker by
        # default (explicit requires=[] keeps the markerless probe honest).
        ask["requires"] = ["decision"] if requires is None else requires
        if ask_kind == "choice":
            ask["options"] = options or [
                {"label": "Do the thing fully", "value": "do", "effect": "It gets done"},
                {"label": "Skip it for now", "value": "no", "effect": "Nothing happens"}]
        t["ask"] = ask
    return api("PUT", "/api/triage/threads", {"threads": [t]})


def thread_state(tid):
    code, th = api("GET", "/api/triage/threads?include_done=1")
    return next((t for t in th["threads"] if t["thread_id"] == tid), None)


def rejected_of(res):
    return " | ".join(" ".join(r["errors"]) for r in res.get("rejected", []))


LONG = "x" * 120

# ============================================================ 1. four-test
print("section 1: the needs-you four-test (contract P3b §1)")

# T1: no markers -> refused at the producer's door, named. Both shapes:
# requires: [] (stated, empty) and no requires key at all. The door's contract
# is per-thread rejection in rejected[]; a single-thread payload of one
# rejected thread reads 400 at the API.
code, res = put_thread("t4-one", "Relay certificate renewal", requires=[])
code2, res2 = api("PUT", "/api/triage/threads", {"threads": [{
    "thread_id": "t4-one-b", "title": "Relay certificate renewal unstated",
    "summary": "s " * 10, "category": "Test", "urgency": "this-week",
    "state": "needs-you",
    "sources": [{"source": "test", "id": "t4-one-b", "note": ""}],
    "ask": {"kind": "choice", "prompt": "Pick one", "options": [
        {"label": "Do the thing fully", "value": "do", "effect": "It gets done"},
        {"label": "Skip it for now", "value": "no", "effect": "Nothing happens"}]}}]})
errs = rejected_of(res) + rejected_of(res2)
check("T1 probe: choice ask with no markers is refused at the door",
      code == 400 and code2 == 400 and "T1-only-he-can" in errs,
      (code, code2, errs[:200]))
check("T1 probe: the refusal names the fix", "requires" in errs, errs[:200])
check("    and the thread never landed",
      thread_state("t4-one") is None and thread_state("t4-one-b") is None)

# T1 positive: markers land
code, res = put_thread("t4-ok", "Relay certificate renewal decision",
                       requires=["decision"])
check("T1 control: markers land", code == 200 and res.get("created") == 1,
      (code, res))

# T1 unknown marker -> validation 400 naming the set
code, res = put_thread("t4-bad", "Unknown marker probe", requires=["whim"])
check("T1 probe: unknown marker is a loud 400 naming the set",
      code == 400 and "decision" in rejected_of(res), (code, res))

# T1 exemption: ack asks carry no markers and land
code, res = put_thread("t4-ack", "An ack card for information only",
                       ask_kind="ack", prompt="Read this and close it.")
check("T1 control: producer ack asks are exempt (land without markers)",
      code == 200, (code, res))

# T2: an owner event newer than the ask's generation invalidates it
ok, failed = store.needs_owner(
    {"thread_id": "x", "title": "t", "ask": {"kind": "choice",
                                             "requires": ["decision"]}},
    {"ask_ts": 100.0, "owner_event_ts": 200.0, "duplicates": []})
check("T2 probe: newer owner event fails still-true-now",
      ok is False and failed == "T2-still-true-now", (ok, failed))
ok, failed = store.needs_owner(
    {"thread_id": "x", "title": "t", "ask": {"kind": "choice",
                                             "requires": ["decision"]}},
    {"ask_ts": 200.0, "owner_event_ts": 100.0, "duplicates": []})
check("T2 control: ask newer than his last event passes T2", ok is True)

# T3: duplicate open ask / recently-settled / ops-log hit
ok, failed = store.needs_owner(
    {"thread_id": "new", "title": "Disk on the backup host at 11 percent free",
     "ask": {"kind": "choice", "requires": ["decision"]}},
    {"ask_ts": 100.0, "owner_event_ts": 0, "duplicates": [
        {"thread_id": "old", "state": "needs-you", "ask_kind": "choice",
         "last_change": 50}]})
check("T3 probe: open duplicate ask fails he-doesnt-know",
      ok is False and failed == "T3-he-doesnt-know", (ok, failed))
ok, failed = store.needs_owner(
    {"thread_id": "new", "title": "Disk on the backup host at 11 percent free",
     "ask": {"kind": "choice", "requires": ["decision"]}},
    {"ask_ts": 100.0, "owner_event_ts": 0, "duplicates": [
        {"thread_id": "old", "state": "needs-you", "ask_kind": "choice",
         "title": "Disk on the backup host at 7 percent free", "last_change": 50}]})
check("T3 probe: fingerprint dedupe catches the retitled duplicate (numbers stripped)",
      ok is False and failed == "T3-he-doesnt-know", (ok, failed))
ok, failed = store.needs_owner(
    {"thread_id": "new", "title": "t", "ask": {"kind": "choice",
                                               "requires": ["decision"]}},
    {"ask_ts": 100.0, "owner_event_ts": 0, "duplicates": [],
     "ops_log_hits": ["Ops Log 2026-09-10: known"]})
check("T3 probe: an ops-log hit fails he-doesnt-know",
      ok is False and failed == "T3-he-doesnt-know", (ok, failed))
ok, failed = store.needs_owner(
    {"thread_id": "new", "title": "t", "ask": {"kind": "choice",
                                               "requires": ["decision"]}},
    {"ask_ts": 100.0, "owner_event_ts": 0, "duplicates": [
        {"thread_id": "old", "state": "working", "ask_kind": "none",
         "last_change": 50}]})
check("T3 control: a working non-ask thread is not a duplicate",
      ok is True, (ok, failed))

# T3 at the producer's door: a second card for the same concern
code, res = put_thread("t4-dupe", "Relay certificate renewal decision",
                       requires=["decision"])
check("T3 door probe: duplicate title refused at the producer's door",
      code == 400 and "T3-he-doesnt-know" in rejected_of(res), (code, res))
check("    and the refusal names the existing concern's route",
      "stable id" in rejected_of(res), rejected_of(res)[:200])

# T4: ladder context with budget left / self-fix paths
ok, failed = store.needs_owner(
    {"thread_id": "x", "title": "t", "ask": {"kind": "choice",
                                             "requires": ["decision"]}},
    {"ask_ts": 100.0, "owner_event_ts": 0, "duplicates": [],
     "ladder": {"retry_budget_left": 1, "self_fix_paths": []}})
check("T4 probe: retry budget left fails agent-cannot-fix",
      ok is False and failed == "T4-agent-cannot-fix", (ok, failed))
ok, failed = store.needs_owner(
    {"thread_id": "x", "title": "t", "ask": {"kind": "choice",
                                             "requires": ["decision"]}},
    {"ask_ts": 100.0, "owner_event_ts": 0, "duplicates": [],
     "ladder": {"retry_budget_left": 0, "self_fix_paths": ["other-executor"]}})
check("T4 probe: a self-fix path fails agent-cannot-fix",
      ok is False and failed == "T4-agent-cannot-fix", (ok, failed))
ok, failed = store.needs_owner(
    {"thread_id": "x", "title": "t", "ask": {"kind": "choice",
                                             "requires": ["decision"]}},
    {"ask_ts": 100.0, "owner_event_ts": 0, "duplicates": [],
     "ladder": {"retry_budget_left": 0, "self_fix_paths": []}})
check("T4 control: exhausted ladder with no self-fix passes", ok is True)

# licensed resync: the SAME thread id re-PUT with its ask lands (invariant 3
# still protects any answer); a NEW ask on an ask-less live thread is gated
code, res = put_thread("t4-ok", "Relay certificate renewal decision",
                       requires=["decision"])
check("licensed resync: same id + same concern lands, no re-interrogation",
      code == 200 and res.get("updated") == 1, (code, res))

# the self-check route (§1.4): the fix as code, callable by any producer.
# Two options: a one-option choice is refused by the option rule before the
# four-test is reached, which is not what these probes are about.
ROUTE_OPTIONS = [{"label": "Do the thing", "value": "do", "effect": "it happens"},
                 {"label": "Leave it as it is", "value": "leave",
                  "effect": "nothing changes"}]
code, res = api("POST", "/api/triage/needs-owner-check", {"thread": {
    "thread_id": "t4-route", "title": "A route check concern",
    "state": "needs-you",
    "ask": {"kind": "choice", "prompt": "Which way?",
            "requires": ["decision"], "options": ROUTE_OPTIONS}}})
check("self-check route: pass is data, not a page",
      code == 200 and res.get("needs_you") is True, (code, res))
code, res = api("POST", "/api/triage/needs-owner-check", {"thread": {
    "thread_id": "t4-route-nope", "title": "Another route check concern",
    "state": "needs-you",
    "ask": {"kind": "choice", "prompt": "Which way?", "options": ROUTE_OPTIONS}}})
check("self-check route: T1 failure comes back with the failed test",
      code == 200 and res.get("needs_you") is False
      and res.get("failed_test") == "T1-only-he-can", (code, res))
check("self-check route: read-only (neither probe thread landed)",
      thread_state("t4-route") is None and thread_state("t4-route-nope") is None)
# a card that fails the card-content rules is a refusal too: 200, the answer is data
code, res = api("POST", "/api/triage/needs-owner-check", {"thread": {
    "thread_id": "t4-route-junk", "title": "A junk route check concern",
    "state": "needs-you",
    "ask": {"kind": "choice", "prompt": "Which way?", "context": "x",
            "requires": ["decision"],
            "options": [{"label": "A", "value": "a", "effect": "it happens"},
                        {"label": "B", "value": "b", "effect": "nothing changes"}]}}})
check("self-check route: a card the validator refuses is 200 needs_you:false",
      code == 200 and res.get("needs_you") is False
      and res.get("failed_test") == "validation" and res.get("detail"), (code, res))
check("self-check route: validation refusal is read-only too",
      thread_state("t4-route-junk") is None)
code, res = api("POST", "/api/triage/needs-owner-check", {})
check("self-check route: a body with no thread is still a 400", code == 400, (code, res))
code, res = api("POST", "/api/triage/needs-owner-check",
                {"thread_id": "t4-route-bare", "title": "bare card"})
check("self-check route: a bare card (no thread key) is still a 400", code == 400, (code, res))

# ============================================================ 2. bare asks
print("section 2: bare cards never light 'what do you want' (contract P3b §4)")

put_thread("t-bare-1", "Bare ask target", ask_kind="ack",
           prompt="Read this and close it.")
api("POST", "/api/triage/threads/t-bare-1/reply", {"value": "ack"})

# R1 at the bot's door
code, res = api("POST", "/api/triage/threads/t-bare-1/result", {
    "outcome": "asked", "state": "needs-you", "note": LONG,
    "ask": {"kind": "ack", "prompt": "So, what do you want to do about this?"}})
check("R1 bot door: the banned phrase is a 400",
      code == 400 and "banned" in json.dumps(res).lower(), (code, res))
# R1 at the producer's door
code, res = put_thread("t-bare-2", "Producer banned phrase",
                       prompt="What do you want done here?")
check("R1 producer door: the banned phrase is a 400",
      code == 400 and "banned" in json.dumps(res).lower(), (code, res))
# R1 in an option effect
code, res = put_thread("t-bare-3", "Banned phrase in effect", options=[
    {"label": "Do the thing", "value": "do", "effect": "does what do you want"},
    {"label": "Leave it", "value": "no", "effect": "nothing"}])
check("R1 option effect: the banned phrase is a 400", code == 400, (code, res))

# R2 at the bot's door: a text ask with no '?' is a statement
code, res = api("POST", "/api/triage/threads/t-bare-1/result", {
    "outcome": "asked", "state": "needs-you", "note": LONG,
    "ask": {"kind": "text", "prompt": "The vendor replied with a new quote today."}})
check("R2 bot door: a text statement is refused with the ack fix",
      code == 400 and "ack" in json.dumps(res), (code, res))
# R2 at the producer's door
code, res = put_thread("t-bare-4", "Producer statement ask", ask_kind="text",
                       prompt="The migration finished overnight.")
check("R2 producer door: a text statement is refused", code == 400, (code, res))

# R3: the pure direction-demand is REWRITTEN at the bot's door
code, res = api("POST", "/api/triage/threads/t-bare-1/result", {
    "outcome": "blocked-external", "state": "needs-you", "note": LONG,
    "ask": {"kind": "text", "prompt": "How should we proceed?"}})
t = thread_state("t-bare-1")
check("R3 bot door: the demand is rewritten to the ack summary (202)",
      code == 202 and res.get("rewritten_ask") is True
      and t["ask"]["kind"] == "ack"
      and t["ask"]["prompt"].startswith("Work stopped at"),
      (code, res, t and t["ask"]["prompt"][:60]))
check("R3: the rewritten card says reply-anything reopens",
      "Reply anything to reopen" in t["ask"]["prompt"], t["ask"]["prompt"])
check("R3: the rewritten card carries no banned phrase",
      store.BANNED_ASK_PHRASE not in t["ask"]["prompt"].lower())
# R3 at the producer's door: refused with the fix (producers can correct)
code, res = put_thread("t-bare-5", "Producer direction demand", ask_kind="text",
                       prompt="What now?")
check("R3 producer door: the demand is refused with the fix",
      code == 400 and "demand for direction" in json.dumps(res), (code, res))
# a REAL question stands as text
code, res = api("POST", "/api/triage/threads/t-bare-1/result", {
    "outcome": "asked", "state": "needs-you", "note": LONG,
    "ask": {"kind": "text",
            "prompt": "Which certificate did you mean, the relay or the site one?"}})
check("R3 control: a real question stands as a text ask", code == 202,
      (code, res))

# the banned-phrase sweep over EVERY stored ask in this DB
swept = 0
code, th = api("GET", "/api/triage/threads?include_done=1&include_snoozed=1")
for t in th["threads"]:
    blob = (t["ask"]["prompt"] + " " + t["ask"].get("context", "") + " "
            + " ".join(o.get("label", "") + " " + o.get("effect", "")
                       for o in t["ask"]["options"])).lower()
    if store.BANNED_ASK_PHRASE in blob:
        swept += 1
check("banned-phrase sweep: no stored ask contains it", swept == 0, swept)

# ============================================== 3. no-action verdict (P7 §2)
print("section 3: the terminal no-action verdict at the store gate (contract P7 §2)")

NOTE80 = ("The decision was note-only: no thread action was owed, his point is "
          "recorded verbatim here, and nothing was dispatched or changed. "
          "Settling deliberately.")
_con = store.db(os.path.join(TMP, "store-gate.db"))


def _mk(tid, ask=None):
    t = {"thread_id": tid, "title": "concern %s" % tid, "summary": "s " * 10,
         "category": "Test", "urgency": "this-week", "state": "needs-you",
         "ask": ask or {"kind": "choice", "prompt": "Which way on this?",
                        "requires": ["decision"],
                        "options": [
             {"label": "Do the thing fully", "value": "do",
              "effect": "It gets done"},
             {"label": "Skip it for now", "value": "no",
              "effect": "Nothing happens"}]},
         "sources": [{"source": "test", "id": tid, "note": ""}]}
    store.put_threads(_con, {"threads": [t]})


def _refused(tid, body):
    try:
        store.post_result(_con, tid, body)
        return None
    except store.StoreError as e:
        return e.status


# A1. the verdict settles: note-only reply (binding note, NON_DECISION_REPLIES)
_mk("A1")
store.reply(_con, "A1", "note-only", note="just record that I wrote it myself")
out = store.post_result(_con, "A1", {"outcome": "note-only", "state": "done",
                                     "no_action": True, "note": NOTE80})
check("A1 no-action verdict settles done",
      out.get("accepted") and out.get("state") == "done", out)
check("A1 closed_by reports the mechanism",
      out.get("closed_by") == "no-action-settle", out)

# A2. no reply newer than the ask -> refused
_mk("A2")
st = _refused("A2", {"outcome": "note-only", "state": "done",
                     "no_action": True, "note": NOTE80})
check("A2 no-action with nothing of his to settle on -> 400", st == 400, st)

# A3. the flag is an assertion, not prose -> string refused
store.reply(_con, "A2", "note-only", note="record this, no action needed")
st = _refused("A2", {"outcome": "note-only", "state": "done",
                     "no_action": "true", "note": NOTE80})
check("A3 string no_action -> 400 (literal bool only)", st == 400, st)

# A4. the verdict that does not settle is ambiguous -> refused
st = _refused("A2", {"outcome": "note-only", "state": "needs-you",
                     "no_action": True, "note": NOTE80,
                     "ask": {"kind": "ack", "prompt": "Read and settle."}})
check("A4 no_action with state != done -> 400", st == 400, st)

# A5/A6. the answer gate is NOT exempted by the verdict
_mk("A5")
store.reply(_con, "A5", "note-only",
            note="record this please; also: which port did you pick?")
st = _refused("A5", {"outcome": "note-only", "state": "done",
                     "no_action": True, "note": NOTE80})
check("A5 unanswered question defeats the verdict -> 400", st == 400, st)
out = store.post_result(_con, "A5", {
    "outcome": "note-only", "state": "done", "no_action": True,
    "note": NOTE80, "answers": "Port 3038, the one the container publishes."})
check("A6 verdict + answers settles done",
      out.get("accepted") and out.get("state") == "done", out)

# A7. preserved: the SAME shape without the flag still hits the old refusal
_mk("A7")
store.reply(_con, "A7", "note-only", note="just record it, nothing to do")
st = _refused("A7", {"outcome": "note-only", "state": "done", "note": NOTE80})
check("A7 done without the verdict still 400s (bot-close unchanged)",
      st == 400, st)

# A8. preserved: the answered-decision close still works
_mk("A8")
store.reply(_con, "A8", "do")
out = store.post_result(_con, "A8", {"outcome": "done", "state": "done",
                                     "note": NOTE80, "answer_applied": True,
                                     "nothing_owed": True})
check("A8 answered-decision close still works",
      out.get("accepted") and out.get("closed_by") == "answered-decision", out)
_con.close()

# ============================================== 4. one object model (P5b)
print("section 4: a write-back updates the originating thread, never adds one (contract P5b)")

con = store.db(os.path.join(TMP, "object-model.db"))
tid = "t-p5b-worker-1"
res = store.put_threads(con, {"threads": [{
    "thread_id": tid, "title": "Agent task derivation test",
    "summary": "Carrier thread for sub-task orchestration and writeback test.",
    "category": "Agent platform", "urgency": "now", "state": "working",
    "ask": {"kind": "none", "prompt": "", "options": []},
    "sources": [{"source": "test", "id": "p5b-1"}]}]})
check("thread created in working state",
      res.get("created") == 1 or res.get("accepted") == 1, res)
initial = con.execute("SELECT count(*) FROM threads").fetchone()[0]
check("store has exactly 1 thread", initial == 1, initial)

res_post = store.post_result(con, tid, {
    "outcome": "done", "state": "needs-you",
    "note": ("Work sub-decomposition finished with full verification. Here is "
             "the verified report " + "x" * 50),
    "ask": {"kind": "choice", "requires": ["decision"],
            "prompt": "Decomposition complete: adopt changes or reject?",
            "options": [
                {"label": "Adopt changes", "value": "adopt", "effect": "Applies changes"},
                {"label": "Reject changes", "value": "reject", "effect": "Discards changes"}]}})
check("result posted directly to the thread", res_post.get("accepted") is True, res_post)
check("thread count unchanged after the result",
      con.execute("SELECT count(*) FROM threads").fetchone()[0] == initial)

reply_res = store.reply(con, tid, "adopt", note="Approved by the owner")
check("reply applied to the thread", reply_res.get("accepted") is True, reply_res)
check("thread count unchanged after the reply",
      con.execute("SELECT count(*) FROM threads").fetchone()[0] == initial)

res_close = store.post_result(con, tid, {
    "outcome": "settled", "state": "done", "answer_applied": True,
    "nothing_owed": True,
    "note": "Final settlement of thread verified and completed cleanly. " + "y" * 50})
check("thread settled to done", res_close.get("accepted") is True, res_close)
check("thread count preserved through the whole lifecycle",
      con.execute("SELECT count(*) FROM threads").fetchone()[0] == 1)
row = con.execute("SELECT state FROM threads WHERE thread_id=?", (tid,)).fetchone()
check("originating thread is done", row["state"] == "done", row["state"])
con.close()

# the same through the API, by way of a claim
before = len(api("GET", "/api/triage/threads?include_done=1")[1]["threads"])
tid = "t-p5b-api-1"
code, res = api("PUT", "/api/triage/threads", {"threads": [{
    "thread_id": tid, "title": "API claim writeback test",
    "summary": "Verifying that executor claim writeback mutates originating thread only.",
    "category": "Agent platform", "urgency": "now", "state": "working",
    "ask": {"kind": "none", "prompt": "", "options": []},
    "sources": [{"source": "test", "id": "p5b-api-1"}]}]})
check("API put thread", code == 200, (code, res))
code, res = api("POST", "/api/triage/threads/%s/claims" % tid, {
    "executor_id": "test-agent", "runtime": "agent",
    "caps": {"timeout_s": 300, "model": "test-model"}})
check("API claim created 201", code == 201, (code, res))
claim_id = res.get("claim_id", "")
code, res = api("POST", "/api/triage/threads/%s/claims/%s/result" % (tid, claim_id), {
    "outcome": "done", "state": "needs-you",
    "note": ("Worker execution finished with complete test artifacts verified. "
             + "w" * 50),
    "ask": {"kind": "choice", "requires": ["decision"],
            "prompt": "Worker card completed: proceed with merge?",
            "options": [
                {"label": "Proceed", "value": "proceed", "effect": "Merges changes"},
                {"label": "Hold", "value": "hold", "effect": "Holds changes"}]}})
check("API claim result written back (202)", code == 202, (code, res))
threads = api("GET", "/api/triage/threads?include_done=1")[1]["threads"]
check("API board gained exactly the one thread it was given",
      len(threads) == before + 1, (before, len(threads)))
mine = next((t for t in threads if t["thread_id"] == tid), None)
check("originating thread updated to needs-you",
      mine is not None and mine["state"] == "needs-you", mine and mine["state"])

# ============================================================ 5. the cold-read gate (T5)
print("section 5: the cold-read gate (T5) -- visible text names the step, and the step is his")

from helm import cold_read  # noqa: E402

# The producer's card shapes below are the two live cards of 2026-10-30/31 on thread
# owner-20261028-daily-security-brief-stopped-completing, which all four tests passed.


def put_card(tid, subject, recap, prompt, options, context, requires=("decision",)):
    t = {"thread_id": tid, "title": "Cold read probe " + tid, "summary": "s " * 10,
         "category": "Test", "urgency": "this-week", "state": "needs-you",
         "sources": [{"source": "test", "id": tid, "note": ""}],
         "ask": {"kind": "choice", "prompt": prompt, "options": options,
                 "subject": subject, "recap": recap, "context": context,
                 "requires": list(requires)}}
    return api("PUT", "/api/triage/threads", {"threads": [t]}), t


# (i) the 2026-10-31 three-failure template card: the question never names the step and
# carries the placeholder. Refused by the deterministic pre-check alone -- the model judge
# must not be called (a stub raises if it is).
TMPL_PROMPT = ("The producer's task 'Helm: Daily security brief stopped completing 10-25 and its "
               "failure monitor cannot fire' is waiting on something only you can do, and its "
               "check still says it is not done after 3 tries. Are you doing it, changing the "
               "plan, or cancelling the task?")
TMPL_OPTIONS = [
    {"label": "I'll do it now", "value": "do",
     "effect": "The producer keeps the task parked and checks again within the hour"},
    {"label": "Change the plan", "value": "replan",
     "effect": "The task goes back to its agent with your note on the card"},
    {"label": "Cancel this task", "value": "cancel",
     "effect": "The producer cancels this task and stops working on it"}]

_real_post_judge = cold_read._post_judge


def _no_model(payload):
    raise AssertionError("the model judge must not be called for this card")


try:
    cold_read._post_judge = _no_model
    cold_read._CACHE.clear()
    (code, res), t5t = put_card("t5-template", "Daily security brief stopped completing",
                                "The daily security brief is not completing; this card asks "
                                "the owner to commit to a step it never names.",
                                TMPL_PROMPT, TMPL_OPTIONS,
                                "The task is waiting for the 120-minute cap reaching the assistant "
                                "brief automation. A producer checked before each wake-up.")
    errs = rejected_of(res)
    check("T5 (i): the placeholder template card is refused at the door",
          code == 400 and "T5-cold-read" in errs, (code, errs[:300]))
    check("T5 (i): the refusal names the fix in plain words",
          "name the step" in errs, errs[:300])
    check("T5 (i): the card never landed", thread_state("t5-template") is None)

    # the self-check route answers the same refusal with the failed test named
    code, chk = api("POST", "/api/triage/needs-owner-check", {"thread": t5t})
    check("T5 (i): needs-owner-check refuses it with failed_test T5-cold-read",
          code == 200 and chk.get("needs_you") is False
          and chk.get("failed_test") == "T5-cold-read",
          (code, str(chk)[:300]))

    # (iv) a question cut mid-sentence is refused without a model call
    (code, res), _ = put_card("t5-trunc", "Nightly backup verification",
                              "The nightly backup check card arrived cut mid-sentence.",
                              "The nightly backup check needs you to confirm the run finished "
                              "and the log shows it ran past the",
                              [{"label": "I'll look now", "value": "look",
                                "effect": "You open the log and read the last lines"},
                               {"label": "Skip it", "value": "skip",
                                "effect": "Nothing happens tonight"}],
                              "The check waits on the log line.")
    errs = rejected_of(res)
    check("T5 (iv): a truncated question is refused without a model call",
          code == 400 and "T5-cold-read" in errs and "ends mid-sentence" in errs,
          (code, errs[:300]))

    # (iv-b) regression (found live 2026-10-31, three suites): the truncation
    # pre-check must refuse only real clips. The broad dangling-word list read the
    # letter "A" as the article "a", so every "Action A" / "Do A" label was refused
    # at the store door (test_snooze, test_store), and od-16's verdict option
    # "... and ask which" (test_one_decision) was refused as a clip.
    check("T5 (iv-b): a named label ending on a capital letter is not a clip",
          not cold_read._truncated("Action A") and not cold_read._truncated("Do A"))
    check("T5 (iv-b): an effect ending on a small word is not a clip",
          not cold_read._truncated("I reopen it and ask which"))
    check("T5 (iv-b): a sentence cut before its noun still is a clip",
          cold_read._truncated("the log shows it ran past the"))
    check("T5 (iv-b): the od-16 verdict card's visible text pre-checks clean",
          cold_read.precheck_errors(
              "Fix check", "Did the fixes land on your phone?",
              [{"label": "All 3 work", "value": "ok", "effect": "I close the thread"},
               {"label": "Some don't", "value": "some", "effect": "I reopen it and ask which"},
               {"label": "Haven't played it yet", "value": "later",
                "effect": "I leave it open"}]) == [])
finally:
    cold_read._post_judge = _real_post_judge
    cold_read._CACHE.clear()

# (ii) the 2026-10-30 card: the step IS named (one line into the assistant) but an
# agent can reach it. The judge says so; the refusal tells the producer to do it itself.
CAPPED_PROMPT = ("The brief's agent run needs more clock than its 75-minute cap. How should it "
               "rise to 120 minutes?")
CAPPED_OPTIONS = [
    {"label": "One line to the assistant (Recommended)", "value": "assistant raises it",
     "effect": "You tell the in-app agent to raise its own brief cap to 120 minutes; the next "
               "run gets the room it needs"},
    {"label": "Agent edits the assistant settings", "value": "agent edits file",
     "effect": "I edit the saved automation settings with a dated backup; the running app may "
               "write its own copy back over mine"},
    {"label": "Hold", "value": "hold",
     "effect": "The brief keeps publishing half-done pages and its heartbeat stays red"}]

_seen = {}


def _capped_judge(payload):
    _seen["payload"] = payload
    return {"named": True, "step": "type one line into the assistant chat",
            "only_owner_can": False}


try:
    cold_read._post_judge = _capped_judge
    cold_read._CACHE.clear()
    (code, res), _ = put_card("t5-capped", "Daily security brief time cap",
                              "The daily security brief gets killed at its fixed 75-minute cap "
                              "before its last steps run.",
                              CAPPED_PROMPT, CAPPED_OPTIONS,
                              "Raising the cap is a change inside the running assistant on the "
                              "desktop.")
    errs = rejected_of(res)
    check("T5 (ii): an agent-reachable step is refused",
          code == 400 and "T5-cold-read" in errs and "an agent can reach this step" in errs,
          (code, errs[:300]))
    sent = json.dumps(_seen.get("payload") or {})
    check("T5 (ii): the judge saw the question and the options, never the context",
          CAPPED_PROMPT[:40] in sent and "One line to the assistant" in sent
          and "the assistant on the desktop" not in sent, sent[:300])
finally:
    cold_read._post_judge = _real_post_judge
    cold_read._CACHE.clear()

# (iii) a card whose step only the owner can do (approve a recurring charge) passes, and still
# when the judge is unreachable (fail-open).
FUNDS_PROMPT = ("The off-site backup plan renews at $14 a month on your card. Approve the "
                "renewal, or drop to the 200 GB plan?")
FUNDS_OPTIONS = [
    {"label": "Renew at $14 a month (Recommended)", "value": "renew",
     "effect": "The plan renews and the backups keep their size"},
    {"label": "Drop to the 200 GB plan", "value": "downgrade",
     "effect": "You pay less; backups older than the quota are pruned"}]


def _funds_judge(payload):
    return {"named": True, "step": "approve the $14 a month backup renewal",
            "only_owner_can": True}


try:
    cold_read._post_judge = _funds_judge
    cold_read._CACHE.clear()
    (code, res), t5m = put_card("t5-funds", "Backup plan renewal",
                                "The home backup plan renews every month on the owner's card; this "
                                "card asks him to approve the spend.",
                                FUNDS_PROMPT, FUNDS_OPTIONS,
                                "The plan is on his personal card and only he can approve "
                                "spending.")
    check("T5 (iii): a real only-his card (funds) passes",
          code == 200 and not res.get("rejected"), (code, str(res)[:300]))

    cold_read._post_judge = lambda payload: None   # judge unreachable
    cold_read._CACHE.clear()
    code, chk = api("POST", "/api/triage/needs-owner-check", {"thread": t5m})
    check("T5 (iii): with the judge unreachable the card still passes (fail-open)",
          code == 200 and chk.get("needs_you") is True, (code, str(chk)[:300]))
finally:
    cold_read._post_judge = _real_post_judge
    cold_read._CACHE.clear()

# (v) a judge that times out fails open and logs one line
_fails_before = cold_read.JUDGE_FAILURES


def _timeout_judge(payload):
    raise TimeoutError("judge timed out")


try:
    cold_read._post_judge = _timeout_judge
    cold_read._CACHE.clear()
    (code, res), _ = put_card("t5-open", "Backup plan renewal (outage)",
                              "The home backup plan renews every month on the owner's card; this "
                              "card asks him to approve the spend.",
                              FUNDS_PROMPT, FUNDS_OPTIONS,
                              "The plan is on his personal card and only he can approve "
                              "spending.")
    check("T5 (v): a model timeout fails open",
          code == 200 and not res.get("rejected"), (code, str(res)[:300]))
    check("T5 (v): the timeout is counted and logged",
          cold_read.JUDGE_FAILURES > _fails_before, cold_read.JUDGE_FAILURES)
finally:
    cold_read._post_judge = _real_post_judge
    cold_read._CACHE.clear()


# ============================================================ wrap
print("%d passed, %d failed" % (PASSED, FAILED))
sys.exit(1 if FAILED else 0)
