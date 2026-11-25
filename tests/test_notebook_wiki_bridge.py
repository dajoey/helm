"""The home wiki takes writes through Helm: `folder` mode at the home profile (the bridge).

Projects/Wiki to Helm Notebook 2026-09, Phase 1 (the owner 2026-09-26: "Bridge now, flip later").
Helm writes the same files the wiki engine serves, while the fleet's other writers (the bridge,
out-of-band scripts over ssh, the wiki engine itself) keep writing them directly until they move.

Covers:
- the wiki stays untrusted input when home is writable: raw HTML escaped, active /att types
  downloaded, keyed on the home profile and not on the read-only mode (PR #3 review H1/H5);
- a Helm write keeps the page's mode and owner, and a new page is not 0600, so the ssh
  appenders (user owner) can still append after Helm (running as root) rewrote a page;
- POST /api/notes/append: O_APPEND, never read-modify-write, so an append racing direct
  appenders loses nothing and a page never shrinks; the bridge's blank-line spacing; create rules;
  the credential guard on the appended lines only; a past daily note accepts an append;
- GET /api/notes/recent and DELETE /api/notes/note (to Trash/<stamp>/<rel>, the bridge's layout);
- a folder-mode rewrite refuses (409) when the file changed under it since it was read;
- the write log (GET /api/notes/writes): every Helm write, its kind, source, principal and the
  file's sha256 after, which is what the straggler check compares the folder against.
"""
# temp-ok: the folders and DBs under test are fixtures this module creates and deletes
from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_bridge_")
WIKI = os.path.join(TMP, "wiki")
os.environ["HELM_SITE"] = "home"
os.environ["HELM_NOTES_PROFILE"] = "wiki"
os.environ["HELM_NOTES_MODE"] = "folder"
os.environ["HELM_NOTES_ROOT"] = WIKI
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_APP_TOKEN"] = "test-token"  # force: a persisted real token must never leak into tests

from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from notebook import config, edit, notes, render  # noqa: E402
import app.notebook_api as notebook_api  # noqa: E402

PASS = FAIL = 0
TODAY = dt.date.today()


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def write(rel, text, mode=None):
    p = os.path.join(WIKI, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(text.encode("utf-8") if isinstance(text, str) else text)
    if mode is not None:
        os.chmod(p, mode)
    return p


def read(rel):
    with open(os.path.join(WIKI, rel), encoding="utf-8") as fh:
        return fh.read()


def seed():
    write("index.md", "# Home\n\nSee [[Ops Log]].\n")
    write("Ops Log.md", "# Ops Log\n\n- 2026-09-26 10:00 ET - seed: first line\n", mode=0o664)
    write("Changelog.md", "- 2026-09-26 10:00 ET - seed - [[index]]: first\n", mode=0o664)
    write("Docker/xss.md", "# xss\n\n<img src=x onerror=\"fetch('/api/inbox/new')\">\n\n<b>bold</b>\n")
    write("Docker/diagram.svg", "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    write("Docker/embeds.md", "# embeds\n\n![[diagram.svg]]\n")
    write("Playbooks/Keys.md", "# Keys\n\nThe vault password: hunter2hunter2\n")
    write("Journal/Day/2020-01-01.md", "# 2020-01-01\n\n- old day\n")
    write(".wiki-engine.auth.json", "{\"secret\": \"x\"}")


def api():
    app = Starlette(routes=list(notebook_api.routes))
    return TestClient(app)


def run() -> int:
    seed()
    notes.full_index()
    con = notes.db()
    client = api()

    # --- the mode and the untrusted folder --------------------------------------------------
    check("folder mode is writable", config.mode() == "folder" and not config.read_only()
          and config.profile().name == "wiki")
    check("config.untrusted_folder() is true for the home profile whatever the mode",
          hasattr(config, "untrusted_folder") and config.untrusted_folder() is True)
    r = client.get("/api/notes/note", params={"rel": "Docker/xss.md"})
    html = r.json().get("html", "") if r.status_code == 200 else ""
    check("raw HTML in a wiki page is escaped at home in folder mode (H1 holds when home is writable)",
          r.status_code == 200 and "<img" not in html and "&lt;img" in html and "<b>" not in html, html[:200])
    check("render.render escapes raw HTML at home in folder mode",
          "<b>" not in render.render("<b>x</b>") and "&lt;b&gt;" in render.render("<b>x</b>"))
    r = client.get("/att/Docker/diagram.svg")
    check("an active attachment type downloads at home in folder mode (H5 holds when home is writable)",
          r.status_code == 200 and r.headers.get("content-type", "").startswith("application/octet-stream")
          and "attachment" in r.headers.get("content-disposition", ""),
          (r.status_code, r.headers.get("content-type"), r.headers.get("content-disposition")))

    # --- mode and owner survive a Helm write ------------------------------------------------------
    p = os.path.join(WIKI, "Changelog.md")
    cur = edit.read(con, "Changelog.md")
    edit.save(con, "Changelog.md", cur["text"] + "- 2026-09-26 11:00 ET - test - [[index]]: saved\n",
              base_mtime=cur["mtime"], source="test")
    check("a Helm save keeps the page's mode (0664 stays 0664, not mkstemp's 0600)",
          (os.stat(p).st_mode & 0o777) == 0o664, oct(os.stat(p).st_mode & 0o777))
    edit.upsert(con, "Docker/new-page.md", "# new page\n", source="test")
    np_mode = os.stat(os.path.join(WIKI, "Docker/new-page.md")).st_mode & 0o777
    check("a page Helm creates is group/other readable, not 0600 (mode follows the umask, max 0664)",
          np_mode & 0o044 == 0o044 and np_mode & 0o022 != 0o022, oct(np_mode))
    calls = []
    real_chown, real_fchown, real_euid = getattr(os, "chown", None), os.fchown, os.geteuid

    def rec_fchown(fd, uid, gid):   # writes chown the open temp file / new folder by fd (W10)
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        name = os.path.basename(os.readlink("/proc/self/fd/%d" % fd))
        calls.append((name if kind == "dir" else (".helm-tmp" if name.startswith(".helm-") else name), uid, gid))
    try:
        os.chown = lambda path, uid, gid: calls.append((os.path.basename(path), uid, gid))
        os.fchown = rec_fchown
        os.geteuid = lambda: 0
        st = os.stat(p)
        cur = edit.read(con, "Changelog.md")
        edit.save(con, "Changelog.md", cur["text"] + "- 2026-09-26 11:05 ET - test - [[index]]: as root\n",
                  base_mtime=cur["mtime"], source="test")
        edit.upsert(con, "Docker/root-made.md", "# made as root\n", source="test")
        edit.upsert(con, "Docker/Sub/deeper.md", "# in a new subfolder\n", source="test")
    finally:
        os.chown, os.fchown, os.geteuid = real_chown, real_fchown, real_euid
    dstat = os.stat(os.path.join(WIKI, "Docker"))
    check("running as root, a rewrite hands the file back to its owner (the ssh appenders run as that user)",
          any(u == st.st_uid and g == st.st_gid for _, u, g in calls), calls)
    check("running as root, a new page takes its folder's owner", any(
        u == dstat.st_uid and g == dstat.st_gid for n, u, g in calls if n.startswith(".helm-")), calls)

    check("running as root, a subfolder Helm creates takes its parent's owner (ssh writers can still add pages)",
          any(n == "Sub" and u == dstat.st_uid for n, u, g in calls), calls)

    # --- append -----------------------------------------------------------------------------------------
    r = client.post("/api/notes/append", json={"rel": "Ops Log.md", "text": "- 2026-09-26 12:00 ET - api: one line",
                                                "gap": "line"})
    check("POST /api/notes/append adds the line at the end", r.status_code == 200
          and read("Ops Log.md").endswith("first line\n- 2026-09-26 12:00 ET - api: one line\n"), (r.status_code, r.text[:200]))
    r = client.post("/api/notes/append", json={"rel": "Ops Log.md", "text": "## block\n\nbody\n"})
    check("the default gap is the bridge's: one blank line between the old text and the block",
          r.status_code == 200 and read("Ops Log.md").endswith("one line\n\n## block\n\nbody\n"), repr(read("Ops Log.md")[-80:]))
    write("Docker/no-newline.md", "# x\n\nlast line without newline")
    client.post("/api/notes/append", json={"rel": "Docker/no-newline.md", "text": "- next", "gap": "line"})
    check("an append never glues onto a last line that lacks its newline",
          read("Docker/no-newline.md").endswith("without newline\n- next\n"), repr(read("Docker/no-newline.md")))
    check("an append keeps the page's mode", (os.stat(os.path.join(WIKI, "Ops Log.md")).st_mode & 0o777) == 0o664)

    before = read("Ops Log.md")
    errs = []

    def via_helm(i):
        try:
            c = notes.db()
            try:
                edit.append(c, "Ops Log.md", "- helm %02d" % i, gap="line", source="race")
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    def direct(i):
        with open(os.path.join(WIKI, "Ops Log.md"), "a", encoding="utf-8") as fh:
            fh.write("- ssh %02d\n" % i)

    ts = [threading.Thread(target=via_helm, args=(i,)) for i in range(25)]
    ts += [threading.Thread(target=direct, args=(i,)) for i in range(25)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    after = read("Ops Log.md")
    lines = set(after.split("\n"))
    check("25 Helm appends racing 25 direct `cat >>` appends: all 50 lines land, none lost",
          not errs and all(("- helm %02d" % i) in lines and ("- ssh %02d" % i) in lines for i in range(25)),
          (errs[:2], [i for i in range(25) if ("- helm %02d" % i) not in lines]))
    check("the page only grew: the old text is intact at the start", after.startswith(before) and len(after) > len(before))

    r = client.post("/api/notes/append", json={"rel": "Docker/missing.md", "text": "x"})
    check("appending to a page that does not exist is a 404 without create", r.status_code == 404, r.status_code)
    r = client.post("/api/notes/append", json={"rel": "Docker/made-by-append.md", "text": "# made\n", "create": True})
    check("with create: true the page is made in an existing folder", r.status_code == 201
          and read("Docker/made-by-append.md") == "# made\n", (r.status_code, r.text[:200]))
    r = client.post("/api/notes/append", json={"rel": "Brandnew/x.md", "text": "x", "create": True})
    check("a brand-new top-level folder is refused (400), as for any new note", r.status_code == 400, r.status_code)
    r = client.post("/api/notes/append", json={"rel": "Ops Log.md", "text": "   \n"})
    check("a blank append is a 400", r.status_code == 400, r.status_code)
    size = os.path.getsize(os.path.join(WIKI, "Changelog.md"))
    r = client.post("/api/notes/append", json={"rel": "Changelog.md", "text": "- token = abcdefghijklmnop"})
    check("an appended line that looks like a credential is refused (400, secret) and nothing is written",
          r.status_code == 400 and r.json().get("secret") is True and os.path.getsize(os.path.join(WIKI, "Changelog.md")) == size,
          (r.status_code, r.text[:200]))
    r = client.post("/api/notes/append", json={"rel": "Playbooks/Keys.md", "text": "- api_key: zzzzzzzzzzzzzzzz"})
    check("a page that already carries a credential-shaped line is exempt (the save rule)", r.status_code == 200, r.status_code)
    r = client.post("/api/notes/append", json={"rel": "Journal/Day/2020-01-01.md", "text": "- appended later", "gap": "line"})
    check("a past day's note accepts an append (the one write history takes)", r.status_code == 200
          and read("Journal/Day/2020-01-01.md").endswith("- appended later\n"), r.status_code)
    r = client.post("/api/notes/append", json={"rel": ".wiki-engine.auth.json", "text": "x"})
    check("a hidden path is refused", r.status_code in (400, 403, 404), r.status_code)
    hit = [h["rel"] for h in notes.search(con, "helm")]
    check("the index sees an append at once (FTS finds the new text)", "Ops Log.md" in hit, hit[:5])

    # --- recent -------------------------------------------------------------------------------------------
    r = client.get("/api/notes/recent", params={"limit": 3})
    rels = [n["rel"] for n in r.json().get("notes", [])] if r.status_code == 200 else []
    check("GET /api/notes/recent lists pages newest first", r.status_code == 200 and len(rels) == 3
          and rels[0] in ("Journal/Day/2020-01-01.md", "Playbooks/Keys.md", "Docker/made-by-append.md"), (r.status_code, rels))
    r = client.get("/api/notes/recent", params={"since": 4102444800})
    check("since filters by mtime", r.status_code == 200 and r.json().get("notes") == [], r.text[:120])

    # --- delete ---------------------------------------------------------------------------------------------
    r = client.request("DELETE", "/api/notes/note", params={"rel": "Docker/new-page.md"})
    moved = r.json().get("trashed_to", "") if r.status_code == 200 else ""
    check("DELETE /api/notes/note moves the page to Trash/<stamp>/<rel> (the bridge's layout)",
          r.status_code == 200 and moved.startswith("Trash/") and moved.endswith("/Docker/new-page.md")
          and os.path.isfile(os.path.join(WIKI, moved)) and not os.path.exists(os.path.join(WIKI, "Docker/new-page.md")),
          (r.status_code, r.text[:200]))
    check("the deleted page leaves the index", not notes.exists(con, "Docker/new-page.md"))
    r = client.request("DELETE", "/api/notes/note", params={"rel": "Docker/never-was.md"})
    check("deleting a missing page is a 404", r.status_code == 404, r.status_code)

    # --- a rewrite never clobbers a direct write it did not see ---------------------------------------------
    cur = edit.read(con, "Changelog.md")
    with open(os.path.join(WIKI, "Changelog.md"), "a", encoding="utf-8") as fh:
        fh.write("- 2026-09-26 12:30 ET - ssh - direct line Helm has not read\n")
    try:
        edit._commit(con, "Changelog.md", cur["text"] + "- stale rewrite\n", "test",
                     expect=(cur["mtime"], cur["size"]))
        clobbered = True
    except edit.EditError as e:
        clobbered = e.status != 409
    check("a folder-mode rewrite whose file changed since it was read is a 409, not a clobber",
          not clobbered and "direct line Helm has not read" in read("Changelog.md"))
    r = client.put("/api/notes/note", json={"rel": "Changelog.md", "text": "- only this line\n", "create": True})
    check("a create:true save (a script's write) of an existing page still replaces it (the bridge's write)",
          r.status_code == 200 and read("Changelog.md") == "- only this line\n", (r.status_code, r.text[:120]))
    media = "# media server\n\ncurl -s 'http://192.0.2.11:8096/library/sections?X-Media-Token=<MEDIA_TOKEN>'\n"
    r = client.put("/api/notes/note", json={"rel": "Docker/media.md", "text": media, "create": True})
    check("PUT /api/notes/note saves a documented placeholder (X-Media-Token=<MEDIA_TOKEN>): 201, not a credential 400",
          r.status_code == 201 and read("Docker/media.md") == media, (r.status_code, r.text[:200]))
    r = client.put("/api/notes/note", json={"rel": "Docker/media2.md", "create": True,
                                            "text": "# media server\n\ncurl '...?X-Media-Token=Zr8kq3LmP0aa'\n"})
    check("PUT /api/notes/note still refuses a real token value (400, secret) and writes nothing",
          r.status_code == 400 and r.json().get("secret") is True and not os.path.exists(os.path.join(WIKI, "Docker/media2.md")),
          (r.status_code, r.text[:200]))

    # --- a write never leaves the folder through a planted symlink -----------------------------------------
    outside = os.path.join(TMP, "outside")
    os.makedirs(outside, exist_ok=True)
    os.symlink(outside, os.path.join(WIKI, "Docker", "link-out"))
    for label, call in (
            ("PUT create:true", lambda: client.put("/api/notes/note", json={"rel": "Docker/link-out/owned.md", "text": "x\n", "create": True})),
            ("append create:true", lambda: client.post("/api/notes/append", json={"rel": "Docker/link-out/owned.md", "text": "x", "create": True})),
            ("PUT create:true one folder deeper", lambda: client.put("/api/notes/note", json={"rel": "Docker/link-out/a/b.md", "text": "x\n", "create": True}))):
        r = call()
        check("%s through a symlinked folder that leaves the root is refused and writes nothing outside" % label,
              r.status_code in (400, 403, 404, 409) and os.listdir(outside) == [], (r.status_code, r.text[:160], os.listdir(outside)))
    os.rename(os.path.join(WIKI, "Trash"), os.path.join(WIKI, "Trash.real"))
    os.symlink(outside, os.path.join(WIKI, "Trash"))
    r = client.request("DELETE", "/api/notes/note", params={"rel": "Docker/made-by-append.md"})
    check("a delete never moves a page out through a symlinked Trash", r.status_code in (400, 403) and os.listdir(outside) == []
          and os.path.exists(os.path.join(WIKI, "Docker/made-by-append.md")), (r.status_code, os.listdir(outside)))
    os.unlink(os.path.join(WIKI, "Trash"))
    os.rename(os.path.join(WIKI, "Trash.real"), os.path.join(WIKI, "Trash"))
    os.unlink(os.path.join(WIKI, "Docker", "link-out"))

    # --- review of PR #8: races the first version lost -----------------------------------------------------
    write("Docker/rename-me.md", "# rename me\n")
    write("Docker/linker.md", "# linker\n\nSee [[rename-me]].\n", mode=0o664)
    notes.full_index()
    real_current, fired = edit._current, []

    def racing_current(c, rel):
        out = real_current(c, rel)
        if rel == "Docker/linker.md":
            fired.append(1)
        if rel == "Docker/linker.md" and len(fired) == 2:   # the rewrite's read (the 1st is the plan's count)
            with open(os.path.join(WIKI, "Docker/linker.md"), "a", encoding="utf-8") as fh:
                fh.write("- ssh line during the rename\n")
        return out
    edit._current = racing_current
    try:
        plan = edit.rename(con, "Docker/rename-me.md", "renamed", dry_run=False, source="test")
    finally:
        edit._current = real_current
    lk = read("Docker/linker.md")
    check("a rename's rewrite of a linking page never drops a line another writer added mid-rename (retries)",
          plan.get("done") and "- ssh line during the rename" in lk and "[[renamed]]" in lk, repr(lk))
    rows = [w for w in notes.writes_since(con, 0) if w["rel"] == "Docker/linker.md" and w["kind"] == "write"]
    check("the rename's rewrite of a linking page keeps the text it replaced", rows and rows[-1]["prev_size"], rows[-1:])

    cur = edit.read(con, "Docker/linker.md")
    with open(os.path.join(WIKI, "Docker/linker.md"), "a", encoding="utf-8") as fh:
        fh.write("- another direct line\n")
    try:
        edit._commit(con, "Docker/linker.md", cur["text"] + "- stale\n", "test", expect=(cur["mtime"], cur["size"]))
        got = None
    except edit.EditError as e:
        got = e
    check("the moved-file 409 carries the disk text (the editor's 'take the disk version' must not blank the page)",
          got is not None and got.status == 409 and "- another direct line" in (got.extra.get("text") or ""),
          got and got.extra.keys())

    write("Docker/race-new.md", "# made by someone else a moment ago\n")
    try:
        edit._commit(con, "Docker/race-new.md", "# mine\n", "test", expect=edit.ABSENT)
        got = None
    except edit.EditError as e:
        got = e
    check("creating a note that another writer made in the meantime is a 409, never an overwrite",
          got is not None and got.status == 409 and read("Docker/race-new.md").startswith("# made by someone else"))

    write("Docker/real-target.md", "# the real page\n")
    os.symlink("real-target.md", os.path.join(WIKI, "Docker", "alias.md"))
    notes.full_index()
    r = client.request("DELETE", "/api/notes/note", params={"rel": "Docker/alias.md"})
    check("deleting a symlinked page trashes the link, not the page it points to",
          r.status_code == 200 and os.path.isfile(os.path.join(WIKI, "Docker/real-target.md"))
          and not os.path.lexists(os.path.join(WIKI, "Docker/alias.md")), (r.status_code, r.text[:160]))

    real_fstat, n = os.fstat, []

    def moving_fstat(fd):
        st = real_fstat(fd)
        n.append(1)
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                               st.st_size, st.st_atime, 1000.0 + len(n), st.st_ctime))
    os.fstat = moving_fstat
    try:
        edit.read(con, "Docker/linker.md")
        got = None
    except edit.EditError as e:
        got = e
    finally:
        os.fstat = real_fstat
    check("a page that never holds still while read is a 409, not stale text with a fresh mtime",
          got is not None and got.status == 409, got)

    xp = write("Docker/exported.md", "# old\n", mode=0o664)
    from notebook import export
    export.atomic_write(xp, b"# new\n")
    check("the store export keeps a file's mode too (it writes the folder at the flip)",
          (os.stat(xp).st_mode & 0o777) == 0o664 and read("Docker/exported.md") == "# new\n", oct(os.stat(xp).st_mode & 0o777))

    con.execute("INSERT INTO writes (ts, rel, kind, prev_text) VALUES (?,?,?,?)", (1.0, "old.md", "write", "x" * 100))
    con.commit()
    notes.log_write(con, "Docker/linker.md", "write", "test", path=os.path.join(WIKI, "Docker/linker.md"), prev_text="y")
    old_prev = con.execute("SELECT prev_text FROM writes WHERE rel='old.md'").fetchone()[0]
    check("undo text older than 30 days is pruned (the log stays small; the row itself stays)", old_prev is None, old_prev)

    # --- delta review of PR #8: the 409's disk text never comes from outside the folder --------------------
    secret_file = os.path.join(TMP, "outside-secret.txt")
    with open(secret_file, "w") as fh:
        fh.write("OUTSIDE-SECRET-BYTES\n")
    os.chmod(secret_file, 0o600)
    write("Docker/V.md", "# V\n")
    for target in (secret_file, "/proc/self/environ"):
        os.remove(os.path.join(WIKI, "Docker/V.md"))
        os.symlink(target, os.path.join(WIKI, "Docker/V.md"))
        e = edit._moved("Docker/V.md")
        body = repr(e.extra)
        check("the moved-file answer for a page swapped to a link out (%s) is a 404 with no text" % os.path.basename(target),
              e.status == 404 and "text" not in e.extra and "OUTSIDE-SECRET" not in body and "HELM_APP_TOKEN" not in body
              and "test-token" not in body, (e.status, body[:120]))
        os.remove(os.path.join(WIKI, "Docker/V.md"))
        write("Docker/V.md", "# V\n")
    e = edit._moved("Docker/V.md")
    check("the moved-file answer for a real page still carries its disk text",
          e.status == 409 and e.extra.get("text") == "# V\n", (e.status, e.extra))
    os.link(os.path.join(WIKI, "Docker/V.md"), os.path.join(TMP, "hardlink-out"))
    e = edit._moved("Docker/V.md")
    check("a page with a second hard link gets no text either", e.status == 404 and "text" not in e.extra, e.extra)
    os.remove(os.path.join(TMP, "hardlink-out"))

    stop, leaks, tries = [], [], 0
    vp = os.path.join(WIKI, "Docker/V.md")

    def flipper():
        while not stop:
            try:
                os.remove(vp)
            except FileNotFoundError:
                pass
            try:
                os.symlink(secret_file, vp)
                os.remove(vp)
            except OSError:
                pass
            try:
                with open(vp, "w") as fh:
                    fh.write("# V\n")
            except OSError:
                pass
    th = threading.Thread(target=flipper, daemon=True)
    th.start()
    try:
        for i in range(300):
            tries += 1
            r = client.put("/api/notes/note", json={"rel": "Docker/V.md", "text": "# V %d\n" % i, "create": True})
            if "OUTSIDE-SECRET" in r.text:
                leaks.append(i)
    finally:
        stop.append(1)
        th.join(timeout=5)
    check("300 plain saves racing a thread that flips the page to a link out never leak outside bytes",
          leaks == [], (tries, leaks[:5]))
    for pth in (vp,):
        if os.path.islink(pth) or os.path.exists(pth):
            os.remove(pth)

    out_owned = os.path.join(TMP, "outside-owned.md")
    with open(out_owned, "w") as fh:
        fh.write("outside\n")
    os.chmod(out_owned, 0o600)
    lp = os.path.join(WIKI, "Docker/exported-link.md")
    os.symlink(out_owned, lp)
    notes.atomic_write(lp, b"# rewritten as a plain file\n")
    lmode = os.stat(lp).st_mode & 0o777
    check("a write over a link out takes the folder's mode, never the outside file's (0600)",
          not os.path.islink(lp) and lmode != 0o600 and lmode & 0o044 == 0o044
          and open(out_owned).read() == "outside\n", oct(lmode))

    real_commit, made = edit._commit, []

    def racing_commit(c, rel, text, source, **kw):
        if kw.get("expect") is edit.ABSENT and rel == "Docker/raced-create.md" and not made:
            made.append(1)
            write("Docker/raced-create.md", "# made by the other writer\n")
        return real_commit(c, rel, text, source, **kw)
    edit._commit = racing_commit
    try:
        out = edit.append(con, "Docker/raced-create.md", "- my line", gap="line", create=True, source="test")
    finally:
        edit._commit = real_commit
    check("an append-create that loses the race to another creator appends instead of failing",
          read("Docker/raced-create.md") == "# made by the other writer\n- my line\n" and not out.get("created"),
          repr(read("Docker/raced-create.md")))

    # --- third review of PR #8: no path is resolved by name in a folder others write (W10) ---------------
    def race(flip, n, act):
        """Run act(i) n times while flip() runs in a thread; returns what act collected."""
        stop, got = [], []

        def loop():
            while not stop:
                try:
                    flip()
                except OSError:
                    pass
        th = threading.Thread(target=loop, daemon=True)
        th.start()
        try:
            for i in range(n):
                try:
                    got.append(act(i))
                except Exception as e:  # noqa: BLE001
                    got.append(("raised", repr(e)))
        finally:
            stop.append(1)
            th.join(timeout=5)
        return got

    lp2 = os.path.join(WIKI, "Docker/leak.md")

    def flip_page():
        try:
            os.remove(lp2)
        except FileNotFoundError:
            pass
        os.symlink("/proc/self/environ", lp2)
        os.remove(lp2)
        with open(lp2, "w") as fh:
            fh.write("# leak\n")

    def save_then_view(i):
        client.put("/api/notes/note", json={"rel": "Docker/leak.md", "text": "# leak %d\n" % i, "create": True})
        return client.get("/api/notes/note", params={"rel": "Docker/leak.md"}).text
    got = race(flip_page, 300, save_then_view)
    check("saves racing a page<->/proc/self/environ flip never put the environment into the index (GET note)",
          not any("HELM_APP_TOKEN" in str(g) or "test-token" in str(g) for g in got),
          [i for i, g in enumerate(got) if "test-token" in str(g)][:5])

    def rescan_then_view(i):
        notes.import_path(con, lp2, force=True)
        return client.get("/api/notes/note", params={"rel": "Docker/leak.md"}).text
    got = race(flip_page, 300, rescan_then_view)
    check("the index pass racing the same flip never indexes the environment",
          not any("test-token" in str(g) for g in got), [i for i, g in enumerate(got) if "test-token" in str(g)][:5])
    # the same window, forced: swap the page for a link out right after the name was checked
    for pth in (lp2,):
        if os.path.lexists(pth):
            os.remove(pth)
    write("Docker/leak.md", "# leak\n")
    real_cp = notes.confined_path

    def swapping_cp(rel, hidden_ok=False):
        out = real_cp(rel, hidden_ok)
        if rel in ("Docker/leak.md", "attachments/pic.png") and out and not os.path.islink(out):
            os.remove(out)
            os.symlink(secret_file, out)
        return out
    notes.confined_path = swapping_cp
    try:
        notes.import_path(con, lp2, force=True)
    finally:
        notes.confined_path = real_cp
    row = con.execute("SELECT text FROM notes WHERE rel='Docker/leak.md'").fetchone()
    check("the index never reads a page swapped for a link out after its check (forced window)",
          not (row and "OUTSIDE-SECRET" in (row[0] or "")), row and row[0][:60])
    if os.path.lexists(lp2):
        os.remove(lp2)

    write("attachments/pic.png", b"\x89PNG fake")
    notes.full_index()
    ap = os.path.join(WIKI, "attachments/pic.png")

    def flip_att():
        os.remove(ap)
        os.symlink("/proc/self/environ", ap)
        os.remove(ap)
        with open(ap, "wb") as fh:
            fh.write(b"\x89PNG fake")
    got = race(flip_att, 300, lambda i: client.get("/att/attachments/pic.png").content)
    check("/att racing an attachment<->/proc/self/environ flip never serves the environment",
          not any(b"test-token" in (g if isinstance(g, bytes) else str(g).encode()) for g in got))
    if os.path.lexists(ap):
        os.remove(ap)
    write("attachments/pic.png", b"\x89PNG fake")
    notebook_api.notes.confined_path = swapping_cp
    try:
        body = client.get("/att/attachments/pic.png").content
    finally:
        notebook_api.notes.confined_path = real_cp
    check("/att never serves an attachment swapped for a link out after its check (forced window)",
          b"OUTSIDE-SECRET" not in body, body[:40])
    if os.path.lexists(ap):
        os.remove(ap)

    outside2 = os.path.join(TMP, "outside2")
    os.makedirs(outside2, exist_ok=True)
    subd = os.path.join(WIKI, "Docker/Swapped")
    os.makedirs(subd, exist_ok=True)

    def flip_dir():
        if os.path.islink(subd):
            os.remove(subd)
            os.makedirs(subd, exist_ok=True)
        else:
            shutil.rmtree(subd, ignore_errors=True)
            os.symlink(outside2, subd)
    got = race(flip_dir, 300, lambda i: client.put("/api/notes/note", json={
        "rel": "Docker/Swapped/p%d.md" % (i % 7), "text": "# p %d\n" % i, "create": True}).status_code)
    check("creates racing a folder<->link-out flip never write anything outside (no page, no temp file)",
          os.listdir(outside2) == [], (os.listdir(outside2)[:5], sorted(set(g for g in got if not isinstance(g, tuple)))))
    if os.path.islink(subd):
        os.remove(subd)

    lines = ["- creator %02d" % i for i in range(20)]
    ths = [threading.Thread(target=lambda ln=ln: client.post("/api/notes/append", json={
        "rel": "Docker/born-racing.md", "text": ln, "gap": "line", "create": True})) for ln in lines]
    for th2 in ths:
        th2.start()
    for th2 in ths:
        th2.join()
    br = read("Docker/born-racing.md")
    check("20 writers append-creating the same new page at once: every acknowledged line is in it",
          all(ln in br for ln in lines), [ln for ln in lines if ln not in br])

    write("Docker/rn2-target.md", "# target\n")
    write("Docker/rn2-a.md", "See [[rn2-target]].\n")
    write("Docker/rn2-b.md", "Also [[rn2-target]].\n")
    notes.full_index()
    real_current2, gone = edit._current, []

    def vanishing_current(c, rel, **kw):
        out = real_current2(c, rel, **kw)
        if rel == "Docker/rn2-a.md":
            gone.append(1)
            if len(gone) == 2:   # the rewrite's read: the page goes before its commit
                os.remove(os.path.join(WIKI, "Docker/rn2-a.md"))
        return out
    edit._current = vanishing_current
    try:
        try:
            plan2 = edit.rename(con, "Docker/rn2-target.md", "rn2-renamed", dry_run=False, source="test")
        except edit.EditError as e:
            plan2 = {"raised": e.status}
    finally:
        edit._current = real_current2
    check("a rename whose linking page vanishes mid-rename still finishes the other pages (W8 holds)",
          plan2.get("done") and "[[rn2-renamed]]" in read("Docker/rn2-b.md"), plan2)

    write("Docker/crlf.md", b"# crlf\r\n\r\nline\r\n")
    check("the editor's read normalises CRLF as the index does", edit.read(con, "Docker/crlf.md")["text"] == "# crlf\n\nline\n")

    # --- the write log ---------------------------------------------------------------------------------
    RACE_RELS = {"old.md", "Docker/V.md", "Docker/leak.md", "Docker/born-racing.md"} | {"Docker/Swapped/p%d.md" % i for i in range(7)}
    r = client.get("/api/notes/writes", params={"since": 0})
    rows = r.json().get("writes", []) if r.status_code == 200 else []
    kinds = {w["kind"] for w in rows}
    check("GET /api/notes/writes lists Helm's writes with kind, source and the sha after",
          r.status_code == 200 and {"write", "append", "delete"} <= kinds
          and all(w.get("sha_after") or w["kind"] == "delete" for w in rows if w["rel"] not in RACE_RELS), (r.status_code, [w for w in rows if not w.get("sha_after") and w["kind"] != "delete" and w["rel"] not in RACE_RELS][:3]))
    last = [w for w in rows if w["rel"] == "Changelog.md"][-1] if rows else {}
    check("the last write's sha matches the file on disk (what the straggler check compares)",
          last.get("sha_after") == hashlib.sha256(open(p, "rb").read()).hexdigest(), last)
    rewrite = [w for w in rows if w["rel"] == "Changelog.md" and w["kind"] == "write"]
    check("a rewrite keeps the text it replaced (the undo while the folder is the truth)",
          rewrite and rewrite[-1].get("prev_size", 0) > len("- only this line\n"), rewrite[-1:] )
    check("a direct write is not in the log (so the straggler check can see it)",
          not any("ssh" in (w.get("source") or "") for w in rows))

    # --- the new routes are write routes (405 at a read-only site) ------------------------------------------
    wr = {(tuple(sorted(r.methods or [])), r.path) for r in notebook_api.write_routes()}
    check("append and delete are write routes (refused 405 wherever the notebook is read-only)",
          (("POST",), "/api/notes/append") in wr and (("DELETE",), "/api/notes/note") in wr, sorted(wr))

    con.close()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
