"""The Helm adapter against a scratch thread store: list parsing, every
action's request shape, the actor stamp, and the store's rejections passed
through word for word as HelmError.

Ported from the reference suite's tests/test_helm_client.py (33 checks). the reference app tested its
client against a fake HTTP Helm; the adapter is in-process, so the fake Helm
is a fake `helm.actions.act` / `helm.actions.create` that records what the
adapter hands the store, and the rejections come from the real store. 24 of
the 33 port (same intent, in-process); the 9 that do not, and why:

  - "list sends the bearer", "no request carried the token in its path",
    "token comes from the environment in tests": there is no HTTP hop and no
    token; the API role's bearer gate covers the notebook routes.
  - "non-JSON body becomes the detail": there is no HTTP body to fail to parse.
  - "a slow reply that landed comes back accepted and marked slow", "a slow
    reply that did not land raises", "a different value on the thread is not
    this answer", "a read timeout is a 504, not a crash": the 45 s HTTP
    timeout and the landed() recovery read are gone with the hop; a store call
    either returns or raises.
  - "dead Helm is a 503 naming start.vbs": there is no separate Helm to be down.

Extra checks cover what replaced them: the actor stamp on a reply and a close
(home keeps the login, work stamps the owner), and threads_citing for `note:` links.
"""
# temp-ok: the DB under test is a fixture this module creates and deletes
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_adapter_")
DB = os.path.join(TMP, "triage.db")
os.environ["HELM_SITE"] = "work"
os.environ["HELM_NOTES_PROFILE"] = "fixed"
os.environ["HELM_NOTES_MODE"] = "store"
os.environ["HELM_NOTES_ROOT"] = os.path.join(TMP, "vault")
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = DB
os.environ["HELM_DATA"] = TMP

from helm import site  # noqa: E402
from notebook import helm_adapter  # noqa: E402

PASS = FAIL = 0
SEEN = []
CALLERS = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def fake_act(con, thread_id, action, body, actor=None):
    SEEN.append(("act", thread_id, action, dict(body), actor))
    return 202, {"accepted": True, "echo": dict(body)}


def fake_create(con, body, actor=None, caller=None):
    SEEN.append(("create", None, "new", dict(body), actor))
    CALLERS.append(caller)
    return 201, {"created": True, "thread_id": "owner-x", "state": "needs-you", "echo": dict(body)}


def run() -> int:
    real_act, real_create = helm_adapter._actions.act, helm_adapter._actions.create

    # Seed two cards through the real store, one of them into the backlog.
    a = helm_adapter.new_thread({"title": "a card", "summary": "s", "category": "Self-test", "urgency": "now"})
    b = helm_adapter.new_thread({"title": "a backlog card", "category": "Self-test", "urgency": "whenever"})
    helm_adapter.act(b["thread_id"], "backlog", {"reason": "not now, there is a line"})

    # List
    lst = helm_adapter.list_threads()
    ids = {t["thread_id"] for t in lst["threads"]}
    check("list asks for the backlog too", a["thread_id"] in ids and b["thread_id"] in ids, ids)
    check("list payload comes back whole", {"generated_at", "woke", "categories", "threads"} <= set(lst)
          and lst["categories"][0]["name"] == "Self-test" and len(lst["threads"]) == 2, list(lst))
    lst2 = helm_adapter.list_threads(include_backlog=False)
    check("list without backlog leaves the backlog card out",
          [t["thread_id"] for t in lst2["threads"]] == [a["thread_id"]], [t["thread_id"] for t in lst2["threads"]])

    # Every action: the store's action name, and exactly the keys Helm reads.
    helm_adapter._actions.act, helm_adapter._actions.create = fake_act, fake_create
    try:
        extra = {"value": "done", "note": "n", "reason": "r", "wake_at": 123.0, "source": "s",
                 "bogus": "dropped", "thread_id": "should-not-forward", "by": "session"}
        # An answer or close through the notebook is the owner's own on a second site.
        expect = {
            "reply": {"value": "done", "note": "n", "by": "owner"},
            "close": {"reason": "r", "by": "owner"},
            "reopen": {"note": "n"},
            "mute": {"note": "n"},
            "snooze": {"wake_at": 123.0, "note": "n"},
            "park": {"reason": "r", "source": "s"},
            "unpark": {"note": "n", "source": "s"},
            "backlog": {"reason": "r", "source": "s"},
            "unbacklog": {"note": "n", "reason": "r", "source": "s"},
        }
        check("the action table covers exactly these nine", set(helm_adapter.ACTIONS) == set(expect))
        for action, want in expect.items():
            r = helm_adapter.act("thread one/two", action, extra)
            kind, tid, act, body, actor = SEEN[-1]
            check("%s calls the store's action with the id as given" % action,
                  kind == "act" and tid == "thread one/two" and act == action, SEEN[-1])
            check("%s forwards only %s" % (action, sorted(want)), body == want, body)
            check("%s returns the store's answer" % action, r.get("accepted") is True and r["echo"] == want)
        r = helm_adapter.act("t1", "snooze", {"wake_at": 5})
        check("missing optional keys are simply absent", SEEN[-1][3] == {"wake_at": 5}, SEEN[-1][3])

        n = len(SEEN)
        try:
            helm_adapter.act("t1", "explode", {})
            check("unknown action raises", False)
        except helm_adapter.HelmError as e:
            check("unknown action is a 404 before any store call", e.status == 404 and "explode" in e.detail and len(SEEN) == n)

        # New thread: only the allowed keys reach the store.
        r = helm_adapter.new_thread({"title": "t", "summary": "s", "category": "c", "urgency": "now", "intent": "mine",
                                     "instruction": "", "links": [{"url": "note:x.md"}], "state": "done", "thread_id": "forged"})
        check("new thread calls the store's create", SEEN[-1][0] == "create", SEEN[-1])
        check("new thread drops state and thread_id", set(SEEN[-1][3]) == {"title", "summary", "category", "urgency", "intent", "instruction", "links"}, SEEN[-1][3])
        check("new thread returns the store's record", r["thread_id"] == "owner-x" and r["created"] is True)
        helm_adapter.new_thread({"title": "t"}, caller="some-watcher")
        check("new thread hands the caller on to the store's create", CALLERS[-1] == "some-watcher", CALLERS)
        helm_adapter.new_thread({"title": "t"})
        check("new thread with no caller passes none", CALLERS[-1] is None, CALLERS)

        # The actor: forwarded to the store as `actor`, stamped as `by` per site.
        helm_adapter.act("t1", "reply", {"value": "done"}, actor="owner")
        check("work stamps a notebook reply as the owner's own and still forwards the login as actor",
              SEEN[-1][3] == {"value": "done", "by": "owner"} and SEEN[-1][4] == "owner", SEEN[-1])
        os.environ["HELM_SITE"] = "home"
        site.reset()
        helm_adapter.act("t1", "close", {"reason": "r"}, actor="owner")
        check("home stamps a login's close as the owner's own too (a login is the owner; DECISIONS #12)",
              SEEN[-1][3] == {"reason": "r", "by": "owner"} and SEEN[-1][4] == "owner", SEEN[-1])
        helm_adapter.act("t1", "reply", {"value": "done"})
        check("no actor at home is the owner's own tap", SEEN[-1][3] == {"value": "done", "by": "owner"}, SEEN[-1])
        os.environ["HELM_SITE"] = "work"
        site.reset()
    finally:
        helm_adapter._actions.act, helm_adapter._actions.create = real_act, real_create

    # Rejections come back verbatim, status and detail, from the real store.
    try:
        helm_adapter.act(a["thread_id"], "snooze", {})
        check("400 raises", False)
    except helm_adapter.HelmError as e:
        check("400 keeps the store's status", e.status == 400)
        check("400 keeps the store's detail word for word", e.detail.startswith("'hours' is required"), e.detail)
        check("400 keeps the error payload", e.payload.get("ok") is False and e.payload.get("detail") == e.detail)
    try:
        helm_adapter.act("missing", "close", {})
        check("404 raises", False)
    except helm_adapter.HelmError as e:
        check("404 from the store passes through", e.status == 404 and "missing" in e.detail, (e.status, e.detail))
    try:
        helm_adapter.new_thread({"summary": "no title"})
        check("a rejected new thread raises", False)
    except helm_adapter.HelmError as e:
        check("a rejected new thread keeps the store's reason", e.status == 400 and "'title' is required" in e.detail, e.detail)

    # A real reply lands with the owner as the source, and the list shows the change.
    r = helm_adapter.act(a["thread_id"], "reply", {"value": "done"})
    check("a real reply is accepted and settles the one-option card", r.get("accepted") is True and r.get("state") == "done", r)
    hcon = helm_adapter.store()
    evs = helm_adapter._events_of(hcon, a["thread_id"])
    check("the reply event carries source owner and its reply_kind field",
          any(e["kind"] == "owner-reply" and e["source"] == "owner" and "reply_kind" in e for e in evs), evs)

    # Threads citing a note by link.
    c = helm_adapter.new_thread({"title": "about a page", "links": [{"url": "note:Docker/helm-gateway", "label": "the page"}]})
    citing = helm_adapter.threads_citing(hcon, "Docker/helm-gateway.md")
    check("threads_citing finds a note: link without the .md", [t["thread_id"] for t in citing] == [c["thread_id"]], citing)
    check("threads_citing finds nothing for another page", helm_adapter.threads_citing(hcon, "Docker/other.md") == [])
    check("store_threads_first_seen_between carries the first event's source and kind",
          all("first_source" in t and "first_kind" in t for t in helm_adapter.store_threads_first_seen_between(hcon, 0, 4e9)))
    check("search and switcher answer through the store",
          any(t["thread_id"] == c["thread_id"] for t in helm_adapter.search_threads(hcon, "about a page"))
          and any(t["thread_id"] == c["thread_id"] for t in helm_adapter.switcher_threads(hcon, "about")))
    hcon.close()

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
