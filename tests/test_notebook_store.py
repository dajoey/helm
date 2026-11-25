"""Store mode against a throwaway folder: upsert in folder mode, the cutover
flip and its round trip, export verify and rebuild (line endings, newer files
adopted, older ones quarantined, strays adopted or set aside, missing files
rewritten, the files table following the folder), and a revision behind every
write.

Ported from the reference suite's tests/test_store.py (101 checks), setup and imports only.
70 of the 101 port. The 31 that do not, and why:

  - "client sees the store as truth" .. "promote records and traces" (26 checks)
    and "block lands at the top" .. "block file follows the store" (5 checks):
    they drive `..\\vault\\vault.py` and `..\\daily_tasklist\\write_block.py`,
    two client-side scripts outside the reference app that are not in the reference tree
    and that the client brief () turns into HTTP clients of the notes
    routes. The rules they exercised (upsert, allow_secret, allow_history,
    move, history) are covered by the direct edit.* checks above them.

Four extra checks at the end prove store mode without an export folder
(HELM_NOTES_ROOT unset: writes are revisions, nothing is written to disk).
"""
# temp-ok: the folder under test is a fixture this module creates and deletes
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VAULT = tempfile.mkdtemp(prefix="helm_test_store_vault_")
INDEX = tempfile.mkdtemp(prefix="helm_test_store_index_")
DB = os.path.join(INDEX, "notes.db")
os.environ["HELM_NOTES_PROFILE"] = "fixed"
os.environ["HELM_NOTES_MODE"] = "folder"
os.environ["HELM_NOTES_ROOT"] = VAULT
os.environ["HELM_NOTES_DB"] = DB
os.environ["HELM_DB"] = os.path.join(INDEX, "triage.db")
os.environ["HELM_DATA"] = INDEX

from notebook import cutover, edit, export, notes  # noqa: E402

export.QUARANTINE = os.path.join(INDEX, "quarantine")

TODAY = dt.date.today()
PAST = TODAY - dt.timedelta(days=2)
OLD = "Contacts/Old Name.md"
NEW = "Contacts/New Name.md"
REF = "Projects/Ongoing/Referrer.md"
PAST_REL = notes.daily_rel(PAST)
TODAY_REL = notes.daily_rel(TODAY)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def path(rel: str) -> str:
    return os.path.join(VAULT, rel.replace("/", os.sep))


def write(rel: str, text: str, newline: str = "\n") -> str:
    p = path(rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return p


def write_bytes(rel: str, data: bytes) -> str:
    p = path(rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


def raw(rel: str) -> bytes:
    with open(path(rel), "rb") as fh:
        return fh.read()


def store_text(con, rel: str) -> str:
    cur = notes.get_text(con, rel)
    return cur[0] if cur else None


def err(fn):
    try:
        fn()
    except edit.EditError as e:
        return e
    return None


def raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def seed():
    write("Home.md", "# Home\n\n[[Referrer]]\n")
    write(OLD, "---\ntype: contact\ncontact: Old Coop\nsid: old\n---\n# Old Name\n\n## Quirks\n\n- first quirk\n",
          newline="\r\n")
    write(REF, "# Referrer\n\nSee [[Old Name]] and [[Old Name#Quirks|the quirks]].\n\n- [ ] a task\n- [x] done task\n")
    write(PAST_REL, "# %s\n\nmentions [[Old Name]]\n" % PAST.isoformat())
    write("Reference/Plain.md", "# Plain\n\nno trailing newline")
    write_bytes("attachments/pic.png", b"\x89PNG\r\n\x1a\n")


def run() -> int:
    seed()
    con = notes.db()

    # --- folder mode -----------------------------------------------------------------------
    check("truth starts as folder", notes.truth(con) == "folder")
    rb = notes.rebuild(con, force=True)
    check("rebuild counted the seed", rb["notes"] == 5 and rb["files"] == 1, rb)
    t = store_text(con, OLD)
    check("text column filled, CRLF normalised", t is not None and "\r" not in t and t.startswith("---\ntype"))
    check("export.rebuild refuses in folder mode", raises(lambda: export.rebuild(con), RuntimeError))

    out = edit.upsert(con, "Reference/New Report.md", "# New Report\n\nhello", source="test")
    check("upsert creates a note", out.get("created") and os.path.isfile(path("Reference/New Report.md")))
    check("upsert adds the trailing newline", raw("Reference/New Report.md").endswith(b"hello\n"))
    out = edit.upsert(con, "Reference/New Report.md", "# New Report\n\nhello again\n", source="test")
    check("upsert saves an existing note", out.get("saved") is True)
    e = err(lambda: edit.upsert(con, "Loose.md", "x"))
    check("upsert refuses the root", e is not None and e.status == 400)
    e = err(lambda: edit.upsert(con, "Junk/x.md", "x"))
    check("upsert refuses an unknown folder", e is not None and e.status == 400)
    e = err(lambda: edit.upsert(con, "Reference/Untitled.md", "x"))
    check("upsert refuses Untitled", e is not None and e.status == 400)
    e = err(lambda: edit.upsert(con, "Reference/report.txt", "x"))
    check("upsert wants .md", e is not None and e.status == 400)
    e = err(lambda: edit.upsert(con, PAST_REL, "# rewritten\n"))
    check("past day refused without allow_history", e is not None and e.status == 403)
    out = edit.upsert(con, PAST_REL, "# %s\n\nmentions [[Old Name]]\n\ncall block\n" % PAST.isoformat(),
                      allow_history=True, source="processor")
    check("past day allowed with allow_history", out.get("saved") is True)
    e = err(lambda: edit.upsert(con, "Reference/Creds.md", "# c\n\npassword: hunter2\n"))
    check("new note with a credential refused", e is not None and e.status == 400 and e.extra.get("secret"))
    out = edit.upsert(con, "Reference/Creds.md", "# c\n\npassword: hunter2\n", allow_secret=True)
    check("allow_secret passes", out.get("created"))
    check("no revisions before the cutover", edit.history(con, OLD) == [])

    # --- the flip -----------------------------------------------------------------------------
    check("preflight passes without the quiet checks", cutover.preflight(con, quiet=False) == [],
          cutover.preflight(con, quiet=False))
    v = cutover.round_trip(con)
    check("round trip is clean", v["clean"], cutover.report(v))
    check("CRLF note is a line-endings difference", OLD in v["normalised"])
    n_notes = con.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
    zpath = cutover.backup(INDEX)
    check("backup zip written", os.path.isfile(zpath) and os.path.getsize(zpath) > 0)
    # W10: the backup follows no link. A file link in the folder to an outside file is not zipped.
    outside = os.path.join(INDEX, "outside-secret.txt")
    with open(outside, "wb") as fh:
        fh.write(b"OUTSIDE-BYTES-NOT-THE-WIKIS")
    os.symlink(outside, path("Reference/Linked.md"))
    zpath = cutover.backup(INDEX + "/linktest")
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
    check("backup follows no file link: the outside file's bytes are not in the zip (W10)",
          b"OUTSIDE-BYTES-NOT-THE-WIKIS" not in blob and "Reference/Linked.md" not in names, names)
    check("backup still holds the real notes, byte for byte",
          "Reference/Plain.md" in names and blob.count(raw("Reference/Plain.md")) >= 1, names)
    os.remove(path("Reference/Linked.md"))
    f = cutover.flip(con)
    check("flip made one revision per note", f["revisions"] == n_notes, f)
    check("truth is store", notes.truth(con) == "store")
    check("cutover_at recorded", bool(notes.get_meta(con, "cutover_at")))
    check("flip refuses twice", raises(lambda: cutover.flip(con), RuntimeError))
    rep = export.rebuild(con, adopt=False)
    check("CRLF file rewritten as LF", OLD in rep["written"] and b"\r" not in raw(OLD))
    check("byte-identical file left alone", "Reference/Plain.md" not in rep["written"])
    check("marker written", os.path.isfile(os.path.join(VAULT, export.MARKER_NAME)))
    v2 = export.verify(con)
    check("verify clean after the export", v2["same"] == n_notes and not v2["normalised"]
          and not v2["differ"] and not v2["missing"] and not v2["strays"], v2)
    check("notes.rebuild refuses in store mode", raises(lambda: notes.rebuild(con), RuntimeError))
    check("import_path refuses a note in store mode",
          raises(lambda: notes.import_path(con, path(OLD)), RuntimeError))
    check("history holds the import", edit.history(con, OLD)[0]["source"] == "import")
    check("preflight now reports the mode", any("already" in p for p in cutover.preflight(con, quiet=False)))

    # --- writes in store mode ---------------------------------------------------------------------
    r = edit.read(con, OLD)
    check("read comes from the store", r["text"] == store_text(con, OLD) and r["editable"])
    out = edit.save(con, OLD, r["text"] + "- second quirk\n", base_mtime=r["mtime"], source="test")
    check("save in store mode", out["saved"] is True)
    check("file follows the store", raw(OLD) == store_text(con, OLD).encode("utf-8"))
    h = edit.history(con, OLD)
    check("save made a revision", len(h) == 2 and h[0]["source"] == "test")
    e = err(lambda: edit.save(con, OLD, "x\n", base_mtime=r["mtime"]))
    check("stale mtime conflicts", e is not None and e.status == 409 and "text" in e.extra)
    out = edit.save(con, OLD, store_text(con, OLD), source="test")
    check("unchanged save is a no-op", out["saved"] is False and len(edit.history(con, OLD)) == 2)

    out = edit.append_daily(con, PAST, "appended line", source="test")
    check("append to a past day", raw(PAST_REL).endswith(b"appended line\n")
          and store_text(con, PAST_REL).endswith("appended line\n"))

    edit.append(con, "Reference/Born By Append.md", "- café line", create=True, source="test")
    row = con.execute("SELECT kind, length(CAST(added AS BLOB)) n FROM writes WHERE rel=? ORDER BY id DESC LIMIT 1",
                      ("Reference/Born By Append.md",)).fetchone()
    wrow = [w for w in notes.writes_since(con) if w["rel"] == "Reference/Born By Append.md"]
    check("store mode: an append that creates its note logs the added text (added_len for the straggler check)",
          row is not None and row["kind"] == "append" and row["n"] == len("- café line\n".encode("utf-8"))
          and wrow and wrow[-1]["added_len"] == len("- café line\n".encode("utf-8")), dict(row) if row else None)

    out = edit.new_note(con, "Contacts", "Brand New", source="test")
    check("new note exported", os.path.isfile(path("Contacts/Brand New.md")) and notes.exists(con, "Contacts/Brand New.md"))
    check("new note came from the template", "type: contact" in store_text(con, "Contacts/Brand New.md"))

    out = edit.toggle_task(con, REF, 5, source="test")
    check("toggle in store and file", out["done"] is True and "- [x] a task" in store_text(con, REF)
          and b"- [x] a task" in raw(REF))

    plan = edit.rename(con, OLD, "New Name", dry_run=True)
    check("rename plan counts the links", plan["total"] == 3 and any(c["daily"] for c in plan["changes"]), plan)
    done = edit.rename(con, OLD, "New Name", dry_run=False, source="test")
    check("rename done", done["done"] is True)
    check("old file gone, new file present", not os.path.exists(path(OLD)) and os.path.isfile(path(NEW)))
    check("store moved the note", notes.exists(con, NEW) and not notes.exists(con, OLD))
    check("referrer rewritten in store and file", "[[New Name]]" in store_text(con, REF)
          and b"[[New Name#Quirks|the quirks]]" in raw(REF))
    check("daily link rewritten", "[[New Name]]" in store_text(con, PAST_REL))
    h = edit.history(con, NEW)
    check("history followed the rename", any(x["source"].startswith("renamed from") for x in h)
          and any(x["source"] == "import" for x in h))
    check("revision text is readable", edit.revision(con, h[-1]["id"])["text"].startswith("---\ntype"))
    check("missing revision is a 404", err(lambda: edit.revision(con, 999999)).status == 404)

    # --- export rebuild policies ---------------------------------------------------------------------
    cur = notes.get_text(con, REF)
    p = write(REF, cur[0] + "\ndirect edit\n")
    os.utime(p, (time.time() + 30, time.time() + 30))
    rep = export.rebuild(con)
    check("newer file adopted", REF in rep["adopted"] and "direct edit" in store_text(con, REF), rep)
    check("adoption is a revision", edit.history(con, REF)[0]["source"] == "adopted from the folder")

    cur = notes.get_text(con, REF)
    p = write(REF, "older stale text\n")
    os.utime(p, (cur[1] - 100, cur[1] - 100))
    rep = export.rebuild(con)
    check("older file quarantined and overwritten", REF in rep["written"] and len(rep["quarantined"]) == 1
          and raw(REF) == cur[0].encode("utf-8"), rep)
    with open(rep["quarantined"][0], "rb") as fh:
        check("quarantine copy holds the stale text", fh.read() == b"older stale text\n")

    write("Systems/Stray.md", "# Stray\r\nline\r\n", newline="")
    rep = export.rebuild(con)
    check("stray in a notebook folder taken in", "Systems/Stray.md" in rep["adopted_new"]
          and notes.exists(con, "Systems/Stray.md") and b"\r" not in raw("Systems/Stray.md"))
    check("a new file alone needs no person", not export.needs_a_person(rep)
          and export.summary(rep) == "1 new files taken in")
    check("adopted stray resolves links to it",
          con.execute("SELECT COUNT(*) c FROM notes WHERE rel='Systems/Stray.md'").fetchone()["c"] == 1)

    write("Untitled.md", "x\n")
    write("Junk/thing.md", "x\n")
    rep = export.rebuild(con)
    check("root stray set aside", "Untitled.md" in rep["removed"] and not os.path.exists(path("Untitled.md")))
    check("unknown-folder stray set aside", "Junk/thing.md" in rep["removed"] and not os.path.exists(path("Junk/thing.md")))
    check("set-aside strays were quarantined", len(rep["quarantined"]) == 2 and export.needs_a_person(rep))

    out = edit.move(con, "Systems/Stray.md", "Reference/Stray.md", source="test")
    check("move in store mode", out["moved"] and notes.exists(con, "Reference/Stray.md")
          and not notes.exists(con, "Systems/Stray.md") and os.path.isfile(path("Reference/Stray.md"))
          and not os.path.exists(path("Systems/Stray.md")))
    check("move is a revision", edit.history(con, "Reference/Stray.md")[0]["source"] == "moved from Systems/Stray.md")
    e = err(lambda: edit.move(con, "Reference/Stray.md", "Junk/Stray.md"))
    check("move refuses an unknown folder", e is not None and e.status == 400)
    e = err(lambda: edit.move(con, "Reference/Stray.md", REF))
    check("move refuses an occupied path", e is not None and e.status == 409)
    check("move leaves the folder in agreement", export.verify(con)["strays"] == [] and export.verify(con)["missing"] == [])
    out = edit.move(con, "Reference/Stray.md", "Team/Stray.md", source="test", redirect=True)
    rd = edit.read(con, "Reference/Stray.md")
    check("a store-mode move with redirect: the old path reads as the moved note",
          out["redirect"] and rd["rel"] == "Team/Stray.md" and rd["redirected_from"] == "Reference/Stray.md", rd)
    check("a store-mode move with redirect: a link to the old path resolves to the new one",
          notes.resolve_note(con, "Reference/Stray") == "Team/Stray.md")
    check("a redirect is not a note: the folder still agrees with the store",
          export.verify(con)["strays"] == [] and export.verify(con)["missing"] == []
          and not notes.exists(con, "Reference/Stray.md"))

    os.remove(path("Contacts/Brand New.md"))
    rep = export.rebuild(con)
    check("missing file rewritten", "Contacts/Brand New.md" in rep["written"] and os.path.isfile(path("Contacts/Brand New.md")))

    write_bytes("attachments/new.png", b"\x89PNG\r\n\x1a\nnew")
    os.remove(path("attachments/pic.png"))
    rep = export.rebuild(con)
    check("files table follows the folder", rep["files"]["added"] == 1 and rep["files"]["removed"] == 1
          and notes.resolve_file(con, "new.png") == "attachments/new.png"
          and notes.resolve_file(con, "pic.png") is None, rep["files"])

    v3 = export.verify(con)
    check("verify clean at the end", not v3["normalised"] and not v3["differ"] and not v3["missing"]
          and not v3["strays"], v3)
    check("summary reads well", export.summary({"written": [], "adopted": []}) == "folder agrees with the store"
          and export.summary({"written": ["a"], "adopted": ["b", "c"]}) == "1 written, 2 adopted")

    # --- a path spelled with empty or "." parts is stored canonically, so the export can write it ----
    for i, bad in enumerate(("Contacts//Evil.md", "Contacts/./Evil.md", "Contacts\\.\\\\Evil.md", "./Contacts/Evil.md")):
        out = edit.upsert(con, bad, "# Evil %d\n" % i, source="test")
        check("upsert of %r lands on the canonical rel" % bad, out.get("rel") == "Contacts/Evil.md", out.get("rel"))
    stored = [r[0] for r in con.execute("SELECT rel FROM notes WHERE rel LIKE 'Contacts/%Evil%' OR rel LIKE '%//%' "
                                        "OR rel LIKE '%/./%'")]
    check("one note in the store, under the canonical rel", stored == ["Contacts/Evil.md"], stored)
    check("that note is on disk where the store says", os.path.exists(path("Contacts/Evil.md")))
    check("export verify has nothing missing for it", "Contacts/Evil.md" not in
          [m if isinstance(m, str) else str(m) for m in export.verify(con).get("missing", [])])
    check("a dot-only or empty path is still a 400",
          all(getattr(err(lambda b=b: edit.norm_rel(b)), "status", None) == 400 for b in (".", "//", "./", "/./")))
    check("a .. part is still a 400", err(lambda: edit.upsert(con, "Contacts/../X.md", "x", source="test")).status == 400)
    check("a surrounding slash is still trimmed", edit.norm_rel("/Contacts/Fine.md/") == "Contacts/Fine.md")

    # --- store mode with no export folder (HELM_NOTES_ROOT unset) -----------------------------------
    os.environ["HELM_NOTES_MODE"] = "store"
    saved_vault = notes.VAULT
    notes.VAULT = ""
    con2 = notes.db(os.path.join(INDEX, "bare.db"))
    check("a fresh DB in store mode is the truth with nothing to flip", notes.truth(con2) == "store")
    out = edit.upsert(con2, "Reference/Only In Store.md", "# Only In Store\n", source="test")
    check("a write without a folder is a revision and nothing else",
          out.get("created") and edit.history(con2, "Reference/Only In Store.md")[0]["source"] == "test"
          and not os.path.exists(os.path.join(saved_vault, "Reference", "Only In Store.md")))
    check("export is disabled and says so",
          not export.enabled() and export.write_note("x.md", "x") is None
          and raises(lambda: export.rebuild(con2), RuntimeError))
    e = err(lambda: edit.attach(con2, "a.png", b"x"))
    check("an attachment without a folder is a 503, not a crash", e is not None and e.status == 503)
    con2.close()
    notes.VAULT = saved_vault
    os.environ["HELM_NOTES_MODE"] = "folder"

    con.close()
    shutil.rmtree(VAULT, ignore_errors=True)
    shutil.rmtree(INDEX, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
