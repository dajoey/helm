"""Every write to a note, in every mode. the reference's app/edit.py, ported.

Save, new from template, checkbox toggle, append to a daily note, rename with
link update, attach a file: each rule of the vault contract that code can
check is checked here, so the HTTP layer stays thin and a test can hit the
rules without a server.

Where the text lands depends on notes.truth(). In `folder` mode (the reference's
phase 1, and home's bridge from the wiki: other writers keep writing the same
files) the file is written and the store re-reads it. In `store` mode the
write is a revision and export.py projects it onto the folder. In
`folder-readonly` mode every write here is refused with a 405: the wiki is the
truth and the wiki engine edits it.

In folder mode a write shares the folder with writers Helm does not see (the bridge,
ssh `cat >>`, the wiki engine), so: an append is one O_APPEND write, never a
read-modify-write; a rewrite refuses (409) when the file changed since it was
read; and a written file keeps its mode and owner (Helm runs as root, the ssh
appenders as the folder's owner). Every write is logged (notes.log_write).

    from notebook import edit
    edit.save(con, "Contacts/Some coop.md", text, base_mtime=1.0)
"""
# temp-ok: mkstemp is used only for an atomic write into the note's own folder
from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
import time

from . import config, export, notes

TEMPLATE_DIR = config.profile().template_dir
TEMPLATE_FOR = dict(config.profile().template_for)
# # [ ] are here too: a wikilink cannot carry them ([[C# Notes]] reads as page C, heading "Notes").
BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*#\[\]\x00-\x1f]')
UNTITLED_RE = re.compile(r"^untitled(\s*\d+)?$", re.I)
TASK_LINE_RE = re.compile(r"^(" + notes.TASK_MARKER + r"\[)( |x|X)(\]\s)")

READONLY_DETAIL = ("the notebook is read-only at this site (a staging copy or a rollback); "
                   "notes are edited in the live Helm notebook")

# Lines that look like a credential. The check reports the shape of the match,
# never its content.
SECRET_PATTERNS = (
    ("password", re.compile(r"(?i)\b(password|passwd|pwd|passphrase)\b\s*[:=]\s*(?P<val>\S+)")),
    ("secret or token", re.compile(r"(?i)\b(secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\b\s*[:=]\s*(?P<val>\S{8,})")),
    ("bearer token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("connection string", re.compile(r"(?i)\b(pwd|password)\s*=\s*[^;\s]+\s*;")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API key", re.compile(r"\b(sk|xox[bap]|ghp|gho|glpat)-[A-Za-z0-9_-]{16,}")),
    ("URL with a password", re.compile(r"://[^/\s:@]+:[^/\s@]+@")),
)
# A documented placeholder in a pattern's value (`<MEDIA_TOKEN>`, `${VAR}`, `$VAR`,
# `{env:VAR}`, `REDACTED`, `***`, `xxx`) names the secret to fetch; it is not one.
# The whole value must be the placeholder, bar closing quotes or punctuation; a
# `&` after it starts the next query parameter, which is checked on its own.
PLACEHOLDER_RE = re.compile(r"<[A-Za-z_][A-Za-z0-9_.-]*>|\$\{[A-Za-z_]\w*\}|\$[A-Z_][A-Z0-9_]*(?![\w$])"
                            r"|\{env:[A-Za-z_]\w*\}|(?i:redacted)|\*{3,}|(?i:x{3,})")
PLACEHOLDER_TAIL_RE = re.compile(r"""['"`,;:.)\]}]*$""")


class EditError(Exception):
    def __init__(self, status: int, detail: str, **extra):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra


# --- Helpers -----------------------------------------------------------------------
def refuse_readonly() -> None:
    """Every write starts here. 405: the method is right, the site is not."""
    if config.read_only():
        raise EditError(405, READONLY_DETAIL, mode=config.mode())


def norm_rel(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip().strip("/")
    parts = rel.split("/")
    if not rel or ".." in parts:
        raise EditError(400, "bad note path")
    # Empty and "." parts are dropped: `Contacts//X.md` is `Contacts/X.md`. notes._parts refuses
    # them for the export, so a store row spelled that way would never be written to disk, and
    # two spellings would be two notes behind one file. Callers that gate on the path (the
    # delete protection) see the canonical spelling too.
    rel = "/".join(p for p in parts if p not in ("", "."))
    if not rel:
        raise EditError(400, "bad note path")
    return rel


def today() -> dt.date:
    return dt.date.today()


def daily_date(rel: str):
    """The date of a daily note, or None for any other note."""
    if not notes.is_daily(rel):
        return None
    try:
        return dt.date.fromisoformat(notes.stem_of(rel))
    except ValueError:
        return None


def editable(rel: str):
    """(editable, reason). A past day's note is history: appends only. At a
    read-only site nothing is editable, and the reason says why."""
    if config.read_only():
        return False, "read-only: " + READONLY_DETAIL
    d = daily_date(rel)
    if d is not None and d < today():
        return False, "history: a past day's note accepts only appends"
    return True, ""


_ensure_dir = notes.ensure_dir
ABSENT = notes.ABSENT


def _atomic_write(path: str, data: bytes, expect=None) -> None:
    """notes.atomic_write: the note keeps its mode and owner; `expect` is checked last."""
    notes.atomic_write(path, data, expect)


def write_text(path: str, text: str, expect=None) -> None:
    """UTF-8 without a BOM, LF endings."""
    _atomic_write(path, text.encode("utf-8"), expect)


def normalise(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _placeholder_len(value: str):
    """How much of a matched value is a placeholder to skip, or None when it is not one."""
    m = PLACEHOLDER_RE.match(value)
    if not m:
        return None
    rest = value[m.end():]
    if PLACEHOLDER_TAIL_RE.match(rest):
        return len(value)
    if rest.startswith("&"):
        return m.end()
    return None


def secret_shape(line: str):
    for name, rx in SECRET_PATTERNS:
        pos = 0
        while True:
            m = rx.search(line, pos)
            if not m:
                break
            skip = _placeholder_len(m.group("val")) if "val" in rx.groupindex else None
            if skip is None:
                return "%s (%s)" % (name, re.sub(r"\S", "x", m.group(0))[:40])
            pos = m.start("val") + skip
    return None


def check_secrets(old_text: str, new_text: str) -> None:
    """A credential never enters a note that did not have one. Only lines added
    by this write are checked, and a note that already carries one is exempt."""
    old_lines = set((old_text or "").split("\n"))
    if any(secret_shape(l) for l in old_lines):
        return
    for ln in (new_text or "").split("\n"):
        if ln in old_lines:
            continue
        shape = secret_shape(ln)
        if shape:
            raise EditError(400, "that line looks like a credential: %s. Notes do not carry "
                                 "credentials; save anyway only if it is not one." % shape,
                            secret=True, shape=shape)


def valid_title(title: str) -> str:
    t = (title or "").strip().rstrip(".")
    if not t:
        raise EditError(400, "a title is required")
    if UNTITLED_RE.match(t):
        raise EditError(400, "'Untitled' is not a name; say what the note is")
    bad = BAD_NAME_CHARS.search(t)
    if bad:
        raise EditError(400, "title cannot contain %r" % bad.group(0))
    if len(t) > 180:
        raise EditError(400, "title is too long (%d chars, max 180)" % len(t))
    return t



def _folders_text() -> str:
    return ", ".join(notes.folders_allowed()) or "(none present)"


# --- Truth ------------------------------------------------------------------------------
def _store(con) -> bool:
    return notes.store_is_truth(con)


def _exists(con, rel: str) -> bool:
    if _store(con) or config.read_only():
        return notes.exists(con, rel)
    return os.path.exists(notes.abspath(rel))


def follow(con, rel: str) -> str:
    """`rel`, or the note it moved to (notes.redirects) when no note is at `rel`, `rel` is exactly
    a moved path, and a note is at the destination now. A real note at `rel` always wins; a
    redirect whose note has since gone (deleted, moved outside Helm) is not followed, so the
    write makes `rel` itself (and that create clears the redirect)."""
    if _exists(con, rel):
        return rel
    dst = notes.follow_redirect(con, rel)
    return dst if dst and _exists(con, dst) else rel


def _current(con, rel: str, stable: bool = True):
    """(text, mtime, size) of a note from whichever place is truth, or None. The
    store at store truth. At a read-only site, the index: it holds exactly the
    notes the folder may show (inside the root, nothing hidden, no link out:
    notes.import_path), so the editor's read and the rendered note answer for the
    same paths. In folder mode (the reference's phase 1), the file itself, through the
    same confinement (notes.confined_path)."""
    if _store(con) or config.read_only():
        return notes.get_text(con, rel)
    # Other writers share the folder: the bytes come through one fd opened without
    # following a link (notes.read_confined), and the text and the (mtime, size) a later
    # write is checked against describe the same file state.
    got = notes.read_confined(rel, stable=stable)
    if got == "UNSTABLE":
        raise EditError(409, "the note kept changing while it was read (another writer); try again")
    return got


def _target(rel: str) -> str:
    """The path a folder-mode write of `rel` lands on (notes.write_target), or 400."""
    path = notes.write_target(rel)
    if path is None:
        raise EditError(400, "that path leads out of the notes folder, or is hidden")
    return path


def _moved(rel: str) -> "EditError":
    """The 409 for a note another writer changed: with the disk text, as _conflict gives,
    so the editor's 'take the disk version' shows the page instead of an empty buffer.
    The text is read only through notes.read_confined: a page swapped meanwhile for a link
    out (or a hard link, or gone) is a 404 with no text, never the link's target."""
    got = notes.read_confined(rel, stable=False)
    if not got or got == "UNSTABLE":
        return EditError(404, "the note moved or was deleted meanwhile; read it again")
    return EditError(409, "the note changed on disk since it was read (another writer); "
                          "read it again and retry", mtime=got[1], text=got[0])


def _commit(con, rel: str, text: str, source: str, expect=None, kind: str = "write",
            prev: str | None = None):
    """The text becomes the note, in whichever place is truth. (mtime, size).
    `expect` is the (mtime, size) the caller read the note at: in folder mode, where
    other writers share the file, a change since then is a 409 instead of a clobber."""
    refuse_readonly()
    created = expect is ABSENT
    if _store(con):
        out = notes.write_text(con, rel, text, source)
        try:
            export.write_note(rel, text)
        except OSError as e:
            print("export: %s: %s" % (rel, e), flush=True)
        if created:
            notes.drop_redirect_at(con, rel)   # a note made at a moved path: the path is its own again
        notes.log_write(con, rel, kind, source, prev_text=prev if kind == "write" else None,
                        added=text if kind == "append" else None)
        return out["mtime"], out["size"]
    path = _target(rel)
    try:
        write_text(path, text, expect)
    except notes.Moved:
        raise _moved(rel) from None
    except (PermissionError, IsADirectoryError) as e:
        raise EditError(400, "that path leads out of the notes folder, or is not a note: %s" % e) from None
    try:
        notes.import_path(con, path, force=True)
        if created:
            notes.drop_redirect_at(con, rel)   # a note made at a moved path: the path is its own again
        notes.log_write(con, rel, kind, source, path=path, prev_text=prev if kind == "write" else None,
                        added=text if kind == "append" else None)
    except Exception as e:  # noqa: BLE001
        if kind != "append":
            raise
        print("notebook: indexing %s after its create: %s" % (rel, e), flush=True)
    st = notes.stat_confined(rel)
    if st is None:
        if kind == "append":
            # The note is created with the line in it: from here nothing may become an error a
            # caller would retry (a retry writes the line twice). W11.
            return time.time(), None
        raise EditError(409, "the note was written, then removed or replaced by another writer at "
                             "once; read it again")
    return st.st_mtime, st.st_size


def _conflict(cur_mtime: float, base_mtime, rel: str, text=None) -> None:
    if base_mtime is not None and abs(cur_mtime - float(base_mtime)) > 0.001:
        extra = {"mtime": cur_mtime}
        if text is not None:
            extra["text"] = text
        raise EditError(409, "the note changed since you opened it", **extra)


# --- Read for editing ----------------------------------------------------------------
def read(con, rel: str) -> dict:
    rel = norm_rel(rel)
    # Where the folder is the truth a hidden path is never read, even while an index
    # written before that rule still lists it; the store serves what the store holds.
    refused = notes.in_skipped_dir(rel) or (notes.hidden(rel) and not _store(con))
    cur = None if refused else _current(con, rel)
    asked = rel
    if cur is None and not refused:
        moved = follow(con, rel)
        if moved != rel and not notes.in_skipped_dir(moved) and (_store(con) or not notes.hidden(moved)):
            rel, cur = moved, _current(con, moved)
    if cur is None:
        raise EditError(404, "no note at " + asked)
    text, mtime, size = cur
    ok, why = editable(rel)
    out = {"rel": rel, "text": text, "mtime": mtime, "size": size, "editable": ok, "reason": why,
           "daily": notes.is_daily(rel), "folder": notes.folder_of(rel), "stem": notes.stem_of(rel)}
    if rel != asked:
        out["redirected_from"] = asked
    return out


# --- Save ------------------------------------------------------------------------------
def save(con, rel: str, text: str, base_mtime=None, allow_secret: bool = False,
         allow_history: bool = False, source: str = "web") -> dict:
    refuse_readonly()
    rel = follow(con, norm_rel(rel))
    cur = _current(con, rel)
    if cur is None:
        raise EditError(404, "no note at %s; create it first" % rel)
    ok, why = editable(rel)
    if not ok and not allow_history:
        raise EditError(403, why)
    old, cur_mtime, cur_size = cur
    _conflict(cur_mtime, base_mtime, rel, old)
    new = normalise(text)
    if new == old:
        return {"rel": rel, "mtime": cur_mtime, "saved": False}
    if not allow_secret:
        check_secrets(old, new)
    mtime, size = _commit(con, rel, new, source, expect=(cur_mtime, cur_size), prev=old)
    return {"rel": rel, "mtime": mtime, "size": size, "saved": True}


def upsert(con, rel: str, text: str, allow_secret: bool = False, allow_history: bool = False,
           source: str = "script") -> dict:
    """Save a note that may not exist yet, anywhere inside the notebook's
    folders. This is what a script's write_note() call becomes."""
    refuse_readonly()
    rel = norm_rel(rel)
    if not rel.lower().endswith(".md"):
        raise EditError(400, "a note's path ends in .md")
    rel = follow(con, rel)          # a script writing a moved page's old path writes the page
    if _exists(con, rel):
        return save(con, rel, text, allow_secret=allow_secret, allow_history=allow_history,
                    source=source)
    if notes.folder_of(rel) not in notes.folders_allowed():
        raise EditError(400, "a note goes in one of the notebook's folders (%s)" % _folders_text())
    valid_title(notes.stem_of(rel))
    ok, why = editable(rel)
    if not ok and not allow_history:
        raise EditError(403, why)
    new = normalise(text)
    if not allow_secret:
        check_secrets("", new)
    mtime, size = _commit(con, rel, new, source, expect=ABSENT)
    return {"rel": rel, "mtime": mtime, "size": size, "created": True}


# --- New notes -------------------------------------------------------------------------
_MOMENT = (("YYYY", "%Y"), ("MM", "%m"), ("DD", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S"))


def _fmt(when: dt.datetime, fmt: str) -> str:
    out = fmt
    for a, b in _MOMENT:
        out = out.replace(a, b)
    return when.strftime(out)


def fill_template(text: str, title: str, when: dt.datetime | None = None) -> str:
    """Obsidian's core template variables: {{title}}, {{date}}, {{time}}, and
    the {{date:FORMAT}} form with moment-style tokens."""
    when = when or dt.datetime.now()

    def sub(m):
        name, fmt = m.group(1), m.group(2)
        if name == "title":
            return title
        if name == "date":
            return _fmt(when, fmt or "YYYY-MM-DD")
        if name == "time":
            return _fmt(when, fmt or "HH:mm")
        return m.group(0)
    return re.sub(r"\{\{\s*(title|date|time)\s*(?::([^}]+))?\s*\}\}", sub, text)


def templates() -> list:
    if not TEMPLATE_DIR or not os.path.isdir(TEMPLATE_DIR):
        return []
    return sorted(f[:-3] for f in os.listdir(TEMPLATE_DIR) if f.endswith(".md"))


def template_text(name: str | None) -> str:
    if not name:
        return ""
    if not TEMPLATE_DIR:
        raise EditError(404, "this site has no templates")
    path = os.path.join(TEMPLATE_DIR, name if name.endswith(".md") else name + ".md")
    if not os.path.isfile(path):
        raise EditError(404, "no template named " + name)
    return notes.read_text(path)


def template_for(folder: str):
    return TEMPLATE_FOR.get(folder)


def new_note(con, folder: str, title: str, template: str | None = None, body: str | None = None,
             when: dt.datetime | None = None, source: str = "web") -> dict:
    refuse_readonly()
    folder = (folder or "").replace("\\", "/").strip("/")
    allowed = notes.folders_allowed()
    daily_root = config.profile().daily_root
    if folder not in allowed or folder == daily_root or folder.startswith(daily_root + "/"):
        raise EditError(400, "a note goes in one of the topic folders (%s); the daily note has its "
                             "own path" % (", ".join(f for f in allowed if f != daily_root
                                                     and not f.startswith(daily_root + "/"))
                                           or "(none present)"))
    title = valid_title(title)
    rel = folder + "/" + title + ".md"
    if _exists(con, rel):
        raise EditError(409, "a note called %r already exists in %s" % (title, folder), rel=rel)
    if body is None:
        tname = template if template is not None else template_for(folder)
        body = fill_template(template_text(tname), title, when) if tname else "# %s\n" % title
    mtime, size = _commit(con, rel, normalise(body), source, expect=ABSENT)
    return {"rel": rel, "mtime": mtime, "size": size, "created": True}


def create_daily(con, d: dt.date | None = None, when: dt.datetime | None = None,
                 source: str = "web") -> dict:
    """Today's note from the Daily template, the way Obsidian's daily-notes
    plugin makes it. A past day is history and is not created here."""
    refuse_readonly()
    d = d or today()
    if d != today():
        raise EditError(403, "only today's note is created here; a past day is history")
    rel = notes.daily_rel(d)
    if _exists(con, rel):
        raise EditError(409, "today's note already exists", rel=rel)
    when = when or dt.datetime.combine(d, dt.datetime.now().time())
    try:
        body = fill_template(template_text("Daily.md"), d.isoformat(), when)
    except EditError:
        body = "# %s\n\n" % d.isoformat()
    mtime, size = _commit(con, rel, normalise(body), source, expect=ABSENT)
    return {"rel": rel, "mtime": mtime, "size": size, "created": True}


# --- Append to a daily note ------------------------------------------------------------
def append_daily(con, d: dt.date, line: str, source: str = "web",
                 allow_secret: bool = False) -> dict:
    """Text onto the end of a daily note as typed, one line or several (line ends
    made plain), plus a newline. Creates the note with its date heading when there is
    none, and never glues onto a last line that lacks its newline. Any day is
    allowed: appending is the one thing history accepts. The text rules are the
    generic append's: line ends normalised, and the credential guard checks only the
    appended lines (a note that already carries one is exempt)."""
    refuse_readonly()
    if not line or not line.strip():
        raise EditError(400, "'line' is required and cannot be blank")
    line = normalise(line).rstrip("\n")
    rel = notes.daily_rel(d)
    cur = _current(con, rel, stable=False)
    if cur is None:
        text = "# %s\n\n" % d.isoformat() + line + "\n"
        if not allow_secret:
            check_secrets("", text)
        mtime, size = _commit(con, rel, text, source, kind="append", expect=ABSENT)
    else:
        if not allow_secret:
            check_secrets(cur[0], line + "\n")
        mtime, size = _append_existing(con, rel, cur, line + "\n", "line", source)
    return {"rel": rel, "mtime": mtime, "size": size, "appended": line}


# --- Append to any note -------------------------------------------------------------------
GAPS = ("blank", "line")


def _separator(tail: bytes, gap: str) -> str:
    """What goes between the note's current end and the appended text: `line` only
    ends an unfinished last line; `blank` (the bridge's append) leaves one blank line."""
    if not tail:
        return ""
    if gap == "line":
        return "" if tail.endswith(b"\n") else "\n"
    if tail.endswith(b"\n\n"):
        return ""
    return "\n" if tail.endswith(b"\n") else "\n\n"


def _append_existing(con, rel: str, cur, body: str, gap: str, source: str):
    """Add `body` to the end of an existing note. In folder mode this is one
    O_APPEND write: the kernel places it at the end at the moment of the write,
    so an append racing other appenders (ssh `cat >>`, the wiki engine) loses nothing
    and the note never shrinks. (mtime, size)."""
    refuse_readonly()
    if _store(con):
        old = cur[0]
        sep = _separator(old.encode("utf-8")[-2:], gap)
        out = notes.write_text(con, rel, old + sep + body, source)
        try:
            export.write_note(rel, old + sep + body)
        except OSError as e:
            print("export: %s: %s" % (rel, e), flush=True)
        notes.log_write(con, rel, "append", source, added=body)
        return out["mtime"], out["size"]
    # One fd, opened without following a link and checked for what it is (open_confined):
    # the tail read and the O_APPEND write both go through it, so a swap to a link out
    # between a check and the open can never make Helm (root) append outside the folder.
    fd = notes.open_confined(rel, os.O_RDWR | os.O_APPEND)
    if fd is None:
        raise EditError(404, "no note at " + rel)
    path = notes.abspath(rel)
    try:
        end = os.lseek(fd, 0, os.SEEK_END)
        tail = os.pread(fd, 2, max(0, end - 2)) if end else b""
        data = (_separator(tail, gap) + body).encode("utf-8")
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)
    # The line is on disk: from here nothing may turn into an error the caller would retry
    # (a retry would write the line twice), so a note swapped away at once still answers.
    try:
        notes.import_path(con, path, force=True)
        notes.log_write(con, rel, "append", source, path=path, added=body)
    except Exception as e:  # noqa: BLE001
        print("notebook: indexing %s after an append: %s" % (rel, e), flush=True)
    st = notes.stat_confined(rel)
    return (st.st_mtime, st.st_size) if st else (time.time(), None)


def append(con, rel: str, text: str, gap: str = "blank", create: bool = False,
           allow_secret: bool = False, source: str = "script") -> dict:
    """Append text to a note (the bridge's append, a script's `cat >>`). A past day's
    note accepts it (appending is the one write history takes). With create, a missing
    note is made under the rules for any new note (an existing folder, a real title).
    The credential guard checks only the appended lines, and a note that already
    carries a credential-shaped line is exempt, as for a save."""
    refuse_readonly()
    rel = norm_rel(rel)
    if not rel.lower().endswith(".md"):
        raise EditError(400, "a note's path ends in .md")
    if notes.hidden(rel):
        raise EditError(400, "that path is hidden from the notebook")
    rel = follow(con, rel)          # an appender still using a moved page's old path appends to the page
    if gap not in GAPS:
        raise EditError(400, "gap is one of %s" % ", ".join(GAPS))
    body = normalise(text).strip("\n")
    if not body.strip():
        raise EditError(400, "'text' is required and cannot be blank")
    body += "\n"
    # This read only feeds the credential check and the exists test; the append itself is
    # O_APPEND at the file's end, so a page other appenders keep moving is fine to append to.
    cur = _current(con, rel, stable=False)
    if cur is None:
        if not create:
            raise EditError(404, "no note at %s; pass create: true to make it" % rel)
        if notes.folder_of(rel) not in notes.folders_allowed():
            raise EditError(400, "a note goes in one of the notebook's folders (%s)" % _folders_text())
        valid_title(notes.stem_of(rel))
        if not allow_secret:
            check_secrets("", body)
        try:
            mtime, size = _commit(con, rel, body, source, kind="append", expect=ABSENT)
            return {"rel": rel, "mtime": mtime, "size": size, "appended": len(body.encode("utf-8")),
                    "created": True}
        except EditError as e:
            if e.status not in (404, 409):
                raise
        cur = _current(con, rel, stable=False)   # another writer made it first: append to theirs
        if cur is None:
            raise EditError(409, "the note appeared and vanished while it was being made; try again")
    if not allow_secret:
        check_secrets(cur[0], body)
    mtime, size = _append_existing(con, rel, cur, body, gap, source)
    return {"rel": rel, "mtime": mtime, "size": size, "appended": len(body.encode("utf-8"))}


# --- Delete (to Trash, the bridge's layout) --------------------------------------------------
def delete(con, rel: str, source: str = "script", when: dt.datetime | None = None,
           reason: str = "") -> dict:
    """Retire a note. In folder mode it moves to Trash/<YYYYmmdd-HHMMSS>/<rel>
    inside the folder (the bridge's delete layout, so both leave the same trail);
    in store mode it leaves the store and the export, and its revisions stay. A
    `reason` (the audit's route always gives one) becomes the last line of the
    note's history, "deleted by <source>: <reason>", carrying the text as it stood."""
    refuse_readonly()
    rel = norm_rel(rel)
    if notes.hidden(rel):
        raise EditError(404, "no note at " + rel)
    if _store(con):
        if not notes.exists(con, rel):
            raise EditError(404, "no note at " + rel)
        last = con.execute("SELECT text FROM notes WHERE rel=?", (rel,)).fetchone()
        try:
            if reason and last is not None:
                # Uncommitted: delete_note's commit lands the history line and the removal together,
                # so a crash never leaves a removed note without its "deleted by" line.
                notes.add_revision(con, rel, "deleted by %s: %s" % (source, reason), last["text"], commit=False)
            notes.delete_note(con, rel)
        except Exception:
            con.rollback()      # a failure takes the history line back with it on a connection that lives on
            raise
        try:
            export.remove_note(rel)
        except OSError as e:
            print("export: %s: %s" % (rel, e), flush=True)
        notes.log_write(con, rel, "delete", source, added=reason or None)
        return {"rel": rel, "deleted": True}
    path = notes.abspath(rel)
    stamp = (when or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest_rel = "Trash/%s/%s" % (stamp, rel)
    dest = _target(dest_rel)
    try:
        # The name itself moves (a symlinked page trashes the link, not its target), by the
        # no-link walk on both sides, never overwriting (W10).
        notes.move_rel(rel, dest_rel)
    except FileNotFoundError:
        raise EditError(404, "no note at " + rel) from None
    except notes.Moved:
        raise EditError(409, "the note was replaced while it was being moved to Trash; read it again") from None
    except (PermissionError, FileExistsError) as e:
        raise EditError(400, "the note could not be moved to Trash: %s" % e) from None
    notes.remove_path(con, path)
    notes.import_path(con, dest, force=True)
    notes.log_write(con, rel, "delete", source, added=dest_rel)
    return {"rel": rel, "deleted": True, "trashed_to": dest_rel}


DELETE_REASON_MAX = 500
DATED_STREAM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def delete_protected(rel: str) -> str:
    """Why a note may not be removed through delete_with_reason, or "" when it may: a daily
    note, and a dated note under the daily folder (the call and case-cleanup streams
    that live under Daily/Meetings and Daily/Ticket Cleanup), are history, the way a past
    day refuses edits."""
    if notes.is_daily(rel):
        return "a daily note is history and is not deleted"
    root = config.profile().daily_root
    if rel.startswith(root + "/") and DATED_STREAM_RE.match(notes.stem_of(rel)):
        return "a dated note under %s is history and is not deleted" % root
    return ""


def delete_with_reason(con, rel: str, reason, source: str = "script") -> dict:
    """POST /api/notes/delete: a caller that keeps the notebook tidy removes a note and says
    why. 400 for a blank reason, 403 for a daily or dated-stream note (whether or not it
    exists), 404 with "no note at <rel>" for a note that is not there, else delete()."""
    refuse_readonly()
    why = " ".join(str(reason or "").split())[:DELETE_REASON_MAX]
    source = " ".join(str(source or "").split()) or "script"     # one line in the history label
    if not why:
        raise EditError(400, "'reason' is required: say why the note is being removed")
    rel = norm_rel(rel)
    protected = delete_protected(rel)
    if protected:
        raise EditError(403, protected)
    return delete(con, rel, source=source, reason=why)


# --- Checkbox toggle -----------------------------------------------------------------------
def toggle_task(con, rel: str, line: int, base_mtime=None, done=None, source: str = "web") -> dict:
    """Flip the checkbox on one body line (1-based, counted after the
    frontmatter, the same numbering the tasks table and the renderer use)."""
    refuse_readonly()
    rel = follow(con, norm_rel(rel))
    cur = _current(con, rel)
    if cur is None:
        raise EditError(404, "no note at " + rel)
    ok, why = editable(rel)
    if not ok:
        raise EditError(403, why)
    text, cur_mtime, _ = cur
    _conflict(cur_mtime, base_mtime, rel)
    _, body = notes.split_frontmatter(text)
    offset = text[:len(text) - len(body)].count("\n")
    lines = text.split("\n")
    idx = offset + int(line) - 1
    if idx < 0 or idx >= len(lines):
        raise EditError(400, "line %d is past the end of the note" % line)
    m = TASK_LINE_RE.match(lines[idx])
    if not m:
        raise EditError(400, "line %d is not a checkbox line" % line)
    now_done = m.group(2) != " "
    want = (not now_done) if done is None else bool(done)
    lines[idx] = m.group(1) + ("x" if want else " ") + m.group(3) + lines[idx][m.end():]
    mtime, _ = _commit(con, rel, "\n".join(lines), source, expect=(cur_mtime, cur[2]), prev=text)
    return {"rel": rel, "line": int(line), "done": want, "mtime": mtime}


# --- Rename with link update ------------------------------------------------------------------
def _link_rx(old_rel: str) -> re.Pattern:
    stem = notes.stem_of(old_rel)
    noext = old_rel[:-3] if old_rel.lower().endswith(".md") else old_rel
    alts = sorted({stem, noext, old_rel}, key=len, reverse=True)
    return re.compile(r"(!?\[\[\s*)(%s)(\s*)(#[^\]|\n]*)?(\|[^\]\n]*)?(\]\])"
                      % "|".join(re.escape(a) for a in alts), re.I)


def _link_texts_resolving_to(con, rel: str) -> set:
    """The lowercased link texts ([[Old]], [[Folder/Old]], [[Folder/Old.md]]) that resolve to
    `rel` right now. With two notes of one name a bare [[Old]] names only the shorter path,
    so a rename must not repoint it when it is the other note that moves. Read before the
    move, while the old name still resolves."""
    stem = notes.stem_of(rel)
    noext = rel[:-3] if rel.lower().endswith(".md") else rel
    return {t.lower() for t in {stem, noext, rel} if notes.resolve_note(con, t) == rel}


def _rewrite_only(rx: re.Pattern, texts: set):
    """`rx` narrowed to the matches whose link text is in `texts`."""
    class _Rx:
        @staticmethod
        def findall(text):
            return [m for m in rx.finditer(text) if m.group(2).lower() in texts]

        @staticmethod
        def sub(repl, text):
            return rx.sub(lambda m: repl(m) if m.group(2).lower() in texts else m.group(0), text)
    return _Rx


def rename_plan(con, rel: str, new_stem: str) -> dict:
    rel = norm_rel(rel)
    if _current(con, rel) is None:
        raise EditError(404, "no note at " + rel)
    if notes.is_daily(rel):
        raise EditError(400, "a daily note is named by its date and is not renamed")
    new_stem = valid_title(new_stem)
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    new_rel = (folder + "/" if folder else "") + new_stem + ".md"
    if new_rel == rel:
        raise EditError(400, "that is already the name")
    if _exists(con, new_rel):
        raise EditError(409, "a note called %r already exists there" % new_stem)
    rx = _rewrite_only(_link_rx(rel), _link_texts_resolving_to(con, rel))
    changes = []
    srcs = [r["src"] for r in con.execute(
        "SELECT DISTINCT src FROM links WHERE target_rel=? ORDER BY src", (rel,))]
    if rel not in srcs:
        srcs.append(rel)
    for src in srcs:
        cur = _current(con, src)
        if cur is None:
            continue
        n = len(rx.findall(cur[0]))
        if n:
            changes.append({"rel": new_rel if src == rel else src, "count": n,
                            "daily": notes.is_daily(src)})
    return {"from": rel, "to": new_rel, "new_stem": new_stem, "changes": changes,
            "total": sum(c["count"] for c in changes)}


def rename(con, rel: str, new_stem: str, dry_run: bool = True, source: str = "web") -> dict:
    refuse_readonly()
    plan = rename_plan(con, rel, new_stem)
    if dry_run:
        plan["done"] = False
        return plan
    rx = _rewrite_only(_link_rx(plan["from"]), _link_texts_resolving_to(con, plan["from"]))
    stem = plan["new_stem"]

    def repl(m):
        return m.group(1) + stem + (m.group(4) or "") + (m.group(5) or "") + m.group(6)

    if _store(con):
        text = notes.get_text(con, plan["from"])[0]
        notes.rename_note(con, plan["from"], plan["to"])
        export.remove_note(plan["from"])
        export.write_note(plan["to"], text)
    else:
        old_path, new_path = notes.abspath(plan["from"]), _target(plan["to"])
        try:
            notes.move_rel(plan["from"], plan["to"])
        except (FileExistsError, notes.Moved):
            raise EditError(409, "a note called %r appeared there, or the note changed, meanwhile" % plan["new_stem"]) from None
        except FileNotFoundError:
            raise EditError(404, "no note at %s (it went away meanwhile)" % plan["from"]) from None
        notes.remove_path(con, old_path)
        notes.import_path(con, new_path, force=True)
    notes.drop_redirect_at(con, plan["to"])
    notes.log_write(con, plan["to"], "move", source, path=None if _store(con) else notes.abspath(plan["to"]),
                    added="renamed from " + plan["from"])
    missed = _rewrite_links(con, [c["rel"] for c in plan["changes"]], rx, repl, source)
    notes.resolve_links(con)
    con.commit()
    plan["done"] = True
    if missed:
        plan["links_not_rewritten"] = missed
    return plan


def _rewrite_links(con, srcs: list, rx: re.Pattern, repl, source: str, changed: list | None = None) -> list:
    """Rewrite the links `rx` matches in each page of `srcs`; the pages it could not rewrite.
    Pages actually rewritten are appended to `changed` when given."""
    missed = []
    for rel in srcs:
        # A linking page (the Changelog links everything) is shared with other writers:
        # rewrite it only if it is still what was read, re-reading on a 409 a few times.
        for _ in range(5):
            try:
                cur = _current(con, rel)
                if cur is None:
                    break
                new_text = rx.sub(repl, cur[0])
                if new_text != cur[0]:
                    _commit(con, rel, new_text, source, expect=(cur[1], cur[2]), prev=cur[0])
                    if changed is not None:
                        changed.append(rel)
                break
            except EditError as e:
                if e.status == 404:
                    break          # the linking page is gone: nothing left to rewrite in it
                if e.status != 409:
                    missed.append(rel)   # the page has moved already: list it, never stop halfway (W11)
                    break
            except OSError as e:
                print("notebook: could not rewrite links in %s: %s" % (rel, e), flush=True)
                missed.append(rel)
                break
        else:
            missed.append(rel)
    return missed


def _path_link_rx(old_rel: str) -> re.Pattern:
    """Links that name `old_rel` by its path ([[Folder/Page]], [[Folder/Page.md]]), not by bare
    name: a bare [[Page]] still finds the page wherever it moves."""
    noext = old_rel[:-3] if old_rel.lower().endswith(".md") else old_rel
    alts = sorted({noext, old_rel}, key=len, reverse=True)
    return re.compile(r"(!?\[\[\s*)(%s)(\s*)(#[^\]|\n]*)?(\|[^\]\n]*)?(\]\])"
                      % "|".join(re.escape(a) for a in alts), re.I)


# --- Move ---------------------------------------------------------------------------------------------
def move(con, rel: str, new_rel: str, source: str = "web", redirect: bool = False,
         links: bool = False, dry_run: bool = False, prune: bool = False) -> dict:
    """A note to another path. This is what a misfiled note's correction becomes: the audit
    moves a daily note into its month's folder with it, and the notebook organizer moves a
    retired folder's pages to Archive/ (POST /api/notes/move).
    With redirect, the old path keeps working (notes.redirects): links to it resolve to the new
    path, a read of it serves the note, a write to it lands in the note. With links, pages that
    link the old path by its path are rewritten to the new one (a bare [[Page]] needs nothing).
    With dry_run, nothing moves: the answer lists the linking pages and any other notes with
    the same name (whose bare [[Page]] links could change target). With prune, the nested folders
    the move left empty are removed, never a top-level one (notes.prune_empty_dirs)."""
    refuse_readonly()
    rel, new_rel = norm_rel(rel), norm_rel(new_rel)
    if not rel.lower().endswith(".md"):
        return move_file(con, rel, new_rel, source=source, redirect=redirect, links=links,
                         dry_run=dry_run, prune=prune)
    if not new_rel.lower().endswith(".md"):
        raise EditError(400, "a note's path ends in .md")
    if _current(con, rel) is None:
        raise EditError(404, "no note at " + rel)
    if new_rel == rel:
        raise EditError(400, "that is already the path")
    if notes.folder_of(new_rel) not in notes.folders_allowed():
        raise EditError(400, "a note goes in one of the notebook's folders (%s)" % _folders_text())
    valid_title(notes.stem_of(new_rel))
    if _exists(con, new_rel):
        raise EditError(409, "a note already exists at " + new_rel)
    linking = [r["src"] for r in con.execute(
        "SELECT DISTINCT src FROM links WHERE target_rel=? ORDER BY src", (rel,))] if links else []
    redirect = bool(redirect) and rel.lower() != new_rel.lower()   # a case-only move is its own redirect
    if dry_run:
        stem = notes.stem_of(new_rel)
        same = [r["rel"] for r in con.execute(
            "SELECT rel FROM notes WHERE stem=? COLLATE NOCASE AND rel<>? ORDER BY rel", (stem, rel))]
        return {"from": rel, "to": new_rel, "moved": False, "redirect": redirect,
                "links": linking, "same_name": same}
    if _store(con):
        text = notes.get_text(con, rel)[0]
        notes.rename_note(con, rel, new_rel, source="moved from " + rel)
        export.remove_note(rel)
        export.write_note(new_rel, text)
    else:
        old_path, new_path = notes.abspath(rel), _target(new_rel)
        try:
            notes.move_rel(rel, new_rel)
        except (FileExistsError, notes.Moved):
            raise EditError(409, "a note already exists at %s, or the note changed meanwhile" % new_rel) from None
        except FileNotFoundError:
            raise EditError(404, "no note at %s (it went away meanwhile)" % rel) from None
        notes.remove_path(con, old_path)
        notes.import_path(con, new_path, force=True)
    notes.drop_redirect_at(con, new_rel)
    notes.log_write(con, new_rel, "move", source, path=None if _store(con) else notes.abspath(new_rel),
                    added="moved from " + rel)
    # The old path gets its own row, so a reader of one page's writes (the straggler check) sees
    # the page left through Helm rather than vanishing.
    notes.log_write(con, rel, "move", source, added="moved to " + new_rel)
    out = {"from": rel, "to": new_rel, "moved": True, "redirect": redirect}
    if prune and notes.VAULT:
        try:
            out["pruned"] = notes.prune_empty_dirs(rel)
        except OSError as e:
            print("notebook: pruning after the move of %s: %s" % (rel, e), flush=True)
    if redirect:
        notes.add_redirect(con, rel, new_rel, source)
    if links and linking:
        new_noext = new_rel[:-3]

        def repl(m):
            return m.group(1) + new_noext + (m.group(4) or "") + (m.group(5) or "") + m.group(6)

        srcs = [new_rel if s == rel else s for s in linking]
        changed = []
        missed = _rewrite_links(con, srcs, _path_link_rx(rel), repl, source, changed=changed)
        out["links"] = changed
        if missed:
            out["links_not_rewritten"] = missed
    notes.resolve_links(con)
    con.commit()
    return out


def move_file(con, rel: str, new_rel: str, source: str = "web", redirect: bool = False,
              links: bool = False, dry_run: bool = False, prune: bool = False) -> dict:
    """A non-note file (an image, a JSON snapshot, a .md.bak-* copy) to another path: move() for
    any source path not ending in .md, so the organizer's archive of a retired folder takes its
    files along, not only its pages. Files live in the folder in both writable modes, so this is a
    rename on disk, and the index follows. The same options as a note's move: with redirect, a
    path embed of the old path and GET /att/<old path> still reach the file; with links, pages
    that name the old path are rewritten; with prune, emptied nested folders go. A file never
    becomes a note (a destination ending in .md is refused) and never replaces anything."""
    if new_rel.lower().endswith(".md"):
        raise EditError(400, "a file does not move to a note's path (.md)")
    if not notes.VAULT:
        raise EditError(503, "this site has no notes folder (HELM_NOTES_ROOT), so no files")
    if notes.stat_confined(rel) is None:
        raise EditError(404, "no file at " + rel)
    if new_rel == rel:
        raise EditError(400, "that is already the path")
    if notes.folder_of(new_rel) not in notes.folders_allowed():
        raise EditError(400, "a file goes in one of the notebook's folders (%s)" % _folders_text())
    if re.search(r"[\x00-\x1f]", new_rel):
        raise EditError(400, "bad file path")
    new_path = _target(new_rel)
    if os.path.lexists(new_path):
        raise EditError(409, "something already exists at " + new_rel)
    linking = [r["src"] for r in con.execute(
        "SELECT DISTINCT src FROM links WHERE target_rel=? ORDER BY src", (rel,))] if links else []
    redirect = bool(redirect) and rel.lower() != new_rel.lower()
    if dry_run:
        name = new_rel.rsplit("/", 1)[-1]
        same = [r["rel"] for r in con.execute(
            "SELECT rel FROM files WHERE name=? COLLATE NOCASE AND rel<>? ORDER BY rel", (name, rel))]
        return {"from": rel, "to": new_rel, "moved": False, "file": True, "redirect": redirect,
                "links": linking, "same_name": same}
    try:
        notes.move_rel(rel, new_rel)
    except (FileExistsError, notes.Moved):
        raise EditError(409, "something already exists at %s, or the file changed meanwhile" % new_rel) from None
    except FileNotFoundError:
        raise EditError(404, "no file at %s (it went away meanwhile)" % rel) from None
    except PermissionError as e:
        raise EditError(400, str(e)) from None
    notes.remove_path(con, notes.abspath(rel))
    notes.import_path(con, new_path, force=True)
    notes.drop_redirect_at(con, new_rel)
    notes.log_write(con, new_rel, "move", source, path=new_path, added="moved from " + rel)
    notes.log_write(con, rel, "move", source, added="moved to " + new_rel)
    out = {"from": rel, "to": new_rel, "moved": True, "file": True, "redirect": redirect}
    if prune:
        try:
            out["pruned"] = notes.prune_empty_dirs(rel)
        except OSError as e:
            print("notebook: pruning after the move of %s: %s" % (rel, e), flush=True)
    if redirect:
        notes.add_redirect(con, rel, new_rel, source)
    if links and linking:
        def repl(m):
            return m.group(1) + new_rel + (m.group(4) or "") + (m.group(5) or "") + m.group(6)

        changed = []
        missed = _rewrite_links(con, linking, _path_link_rx(rel), repl, source, changed=changed)
        out["links"] = changed
        if missed:
            out["links_not_rewritten"] = missed
    notes.resolve_links(con)
    con.commit()
    return out


# --- Folders ------------------------------------------------------------------------------------------
def create_folder(con, name: str, source: str = "script") -> dict:
    """A new top-level folder in a wiki (where the folders are the directories in the root).
    The declared shape's folders are fixed. Idempotent: an existing folder is not an error."""
    refuse_readonly()
    prof = config.profile()
    if prof.fixed:
        raise EditError(400, "this notebook's folders are fixed (%s)" % _folders_text())
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise EditError(400, "a folder is one top-level name, without slashes")
    valid_title(name)
    if name != name.strip(" ."):
        raise EditError(400, "a folder name does not start or end with a dot or a space")
    hidden_names = {x.lower() for x in set(prof.skip_dirs) | set(prof.skip_top)}
    if name.startswith(".") or name.lower() in hidden_names:
        raise EditError(400, "that folder name is hidden from the notebook")
    if name in notes.folders_allowed():
        return {"folder": name, "created": False}
    try:
        notes.ensure_dir(os.path.join(notes.VAULT, name))
    except (PermissionError, OSError) as e:
        raise EditError(400, "the folder could not be made: %s" % e) from None
    if name not in notes.folders_allowed():
        raise EditError(409, "the folder was made, but is not one of the notebook's (a link?)")
    notes.log_write(con, name, "folder", source, added="made folder " + name)
    return {"folder": name, "created": True}


# --- Attachments ---------------------------------------------------------------------------------
def attach(con, filename: str, data: bytes, when: dt.datetime | None = None) -> dict:
    """Save a file into attachments/ with Obsidian's naming for pasted images
    and return the wikilink that embeds or links it. Files live in the folder
    in both writable modes."""
    refuse_readonly()
    if not notes.VAULT:
        raise EditError(503, "this site has no notes folder (HELM_NOTES_ROOT), so nowhere to "
                             "keep a file")
    if not data:
        raise EditError(400, "the file is empty")
    if len(data) > 50 * 1024 * 1024:
        raise EditError(413, "file is over 50 MB")
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    name = BAD_NAME_CHARS.sub("", name).rstrip(".")
    when = when or dt.datetime.now()
    if not name or name.lower().startswith("image.") or name.lower() in ("image", "blob"):
        ext = os.path.splitext(name)[1].lower() or ".png"
        name = "Pasted image %s%s" % (when.strftime("%Y%m%d%H%M%S"), ext)
    root, ext = os.path.splitext(name)
    folder = os.path.join(notes.VAULT, "attachments")
    cand, n = name, 1
    while os.path.exists(os.path.join(folder, cand)):
        cand = "%s %d%s" % (root, n, ext)
        n += 1
    path = _target("attachments/" + cand)
    _atomic_write(path, data)
    notes.import_path(con, path, force=True)
    notes.log_write(con, "attachments/" + cand, "attach", "attach", path=path)
    embed = ext.lower() in notes.IMAGE_EXT
    return {"rel": "attachments/" + cand, "name": cand, "size": len(data), "image": embed,
            "wikilink": ("![[%s]]" if embed else "[[%s]]") % cand}


# --- History ---------------------------------------------------------------------------------------
def history(con, rel: str, limit: int = 50) -> list:
    """Past revisions of a note, newest first. Empty before the cutover, and
    always empty at a read-only site (the wiki keeps its own history)."""
    return notes.revisions(con, norm_rel(rel), limit)


def revision(con, rev_id: int) -> dict:
    text = notes.revision_text(con, rev_id)
    if text is None:
        raise EditError(404, "no revision %d" % rev_id)
    return {"id": rev_id, "text": text}


# --- Autocomplete --------------------------------------------------------------------------------
def complete(con, q: str, kind: str = "note", limit: int = 12) -> list:
    """Candidates for [[ in the editor: notes by stem or title, files by name."""
    toks = [t.lower() for t in (q or "").split() if t]
    out = []
    if kind in ("note", "all"):
        for r in con.execute("SELECT rel, stem, title, folder, mtime FROM notes").fetchall():
            hay = (r["stem"] + " " + r["title"]).lower()
            if all(t in hay for t in toks):
                starts = bool(toks) and r["stem"].lower().startswith(toks[0])
                out.append((0 if starts else 1, -r["mtime"],
                            {"kind": "note", "label": r["stem"], "detail": r["folder"], "rel": r["rel"],
                             "insert": r["stem"]}))
    if kind in ("file", "all"):
        for r in con.execute("SELECT rel, name, mtime FROM files").fetchall():
            if all(t in r["name"].lower() for t in toks):
                out.append((2, -r["mtime"], {"kind": "file", "label": r["name"], "detail": "attachments",
                                             "rel": r["rel"], "insert": r["name"]}))
    out.sort(key=lambda x: (x[0], x[1]))
    return [o[2] for o in out[:limit]]
