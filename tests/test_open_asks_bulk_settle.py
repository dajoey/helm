"""A compact read of the open asks, and bulk settle, for chat sessions (P5, P6, P2 paging).

Run: python3 tests/test_open_asks_bulk_settle.py

GET /api/triage/open-asks?limit=&cursor= pages the open cards that ask the owner something (lit:
needs-you with an ask, not snoozed, muted or retired): thread id, title, the ask's kind, prompt and
options (label, value, effect), urgency, category, age and idle days. `?view=stale&days=` pages the
backlog cards idle more than `days` (default 30): id, title, category, idle days. Both read only;
the full list route is unchanged.

POST /api/triage/bulk-settle {items: [{thread_id, value | note, reason}]} runs each item through
the single-thread reply door (value: the answer; note alone: a note-only answer; reason rides in the
note), with the same gates and the same events, and answers every item: ok, or refused with the
reason. An item with no value and no note is refused, never closed or skipped.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="helm-open-asks-")
DB = os.path.join(TMP, "t.db")
os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_DB"] = DB
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ["HELM_DISPATCHER_HB_FILE"] = os.path.join(TMP, "hb.json")
os.environ["HELM_SITE"] = "work"
os.environ.pop("HELM_MODULES", None)
os.environ.pop("HELM_SENTRY_DSN", None)
os.environ.pop("HELM_CARD_RULES", None)

from starlette.testclient import TestClient  # noqa: E402

import app.service as svc  # noqa: E402
import app.web as web  # noqa: E402
from helm import site, store  # noqa: E402

PASS = FAIL = 0
CLIENT = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
ASKS = "/api/triage/open-asks"
BULK = "/api/triage/bulk-settle"
DAY = 86400.0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:600] if detail != "" else ""))


def use_site(name):
    os.environ["HELM_SITE"] = name
    site.reset()


def raw():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def sql(q, args=()):
    con = raw()
    try:
        cur = con.execute(q, args)
        con.commit()
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


CHOICE = {"kind": "choice", "prompt": "Which paint goes on the shed?",
          "context": "The shed needs one coat before the frost; both colours are in stock.",
          "requires": ["decision"],
          "options": [{"label": "Green paint (Recommended)", "value": "green", "effect": "Green is ordered.",
                       "terminal": True},
                      {"label": "Blue paint", "value": "blue", "effect": "Blue is ordered."}]}


def put(tid, ask=None, state="needs-you", urgency="whenever", category="Garden", title=None, **extra):
    t = {"thread_id": tid, "title": title or ("Card " + tid), "summary": "s", "category": category,
         "urgency": urgency, "state": state, "ask": ask if ask is not None else json.loads(json.dumps(CHOICE)),
         "sources": [{"source": "probe", "id": tid}]}
    t.update(extra)
    r = CLIENT.put("/api/triage/threads", json={"threads": [t]}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r


def age(tid, first_seen_days, idle_days):
    now = time.time()
    sql("UPDATE threads SET first_seen=?, last_change=? WHERE thread_id=?",
        (now - first_seen_days * DAY, now - idle_days * DAY, tid))


def page(params=""):
    r = CLIENT.get(ASKS + params, headers=AUTH)
    return r.status_code, r.json()


def walk(params, limit):
    """Every row of a view, page by page. Returns (ids in order, pages, last body)."""
    ids, pages, cursor, body = [], 0, None, {}
    while True:
        q = params + ("&" if params else "?") + "limit=%d" % limit
        if cursor:
            q += "&cursor=" + cursor
        code, body = page(q)
        if code != 200:
            return ids, pages, body
        pages += 1
        ids += [t["thread_id"] for t in body["threads"]]
        cursor = body.get("next_cursor")
        if not cursor or pages > 50:
            return ids, pages, body


def events(tid):
    t = CLIENT.get("/api/triage/threads/" + tid, headers=AUTH).json()
    return [(e["kind"], e["source"], e.get("source_id", ""), e["body"], e.get("reply_kind", ""))
            for e in t["events"]], t["state"]


def bulk(items, **top):
    body = dict(top, items=items)
    r = CLIENT.post(BULK, json=body, headers=AUTH)
    return r.status_code, r.json()


def run():
    print("\n1. open-asks lists only the lit cards that ask him something")
    put("a-now", urgency="now")
    put("a-week", urgency="this-week")
    put("a-old", urgency="whenever")
    put("a-new", urgency="whenever")
    put("a-ack", ask={"kind": "ack", "prompt": "Read the paint order?"}, urgency="whenever")
    put("a-text", ask={"kind": "text", "prompt": "What colour is the gate?", "requires": ["decision"],
                       "context": "The gate gets the same coat as the shed."}, urgency="whenever")
    age("a-old", 40, 35)
    age("a-new", 2, 1)
    age("a-ack", 10, 10)
    age("a-text", 5, 5)
    put("x-wait", ask={"kind": "none"}, state="waiting-external", blocked_on="the vendor's paint order")
    put("x-done", ask={"kind": "none"}, state="done")
    put("x-snoozed")
    CLIENT.post("/api/triage/threads/x-snoozed/snooze", json={"hours": 24}, headers=AUTH)
    put("x-muted")
    sql("UPDATE threads SET suppressed=1 WHERE thread_id='x-muted'")
    put("x-retired")
    sql("UPDATE threads SET tombstoned=1 WHERE thread_id='x-retired'")
    put("x-backlog")
    CLIENT.post("/api/triage/threads/x-backlog/backlog", json={"reason": "not now, the frost is weeks off"},
                headers=AUTH)
    code, body = page()
    check("200", code == 200, body)
    got = [t["thread_id"] for t in body.get("threads") or []]
    check("only the six lit asks", sorted(got) == sorted(["a-now", "a-week", "a-old", "a-new", "a-ack", "a-text"]),
          got)
    check("ordered by urgency, then oldest first", got == ["a-now", "a-week", "a-old", "a-ack", "a-text", "a-new"], got)
    check("total and count", body.get("total") == 6 and body.get("count") == 6 and body.get("next_cursor") is None,
          {k: body.get(k) for k in ("total", "count", "next_cursor")})
    row = next((t for t in body.get("threads") or [] if t["thread_id"] == "a-old"), {})
    check("a row's fields", set(row) == {"thread_id", "title", "kind", "prompt", "options", "urgency", "category",
                                         "age_days", "idle_days"}, sorted(row))
    check("options are label, value and effect only", row.get("options") == [
        {"label": "Green paint (Recommended)", "value": "green", "effect": "Green is ordered."},
        {"label": "Blue paint", "value": "blue", "effect": "Blue is ordered."}], row.get("options"))
    check("age and idle in days", abs(row.get("age_days", 0) - 40) < 0.2 and abs(row.get("idle_days", 0) - 35) < 0.2, row)
    ack = next((t for t in body.get("threads") or [] if t["thread_id"] == "a-ack"), {})
    check("an ack row carries its kind and no options", ack.get("kind") == "ack" and ack.get("options") == [], ack)
    check("the asks view says how many backlog cards are stale", body.get("stale_total") == 0, body.get("stale_total"))

    print("\n2. paging: limit + cursor walks every row once")
    ids, pages, last = walk("", 2)
    check("three pages of 2, 2 and 2", pages == 3 and len(ids) == 6, (pages, ids, last))
    check("no row twice, none missing, the same order", ids == got, ids)
    code, b1 = page("?limit=2")
    sql("UPDATE threads SET state='done', ask_kind='none' WHERE thread_id=?", (b1["threads"][0]["thread_id"],))
    code, b2 = page("?limit=2&cursor=" + b1["next_cursor"])
    check("a card settled between pages costs no other row",
          [t["thread_id"] for t in b2["threads"]] == got[2:4], [t["thread_id"] for t in b2["threads"]])
    sql("UPDATE threads SET state='needs-you', ask_kind='choice' WHERE thread_id=?", (b1["threads"][0]["thread_id"],))
    check("limit is capped", page("?limit=100000")[1].get("limit") == store.OPEN_ASKS_LIMIT_MAX, page("?limit=100000")[1])
    for bad in ("?limit=abc", "?limit=0", "?cursor=not-a-cursor", "?view=nope", "?view=stale&days=0",
                "?view=stale&days=x"):
        code, b = page(bad)
        check("a bad query is a 400 (%s)" % bad, code == 400 and b.get("ok") is False, (code, b))
    stale_cursor = base64.urlsafe_b64encode(json.dumps(["stale", 0, "a"]).encode()).decode()
    check("a cursor from the other view is a 400", page("?cursor=" + stale_cursor)[0] == 400)

    print("\n3. a page stays small: 400 cards with full-length text")
    for chunk in range(4):
        batch = [{"thread_id": "big-%03d" % i, "title": "T" * 296 + "%04d" % i, "summary": "s", "category": "Garden",
                  "urgency": "whenever", "state": "needs-you", "sources": [{"source": "probe", "id": "big-%d" % i}],
                  "ask": {"kind": "choice", "prompt": ("Which paint goes on shed %d? " % i).ljust(1000, "x"),
                          "requires": ["decision"], "context": "C" * 1400,
                          "options": [{"label": "L" * 46 + " (Recommended)", "value": "v%d" % i, "effect": "E" * 200},
                                      {"label": "M" * 60, "value": "w%d" % i, "effect": "F" * 200}]}}
                 for i in range(chunk * 100, chunk * 100 + 100)]
        r = CLIENT.put("/api/triage/threads", json={"threads": batch}, headers=AUTH)
        assert r.status_code == 200 and r.json().get("created") == 100, r.text[:300]
    r = CLIENT.get(ASKS, headers=AUTH)
    check("the default page is well under 1 MB", len(r.content) < 200_000, len(r.content))
    r = CLIENT.get(ASKS + "?limit=%d" % store.OPEN_ASKS_LIMIT_MAX, headers=AUTH)
    check("the largest page is under 1 MB", len(r.content) < 1_000_000, len(r.content))
    full = CLIENT.get("/api/triage/threads", headers=AUTH)
    fb = full.json()
    check("the full list route is unchanged: same keys, every lit card, context included",
          set(fb) == {"generated_at", "woke", "categories", "threads"} and len(fb["threads"]) >= 406
          and any(t["ask"].get("context") for t in fb["threads"]), (sorted(fb), len(fb["threads"])))
    sql("UPDATE threads SET tombstoned=1 WHERE thread_id LIKE 'big-%'")

    print("\n4. the stale view: backlog idle more than 30 days, read only")
    for tid, idle in (("s-31", 31), ("s-45", 45), ("s-29", 29), ("s-90", 90)):
        put(tid)
        CLIENT.post("/api/triage/threads/%s/backlog" % tid, json={"reason": "not now, the frost is weeks off"},
                    headers=AUTH)
        age(tid, idle + 5, idle)
    sql("UPDATE threads SET last_change=? WHERE thread_id='x-backlog'", (time.time() - 10 * DAY,))
    before = sql("SELECT thread_id, state, last_change, ask_kind FROM threads ORDER BY thread_id")
    n_events = sql("SELECT COUNT(*) n FROM events")[0]["n"]
    code, body = page("?view=stale")
    check("200", code == 200, body)
    got = [t["thread_id"] for t in body.get("threads") or []]
    check("only backlog idle more than 30 days, idlest first", got == ["s-90", "s-45", "s-31"], got)
    row = (body.get("threads") or [{}])[0]
    check("a stale row's fields", set(row) == {"thread_id", "title", "category", "idle_days"}, sorted(row))
    check("idle days", abs(row.get("idle_days", 0) - 90) < 0.2, row)
    check("the view and the cut-off are named", body.get("view") == "stale" and body.get("days") == 30, body)
    code, b10 = page("?view=stale&days=9")
    check("days=9 brings in a backlog card idle 10 days",
          [t["thread_id"] for t in b10["threads"]] == ["s-90", "s-45", "s-31", "s-29", "x-backlog"],
          [t["thread_id"] for t in b10["threads"]])
    ids, pages, _ = walk("?view=stale&days=9", 2)
    check("stale pages walk every row once", ids == ["s-90", "s-45", "s-31", "s-29", "x-backlog"] and pages == 3,
          (ids, pages))
    check("reading changed no thread and wrote no event",
          sql("SELECT thread_id, state, last_change, ask_kind FROM threads ORDER BY thread_id") == before
          and sql("SELECT COUNT(*) n FROM events")[0]["n"] == n_events)
    check("the asks view counts the stale backlog", page()[1].get("stale_total") == 3, page()[1].get("stale_total"))

    print("\n5. bulk settle: each item through the single door, same events")
    # (name, bulk item, the body the single reply door gets for the same answer)
    pairs = [("value, terminal", {"value": "green", "note": "the owner in chat: green"},
              {"value": "green", "note": "the owner in chat: green"}),
             ("value, not terminal", {"value": "blue"}, {"value": "blue", "note": ""}),
             ("value with a reason", {"value": "blue", "reason": "the owner in chat picked blue"},
              {"value": "blue", "note": "the owner in chat picked blue"}),
             ("value, note and reason", {"value": "blue", "note": "Use the leftover tin first",
                                         "reason": "the owner in chat picked blue"},
              {"value": "blue", "note": "Use the leftover tin first\n\nthe owner in chat picked blue"}),
             ("note only", {"note": "Use the leftover tin first"},
              {"value": "note-only", "note": "Use the leftover tin first"}),
             ("note and reason", {"note": "Use the leftover tin first", "reason": "the owner in chat"},
              {"value": "note-only", "note": "Use the leftover tin first\n\nthe owner in chat"})]
    for i, (name, item, single_body) in enumerate(pairs):
        one, two = "single-%s" % "abcdef"[i], "bulk-%s" % "abcdef"[i]
        door = "reply"
        put(one)
        put(two)
        n1, n2 = len(events(one)[0]), len(events(two)[0])
        r1 = CLIENT.post("/api/triage/threads/%s/%s" % (one, door), json=single_body, headers=AUTH)
        code, res = bulk([dict(item, thread_id=two)])
        item_res = (res.get("results") or [{}])[0]
        check("%s: single 202, bulk ok" % name, r1.status_code == 202 and code == 200 and item_res.get("ok") is True
              and item_res.get("status") == 202 and item_res.get("action") == door, (r1.status_code, code, res))
        e1, s1 = events(one)
        e2, s2 = events(two)
        e1, e2 = e1[n1:], e2[n2:]
        check("%s: the settle wrote events" % name, len(e1) >= 1, e1)
        check("%s: the same events and the same state" % name, e1 == e2 and s1 == s2, (e1[-3:], e2[-3:], s1, s2))
        check("%s: the same store answer" % name,
              {k: v for k, v in r1.json().items() if k != "thread_id"}
              == {k: v for k, v in (item_res.get("result") or {}).items() if k != "thread_id"},
              (r1.json(), item_res.get("result")))

    print("\n6. bulk settle: per-item refusal, never a silent skip")
    put("ok-one")
    put("ok-two")
    put("ok-three")
    n_ok3 = len(events("ok-three")[0])
    code, res = bulk([{"thread_id": "ok-one", "value": "green"},
                      {"thread_id": "no-such-thread-zz", "value": "green"},
                      {"thread_id": "ok-three", "reason": "the owner in chat: drop it"},
                      {"thread_id": "ok-two", "value": "green", "reason": "the owner in chat: green"}])
    rs = res.get("results") or []
    check("200 with one result per item, in order", code == 200 and [r.get("thread_id") for r in rs]
          == ["ok-one", "no-such-thread-zz", "ok-three", "ok-two"], (code, res))
    check("the unknown thread is refused with 404 and the reason", len(rs) == 4 and rs[1].get("ok") is False
          and rs[1].get("status") == 404 and "no such thread" in (rs[1].get("reason") or ""), rs[1:2])
    check("a reason with no value or note is refused, and the card is untouched",
          len(rs) == 4 and rs[2].get("ok") is False and rs[2].get("status") == 400
          and "value or note" in (rs[2].get("reason") or "") and events("ok-three")[1] == "needs-you"
          and len(events("ok-three")[0]) == n_ok3, (rs[2:3], events("ok-three")[1]))
    check("the items beside them still ran", len(rs) == 4 and rs[0].get("ok") and rs[3].get("ok")
          and events("ok-one")[1] == "done" and events("ok-two")[1] == "done", rs)
    check("the totals", res.get("ok") is False and res.get("count") == 4 and res.get("settled") == 2
          and res.get("refused") == 2, res)
    put("dup-1")
    code, res = bulk([
        "not an object",
        {"value": "green"},
        {"thread_id": "dup-1"},
        {"thread_id": "dup-1", "reason": "b"},
        {"thread_id": "dup-1", "note": 5},
        {"thread_id": "dup-1", "value": "x", "reason": 5},
        {"thread_id": "dup-1", "value": "blue", "note": "a", "reason": "b"},
        {"thread_id": "dup-1", "value": "green"},
    ])
    rs = res.get("results") or []
    check("every malformed item is refused by name", [r.get("ok") for r in rs]
          == [False, False, False, False, False, False, True, False], rs)
    notes = [e[3] for e in events("dup-1")[0] if e[0] == "owner-note"]
    check("the literal shape {thread_id, value, note, reason} answers with the note and the reason",
          notes[-1:] == ["a\n\nb"] and events("dup-1")[1] == "working", (notes, events("dup-1")[1]))
    check("each refusal says why", all(r.get("reason") for r in rs if not r.get("ok")), rs)
    check("a thread named twice in one request is refused the second time",
          rs and "already" in (rs[-1].get("reason") or ""), rs[-1:] if rs else rs)

    print("\n7. bulk settle: the same answer, the same record")
    draft_note = ("Drafted the reply to the paint shop covering the six colour questions. It was never "
                  "sent. Full text below.")
    for tid in ("gate-single", "gate-bulk"):
        put(tid, ask={"kind": "ack", "prompt": "Read?"})
        CLIENT.post("/api/triage/threads/%s/result" % tid, headers=AUTH, json={
            "outcome": "drafted", "state": "needs-you", "note": draft_note, "ask": json.loads(json.dumps(CHOICE))})
    r1 = CLIENT.post("/api/triage/threads/gate-single/reply", json={"value": "__use_draft__:abc"}, headers=AUTH)
    code, res = bulk([{"thread_id": "gate-bulk", "value": "__use_draft__:abc"}])
    rs = (res.get("results") or [{}])[0]
    check("a draft press from a bearer: 403 at both doors", r1.status_code == 403 and rs.get("status") == 403
          and rs.get("reason") == r1.json().get("detail"), (r1.json(), rs))
    put("by-top")
    put("by-none")
    code, res = bulk([{"thread_id": "by-top", "value": "green"}], by="owner")
    code, res2 = bulk([{"thread_id": "by-none", "value": "green"}])
    check("a top-level by applies to every item",
          events("by-top")[0][-1][3] == "settled by choosing 'Green paint (Recommended)'", events("by-top")[0][-1])

    print("\n8. the request itself")
    for name, payload in (("no items", {}), ("items not a list", {"items": {"a": 1}}), ("an empty list", {"items": []}),
                          ("too many items", {"items": [{"thread_id": "x", "value": "v"}] * (store.BULK_SETTLE_MAX + 1)})):
        r = CLIENT.post(BULK, json=payload, headers=AUTH)
        check("%s: 400" % name, r.status_code == 400 and r.json().get("ok") is False, (r.status_code, r.text[:200]))
    check("no token: 401", CLIENT.post(BULK, json={"items": [{"thread_id": "x", "value": "v"}]}).status_code == 401)
    check("no token: open-asks 401", CLIENT.get(ASKS).status_code == 401)
    check("the web role proxies neither route", not web.ALLOW_POST.match(BULK) and not web.ALLOW_GET.match(ASKS))

    print("\n9. at home too")
    use_site("home")
    try:
        for tid in ("h-single", "h-bulk"):
            put(tid, ask={"kind": "choice", "prompt": "Which paint goes on the shed?", "requires": "decision",
                          "subject": "Shed paint", "recap": "The shed needs a colour. Only you can pick it.",
                          "context": CHOICE["context"], "options": CHOICE["options"]})
        code, body = page()
        check("home: open-asks lists the lit cards", code == 200 and {"h-single", "h-bulk"}
              <= {t["thread_id"] for t in body.get("threads") or []}, body)
        n1, n2 = len(events("h-single")[0]), len(events("h-bulk")[0])
        r1 = CLIENT.post("/api/triage/threads/h-single/reply", json={"value": "green"}, headers=AUTH)
        code, res = bulk([{"thread_id": "h-bulk", "value": "green"}])
        e1, e2 = events("h-single"), events("h-bulk")
        check("home: bulk and single give the same events",
              r1.status_code == 202 and (res.get("results") or [{}])[0].get("ok") is True
              and e1[0][n1:] == e2[0][n2:] and e1[1] == e2[1] and len(e1[0]) > n1, (r1.json(), res, e1, e2))
    finally:
        use_site("home",)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
