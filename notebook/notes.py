"""The note store: every note's text, its revisions, and the index over them
(frontmatter, wikilinks, checkbox lines, FTS5). the reference's app/notes.py, ported.

Three truths, chosen by HELM_NOTES_MODE (notebook/config.py):

- `store`: the store is truth. Writes come in through write_text() as
  revisions, and export.py projects them onto the folder. import_path()
  refuses a note unless told the caller means it (export.py adopting a stray).
- `folder-readonly` (home): the wiki folder is truth and nothing here writes
  it. The folder is indexed into this DB in full at start (rebuild) and
  rescanned by mtime every 60 s (start_rescan / rescan_if_due). Every write
  is refused in edit.py.
- `folder` (the reference's phase 1, kept for the ported suites and the cutover's
  reversal): the folder is truth and the editor writes files, which are
  re-indexed at once.

Inside `folder` and `store` the meta row `truth` still says which of the two
applies, so a the reference app notes.db carries its own state across the cutover.

    from notebook import notes
    con = notes.db()
    notes.truth(con)
    notes.search(con, "bluestem printing")
"""
from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time

from . import config

VAULT = config.root()
DB_PATH = config.db_path()

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}

WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\|#\n]+)(#[^\]\|\n]*)?(\|[^\]\n]*)?\]\]")
# A checkbox line as the renderer draws it: a bullet or a numbered marker (1. or 1)), optionally
# inside block quotes (> or >>). The toggle in edit.py shares TASK_MARKER so the two cannot drift.
TASK_MARKER = r"\s*(?:>\s*)*(?:[-*+]|\d{1,9}[.)])\s+"
TASK_RE = re.compile(r"^" + TASK_MARKER + r"\[( |x|X)\]\s+(.*)$")
HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")
DAILY_RE = config.DAILY_RE
FM_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
# the wiki engine's position and anchor suffixes on a link target: [[Page@123]], [[Page$id]].
SB_SUFFIX_RE = re.compile(r"[@$][^/]*$")

DDL = """
CREATE TABLE IF NOT EXISTS notes (
  rel         TEXT PRIMARY KEY,
  folder      TEXT NOT NULL,
  stem        TEXT NOT NULL,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  frontmatter TEXT NOT NULL DEFAULT '{}',
  mtime       REAL NOT NULL,
  size        INTEGER NOT NULL,
  imported_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder);
CREATE INDEX IF NOT EXISTS idx_notes_stem ON notes(stem COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_notes_mtime ON notes(mtime);
-- Covers the graph's per-folder scan (folder, mtime) without reading note bodies.
CREATE INDEX IF NOT EXISTS idx_notes_folder_mtime ON notes(folder, mtime);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title, body, content='notes', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
  INSERT INTO notes_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;

CREATE TABLE IF NOT EXISTS links (
  src        TEXT NOT NULL,
  target     TEXT NOT NULL,
  target_rel TEXT,
  heading    TEXT NOT NULL DEFAULT '',
  alias      TEXT NOT NULL DEFAULT '',
  embed      INTEGER NOT NULL DEFAULT 0,
  line       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_rel);

CREATE TABLE IF NOT EXISTS tasks (
  rel  TEXT NOT NULL,
  line INTEGER NOT NULL,
  done INTEGER NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_rel ON tasks(rel);

CREATE TABLE IF NOT EXISTS files (
  rel   TEXT PRIMARY KEY,
  name  TEXT NOT NULL,
  size  INTEGER NOT NULL,
  mtime REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS revisions (
  id     INTEGER PRIMARY KEY,
  rel    TEXT NOT NULL,
  ts     REAL NOT NULL,
  source TEXT NOT NULL,
  text   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_rel ON revisions(rel, ts);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Every write Helm makes, in every mode (the wiki bridge, Projects/Wiki to Helm Notebook 2026-09).
-- While the folder is the truth, other writers (other writers) change it too;
-- a folder file whose sha differs from its last row here was changed by one of them, which is
-- what the straggler check reads before the flip. prev_text keeps what a rewrite replaced.
CREATE TABLE IF NOT EXISTS writes (
  id         INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  rel        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT '',
  principal  TEXT NOT NULL DEFAULT '',
  size_after INTEGER,
  sha_after  TEXT,
  prev_size  INTEGER,
  prev_text  TEXT,
  added      TEXT
);
CREATE INDEX IF NOT EXISTS idx_writes_ts ON writes(ts);
CREATE INDEX IF NOT EXISTS idx_writes_rel ON writes(rel, ts);

-- A moved note's old path (the notebook organizer, 2026-09-26). The old path keeps working: a
-- wikilink to it resolves to the new one, a read of it serves the new note, a write to it lands
-- in the new note. Only while no note is at the old path: a real note always wins. Not a note,
-- so no list, search, count or export ever shows it; `rebuild` never touches this table.
CREATE TABLE IF NOT EXISTS redirects (
  src    TEXT PRIMARY KEY,
  dst    TEXT NOT NULL,
  ts     REAL NOT NULL,
  source TEXT NOT NULL DEFAULT ''
);
"""


# --- Connection ---------------------------------------------------------------
def db(path: str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(DDL)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(notes)")}
    if "text" not in cols:
        con.execute("ALTER TABLE notes ADD COLUMN text TEXT NOT NULL DEFAULT ''")
        con.commit()
    return con


# --- Mode -----------------------------------------------------------------------
def get_meta(con, key: str, default: str | None = None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(con, key: str, value: str) -> None:
    con.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET"
                " value=excluded.value", (key, value))
    con.commit()


def mode() -> str:
    return config.mode()


def truth(con) -> str:
    """'folder-readonly' at home; otherwise the meta row: 'folder' until the cutover and
    'store' after it. A fresh DB in store mode starts as store (nothing to flip)."""
    m = mode()
    if m == "folder-readonly":
        return m
    return get_meta(con, "truth", "store" if m == "store" else "folder")


def store_is_truth(con) -> bool:
    return truth(con) == "store"


def read_only(con=None) -> bool:
    return mode() == "folder-readonly"


# --- Paths --------------------------------------------------------------------
def walk_folder():
    """os.fwalk(follow_symlinks=False) over the notes folder (W11): each folder opened from its
    parent's fd, never through a link. It starts at the root's real path (fwalk will not start
    through a link, and HELM_NOTES_ROOT may be one) and spells every dirpath under VAULT again,
    so relpath() holds. dirnames may be pruned in place, as with os.walk."""
    real = os.path.realpath(VAULT)
    for dirpath, dirnames, filenames, dfd in os.fwalk(real, follow_symlinks=False):
        yield VAULT + dirpath[len(real):], dirnames, filenames, dfd


def relpath(abspath: str) -> str:
    return os.path.relpath(abspath, VAULT).replace("\\", "/")


def abspath(rel: str) -> str:
    return os.path.join(VAULT, rel.replace("/", os.sep))


def confined_path(rel: str, hidden_ok: bool = False):
    """The real path of a file inside the notes folder, or None when the notebook
    may not read it: an empty or absolute path, a `..` or `.` part, a hidden path
    (hidden()), or a real path (symlinks resolved) that is outside the folder's
    real path or hidden inside it (a visible name linked onto .chrome-data/ is as
    hidden as the folder). Every read of a file under the folder goes through
    here: the index (import_path), the attachment route, the editor's read in
    folder mode, and the export's comparison and adoption.

    hidden_ok is the export reading the store's own note by the name the store
    gave it: that name may be hidden (the store accepted it), but a link from it
    may still not lead anywhere hidden or out of the folder."""
    if not VAULT or not rel or "\x00" in rel:
        return None
    clean = rel.replace("\\", "/")
    if clean.startswith("/") or re.match(r"^[A-Za-z]:", clean):
        return None
    parts = clean.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if not hidden_ok and hidden(clean):
        return None
    try:
        root = os.path.realpath(VAULT)
        literal = os.path.join(root, *parts)
        p = os.path.realpath(literal)
    except OSError:
        return None   # a name changing under realpath (EINVAL/ENOENT mid-flip) is not a page now
    if p != literal:
        return None   # W10: a link anywhere in the path is not the wiki's, inside or out
    if not p.startswith(root + os.sep):
        return None
    if (p != literal or not hidden_ok) and hidden(os.path.relpath(p, root).replace(os.sep, "/")):
        return None
    # realpath resolves symlinks, not hard links: a name inside the folder can be the
    # same inode as a hidden file, or as a readable file outside it. A regular file
    # with more than one link is not the wiki's (found by the merge's adversarial
    # verifier, which served .wiki-engine.auth.json through one).
    try:
        st = os.stat(p)
    except OSError:
        return p   # nothing there: the caller answers for a missing file
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        return None
    return p


def write_target(rel: str, hidden_ok: bool = False):
    """Where a write of `rel` may land: the literal path under the folder, or None when
    it would leave the folder. A write lands by renaming a sibling temp file over the
    name (or, for an append, only after confined_path passed), so the name itself is
    replaced, never followed: a link sitting at the name is overwritten as a plain file
    (the export's rule for a note whose file became a link out). What must hold is the
    way down: the nearest folder that already exists above the name must resolve inside
    the folder's real path and not be hidden, so a planted `Docker/x -> /anywhere` never
    receives a file. Every write into the folder asks here: the editor's commit,
    append's create, a move's destination, delete's Trash, attachments, and the store's
    export."""
    if not VAULT or not rel or "\x00" in rel:
        return None
    clean = rel.replace("\\", "/")
    if clean.startswith("/") or re.match(r"^[A-Za-z]:", clean):
        return None
    parts = clean.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if not hidden_ok and hidden(clean):
        return None
    try:
        root = os.path.realpath(VAULT)
        literal = os.path.join(root, *parts)
        cur = os.path.dirname(literal)
        while not os.path.lexists(cur) and cur != root and len(cur) > len(root):
            cur = os.path.dirname(cur)
        real = os.path.realpath(cur)
    except OSError:
        return None   # mid-flip: not a place to write now
    if real != root and not real.startswith(root + os.sep):
        return None
    if real != root and hidden(os.path.relpath(real, root).replace(os.sep, "/") + "/x"):
        return None
    return literal


def in_skipped_dir(rel: str) -> bool:
    prof = config.profile()
    dirs = rel.split("/")[:-1]
    if dirs and dirs[0] in prof.skip_top:
        return True
    return any(part in prof.skip_dirs or part.startswith(".") for part in dirs)


def prune_walk(dirpath: str, dirnames: list, root: str) -> None:
    """os.walk's dirnames, pruned in place to the folders the profile indexes: no skipped or
    dot folder at any depth, and no top-level folder in skip_top."""
    prof = config.profile()
    top = os.path.normpath(dirpath) == os.path.normpath(root)
    dirnames[:] = [d for d in dirnames if d not in prof.skip_dirs and not d.startswith(".")
                   and not (top and d in prof.skip_top)]


def hidden(rel: str) -> bool:
    """A path the notebook never indexes or serves from the folder: any part, a
    folder or the file itself, whose name starts with "." (.chrome-data/,
    .wiki-engine.auth.json, .git/, Personal/.cache/), or a folder the
    profile skips (_plug/, node_modules/ ...)."""
    clean = rel.replace("\\", "/")
    return in_skipped_dir(clean) or clean.rsplit("/", 1)[-1].startswith(".")


def folder_of(rel: str) -> str:
    return config.profile().folder_of(rel)


def folders_allowed() -> list:
    """Where a note may be filed: the profile's list or the folders present in the
    wiki root (home)."""
    return config.profile().folders_present(VAULT)


def topic_folders() -> tuple:
    return tuple(config.profile().topic_folders)


def daily_rel(d) -> str:
    return config.profile().daily_rel(d)


def is_daily(rel: str) -> bool:
    return config.profile().is_daily(rel)


def stem_of(rel: str) -> str:
    return os.path.splitext(rel.rsplit("/", 1)[-1])[0]


# --- Parsing ------------------------------------------------------------------
def read_text(path: str, retry_empty: bool = False) -> str:
    """Read a note that an editor may be writing at this moment. A locked file
    is retried, never treated as gone. With retry_empty, a zero-byte read is
    retried too, which a watcher wants (a save truncates before it writes)
    and a full walk does not (some notes really are empty)."""
    last = None
    for attempt in range(3):
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            last = e
            if os.name != "nt":
                break  # a POSIX open() error is not an editor lock; retrying only delays the walk
            time.sleep(0.2 * (attempt + 1))
            continue
        if not raw and retry_empty and attempt < 2:
            time.sleep(0.2 * (attempt + 1))
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n")
    if last:
        raise last
    return ""


def split_frontmatter(text: str):
    """(frontmatter dict, body). Values stay raw strings; a list continued on
    indented lines is joined into one."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fm = {}
    key = None
    for ln in text[4:end].split("\n"):
        m = FM_KEY_RE.match(ln)
        if m:
            key = m.group(1)
            fm[key] = m.group(2).strip()
        elif key is not None and ln.startswith((" ", "\t", "-")):
            fm[key] = (fm[key] + " " + ln.strip()).strip()
    return fm, text[end + 5:]


def scan(body: str):
    """(title, links, tasks) from a note body, skipping fenced code."""
    links, tasks = [], []
    fence = None
    title = None
    for i, ln in enumerate(body.split("\n"), 1):
        s = ln.strip()
        if fence:
            if s.startswith(fence):
                fence = None
            continue
        if s.startswith("```") or s.startswith("~~~"):
            fence = s[:3]
            continue
        if title is None:
            m = HEADING_RE.match(ln)
            if m:
                title = m.group(1)
        m = TASK_RE.match(ln)
        if m:
            tasks.append((i, m.group(1) != " ", m.group(2).strip()))
        for m in WIKILINK_RE.finditer(ln):
            links.append({"embed": bool(m.group(1)), "target": m.group(2).strip(),
                          "heading": (m.group(3) or "")[1:].strip(),
                          "alias": (m.group(4) or "")[1:].strip(), "line": i})
    return title, links, tasks


def is_file_target(target: str) -> bool:
    ext = os.path.splitext(target.strip())[1].lower()
    return bool(ext) and ext != ".md"


# --- Resolution ---------------------------------------------------------------
def _clean_target(target: str, strip_suffix: bool = True) -> str:
    t = target.strip().replace("\\", "/").strip("/")
    if strip_suffix:
        t = SB_SUFFIX_RE.sub("", t)
    if t.lower().endswith(".md"):
        t = t[:-3]
    return t


def _target_forms(target: str) -> list:
    """The names a wikilink target may mean, best first: the name as written (a note may be
    called owner@example.com or Budget $5), then with the wiki engine's @pos/$anchor suffix dropped."""
    forms = []
    for strip in (False, True):
        t = _clean_target(target, strip)
        if t and t not in forms:
            forms.append(t)
    return forms


def resolve_note(con, target: str):
    """A wikilink target to a vault-relative path, or None. A path ([[Path/Page]],
    the wiki engine's form) matches the note at that path, else by suffix; a bare
    name ([[Page]]) matches any note with that stem, shortest path first."""
    forms = _target_forms(target)
    for t in forms:
        if "/" in t:
            # The suffix is matched literally: a target (a thread's source id, topic.refs) can
            # hold % or _, which LIKE would otherwise read as wildcards.
            lit = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            row = con.execute(
                "SELECT rel FROM notes WHERE lower(rel)=lower(?) OR lower(rel) LIKE lower(?) ESCAPE '\\'"
                " ORDER BY length(rel) LIMIT 1", (t + ".md", "%/" + lit + ".md")).fetchone()
        else:
            row = con.execute(
                "SELECT rel FROM notes WHERE stem=? COLLATE NOCASE ORDER BY length(rel) LIMIT 1",
                (t,)).fetchone()
        if row:
            return row["rel"]
    for t in forms:
        hit = redirect_for_target(con, t)
        if hit:
            return hit
    return None


# --- Redirects (moved notes) --------------------------------------------------
REDIRECT_HOPS = 5


def _redirect_rows(con, sql: str, args: tuple) -> list:
    """Rows of the redirects table; none where it does not exist (a bare test index)."""
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def _note_at(con, rel: str) -> bool:
    return con.execute("SELECT 1 FROM notes WHERE rel=?", (rel,)).fetchone() is not None


def follow_redirect(con, rel: str):
    """Where a moved note's old path leads now (following a chain of moves), or None when `rel`
    is not a moved path. The path must match exactly, case included (the home folder is
    case-sensitive: Memory/dreams.md is not Memory/Dreams.md). Callers check first that no note
    is at `rel` (a real note wins) and, before writing, that a note is at the answer."""
    seen, cur = {rel}, rel
    for _ in range(REDIRECT_HOPS):
        rows = _redirect_rows(con, "SELECT dst FROM redirects WHERE src=?", (cur,))
        if not rows:
            break
        cur = rows[0]["dst"]
        if cur in seen:
            return None           # a cycle: nothing sensible to serve
        seen.add(cur)
    return None if cur == rel else cur


def redirect_for_target(con, t: str):
    """A cleaned wikilink target that matches no note, matched against moved paths by the same
    rules as notes (exact path or path suffix; a bare name by file name), to the note it moved
    to, or None. Only a destination that is a note now counts."""
    if not t:
        return None
    lit = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = _redirect_rows(
        con, "SELECT src FROM redirects WHERE lower(src)=lower(?) OR lower(src) LIKE lower(?) ESCAPE '\\'"
             " ORDER BY length(src) LIMIT 1", (t + ".md", "%/" + lit + ".md"))
    if not rows:
        return None
    dst = follow_redirect(con, rows[0]["src"])
    return dst if dst and _note_at(con, dst) else None


def moved_file(con, rel: str):
    """Where a moved file's old path leads now (edit.move_file), when a file is there, or None."""
    dst = follow_redirect(con, rel)
    if not dst:
        return None
    row = con.execute("SELECT rel FROM files WHERE lower(rel)=lower(?)", (dst,)).fetchone()
    return row["rel"] if row else None


def add_redirect(con, src: str, dst: str, source: str = "") -> None:
    """`src` moved to `dst`. Older moves that led to `src` now lead to `dst` (no chains to walk);
    a redirect from `dst` is dropped, since a note is there now."""
    con.execute("DELETE FROM redirects WHERE src=?", (dst,))
    con.execute("UPDATE redirects SET dst=? WHERE dst=?", (dst, src))
    con.execute("INSERT OR REPLACE INTO redirects (src, dst, ts, source) VALUES (?,?,?,?)",
                (src, dst, time.time(), (source or "")[:80]))


def prune_empty_dirs(rel: str) -> list:
    """Remove the folders above `rel` that are empty now, deepest first, down to but never
    including the top-level folder (at home the top-level folders are where a note may be filed:
    removing one would refuse every later create there, review 28) and never the root. A folder
    still holding anything (a note, an attachment) stays, and a link is never followed or removed.
    An emptied top-level folder stays on disk; the home folder list hides it (folders()).
    Returns the folders removed."""
    parts = rel.replace("\\", "/").strip("/").split("/")[:-1]
    removed = []
    while len(parts) > 1:
        if any(p.startswith(".") or p in ("", "..") for p in parts):
            break
        pfd = _walk(parts[:-1])
        if pfd is None:
            break
        try:
            os.rmdir(parts[-1], dir_fd=pfd)
        except OSError:          # not empty, gone, or a link (ENOTDIR): stop here
            break
        finally:
            os.close(pfd)
        removed.append("/".join(parts))
        parts = parts[:-1]
    return removed


def drop_redirect_at(con, rel: str) -> None:
    """A note was made at `rel`: it is no longer a moved path."""
    con.execute("DELETE FROM redirects WHERE src=?", (rel,))


def redirects(con, limit: int = 500) -> list:
    return [dict(r) for r in _redirect_rows(
        con, "SELECT src, dst, ts, source FROM redirects ORDER BY ts DESC LIMIT ?", (int(limit),))]


def resolve_file(con, name: str):
    """A file name (or path) to the vault-relative path of a non-note file. An
    exact path wins, then a moved file's old path (edit.move_file); else by name,
    where attachments/ wins over the same name anywhere else."""
    clean = name.strip().replace("\\", "/").strip("/")
    if "/" in clean:
        row = con.execute("SELECT rel FROM files WHERE lower(rel)=lower(?)", (clean,)).fetchone()
        if row:
            return row["rel"]
        moved = moved_file(con, clean)
        if moved:
            return moved
    n = os.path.basename(clean)
    if not n:
        return None
    row = con.execute(
        "SELECT rel FROM files WHERE name=? COLLATE NOCASE"
        " ORDER BY CASE WHEN rel LIKE 'attachments/%' THEN 0 ELSE 1 END, length(rel) LIMIT 1",
        (n,)).fetchone()
    return row["rel"] if row else None


class _BulkResolver:
    """The whole index in memory, for the full pass. Resolving four thousand
    links one SQL query each is a second and a half; from three dicts it is
    a few milliseconds, which is what keeps the 900-page wiki's index fast."""

    def __init__(self, con):
        self.by_stem: dict = {}
        self.by_rel: dict = {}
        for r in con.execute("SELECT rel, stem FROM notes"):
            self.by_stem.setdefault(r["stem"].lower(), []).append(r["rel"])
            self.by_rel[r["rel"].lower()] = r["rel"]
        # Moved notes' old paths: consulted only when no note matches (resolve_note's fallback).
        self.redirect_of: dict = {}
        self.redirect_by_stem: dict = {}
        self.redirect_src: dict = {}      # exact old path -> new: moved files (follow_redirect's rule)
        for r in _redirect_rows(con, "SELECT src, dst FROM redirects", ()):
            self.redirect_src[r["src"]] = r["dst"]
            self.redirect_of[r["src"].lower()] = r["dst"]
            self.redirect_by_stem.setdefault(stem_of(r["src"]).lower(), []).append(r["src"])
        self.files_by_rel: dict = {}
        self.files_by_name: dict = {}
        for r in con.execute("SELECT rel, name FROM files"):
            self.files_by_rel[r["rel"].lower()] = r["rel"]
            self.files_by_name.setdefault(r["name"].lower(), []).append(r["rel"])
        self.cache: dict = {}

    def note(self, target: str):
        forms = [t.lower() for t in _target_forms(target)]
        for low in forms:
            if "/" in low:
                hit = self.by_rel.get(low + ".md")
                if hit:
                    return hit
                cands = [r for r in self.by_stem.get(low.rsplit("/", 1)[-1], [])
                         if r.lower().endswith("/" + low + ".md")]
            else:
                cands = self.by_stem.get(low, [])
            if cands:
                return min(cands, key=len)
        for low in forms:
            hit = self._moved(low)
            if hit:
                return hit
        return None

    def _moved(self, low: str):
        """resolve_note's redirect fallback, from the dicts."""
        if not self.redirect_of:
            return None
        srcs = [s for s in self.redirect_by_stem.get(low.rsplit("/", 1)[-1], [])
                if s.lower() == low + ".md" or s.lower().endswith("/" + low + ".md")]
        if not srcs:
            return None
        cur, seen = min(srcs, key=len), set()
        for _ in range(REDIRECT_HOPS):
            nxt = self.redirect_of.get(cur.lower())
            if nxt is None or nxt in seen:
                break
            seen.add(nxt)
            cur = nxt
        return self.by_rel.get(cur.lower())

    def file(self, name: str):
        clean = name.strip().replace("\\", "/").strip("/")
        if "/" in clean and clean.lower() in self.files_by_rel:
            return self.files_by_rel[clean.lower()]
        if "/" in clean and clean in self.redirect_src:
            cur, seen = clean, {clean}
            for _ in range(REDIRECT_HOPS):         # a moved file's old path (resolve_file's order)
                nxt = self.redirect_src.get(cur)
                if nxt is None or nxt in seen:
                    break
                seen.add(nxt)
                cur = nxt
            if cur != clean and cur.lower() in self.files_by_rel:
                return self.files_by_rel[cur.lower()]
        cands = self.files_by_name.get(os.path.basename(clean).lower(), [])
        if not cands:
            return None
        return min(cands, key=lambda r: (0 if r.startswith("attachments/") else 1, len(r)))

    def resolve(self, target: str):
        if target not in self.cache:
            # A note's name may end like an extension (owner@example.com): a file-looking
            # target no file answers is tried as a note.
            self.cache[target] = ((self.file(target) if is_file_target(target) else None)
                                  or self.note(target))
        return self.cache[target]


def _resolve_rows(con, rows):
    cache = {}
    for r in rows:
        key = r["target"]
        if key not in cache:
            cache[key] = ((resolve_file(con, key) if is_file_target(key) else None)
                          or resolve_note(con, key))
        con.execute("UPDATE links SET target_rel=? WHERE rowid=?", (cache[key], r["rowid"]))


def resolve_links(con, src: str | None = None) -> None:
    if src is None:
        bulk = _BulkResolver(con)
        rows = con.execute("SELECT rowid, target FROM links").fetchall()
        con.executemany("UPDATE links SET target_rel=? WHERE rowid=?",
                        [(bulk.resolve(r["target"]), r["rowid"]) for r in rows])
        return
    rows = con.execute("SELECT rowid, target FROM links WHERE src=?", (src,)).fetchall()
    _resolve_rows(con, rows)


def reresolve_targeting(con, rel: str) -> None:
    """Links anywhere that name this note or file get resolved again, so a
    note that just appeared satisfies them and one that vanished stops."""
    stem = stem_of(rel)
    name = rel.rsplit("/", 1)[-1]
    norel = rel[:-3] if rel.lower().endswith(".md") else rel
    rows = con.execute(
        "SELECT rowid, target FROM links WHERE target_rel=? OR lower(target) IN (?,?,?,?)",
        (rel, stem.lower(), name.lower(), norel.lower(), rel.lower())).fetchall()
    _resolve_rows(con, rows)


# --- Import -------------------------------------------------------------------
def upsert_note(con, rel: str, text: str, mtime: float, size: int) -> dict:
    fm, body = split_frontmatter(text)
    title, links, tasks = scan(body)
    stem = stem_of(rel)
    title = title or stem
    now = time.time()
    con.execute(
        "INSERT INTO notes (rel, folder, stem, title, body, frontmatter, mtime, size, imported_at,"
        " text) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rel) DO UPDATE SET folder=excluded.folder,"
        " stem=excluded.stem, title=excluded.title, body=excluded.body,"
        " frontmatter=excluded.frontmatter, mtime=excluded.mtime, size=excluded.size,"
        " imported_at=excluded.imported_at, text=excluded.text",
        (rel, folder_of(rel), stem, title, body, json.dumps(fm), mtime, size, now, text))
    con.execute("DELETE FROM links WHERE src=?", (rel,))
    con.executemany(
        "INSERT INTO links (src, target, heading, alias, embed, line) VALUES (?,?,?,?,?,?)",
        [(rel, l["target"], l["heading"], l["alias"], 1 if l["embed"] else 0, l["line"])
         for l in links])
    con.execute("DELETE FROM tasks WHERE rel=?", (rel,))
    con.executemany("INSERT INTO tasks (rel, line, done, text) VALUES (?,?,?,?)",
                    [(rel, ln, 1 if done else 0, txt) for ln, done, txt in tasks])
    return {"rel": rel, "title": title, "links": len(links), "tasks": len(tasks)}


def upsert_file(con, rel: str, mtime: float, size: int) -> None:
    con.execute(
        "INSERT INTO files (rel, name, size, mtime) VALUES (?,?,?,?)"
        " ON CONFLICT(rel) DO UPDATE SET name=excluded.name, size=excluded.size,"
        " mtime=excluded.mtime", (rel, rel.rsplit("/", 1)[-1], size, mtime))


def _unchanged(con, table, rel, mtime, size) -> bool:
    row = con.execute("SELECT mtime, size FROM %s WHERE rel=?" % table, (rel,)).fetchone()
    return bool(row) and abs(row["mtime"] - mtime) < 1e-6 and row["size"] == size


def import_path(con, path: str, resolve: bool = True, force: bool = False,
                adopt: bool = False):
    """Index one file by absolute path. Returns what was done, or None when the
    notebook may not take it: a path outside the vault, a hidden one (hidden()),
    a link whose real path leaves the folder or lands on a hidden path
    (confined_path), or anything but a regular file (a FIFO would hang the
    read). The file is read through its confined real path. In store mode a
    note is refused unless `adopt` says the caller means to take the file's
    text as a new revision."""
    rel = relpath(path)
    if rel.startswith("..") or hidden(rel):
        return None
    # One fd reached by the no-link walk (W10): the type, mtime, size and bytes all come
    # from the file actually opened, so a swap after any check cannot put an outside
    # file's bytes (/proc/self/environ) into the index.
    if not os.access(abspath(rel), os.R_OK):
        return None   # unreadable now, changed or not: unseen, so a stale row is dropped (a gate only)
    fd = open_confined(rel.replace(os.sep, "/"))
    if fd is None:
        return None   # a link, a hard link, not a regular file, unreadable, or gone: unseen
    try:
        st = os.fstat(fd)
        raw = _read_fd(fd)[0] if rel.lower().endswith(".md") else None
    except OSError:
        return None
    finally:
        os.close(fd)
    if rel.lower().endswith(".md"):
        if store_is_truth(con):
            if not adopt:
                raise RuntimeError("the store is truth; %s is not imported from the folder" % rel)
            return write_text(con, rel, _decode(raw), "adopted from the folder")
        if not force and _unchanged(con, "notes", rel, st.st_mtime, st.st_size):
            return {"rel": rel, "unchanged": True}
        out = upsert_note(con, rel, _decode(raw), st.st_mtime, st.st_size)
        if resolve:
            resolve_links(con, rel)
            reresolve_targeting(con, rel)
            con.commit()
        return out
    if not force and _unchanged(con, "files", rel, st.st_mtime, st.st_size):
        return {"rel": rel, "unchanged": True}
    upsert_file(con, rel, st.st_mtime, st.st_size)
    if resolve:
        reresolve_targeting(con, rel)
        con.commit()
    return {"rel": rel, "file": True}


def remove_path(con, path: str) -> bool:
    rel = relpath(path)
    if rel.startswith(".."):
        return False
    gone = con.execute("DELETE FROM notes WHERE rel=?", (rel,)).rowcount
    con.execute("DELETE FROM links WHERE src=?", (rel,))
    con.execute("DELETE FROM tasks WHERE rel=?", (rel,))
    gone += con.execute("DELETE FROM files WHERE rel=?", (rel,)).rowcount
    reresolve_targeting(con, rel)
    con.commit()
    return gone > 0


# --- Store-mode writes ------------------------------------------------------------
def write_text(con, rel: str, text: str, source: str) -> dict:
    """The text becomes the note's current revision. Store mode's only way in."""
    now = time.time()
    size = len(text.encode("utf-8"))
    out = upsert_note(con, rel, text, now, size)
    con.execute("INSERT INTO revisions (rel, ts, source, text) VALUES (?,?,?,?)",
                (rel, now, source, text))
    resolve_links(con, rel)
    reresolve_targeting(con, rel)
    con.commit()
    out.update({"mtime": now, "size": size})
    return out


def add_revision(con, rel: str, source: str, text: str, commit: bool = True) -> None:
    """One more line in a note's history that changes nothing in the note: who did what,
    with the text as it then stood. A removal uses it, so the last line of a gone note's
    history says who removed it and why, and the text is one click from coming back.
    `commit=False` leaves it to the caller's commit, so the line and the change it records
    land together or not at all."""
    con.execute("INSERT INTO revisions (rel, ts, source, text) VALUES (?,?,?,?)",
                (rel, time.time(), source, text))
    if commit:
        con.commit()


def rename_note(con, old: str, new: str, source: str | None = None) -> None:
    """Move a note's row, links, tasks and revisions to a new path, and record
    the move as a revision. Link resolution for both names is redone so
    nothing points at a ghost."""
    row = con.execute("SELECT text, mtime, size FROM notes WHERE rel=?", (old,)).fetchone()
    if row is None:
        raise KeyError(old)
    upsert_note(con, new, row["text"], row["mtime"], row["size"])
    con.execute("DELETE FROM notes WHERE rel=?", (old,))
    con.execute("DELETE FROM links WHERE src=?", (old,))
    con.execute("DELETE FROM tasks WHERE rel=?", (old,))
    con.execute("UPDATE revisions SET rel=? WHERE rel=?", (new, old))
    con.execute("INSERT INTO revisions (rel, ts, source, text) VALUES (?,?,?,?)",
                (new, time.time(), source or ("renamed from " + old), row["text"]))
    resolve_links(con, new)
    reresolve_targeting(con, old)
    reresolve_targeting(con, new)
    con.commit()


# --- Writing a file in the folder (the editor in folder mode and the store's export) ------
ABSENT = ("absent",)   # an `expect` meaning: the caller saw no file here
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC


class Moved(Exception):
    """The file is not what the caller read: another writer got there first."""


# Helm runs as root over a folder that many agents write (DECISIONS W10). A name is never
# resolved through the folder again after it was checked: every path is walked from an fd
# on the root, one part at a time, opening each folder with O_NOFOLLOW, and every read,
# write, create, rename and remove happens relative to the folder fd that walk holds. A
# folder or a name swapped for a link at any moment is refused, never followed (a link
# anywhere in a path is not the wiki's: the live wiki holds none). Reads and writes of a
# note's bytes, the index, /att, the export and the write log all go through here.
def _parts(rel: str, hidden_ok: bool = False):
    if not VAULT or not rel or "\x00" in rel:
        return None
    clean = rel.replace("\\", "/")
    if clean.startswith("/") or re.match(r"^[A-Za-z]:", clean):
        return None
    parts = clean.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if not hidden_ok and hidden(clean):
        return None
    return parts


def _walk(dir_parts, create: bool = False):
    """An fd on the folder `dir_parts` under the root, walked with O_NOFOLLOW, or None.
    With create, missing folders are made, each with its parent's mode and owner."""
    try:
        fd = os.open(os.path.realpath(VAULT), _DIR_FLAGS)
    except OSError:
        return None
    for part in dir_parts:
        try:
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            if not create:
                os.close(fd)
                return None
            pst = os.fstat(fd)
            try:
                os.mkdir(part, pst.st_mode & 0o7777, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError:
                os.close(fd)
                return None
            try:
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.fchmod(nfd, pst.st_mode & 0o7777)
                if os.geteuid() == 0:
                    os.fchown(nfd, pst.st_uid, pst.st_gid)
            except OSError:
                os.close(fd)
                return None
        except OSError:          # ELOOP (a link), ENOTDIR, EACCES: not a way down
            os.close(fd)
            return None
        os.close(fd)
        fd = nfd
    return fd


def open_confined(rel: str, flags: int = os.O_RDONLY, hidden_ok: bool = False):
    """An fd on the regular, single-link file at `rel`, reached without following any
    link, or None. Never creates or truncates; non-blocking (a FIFO cannot hang it)."""
    parts = _parts(rel, hidden_ok)
    if parts is None:
        return None
    dfd = _walk(parts[:-1])
    if dfd is None:
        return None
    try:
        fd = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _decode(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n")


def _read_fd(fd):
    """(bytes, fstat before, fstat after) of an open file."""
    before = os.fstat(fd)
    chunks = []
    while True:
        b = os.read(fd, 1 << 20)
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks), before, os.fstat(fd)


def read_confined(rel: str, stable: bool = True, hidden_ok: bool = False):
    """(text, mtime, size) of the note at `rel` read through open_confined, CRLF
    normalised as the index does, or None. With `stable`, the text and the (mtime,
    size) describe one file state (re-read while another writer appends); "UNSTABLE"
    after five tries."""
    for _ in range(5):
        fd = open_confined(rel, hidden_ok=hidden_ok)
        if fd is None:
            return None
        try:
            raw, before, after = _read_fd(fd)
        finally:
            os.close(fd)
        if (before.st_mtime, before.st_size) == (after.st_mtime, after.st_size) or not stable:
            return _decode(raw), after.st_mtime, after.st_size
    return "UNSTABLE"


def read_bytes_confined(rel: str, hidden_ok: bool = False):
    """(bytes, mtime, size) of the file at `rel` through open_confined, or None (/att)."""
    fd = open_confined(rel, hidden_ok=hidden_ok)
    if fd is None:
        return None
    try:
        raw, _, after = _read_fd(fd)
    finally:
        os.close(fd)
    return raw, after.st_mtime, after.st_size


def stat_confined(rel: str, hidden_ok: bool = False):
    """os.stat_result of the regular file at `rel` (no link followed), or None."""
    fd = open_confined(rel, hidden_ok=hidden_ok)
    if fd is None:
        return None
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def changed_since(path: str, expect) -> bool:
    """By path, for callers outside the folder walk (kept for export's comparisons)."""
    if expect is None:
        return False
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return expect is not ABSENT
    if expect is ABSENT:
        return True
    return abs(st.st_mtime - float(expect[0])) > 0.0005 or st.st_size != expect[1]


def _changed_at(dfd: int, name: str, expect) -> bool:
    if expect is None:
        return False
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return expect is not ABSENT
    if expect is ABSENT:
        return True
    return (not stat.S_ISREG(st.st_mode) or abs(st.st_mtime - float(expect[0])) > 0.0005
            or st.st_size != expect[1])


def ensure_dir(d: str) -> None:
    """The folder `d` (absolute, under the root) exists, made through the no-link walk."""
    rel = relpath(d)
    if rel in ("", "."):
        return
    fd = _walk(rel.replace(os.sep, "/").split("/"), create=True)
    if fd is None:
        raise PermissionError("%s leads out of the notes folder" % rel)
    os.close(fd)


def atomic_write_rel(rel: str, data: bytes, expect=None, hidden_ok: bool = False) -> None:
    """Write `rel` through a temp file in the same held folder fd, then rename (or, for a
    create, link: an existing file is Moved, never overwritten). The note keeps its mode
    and owner; a new one takes its folder's owner and the folder's mode without execute
    bits (Helm is root; the folder's other writers are not). `expect` is checked last,
    just before the rename. A link at the name is replaced, never followed."""
    parts = _parts(rel, hidden_ok)
    if parts is None:
        raise PermissionError("%s leads out of the notes folder, or is hidden" % rel)
    dfd = _walk(parts[:-1], create=True)
    if dfd is None:
        raise PermissionError("%s leads out of the notes folder" % rel)
    name = parts[-1]
    try:
        try:
            before = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                before = None
            elif not stat.S_ISREG(before.st_mode):
                raise IsADirectoryError("%s is not a file" % rel)
        except FileNotFoundError:
            before = None
        src = before or os.fstat(dfd)
        mode = (before.st_mode & 0o7777) if before else (src.st_mode & 0o666)
        tmp = ".helm-%s.tmp" % os.urandom(6).hex()
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, 0o600,
                         dir_fd=dfd)
        except FileNotFoundError:
            raise Moved(rel) from None   # the folder was removed under the write
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fchmod(fd, mode)
                if os.geteuid() == 0:
                    os.fchown(fd, src.st_uid, src.st_gid)
            finally:
                os.close(fd)
            try:
                if expect is ABSENT:
                    try:
                        os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                    except FileExistsError:
                        raise Moved(rel) from None
                    try:
                        os.unlink(tmp, dir_fd=dfd)
                    except OSError:
                        pass   # the note landed; a temp name someone else removed is not a failed create
                else:
                    if _changed_at(dfd, name, expect):
                        raise Moved(rel)
                    os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except FileNotFoundError:
                raise Moved(rel) from None   # the folder was removed under the write
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


def atomic_write(path: str, data: bytes, expect=None, hidden_ok: bool = False) -> None:
    """atomic_write_rel for an absolute path under the root."""
    rel = relpath(path)
    if rel.startswith(".."):
        raise PermissionError("%s is outside the notes folder" % path)
    atomic_write_rel(rel.replace(os.sep, "/"), data, expect, hidden_ok=hidden_ok)


RENAME_NOREPLACE = 1


def _load_renameat2():
    """libc renameat2, or None where the platform has none (it is Linux 3.15+ / glibc 2.28+)."""
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        return fn
    except (OSError, AttributeError):
        return None


_renameat2 = _load_renameat2()


def _rename_noreplace(sfd: int, src: str, dfd: int, dst: str, src_rel: str) -> None:
    """One atomic rename that never overwrites (renameat2 RENAME_NOREPLACE). Without it:
    link, check the source is still the file that was linked, then unlink; a newer version
    renamed over the source meanwhile backs the move out (Moved), and a failed unlink takes
    the new name away again, so a page is never left in two places or lost."""
    if _renameat2 is not None:
        import ctypes
        if _renameat2(sfd, os.fsencode(src), dfd, os.fsencode(dst), RENAME_NOREPLACE) == 0:
            return
        err = ctypes.get_errno()
        if err not in (errno.ENOSYS, errno.EINVAL):   # EINVAL: this filesystem has no NOREPLACE
            raise OSError(err, os.strerror(err), dst)
    before = os.stat(src, dir_fd=sfd, follow_symlinks=False)
    os.link(src, dst, src_dir_fd=sfd, dst_dir_fd=dfd, follow_symlinks=False)
    try:
        now = os.stat(src, dir_fd=sfd, follow_symlinks=False)
        if (now.st_ino, now.st_dev) != (before.st_ino, before.st_dev):
            raise Moved(src_rel)
        os.unlink(src, dir_fd=sfd)
    except BaseException:
        try:
            os.unlink(dst, dir_fd=dfd)
        except OSError:
            pass
        raise


def move_rel(src_rel: str, dst_rel: str, hidden_ok: bool = False) -> None:
    """Move the name `src_rel` (a link is moved as the link) to `dst_rel` inside the folder,
    both reached by the no-link walk; an existing destination is FileExistsError."""
    sp, dp = _parts(src_rel, hidden_ok), _parts(dst_rel, hidden_ok)
    if sp is None or dp is None:
        raise PermissionError("that path leads out of the notes folder, or is hidden")
    sfd = _walk(sp[:-1])
    if sfd is None:
        raise FileNotFoundError(src_rel)
    try:
        dfd = _walk(dp[:-1], create=True)
        if dfd is None:
            raise PermissionError("%s leads out of the notes folder" % dst_rel)
        try:
            _rename_noreplace(sfd, sp[-1], dfd, dp[-1], src_rel)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def unlink_rel(rel: str, hidden_ok: bool = False) -> bool:
    parts = _parts(rel, hidden_ok)
    if parts is None:
        return False
    dfd = _walk(parts[:-1])
    if dfd is None:
        return False
    try:
        os.unlink(parts[-1], dir_fd=dfd)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(dfd)


# --- The write log ------------------------------------------------------------------
PREV_TEXT_DAYS = 30
def _principal() -> str:
    """The per-caller token the request carried (helm.tokens), or "" for the site token."""
    try:
        from helm import tokens as _tokens
        return _tokens.PRINCIPAL.get() or ""
    except Exception:  # noqa: BLE001 - the notebook runs without the helm package in some tools
        return ""


def file_sha(path: str):
    """(size, sha256) of the note file at `path` (read through the no-link walk), or
    (None, None) when it is gone or not the folder's."""
    rel = relpath(path)
    got = None if rel.startswith("..") else read_bytes_confined(rel.replace(os.sep, "/"), hidden_ok=True)
    if got is None:
        return None, None
    return len(got[0]), hashlib.sha256(got[0]).hexdigest()


def log_write(con, rel: str, kind: str, source: str = "", path: str | None = None,
              prev_text: str | None = None, added: str | None = None) -> None:
    """One row per Helm write: what, who (source and token principal), and the file's
    size and sha256 after it, read back from `path` when the folder holds the note."""
    size, sha = file_sha(path) if path else (None, None)
    con.execute("INSERT INTO writes (ts, rel, kind, source, principal, size_after, sha_after, prev_size,"
                " prev_text, added) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), rel, kind, (source or "")[:80], _principal()[:80], size, sha,
                 None if prev_text is None else len(prev_text.encode("utf-8")), prev_text, added))
    # The undo text is for the recent past; the row (who, what, sha) stays for good.
    con.execute("UPDATE writes SET prev_text=NULL WHERE prev_text IS NOT NULL AND ts < ?",
                (time.time() - PREV_TEXT_DAYS * 86400,))
    con.commit()


def writes_since(con, since: float = 0.0, limit: int = 500) -> list:
    rows = con.execute("SELECT id, ts, rel, kind, source, principal, size_after, sha_after, prev_size,"
                       " CASE WHEN kind='append' THEN length(CAST(added AS BLOB)) END AS added_len"
                       " FROM writes WHERE ts > ? ORDER BY id LIMIT ?", (float(since), int(limit))).fetchall()
    return [dict(r) for r in rows]


def recent(con, limit: int = 50, since: float = 0.0) -> list:
    """Notes by mtime, newest first (the bridge's recent)."""
    rows = con.execute("SELECT * FROM notes WHERE mtime > ? ORDER BY mtime DESC LIMIT ?",
                       (float(since), int(limit))).fetchall()
    return [_note_dict(r) for r in rows]


def delete_note(con, rel: str) -> bool:
    """Drop a note from the store. Its revisions stay, so nothing is lost."""
    gone = con.execute("DELETE FROM notes WHERE rel=?", (rel,)).rowcount
    con.execute("DELETE FROM links WHERE src=?", (rel,))
    con.execute("DELETE FROM tasks WHERE rel=?", (rel,))
    reresolve_targeting(con, rel)
    con.commit()
    return gone > 0


def revisions(con, rel: str, limit: int = 50) -> list:
    rows = con.execute("SELECT id, ts, source, length(text) AS size FROM revisions WHERE rel=?"
                       " ORDER BY ts DESC, id DESC LIMIT ?", (rel, limit)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "source": r["source"], "size": r["size"]} for r in rows]


def revision_text(con, rev_id: int):
    r = con.execute("SELECT text FROM revisions WHERE id=?", (rev_id,)).fetchone()
    return r["text"] if r else None


# --- The full pass and the rescan -----------------------------------------------------
LAST_INDEX: dict | None = None
RESCAN_EVERY_S = 60
_scan_lock = threading.Lock()
_last_scan = 0.0
_rescan_thread: threading.Thread | None = None
_rescan_stop = threading.Event()


def rebuild(con, force: bool = False) -> dict:
    """Walk the whole folder. Unchanged files (same mtime and size) are skipped
    unless force; rows for files no longer on disk are removed. One transaction
    for the whole pass: the index is never seen half-built, and a thousand
    notes commit once instead of a thousand times. Folder modes only: in store
    mode the folder is a projection and export.rebuild is the equivalent pass."""
    global LAST_INDEX, _last_scan
    if store_is_truth(con):
        raise RuntimeError("the store is truth; use export.rebuild to refresh the folder")
    t0 = time.time()
    seen_notes, seen_files = set(), set()
    imported = skipped = 0
    with _scan_lock:
        if os.path.isdir(VAULT):
            # fwalk (W11): each folder is opened relative to its parent's fd and never through a
            # link, so a folder swapped for a link mid-walk is not descended.
            for dirpath, dirnames, filenames, _dfd in walk_folder():
                prune_walk(dirpath, dirnames, VAULT)
                for fn in filenames:
                    if fn.startswith(".") or fn.endswith(".tmp"):
                        continue
                    p = os.path.join(dirpath, fn)
                    rel = relpath(p)
                    try:
                        out = import_path(con, p, resolve=False, force=force)
                    except OSError as e:
                        # One unreadable file (a permission) must not abort the whole pass. It
                        # is unseen, so a row an older index holds for it is dropped below:
                        # the index only ever lists what can be read now.
                        skipped += 1
                        print("notes index: skipped %s: %s" % (rel, e), flush=True)
                        continue
                    if out is None:
                        # Not the notebook's (a link out of the folder or onto a hidden path,
                        # not a regular file) or gone: unseen, so a row an older index holds
                        # for it is dropped below.
                        skipped += 1
                        continue
                    (seen_notes if fn.lower().endswith(".md") else seen_files).add(rel)
                    if not out.get("unchanged"):
                        imported += 1
        removed = 0
        for r in con.execute("SELECT rel FROM notes").fetchall():
            if r["rel"] not in seen_notes:
                con.execute("DELETE FROM notes WHERE rel=?", (r["rel"],))
                con.execute("DELETE FROM links WHERE src=?", (r["rel"],))
                con.execute("DELETE FROM tasks WHERE rel=?", (r["rel"],))
                removed += 1
        for r in con.execute("SELECT rel FROM files").fetchall():
            if r["rel"] not in seen_files:
                con.execute("DELETE FROM files WHERE rel=?", (r["rel"],))
                removed += 1
        # Only a pass that changed something needs the links re-resolved; an idle
        # rescan of an unchanged wiki is a walk and nothing else.
        if imported or removed or force:
            resolve_links(con)
        # This DB was built from the folder: say so, or a store-mode start would read the
        # missing row as "the store is truth" and serve it without the cutover.
        con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('truth', 'folder')")
        con.commit()
        _last_scan = time.time()
    LAST_INDEX = {"at": _last_scan, "notes": len(seen_notes), "files": len(seen_files),
                  "imported": imported, "removed": removed, "skipped": skipped,
                  "seconds": round(time.time() - t0, 2)}
    return {"notes": len(seen_notes), "files": len(seen_files), "imported": imported, "skipped": skipped,
            "removed": removed, "seconds": LAST_INDEX["seconds"]}


def rescan_if_due(con=None, every_s: float | None = None, force: bool = False):
    """The mtime rescan, when RESCAN_EVERY_S have passed since the last pass
    (or `force`). Cheap to call from every read: it is a clock comparison
    until a pass is due, and a pass that another thread is already running
    is not waited for, unless `force`: then it waits and runs its own pass.
    Returns the pass's summary, or None when nothing ran.
    Folder modes only."""
    if mode() == "store":
        return None
    every = RESCAN_EVERY_S if every_s is None else every_s
    if not force and time.time() - _last_scan < every:
        return None
    # An unforced call (every read makes one) skips a pass another thread is running. A forced
    # one wants the folder as it is now: it waits for that pass (rebuild takes the lock) and runs
    # its own, or a file written just before the call stays unindexed until the next cycle.
    if not force and _scan_lock.locked():
        return None
    own = con is None
    con = con or db()
    try:
        return rebuild(con)
    finally:
        if own:
            con.close()


def _rescan_loop(every_s: float, stop: threading.Event) -> None:
    while not stop.wait(every_s):
        try:
            rescan_if_due(force=True)
        except Exception as e:  # noqa: BLE001
            print("notes rescan: %s: %s" % (type(e).__name__, e), flush=True)


def start_rescan(every_s: float | None = None) -> threading.Thread | None:
    """Rescan the folder by mtime on a daemon thread, every RESCAN_EVERY_S.
    Idempotent: a second call returns the running thread. The full index at
    start is the caller's (full_index); this only keeps it current."""
    global _rescan_thread
    if mode() == "store":
        return None
    if _rescan_thread is not None and _rescan_thread.is_alive():
        return _rescan_thread
    every = RESCAN_EVERY_S if every_s is None else every_s
    _rescan_stop.clear()
    _rescan_thread = threading.Thread(target=_rescan_loop, args=(every, _rescan_stop),
                                      name="helm-notes-rescan", daemon=True)
    _rescan_thread.start()
    return _rescan_thread


def stop_rescan(timeout: float = 10.0) -> bool:
    """Stop the rescan thread and wait for a pass in progress to finish. For a
    caller about to remove the folder or exit (the tests): a daemon thread still
    printing while the interpreter finalizes aborts it. True when no thread is
    left running."""
    global _rescan_thread
    th = _rescan_thread
    _rescan_stop.set()
    if th is None:
        return True
    th.join(timeout)
    if th.is_alive():
        return False
    _rescan_thread = None
    return True


def full_index(con=None) -> dict | None:
    """The index at start: every file in the folder, unchanged ones skipped
    when a cache from an earlier run is there. Store mode: nothing to do."""
    if mode() == "store":
        return None
    own = con is None
    con = con or db()
    try:
        return rebuild(con)
    finally:
        if own:
            con.close()


# --- Reads --------------------------------------------------------------------
def _note_dict(r) -> dict:
    return {"rel": r["rel"], "folder": r["folder"], "stem": r["stem"], "title": r["title"],
            "mtime": r["mtime"], "size": r["size"], "daily": is_daily(r["rel"])}


def get(con, rel: str):
    r = con.execute("SELECT * FROM notes WHERE rel=?", (rel,)).fetchone()
    if r is None:
        return None
    out = _note_dict(r)
    out["body"] = r["body"]
    out["frontmatter"] = json.loads(r["frontmatter"] or "{}")
    return out


def get_text(con, rel: str):
    """(text, mtime, size) of a stored note, or None."""
    r = con.execute("SELECT text, mtime, size FROM notes WHERE rel=?", (rel,)).fetchone()
    return (r["text"], r["mtime"], r["size"]) if r else None


def exists(con, rel: str) -> bool:
    return con.execute("SELECT 1 FROM notes WHERE rel=?", (rel,)).fetchone() is not None


def folders(con, visible=None) -> list:
    """The folders with their note counts: the profile's fixed list first or
    the top-level folders present in the wiki (home), then any other folder the
    index holds. At home a note at the root of the space is listed under ''.
    At home a folder with no notes is not listed (a retired folder whose pages the notebook
    organizer archived, or one holding only leftover files): there is nothing in it to read.
    Work's fixed folders are always listed. With `visible`, only the notes it passes count
    (an older index's row for a path now hidden names no folder)."""
    if visible is None:
        counts = {r["folder"]: r["c"] for r in
                  con.execute("SELECT folder, COUNT(*) c FROM notes GROUP BY folder")}
    else:
        counts = {}
        for r in con.execute("SELECT rel, folder FROM notes"):
            if visible(r["rel"]):
                counts[r["folder"]] = counts.get(r["folder"], 0) + 1
    known = folders_allowed()
    if not config.profile().fixed:
        known = [f for f in known if counts.get(f)]
    out = [{"folder": f, "count": counts.get(f, 0)} for f in known]
    extra = sorted(k for k in counts if k not in known and k)
    out += [{"folder": f, "count": counts[f]} for f in extra]
    if not config.profile().fixed and counts.get(""):
        out.append({"folder": "", "count": counts[""]})
    return out


def list_folder(con, folder: str) -> list:
    rows = con.execute("SELECT * FROM notes WHERE folder=?", (folder,)).fetchall()
    items = [_note_dict(r) for r in rows]
    daily_root = config.profile().daily_root
    if folder == daily_root or folder.startswith(daily_root + "/"):
        items.sort(key=lambda n: n["stem"], reverse=True)
    else:
        items.sort(key=lambda n: n["stem"].lower())
    return items


def tasks_for(con, rel: str) -> list:
    return [{"line": r["line"], "done": bool(r["done"]), "text": r["text"]}
            for r in con.execute("SELECT line, done, text FROM tasks WHERE rel=? ORDER BY line",
                                 (rel,))]


def first_paragraph(body: str, cap: int = 300) -> str:
    """The first prose paragraph (over 12 chars) of a body, markdown marks and wikilink brackets dropped, at most `cap`
    chars. Headings, fences, tables and list lines do not count. Reads only as far as it needs."""
    para, fenced = [], False
    for line in (body or "").splitlines():
        t = line.strip()
        if t.startswith("```") or t.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if not t or t.startswith(("#", "|", "- ", "* ", "+ ", ">", "---")):
            if para:
                text = " ".join(para)
                if len(text) > 12:
                    break
                para = []
            continue
        para.append(t)
    text = " ".join(para)
    if len(text) <= 12:
        return ""
    text = re.sub(r"\[\[([^\]|]+)\|?([^\]]*)\]\]", lambda m: m.group(2) or m.group(1), text)
    text = re.sub(r"[*_`]+", "", text)
    return text[:cap]


BACKLINK_TEXT = 40   # the page draws this many backlink lines; the rest are listed with no text read


def backlinks(con, rel: str, visible=lambda rel: True, text_limit: int = BACKLINK_TEXT) -> list:
    rows = con.execute(
        "SELECT l.src, l.alias, l.line, n.title, n.folder, n.mtime FROM links l"
        " JOIN notes n ON n.rel=l.src WHERE l.target_rel=? AND l.src != ?"
        " ORDER BY n.mtime DESC", (rel, rel)).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["src"] in seen or not visible(r["src"]):
            continue
        seen.add(r["src"])
        out.append({"rel": r["src"], "title": r["title"], "folder": r["folder"],
                    "line": r["line"], "daily": is_daily(r["src"]), "text": ""})
    # Only the lines the page draws are read: a hub linked from hundreds of notes does not load hundreds of bodies per view.
    for b in out[:text_limit]:
        body = con.execute("SELECT body FROM notes WHERE rel=?", (b["rel"],)).fetchone()
        b["text"] = line_at(body["body"], b["line"]) if body else ""
    return out


LINE_CAP = 240


def line_at(body: str, n: int, cap: int = LINE_CAP) -> str:
    """Line `n` (1-based, as links.line and tasks.line count) of a body, raw markdown, at
    most `cap` chars. Walks newlines with find, never splits the body."""
    if n < 1:
        return ""
    pos = 0
    for _ in range(n - 1):
        pos = body.find("\n", pos)
        if pos < 0:
            return ""
        pos += 1
    end = body.find("\n", pos)
    return body[pos:end if end >= 0 else len(body)][:cap]


def outlinks(con, rel: str) -> list:
    rows = con.execute(
        "SELECT l.target, l.target_rel, l.alias, l.embed, l.line, n.title FROM links l"
        " LEFT JOIN notes n ON n.rel=l.target_rel WHERE l.src=? ORDER BY l.line", (rel,))
    return [{"target": r["target"], "rel": r["target_rel"], "alias": r["alias"],
             "embed": bool(r["embed"]), "line": r["line"], "title": r["title"]} for r in rows]


def changed_between(con, start: float, end: float, limit: int = 200) -> list:
    """Notes whose mtime falls in [start, end), newest first: the pages changed
    that day (the Day view's changed list)."""
    rows = con.execute(
        "SELECT rel, title, stem, folder, mtime, size FROM notes WHERE mtime >= ? AND mtime < ?"
        " ORDER BY mtime DESC LIMIT ?", (start, end, limit)).fetchall()
    return [_note_dict(r) for r in rows]


def daily_mentions(con, needles: list, limit: int = 20) -> list:
    """Daily notes whose body contains any needle, newest first."""
    needles = [n for n in needles if n and len(n) >= 4]
    if not needles:
        return []
    where = " OR ".join("body LIKE ? ESCAPE '\\'" for _ in needles)
    args = ["%" + n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for n in needles]
    daily_folder = folder_of(daily_rel(dt.date.today()))
    rows = con.execute(
        "SELECT rel, title, stem, folder, mtime, size FROM notes WHERE folder=? AND (%s)"
        " ORDER BY stem DESC LIMIT ?" % where, [daily_folder] + args + [limit]).fetchall()
    return [_note_dict(r) for r in rows if is_daily(r["rel"])]


def fts_query(q: str) -> str:
    toks = re.findall(r"[^\s\"]+", q or "")
    return " ".join('"%s"*' % t.replace('"', '""') for t in toks)


def search(con, q: str, limit: int = 30) -> list:
    fq = fts_query(q)
    if not fq:
        return []
    rows = con.execute(
        "SELECT n.rel, n.title, n.folder, n.stem, n.mtime,"
        " snippet(notes_fts, 1, '\x02', '\x03', ' ... ', 16) AS snip"
        " FROM notes_fts f JOIN notes n ON n.rowid=f.rowid"
        " WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts, 6.0, 1.0) LIMIT ?",
        (fq, limit)).fetchall()
    return [{"rel": r["rel"], "title": r["title"], "folder": r["folder"], "stem": r["stem"],
             "mtime": r["mtime"], "snippet": r["snip"], "daily": is_daily(r["rel"])}
            for r in rows]


def switcher(con, q: str, limit: int = 20) -> list:
    toks = [t.lower() for t in (q or "").split() if t]
    rows = con.execute("SELECT rel, folder, stem, title, mtime, size FROM notes").fetchall()
    hits = []
    for r in rows:
        hay = (r["stem"] + " " + r["title"]).lower()
        if all(t in hay for t in toks):
            starts = r["stem"].lower().startswith(toks[0]) if toks else False
            hits.append((0 if starts else 1, -r["mtime"], _note_dict(r)))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [h[2] for h in hits[:limit]]


def stats(con) -> dict:
    n = con.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
    f = con.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    l = con.execute("SELECT COUNT(*) c, SUM(target_rel IS NOT NULL) r FROM links").fetchone()
    return {"notes": n, "files": f, "links": l["c"], "links_resolved": l["r"] or 0}


if __name__ == "__main__":
    import sys
    c = db()
    print(rebuild(c, force="--force" in sys.argv))
    print(stats(c))


# --- Helm reads: changes, graph, tasks, atlas, tail ------------------------------------------
def _added_lines(added, n: int = 3) -> list:
    """The last `n` non-empty lines of an appended text, each at most LINE_CAP chars."""
    out = []
    if not added:
        return out
    tail = added[-4000:] if len(added) > 4000 else added
    for ln in reversed(tail.split("\n")):
        if ln.strip():
            out.append(ln.rstrip()[:LINE_CAP])
            if len(out) == n:
                break
    out.reverse()
    return out


def changes(con, since: float, limit: int, visible=lambda rel: True) -> list:
    """Helm's own writes, newest first, joined to the note's title and folder. A write whose
    note is gone or hidden is skipped. Pages of 300 rows down the ts index until `limit`
    rows are kept, at most 10 pages (so a run of hidden or gone notes cannot walk the table)."""
    out, page = [], 300
    for k in range(10):
        rows = con.execute(
            "SELECT w.ts, w.rel, w.kind, w.source, w.principal, substr(w.added, -4000) AS added,"
            " n.title, n.folder FROM writes w JOIN notes n ON n.rel = w.rel"
            " WHERE w.ts > ? ORDER BY w.ts DESC, w.id DESC LIMIT ? OFFSET ?",
            (float(since), page, k * page)).fetchall()
        for r in rows:
            if not visible(r["rel"]):
                continue
            out.append({"rel": r["rel"], "title": r["title"], "folder": r["folder"], "ts": r["ts"],
                        "kind": r["kind"], "source": r["source"], "principal": r["principal"],
                        "lines": _added_lines(r["added"])})
            if len(out) >= limit:
                return out
        if len(rows) < page:
            break
    return out


def _top(folder: str) -> str:
    return folder.split("/", 1)[0]


def _day_edges(today=None) -> list:
    """Local-midnight epochs bounding the last 7 days: 8 edges, oldest first (DST-safe)."""
    today = today or dt.date.today()
    return [dt.datetime.combine(today - dt.timedelta(days=6 - i), dt.time()).timestamp()
            for i in range(8)][:7] + [dt.datetime.combine(today + dt.timedelta(days=1), dt.time()).timestamp()]


def graph(con, limit: int, visible=lambda rel: True) -> dict:
    """Most-linked notes, per-top-level-folder shelves, and counts. Folder visibility is judged
    per folder (a hidden path part), the last note by `visible`. Never reads a note body."""
    # most linked: distinct sources per target, over-fetched so hidden/gone targets can drop out.
    cand = con.execute(
        "SELECT target_rel, COUNT(DISTINCT src) c FROM links WHERE target_rel IS NOT NULL"
        " AND src != target_rel GROUP BY target_rel ORDER BY c DESC, target_rel LIMIT ?",
        (limit * 4 + 40,)).fetchall()
    most = []
    for r in cand:
        n = con.execute("SELECT title, folder FROM notes WHERE rel=?", (r["target_rel"],)).fetchone()
        if n is None or not visible(r["target_rel"]):
            continue
        most.append({"rel": r["target_rel"], "title": n["title"], "folder": n["folder"], "links_in": r["c"]})
        if len(most) >= limit:
            break
    # shelves: one covering-index pass over (folder, mtime)
    edges = _day_edges()
    shelves = {}
    total = 0
    for r in con.execute("SELECT folder, mtime FROM notes"):
        f = r["folder"]
        if hidden(f + "/x.md" if f else "x.md"):
            continue
        total += 1
        top = _top(f)
        s = shelves.setdefault(top, {"folder": top, "count": 0, "last_mtime": 0.0, "_lf": f,
                                     "week": [0] * 7})
        s["count"] += 1
        m = r["mtime"]
        if m > s["last_mtime"]:
            s["last_mtime"], s["_lf"] = m, f
        if m >= edges[0] and m < edges[7]:
            for i in range(7):
                if edges[i] <= m < edges[i + 1]:
                    s["week"][i] += 1
                    break
    folders = []
    for s in sorted(shelves.values(), key=lambda s: (-s["last_mtime"], s["folder"])):
        s["last_rel"], s["last_title"] = None, None
        for n in con.execute("SELECT rel, title FROM notes WHERE folder=? AND mtime=? LIMIT 5",
                             (s["_lf"], s["last_mtime"])):
            if visible(n["rel"]):
                s["last_rel"], s["last_title"] = n["rel"], n["title"]
                break
        del s["_lf"]
        folders.append(s)
    unresolved = sum(1 for r in con.execute(
        "SELECT target FROM links WHERE target_rel IS NULL AND embed=0") if not is_file_target(r["target"]))
    return {"most_linked": most, "folders": folders, "total_notes": total, "unresolved_links": unresolved}


def open_tasks(con, limit: int, visible=lambda rel: True) -> dict:
    """Notes with open checkboxes, most open first. One grouped pass over tasks."""
    rows = con.execute(
        "SELECT t.rel, SUM(t.done = 0) o, SUM(t.done = 1) d, n.title, n.folder, n.mtime FROM tasks t"
        " JOIN notes n ON n.rel = t.rel GROUP BY t.rel HAVING o > 0").fetchall()
    rows = [r for r in rows if visible(r["rel"])]
    rows.sort(key=lambda r: (-r["o"], -r["mtime"], r["rel"]))
    out = []
    for r in rows[:limit]:
        items = [{"line": t["line"], "text": t["text"][:LINE_CAP]} for t in con.execute(
            "SELECT line, text FROM tasks WHERE rel=? AND done=0 ORDER BY line LIMIT 5", (r["rel"],))]
        out.append({"rel": r["rel"], "title": r["title"], "folder": r["folder"], "open": r["o"],
                    "done": r["d"], "items": items})
    return {"notes": out, "total_open": sum(r["o"] for r in rows)}


ATLAS_SHELVES = ("Docker", "APIs", "Network")
ATLAS_PER_HOST = 24


def atlas(con, visible=lambda rel: True) -> list:
    """Every note under Hosts/, with the Docker/APIs/Network pages that link to it. Wiki
    profile only (the caller returns [] under the fixed profile)."""
    hosts = con.execute(
        "SELECT rel, title, mtime FROM notes WHERE folder = 'Hosts' OR (folder >= 'Hosts/' AND folder < 'Hosts0')"
        " ORDER BY rel").fetchall()
    out = []
    for h in hosts:
        if not visible(h["rel"]):
            continue
        srcs = [r["src"] for r in con.execute("SELECT DISTINCT src FROM links WHERE target_rel=?", (h["rel"],))]
        srcs = [s for s in srcs if s != h["rel"] and _top(folder_of(s)) in ATLAS_SHELVES]
        pages = []
        for s in srcs:
            n = con.execute("SELECT title, folder, mtime FROM notes WHERE rel=?", (s,)).fetchone()
            if n is not None and visible(s):
                pages.append((n["mtime"], {"rel": s, "title": n["title"], "folder": n["folder"]}))
        pages.sort(key=lambda p: (-p[0], p[1]["rel"]))
        out.append({"rel": h["rel"], "title": h["title"], "mtime": h["mtime"],
                    "pages": [p[1] for p in pages[:ATLAS_PER_HOST]]})
    return out


_HEAD_LINE = re.compile(r"#{1,6}[ \t]+(.+?)\s*#*\s*$")


def tail(con, rel: str, n: int):
    """A note's last `n` non-empty, non-heading lines, and the last heading, walking back from
    the end of the body with rfind (nothing is split). Lines count as links.line does: 1-based
    in the body. None when there is no such note."""
    r = con.execute("SELECT body, size FROM notes WHERE rel=?", (rel,)).fetchone()
    if r is None:
        return None
    body = r["body"]
    lines, end = [], len(body)
    if body.endswith("\n"):
        end -= 1
    lineno = body.count("\n", 0, end) + 1
    pos = end
    while pos >= 0 and (len(lines) < n):
        start = body.rfind("\n", 0, pos) + 1
        text = body[start:pos]
        if text.strip() and not _HEAD_LINE.match(text):
            lines.append({"line": lineno, "text": text.rstrip()[:400]})
        pos, lineno = start - 1, lineno - 1
    heading = None
    k = len(body)
    while True:
        k = body.rfind("\n#", 0, k)
        cand = k + 1 if k >= 0 else (0 if body.startswith("#") else -1)
        if cand < 0:
            break
        e = body.find("\n", cand)
        m = _HEAD_LINE.match(body[cand:e if e >= 0 else len(body)])
        if m:
            heading = {"line": body.count("\n", 0, cand) + 1, "text": m.group(1)}
            break
        if k < 0:
            break
    lines.reverse()
    return {"rel": rel, "lines": lines, "last_heading": heading, "size": r["size"]}
