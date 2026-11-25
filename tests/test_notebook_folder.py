"""Folder-readonly mode (home): the wiki folder indexed read-only.

Covers: the full index and its counts; the wiki engine wikilinks in both forms
([[Path/Page]] and [[Path/Page|alias]]), a bare [[Page]] by stem and the
@pos suffix; backlinks; FTS search; every write route answering 405 with a
JSON reason (enumerated from notebook_api.routes) while the inbox writes stay
open; attachment path confinement (`..`, absolute, a symlink out of the root, which is
not indexed either);
the 60 s mtime rescan (on demand and from the background thread); the timed
index of a generated 900-page / 13 MB folder with ~1,600 attachments (must be
under 60 s); and the home Day view (thread timeline + pages changed that day)
with the card <-> note join through `note:` links.
"""
# temp-ok: the folders and DBs under test are fixtures this module creates and deletes
from __future__ import annotations

import datetime as dt
import os
import random
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_folder_")
WIKI = os.path.join(TMP, "wiki")
BIG = os.path.join(TMP, "big")
OUTSIDE = os.path.join(TMP, "outside")
os.environ["HELM_SITE"] = "home"
os.environ["HELM_NOTES_PROFILE"] = "wiki"
os.environ["HELM_NOTES_MODE"] = "folder-readonly"
os.environ["HELM_NOTES_ROOT"] = WIKI
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_APP_TOKEN"] = "test-token"  # force: a persisted real token must never leak into tests

from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from helm import store  # noqa: E402
from notebook import config, notes  # noqa: E402
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


def write(root, rel, text, mtime=None):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(text, bytes):
        with open(p, "wb") as fh:
            fh.write(text)
    else:
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def seed_wiki():
    write(WIKI, "index.md", "# Home\n\nStart at [[Docker/helm-gateway]] and the [[Changelog]].\n")
    write(WIKI, "Changelog.md", "# Changelog\n\n- 2026-09-22 10:00 ET - the bridge - [[Docker/helm-gateway]]: rebuilt\n")
    write(WIKI, "Docker/helm-gateway.md",
          "# helm-gateway\n\nSee [[Playbooks/Talking To the owner|the playbook]] and [[Projects/Helm Web UI]].\n\n"
          "![](attachments/diagram.png)\n\n![[diagram.png]]\n\n- [ ] rotate the token\n- [x] moved to compose\n")
    write(WIKI, "Playbooks/Talking To the owner.md", "# Talking To the owner\n\nAsk one question at a time.\n")
    write(WIKI, "Projects/Helm Web UI.md",
          "# Helm Web UI\n\nThe screens for [[Docker/helm-gateway|the gateway]], also [[helm-gateway]] "
          "by stem, [[Nope/Missing]] which does not exist, and [[Docker/helm-gateway@120]] at a position.\n")
    write(WIKI, "Journal/Day/%s.md" % TODAY.isoformat(),
          "# %s\n\n- [ ] open task\n\nWorked on [[Docker/helm-gateway]].\n" % TODAY.isoformat())
    write(WIKI, "Docker/attachments/diagram.png", b"\x89PNG\r\n\x1a\n" + b"\1" * 40)
    write(WIKI, "attachments/logo.png", b"\x89PNG\r\n\x1a\n" + b"\2" * 40)
    write(WIKI, "_plug/thing.md", "# a plug, skipped\n")
    write(WIKI, ".wiki-engine/state.md", "# state, skipped\n")
    write(OUTSIDE, "secret.png", b"\x89PNG outside the root")
    os.symlink(os.path.join(OUTSIDE, "secret.png"), os.path.join(WIKI, "Docker", "escape.png"))
    os.symlink(os.path.join(WIKI, "attachments", "logo.png"), os.path.join(WIKI, "Docker", "inside-link.png"))


WORDS = ("gateway compose caddy authentik token wiki wiki-engine backup restic nas monitor error-tracker "
         "helm pump parks dispatcher heartbeat digest sweep recheck lesson gate stall "
         "docker container image port firewall runbook playbook changelog ops log").split()


def seed_big(pages=900, files=1600, target_bytes=13 * 1024 * 1024):
    """A synthetic wiki the size of the real one: 900 pages of prose with
    wikilinks in both forms, ~13 MB of markdown, ~1,600 attachment files."""
    rnd = random.Random(7)
    folders = ["Docker", "Playbooks", "Projects", "APIs", "Hosts", "Ops", "Journal/Day", "Reference",
               "Runbooks", "Agents", "Fleet", "Notes"]
    per_page = target_bytes // pages
    names = []
    for i in range(pages):
        folder = folders[i % len(folders)]
        name = "%s/Page %04d" % (folder, i)
        names.append(name)
    for i, name in enumerate(names):
        parts = ["---\ntype: page\nupdated: 2026-09-%02d\n---\n# %s\n\n" % (1 + i % 28, name.split("/")[-1])]
        size = len(parts[0])
        while size < per_page:
            k = rnd.randrange(4)
            if k == 0 and i:
                j = rnd.randrange(i)
                link = "[[%s]]" % names[j] if rnd.random() < 0.5 else "[[%s|%s]]" % (names[j], "see that page")
                line = "See %s for more. " % link + " ".join(rnd.choice(WORDS) for _ in range(20)) + "\n\n"
            elif k == 1:
                line = "- [%s] %s\n" % (" " if rnd.random() < 0.5 else "x", " ".join(rnd.choice(WORDS) for _ in range(8)))
            else:
                line = " ".join(rnd.choice(WORDS) for _ in range(40)) + ".\n\n"
            parts.append(line)
            size += len(line)
        write(BIG, name + ".md", "".join(parts))
    for i in range(files):
        folder = folders[i % len(folders)]
        write(BIG, "%s/attachments/file-%04d.png" % (folder, i), b"\x89PNG" + bytes([i % 251]) * 64)
    total = 0
    for dirpath, _, fns in os.walk(BIG):
        for fn in fns:
            if fn.endswith(".md"):
                total += os.path.getsize(os.path.join(dirpath, fn))
    return total


def run() -> int:
    seed_wiki()

    # --- the timed index (first, before any background thread exists) --------------------------
    md_bytes = seed_big()
    check("synthetic folder is the wiki's size (900 pages, ~13 MB, 1600 files)",
          md_bytes >= 12 * 1024 * 1024, md_bytes)
    saved = notes.VAULT
    notes.VAULT = BIG
    try:
        bcon = notes.db(os.path.join(TMP, "big.db"))
        t0 = time.time()
        rb = notes.rebuild(bcon, force=True)
        secs = time.time() - t0
        print("  timed index: %d notes, %d files, %.2f s (limit 60 s)" % (rb["notes"], rb["files"], secs))
        check("900 pages and 1600 attachments index in under 60 s", secs < 60 and rb["notes"] == 900 and rb["files"] == 1600,
              (secs, rb))
        st = notes.stats(bcon)
        check("the big index resolved its links", st["links"] > 1000 and st["links_resolved"] == st["links"], st)
        check("FTS answers over the big index", len(notes.search(bcon, "authentik")) > 0)
        t1 = time.time()
        rb2 = notes.rebuild(bcon)
        check("an idle rescan of the big folder imports nothing and is quick",
              rb2["imported"] == 0 and time.time() - t1 < 10, (rb2, time.time() - t1))
        bcon.close()
    finally:
        notes.VAULT = saved
    shutil.rmtree(BIG, ignore_errors=True)

    # --- the index ---------------------------------------------------------------------------
    check("the profile is the wiki one and the mode is folder-readonly",
          config.profile().name == "wiki" and config.mode() == "folder-readonly" and config.read_only())
    rb = notes.full_index()
    check("full_index indexes the wiki: 6 pages, 2 files (no link is followed, inside or out: W10), plugs and dot-folders skipped",
          rb["notes"] == 6 and rb["files"] == 2 and rb["skipped"] == 2, rb)
    con0 = notes.db()
    check("the symlink out of the root is not in the index at all",
          con0.execute("SELECT 1 FROM files WHERE rel='Docker/escape.png'").fetchone() is None)
    con0.close()
    check("LAST_INDEX records the pass", notes.LAST_INDEX and notes.LAST_INDEX["notes"] == 6)
    con = notes.db()
    check("truth is the folder, read-only", notes.truth(con) == "folder-readonly" and not notes.store_is_truth(con))
    fo = {f["folder"]: f["count"] for f in notes.folders(con)}
    # attachments/ holds files but no notes: since the notebook organizer (PR #28) a home folder with
    # no notes is not listed; it is still a folder a note may be filed under.
    check("folders are the wiki's top-level folders with notes, plus the root as ''",
          fo == {"Docker": 1, "Journal": 1, "Playbooks": 1, "Projects": 1, "": 2}
          and "attachments" in notes.folders_allowed(), fo)
    check("the daily note lives at Journal/Day/YYYY-MM-DD.md at home",
          notes.daily_rel(TODAY) == "Journal/Day/%s.md" % TODAY.isoformat()
          and notes.is_daily("Journal/Day/%s.md" % TODAY.isoformat())
          and not notes.is_daily("Journal/Week/%s.md" % TODAY.isoformat()))

    # --- wikilinks ------------------------------------------------------------------------------
    links = {l["target"]: l["rel"] for l in notes.outlinks(con, "Projects/Helm Web UI.md")}
    check("[[Path/Page|alias]] resolves against the page path", links.get("Docker/helm-gateway") == "Docker/helm-gateway.md", links)
    check("[[Page]] resolves by stem", links.get("helm-gateway") == "Docker/helm-gateway.md", links)
    check("[[Path/Page@pos]] resolves with the wiki engine's position suffix dropped",
          links.get("Docker/helm-gateway@120") == "Docker/helm-gateway.md", links)
    check("a missing page stays unresolved", "Nope/Missing" in links and links["Nope/Missing"] is None, links)
    gw = {l["target"]: (l["rel"], l["alias"]) for l in notes.outlinks(con, "Docker/helm-gateway.md")}
    check("[[Path/Page]] resolves, and the alias is kept",
          gw.get("Playbooks/Talking To the owner") == ("Playbooks/Talking To the owner.md", "the playbook")
          and gw.get("Projects/Helm Web UI") == ("Projects/Helm Web UI.md", ""), gw)
    check("a root page resolves from [[Changelog]]",
          {l["target"]: l["rel"] for l in notes.outlinks(con, "index.md")}.get("Changelog") == "Changelog.md")
    bl = sorted(b["rel"] for b in notes.backlinks(con, "Docker/helm-gateway.md"))
    check("backlinks list every page that links here, once each",
          bl == ["Changelog.md", "Journal/Day/%s.md" % TODAY.isoformat(), "Projects/Helm Web UI.md", "index.md"], bl)
    check("FTS search finds a page by a body word", [h["rel"] for h in notes.search(con, "playbook")] != []
          and any(h["rel"] == "Playbooks/Talking To the owner.md" for h in notes.search(con, "question")))
    st = notes.stats(con)
    check("stats: links counted, all but the missing one resolved", st["links_resolved"] == st["links"] - 1, st)

    # --- path confinement, at the function -----------------------------------------------------------
    check("confined_path refuses empty, absolute and dotted paths",
          notes.confined_path("") is None and notes.confined_path("/etc/passwd") is None
          and notes.confined_path("Docker/../../x.png") is None and notes.confined_path("./x.png") is None)
    check("confined_path refuses a symlink that leaves the root", notes.confined_path("Docker/escape.png") is None)
    check("confined_path accepts a real file and refuses a link even when it stays inside (W10/W11)",
          notes.confined_path("attachments/logo.png") and notes.confined_path("Docker/inside-link.png") is None)

    # --- the rescan ---------------------------------------------------------------------------------
    write(WIKI, "Docker/new-page.md", "# New page\n\nzebrawood arrived.\n")
    check("a read before the rescan is due sees nothing new", notes.rescan_if_due(con) is None
          and notes.search(con, "zebrawood") == [])
    out = notes.rescan_if_due(con, force=True)
    check("the mtime rescan picks up a new page", out and out["imported"] == 1
          and [h["rel"] for h in notes.search(con, "zebrawood")] == ["Docker/new-page.md"], out)
    write(WIKI, "Docker/new-page.md", "# New page\n\nzebrawood left; quokka arrived.\n", mtime=time.time() + 5)
    notes.rescan_if_due(con, force=True)
    check("the rescan re-reads a changed page (mtime moved)", notes.search(con, "quokka") != [])
    os.remove(os.path.join(WIKI, "Docker", "new-page.md"))
    out = notes.rescan_if_due(con, force=True)
    check("the rescan drops a deleted page", not notes.exists(con, "Docker/new-page.md") and out["removed"] == 1, out)
    th = notes.start_rescan(every_s=0.3)
    check("start_rescan starts one daemon thread and is idempotent",
          th is not None and th.daemon and notes.start_rescan(every_s=0.3) is th)
    write(WIKI, "Docker/threaded.md", "# Threaded\n\nwombat spotted by the background pass.\n")
    seen = False
    for _ in range(50):
        time.sleep(0.2)
        if notes.search(con, "wombat"):
            seen = True
            break
    check("the background rescan indexes a new page within a few passes", seen)
    con.close()

    # --- HTTP: reads work, every notebook write is 405, inbox writes stay open ------------------------
    app = Starlette(routes=notebook_api.routes)
    client = TestClient(app)
    r = client.get("/api/notes/meta")
    check("GET /api/notes/meta reports the mode, profile, root and index",
          r.status_code == 200 and r.json()["mode"] == "folder-readonly" and r.json()["profile"] == "wiki"
          and r.json()["read_only"] is True and r.json()["root"] == WIKI and r.json()["notes"]["notes"] == 7
          and r.json()["last_index"]["notes"] == 7, r.json())
    r = client.get("/api/notes/folders")
    check("GET /api/notes/folders lists the wiki's folders and says read-only",
          r.status_code == 200 and any(f["folder"] == "Docker" for f in r.json()["folders"]) and r.json()["read_only"] is True
          and r.json()["today"]["exists"] is True, r.json())
    r = client.get("/api/notes/note", params={"rel": "Docker/helm-gateway.md"})
    j = r.json()
    check("GET /api/notes/note renders wikilinks as in-app links with the alias as the text",
          r.status_code == 200 and 'href="#/note/Playbooks%2FTalking%20To%20the%20owner.md"' in j["html"]
          and ">the playbook</a>" in j["html"], j.get("html", "")[:300])
    check("a markdown image and an Obsidian embed both resolve to /att/ by name",
          j["html"].count('src="/att/Docker%2Fattachments%2Fdiagram.png"') == 2, j["html"])
    check("the note says it is not editable and why, and carries its backlinks",
          j["editable"] is False and "read-only" in j["reason"] and len(j["backlinks"]) == 4 and j["topic"] is False, j)
    r = client.get("/api/notes/list", params={"folder": "Docker"})
    check("GET /api/notes/list lists a folder", r.status_code == 200 and [n["stem"] for n in r.json()["notes"]] == ["helm-gateway", "threaded"], r.json())
    r = client.get("/api/notes/raw", params={"rel": "Docker/helm-gateway.md"})
    check("GET /api/notes/raw returns the text, not editable", r.status_code == 200 and r.json()["text"].startswith("# helm-gateway")
          and r.json()["editable"] is False)
    r = client.get("/api/notes/templates")
    check("home has no templates", r.status_code == 200 and r.json() == {"templates": [], "by_folder": {}}, r.json())
    r = client.get("/api/notes/history", params={"rel": "Docker/helm-gateway.md"})
    check("history is empty and names the truth", r.status_code == 200 and r.json()["revisions"] == [] and r.json()["truth"] == "folder-readonly")
    r = client.get("/api/notes/complete", params={"q": "helm"})
    check("autocomplete answers", r.status_code == 200 and any(i["rel"] == "Docker/helm-gateway.md" for i in r.json()["items"]))
    r = client.get("/api/search", params={"q": "playbook"})
    check("GET /api/search answers notes and threads together", r.status_code == 200 and set(r.json()) == {"q", "notes", "threads"}
          and r.json()["notes"])
    r = client.get("/api/switcher", params={"q": "talk"})
    check("GET /api/switcher matches a note by stem", any(n["rel"] == "Playbooks/Talking To the owner.md" for n in r.json()["notes"]))

    writes = notebook_api.write_routes()
    want = {("/api/day/append", "POST"), ("/api/notes/note", "PUT"), ("/api/notes/new", "POST"),
            ("/api/notes/daily/new", "POST"), ("/api/notes/toggle", "POST"), ("/api/notes/rename", "POST"),
            ("/api/notes/attach", "POST"), ("/api/notes/append", "POST"), ("/api/notes/note", "DELETE"),
            ("/api/notes/move", "POST"), ("/api/notes/folder", "POST"), ("/api/notes/delete", "POST")}
    have = {(rt.path, m) for rt in writes for m in rt.methods if m in ("POST", "PUT", "DELETE")}
    check("write_routes enumerates every notebook write route", have == want, have ^ want)
    all_posts = {(rt.path, m) for rt in notebook_api.routes for m in rt.methods if m in ("POST", "PUT")}
    check("the only other POST routes are the inbox's", all_posts - have == {("/api/inbox/new", "POST"), ("/api/inbox/{thread_id}/{action}", "POST")},
          all_posts - have)
    bodies = {"/api/day/append": {"date": TODAY.isoformat(), "line": "x"},
              "/api/notes/note": {"rel": "Docker/helm-gateway.md", "text": "# x\n"},
              "/api/notes/new": {"folder": "Docker", "title": "New"}, "/api/notes/daily/new": {},
              "/api/notes/toggle": {"rel": "Docker/helm-gateway.md", "line": 5},
              "/api/notes/rename": {"rel": "Docker/helm-gateway.md", "new_stem": "x", "dry_run": True},
              "/api/notes/append": {"rel": "Docker/helm-gateway.md", "text": "- appended"},
              "/api/notes/move": {"rel": "Docker/helm-gateway.md", "new_rel": "Docker/moved.md"},
              "/api/notes/folder": {"name": "Archive"},
              "/api/notes/delete": {"rel": "Docker/helm-gateway.md", "reason": "stale", "source": "audit"}}
    for path, method in sorted(want):
        if path == "/api/notes/attach":
            r = client.post(path, content=b"\x89PNG", headers={"X-Filename": "a.png"})
        elif method == "DELETE":
            r = client.request("DELETE", path, params={"rel": "Docker/helm-gateway.md"})
        else:
            r = client.request(method, path, json=bodies[path])
        j = r.json()
        check("%s %s is 405 with a JSON reason" % (method, path),
              r.status_code == 405 and j["ok"] is False and "read-only" in j["detail"] and j["mode"] == "folder-readonly"
              and r.headers.get("allow") == "GET", (r.status_code, j))
    check("nothing was written by the refused calls",
          notes.get_text(notes.db(), "Docker/helm-gateway.md")[0].startswith("# helm-gateway")
          and "- appended" not in notes.get_text(notes.db(), "Docker/helm-gateway.md")[0]
          and os.path.exists(os.path.join(WIKI, "Docker", "helm-gateway.md"))
          and not os.path.exists(os.path.join(WIKI, "Docker", "New.md")) and not os.path.exists(os.path.join(WIKI, "attachments", "a.png"))
          and not os.path.exists(os.path.join(WIKI, "Docker", "moved.md")) and not os.path.exists(os.path.join(WIKI, "Archive")))
    r = client.post("/api/inbox/new", json={"title": "A card about the gateway page", "category": "Docker",
                                             "links": [{"url": "note:Docker/helm-gateway", "label": "the page"}]},
                    headers={"X-Helm-Actor": "owner"})
    check("POST /api/inbox/new stays open at a read-only site", r.status_code == 201 and r.json()["created"] is True, r.json())
    tid = r.json()["thread_id"]
    r = client.post("/api/inbox/new", json={"title": "A card about nothing in the wiki"})
    other = r.json()["thread_id"]
    r = client.get("/api/inbox")
    check("GET /api/inbox is the store's list", r.status_code == 200 and {t["thread_id"] for t in r.json()["threads"]} >= {tid, other})

    # --- attachments over HTTP ----------------------------------------------------------------------------
    r = client.get("/att/Docker%2Fattachments%2Fdiagram.png")
    check("an indexed attachment is served with its type", r.status_code == 200 and r.headers["content-type"].startswith("image/png")
          and r.content.startswith(b"\x89PNG"))
    r = client.get("/att/Docker%2Finside-link.png")
    check("a symlink that stays inside the root is not served either (W10: no link is followed)", r.status_code in (403, 404), r.status_code)
    r = client.get("/att/Docker%2Fescape.png")
    check("a symlink out of the root is refused (403), not served", r.status_code == 403 and r.json()["ok"] is False, r.status_code)
    r = client.get("/att/..%2F..%2Foutside%2Fsecret.png")
    check("a .. path is refused", r.status_code == 403, r.status_code)
    r = client.get("/att/../../outside/secret.png")
    check("a raw .. path is never served", r.status_code != 200 and b"outside the root" not in r.content, r.status_code)
    r = client.get("/att/%2Fetc%2Fpasswd")
    check("an absolute path is refused", r.status_code == 403, r.status_code)
    r = client.get("/att/Docker%2Fnope.png")
    check("an unknown file is 404", r.status_code == 404)
    r = client.get("/att/Docker%2Fhelm-gateway.md")
    check("a note is not an attachment", r.status_code == 404)
    r = client.get("/att/Docker%2Fattachments%2Fdiagram.png%00")
    check("a NUL byte in an attachment path is refused (403), never a 500", r.status_code == 403, r.status_code)

    # --- the index against a hostile folder: a symlinked page that leaves the root, an unreadable file, a
    # --- directory symlink, and the raw read staying inside the index in read-only mode -------------------
    write(OUTSIDE, "leak.md", "# outside\n\nnot part of the wiki\n")
    os.symlink(os.path.join(OUTSIDE, "leak.md"), os.path.join(WIKI, "Docker", "leak.md"))
    os.symlink(OUTSIDE, os.path.join(WIKI, "linkdir"))
    import socket as _socket
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.bind(os.path.join(WIKI, "Docker", "unreadable.md"))   # open() on a socket raises OSError (ENXIO)
    write(WIKI, "Docker/after-the-bad-one.md", "# after\n\nindexed in the same pass as the unreadable file\n")
    notes.rescan_if_due(force=True)
    con = notes.db()
    check("a .md symlink pointing out of the root is not indexed as a page",
          not notes.exists(con, "Docker/leak.md") and notes.LAST_INDEX["skipped"] >= 1, notes.LAST_INDEX)
    check("one unreadable file does not abort the pass: the page written in the same pass is indexed",
          notes.exists(con, "Docker/after-the-bad-one.md") and notes.exists(con, "Docker/helm-gateway.md"), notes.LAST_INDEX)
    r = client.get("/api/notes/folders")
    check("a directory symlink is not a folder (the index never descends it)",
          r.status_code == 200 and "linkdir" not in [f["folder"] for f in r.json()["folders"]], r.json())
    r = client.get("/api/notes/raw", params={"rel": "Docker/leak.md"})
    check("GET /api/notes/raw never reads through a symlink out of the root", r.status_code == 404, r.status_code)
    r = client.get("/api/notes/raw", params={"rel": "Docker/attachments/diagram.png"})
    check("GET /api/notes/raw serves only indexed pages in read-only mode, never an attachment's bytes",
          r.status_code == 404, r.status_code)
    r = client.get("/api/notes/raw", params={"rel": "Docker/after-the-bad-one.md"})
    check("GET /api/notes/raw still reads an indexed page", r.status_code == 200 and r.json()["text"].startswith("# after"), r.status_code)
    sock.close()
    for junk in ("Docker/unreadable.md", "Docker/leak.md", "linkdir"):
        os.unlink(os.path.join(WIKI, junk))
    con.close()

    # --- the home Day view and the card <-> note join ------------------------------------------------------
    yesterday = time.time() - 86400
    os.utime(os.path.join(WIKI, "Playbooks", "Talking To the owner.md"), (yesterday, yesterday))
    notes.rescan_if_due(force=True)
    r = client.get("/api/day", params={"date": TODAY.isoformat()})
    d = r.json()
    check("GET /api/day renders today's journal page, read-only", r.status_code == 200 and d["exists"] is True
          and d["editable"] is False and "open task" in d["html"] and d["open_tasks"] == 1, d.get("html"))
    filed = [e for e in d["timeline"] if e["kind"] == "filed"]
    check("the timeline holds the cards filed today, from the structured first event",
          {e["thread_id"] for e in filed} == {tid, other} and all(e["label"] == "you wrote it" for e in filed), filed)
    changed = [n["rel"] for n in d["changed"]]
    check("the Day view lists the pages changed that day, not yesterday's nor the day's own note",
          "Docker/helm-gateway.md" in changed and "Playbooks/Talking To the owner.md" not in changed
          and d["rel"] not in changed, changed)
    check("no calls at home, and the day's linked pages are listed",
          d["calls"] == [] and d["mentions"] == [{"rel": "Docker/helm-gateway.md", "title": "helm-gateway"}], d["mentions"])
    r = client.post("/api/inbox/%s/reply" % tid, json={"value": "done"}, headers={"X-Helm-Actor": "owner"})
    check("a reply through the notebook's inbox route is accepted", r.status_code == 202 and r.json()["accepted"] is True, r.json())
    r = client.get("/api/thread/%s" % tid)
    src = [e["source"] for e in r.json()["events"] if e["kind"] == "owner-reply"]
    check("the reply through the notebook is the owner's own tap (a login is the owner; DECISIONS #12)", src == ["owner"], src)
    r = client.get("/api/day", params={"date": TODAY.isoformat()})
    web_rows = [e for e in r.json()["timeline"] if e["thread_id"] == tid and e["kind"] == "settled"]
    check("the Day view reads the login's settle as yours, from the source field",
          web_rows and web_rows[0]["label"] == "you settled it" and web_rows[0]["actor"] == "you", web_rows)
    r = client.get("/api/topic", params={"rel": "Docker/helm-gateway.md"})
    t = r.json()
    check("the Topic hub lists the thread that cites the note by note: link, as linked, nothing else",
          r.status_code == 200 and [x["thread_id"] for x in t["settled"]] == [tid] and t["settled"][0]["match"] == "linked"
          and t["open"] == [] and t["topic"] is False and len(t["backlinks"]) == 4, t)
    r = client.get("/api/thread/%s" % tid)
    check("GET /api/thread/{id} carries the reference's suppressed flag", r.status_code == 200 and r.json()["suppressed"] is False, r.json().keys())
    write(WIKI, "Projects/Caf\u00e9 plan.md", "# Caf\u00e9 plan\n\nA page whose name the store's JSON escapes.\n")
    notes.rescan_if_due(force=True)
    r = client.post("/api/inbox/new", json={"title": "A card about the caf\u00e9 page", "category": "Projects",
                                             "links": [{"url": "note:Projects/Caf\u00e9 plan", "label": "the page"}]},
                    headers={"X-Helm-Actor": "owner"})
    cafe = r.json()["thread_id"]
    r = client.get("/api/topic", params={"rel": "Projects/Caf\u00e9 plan.md"})
    check("the Topic hub lists a thread citing a non-ASCII page name (the store escapes it in JSON)",
          r.status_code == 200 and [x["thread_id"] for x in r.json()["open"]] == [cafe] and r.json()["open"][0]["match"] == "linked", r.json())
    r = client.get("/api/topic", params={"rel": "Playbooks/Talking To the owner.md"})
    check("a note nothing cites has no threads and no work heuristics",
          r.json()["open"] == [] and r.json()["settled"] == [] and r.json()["keys"]["name"] == "" and r.json()["keys"]["cases"] == [])
    r = client.get("/api/day", params={"date": "not-a-date"})
    check("a bad date is a 400 with the error shape", r.status_code == 400 and r.json() == {"ok": False, "detail": "date must be YYYY-MM-DD"})
    r = client.get("/api/notes/note", params={"rel": "Nope/x.md"})
    check("a missing note is a 404 with the error shape", r.status_code == 404 and r.json()["ok"] is False)

    # The rescan thread is stopped and joined before its folder goes and the interpreter exits: a
    # daemon thread still printing during finalization aborts Python (rc=-6) after every check passed.
    stopped = hasattr(notes, "stop_rescan") and notes.stop_rescan()
    check("stop_rescan stops and joins the rescan thread before the folder is removed",
          stopped and not th.is_alive() and not any(t.name == "helm-notes-rescan" for t in threading.enumerate()),
          [t.name for t in threading.enumerate()])
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
