"""The notebook against its own folder (PR #3 review, findings 1-4).

At home the notebook is the wiki folder, written by other tools and read in
an origin whose web role proxies authenticated API calls with the bearer injected server-side.
So the folder's content is untrusted, and this suite plants what an agent could plant:

  1. Raw HTML in a page (<script>, <img onerror>, <iframe>, on* handlers) renders as escaped
     text at a folder-readonly site, in the note view and the Day view, while the notebook's
     own markup (wikilinks, checkboxes) still renders. The Topic hub carries no rendered HTML.
     The store keeps the reference's rendering (docs/DECISIONS.md H1).
  2. A note or file whose real path leaves the root (a symlinked note, a symlinked folder,
     /proc/self/environ) is never indexed and no read path serves it: the note, raw, topic,
     search and /att routes, the per-file importer, the editor's read in folder mode and the
     export's adoption. An index written before the rule loses the row on the next pass.
  3. Dot-prefixed files and folders at any depth (.chrome-data/, .wiki-engine.auth.json,
     Personal/.cache/, Personal/.hidden-note.md) are never indexed, and /att and the note
     routes refuse them: by name, through a visible symlink that points at one, and when an old
     index still lists them.
  4. /att: at a folder-readonly site the active types (html, htm, svg, xml, xhtml, js, mjs, and a
     .png name that is a link to an .html file) download as application/octet-stream; images
     stay inline; every /att response carries X-Content-Type-Options: nosniff and
     Content-Security-Policy: sandbox, at the api role and through the web role.
"""
# temp-ok: the folders and DBs under test are fixtures this module creates and deletes
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
import time
from html.parser import HTMLParser
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_hardening_")
WIKI = os.path.join(TMP, "wiki")
OUTSIDE = os.path.join(TMP, "outside")
WORK = os.path.join(TMP, "work-notes")
SECRET = "TOPSECRET-outside-the-root-7d41"
TOKEN = "hardening-test-token-51c7"
os.environ["HELM_NOTES_PROFILE"] = "wiki"
os.environ["HELM_NOTES_MODE"] = "folder-readonly"
os.environ["HELM_NOTES_ROOT"] = WIKI
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_APP_TOKEN"] = TOKEN          # force: a persisted real token must never leak into tests
os.environ["HELM_API_URL"] = "http://api.test"
for k in ("LEGACY_DASHBOARD_URL", "LEGACY_CONSOLE_TOKEN", "HELM_WEB_REQUIRE_AUTH_HEADER"):
    os.environ.pop(k, None)

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from notebook import config, edit, export, notes, render  # noqa: E402
import app.notebook_api as notebook_api  # noqa: E402
import app.web as web  # noqa: E402

PASS = FAIL = 0
TODAY = dt.date.today().isoformat()
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
AUTH = {"X-Forwarded-User": "owner"}
ACTIVE = ("page.html", "page.htm", "pic.svg", "data.xml", "doc.xhtml", "app.js", "mod.mjs")
IMAGES = ("a.png", "a.jpg", "a.jpeg", "a.gif", "a.webp", "a.ico")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:400] if detail != "" else ""))


def write(root, rel, data, mtime=None):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def link(target, root, rel):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    os.symlink(target, p)
    return p


def att(rel):
    return "/att/" + quote(rel, safe="")


def err(fn):
    try:
        fn()
    except edit.EditError as e:
        return e
    return None


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def live(html_text):
    """What a browser would run if this HTML went into innerHTML: a raw tag from the payload,
    an on* handler, a javascript: URL. Parsed, not grepped, so escaped text never counts."""
    p = _Tags()
    p.feed(html_text or "")
    p.close()
    bad = []
    for tag, attrs in p.tags:
        if tag in ("script", "iframe", "svg", "object", "embed", "b"):
            bad.append(tag)
        if tag == "img" and attrs.get("src") == "x":
            bad.append("img src=x")
        for k, v in attrs.items():
            if k.startswith("on"):
                bad.append("%s %s=" % (tag, k))
            if k in ("href", "src") and (v or "").strip().lower().startswith("javascript:"):
                bad.append("%s %s=javascript:" % (tag, k))
    return bad


PAYLOAD = (
    "<script>fetch('/api/triage/threads/create',{method:'POST'})</script>\n\n"
    "<img src=x onerror=\"fetch('/api/triage/threads/create',{method:'POST',body:'{}'})\">\n\n"
    "<iframe src=\"https://evil.example/\"></iframe>\n\n"
    "Inline <b onmouseover=\"alert(1)\">bold</b>, <svg onload=alert(1)></svg> and [a link](javascript:alert(1)).\n"
)


def seed():
    # 1. hostile pages: a note and today's journal page
    write(WIKI, "Hostile/Payload.md",
          "# Payload <img src=x onerror=alert(1)>\n\n" + PAYLOAD + "\nSee [[Docs/Real]].\n\n- [ ] a task line\n")
    write(WIKI, "Journal/Day/%s.md" % TODAY, "# %s\n\n%s\n- [ ] open task\n" % (TODAY, PAYLOAD))
    write(WIKI, "Docs/Real.md", "# Real\n\nplain page.\n")
    # 2. links out of the root
    write(OUTSIDE, "secret.md", "# Stolen\n\n%s\n" % SECRET)
    write(OUTSIDE, "dir/Inner.md", "# Inner\n\n%s\n" % SECRET)
    write(OUTSIDE, "dir/pic.png", PNG + SECRET.encode())
    write(OUTSIDE, "loose.txt", SECRET)
    link(os.path.join(OUTSIDE, "secret.md"), WIKI, "Leak.md")              # a note out of the root
    link(os.path.join(OUTSIDE, "dir"), WIKI, "Linked")                      # a folder out of the root
    link(os.path.join(OUTSIDE, "loose.txt"), WIKI, "Docs/loose.txt")        # a file out of the root
    if os.path.exists("/proc/self/environ"):
        link("/proc/self/environ", WIKI, "Env.md")                          # the reviewer's plant
    link(os.path.join(WIKI, "Docs", "Real.md"), WIKI, "Docs/Alias.md")     # inside the root: fine
    # 3. hidden files and folders, and visible names that link onto them
    write(WIKI, ".chrome-data/Default/Cookies", b"SQLite format 3\x00" + SECRET.encode())
    write(WIKI, ".chrome-data/Default/Preferences", '{"session": "%s"}' % SECRET)
    write(WIKI, ".chrome-data/notes.md", "# in a dot folder\n\n%s\n" % SECRET)
    write(WIKI, ".wiki-engine.auth.json", '{"token": "%s"}' % SECRET)
    write(WIKI, "Personal/.cache/cache.json", '{"k": "%s"}' % SECRET)
    write(WIKI, "Personal/.hidden-note.md", "# hidden\n\n%s\n" % SECRET)
    write(WIKI, "Personal/Visible.md", "# Visible\n\nfine.\n")
    link(os.path.join(WIKI, ".wiki-engine.auth.json"), WIKI, "Docs/auth.json")
    link(os.path.join(WIKI, ".chrome-data", "notes.md"), WIKI, "Docs/Chrome.md")
    link(os.path.join(WIKI, ".chrome-data"), WIKI, "Docs/chrome")
    # 4. attachments of every type
    for name in ACTIVE:
        write(WIKI, "Files/" + name, "<script>document.title='active'</script>")
    for name in IMAGES:
        write(WIKI, "Files/" + name, PNG)
    write(WIKI, "Files/notes.txt", "plain text\n")
    link(os.path.join(WIKI, "Files", "page.html"), WIKI, "Files/innocent.png")


def rels(con):
    return ({r["rel"] for r in con.execute("SELECT rel FROM notes")},
            {r["rel"] for r in con.execute("SELECT rel FROM files")})


def run() -> int:
    seed()
    api = TestClient(Starlette(routes=notebook_api.routes))
    web.UPSTREAM_TRANSPORT = httpx.ASGITransport(app=Starlette(routes=notebook_api.routes))
    wc = TestClient(web.app, client=("127.0.0.1", 40000))

    check("the notebook is the wiki shape, folder-readonly", config.read_only() and config.profile().name == "wiki")
    notes.full_index()
    con = notes.db()
    note_rels, file_rels = rels(con)

    # --- 1. raw HTML ---------------------------------------------------------------------------------
    print("\n1. raw HTML in a wiki page renders as text at a folder-readonly site")
    r = api.get("/api/notes/note", params={"rel": "Hostile/Payload.md"})
    h = r.json().get("html", "")
    check("note view: no live <script>, <img onerror>, <iframe>, <svg onload>, on* handler or javascript: link",
          r.status_code == 200 and live(h) == [], live(h) or r.text[:200])
    check("note view: the payload is shown as escaped text",
          "&lt;script&gt;" in h and "&lt;iframe" in h and "&lt;img src=x onerror=" in h and "&lt;b onmouseover=" in h, h[:600])
    check("note view: a raw tag in the heading is text too",
          "<h1>Payload &lt;img src=x onerror=alert(1)&gt;</h1>" in h, h[:200])
    check("note view: the notebook's own markup still renders (wikilink, checkbox)",
          '<a class="wl" href="#/note/Docs%2FReal.md"' in h and '<input type="checkbox" class="task-box"' in h, h[-400:])
    r = api.get("/api/day", params={"date": TODAY})
    d = r.json().get("html", "")
    check("Day view: today's journal page renders with no live tag or handler",
          r.status_code == 200 and r.json()["exists"] and live(d) == [] and "&lt;script&gt;" in d, live(d) or d[:300])
    check("Day view: its checkbox still renders", '<input type="checkbox" class="task-box"' in d, d[-300:])
    r = api.get("/api/topic", params={"rel": "Hostile/Payload.md"})
    t = r.json()
    check("Topic hub: carries no rendered HTML (the page draws its fields through esc())",
          r.status_code == 200 and "html" not in t and t["title"] == "Payload <img src=x onerror=alert(1)>", t)
    r = wc.get("/api/notes/note?rel=" + quote("Hostile/Payload.md", safe=""), headers=AUTH)
    check("through the web role: the note the browser receives is the escaped one",
          r.status_code == 200 and live(r.json()["html"]) == [] and "&lt;script&gt;" in r.json()["html"], r.text[:200])
    check("render(): raw HTML escaped when read-only", render.render("<b>x</b>") == "<p>&lt;b&gt;x&lt;/b&gt;</p>\n",
          render.render("<b>x</b>"))

    # --- 2. links out of the root ----------------------------------------------------------------------
    print("\n2. a note or file whose real path leaves the root is neither indexed nor served")
    check("index: the symlinked note, the symlinked folder's note and /proc/self/environ are not notes",
          not ({"Leak.md", "Env.md", "Linked/Inner.md"} & note_rels), note_rels)
    check("index: the symlinked file and the symlinked folder's file are not attachments",
          not ({"Docs/loose.txt", "Linked/pic.png"} & file_rels), file_rels)
    check("index: a symlink that stays inside the root is not indexed either (W10: no link is followed)", "Docs/Alias.md" not in note_rels, note_rels)
    n = con.execute("SELECT count(*) c FROM notes WHERE text LIKE ? OR text LIKE ?",
                    ("%" + SECRET + "%", "%PATH=%")).fetchone()["c"]
    check("index: no note text holds the outside file or the process environment", n == 0, n)
    check("import_path refuses a symlinked note, a note in a symlinked folder and /proc/self/environ",
          notes.import_path(con, os.path.join(WIKI, "Leak.md")) is None
          and notes.import_path(con, os.path.join(WIKI, "Linked", "Inner.md")) is None
          and notes.import_path(con, os.path.join(WIKI, "Linked", "pic.png")) is None
          and (not os.path.lexists(os.path.join(WIKI, "Env.md"))
               or notes.import_path(con, os.path.join(WIKI, "Env.md")) is None))
    check("confined_path refuses a symlinked folder that leaves the root",
          notes.confined_path("Linked/Inner.md") is None and notes.confined_path("Linked/pic.png") is None)
    for rel in ("Leak.md", "Env.md", "Linked/Inner.md"):
        r = api.get("/api/notes/note", params={"rel": rel})
        check("GET /api/notes/note %s -> 404, nothing of the outside file" % rel,
              r.status_code == 404 and SECRET not in r.text and "PATH=" not in r.text, (r.status_code, r.text[:200]))
        r = api.get("/api/notes/raw", params={"rel": rel})
        check("GET /api/notes/raw %s -> 404, nothing of the outside file" % rel,
              r.status_code == 404 and SECRET not in r.text and "PATH=" not in r.text, (r.status_code, r.text[:200]))
        r = api.get("/api/topic", params={"rel": rel})
        check("GET /api/topic %s -> 404" % rel, r.status_code == 404, r.status_code)
    for rel in ("Docs/loose.txt", "Linked/pic.png"):
        r = api.get(att(rel))
        check("GET /att %s is refused, nothing of the outside file" % rel,
              r.status_code in (403, 404) and SECRET.encode() not in r.content, r.status_code)
    r = api.get("/api/search", params={"q": SECRET})
    check("search finds nothing from outside the root", r.status_code == 200 and r.json()["notes"] == [], r.json())
    r = api.get("/api/notes/folders")
    check("a symlinked folder that leaves the root is not listed as a folder",
          r.status_code == 200 and "Linked" not in [f["folder"] for f in r.json()["folders"]], r.json().get("folders"))
    r = wc.get("/api/notes/raw?rel=Leak.md", headers=AUTH)
    check("through the web role: raw of the symlinked note is 404 and carries no outside byte",
          r.status_code == 404 and SECRET not in r.text, (r.status_code, r.text[:200]))
    # an index written before the rule (the old importer took the link) loses the rows on the next pass
    notes.upsert_note(con, "Leak.md", "# Stolen\n\n%s\n" % SECRET, time.time(), 40)
    notes.upsert_file(con, "Docs/loose.txt", time.time(), 30)
    con.commit()
    notes.rebuild(con)
    note_rels, file_rels = rels(con)
    check("the next pass drops an old index's rows for links out of the root",
          "Leak.md" not in note_rels and "Docs/loose.txt" not in file_rels, (note_rels, file_rels))
    r = api.get("/api/notes/note", params={"rel": "Leak.md"})
    check("and the note route no longer has it", r.status_code == 404 and SECRET not in r.text, r.status_code)
    # folder mode (the reference's phase 1): the editor reads the file itself, through the same confinement
    os.environ["HELM_NOTES_MODE"] = "folder"
    try:
        def read_status(rel):
            e = err(lambda: edit.read(con, rel))
            return e.status if e else 200
        got = {rel: read_status(rel) for rel in ("Leak.md", "Linked/Inner.md", ".wiki-engine.auth.json", "Docs/auth.json")}
        check("folder mode: the editor's read of a link out, a hidden file or a link onto one is 404, not the file",
              set(got.values()) == {404}, got)
        try:
            edit.read(con, "Docs/Alias.md")
            alias = 200
        except edit.EditError as e:
            alias = e.status
        check("folder mode: the editor reads a real note, and an inside link is 404 (W10: no link is followed)",
              edit.read(con, "Docs/Real.md")["text"].startswith("# Real") and alias == 404, alias)
    finally:
        os.environ["HELM_NOTES_MODE"] = "folder-readonly"

    # --- 3. hidden files and folders -------------------------------------------------------------------
    print("\n3. dot-prefixed files and folders are never indexed or served")
    dotted = sorted(r for r in note_rels | file_rels if any(p.startswith(".") for p in r.split("/")))
    check("index: nothing with a dot-prefixed part at any depth", dotted == [], dotted)
    check("index: visible names that link onto hidden files are not indexed either",
          "Docs/auth.json" not in file_rels and "Docs/Chrome.md" not in note_rels, (file_rels, note_rels))
    check("index: the visible page beside them is", "Personal/Visible.md" in note_rels, note_rels)
    check("import_path refuses a dot file, a note in a dot folder, and a visible link onto a hidden file",
          notes.import_path(con, os.path.join(WIKI, "Personal", ".hidden-note.md")) is None
          and notes.import_path(con, os.path.join(WIKI, ".wiki-engine.auth.json")) is None
          and notes.import_path(con, os.path.join(WIKI, ".chrome-data", "notes.md")) is None
          and notes.import_path(con, os.path.join(WIKI, "Docs", "auth.json")) is None
          and notes.import_path(con, os.path.join(WIKI, "Docs", "Chrome.md")) is None)
    hidden_files = (".wiki-engine.auth.json", ".chrome-data/Default/Cookies", ".chrome-data/Default/Preferences",
                    "Personal/.cache/cache.json", "Docs/auth.json", "Docs/chrome/Default/Cookies")
    for rel in hidden_files:
        r = api.get(att(rel))
        check("GET /att %s -> 403, not served" % rel, r.status_code == 403 and SECRET.encode() not in r.content,
              (r.status_code, r.content[:80]))
    for rel in (".wiki-engine.auth.json", ".chrome-data/notes.md", "Personal/.hidden-note.md", "Docs/Chrome.md"):
        r = api.get("/api/notes/raw", params={"rel": rel})
        check("GET /api/notes/raw %s -> 404, not the file" % rel, r.status_code == 404 and SECRET not in r.text,
              (r.status_code, r.text[:120]))
        r = api.get("/api/notes/note", params={"rel": rel})
        check("GET /api/notes/note %s -> 404" % rel, r.status_code == 404 and SECRET not in r.text, r.status_code)
    # an index written before the rule still lists hidden rows: the routes refuse them anyway
    notes.upsert_note(con, "Personal/.hidden-note.md", "# hidden\n\n%s\n" % SECRET, time.time(), 40)
    notes.upsert_file(con, ".wiki-engine.auth.json", time.time(), 30)
    notes.upsert_file(con, ".chrome-data/Default/Cookies", time.time(), 30)
    con.commit()
    for path in ("/api/notes/note", "/api/notes/raw", "/api/topic"):
        r = api.get(path, params={"rel": "Personal/.hidden-note.md"})
        check("old index row: GET %s of a hidden note -> 404" % path, r.status_code == 404 and SECRET not in r.text,
              (r.status_code, r.text[:120]))
    for rel in (".wiki-engine.auth.json", ".chrome-data/Default/Cookies"):
        r = api.get(att(rel))
        check("old index row: GET /att %s -> 403" % rel, r.status_code == 403 and SECRET.encode() not in r.content,
              r.status_code)
    notes.rebuild(con)
    note_rels, file_rels = rels(con)
    check("the next pass drops the old hidden rows",
          "Personal/.hidden-note.md" not in note_rels and not any(r.startswith(".") for r in file_rels),
          (note_rels, file_rels))
    r = wc.get(att(".wiki-engine.auth.json"), headers=AUTH)
    check("through the web role: /att of a hidden file is refused", r.status_code == 403 and SECRET.encode() not in r.content,
          r.status_code)

    # --- 4. /att headers -----------------------------------------------------------------------------------
    print("\n4. /att: active types download, images stay inline, every response is sandboxed and never sniffed")

    def att_ok(resp, attachment):
        hd = resp.headers
        cd = hd.get("content-disposition", "")
        sandboxed = hd.get("x-content-type-options") == "nosniff" and hd.get("content-security-policy") == "sandbox"
        if attachment:
            return (resp.status_code == 200 and sandboxed and cd.startswith("attachment")
                    and hd.get("content-type") == "application/octet-stream")
        return resp.status_code == 200 and sandboxed and not cd.startswith("attachment")

    for name in ("innocent.png",):   # a .png name linked onto page.html: W10 serves no link at all
        rel = "Files/" + name
        r = api.get(att(rel))
        check("home api: %s (a link onto an .html) is not served, nosniff, CSP sandbox" % name,
              r.status_code in (403, 404) and b"<script>" not in r.content
              and r.headers.get("x-content-type-options") == "nosniff", dict(r.headers))
        r = wc.get(att(rel), headers=AUTH)
        check("home web role: %s (a link onto an .html) is not served" % name,
              r.status_code in (403, 404) and b"<script>" not in r.content, dict(r.headers))
    for name in ACTIVE:
        rel = "Files/" + name
        r = api.get(att(rel))
        check("home api: %s downloads as octet-stream, nosniff, CSP sandbox" % name, att_ok(r, True), dict(r.headers))
        r = wc.get(att(rel), headers=AUTH)
        check("home web role: %s downloads as octet-stream, nosniff, CSP sandbox" % name,
              att_ok(r, True) and r.content.startswith(b"<script>"), dict(r.headers))
    for name in IMAGES:
        rel = "Files/" + name
        r = api.get(att(rel))
        check("home api: %s stays inline as an image, nosniff, CSP sandbox" % name,
              att_ok(r, False) and r.headers["content-type"].startswith("image/"), dict(r.headers))
        r = wc.get(att(rel), headers=AUTH)
        check("home web role: %s stays inline as an image, nosniff, CSP sandbox" % name,
              att_ok(r, False) and r.headers["content-type"].startswith("image/") and r.content == PNG, dict(r.headers))
    r = api.get(att("Files/notes.txt"))
    check("home api: a text file stays inline as text/plain, sandboxed",
          att_ok(r, False) and r.headers["content-type"].startswith("text/plain"), dict(r.headers))
    r = api.get(att("Files/nope.png"))
    check("home api: a refusal from /att is sandboxed and never sniffed too",
          r.status_code == 404 and r.headers.get("content-security-policy") == "sandbox"
          and r.headers.get("x-content-type-options") == "nosniff", dict(r.headers))
    r = wc.get(att("Files/nope.png"), headers=AUTH)
    check("home web role: the same refusal, sandboxed",
          r.status_code == 404 and r.headers.get("content-security-policy") == "sandbox", dict(r.headers))
    # --- 5. hard links, an older index's rows in the listings, a file that turned unreadable -------------
    print("\n5. a hard link is not a symlink: realpath cannot see it, so the link count must")
    os.link(os.path.join(WIKI, ".wiki-engine.auth.json"), os.path.join(WIKI, "Files", "authlink.json"))
    os.link(os.path.join(WIKI, "Personal", ".hidden-note.md"), os.path.join(WIKI, "HardHidden.md"))
    os.link(os.path.join(OUTSIDE, "loose.txt"), os.path.join(WIKI, "Files", "hard.txt"))    # the same tmp filesystem
    os.link(os.path.join(OUTSIDE, "secret.md"), os.path.join(WIKI, "HardOut.md"))
    notes.rebuild(con)
    note_rels, file_rels = rels(con)
    check("index: a hard link to a hidden file, or to a file outside the wiki, is not indexed",
          not ({"HardHidden.md", "HardOut.md"} & note_rels) and not ({"Files/authlink.json", "Files/hard.txt"} & file_rels),
          (note_rels, file_rels))
    check("confined_path refuses a regular file with more than one link",
          all(notes.confined_path(r) is None for r in ("Files/authlink.json", "HardHidden.md", "Files/hard.txt", "HardOut.md")))
    check("import_path refuses each of them too",
          all(notes.import_path(con, os.path.join(WIKI, r)) is None for r in ("Files/authlink.json", "HardHidden.md", "Files/hard.txt", "HardOut.md")))
    for rel in ("Files/authlink.json", "Files/hard.txt"):
        r = api.get(att(rel))
        check("GET /att %s (a hard link) is refused, nothing of the file" % rel, r.status_code == 403 and SECRET.encode() not in r.content, r.status_code)
        r = wc.get(att(rel), headers=AUTH)
        check("through the web role too: /att %s" % rel, r.status_code == 403 and SECRET.encode() not in r.content, r.status_code)
    for rel in ("HardHidden.md", "HardOut.md"):
        for path in ("/api/notes/note", "/api/notes/raw", "/api/topic"):
            r = api.get(path, params={"rel": rel})
            check("GET %s %s (a hard link) -> 404, nothing of the file" % (path, rel), r.status_code == 404 and SECRET not in r.text,
                  (r.status_code, r.text[:100]))
    # an older index's rows for a hidden or refused path never surface through search, the switcher, list or completion
    notes.upsert_note(con, "Personal/.hidden-note.md", "# hidden\n\n%s\n" % SECRET, time.time(), 40)
    notes.upsert_note(con, "HardHidden.md", "# hidden link\n\n%s\n" % SECRET, time.time(), 40)
    con.commit()
    r = api.get("/api/search", params={"q": SECRET})
    check("old index row: /api/search returns neither the hidden note nor the hard link", r.status_code == 200 and r.json()["notes"] == [], r.json())
    r = api.get("/api/switcher", params={"q": "hidden"})
    check("old index row: /api/switcher lists neither", r.status_code == 200
          and not any((n or {}).get("rel") in ("Personal/.hidden-note.md", "HardHidden.md") for n in r.json()["notes"]), r.json())
    r = api.get("/api/notes/list", params={"folder": "Personal"})
    check("old index row: /api/notes/list omits the hidden note but keeps the visible one",
          not any(n["rel"] == "Personal/.hidden-note.md" for n in r.json()["notes"]) and any(n["rel"] == "Personal/Visible.md" for n in r.json()["notes"]), r.json())
    r = api.get("/api/notes/complete", params={"q": "hidden", "kind": "note"})
    check("old index row: /api/notes/complete offers neither", r.status_code == 200
          and not any((i or {}).get("rel") in ("Personal/.hidden-note.md", "HardHidden.md") for i in r.json()["items"]), r.json())
    notes.rebuild(con)
    # a file that becomes unreadable without changing (same mtime and size) loses its row on the next pass
    write(WIKI, "Docs/Quiet.md", "# Quiet\n\nreadable when indexed.\n")
    notes.rebuild(con)
    check("a readable page is indexed", notes.exists(con, "Docs/Quiet.md"))
    quiet = os.path.realpath(os.path.join(WIKI, "Docs", "Quiet.md"))
    real_access = os.access
    os.access = lambda p, mode, *a, **kw: False if os.path.realpath(p) == quiet else real_access(p, mode, *a, **kw)  # root reads anything: stand in for chmod 000
    try:
        rb = notes.rebuild(con)
    finally:
        os.access = real_access
    check("a page that became unreadable without changing loses its row and counts as skipped",
          not notes.exists(con, "Docs/Quiet.md") and rb["skipped"] >= 1, (notes.exists(con, "Docs/Quiet.md"), rb))
    notes.rebuild(con)
    check("readable again, it comes back", notes.exists(con, "Docs/Quiet.md"))
    for junk in ("Files/authlink.json", "HardHidden.md", "Files/hard.txt", "HardOut.md"):
        os.unlink(os.path.join(WIKI, junk))
    con.close()

    # --- store mode -------------------------------------------------------------------------------------
    print("\nstore mode: /att sandboxed, the reference's rendering kept, the export never adopts through a link")
    os.environ["HELM_NOTES_PROFILE"] = "fixed"
    os.environ["HELM_NOTES_MODE"] = "store"
    config.reset()
    saved_vault, saved_db = notes.VAULT, notes.DB_PATH
    notes.VAULT = WORK
    notes.DB_PATH = os.path.join(TMP, "work-notes.db")
    try:
        write(WORK, "attachments/page.html", "<script>document.title='x'</script>")
        write(WORK, "attachments/pic.svg", "<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>")
        write(WORK, "attachments/pic.png", PNG)
        write(WORK, "Contacts/Real Stray.md", "# Real Stray\n\nwritten behind the store.\n")
        write(WORK, "Contacts/.hidden-stray.md", "# hidden\n\n%s\n" % SECRET)
        link(os.path.join(OUTSIDE, "secret.md"), WORK, "Contacts/Stray.md")
        wcon = notes.db()
        check("the store is the truth", notes.store_is_truth(wcon) and not config.read_only())
        notes.write_text(wcon, "Contacts/Victim.md", "# Victim\n\nthe store's text.\n", "test")
        export.write_note("Contacts/Victim.md", "# Victim\n\nthe store's text.\n")
        victim = os.path.join(WORK, "Contacts", "Victim.md")
        os.remove(victim)
        os.symlink(os.path.join(OUTSIDE, "secret.md"), victim)     # the note's file becomes a link out
        future = time.time() + 600
        os.utime(os.path.join(OUTSIDE, "secret.md"), (future, future))
        rep = export.rebuild(wcon)
        stored = [r["rel"] for r in wcon.execute("SELECT rel FROM notes WHERE text LIKE ?", ("%" + SECRET + "%",))]
        revs = wcon.execute("SELECT count(*) c FROM revisions WHERE text LIKE ?", ("%" + SECRET + "%",)).fetchone()["c"]
        check("export: a stray that is a link out of the folder is not adopted, and nothing outside enters the store",
              not notes.exists(wcon, "Contacts/Stray.md") and stored == [] and revs == 0, (stored, revs, rep))
        check("export: a hidden stray is neither adopted nor listed",
              not notes.exists(wcon, "Contacts/.hidden-stray.md")
              and "Contacts/.hidden-stray.md" not in export.verify(wcon)["strays"], rep)
        check("export: a real stray in a notebook folder is still adopted", "Contacts/Real Stray.md" in rep["adopted_new"], rep)
        check("export: a store note whose file became a link out is rewritten as a plain file, the outside file untouched",
              not os.path.islink(victim) and open(victim, encoding="utf-8").read() == "# Victim\n\nthe store's text.\n"
              and SECRET in open(os.path.join(OUTSIDE, "secret.md"), encoding="utf-8").read()
              and "Contacts/Victim.md" not in rep["adopted"], rep)
        check("export: disk_bytes never reads through a link out of the folder",
              export.disk_bytes("Contacts/Stray.md") is None)
        wapi = TestClient(Starlette(routes=notebook_api.routes))
        r = wapi.get(att("attachments/page.html"))
        check("store api: an .html attachment keeps its type (the reference's) but is sandboxed and never sniffed",
              r.status_code == 200 and r.headers["content-type"].startswith("text/html")
              and not r.headers.get("content-disposition", "").startswith("attachment")
              and r.headers.get("content-security-policy") == "sandbox" and r.headers.get("x-content-type-options") == "nosniff",
              dict(r.headers))
        r = wapi.get(att("attachments/pic.svg"))
        check("store api: an .svg attachment is sandboxed", r.status_code == 200
              and r.headers.get("content-security-policy") == "sandbox", dict(r.headers))
        r = wapi.get(att("attachments/pic.png"))
        check("store api: an image is inline and sandboxed", r.status_code == 200 and r.headers["content-type"] == "image/png"
              and r.headers.get("content-security-policy") == "sandbox" and r.headers.get("x-content-type-options") == "nosniff",
              dict(r.headers))
        r = wc.get(att("attachments/page.html"), headers=AUTH)
        check("store web role: the .html attachment reaches the browser with CSP sandbox and nosniff",
              r.status_code == 200 and r.headers.get("content-security-policy") == "sandbox"
              and r.headers.get("x-content-type-options") == "nosniff", dict(r.headers))
        check("store mode: raw HTML still renders as the reference app rendered it (docs/DECISIONS.md H1)",
              render.render("<b>kept</b>") == "<p><b>kept</b></p>\n", render.render("<b>kept</b>"))
        wcon.close()
    finally:
        notes.VAULT, notes.DB_PATH = saved_vault, saved_db
        os.environ["HELM_NOTES_PROFILE"] = "wiki"
        os.environ["HELM_NOTES_MODE"] = "folder-readonly"
        config.reset()

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
