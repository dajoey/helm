"""POST /api/notes/delete: a caller that keeps the notebook tidy removes a note, and says why.

The client's Friday notebook audit reads a second notebook through the api and applies its
fixes through the api (a numbered copy that is byte-identical to its original, a note a
generator mirrored from a ticket system, an empty note, the numbered half of a merged
pair). Every fix it needs existed except the removal. The contract it asked for:

  - {rel, reason, source} removes the note from the notebook and its file from the export;
    its revisions stay, and the history gains one last line saying who removed it and why,
    so the text is still recoverable;
  - a blank reason is 400; a daily note, or a note in the dated streams under Daily/Meetings
    and Daily/Ticket Cleanup, is 403 (history, like a past day that refuses edits);
  - a note that is not there is a 404 with a JSON detail ("no note at <rel>"), so a caller can
    tell a missing note from a missing route (whose 404 is a bare "Not Found");
  - the answer is {rel, deleted: true}.

Run against a store-mode notebook with an export folder, over HTTP, the way the client
reaches it. DELETE /api/notes/note (the bridge's delete) keeps working as it did.
"""
# temp-ok: the folders and DBs under test are fixtures this module creates and deletes
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_delete_route_")
VAULT = os.path.join(TMP, "vault")
os.environ["HELM_NOTES_PROFILE"] = "fixed"
os.environ["HELM_NOTES_MODE"] = "store"
os.environ["HELM_NOTES_ROOT"] = VAULT
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP

from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from notebook import edit, export, notes  # noqa: E402
import app.notebook_api as notebook_api  # noqa: E402

PASS = FAIL = 0
export.QUARANTINE = os.path.join(TMP, "quarantine")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail != "" else ""))


def js(r):
    """The JSON body, or {} for a body that is not JSON (a bare 'Not Found')."""
    try:
        out = r.json()
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def on_disk(rel):
    return os.path.exists(os.path.join(VAULT, rel.replace("/", os.sep)))


def run():
    os.makedirs(VAULT, exist_ok=True)
    client = TestClient(Starlette(routes=notebook_api.routes))
    con = notes.db()

    # --- the verify path the client gave: create, move, delete --------------------------------
    rel = "Reference/zz delete route test.md"
    moved = "Systems/zz delete route test.md"
    text = "# zz delete route test\n\nA scratch note.\n"
    r = client.put("/api/notes/note", json={"rel": rel, "text": text, "create": True, "source": "audit-test"})
    check("the test note is created", r.status_code == 201, (r.status_code, r.text))
    r = client.post("/api/notes/move", json={"rel": rel, "new_rel": moved})
    check("and moved to Systems", r.status_code == 200, (r.status_code, r.text))
    check("its file is in the export", on_disk(moved))

    r = client.post("/api/notes/delete", json={"rel": moved, "reason": "byte-identical copy of Systems/Original",
                                               "source": "audit"})
    check("POST /api/notes/delete answers 200 {rel, deleted: true}",
          r.status_code == 200 and js(r).get("rel") == moved and js(r).get("deleted") is True,
          (r.status_code, r.text))
    r = client.get("/api/notes/list", params={"folder": "Systems"})
    check("the note is gone from its folder's list",
          r.status_code == 200 and moved not in [n["rel"] for n in js(r).get("notes", [])], r.text)
    check("its file is gone from the export", not on_disk(moved))
    r = client.get("/api/notes/note", params={"rel": moved})
    check("reading it is a 404 with a JSON detail", r.status_code == 404 and "detail" in js(r), (r.status_code, r.text))

    r = client.get("/api/notes/history", params={"rel": moved})
    revs = js(r).get("revisions", [])
    check("the history still returns the revisions", r.status_code == 200 and len(revs) >= 2, r.text)
    check("the newest line says who removed it and why",
          revs and revs[0]["source"] == "deleted by audit: byte-identical copy of Systems/Original", revs[:1])
    rev = client.get("/api/notes/revision", params={"id": revs[0]["id"]}) if revs else None
    check("and carries the text, so it can be brought back", rev is not None and js(rev).get("text") == text,
          rev.text if rev is not None else "")
    r = client.get("/api/notes/writes", params={"since": 0})
    dels = [w for w in js(r).get("writes", []) if w["kind"] == "delete" and w["rel"] == moved]
    check("the write log has a delete row from the caller's source", len(dels) == 1 and dels[0]["source"] == "audit", dels)

    # --- a blank reason ------------------------------------------------------------------------
    keep = "Reference/zz keep me.md"
    client.put("/api/notes/note", json={"rel": keep, "text": "# keep\n", "create": True, "source": "audit-test"})
    for label, body in (("missing", {"rel": keep, "source": "audit"}),
                        ("empty", {"rel": keep, "reason": "", "source": "audit"}),
                        ("blank", {"rel": keep, "reason": "   \n ", "source": "audit"})):
        r = client.post("/api/notes/delete", json=body)
        check("a %s reason is 400" % label, r.status_code == 400 and "reason" in js(r).get("detail", ""),
              (r.status_code, r.text))
    check("and the note is still there", notes.exists(con, keep) and on_disk(keep))

    # --- daily notes and the dated streams are history -----------------------------------------
    protected = ["Daily/2026/2026-09/2026-09-30.md", "Daily/2026-09-29.md",
                 "Daily/Meetings/2026-09-30 1430 portal call.md",
                 "Daily/Ticket Cleanup/2026-09-30 cleanup.md"]
    for p in protected:
        notes.write_text(con, p, "# " + p + "\n", "seed")
        r = client.post("/api/notes/delete", json={"rel": p, "reason": "stale", "source": "audit"})
        check("%s is refused 403" % p, r.status_code == 403 and "detail" in js(r), (r.status_code, r.text))
        check("%s is still there" % p, notes.exists(con, p))
    call = "Daily/Meetings/2026-09-30 1430 portal call.md"
    for label, spelled in (("doubled slash", "Daily//Calls/2026-09-30 1430 portal call.md"),
                           ("dot segment", "Daily/./Calls/2026-09-30 1430 portal call.md"),
                           ("backslashes", call.replace("/", "\\")),
                           ("padding spaces", "  " + call + "  "),
                           ("trailing slash", call + "/")):
        r = client.post("/api/notes/delete", json={"rel": spelled, "reason": "stale", "source": "audit"})
        check("the protected stream note spelled with %s is still 403" % label, r.status_code == 403, (r.status_code, r.text))
    check("none of those removed it", notes.exists(con, call))
    r = client.post("/api/notes/delete", json={"rel": "Daily/2026/2026-09/2026-01-01.md", "reason": "stale"})
    check("a daily note that does not exist is 403 too (the rule does not depend on the store)", r.status_code == 403,
          (r.status_code, r.text))

    # --- the history label is one line --------------------------------------------------------------
    tidy = "Reference/zz tidy label.md"
    client.put("/api/notes/note", json={"rel": tidy, "text": "# tidy\n", "create": True, "source": "audit-test"})
    r = client.post("/api/notes/delete", json={"rel": tidy, "reason": "copy\nof\tX " + "y" * 600, "source": "au\ndit"})
    label = (js(client.get("/api/notes/history", params={"rel": tidy})).get("revisions") or [{}])[0].get("source", "")
    check("the label keeps reason and source on one line, the reason cut at 500",
          r.status_code == 200 and "\n" not in label and "\t" not in label and label.startswith("deleted by au dit: copy of X ")
          and len(label) <= len("deleted by au dit: ") + 500, (r.status_code, label[:80], len(label)))

    # --- the history line and the removal land together, or not at all ----------------------------
    atomic = "Reference/zz atomic.md"
    client.put("/api/notes/note", json={"rel": atomic, "text": "# atomic\n", "create": True, "source": "audit-test"})
    before = len(js(client.get("/api/notes/history", params={"rel": atomic})).get("revisions", []))
    real = notes.reresolve_targeting

    def boom(*a, **k):
        raise RuntimeError("injected failure inside delete_note, before its commit")

    notes.reresolve_targeting = boom
    try:
        r = client.post("/api/notes/delete", json={"rel": atomic, "reason": "stale", "source": "audit"})
    finally:
        notes.reresolve_targeting = real
    second = notes.db()                      # a fresh connection sees only what was committed
    revs = [x["source"] for x in notes.revisions(second, atomic)]
    check("a failure inside the removal is a 500, the note is still there",
          r.status_code == 500 and notes.exists(second, atomic) and on_disk(atomic), (r.status_code, r.text))
    check("and it left no 'deleted by' line behind", len(revs) == before and not any(s.startswith("deleted by") for s in revs), revs)
    second.close()
    r = client.post("/api/notes/delete", json={"rel": atomic, "reason": "stale", "source": "audit"})
    check("the same delete then succeeds, with its line", r.status_code == 200 and
          [x["source"] for x in notes.revisions(con, atomic)][0] == "deleted by audit: stale", (r.status_code, r.text))

    # --- a note that is not there ----------------------------------------------------------------
    r = client.post("/api/notes/delete", json={"rel": "Reference/nope.md", "reason": "stale", "source": "audit"})
    check("a missing note is 404 with its path in the detail",
          r.status_code == 404 and js(r).get("detail") == "no note at Reference/nope.md", (r.status_code, r.text))
    r = client.post("/api/notes/nope", json={})
    check("a missing ROUTE stays a bare Not Found, so the caller can tell them apart",
          r.status_code == 404 and "detail" not in js(r), (r.status_code, r.text))
    r = client.post("/api/notes/delete", json={"reason": "stale"})
    check("no rel is 400", r.status_code == 400, (r.status_code, r.text))
    r = client.post("/api/notes/delete", json={"rel": "../etc/passwd.md", "reason": "stale"})
    check("a path that climbs out is 400", r.status_code == 400, (r.status_code, r.text))

    # --- the route is a write route, and the old one still works --------------------------------
    have = {(rt.path, m) for rt in notebook_api.write_routes() for m in rt.methods}
    check("/api/notes/delete is a notebook write route (405 at a read-only site)",
          ("/api/notes/delete", "POST") in have, sorted(have))
    r = client.delete("/api/notes/note", params={"rel": keep})
    check("DELETE /api/notes/note (delete) still removes a note without a reason",
          r.status_code == 200 and js(r).get("deleted") is True and not notes.exists(con, keep), (r.status_code, r.text))

    con.close()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
