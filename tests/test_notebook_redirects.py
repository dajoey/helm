"""Moving a note keeps its old path working: POST /api/notes/move, the redirects table, and
POST /api/notes/folder, at the home profile in `folder` mode (what runs at home).

Why (2026-09-26): Helm showed the top-level `Dream Journal` folder as stale — its writer, the
the assistant dream, died 08-12 — while the live nightly journal sat in `Memory/Dreams/`. the owner: the
notebook's "whole purpose is to be AI organized", and he chose an organizer that "acts on its
own": moves and archives without asking, never deletes, and every old path keeps working. The
organizer needs a move route, a way to make `Archive/`, and moves that do not break the
hundreds of links (the append-only Changelog among them) that name the old paths.

Covers:
- POST /api/notes/folder makes a top-level folder with its parent's mode and owner, is
  idempotent, refuses slashes, hidden and skipped names, and is logged;
- a dry run moves nothing and names the linking pages and same-name notes;
- a move with links rewrites path links ([[Folder/Page]]) in the linking pages (the assistant page
  whose date the live journal will reuse);
- a move with a redirect (the default) leaves the Changelog's text alone, yet its link, a
  suffix link and a bare-name link all resolve to the moved note, with backlinks;
- the old path reads (GET note, GET raw, GET topic) as the moved note with `redirected_from`;
  an append or a create:true write to it lands in the moved note, never a second page;
- chains collapse (A->B->C: A leads to C), a real note at an old path wins over its redirect,
  a rebuild keeps the redirects, GET /api/notes/redirects lists them;
- a move removes the folders it left empty (a folder still holding a file stays), and the home
  folder list leaves out folders with no notes;
- the write log has a row under both the old path ("moved to") and the new ("moved from"),
  which is what the straggler check reads for a page that left;
- refusals: an occupied destination (409), a folder that does not exist (400), a hidden one.
"""
# temp-ok: the folders and DBs under test are fixtures this module creates and deletes
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_redirects_")
WIKI = os.path.join(TMP, "wiki")
os.environ["HELM_NOTES_PROFILE"] = "wiki"
os.environ["HELM_NOTES_MODE"] = "folder"
os.environ["HELM_NOTES_ROOT"] = WIKI
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_APP_TOKEN"] = "test-token"  # force: a persisted real token must never leak into tests

from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from notebook import notes, render  # noqa: E402
import app.notebook_api as notebook_api  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def write(rel, text):
    p = os.path.join(WIKI, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return p


def read(rel):
    with open(os.path.join(WIKI, rel.replace("/", os.sep)), encoding="utf-8") as fh:
        return fh.read()


def exists(rel):
    return os.path.exists(os.path.join(WIKI, rel.replace("/", os.sep)))


CHANGELOG = ("# Changelog\n\n- 2026-09-25 05:00 ET - nightly-dream - [[Memory/Dreams/2026-09-25]]: journal\n"
             "- 2026-09-21 11:58 ET - the agent - [[Memory/Dreams]]: header rewritten\n")


def seed():
    os.makedirs(WIKI)
    os.chmod(WIKI, 0o2775)
    write("Changelog.md", CHANGELOG)
    write("Memory/Dreams.md", "# Dreams\n\n## Journal\n- [[Memory/Dreams/2026-09-25]] — leak night\n"
                              "- [[Memory/Dreams/2026-08-09]] — second\n")
    write("Memory/Dreams/2026-09-25.md", "# Dream 2026-09-25\n\n**HEADLINE:** leak night\n")
    write("Memory/Dreams/2026-08-09.md", "# Dream 2026-08-09\n\n- the assistant's journal: [[Dream Journal/2026-08-08]]\n")
    write("Memory/wiki_is_truth.md", "# The wiki is truth\n")
    write("Dream Journal/2026-08-08.md", "# Dream Journal 2026-08-08 (the assistant)\n")
    write("Dream Journal/Hypothesis Ledger.md", "# Hypothesis Ledger\n")
    write("Playbooks/Readers.md", "# Readers\n\nSuffix [[Dreams/2026-09-25]], bare [[Dreams]], "
                                  "aliased [[Memory/Dreams/2026-09-25#Fact-check|the check]].\n")


def run():
    seed()
    con = notes.db()
    notes.full_index(con)
    client = TestClient(Starlette(routes=notebook_api.routes))

    # --- folders -------------------------------------------------------------------------------------
    r = client.post("/api/notes/folder", json={"name": "Archive", "source": "notebook-organizer"})
    st = os.stat(os.path.join(WIKI, "Archive")) if exists("Archive") else None
    check("POST /api/notes/folder makes a top-level folder (201) with the root's mode",
          r.status_code == 201 and r.json() == {"folder": "Archive", "created": True} and st is not None
          and stat.S_IMODE(st.st_mode) == 0o2775, (r.status_code, r.json(), st and oct(st.st_mode)))
    check("the new folder is one a note may be filed under", "Archive" in notes.folders_allowed())
    r = client.post("/api/notes/folder", json={"name": "Archive"})
    check("making it again is not an error (200, created false)", r.status_code == 200 and r.json()["created"] is False, r.json())
    bad = {n: client.post("/api/notes/folder", json={"name": n}).status_code
           for n in ("", "a/b", "..", ".hidden", "_plug", "Untitled")}
    check("slashes, dot names, hidden and skipped folders are refused (400)",
          all(v == 400 for v in bad.values()) and not exists("a") and not exists(".hidden"), bad)
    rows = client.get("/api/notes/writes").json()
    rows = rows.get("writes", rows) if isinstance(rows, dict) else rows
    check("the folder is in the write log", any(w["rel"] == "Archive" and w["kind"] == "folder" for w in rows), rows[-3:])
    client.post("/api/notes/folder", json={"name": "Dream Journal"})

    # --- dry run -------------------------------------------------------------------------------------
    r = client.post("/api/notes/move", json={"rel": "Dream Journal/2026-08-08.md",
                                             "new_rel": "Archive/Dream Journal/2026-08-08.md",
                                             "links": True, "dry_run": True})
    j = r.json()
    check("a dry run moves nothing and names the linking pages and the same-name notes",
          r.status_code == 200 and j["moved"] is False and j["links"] == ["Memory/Dreams/2026-08-09.md"]
          and j["same_name"] == [] and exists("Dream Journal/2026-08-08.md") and not exists("Archive/Dream Journal"), j)

    # --- archive the assistant page, rewriting its path links -----------------------------------------------
    r = client.post("/api/notes/move", json={"rel": "Dream Journal/2026-08-08.md",
                                             "new_rel": "Archive/Dream Journal/2026-08-08.md",
                                             "links": True, "source": "notebook-organizer"})
    j = r.json()
    check("a move with links: the note moves (nested folders made) and its path links follow it",
          r.status_code == 200 and j["moved"] and j["redirect"] is True
          and exists("Archive/Dream Journal/2026-08-08.md") and not exists("Dream Journal/2026-08-08.md")
          and "[[Archive/Dream Journal/2026-08-08]]" in read("Memory/Dreams/2026-08-09.md")
          and j["links"] == ["Memory/Dreams/2026-08-09.md"], j)

    # --- move the live journal with a redirect only --------------------------------------------------
    before = read("Changelog.md")
    r = client.post("/api/notes/move", json={"rel": "Memory/Dreams/2026-09-25.md",
                                             "new_rel": "Dream Journal/2026-09-25.md", "source": "notebook-organizer"})
    check("a move with the default redirect", r.status_code == 200 and r.json()["redirect"] is True
          and exists("Dream Journal/2026-09-25.md") and not exists("Memory/Dreams/2026-09-25.md"), r.json())
    check("the append-only Changelog is not rewritten", read("Changelog.md") == before)
    check("the Changelog's link to the old path resolves to the moved note",
          notes.resolve_note(con, "Memory/Dreams/2026-09-25") == "Dream Journal/2026-09-25.md")
    check("a suffix link to the old path resolves too", notes.resolve_note(con, "Dreams/2026-09-25") == "Dream Journal/2026-09-25.md")
    back = [b["rel"] if isinstance(b, dict) else b for b in notes.backlinks(con, "Dream Journal/2026-09-25.md")]
    check("the moved note's backlinks include the pages that name its old path",
          {"Changelog.md", "Playbooks/Readers.md", "Memory/Dreams.md"} <= set(back), back)
    html = render.render("see [[Memory/Dreams/2026-09-25|the journal]]", render.Resolver(con))
    check("a rendered link to the old path is a live link to the new one",
          'href="#/note/Dream%20Journal%2F2026-09-25.md"' in html and "missing" not in html, html)
    notes.full_index(con)
    check("the full index pass resolves old paths the same way (bulk resolver)",
          con.execute("SELECT target_rel FROM links WHERE src='Changelog.md' AND target='Memory/Dreams/2026-09-25'")
          .fetchone()["target_rel"] == "Dream Journal/2026-09-25.md")

    r = client.get("/api/notes/note", params={"rel": "Memory/Dreams/2026-09-25.md"})
    check("GET note of the old path serves the moved note and says so",
          r.status_code == 200 and r.json()["rel"] == "Dream Journal/2026-09-25.md"
          and r.json()["redirected_from"] == "Memory/Dreams/2026-09-25.md", r.json())
    r = client.get("/api/notes/raw", params={"rel": "Memory/Dreams/2026-09-25.md"})
    check("GET raw of the old path serves the moved note's text (what nb.read sees)",
          r.status_code == 200 and r.json()["rel"] == "Dream Journal/2026-09-25.md" and "leak night" in r.json()["text"]
          and r.json()["redirected_from"] == "Memory/Dreams/2026-09-25.md", r.json())
    r = client.get("/api/topic", params={"rel": "Memory/Dreams/2026-09-25.md"})
    check("GET topic of the old path answers for the moved note", r.status_code == 200, r.status_code)

    r = client.post("/api/notes/append", json={"rel": "Memory/Dreams/2026-09-25.md", "text": "- late line", "gap": "line"})
    check("an append to the old path lands in the moved note, not a new page",
          r.status_code == 200 and r.json()["rel"] == "Dream Journal/2026-09-25.md"
          and read("Dream Journal/2026-09-25.md").rstrip().endswith("- late line") and not exists("Memory/Dreams/2026-09-25.md"), r.json())
    r = client.put("/api/notes/note", json={"rel": "Memory/Dreams/2026-09-25.md", "text": "# Dream 2026-09-25\n\nrewritten\n",
                                            "create": True})
    check("a create:true write to the old path writes the moved note",
          r.status_code == 200 and r.json()["rel"] == "Dream Journal/2026-09-25.md"
          and "rewritten" in read("Dream Journal/2026-09-25.md") and not exists("Memory/Dreams/2026-09-25.md"), r.json())

    # --- the index page, then a bare-name link -----------------------------------------------------------
    r = client.post("/api/notes/move", json={"rel": "Memory/Dreams.md", "new_rel": "Dream Journal/Index.md"})
    check("a bare-name link to a renamed-by-move note resolves through its redirect",
          r.status_code == 200 and notes.resolve_note(con, "Dreams") == "Dream Journal/Index.md"
          and notes.resolve_note(con, "Memory/Dreams") == "Dream Journal/Index.md", r.json())

    # --- chains, a real note wins, rebuild keeps them ---------------------------------------------------
    client.post("/api/notes/move", json={"rel": "Dream Journal/2026-09-25.md", "new_rel": "Archive/2026-09-25.md"})
    red = {x["src"]: x["dst"] for x in client.get("/api/notes/redirects").json()["redirects"]}
    check("a chain collapses: the first old path leads straight to the last place",
          red.get("Memory/Dreams/2026-09-25.md") == "Archive/2026-09-25.md"
          and red.get("Dream Journal/2026-09-25.md") == "Archive/2026-09-25.md"
          and notes.resolve_note(con, "Memory/Dreams/2026-09-25") == "Archive/2026-09-25.md", red)
    write("Dream Journal/2026-09-25.md", "# A new page at the old path\n")
    notes.full_index(con)
    r = client.get("/api/notes/note", params={"rel": "Dream Journal/2026-09-25.md"})
    check("a real note at an old path wins over its redirect",
          r.status_code == 200 and r.json()["rel"] == "Dream Journal/2026-09-25.md" and "redirected_from" not in r.json()
          and notes.resolve_note(con, "Dream Journal/2026-09-25") == "Dream Journal/2026-09-25.md", r.json())
    notes.rebuild(con, force=True)
    check("a rebuild keeps the redirects", notes.follow_redirect(con, "Memory/Dreams/2026-09-25.md") == "Archive/2026-09-25.md")

    # --- the write log ---------------------------------------------------------------------------------------
    rows = client.get("/api/notes/writes").json()
    rows = rows.get("writes", rows) if isinstance(rows, dict) else rows
    mv = [(w["rel"], w["kind"]) for w in rows if w["kind"] == "move"]
    check("a move logs a row under the old path and one under the new",
          ("Memory/Dreams/2026-08-09.md", "move") not in mv
          and ("Dream Journal/2026-08-08.md", "move") in mv and ("Archive/Dream Journal/2026-08-08.md", "move") in mv, mv)
    notes_col = {r2["rel"]: r2["added"] for r2 in con.execute("SELECT rel, added FROM writes WHERE kind='move'")}
    check("the rows say where the page went and where it came from",
          notes_col.get("Dream Journal/2026-08-08.md") == "moved to Archive/Dream Journal/2026-08-08.md"
          and notes_col.get("Archive/Dream Journal/2026-08-08.md") == "moved from Dream Journal/2026-08-08.md", notes_col)

    # --- review 28: a write never follows a redirect into the wrong note -------------------------------------------
    write("Projects/Foo.md", "# Foo\n")
    write("Projects/Baz.md", "# Baz, the original\n")
    notes.full_index(con)
    client.post("/api/notes/move", json={"rel": "Projects/Foo.md", "new_rel": "Archive/Foo.md"})
    client.request("DELETE", "/api/notes/note", params={"rel": "Archive/Foo.md"})
    r = client.put("/api/notes/note", json={"rel": "Projects/Foo.md", "text": "# Foo, again\n", "create": True})
    check("a redirect whose note is gone is not followed: the write makes the old path itself",
          r.status_code == 201 and r.json()["rel"] == "Projects/Foo.md" and exists("Projects/Foo.md")
          and not exists("Archive/Foo.md"), r.json())
    check("...and that create clears the stale redirect", notes.follow_redirect(con, "Projects/Foo.md") is None)

    client.post("/api/notes/move", json={"rel": "Projects/Baz.md", "new_rel": "Archive/Baz.md"})
    r = client.post("/api/notes/new", json={"folder": "Projects", "title": "Baz"})
    check("a note made at a moved path clears its redirect",
          r.status_code == 201 and notes.follow_redirect(con, "Projects/Baz.md") is None, (r.status_code, r.json()))
    client.request("DELETE", "/api/notes/note", params={"rel": "Projects/Baz.md"})
    r = client.put("/api/notes/note", json={"rel": "Projects/Baz.md", "text": "# a script's page\n", "create": True})
    check("the old redirect does not come back after that note is deleted: the moved page is untouched",
          r.status_code == 201 and r.json()["rel"] == "Projects/Baz.md"
          and read("Archive/Baz.md") == "# Baz, the original\n", (r.json(), read("Archive/Baz.md")))

    r = client.put("/api/notes/note", json={"rel": "Memory/dreams.md", "text": "# lower-case page\n", "create": True})
    check("a path differing only in case is its own page (the folder is case-sensitive)",
          r.status_code == 201 and r.json()["rel"] == "Memory/dreams.md" and exists("Memory/dreams.md")
          and read("Dream Journal/Index.md").startswith("# Dreams"), r.json())

    write("Projects/case.md", "# case\n")
    notes.full_index(con)
    r = client.post("/api/notes/move", json={"rel": "Projects/case.md", "new_rel": "Projects/Case.md", "dry_run": True})
    check("a dry run of a case-only move says no redirect, as the move stores none",
          r.status_code == 200 and r.json()["moved"] is False and r.json()["redirect"] is False
          and exists("Projects/case.md"), r.json())
    r = client.post("/api/notes/move", json={"rel": "Projects/case.md", "new_rel": "Projects/Case.md"})
    check("a case-only move stores no redirect", r.status_code == 200 and r.json()["redirect"] is False
          and notes.follow_redirect(con, "Projects/case.md") is None, r.json())
    r = client.post("/api/notes/move", json={"rel": "Projects/Case.md", "new_rel": "Archive/Case.md", "redirect": "maybe"})
    check("a yes/no field that is neither is a 400, and nothing moves", r.status_code == 400 and exists("Projects/Case.md"), r.json())
    r = client.post("/api/notes/move", json={"rel": "Projects/Case.md", "new_rel": "Archive/Case.md", "redirect": "false"})
    check("\"false\" means false", r.status_code == 200 and r.json()["redirect"] is False
          and notes.follow_redirect(con, "Projects/Case.md") is None, r.json())
    bad = {n: client.post("/api/notes/folder", json={"name": n}).status_code for n in ("New.", "New .", "_PLUG", "_Plug")}
    check("a folder name ending in a dot, or a skipped name in any case, is refused",
          all(v == 400 for v in bad.values()) and not exists("New.") and not exists("New ."), bad)

    # --- emptied folders -----------------------------------------------------------------------------------------
    write("Retired/sub/only.md", "# only\n")
    write("Leftover/page.md", "# page\n")
    write("Leftover/picture.png", "png")
    write("Kept/a.md", "# a\n")
    notes.full_index(con)
    r = client.post("/api/notes/move", json={"rel": "Retired/sub/only.md", "new_rel": "Archive/Retired/sub/only.md"})
    check("a move removes the nested folders it left empty (prune is the default), never the top-level one",
          r.status_code == 200 and r.json().get("pruned") == ["Retired/sub"] and not exists("Retired/sub")
          and exists("Retired"), r.json())
    listed = {f["folder"] for f in client.get("/api/notes/folders").json()["folders"]}
    check("the emptied top-level folder is hidden from the home folder list", "Retired" not in listed, listed)
    r = client.put("/api/notes/note", json={"rel": "Retired/late.md", "text": "# a late writer\n", "create": True})
    r2 = client.post("/api/notes/append", json={"rel": "Retired/later.md", "text": "- a later line", "create": True})
    check("a writer can still create notes in a folder whose last note left (review 28)",
          r.status_code == 201 and r2.status_code == 201 and exists("Retired/late.md") and exists("Retired/later.md"),
          (r.status_code, r.json(), r2.status_code, r2.json()))
    r = client.post("/api/notes/move", json={"rel": "Leftover/page.md", "new_rel": "Archive/Leftover/page.md"})
    check("a folder still holding a file stays on disk", r.status_code == 200 and r.json().get("pruned") == []
          and exists("Leftover/picture.png"), r.json())
    listed = {f["folder"] for f in client.get("/api/notes/folders").json()["folders"]}
    check("the home folder list leaves out folders with no notes (Leftover), keeps the rest",
          "Leftover" not in listed and "Archive" in listed and "Memory" in listed and "Retired" in listed, listed)
    r = client.post("/api/notes/move", json={"rel": "Kept/a.md", "new_rel": "Archive/Kept/a.md", "prune": False})
    check("prune: false leaves the emptied folder", r.status_code == 200 and "pruned" not in r.json() and exists("Kept"), r.json())

    # --- refusals ----------------------------------------------------------------------------------------------
    r = client.post("/api/notes/move", json={"rel": "Memory/wiki_is_truth.md", "new_rel": "Dream Journal/Hypothesis Ledger.md"})
    check("a move onto an existing note is 409 and moves nothing",
          r.status_code == 409 and exists("Memory/wiki_is_truth.md") and read("Dream Journal/Hypothesis Ledger.md") == "# Hypothesis Ledger\n",
          r.json())
    r = client.post("/api/notes/move", json={"rel": "Memory/wiki_is_truth.md", "new_rel": "Nowhere/wiki_is_truth.md"})
    check("a move into a folder that does not exist is 400", r.status_code == 400 and exists("Memory/wiki_is_truth.md"), r.json())
    r = client.post("/api/notes/move", json={"rel": "Memory/wiki_is_truth.md", "new_rel": ".secret/wiki_is_truth.md"})
    check("a move into a hidden folder is 400", r.status_code == 400 and exists("Memory/wiki_is_truth.md"), r.json())
    r = client.post("/api/notes/move", json={"rel": "Memory/nope.md", "new_rel": "Archive/nope.md"})
    check("a move of a note that is not there is 404", r.status_code == 404, r.json())

    con.close()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
