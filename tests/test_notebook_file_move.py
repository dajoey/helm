"""Moving a non-note file (an image, a JSON snapshot, a .md.bak-* copy) through POST /api/notes/move,
at the home profile in `folder` mode (what runs at home).

Why (2026-09-27): the notebook organizer retires a folder by moving its pages to Archive/ through
POST /api/notes/move, but that route took only notes (a path ending in .md), so every image, state
file and backup copy in a retired folder stayed behind on disk (a0client/*.webp,
cowork/generated-images/*.png, Memory/Dreams/_state/snapshot.json, Personal/mods/*.md.bak-*),
keeping the retired folders alive. the owner's rule is archive, never delete, so Helm has to move them.

Covers:
- a file moves on disk to the new path (parent folders made), bytes unchanged, and the index
  follows (the files row is at the new path, not the old);
- an embed by path ([[folder/photo.webp]]) and by bare name both resolve to the moved file, and
  the old /att/ path still serves it (the redirect, the default);
- links: true rewrites path links to the file in the linking pages;
- a dry run moves nothing and names the linking pages;
- a state file under a nested folder and a .md.bak-* copy move too, and the emptied nested
  folders are pruned;
- the write log has a row under the old path ("moved to") and the new ("moved from");
- refusals: a note path as destination (400), an occupied destination (409), a folder that
  does not exist (400), a hidden destination or source (400/404), a link as the source (404),
  a file that is not there (404); nothing moves on any of them;
- a note move is unchanged (still needs .md at both ends).
"""
# temp-ok: the folders and DBs under test are fixtures this module creates and deletes
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_file_move_")
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

from notebook import notes  # noqa: E402
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


def path(rel):
    return os.path.join(WIKI, rel.replace("/", os.sep))


def write(rel, data):
    p = path(rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


def read(rel):
    try:
        with open(path(rel), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def exists(rel):
    return os.path.lexists(path(rel))


PHOTO = b"RIFF\x00\x00\x00\x00WEBPVP8 fake photo bytes"


def seed():
    os.makedirs(WIKI)
    write("Archive/README.md", "# Archive\n")
    write("a0client/Index.md", "# a0\n")
    write("a0client/photo.webp", PHOTO)
    write("cowork/generated-images/one.png", b"\x89PNG one")
    write("Memory/Dreams.md", "# Dreams\n")
    write("Memory/Dreams/_state/snapshot.json", '{"night": 1}\n')
    write("Memory/Dreams/_state/snapshot.json.1", '{"night": 0}\n')
    write("Personal/mods/Mods.md.bak-20260901", "# old copy\n")
    write("Personal/Gallery.md", "# Gallery\n\nPath ![[a0client/photo.webp]], bare ![[photo.webp]].\n"
                                 "Picture ![[cowork/generated-images/one.png|a picture]].\n")
    write("a0client/shared.png", b"a0 shared")
    write("attachments/shared.png", b"attachments shared")
    write("Personal/Shared.md", "# Shared\n\n![[a0client/shared.png]]\n")
    write("Personal/.secret.png", b"hidden")
    write("Personal/keep.png", b"keep")
    os.symlink(path("Personal/keep.png"), path("Personal/link.png"))


def targets(con, src):
    return {r["target"]: r["target_rel"] for r in con.execute(
        "SELECT target, target_rel FROM links WHERE src=?", (src,))}


def file_row(con, rel):
    return con.execute("SELECT rel FROM files WHERE rel=?", (rel,)).fetchone() is not None


def run():
    seed()
    con = notes.db()
    notes.full_index(con)
    client = TestClient(Starlette(routes=notebook_api.routes))
    check("the fixture's files are indexed", file_row(con, "a0client/photo.webp")
          and file_row(con, "Memory/Dreams/_state/snapshot.json"))

    # --- dry run -----------------------------------------------------------------------------------------
    r = client.post("/api/notes/move", json={"rel": "a0client/photo.webp",
                                             "new_rel": "Archive/a0client/photo.webp",
                                             "links": True, "dry_run": True})
    j = r.json()
    check("a file's dry run moves nothing and names the pages that link it",
          r.status_code == 200 and j.get("moved") is False and j.get("links") == ["Personal/Gallery.md"]
          and exists("a0client/photo.webp") and not exists("Archive/a0client"), (r.status_code, j))

    # --- an image, with the redirect (the organizer's call: redirect true, links false) ---------------
    r = client.post("/api/notes/move", json={"rel": "a0client/photo.webp",
                                             "new_rel": "Archive/a0client/photo.webp",
                                             "redirect": True, "links": False, "source": "notebook-organizer"})
    j = r.json()
    check("POST /api/notes/move moves an image on disk, making the parent folders (200, moved)",
          r.status_code == 200 and j.get("moved") is True and j.get("to") == "Archive/a0client/photo.webp"
          and not exists("a0client/photo.webp") and exists("Archive/a0client/photo.webp")
          and read("Archive/a0client/photo.webp") == PHOTO, (r.status_code, j))
    check("the index follows the file", file_row(con, "Archive/a0client/photo.webp")
          and not file_row(con, "a0client/photo.webp"))
    t = targets(con, "Personal/Gallery.md")
    check("a path embed and a bare-name embed of the file both resolve to its new path",
          t.get("a0client/photo.webp") == "Archive/a0client/photo.webp"
          and t.get("photo.webp") == "Archive/a0client/photo.webp", t)
    notes.full_index(con)
    t = targets(con, "Personal/Gallery.md")
    check("a full index pass keeps the path embed resolved", t.get("a0client/photo.webp") == "Archive/a0client/photo.webp", t)
    r = client.post("/api/notes/move", json={"rel": "a0client/shared.png", "new_rel": "Archive/a0client/shared.png"})
    t1 = targets(con, "Personal/Shared.md")
    notes.full_index(con)
    t2 = targets(con, "Personal/Shared.md")
    check("a path embed follows the redirect, not a same-name file in attachments/ (per link and in a full pass)",
          r.status_code == 200 and t1.get("a0client/shared.png") == "Archive/a0client/shared.png"
          and t2.get("a0client/shared.png") == "Archive/a0client/shared.png", (r.json(), t1, t2))
    r = client.get("/att/Archive/a0client/photo.webp")
    check("GET /att/ serves the file at its new path", r.status_code == 200 and r.content == PHOTO, r.status_code)
    r = client.get("/att/a0client/photo.webp")
    check("GET /att/ at the old path still serves the moved file", r.status_code == 200 and r.content == PHOTO,
          r.status_code)
    note = client.get("/api/notes/note", params={"rel": "Personal/Gallery.md"}).json()
    check("the rendered page embeds the file from its new path",
          "/att/" + quote("Archive/a0client/photo.webp", safe="") in (note.get("html") or ""),
          (note.get("html") or "")[:400])
    rows = client.get("/api/notes/writes").json()
    rows = rows.get("writes", rows) if isinstance(rows, dict) else rows
    check("the write log has the move under both paths",
          any(w["rel"] == "a0client/photo.webp" and w["kind"] == "move" for w in rows)
          and any(w["rel"] == "Archive/a0client/photo.webp" and w["kind"] == "move" for w in rows), rows[-3:])

    # --- links: true rewrites path links ------------------------------------------------------------------
    r = client.post("/api/notes/move", json={"rel": "cowork/generated-images/one.png",
                                             "new_rel": "Archive/cowork/generated-images/one.png", "links": True})
    j = r.json()
    text = (read("Personal/Gallery.md") or b"").decode()
    check("links: true rewrites the path link to the file, alias kept",
          r.status_code == 200 and j.get("links") == ["Personal/Gallery.md"]
          and "![[Archive/cowork/generated-images/one.png|a picture]]" in text
          and "![[cowork/generated-images/one.png" not in text, (j, text))
    check("the emptied nested folder is pruned, never the top-level one",
          j.get("pruned") == ["cowork/generated-images"] and not exists("cowork/generated-images")
          and exists("cowork"), j)

    # --- state files and backup copies -------------------------------------------------------------------
    r1 = client.post("/api/notes/move", json={"rel": "Memory/Dreams/_state/snapshot.json",
                                              "new_rel": "Archive/Memory/Dreams/_state/snapshot.json"})
    r2 = client.post("/api/notes/move", json={"rel": "Memory/Dreams/_state/snapshot.json.1",
                                              "new_rel": "Archive/Memory/Dreams/_state/snapshot.json.1"})
    check("state files move, and the last one out prunes the emptied folders",
          r1.status_code == 200 and r2.status_code == 200 and exists("Archive/Memory/Dreams/_state/snapshot.json")
          and exists("Archive/Memory/Dreams/_state/snapshot.json.1") and not exists("Memory/Dreams")
          and r2.json().get("pruned") == ["Memory/Dreams/_state", "Memory/Dreams"] and exists("Memory/Dreams.md"),
          (r1.json(), r2.json()))
    r = client.post("/api/notes/move", json={"rel": "Personal/mods/Mods.md.bak-20260901",
                                             "new_rel": "Archive/Personal/mods/Mods.md.bak-20260901"})
    check("a .md.bak-* copy moves as a file", r.status_code == 200
          and read("Archive/Personal/mods/Mods.md.bak-20260901") == b"# old copy\n", r.json())

    # --- refusals -------------------------------------------------------------------------------------------
    write("Personal/pic.png", b"pic")
    write("Archive/pic.png", b"other")
    notes.full_index(con)
    cases = {
        "note destination": ({"rel": "Personal/pic.png", "new_rel": "Archive/pic.md"}, 400),
        "occupied": ({"rel": "Personal/pic.png", "new_rel": "Archive/pic.png"}, 409),
        "no such folder": ({"rel": "Personal/pic.png", "new_rel": "Nowhere/pic.png"}, 400),
        "hidden destination": ({"rel": "Personal/pic.png", "new_rel": "Archive/.pic.png"}, 400),
        "dot folder": ({"rel": "Personal/pic.png", "new_rel": "Archive/.git/pic.png"}, 400),
        "dotdot": ({"rel": "Personal/pic.png", "new_rel": "Archive/../../pic.png"}, 400),
        "hidden source": ({"rel": "Personal/.secret.png", "new_rel": "Archive/secret.png"}, 404),
        "link source": ({"rel": "Personal/link.png", "new_rel": "Archive/link.png"}, 404),
        "missing": ({"rel": "Personal/nope.png", "new_rel": "Archive/nope.png"}, 404),
    }
    got = {k: client.post("/api/notes/move", json=b).status_code for k, (b, _) in cases.items()}
    check("refusals answer 400/409/404 as they should", all(got[k] == s for k, (_, s) in cases.items()), got)
    check("nothing moved on any refusal",
          read("Personal/pic.png") == b"pic" and read("Archive/pic.png") == b"other" and exists("Personal/.secret.png")
          and os.path.islink(path("Personal/link.png")) and not exists("Archive/secret.png")
          and not exists("Archive/link.png") and not exists("Nowhere"), sorted(os.listdir(path("Archive"))))

    # --- a note move is unchanged --------------------------------------------------------------------------
    r = client.post("/api/notes/move", json={"rel": "a0client/Index.md", "new_rel": "Archive/a0client/Index.png"})
    check("a note still cannot move to a non-note path (400)", r.status_code == 400 and exists("a0client/Index.md"), r.json())
    r = client.post("/api/notes/move", json={"rel": "a0client/Index.md", "new_rel": "Archive/a0client/Index.md"})
    check("a note still moves", r.status_code == 200 and exists("Archive/a0client/Index.md"), r.json())

    con.close()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
