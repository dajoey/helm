"""The one-way export: the store's notes projected onto the folder. the reference's
app/export.py, ported.

In store mode the folder (HELM_NOTES_ROOT) is a projection. Every write
through edit.py lands here as a file at once; rebuild() makes the whole
folder agree with the store again, and verify() says where it does not.
Non-note files (images, PDFs, the odd zip) are not projected: they live in
the folder and the store only lists them.

A file that differs from the store is the store's to overwrite, but its text
is kept first under $HELM_DATA/quarantine so a direct edit is never simply
lost. A note file the store does not know is adopted as a new revision when
it sits in one of the notebook's folders, and quarantined when it does not.

The folder is optional in store mode: with no HELM_NOTES_ROOT every function
here is a no-op that says so (enabled() is False).

    python3 -m notebook.export --check
    python3 -m notebook.export --rebuild
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time

from . import config, notes

MARKER_NAME = "NOTEBOOK_EXPORT_README.txt"
MARKER_TEXT = """This folder is an export.

The notebook's notes live in Helm's note store and are written here, one file
per note, after every change. Editing a file here changes nothing: the store
wins at the next check and the edited copy is set aside under the data
folder's quarantine directory. Open the notebook in Helm to edit a note, or
write through the notes API from a script.

Images and other non-note files in this folder are not exported; they live
here and nowhere else.
"""
QUARANTINE = os.path.join(config.data_dir(), "quarantine")
VERIFY_EVERY_S = int(os.environ.get("HELM_NOTES_VERIFY_EVERY_S") or 300)
LAST_VERIFY: dict | None = None
_verify_thread: threading.Thread | None = None


def enabled() -> bool:
    """Is there a folder to export to? Store mode without HELM_NOTES_ROOT has none."""
    return bool(notes.VAULT)


# --- Files ---------------------------------------------------------------------------------
def atomic_write(path: str, data: bytes) -> None:
    """notes.atomic_write: a temp file renamed over the target, keeping the file's mode
    and owner (the export writes the folder other writers share once home is the store)."""
    notes.atomic_write(path, data, hidden_ok=True)


def write_note(rel: str, text: str):
    """The note's text, byte for byte, at its path in the folder. None when
    there is no folder."""
    if not enabled():
        return None
    path = notes.write_target(rel, hidden_ok=True)
    if path is None:
        print("export: %s leads out of the notes folder; not written" % rel, flush=True)
        return None
    atomic_write(path, text.encode("utf-8"))
    return path


def remove_note(rel: str) -> bool:
    if not enabled():
        return False
    return notes.unlink_rel(rel, hidden_ok=True)


def write_marker() -> str:
    path = os.path.join(notes.VAULT, MARKER_NAME)
    got = notes.read_bytes_confined(MARKER_NAME, hidden_ok=True)
    if got is None or got[0] != MARKER_TEXT.encode("utf-8"):
        atomic_write(path, MARKER_TEXT.encode("utf-8"))
    return path


def quarantine(rel: str) -> str:
    """Copy a folder file into quarantine/<stamp>/<rel> and return the copy's path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(QUARANTINE, stamp, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    got = notes.read_bytes_confined(rel, hidden_ok=True)   # never a copy through a link (W10)
    with open(dest, "wb") as fh:
        fh.write(got[0] if got else b"")
    if got:
        os.utime(dest, (got[1], got[1]))
    return dest


# --- Comparing --------------------------------------------------------------------------------
def disk_bytes(rel: str):
    """The bytes at a note's path, or None when there is no file there the
    notebook may read: missing, or a link out of the folder or onto a hidden
    path. The note's own name may be hidden (the store chose it); a link from it
    may not lead anywhere hidden. A note whose file is such a link reads as
    missing, and the rebuild puts a plain file with the store's text back."""
    got = notes.read_bytes_confined(rel, hidden_ok=True)
    return None if got is None else got[0]


def normalised(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n")


def classify(store_text: str, raw) -> str:
    """same: identical bytes. normalised: the same note once line endings and
    a BOM are ignored. differ: different text. missing: no file."""
    if raw is None:
        return "missing"
    if raw == store_text.encode("utf-8"):
        return "same"
    if normalised(raw) == store_text:
        return "normalised"
    return "differ"


def folder_notes() -> set:
    """Every note path on disk the notebook may read: outside the skipped folders,
    nothing hidden, no link out of the folder or onto a hidden path
    (notes.confined_path). Anything else is never a stray to adopt or quarantine."""
    out = set()
    if not enabled() or not os.path.isdir(notes.VAULT):
        return out
    for dirpath, dirnames, filenames, _dfd in notes.walk_folder():
        notes.prune_walk(dirpath, dirnames, notes.VAULT)
        for fn in filenames:
            if fn.lower().endswith(".md"):
                rel = notes.relpath(os.path.join(dirpath, fn))
                if notes.confined_path(rel) is not None:
                    out.add(rel)
    return out


def verify(con) -> dict:
    """Where the folder disagrees with the store. Read-only."""
    rows = con.execute("SELECT rel, text FROM notes").fetchall()
    rep = {"same": 0, "normalised": [], "differ": [], "missing": [], "strays": []}
    seen = set()
    for r in rows:
        seen.add(r["rel"])
        c = classify(r["text"], disk_bytes(r["rel"]))
        if c == "same":
            rep["same"] += 1
        else:
            rep[c].append(r["rel"])
    rep["strays"] = sorted(folder_notes() - seen)
    for k in ("normalised", "differ", "missing"):
        rep[k].sort()
    return rep


def adoptable(rel: str) -> bool:
    """A stray note is taken into the store only from one of the notebook's
    folders. Anything else (a new top-level folder, an Untitled at the root) is
    the contract's holding pen and is set aside instead."""
    return notes.folder_of(rel) in notes.folders_allowed()


# --- Rebuild --------------------------------------------------------------------------------
NEWER_S = 2.0


def file_is_newer(con, rel: str) -> bool:
    """A folder file written after the store's revision, by more than the gap
    between a revision and its own export."""
    cur = notes.get_text(con, rel)
    try:
        st = os.stat(notes.abspath(rel))
    except OSError:
        return False
    return cur is not None and st.st_mtime > cur[1] + NEWER_S


def adopt_file(con, rel: str, source: str = "adopted from the folder") -> bool:
    """The folder file's text becomes the note's current revision, and the
    file is rewritten only when its bytes were not already normalised."""
    raw = disk_bytes(rel)
    if raw is None:
        return False
    text = normalised(raw)
    notes.write_text(con, rel, text, source)
    if raw != text.encode("utf-8"):
        write_note(rel, text)
    return True


def rebuild(con, adopt: bool = True) -> dict:
    """Make the folder agree with the store. Store mode only.

    A note whose file is missing or only differs in line endings is written
    from the store. A file whose text differs is adopted as a new revision when
    it is newer than the store's, and quarantined then overwritten when it is
    not. A stray note file is adopted when it sits in a notebook folder, and
    quarantined and removed when it does not.
    """
    if not notes.store_is_truth(con):
        raise RuntimeError("the folder is truth; nothing to export until the cutover")
    if not enabled():
        raise RuntimeError("no export folder: HELM_NOTES_ROOT is not set")
    t0 = time.time()
    rep = verify(con)
    out = {"written": [], "quarantined": [], "adopted": [], "adopted_new": [], "removed": [],
           "same": rep["same"]}
    for rel in rep["missing"] + rep["normalised"]:
        write_note(rel, notes.get_text(con, rel)[0])
        out["written"].append(rel)
    for rel in rep["differ"]:
        if adopt and file_is_newer(con, rel):
            adopt_file(con, rel)
            out["adopted"].append(rel)
        else:
            out["quarantined"].append(quarantine(rel))
            write_note(rel, notes.get_text(con, rel)[0])
            out["written"].append(rel)
    for rel in rep["strays"]:
        if adopt and adoptable(rel):
            if adopt_file(con, rel):
                out["adopted_new"].append(rel)
        else:
            out["quarantined"].append(quarantine(rel))
            remove_note(rel)
            out["removed"].append(rel)
    out["files"] = sync_files(con)
    write_marker()
    out["seconds"] = round(time.time() - t0, 2)
    return out


def sync_files(con) -> dict:
    """The files table follows the folder, which is where non-note files live
    in both modes."""
    seen = set()
    added = 0
    if enabled() and os.path.isdir(notes.VAULT):
        for dirpath, dirnames, filenames, _dfd in notes.walk_folder():
            notes.prune_walk(dirpath, dirnames, notes.VAULT)
            for fn in filenames:
                if fn.lower().endswith(".md") or fn == MARKER_NAME or fn.startswith(".helm-"):
                    continue
                p = os.path.join(dirpath, fn)
                out = notes.import_path(con, p, resolve=False)
                if out is None:
                    continue   # hidden, a link out of the folder or onto a hidden path, or gone
                seen.add(notes.relpath(p))
                if not out.get("unchanged"):
                    added += 1
    removed = 0
    for r in con.execute("SELECT rel FROM files").fetchall():
        if r["rel"] not in seen:
            con.execute("DELETE FROM files WHERE rel=?", (r["rel"],))
            removed += 1
    if added or removed:
        notes.resolve_links(con)
    con.commit()
    return {"files": len(seen), "added": added, "removed": removed}


def summary(rep: dict) -> str:
    """One line for a log or a card."""
    parts = []
    for k, label in (("written", "written"), ("adopted", "adopted"), ("adopted_new", "new files taken in"),
                     ("quarantined", "quarantined"), ("removed", "removed")):
        if rep.get(k):
            parts.append("%d %s" % (len(rep[k]), label))
    return ", ".join(parts) if parts else "folder agrees with the store"


def needs_a_person(rep: dict) -> bool:
    """A differing file adopted, or anything set aside: somebody wrote into the
    folder behind the store. A new file in a notebook folder is routine."""
    return bool(rep.get("adopted") or rep.get("quarantined") or rep.get("removed"))


# --- The periodic check (store mode, the API starts it) ------------------------------------------
def _card_export_changes(rep: dict) -> None:
    """One Helm thread when a verify pass found a note changed or added behind
    the store's back, so the writer that did it gets found and repointed."""
    if not needs_a_person(rep):
        return
    from . import helm_adapter
    lines = []
    for k, label in (("adopted", "adopted, the file was newer"), ("quarantined", "quarantined"),
                     ("removed", "removed from the folder")):
        for item in rep.get(k) or []:
            lines.append("%s: %s" % (label, item))
    try:
        helm_adapter.new_thread({
            "title": "Notebook: a note was written in the folder behind the store (%s)" % summary(rep),
            "summary": "Something wrote note files into the export folder directly. A file newer than "
                       "the store's revision was taken in as the note's newest version; anything else "
                       "was copied to the quarantine folder and rewritten from the store. Find the "
                       "writer and point it at the notes API.\n\n" + "\n".join(lines[:40]),
            "category": "Notebook", "urgency": "whenever", "intent": "mine",
        }, actor="helm-notebook")
    except Exception as e:  # noqa: BLE001
        print("notebook: could not file the export card: %s" % e, flush=True)


def verify_once() -> dict | None:
    """One export check now. Store mode with a folder only; else None."""
    global LAST_VERIFY
    if config.mode() != "store" or not enabled():
        return None
    con = notes.db()
    try:
        if not notes.store_is_truth(con):
            return None
        rep = rebuild(con)
    finally:
        con.close()
    LAST_VERIFY = {"at": time.time(), "summary": summary(rep), "same": rep["same"]}
    if summary(rep) != "folder agrees with the store":
        print("notebook: export %s" % summary(rep), flush=True)
        _card_export_changes(rep)
    return rep


def _verify_loop() -> None:
    while True:
        time.sleep(VERIFY_EVERY_S)
        try:
            verify_once()
        except Exception as e:  # noqa: BLE001
            print("notebook: verify failed: %s: %s" % (type(e).__name__, e), flush=True)


def start_verify() -> threading.Thread | None:
    """The export check at start and every VERIFY_EVERY_S on a daemon thread.
    Idempotent. Nothing in the folder modes or without a folder."""
    global _verify_thread
    if config.mode() != "store" or not enabled():
        return None
    if _verify_thread is not None and _verify_thread.is_alive():
        return _verify_thread
    try:
        verify_once()
    except Exception as e:  # noqa: BLE001
        print("notebook: first verify failed: %s: %s" % (type(e).__name__, e), flush=True)
    _verify_thread = threading.Thread(target=_verify_loop, name="helm-notes-export-verify",
                                      daemon=True)
    _verify_thread.start()
    return _verify_thread


if __name__ == "__main__":
    import json
    import sys
    c = notes.db()
    if "--rebuild" in sys.argv:
        print(json.dumps(rebuild(c), indent=1))
    else:
        v = verify(c)
        print("truth:", notes.truth(c))
        print("same %d, normalised %d, differ %d, missing %d, strays %d"
              % (v["same"], len(v["normalised"]), len(v["differ"]), len(v["missing"]), len(v["strays"])))
        for k in ("differ", "missing", "strays"):
            for rel in v[k]:
                print("  %s: %s" % (k, rel))
        if "--verbose" in sys.argv:
            for rel in v["normalised"]:
                print("  normalised: %s" % rel)
