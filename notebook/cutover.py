"""The cutover: the store becomes truth and the folder becomes its export.
a first cut of the notebook's cutover, kept without the client-only halves (the git
untracking of the client's repo and the retirement of the note app's folders).

    python3 -m notebook.cutover        dry run: the checks and the round-trip report
    python3 -m notebook.cutover --go   the flip itself

--go, in order: the preconditions (the folder is truth, the templates are in
place), a forced import of every note, a round-trip check of each note against
its file that aborts on any difference beyond line endings, a zip of the
folder under $HELM_DATA, the flip (truth=store, one revision per note), and
the export rebuild that rewrites CRLF files as LF and writes the marker file.

On start (on_start): the api runs the same --go sequence itself, before it serves,
when it starts in store mode with a folder and the DB is still the folder's (meta
`truth` is `folder`, or a folder-built DB from before that row: notes and no
revision). So the home flip is one ordinary deploy, the HELM_NOTES_MODE change.
It fails closed: a preflight problem or a round trip that is not clean changes
nothing and the api refuses to start (the puller's smoke test fails and it rolls
back to the folder-mode image). Once flipped, a later step that fails is logged
and the api starts anyway: store mode is then the only mode that can serve it.
Already the store, folder modes, or no folder (work without an export): nothing.

Reversing it: stop the server, `python3 -m notebook.export --rebuild` so the
folder is current, set `truth` back to `folder` in the meta table, and run the
site with HELM_NOTES_MODE=folder. The zip is the belt to that suspender.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import zipfile

from . import config, export, notes

ZIP_SKIP = {".notes", ".copilot", "copilot", ".trash", ".cache", ".git_backup", "__pycache__"}
TEMPLATES = ("Daily.md", "Contact.md", "Build.md", "Runbook.md")


# --- Checks -----------------------------------------------------------------------------
def preflight(con, quiet: bool = True) -> list:
    """What stands in the way. Empty means go. `quiet` is kept from the reference's
    signature (its checks for a running note app and server were client-only)."""
    from . import edit
    problems = []
    if notes.truth(con) != "folder":
        problems.append("truth is already %r" % notes.truth(con))
    if not notes.VAULT or not os.path.isdir(notes.VAULT):
        problems.append("no vault folder at %r" % notes.VAULT)
    if edit.TEMPLATE_DIR:
        missing = [t for t in TEMPLATES if not os.path.isfile(os.path.join(edit.TEMPLATE_DIR, t))]
        if missing:
            problems.append("templates missing from %s: %s" % (edit.TEMPLATE_DIR, ", ".join(missing)))
    return problems


# --- Import and round trip --------------------------------------------------------------------
def round_trip(con) -> dict:
    """Every note read into the store, then each one compared with its file."""
    rb = notes.rebuild(con, force=True)
    v = export.verify(con)
    v["imported"] = rb["notes"]
    v["files"] = rb["files"]
    v["clean"] = not (v["differ"] or v["missing"] or v["strays"])
    return v


def report(v: dict) -> str:
    lines = ["notes %d, files %d" % (v["imported"], v["files"]),
             "byte-identical %d, line endings only %d, differ %d, missing %d, strays %d"
             % (v["same"], len(v["normalised"]), len(v["differ"]), len(v["missing"]), len(v["strays"]))]
    for k in ("differ", "missing", "strays"):
        for rel in v[k]:
            lines.append("  %s: %s" % (k, rel))
    return "\n".join(lines)


# --- Backup -----------------------------------------------------------------------------------------
def backup(dest_dir: str | None = None) -> str:
    """A zip of the folder, editors' own folders left out. Each file is read through
    the no-link walk (W10): a link, or a file swapped for one mid-walk, is not zipped."""
    dest_dir = dest_dir or config.data_dir()
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "vault_cutover_backup_%s.zip" % time.strftime("%Y-%m-%d_%H%M%S"))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames, _dfd in notes.walk_folder():
            dirnames[:] = [d for d in dirnames if d not in ZIP_SKIP]
            for fn in filenames:
                rel = notes.relpath(os.path.join(dirpath, fn))
                got = notes.read_bytes_confined(rel, hidden_ok=True)
                if got is None:
                    print("backup: skipped %s (a link or not a regular file)" % rel, flush=True)
                    continue
                info = zipfile.ZipInfo(rel, max(time.localtime(got[1])[:6], (1980, 1, 1, 0, 0, 0)))
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, got[0])
    return path


# --- The flip -------------------------------------------------------------------------------------------
def flip(con) -> dict:
    """The store becomes truth: one revision per note, then the meta rows."""
    if notes.truth(con) != "folder":
        raise RuntimeError("truth is already %r" % notes.truth(con))
    now = time.time()
    n = con.execute("INSERT INTO revisions (rel, ts, source, text)"
                    " SELECT rel, ?, 'import', text FROM notes", (now,)).rowcount
    con.commit()
    notes.set_meta(con, "cutover_at", dt.datetime.now().isoformat(timespec="seconds"))
    notes.set_meta(con, "truth", "store")
    return {"revisions": n, "cutover_at": notes.get_meta(con, "cutover_at")}


# --- The sequence ---------------------------------------------------------------------------------------
REVERSAL = ("reversal: notebook/cutover.py docstring (export --rebuild, meta truth=folder,"
            " HELM_NOTES_MODE=folder)")


class NotReady(RuntimeError):
    """The flip did not happen and nothing changed: preflight problems (code 2) or a
    round trip that is not clean (code 3). `lines` is what the CLI prints."""

    def __init__(self, message: str, code: int, lines: list):
        super().__init__(message)
        self.code = code
        self.lines = lines


class AfterFlip(RuntimeError):
    """The flip happened (truth=store) and a step after it failed."""


def counts(v: dict) -> str:
    return ("byte-identical %d, normalised %d, differ %d, missing %d, strays %d"
            % (v["same"], len(v["normalised"]), len(v["differ"]), len(v["missing"]), len(v["strays"])))


def go(con, say=print) -> dict:
    """--go: preflight, round trip, backup, flip, export rebuild, verify. Raises
    NotReady before the flip (nothing changed) and AfterFlip after it."""
    problems = preflight(con)
    if problems:
        raise NotReady("not ready: " + "; ".join(problems), 2,
                       ["not ready:"] + ["  " + p for p in problems])
    v = round_trip(con)
    say(report(v))
    if not v["clean"]:
        bad = ["%s %s" % (k, rel) for k in ("differ", "missing", "strays") for rel in v[k]]
        more = " and %d more" % (len(bad) - 20) if len(bad) > 20 else ""
        raise NotReady("round trip is not clean (%s): %s%s" % (counts(v), ", ".join(bad[:20]), more),
                       3, ["round trip is not clean; nothing changed"])
    zpath = backup()
    say("backup: " + zpath)
    f = flip(con)
    say("flip: %s" % f)
    try:
        rep = export.rebuild(con, adopt=False)
        say("export: %s (%d files rewritten with LF endings)" % (export.summary(rep), len(rep["written"])))
        v2 = export.verify(con)
        say("verify: " + counts(v2))
    except Exception as e:  # noqa: BLE001
        raise AfterFlip("%s: %s" % (type(e).__name__, e)) from e
    return {"round_trip": v, "backup": zpath, "flip": f, "export": rep, "verify": v2}


# --- On start (the api's lifespan, app/notebook_api.startup) ------------------------------------
def pending(con) -> bool:
    """Is this a store-mode start on a DB the cutover has not flipped yet?"""
    if config.mode() != "store" or not notes.VAULT:
        return False
    t = notes.get_meta(con, "truth")
    if t is not None:
        return t == "folder"
    # No row: a DB indexed in folder mode before notes.rebuild recorded one holds notes and
    # no revision (every store-mode write is one); a fresh store DB holds no note at all.
    return (con.execute("SELECT 1 FROM notes LIMIT 1").fetchone() is not None
            and con.execute("SELECT 1 FROM revisions LIMIT 1").fetchone() is None)


def _alert(kind: str, message: str, level: str) -> None:
    """The same line to the error tracker, when the process has a DSN (app/service.py inits it)."""
    try:
        import sentry_sdk
        if sentry_sdk.is_initialized():
            sentry_sdk.capture_message(message, level=level,
                                       fingerprint=["helm-notebook-cutover", kind])
    except Exception:  # noqa: BLE001
        pass


def on_start() -> dict | None:
    """The cutover, when pending(), before the api serves. None when there is nothing
    to do. Raises NotReady (nothing changed): the api must not start."""
    con = notes.db()
    try:
        if not pending(con):
            return None
        if notes.get_meta(con, "truth") is None:
            notes.set_meta(con, "truth", "folder")   # what the DB is; preflight reads it
        try:
            res = go(con, say=lambda _line: None)
        except AfterFlip as e:
            msg = ("notebook cutover: flipped (truth=store) but the export after the flip failed: %s;"
                   " starting in store mode, the export check retries; %s" % (e, REVERSAL))
            print(msg, flush=True)
            _alert("after-flip", msg, "error")
            return {"flipped": True, "error": str(e)}
        except Exception as e:  # noqa: BLE001  (NotReady, or a failure before the flip: the backup)
            if not isinstance(e, NotReady):
                e = NotReady("%s: %s" % (type(e).__name__, e), 1, [])
            msg = ("notebook cutover: refused, nothing changed (truth=%s), the api will not start"
                   " in store mode: %s" % (notes.get_meta(con, "truth"), e))
            print(msg, flush=True)
            _alert("refused", msg, "error")
            raise e
        v = res["round_trip"]
        msg = ("notebook cutover: flipped (truth=store): notes %d, files %d, %s; revisions %d;"
               " backup %s; export %s; verify %s; %s"
               % (v["imported"], v["files"], counts(v), res["flip"]["revisions"], res["backup"],
                  export.summary(res["export"]), counts(res["verify"]), REVERSAL))
        print(msg, flush=True)
        _alert("flipped", msg, "warning")
        return {"flipped": True, "backup": res["backup"], "revisions": res["flip"]["revisions"]}
    finally:
        con.close()


# --- CLI ----------------------------------------------------------------------------------------------------
def main(argv) -> int:
    con = notes.db()
    try:
        if "--go" in argv:
            try:
                go(con)
            except NotReady as e:
                for line in e.lines:
                    print(line)
                return e.code
        else:
            problems = preflight(con, quiet=False)
            print("truth: " + notes.truth(con))
            for p in problems:
                print("problem: " + p)
            if notes.truth(con) == "folder":
                print(report(round_trip(con)))
            else:
                v = export.verify(con)
                print("export verify: " + counts(v))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
