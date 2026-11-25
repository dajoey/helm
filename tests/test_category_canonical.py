"""One spelling per category (P7).

Run: python3 tests/test_category_canonical.py

A category that differs from an existing one only by case or whitespace is stored as the existing
canonical spelling: the most-used one (threads carrying it), ties going to the oldest. Every door
that sets a category does this: PUT /api/triage/threads, POST /threads/create (and /new), and
POST /resort. Any other difference ("Gardens" beside "Garden") is a different category.

POST /api/triage/categories/merge-case merges the case-only duplicates already stored: a dry run
(the default) lists each group and its counts and writes nothing; `{"apply": true}` first writes a
JSON record of the old->new mapping and the affected thread ids beside the database, then rewrites.
Bearer only; the web role does not proxy it.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="helm-category-canon-")
DB = os.path.join(TMP, "t.db")
os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_DB"] = DB
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ["HELM_DISPATCHER_HB_FILE"] = os.path.join(TMP, "hb.json")
os.environ["HELM_SITE"] = "home"
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
MERGE = "/api/triage/categories/merge-case"
RECORD_DIR = os.path.join(TMP, "category-merges")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:600] if detail != "" else ""))


def raw():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def seed(tid, category, first_seen):
    """A thread written straight into the table, the way a database from before this change holds
    its spellings (no door canonicalised them)."""
    con = store.db(DB)
    try:
        con.execute("INSERT INTO threads (thread_id, title, summary, category, urgency, state, ask_kind,"
                    " first_seen, last_change) VALUES (?,?,?,?,?,?,?,?,?)",
                    (tid, "Seeded " + tid, "", category, "whenever", "waiting-external", "none",
                     first_seen, first_seen))
        con.execute("INSERT INTO categories (name, first_seen, last_seen) VALUES (?,?,?)"
                    " ON CONFLICT(name) DO NOTHING", (category, first_seen, first_seen))
        con.commit()
    finally:
        con.close()


def category_of(tid):
    con = raw()
    try:
        r = con.execute("SELECT category FROM threads WHERE thread_id=?", (tid,)).fetchone()
        return r["category"] if r else None
    finally:
        con.close()


def table_names():
    con = raw()
    try:
        return sorted(r["name"] for r in con.execute("SELECT name FROM categories"))
    finally:
        con.close()


def thread_spellings():
    con = raw()
    try:
        return {r["category"]: r["n"] for r in con.execute(
            "SELECT category, COUNT(*) n FROM threads GROUP BY category")}
    finally:
        con.close()


def put(tid, category, **kw):
    t = {"thread_id": tid, "title": kw.get("title", "Card " + tid), "summary": "s", "category": category,
         "urgency": "whenever", "state": "waiting-external", "ask": {"kind": "none"},
         "blocked_on": "the paint shop's reply", "recheck_at": time.time() + 86400,
         "sources": [{"source": "probe", "id": tid}]}
    return CLIENT.put("/api/triage/threads", json={"threads": [t]}, headers=AUTH)


def create(category, title):
    r = CLIENT.post("/api/triage/threads/create", headers=AUTH,
                    json={"title": title, "category": category, "intent": "mine"})
    return r.status_code, r.json()


def run():
    old = time.time() - 30 * 86400
    print("\n1. the most-used spelling wins")
    for i in range(3):
        seed("g-big-%d" % i, "Garden", old + 100 + i)
    seed("g-small", "garden", old)          # older, but used once
    r = put("p-garden", "GARDEN")
    check("PUT accepted", r.status_code == 200 and r.json().get("created") == 1, r.text[:300])
    check("PUT 'GARDEN' is stored as 'Garden' (3 uses beat 1 older use)", category_of("p-garden") == "Garden",
          category_of("p-garden"))
    check("store.canonical_category agrees", store.canonical_category(store.db(DB), "gArDeN") == "Garden")

    print("\n2. a tie goes to the oldest spelling")
    seed("pl-new", "plumbing", old + 500)
    seed("pl-old", "Plumbing", old + 10)
    r = put("p-plumb", "PLUMBING")
    check("PUT 'PLUMBING' is stored as 'Plumbing' (one use each, older wins)", category_of("p-plumb") == "Plumbing",
          category_of("p-plumb"))

    print("\n3. whitespace-only differences merge; any other difference does not")
    put("p-ws", "  garden  ")
    check("'  garden  ' is stored as 'Garden'", category_of("p-ws") == "Garden", category_of("p-ws"))
    seed("gs-1", "Garden Shed", old)
    put("p-ws2", "garden   shed")
    check("'garden   shed' is stored as 'Garden Shed'", category_of("p-ws2") == "Garden Shed", category_of("p-ws2"))
    put("p-plural", "Gardens")
    check("'Gardens' stays its own category", category_of("p-plural") == "Gardens", category_of("p-plural"))
    put("p-dash", "Garden-Shed")
    check("'Garden-Shed' stays its own category", category_of("p-dash") == "Garden-Shed", category_of("p-dash"))
    put("p-empty", "")
    check("an empty category stays empty", category_of("p-empty") == "", category_of("p-empty"))
    put("p-new", "Roof Work")
    check("a brand-new category is stored as given", category_of("p-new") == "Roof Work", category_of("p-new"))

    print("\n4. a resync of an existing thread with the wrong case keeps the canonical spelling")
    put("p-resync", "Garden")
    r = put("p-resync", "garden")
    check("the resync is stored as 'Garden'", category_of("p-resync") == "Garden", category_of("p-resync"))

    print("\n5. two new spellings in one batch become one")
    batch = [{"thread_id": "b-%d" % i, "title": "Batch %d" % i, "summary": "s", "category": c,
              "urgency": "whenever", "state": "waiting-external", "ask": {"kind": "none"},
              "blocked_on": "the paint shop's reply", "recheck_at": time.time() + 86400,
              "sources": [{"source": "probe", "id": "b-%d" % i}]}
             for i, c in enumerate(["Fencing", "fencing", "FENCING "])]
    r = CLIENT.put("/api/triage/threads", json={"threads": batch}, headers=AUTH)
    check("batch accepted", r.status_code == 200 and r.json().get("created") == 3, r.text[:300])
    check("all three are stored as the first spelling", [category_of("b-%d" % i) for i in range(3)]
          == ["Fencing"] * 3, [category_of("b-%d" % i) for i in range(3)])

    print("\n6. the create door and the resort door")
    code, body = create("garden", "A hand-written garden card")
    check("create: 201", code == 201, body)
    check("create: 'garden' is stored as 'Garden'", category_of(body.get("thread_id")) == "Garden",
          category_of(body.get("thread_id")))
    r = CLIENT.post("/api/triage/threads/new", headers=AUTH,
                    json={"title": "Another hand-written card", "category": "PLUMBING", "intent": "mine"})
    check("/threads/new: 'PLUMBING' is stored as 'Plumbing'", r.status_code == 201
          and category_of(r.json().get("thread_id")) == "Plumbing", r.text[:300])
    before = table_names()
    r = CLIENT.post("/api/triage/resort", headers=AUTH, json={"category": "gaRDen"})
    check("resort: 202 naming the canonical spelling", r.status_code == 202 and r.json().get("category") == "Garden",
          r.text[:300])
    check("resort: no new spelling in the categories table", table_names() == before, (before, table_names()))

    print("\n7. a second site canonicalises the same way")
    os.environ["HELM_SITE"] = "work"
    site.reset()
    try:
        r = CLIENT.put("/api/triage/threads", headers=AUTH, json={"threads": [
            {"thread_id": "w-garden", "title": "Work garden card", "category": "garden",
             "ask": {"kind": "ack", "prompt": "Read this?"}}]})
        check("work PUT: 'garden' is stored as 'Garden'", r.status_code == 200 and category_of("w-garden") == "Garden",
              (r.status_code, category_of("w-garden")))
        r = CLIENT.post("/api/triage/threads/new", headers=AUTH,
                        json={"title": "Work hand card", "category": "garden shed", "intent": "mine"})
        check("work /threads/new: 'garden shed' is stored as 'Garden Shed'",
              r.status_code == 201 and category_of(r.json().get("thread_id")) == "Garden Shed", r.text[:300])
    finally:
        os.environ["HELM_SITE"] = "home"
        site.reset()

    print("\n8. merge-case: the dry run lists the groups and writes nothing")
    # Duplicates a database already holds (written before this change).
    seed("m-a", "Lighting", old + 1)
    seed("m-b", "Lighting", old + 2)
    seed("m-c", "lighting", old + 3)
    seed("m-d", "LIGHTING ", old + 4)
    seed("m-e", "garden", old + 5)
    seed("m-f", "Lightings", old + 6)
    before_threads = thread_spellings()
    r = CLIENT.post(MERGE, headers=AUTH, json={})
    body = r.json()
    check("dry run: 200, dry_run true", r.status_code == 200 and body.get("dry_run") is True, body)
    groups = {g["canonical"]: g for g in body.get("groups") or []}
    check("dry run: Lighting, Garden and Plumbing are the groups", sorted(groups) == ["Garden", "Lighting", "Plumbing"],
          sorted(groups))
    lg = groups.get("Lighting") or {}
    check("dry run: Lighting's variants and counts", sorted((v["name"], v["threads"]) for v in lg.get("merge") or [])
          == [("LIGHTING ", 1), ("lighting", 1)] and lg.get("canonical_threads") == 2, lg)
    check("dry run: Garden's variant is 'garden' with its threads",
          [(v["name"], v["threads"]) for v in (groups.get("Garden") or {}).get("merge") or []] == [("garden", 2)],
          groups.get("Garden"))
    check("dry run: the totals", body.get("pairs") == 4 and body.get("threads_to_rewrite") == 5, body)
    check("dry run: 'Lightings' is not merged", "Lightings" not in json.dumps(body.get("groups")), body.get("groups"))
    check("dry run: no record written", not glob.glob(os.path.join(RECORD_DIR, "*")), os.listdir(TMP))
    check("dry run: no thread changed", thread_spellings() == before_threads)
    r = CLIENT.post(MERGE, headers=AUTH, json={"apply": False})
    check("apply false is a dry run too", r.json().get("dry_run") is True and thread_spellings() == before_threads)

    print("\n9. merge-case: the record must be written first, or nothing changes")
    os.makedirs(TMP, exist_ok=True)
    with open(RECORD_DIR, "w") as f:   # a file where the record directory should be
        f.write("x")
    r = CLIENT.post(MERGE, headers=AUTH, json={"apply": True})
    check("apply with no writable record directory: refused", r.status_code >= 400 and r.json().get("ok") is False,
          (r.status_code, r.text[:300]))
    check("and no thread changed", thread_spellings() == before_threads)
    os.remove(RECORD_DIR)

    print("\n9b. merge-case: a rewrite that fails is rolled back, and its record says so")
    real_fts = store._fts_thread

    def broken_fts(*a, **k):
        raise sqlite3.OperationalError("disk I/O error (simulated)")
    store._fts_thread = broken_fts
    try:
        try:
            store.merge_category_case(store.db(DB), apply=True, record_dir=os.path.join(TMP, "failed-merge"))
            raised = False
        except sqlite3.OperationalError:
            raised = True
    finally:
        store._fts_thread = real_fts
    failed = glob.glob(os.path.join(TMP, "failed-merge", "*.json"))
    frec = json.load(open(failed[0])) if failed else {}
    check("the failure reaches the caller", raised)
    check("no thread changed", thread_spellings() == before_threads)
    check("its record is marked rolled back", len(failed) == 1 and str(frec.get("status", "")).startswith("rolled back"),
          frec.get("status"))

    print("\n10. merge-case: apply writes the record, then rewrites")
    r = CLIENT.post(MERGE, headers=AUTH, json={"apply": True})
    body = r.json()
    check("apply: 200, dry_run false", r.status_code == 200 and body.get("dry_run") is False, body)
    files = glob.glob(os.path.join(RECORD_DIR, "*.json"))
    check("apply: one record file", len(files) == 1, files)
    check("apply: the response names the record", files and body.get("record") == files[0], (body.get("record"), files))
    rec = json.load(open(files[0])) if files else {}
    check("apply: the record file is 0600", files and stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600)
    check("apply: the record says the rewrite was committed", rec.get("status") == "applied" and rec.get("applied_at"),
          {k: rec.get(k) for k in ("status", "applied_at")})
    check("apply: no temporary file is left beside the record", not glob.glob(os.path.join(RECORD_DIR, "*.tmp")),
          os.listdir(RECORD_DIR) if os.path.isdir(RECORD_DIR) else None)
    maps = {(m["from"], m["to"]): sorted(m["thread_ids"]) for m in rec.get("merges") or []}
    check("apply: the record maps each variant to its canonical, with the thread ids", maps == {
        ("LIGHTING ", "Lighting"): ["m-d"], ("lighting", "Lighting"): ["m-c"],
        ("garden", "Garden"): ["g-small", "m-e"], ("plumbing", "Plumbing"): ["pl-new"]}, maps)
    check("apply: the response counts what it rewrote", body.get("pairs") == 4 and body.get("threads_rewritten") == 5,
          body)
    after = thread_spellings()
    check("apply: no variant spelling is left on a thread",
          not any(k in after for k in ("lighting", "LIGHTING ", "garden", "plumbing")), after)
    check("apply: the canonical counts grew by the merged threads",
          after.get("Lighting") == 4 and after.get("Garden") == before_threads["Garden"] + 2, after)
    check("apply: 'Lightings' is untouched", category_of("m-f") == "Lightings")
    names = table_names()
    check("apply: the categories table holds one spelling per key",
          "lighting" not in names and "garden" not in names and "plumbing" not in names and "Lighting" in names, names)
    con = raw()
    try:
        cat = con.execute("SELECT first_seen FROM categories WHERE name='Garden'").fetchone()
    finally:
        con.close()
    check("apply: the canonical row keeps the oldest first_seen of its group",
          cat and abs(cat["first_seen"] - old) < 1, dict(cat) if cat else None)
    r = CLIENT.post(MERGE, headers=AUTH, json={})
    check("a dry run after the merge finds nothing", r.json().get("groups") == [] and r.json().get("pairs") == 0,
          r.json())
    hits = CLIENT.get("/api/triage/search?q=Lighting", headers=AUTH).json()
    check("search still finds a merged thread by its category",
          "m-c" in {t.get("thread_id") for t in hits.get("threads") or []}, hits)

    print("\n11. the route is bearer only and not proxied by the web role")
    check("no token: 401", CLIENT.post(MERGE, json={}).status_code == 401)
    check("the web role does not proxy it", not web.ALLOW_POST.match(MERGE) and not web.ALLOW_GET.match(MERGE))
    check("a non-object body is a 400", CLIENT.post(MERGE, headers=AUTH, content=b"[1]").status_code == 400)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
