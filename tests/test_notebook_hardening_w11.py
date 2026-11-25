"""Review 8d's follow-ups, closed (DECISIONS W11). Home profile, folder mode (the wiki bridge).

- an append that created its note never answers an error after the create landed (a caller's retry
  would write the line twice), and append_daily neither;
- a rename lists a linking page it could not rewrite (a 400 or an OSError after the page moved)
  instead of stopping halfway;
- move_rel is one atomic no-overwrite rename (renameat2 RENAME_NOREPLACE), and its fallback never
  unlinks a newer version a writer put at the source, nor leaves a page in Trash while answering 404;
- /att streams from the checked fd with one Range (206, capped when open-ended), an ETag (304) and
  a filename* download name, and the web role forwards Range / If-None-Match and passes them back;
- the name-based pre-checks and a folder removed under a write answer 404/409, never a 500;
- the folder walks never descend through a folder swapped for a link mid-walk (os.fwalk);
- confined_path agrees with W10: a link inside the folder is refused too;
- GET /api/notes/writes carries added_len (the bytes an append added) for the straggler check;
- the notebook screen never says the wiki engine or "the wiki is the source of truth".
"""
# temp-ok: the folders and DBs under test are fixtures this module creates and deletes
from __future__ import annotations

import datetime as dt
import errno
import os
import re
import shutil
import stat
import sys
import tempfile
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_w11_")
WIKI = os.path.join(TMP, "wiki")
OUT = os.path.join(TMP, "outside")
TOKEN = "test-token"
os.environ["HELM_SITE"] = "home"
os.environ["HELM_NOTES_PROFILE"] = "wiki"
os.environ["HELM_NOTES_MODE"] = "folder"
os.environ["HELM_NOTES_ROOT"] = WIKI
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_APP_TOKEN"] = TOKEN   # force: a persisted real token must never leak into tests
os.environ["HELM_API_URL"] = "http://api.test"

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from notebook import edit, notes  # noqa: E402
import app.notebook_api as notebook_api  # noqa: E402
import app.web as web  # noqa: E402

PASS = FAIL = 0
AUTH = {"X-Forwarded-User": "owner"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def write(rel, data):
    p = os.path.join(WIKI, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data.encode() if isinstance(data, str) else data)
    return p


def read(rel):
    with open(os.path.join(WIKI, rel), encoding="utf-8") as fh:
        return fh.read()


def att(rel):
    return "/att/" + quote(rel, safe="")


def run() -> int:
    os.makedirs(OUT, exist_ok=True)
    write("index.md", "# Home\n")
    write("Docker/linker-a.md", "See [[target]].\n")
    write("Docker/linker-b.md", "Also [[target]].\n")
    write("Docker/target.md", "# target\n")
    write("attachments/clip.mp4", bytes(range(256)) * 4)   # 1024 bytes
    write("attachments/Café 日本.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
    write("attachments/a%20b.png", b"PCT-LITERAL")        # a literal %20 in the name
    write("attachments/a b.png", b"PCT-SPACE")             # its decoded twin
    notes.full_index()
    con = notes.db()
    api = TestClient(Starlette(routes=notebook_api.routes), headers={"Authorization": "Bearer " + TOKEN})
    web.UPSTREAM_TRANSPORT = httpx.ASGITransport(app=Starlette(routes=notebook_api.routes))
    wc = TestClient(web.app, client=("127.0.0.1", 40000))

    # --- 1. an append that created its note never errors after the create landed -------------------
    real_stat, misses = notes.stat_confined, []

    def missing_once(rel, **kw):
        if rel in ("Docker/born.md", notes.daily_rel(dt.date(2020, 2, 2))) and len(misses) < 2 and rel not in misses:
            misses.append(rel)
            return None
        return real_stat(rel, **kw)
    notes.stat_confined = missing_once
    try:
        try:
            out = edit.append(con, "Docker/born.md", "- the line", gap="line", create=True, source="t")
        except edit.EditError as e:
            out = {"raised": e.status}
        try:
            day = edit.append_daily(con, dt.date(2020, 2, 2), "- a day line", source="t")
        except edit.EditError as e:
            day = {"raised": e.status}
    finally:
        notes.stat_confined = real_stat
    check("an append-create whose stat misses right after the create is done, and the line is written once",
          out.get("created") and read("Docker/born.md") == "- the line\n", (out, read("Docker/born.md")))
    check("append_daily creating the day never answers a retryable error after the create landed",
          "raised" not in day and read(notes.daily_rel(dt.date(2020, 2, 2))).count("- a day line") == 1, day)

    # --- 2. a rename lists a linking page it could not rewrite ------------------------------------------
    real_commit = edit._commit

    def refusing_commit(c, rel, text, source, **kw):
        if rel == "Docker/linker-a.md" and kw.get("prev") is not None:
            raise edit.EditError(400, "that path leads out of the notes folder")
        if rel == "Docker/linker-b.md" and kw.get("prev") is not None:
            raise OSError(errno.EIO, "disk said no")
        return real_commit(c, rel, text, source, **kw)
    edit._commit = refusing_commit
    try:
        try:
            plan = edit.rename(con, "Docker/target.md", "renamed", dry_run=False, source="t")
        except Exception as e:  # noqa: BLE001
            plan = {"raised": repr(e)}
    finally:
        edit._commit = real_commit
    check("a rename whose linking pages refuse (400, OSError) finishes and lists them, never stops halfway",
          plan.get("done") and set(plan.get("links_not_rewritten") or []) == {"Docker/linker-a.md", "Docker/linker-b.md"}
          and os.path.exists(os.path.join(WIKI, "Docker/renamed.md")), plan)

    # --- 3. move_rel: one atomic no-overwrite rename ---------------------------------------------------------
    write("Docker/m-src.md", "# src\n")
    write("Docker/m-dst.md", "# already here\n")
    try:
        notes.move_rel("Docker/m-src.md", "Docker/m-dst.md")
        moved = "moved over"
    except FileExistsError:
        moved = "refused"
    check("move_rel never overwrites an existing destination, and both stay intact",
          moved == "refused" and read("Docker/m-src.md") == "# src\n" and read("Docker/m-dst.md") == "# already here\n", moved)
    ino = os.stat(os.path.join(WIKI, "Docker/m-src.md")).st_ino
    notes.move_rel("Docker/m-src.md", "Docker/Sub2/m-moved.md")
    check("move_rel moves the name itself (same inode), leaving nothing at the source",
          os.stat(os.path.join(WIKI, "Docker/Sub2/m-moved.md")).st_ino == ino
          and not os.path.lexists(os.path.join(WIKI, "Docker/m-src.md")))
    check("move_rel uses renameat2(RENAME_NOREPLACE) where the platform has it (Linux here)",
          getattr(notes, "_renameat2", None) is not None)

    # the fallback (no renameat2): a newer version renamed over the source between link and unlink survives
    saved = getattr(notes, "_renameat2", None)
    notes._renameat2 = None
    write("Docker/f-src.md", "# old\n")
    real_link = os.link

    def link_then_replace(src, dst, *a, **kw):
        real_link(src, dst, *a, **kw)
        tmp = os.path.join(WIKI, "Docker/.newer")
        with open(tmp, "w") as fh:
            fh.write("# newer, written meanwhile\n")
        os.replace(tmp, os.path.join(WIKI, "Docker/f-src.md"))
    os.link = link_then_replace
    try:
        try:
            notes.move_rel("Docker/f-src.md", "Docker/f-dst.md")
            fb = "moved"
        except (FileExistsError, notes.Moved, OSError) as e:
            fb = type(e).__name__
    finally:
        os.link = real_link
        notes._renameat2 = saved
    check("the fallback move never unlinks a newer version put at the source; it backs out and says so",
          fb != "moved" and read("Docker/f-src.md") == "# newer, written meanwhile\n"
          and not os.path.lexists(os.path.join(WIKI, "Docker/f-dst.md")), (fb, os.listdir(os.path.join(WIKI, "Docker"))))

    # --- 4. /att: stream, one Range, ETag, filename* ------------------------------------------------------
    body = bytes(range(256)) * 4
    r = api.get(att("attachments/clip.mp4"), headers={"Range": "bytes=10-19"})
    check("GET /att with Range bytes=10-19 is a 206 with those 10 bytes and a Content-Range",
          r.status_code == 206 and r.content == body[10:20] and r.headers.get("content-range") == "bytes 10-19/1024"
          and r.headers.get("accept-ranges") == "bytes", (r.status_code, r.headers.get("content-range")))
    r = api.get(att("attachments/clip.mp4"), headers={"Range": "bytes=-16"})
    check("a suffix range is the last N bytes", r.status_code == 206 and r.content == body[-16:], r.status_code)
    saved_cap = getattr(notebook_api, "ATT_MAX_RANGE", None)
    notebook_api.ATT_MAX_RANGE = 100
    try:
        r = api.get(att("attachments/clip.mp4"), headers={"Range": "bytes=500-"})
    finally:
        if saved_cap is not None:
            notebook_api.ATT_MAX_RANGE = saved_cap
    check("an open-ended range is capped (one answer is at most that much)",
          r.status_code == 206 and r.content == body[500:600] and r.headers.get("content-range") == "bytes 500-599/1024",
          (r.status_code, r.headers.get("content-range")))
    r = api.get(att("attachments/clip.mp4"), headers={"Range": "bytes=5000-6000"})
    check("an unsatisfiable range is a 416 with bytes */size", r.status_code == 416
          and r.headers.get("content-range") == "bytes */1024", (r.status_code, r.headers.get("content-range")))
    r = api.get(att("attachments/clip.mp4"))
    etag = r.headers.get("etag")
    check("a plain GET is the whole file with an ETag", r.status_code == 200 and r.content == body and etag, r.status_code)
    r = api.get(att("attachments/clip.mp4"), headers={"If-None-Match": etag or "x"})
    check("If-None-Match with the ETag is a 304 with no body", r.status_code == 304 and r.content == b"", r.status_code)
    name = "Café 日本.svg"
    r = api.get(att("attachments/" + name))
    cd = r.headers.get("content-disposition", "")
    check("an active attachment with a non-latin-1 name downloads (filename*), never a 500",
          r.status_code == 200 and cd.startswith("attachment") and "filename*=UTF-8''" in cd
          and r.headers.get("content-type", "").startswith("application/octet-stream"), (r.status_code, cd))
    # Starlette's TestClient unquotes the path a second time (httpx hands it an already-decoded
    # path), which a real server never does, so this drives the app with the scope uvicorn builds:
    # `path` decoded once, `raw_path` as sent.
    import asyncio

    def asgi_get(raw):
        got = {}
        scope = {"type": "http", "method": "GET", "path": unquote(raw), "raw_path": raw.encode(),
                 "query_string": b"", "headers": [(b"authorization", ("Bearer " + TOKEN).encode())],
                 "http_version": "1.1", "scheme": "http", "server": ("t", 80), "client": ("t", 1),
                 "root_path": ""}

        async def drive():
            sent = []
            done = asyncio.Event()

            async def receive():
                if not sent:
                    sent.append(1)
                    return {"type": "http.request", "body": b"", "more_body": False}
                await done.wait()                 # a streamed body polls for a disconnect
                return {"type": "http.disconnect"}

            async def send(msg):
                if msg["type"] == "http.response.start":
                    got["status"] = msg["status"]
                elif msg["type"] == "http.response.body":
                    got["body"] = got.get("body", b"") + msg.get("body", b"")
                    if not msg.get("more_body"):
                        done.set()
            await Starlette(routes=notebook_api.routes)(scope, receive, send)
        asyncio.run(asyncio.wait_for(drive(), 20))
        return got.get("status"), got.get("body", b"")
    st, got = asgi_get("/att/attachments%2Fa%2520b.png")
    check("/att decodes the path once: a file named with a literal %20 serves itself, not its decoded twin",
          st == 200 and got == b"PCT-LITERAL", (st, got[:20]))
    st, got = asgi_get("/att/attachments%2Fa%20b.png")
    check("/att: the file named with a real space still serves its own bytes",
          st == 200 and got == b"PCT-SPACE", (st, got[:20]))
    os.remove(os.path.join(WIKI, "attachments/a b.png"))
    notes.full_index()
    st, got = asgi_get("/att/attachments%2Fa%2520b.png")
    check("/att: the literal-%20 file still serves with its decoded twin gone",
          st == 200 and got == b"PCT-LITERAL", (st, got[:20]))
    r = wc.get(att("attachments/clip.mp4"), headers=dict(AUTH, Range="bytes=10-19"))
    check("through the web role: Range is forwarded and the 206 + Content-Range come back",
          r.status_code == 206 and r.content == body[10:20] and r.headers.get("content-range") == "bytes 10-19/1024"
          and r.headers.get("content-security-policy") == "sandbox", (r.status_code, dict(r.headers)))
    r = wc.get(att("attachments/clip.mp4"), headers=AUTH)
    et2 = r.headers.get("etag")
    r2 = wc.get(att("attachments/clip.mp4"), headers=dict(AUTH, **{"If-None-Match": et2 or "x"}))
    check("through the web role: the ETag comes back and If-None-Match gets a 304",
          et2 and r2.status_code == 304, (et2, r2.status_code))
    check("through the web role: a plain GET is the whole file with its length",
          r.status_code == 200 and r.content == body and r.headers.get("content-length") == "1024",
          (r.status_code, len(r.content), r.headers.get("content-length")))
    r = wc.head(att("attachments/clip.mp4"), headers=AUTH)
    check("through the web role: HEAD is the headers with no body, sandboxed",
          r.status_code == 200 and r.content == b"" and r.headers.get("content-length") == "1024"
          and r.headers.get("etag") == et2 and r.headers.get("content-security-policy") == "sandbox",
          (r.status_code, len(r.content), dict(r.headers)))
    r = wc.get(att("attachments/nope.png"), headers=AUTH)
    check("through the web role: a missing attachment is the api's 404, sandboxed",
          r.status_code == 404 and r.headers.get("content-security-policy") == "sandbox", r.status_code)

    # --- 5. name-based pre-checks and a folder removed under a write: never a 500 ----------------------
    real_rp = os.path.realpath

    def einval(p, *a, **kw):
        if "flipme" in str(p):
            raise OSError(errno.EINVAL, "Invalid argument")
        return real_rp(p, *a, **kw)
    os.makedirs(os.path.join(WIKI, "Docker", "flipme-dir"), exist_ok=True)
    os.path.realpath = einval
    try:
        a = notes.confined_path("Docker/flipme.md")
        b = notes.write_target("Docker/flipme-dir/x.md")   # write_target resolves the nearest existing folder
        r = api.get("/api/notes/note", params={"rel": "Docker/flipme.md"})
        r2 = api.get(att("attachments/flipme.png"))
    finally:
        os.path.realpath = real_rp
    check("confined_path / write_target answer None when realpath fails mid-flip (EINVAL), and the routes 404/403",
          a is None and b is None and r.status_code in (403, 404) and r2.status_code in (403, 404),
          (a, b, r.status_code, r2.status_code))
    gone_dir = os.path.join(WIKI, "Docker/Vanishing")
    os.makedirs(gone_dir, exist_ok=True)
    real_walk = notes._walk

    def walk_then_remove(parts, create=False):
        fd = real_walk(parts, create)
        if parts and parts[-1] == "Vanishing" and fd is not None and os.path.isdir(gone_dir):
            shutil.rmtree(gone_dir)
        return fd
    notes._walk = walk_then_remove
    try:
        r = api.put("/api/notes/note", json={"rel": "Docker/Vanishing/p.md", "text": "# p\n", "create": True})
    finally:
        notes._walk = real_walk
    check("a write whose folder is removed under it answers 404/409, not a 500", r.status_code in (404, 409), r.status_code)

    # --- 6. the walks never descend through a folder swapped for a link mid-walk -------------------------
    write("Docker/Deep/inside.md", "# inside\n")
    with open(os.path.join(OUT, "outside-secret.md"), "w") as fh:
        fh.write("# OUTSIDE\n")
    seen = []
    real_import = notes.import_path

    def rec_import(c, path, *a, **kw):
        seen.append(path)
        return real_import(c, path, *a, **kw)
    real_scandir, swapped = os.scandir, []

    def swapping_scandir(p=None):
        it = list(real_scandir(p)) if p is not None else list(real_scandir())
        target = os.path.join(WIKI, "Docker", "Deep")
        where = os.readlink("/proc/self/fd/%d" % p) if isinstance(p, int) else str(p)
        if where.rstrip("/").endswith("/Docker") and not swapped and os.path.isdir(target) and not os.path.islink(target):
            swapped.append(1)
            shutil.rmtree(target)
            os.symlink(OUT, target)
        return _FakeScandir(it)
    notes.import_path = rec_import
    os.scandir = swapping_scandir
    try:
        notes.rebuild(con, force=True)
    finally:
        os.scandir = real_scandir
        notes.import_path = real_import
    check("the index walk never descends into a folder swapped for a link mid-walk (no outside path offered)",
          swapped and not any("outside-secret" in str(s) for s in seen), [s for s in seen if "outside" in str(s)][:3])
    if os.path.islink(os.path.join(WIKI, "Docker", "Deep")):
        os.remove(os.path.join(WIKI, "Docker", "Deep"))

    # --- 7. confined_path agrees with W10 ------------------------------------------------------------------
    write("Docker/real-page.md", "# real\n")
    os.symlink("real-page.md", os.path.join(WIKI, "Docker", "alias-page.md"))
    check("confined_path refuses a link that stays inside the folder too (W10)",
          notes.confined_path("Docker/alias-page.md") is None and notes.confined_path("Docker/real-page.md") is not None)

    # --- 8. GET /api/notes/writes carries added_len ----------------------------------------------------------
    edit.append(con, "Docker/real-page.md", "- café line", gap="line", source="t")
    r = api.get("/api/notes/writes", params={"since": 0})
    rows = [w for w in r.json().get("writes", []) if w["rel"] == "Docker/real-page.md" and w["kind"] == "append"]
    check("GET /api/notes/writes carries added_len, the bytes an append added (UTF-8)",
          rows and rows[-1].get("added_len") == len("- café line\n".encode("utf-8")), rows[-1:] if rows else r.text[:120])

    # --- 9. the notebook screen's words ------------------------------------------------------------------------
    with open(os.path.join(ROOT, "web", "notebook", "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    strings = re.findall(r'"[^"\n]*"|\'[^\'\n]*\'|`[^`]*`', js)
    bad = [s for s in strings if re.search(r"the wiki engine|the wiki is the source of truth", s, re.I)]
    check("no user-visible string in web/notebook/app.js names the wiki engine or calls the wiki the truth", not bad, bad[:3])
    check("the read-only refusal no longer sends people to the wiki engine",
          "the wiki engine" not in edit.READONLY_DETAIL and "source of truth" not in edit.READONLY_DETAIL, edit.READONLY_DETAIL)

    # --- 11. review 18: /att never leaks its fd when the client leaves before the body ------------------
    import asyncio
    clip = os.path.realpath(os.path.join(WIKI, "attachments/clip.mp4"))

    def open_on_clip():
        n = 0
        for fd in os.listdir("/proc/self/fd"):
            try:
                if os.readlink("/proc/self/fd/" + fd) == clip:
                    n += 1
            except OSError:
                pass
        return n

    async def disconnect_at_once(headers):
        app = Starlette(routes=notebook_api.routes)
        scope = {"type": "http", "method": "GET", "path": "/att/attachments/clip.mp4",
                 "raw_path": b"/att/attachments/clip.mp4", "query_string": b"", "headers": headers,
                 "http_version": "1.1", "scheme": "http", "server": ("t", 80), "client": ("t", 1), "root_path": ""}

        async def receive():
            return {"type": "http.disconnect"}

        async def send(msg):
            pass
        await app(scope, receive, send)
    before = open_on_clip()
    for _ in range(20):
        asyncio.run(disconnect_at_once([]))
        asyncio.run(disconnect_at_once([(b"range", b"bytes=10-19")]))
    check("/att closes its fd when the client disconnects before the body starts (200 and 206)",
          open_on_clip() == before == 0, (before, open_on_clip()))

    # --- 12. review 18: a notes root that is itself a link still indexes ----------------------------------
    link_root = os.path.join(TMP, "wiki-link")
    os.symlink(WIKI, link_root)
    saved_vault = notes.VAULT
    notes.VAULT = link_root
    try:
        c2 = notes.db(os.path.join(TMP, "notes-linkroot.db"))
        rb = notes.rebuild(c2, force=True)
        n2 = c2.execute("SELECT count(*) FROM notes").fetchone()[0]
        c2.close()
    finally:
        notes.VAULT = saved_vault
    check("a rebuild with HELM_NOTES_ROOT pointing at a link to the folder indexes the pages (not an empty index)",
          n2 >= 5, (n2, rb))

    # --- 13. review 18: a move whose source vanished meanwhile is a 404, not a 500 ------------------------
    write("Docker/mv-src.md", "# mv\n")
    notes.full_index()
    real_rn = notes._rename_noreplace

    def vanished(*a, **kw):
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")
    notes._rename_noreplace = vanished
    try:
        try:
            edit.move(con, "Docker/mv-src.md", "Docker/mv-dst.md", source="t")
            mv = 200
        except edit.EditError as e:
            mv = e.status
        try:
            edit.rename(con, "Docker/mv-src.md", "mv-renamed", dry_run=False, source="t")
            rn = 200
        except edit.EditError as e:
            rn = e.status
    finally:
        notes._rename_noreplace = real_rn
    check("a move or rename whose source vanished meanwhile answers 404, not a 500", mv == 404 and rn == 404, (mv, rn))

    # --- 14. review 18: a create whose temp cleanup fails after the link landed is still one line ---------
    real_unlink = os.unlink
    fired = []

    def tmp_gone(pth, *a, **kw):
        if isinstance(pth, str) and pth.startswith(".helm-") and kw.get("dir_fd") is not None and not fired:
            fired.append(1)
            real_unlink(pth, *a, **kw)
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", pth)
        return real_unlink(pth, *a, **kw)
    os.unlink = tmp_gone
    try:
        try:
            out14 = edit.append(con, "Docker/born2.md", "- only once", gap="line", create=True, source="t")
        except edit.EditError as e:
            out14 = {"raised": e.status}
    finally:
        os.unlink = real_unlink
    check("a create whose temp-file cleanup fails after the link landed is done, with the line once",
          out14.get("created") and read("Docker/born2.md") == "- only once\n", (out14, read("Docker/born2.md")))
    r = api.get("/api/notes/writes", params={"since": 0})
    rows = r.json().get("writes", [])
    born = [w for w in rows if w["rel"] == "Docker/born2.md" and w["kind"] == "append"]
    others = [w for w in rows if w["kind"] in ("delete", "move")]
    check("added_len is the created line's bytes on an append-create row, and null on delete/move rows",
          born and born[-1]["added_len"] == len("- only once\n".encode()) and all(w.get("added_len") is None for w in others),
          (born[-1:], [w.get("added_len") for w in others][:3]))

    # --- 10. no script in the repo writes the wiki folder directly --------------------------------------------
    direct = []
    for top in ("git-hooks", "tools", "deploy"):
        for dp, dn, fn in os.walk(os.path.join(ROOT, top)):
            for f in fn:
                path = os.path.join(dp, f)
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if "wiki-engine/space" in line and re.search(r">>|cat\s*>|open\([^)]*['\"][aw]['\"]", line):
                                direct.append("%s:%d" % (os.path.relpath(path, ROOT), i))
                except OSError:
                    pass
    check("no script in the repo (git-hooks, tools, deploy) appends to the wiki folder directly", not direct, direct)

    con.close()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


class _FakeScandir:
    """os.scandir's context-manager iterator over a list (the swapping hook reads it eagerly)."""

    def __init__(self, items):
        self._it = iter(items)

    def __iter__(self):
        return self._it

    def __next__(self):
        return next(self._it)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


if __name__ == "__main__":
    raise SystemExit(run())
