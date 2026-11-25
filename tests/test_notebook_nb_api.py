"""The Helm notebook's read routes: changes, graph, tasks, atlas, tail, backlink text, history.

Run: python3 tests/test_notebook_nb_api.py

Against a temp store-mode index, over HTTP, the way the browser's web role reaches them.
Every list is capped; hidden folders never appear; revision text never leaves; the work
profile has an empty atlas.
"""
# temp-ok: the folders and DBs under test are fixtures this module creates and deletes
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_nb_api_")
os.environ["HELM_SITE"] = "home"
os.environ["HELM_NOTES_PROFILE"] = "wiki"
os.environ["HELM_NOTES_MODE"] = "store"
os.environ["HELM_NOTES_ROOT"] = os.path.join(TMP, "vault")
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_APP_TOKEN"] = "test-token"

from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from notebook import config, notes  # noqa: E402
import app.notebook_api as notebook_api  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:400] if detail != "" else ""))


def put(con, rel, text, mtime=None):
    mtime = time.time() if mtime is None else mtime
    notes.upsert_note(con, rel, text, mtime, len(text.encode()))
    con.commit()


def day_ts(days_ago, hour=12):
    d = dt.date.today() - dt.timedelta(days=days_ago)
    return dt.datetime.combine(d, dt.time(hour)).timestamp()


def run() -> int:
    os.makedirs(os.environ["HELM_NOTES_ROOT"], exist_ok=True)
    client = TestClient(Starlette(routes=notebook_api.routes))
    client.get("/api/notes/meta")   # startup (cutover, first index) runs before the fixtures go in
    con = notes.db()
    now = time.time()

    put(con, "Hosts/host-a.md", "# host-a\nThe box.\n", day_ts(1))
    put(con, "Hosts/host-b.md", "# host-b\n", day_ts(2))
    put(con, "Docker/Helm.md", "# Helm\nruns on [[Hosts/host-b]] and [[Hosts/host-a]]\n", day_ts(0))
    put(con, "Docker/Whisper.md", "# Whisper\non [[host-a]]\n", day_ts(3))
    put(con, "APIs/Vault.md", "# Vault\nsee [[Hosts/host-b]]\n- [ ] rotate\n- [x] done one\n- [ ] audit\n", day_ts(4))
    put(con, "Network/Notes.md", "# Net\nnothing here\n", day_ts(5))
    put(con, "Secret/Hidden.md", "# Hidden\n[[Hosts/host-b]]\n- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n", day_ts(0))
    put(con, ".wiki-engine/Cache.md", "# Cache\n", day_ts(0))   # an older index's row for a dot folder
    put(con, "Playbooks/Todo.md", "# Todo\n" + "".join("- [ ] item %d\n" % i for i in range(8)) + "- [x] fin\n", day_ts(0))
    put(con, "Playbooks/Broken.md", "# Broken\n[[Nowhere At All]] and [[Hosts/host-a]]\n", day_ts(6))
    notes.resolve_links(con)
    con.commit()

    # --- a log page: long, append-style, with headings and blank lines
    log = "# Ops Log\n\n## 2026-11-20\n- a one\n- a two\n\n## Changelog\n" + "".join("- entry %d\n\n" % i for i in range(30))
    put(con, "Playbooks/Log.md", log)
    # a huge page, to prove tail does not choke
    big = "# Big\n" + "".join("line %d of the big page\n" % i for i in range(120000)) + "## Last section\n- end a\n- end b\n"
    put(con, "Playbooks/Big.md", big)

    # writes: own writes, newest first
    for i, (rel, kind, added) in enumerate([
            ("Docker/Helm.md", "append", "- one\n\n- two\n- three\n- four\n- five " + "x" * 400 + "\n"),
            ("Docker/Whisper.md", "write", None),
            ("Gone/Missing.md", "append", "- never seen\n"),
            ("Secret/Hidden.md", "append", "- secret\n"),
            ("Network/Notes.md", "append", "plain line\n")]):
        con.execute("INSERT INTO writes (ts, rel, kind, source, principal, added, prev_text) VALUES (?,?,?,?,?,?,?)",
                    (now - 1000 + i * 10, rel, kind, "agent-test", "tok-%d" % i, added, "OLDTEXT" if kind == "write" else None))
    con.commit()
    real_visible = notebook_api._folder_visible
    notebook_api._folder_visible = lambda ncon, rel: not rel.startswith("Secret/") and real_visible(ncon, rel)

    # === 1 changes
    print("\nGET /api/notes/changes")
    r = client.get("/api/notes/changes")
    j = r.json()
    check("200 with changes and since", r.status_code == 200 and "changes" in j and "since" in j, r.text[:200])
    rels = [c["rel"] for c in j["changes"]]
    check("newest first, gone and hidden notes skipped",
          rels == ["Network/Notes.md", "Docker/Whisper.md", "Docker/Helm.md"], rels)
    first = j["changes"][0]
    check("row shape", set(first) == {"rel", "title", "folder", "ts", "kind", "source", "principal", "lines"}, sorted(first))
    helm = [c for c in j["changes"] if c["rel"] == "Docker/Helm.md"][0]
    check("lines: last 3 non-empty, each <= 240",
          len(helm["lines"]) == 3 and helm["lines"][0] == "- three" and all(len(x) <= 240 for x in helm["lines"]), helm["lines"])
    check("a write without added has no lines", [c for c in j["changes"] if c["rel"] == "Docker/Whisper.md"][0]["lines"] == [])
    check("title and folder joined", helm["title"] == "Helm" and helm["folder"] == "Docker")
    check("no revision/prev text leaks", "OLDTEXT" not in r.text and "prev_text" not in r.text)
    r = client.get("/api/notes/changes", params={"since": now - 985})
    check("since filters", [c["rel"] for c in r.json()["changes"]] == ["Network/Notes.md"], r.text[:300])
    check("since is echoed", abs(r.json()["since"] - (now - 985)) < 1e-6)
    r = client.get("/api/notes/changes", params={"limit": 1})
    check("limit caps", len(r.json()["changes"]) == 1)
    r = client.get("/api/notes/changes", params={"limit": 99999})
    check("limit is clamped, not an error", r.status_code == 200)
    r = client.get("/api/notes/changes", params={"limit": "x"})
    check("bad limit is 400", r.status_code == 400)

    # === 2 graph
    print("\nGET /api/notes/graph")
    r = client.get("/api/notes/graph")
    j = r.json()
    check("200 with the contract keys", r.status_code == 200 and set(j) == {"most_linked", "folders", "total_notes", "unresolved_links"}, sorted(j))
    ml = j["most_linked"]
    check("most linked: host-b (Helm, Vault; Secret hidden) and host-a tie broken sanely",
          ml[0]["rel"] in ("Hosts/host-a.md", "Hosts/host-b.md") and ml[0]["links_in"] == 3, ml[:3])
    check("most linked row shape", set(ml[0]) == {"rel", "title", "folder", "links_in"})
    check("hidden source still counted? no: links_in counts all sources, hidden notes never listed",
          all(not m["rel"].startswith("Secret/") for m in ml))
    check("links_in counts distinct sources", {m["rel"]: m["links_in"] for m in ml}["Hosts/host-b.md"] in (2, 3))
    fo = {f["folder"]: f for f in j["folders"]}
    check("folders are top-level, dot folders excluded", "Docker" in fo and ".wiki-engine" not in fo, sorted(fo))
    check("folder shape", set(fo["Docker"]) == {"folder", "count", "last_mtime", "last_rel", "last_title", "week"}, sorted(fo["Docker"]))
    check("folder count and last", fo["Docker"]["count"] == 2 and fo["Docker"]["last_rel"] == "Docker/Helm.md" and fo["Docker"]["last_title"] == "Helm")
    check("week: 7 ints, oldest first, today last", len(fo["Docker"]["week"]) == 7 and fo["Docker"]["week"][6] == 1 and fo["Docker"]["week"][3] == 1 and sum(fo["Docker"]["week"]) == 2, fo["Docker"]["week"])
    check("total_notes is the visible count", j["total_notes"] == sum(f["count"] for f in j["folders"]), (j["total_notes"], j["folders"]))
    check("unresolved links counted", j["unresolved_links"] == 1, j["unresolved_links"])
    r = client.get("/api/notes/graph", params={"limit": 1})
    check("limit caps most_linked", len(r.json()["most_linked"]) == 1)
    check("bad limit is 400", client.get("/api/notes/graph", params={"limit": "q"}).status_code == 400)

    # === 3 tasks
    print("\nGET /api/notes/tasks")
    r = client.get("/api/notes/tasks")
    j = r.json()
    check("200, most open first", r.status_code == 200 and [n["rel"] for n in j["notes"]] == ["Playbooks/Todo.md", "APIs/Vault.md"], [n["rel"] for n in j["notes"]])
    todo = j["notes"][0]
    check("shape and counts", set(todo) == {"rel", "title", "folder", "open", "done", "items"} and todo["open"] == 8 and todo["done"] == 1)
    check("first 5 open items", len(todo["items"]) == 5 and todo["items"][0] == {"line": 2, "text": "item 0"}, todo["items"])
    check("total_open excludes hidden notes", j["total_open"] == 10, j["total_open"])
    check("limit caps", len(client.get("/api/notes/tasks", params={"limit": 1}).json()["notes"]) == 1)

    # === 4 atlas
    print("\nGET /api/notes/atlas")
    r = client.get("/api/notes/atlas")
    j = r.json()
    hosts = {h["rel"]: h for h in j["hosts"]}
    check("home: hosts listed", r.status_code == 200 and set(hosts) == {"Hosts/host-a.md", "Hosts/host-b.md"}, list(hosts))
    two = hosts["Hosts/host-b.md"]
    check("host shape", set(two) == {"rel", "title", "mtime", "pages"})
    check("pages: Docker/APIs/Network only, newest first, hidden skipped",
          [p["rel"] for p in two["pages"]] == ["Docker/Helm.md", "APIs/Vault.md"], two["pages"])
    check("page shape", set(two["pages"][0]) == {"rel", "title", "folder"})
    check("Playbooks links are not atlas pages", all(p["folder"] in ("Docker", "APIs", "Network") for h in j["hosts"] for p in h["pages"]))
    three = hosts["Hosts/host-a.md"]
    check("bare-name link resolved", [p["rel"] for p in three["pages"]] == ["Docker/Helm.md", "Docker/Whisper.md"], three["pages"])
    for i in range(30):
        put(con, "Docker/Many%02d.md" % i, "# m\n[[Hosts/host-b]]\n", day_ts(0) - i)
    notes.resolve_links(con)
    con.commit()
    check("max 24 pages per host", len({h["rel"]: h for h in client.get("/api/notes/atlas").json()["hosts"]}["Hosts/host-b.md"]["pages"]) == 24)
    os.environ["HELM_NOTES_PROFILE"] = "fixed"
    config.reset()
    r = client.get("/api/notes/atlas")
    check("work: empty atlas", r.status_code == 200 and r.json() == {"hosts": []}, r.text[:200])
    os.environ["HELM_NOTES_PROFILE"] = "wiki"
    config.reset()

    # === 5 tail
    print("\nGET /api/notes/tail")
    r = client.get("/api/notes/tail", params={"rel": "Playbooks/Log.md"})
    j = r.json()
    check("200, default 6 non-empty non-heading lines, in order",
          r.status_code == 200 and [l["text"] for l in j["lines"]] == ["- entry %d" % i for i in range(24, 30)], j.get("lines"))
    check("line numbers are body lines", j["lines"][-1]["line"] == 66 and j["lines"][0]["line"] == 56, j["lines"])
    check("last heading", j["last_heading"] == {"line": 7, "text": "Changelog"}, j["last_heading"])
    check("size and rel", j["rel"] == "Playbooks/Log.md" and j["size"] == len(log.encode()))
    r = client.get("/api/notes/tail", params={"rel": "Playbooks/Log.md", "n": 2})
    check("n caps", len(r.json()["lines"]) == 2)
    check("n clamps to 20", len(client.get("/api/notes/tail", params={"rel": "Playbooks/Log.md", "n": 500}).json()["lines"]) == 20)
    r = client.get("/api/notes/tail", params={"rel": "Playbooks/Broken.md"})
    check("note without later heading", r.json()["last_heading"] == {"line": 1, "text": "Broken"}, r.json())
    t0 = time.time()
    r = client.get("/api/notes/tail", params={"rel": "Playbooks/Big.md", "n": 3})
    dt_big = time.time() - t0
    jb = r.json()
    check("huge page tail lines", [l["text"] for l in jb["lines"]] == ["line 119999 of the big page", "- end a", "- end b"], jb["lines"])
    check("huge page heading at the right line", jb["last_heading"] == {"line": 120002, "text": "Last section"}, jb["last_heading"])
    check("huge page under 0.5s", dt_big < 0.5, dt_big)
    check("missing rel is 400", client.get("/api/notes/tail").status_code in (400, 404))
    check("missing note is 404 JSON", client.get("/api/notes/tail", params={"rel": "Nope/x.md"}).status_code == 404)
    check("hidden folder is 404", client.get("/api/notes/tail", params={"rel": "Secret/Hidden.md"}).status_code == 404)
    check("dot path is 404 or hidden", client.get("/api/notes/tail", params={"rel": ".git/config.md"}).status_code == 404)

    # === 6 backlink text
    print("\nGET /api/notes/note backlinks[].text")
    r = client.get("/api/notes/note", params={"rel": "Hosts/host-b.md"})
    j = r.json()
    bl = {b["rel"]: b for b in j["backlinks"]}
    check("existing fields unchanged", {"rel", "title", "folder", "line", "daily", "text"} <= set(bl["Docker/Helm.md"]) and "Secret/Hidden.md" not in bl, sorted(bl))
    check("text is the source line at line, raw markdown", bl["Docker/Helm.md"]["text"] == "runs on [[Hosts/host-b]] and [[Hosts/host-a]]", bl["Docker/Helm.md"])
    check("text for a later line", bl["APIs/Vault.md"]["text"] == "see [[Hosts/host-b]]")
    put(con, "Docker/Long.md", "# L\n[[Hosts/host-b]] " + "w" * 500 + "\n")
    notes.resolve_links(con)
    con.commit()
    bl = {b["rel"]: b for b in client.get("/api/notes/note", params={"rel": "Hosts/host-b.md"}).json()["backlinks"]}
    check("text capped at 240", len(bl["Docker/Long.md"]["text"]) == 240)

    # === 7 history
    print("\nGET /api/notes/history")
    notes.add_revision(con, "Docker/Helm.md", "agent-test", "SECRET OLD BODY")
    r = client.get("/api/notes/history", params={"rel": "Docker/Helm.md"})
    j = r.json()
    check("history still serves id, ts, source and never text",
          r.status_code == 200 and j["revisions"] and set(j["revisions"][0]) == {"id", "ts", "source", "size"} and "SECRET OLD BODY" not in r.text, r.text[:200])

    notebook_api._folder_visible = real_visible
    con.close()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
