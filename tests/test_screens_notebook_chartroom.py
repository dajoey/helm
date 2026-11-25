"""Playwright drive: the notebook in the Chart Room design (home, note, topic, day, folder, the Connected pane).

Run: python3 tests/test_screens_notebook_chartroom.py

Synthetic API payloads (the shapes of app/notebook_api.py's changes / graph / tasks / atlas / tail / history routes) exercise
the real notebook module and the shipped static assets:

  1  the home asks exactly one batched read per feed, in parallel, and no per-note / per-folder read; the atlas is a home read
  2  Changed today groups the Helm writes by page with who wrote what; an edit outside Helm comes from the recent list;
     a site without the feed (404) says nothing, a failing one says so
  3  the shelves: a spine per folder from the graph, taller for more pages, Archive dashed, the folder list when there is no graph
  4  the fleet atlas: a chart of hosts (the busiest few, then all), a list on a phone, none on a second site
  5  in play: the notes live threads link to, with chips of what is open, and the same count in the rail
  6  today's page: journal lines, tick a box, the append box; read-only keeps all three out; the search field opens a finder
  7  a note: outline, at-a-glance line, pills, chips for wikilinks, latest entries only for an append-style page
  8  the Connected pane: waiting on you, threads, linked from with the sentence and the matched title marked, links out, days
  10 topic, day (a log, newest first, a Now line), folder (sort, sub-shelves, dailies by month)
  11 a phone: no sideways scroll on any page, 44px targets on the home, the Connected content inline in the note
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time

os.environ["TZ"] = "America/New_York"   # the browser context runs in this zone; local midnight must agree in any container
time.tzset()
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "screens"))
sys.path.insert(0, HERE)

if os.environ.get("HELM_SKIP_DRIVES") == "1":
    print("  FAIL the drives were skipped (HELM_SKIP_DRIVES=1): this suite never passes quietly")
    print("0 passed, 1 failed")
    sys.exit(1)

from playwright.sync_api import sync_playwright  # noqa: E402

import notebook_browser_fixture as bf  # noqa: E402
import shoot  # noqa: E402
from test_screens_drive import PHONE, WIDE, Drive  # noqa: E402

passed = failed = 0
NOW = time.time()
TODAY = dt.date.today()
YEST = TODAY - dt.timedelta(days=1)
T0 = dt.datetime.combine(TODAY, dt.time(0, 0)).timestamp()
REL = "Projects/One Helm.md"
ROUTE = "#/note/" + urllib.parse.quote(REL, safe="")


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + repr(detail)[:600] if detail else ""))


ful = bf.fulfill


def q(route):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query)


FOLDERS = [("", 3), ("Fleet", 155), ("Projects", 260), ("Journal", 48), ("Archive", 120), ("Projects/Helm", 14), ("Daily", 40)]


def folders():
    return {"folders": [{"folder": f, "count": c} for f, c in FOLDERS], "stats": {"notes": 1372, "files": 210, "links": 4210, "links_resolved": 4102},
            "today": {"date": TODAY.isoformat(), "rel": "Daily/t.md", "exists": True, "open_tasks": 2},
            "previous": {"date": YEST.isoformat(), "rel": "Daily/y.md", "open_tasks": 1}, "allowed": ["Projects"], "read_only": False}


def day_payload(today=True, date=None):
    d = date or TODAY.isoformat()
    return {"date": d, "rel": "Daily/%s.md" % d, "exists": True, "editable": today, "mtime": NOW,
            "html": '<h1>Day</h1><p>6:05 PM Vaultwarden migration: 13 boxes left.</p><p>9:48 PM The redesign is the job tonight.</p>'
                    '<ul><li class="task"><input type="checkbox" class="task-box" data-line="4"> Pick the name</li></ul>',
            "frontmatter": {}, "open_tasks": 2,
            "tasks": [{"line": 4, "done": False, "text": "Pick the portal name"}, {"line": 5, "done": False, "text": "Read the mock-up"}, {"line": 6, "done": True, "text": "Done one"}],
            "timeline": [{"ts": NOW - 5000, "thread_id": "t-a", "title": "First thread", "kind": "filed", "label": "filed", "count": 1},
                         {"ts": NOW - 900, "thread_id": "t-b", "title": "Second thread", "kind": "answered", "label": "you answered", "count": 1}],
            "calls": [], "mentions": [], "changed": [{"rel": "Docker/helm-app.md", "title": "helm-app"}], "today": today,
            "carry": {"date": YEST.isoformat(), "rel": "Daily/y.md", "tasks": [{"line": 2, "done": False, "text": "Carry me"}]},
            "prev": YEST.isoformat(), "next": (TODAY + dt.timedelta(days=1)).isoformat()}


def changes_payload():
    def row(rel, title, ts, who, lines):
        return {"rel": rel, "title": title, "folder": rel.split("/")[0], "ts": NOW - ts, "kind": "append", "source": "helm", "principal": who, "lines": lines}
    return {"changes": [row("Changelog.md", "Changelog", 100, "agent", ["- first line", "- Docker/helm-app: rebuilt."]),
                        row("Projects/One Helm.md", "One Helm", 400, "owner", ["- the assistant PR merged."]),
                        row("Changelog.md", "Changelog", 900, "default", ["- older line"])], "since": T0}


def graph_payload():
    fl = [{"folder": f, "count": c, "last_mtime": NOW - 3600 * i, "last_rel": f + "/X.md", "last_title": "Newest in " + f, "week": [0, 1, 2, 0, 3, 1, 4]}
          for i, (f, c) in enumerate([("Fleet", 155), ("Projects", 260), ("Journal", 48), ("Archive", 120)])]
    return {"most_linked": [{"rel": "Ops Log.md", "title": "Ops Log", "folder": "Journal", "links_in": 83}, {"rel": "Changelog.md", "title": "Changelog", "folder": "Journal", "links_in": 62}],
            "folders": fl, "total_notes": 1372, "unresolved_links": 4}


def tasks_payload():
    return {"notes": [{"rel": "Projects/Vault.md", "title": "Vault", "folder": "Projects", "open": 13, "done": 1, "items": [{"line": 3, "text": "Move the vault"}]}], "total_open": 14}


def atlas_payload(n=8):
    return {"hosts": [{"rel": "Hosts/h%d.md" % i, "title": "host%d" % i, "mtime": NOW - i, "pages": [
        {"rel": "Docker/d%d%d.md" % (i, k), "title": "svc%d%d" % (i, k), "folder": "Docker" if k % 2 else "APIs"} for k in range(max(1, 5 - i // 2))]} for i in range(n)]}


def recent_payload():
    return {"notes": [{"rel": "Projects/One Helm.md", "folder": "Projects", "stem": "One Helm", "title": "One Helm", "mtime": NOW - 60, "size": 100, "daily": False},
                      {"rel": "Hosts/media-box.md", "folder": "Hosts", "stem": "media-box", "title": "media-box", "mtime": NOW - 120, "size": 100, "daily": False},
                      {"rel": "Old.md", "folder": "", "stem": "Old", "title": "Old page", "mtime": NOW - 5 * 86400, "size": 100, "daily": False}]}


def thread(tid, title, state="needs-you", summary="Install the first run and report back."):
    return {"thread_id": tid, "title": title, "category": "Helm", "state": state, "urgency": "this-week", "last_change": NOW - 8000, "summary": summary,
            "events": [], "links": [{"label": "One Helm", "url": "note:" + REL}], "ask": {"kind": "ack", "prompt": "ok"}, "match": "linked"}


NOTE_HTML = ('<h1>One Helm: the combined app</h1><h2>Shape</h2><p>See <a class="wl" href="#/note/Docker%2Fhelm-app.md" data-rel="Docker/helm-app.md">Docker/helm-app</a> '
             'and card t_ab12cd34 and the private card t_99999999.</p><h2>Decisions</h2><ul><li>One app.</li></ul>'
             '<h2>Open items</h2><ul><li class="task"><input type="checkbox" class="task-box" data-line="9"> Install it</li></ul><h2>Log</h2><ul><li>old</li></ul>')


def note_payload(size=2000, html=NOTE_HTML):
    return {"rel": REL, "folder": "Projects", "stem": "One Helm", "title": "One Helm: the combined app", "mtime": NOW - 600, "size": size, "daily": False,
            "frontmatter": {"title": "One Helm", "status": "live", "tags": ["project", "helm"], "owner": "owner"}, "html": html,
            "tasks": [{"line": 9, "done": False, "text": "Install it"}], "editable": True, "reason": "", "topic": True,
            "outlinks": [{"target": "Docker/helm-app", "rel": "Docker/helm-app.md", "alias": None, "embed": False, "line": 4, "title": "Docker/helm-app"},
                         {"target": "Gone", "rel": None, "alias": "Gone", "embed": False, "line": 6, "title": None}],
            "backlinks": [{"rel": "Docker/helm-app.md", "title": "helm-app", "folder": "Docker", "line": 2, "daily": False,
                           "text": "- The image is built for [[Projects/One Helm|One Helm]] and [[Other Page]]."},
                          {"rel": "Daily/2026-11-21.md", "title": "2026-11-21", "folder": "Daily", "line": 3, "daily": True, "text": "worked on [[One Helm]]"}]}


def topic_payload():
    return {"rel": REL, "title": "One Helm", "topic": True, "keys": {"stem": "One Helm", "name": "one helm", "cases": []},
            "open": [thread("t-ask", "Install on the Fold"), thread("t-run", "Fix the cast", "working", "Sketching")], "settled": [thread("t-old", "Old one", "done", "Done")], "settled_total": 3,
            "backlinks": note_payload()["backlinks"], "daily_mentions": [{"stem": "2026-11-21", "rel": "Daily/2026-11-21.md"}]}


def list_payload(folder):
    return {"folder": folder, "notes": [
        {"rel": "%s/Zed.md" % folder, "folder": folder, "stem": "Zed", "title": "Zed note", "mtime": NOW - 10, "size": 3000, "daily": False},
        {"rel": "%s/Alpha.md" % folder, "folder": folder + "/Sub", "stem": "Alpha", "title": "Alpha note", "mtime": NOW - 5000, "size": 900, "daily": False}]}


def daily_list():
    return {"folder": "Daily", "notes": [{"rel": "Daily/2026-11-21.md", "folder": "Daily", "stem": "2026-11-21", "title": "2026-11-21", "mtime": NOW, "size": 10, "daily": True},
                                         {"rel": "Daily/2026-10-02.md", "folder": "Daily", "stem": "2026-10-02", "title": "2026-10-02", "mtime": NOW - 90 * 86400, "size": 10, "daily": True}]}


class Site:
    """The routes one drive installs; flags flip a feed's answer, `seen` lists every request."""

    def __init__(self, site="home", mode="store", size=2000, html=NOTE_HTML):
        self.site, self.mode, self.size, self.html = site, mode, size, html
        self.seen, self.posts, self.flags = [], [], {}
        self.threads = [thread("t-ask", "Install on the Fold"), thread("t-run", "Fix the cast", "working", "Sketching")]
        self.cards = []

    def install(self, ctx):
        bf.install_shell_api(ctx)
        ctx.on("request", lambda r: self.seen.append(r.url))
        mods = ["inbox", "notebook", "activity", "search"]
        ctx.route("**/api/meta", lambda r: ful(r, {"ok": True, "site": self.site, "modules": mods, "notes_mode": self.mode, "web": {}}))
        ctx.route("**/api/triage/threads*", lambda r: ful(r, {"threads": self.threads, "total": len(self.threads), "page": 1, "pages": 1}))
        ctx.route("**/api/notes/folders", lambda r: ful(r, folders()))
        ctx.route("**/api/notes/meta", lambda r: ful(r, {"ok": True, "notes": {"notes": 1372, "files": 210, "links": 4210, "links_resolved": 4102}, "vault": "C:/x", "mode": self.mode}))
        ctx.route("**/api/notes/recent?limit=12", lambda r: ful(r, recent_payload()))
        ctx.route("**/api/day?date=*", lambda r: ful(r, day_payload(q(r)["date"][0] == TODAY.isoformat(), q(r)["date"][0])))
        ctx.route("**/api/notes/changes?*", lambda r: ful(r, changes_payload()) if not self.flags.get("changes") else ful(r, {"detail": "no feed" if self.flags["changes"] == 404 else "boom"}, self.flags["changes"]))
        ctx.route("**/api/notes/graph?*", lambda r: ful(r, graph_payload()) if not self.flags.get("nograph") else ful(r, {"detail": "no"}, 404))
        ctx.route("**/api/notes/tasks?*", lambda r: ful(r, tasks_payload()))
        ctx.route("**/api/notes/atlas", lambda r: ful(r, atlas_payload()))
        ctx.route("**/api/notes/note?rel=*", lambda r: ful(r, note_payload(self.size, self.html)))
        ctx.route("**/api/topic?rel=*", lambda r: ful(r, topic_payload()))
        ctx.route("**/api/notes/history?rel=*", lambda r: ful(r, {"rel": REL, "truth": "store", "revisions": [{"id": 1, "ts": NOW - 600, "source": "agent", "size": 2000}]}))
        ctx.route("**/api/notes/tail?*", lambda r: ful(r, {"rel": REL, "lines": [{"line": 3, "text": "- 2026-11-16 00:26 Live on the app."}, {"line": 4, "text": "- 2026-11-21 21:48 PR merged, see t_ab12cd34."}],
                                                           "last_heading": {"line": 2, "text": "Log"}, "size": 30000}))
        ctx.route("**/api/notes/list?folder=*", lambda r: ful(r, daily_list() if q(r)["folder"][0] == "Daily" else list_payload(q(r)["folder"][0])))
        ctx.route("**/api/notes/raw?rel=*", lambda r: ful(r, {"rel": REL, "text": "# x\n", "mtime": NOW, "editable": True}))

        def post(route):
            self.posts.append((route.request.url, route.request.post_data))
            if "/api/day/append" in route.request.url:
                return ful(route, day_payload())
            return ful(route, {"ok": True})
        ctx.route("**/api/day/append", post)
        ctx.route("**/api/notes/toggle", post)

    def reads(self, part):
        return [u for u in self.seen if part in u]


def drive(browser, site, size, route, **kw):
    return Drive(browser, bf.SimpleNamespace(web_url=kw.pop("url")), size, route, before_goto=lambda ctx: site.install(ctx), **kw)


def main():
    with bf.static_site() as srv, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        url = srv.web_url

        print("\n1 · the home's reads")
        s = Site()
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nb-home")
        d.page.wait_for_timeout(500)
        for part, n in (("/api/notes/recent?limit=12", 1), ("/api/day?date=", 1), ("/api/notes/changes?", 1), ("/api/notes/graph?limit=12", 1),
                        ("/api/notes/tasks?limit=20", 1), ("/api/notes/atlas", 1)):
            got = s.reads(part)
            check("one read of %s" % part, len(got) == n, got)
        ch = urllib.parse.parse_qs(urllib.parse.urlsplit(s.reads("/api/notes/changes?")[0]).query)
        check("changes asks since local midnight, limit 200", abs(float(ch["since"][0]) - T0) < 5 and ch["limit"] == ["200"], ch)
        check("no per-note, per-folder or topic read", not [u for u in s.seen if "/api/notes/note?" in u or "/api/notes/list?" in u or "/api/topic?" in u or "/history" in u or "/tail" in u], s.seen[-6:])
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\n2 · changed today")
        s = Site()
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nbx-chg-i")
        groups = d.eval("[...document.querySelectorAll('.nbx-chg-i')].map(g => ({page: g.querySelector('.hm-ochip-t').textContent, lines: [...g.querySelectorAll('.nbx-ln')].map(l => l.textContent)}))")
        check("the pages Helm wrote to come first, one group per page, newest page first", [g["page"] for g in groups][:2] == ["Changelog", "One Helm"], groups)
        cl = groups[0]["lines"]
        check("a page's newest lines lead, each with who wrote it, markup gone", cl[0].startswith("agent") and "Docker/helm-app: rebuilt." in cl[0] and "- " not in cl[0], cl)
        check("an owner's own writes read as You", any(l.startswith("You") for g in groups for l in g["lines"]), groups)
        check("a page edited outside Helm today comes from the recent list, marked", "media-box" in [g["page"] for g in groups] and d.eval("document.querySelector('.nbx-out').textContent").startswith("Edited outside"), groups)
        check("a page not touched today is not listed", "Old page" not in [g["page"] for g in groups])
        check("the deck counts what changed", "3 changed today" in d.eval("document.querySelector('.nb-home-deck').textContent"), d.eval("document.querySelector('.nb-home-deck').textContent"))
        check("each page is a chip that peeks", d.eval("document.querySelectorAll('.nbx-chg-h a.hm-ochip[data-peek=note]').length") >= 3)
        d.close()
        s = Site()
        s.flags["changes"] = 404
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nbx-chg")
        check("a site without the feed says nothing and still lists the pages edited today", not d.eval("!!document.querySelector('.nbx-chg .nb-inline-error')") and d.eval("document.querySelectorAll('.nbx-chg-i').length") == 2)
        d.close()
        s = Site()
        s.flags["changes"] = 500
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nbx-chg .nb-inline-error")
        check("a failing feed is said, and the rest of the home is there", d.eval("!!document.querySelector('.nb-today')") and d.eval("!!document.querySelector('.nbx-shelves')"))
        d.close()

        print("\n3 · the shelves")
        s = Site()
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nbx-spine")
        sp = d.eval("[...document.querySelectorAll('.nb-folder-list .nbx-spine')].map(a => ({t: a.querySelector('b').textContent, h: a.getBoundingClientRect().height, cold: a.classList.contains('cold'), href: a.getAttribute('href'), nw: (a.querySelector('.nw')||{}).textContent}))")
        names = [x["t"] for x in sp]
        check("a spine for every top-level folder, nested folders left to their parent", "Projects" in names and "Fleet" in names and "Projects/Helm" not in names, names)
        check("Archive is dashed and last", sp[-1]["t"] in ("Archive", "Root pages") and any(x["cold"] and x["t"] == "Archive" for x in sp), sp)
        byname = {x["t"]: x for x in sp}
        check("a shelf with more pages is taller", byname["Projects"]["h"] > byname["Journal"]["h"], (byname["Projects"]["h"], byname["Journal"]["h"]))
        check("a spine says its newest page on hover", "Newest in Fleet" in (byname["Fleet"]["nw"] or ""), byname["Fleet"])
        check("a spine is a real link to the folder", byname["Projects"]["href"] == "#/notebook/Projects", byname["Projects"])
        check("the 7-day activity is drawn", d.eval("document.querySelectorAll('.nbx-spine .wk i').length") >= 7)
        d.close()
        s = Site()
        s.flags["nograph"] = True
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nbx-spine")
        check("with no graph the folder list is the shelf (root pages first, as the index lists them)",
              d.eval("document.querySelector('.nb-folder-list a').getAttribute('href')") == "#/notebook?folder=" and d.eval("!document.querySelector('.nbx-spine .wk')"))
        d.close()

        print("\n4 · the fleet atlas")
        s = Site()
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nbx-atlas-box svg.chart")
        check("the busiest six hosts are plotted", d.eval("document.querySelectorAll('.nbx-atlas svg.chart .hostg').length") == 6)
        check("a host is a link that peeks as a host; a page peeks as a note",
              d.eval("document.querySelector('.hostg').dataset.peek") == "host" and d.eval("document.querySelector('.pgl').dataset.peek") == "note"
              and d.eval("document.querySelector('.hostg').getAttribute('href')").startswith("#/note/Hosts"))
        d.page.click("[data-atlasmore]")
        d.page.wait_for_function("document.querySelectorAll('.nbx-atlas svg.chart .hostg').length === 8")
        check("Show all draws every host", True)
        check("the chart has its grid and axis coordinates", d.eval("document.querySelectorAll('.nbx-atlas .grid').length") > 20 and d.eval("document.querySelectorAll('.nbx-atlas .axis').length") >= 3)
        check("no page errors", not d.errors, d.errors)
        d.close()
        d = drive(browser, s, PHONE, "#/notebook", url=url)
        d.wait("#main .nbx-atlas-box")
        d.page.wait_for_selector(".nbx-atlas-list")
        check("on a phone the atlas is a list of hosts with their pages as chips", d.eval("!document.querySelector('.nbx-atlas svg.chart')") and d.eval("document.querySelectorAll('.nbx-atlas-list li').length") == 6)
        d.close()

        print("\n5 · in play")
        s = Site()
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nbx-play li")
        play = d.eval("[...document.querySelectorAll('.nbx-play li')].map(li => ({page: li.querySelector('.hm-ochip-t').textContent, kinds: [...li.querySelectorAll('.why .hm-ochip')].map(c => c.className.split(' ').find(x => x.startsWith('t-')))}))")
        check("a page a live thread links to is in play, with its ask and thread as chips", play and play[0]["page"] == "Projects/One Helm" and play[0]["kinds"] == ["t-ask", "t-thread"], play)
        check("the pages with open boxes show them", d.eval("document.querySelector('.nbx-boxes').textContent").find("13 open") >= 0)
        check("the rail counts what is in play", d.eval("document.querySelector('#list-notebook a[href=\"#/notebook?play=1\"] .n').textContent") == "1")
        d.page.click('#list-notebook a[href="#/notebook?play=1"]')
        d.page.wait_for_timeout(600)
        check("the rail row goes to the section", "play=1" in d.hash())
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\n6 · today's page")
        s = Site()
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nbx-journal")
        check("the journal lines are there, with their times set apart", d.eval("document.querySelectorAll('.nbx-journal .nbx-jt').length") == 2 and "6:05 PM" in d.eval("document.querySelector('.nbx-jt').textContent"))
        check("the day's own title is not repeated", d.eval("!document.querySelector('.nbx-journal h1')"))
        d.page.click(".nbx-todo input >> nth=0")
        d.page.wait_for_timeout(500)
        tg = [x for x in s.posts if "/api/notes/toggle" in x[0]]
        check("ticking a box toggles that line of today's note", tg and json.loads(tg[0][1]) == {"rel": "Daily/%s.md" % TODAY.isoformat(), "line": 4, "base_mtime": day_payload()["mtime"], "done": True}, tg)
        d.page.fill("#append-line", "a line from home")
        d.page.click("[data-append]")
        d.page.wait_for_timeout(500)
        ap = [x for x in s.posts if "/api/day/append" in x[0]]
        check("the append box appends to today", ap and json.loads(ap[0][1])["line"] == "a line from home" and json.loads(ap[0][1])["date"] == TODAY.isoformat(), ap)
        check("the box is empty again", d.eval("document.getElementById('append-line').value") == "")
        check("the search field says Ctrl K", d.eval("document.querySelector('.nbx-search kbd').textContent") == "Ctrl K")
        d.page.click("[data-pal]")
        check("the search field opens the command palette", d.until("window.Helm.palette && window.Helm.palette.isOpen()"))
        check("no page errors", not d.errors, d.errors)
        d.close()
        s = Site(mode="folder-readonly")
        d = drive(browser, s, (1440, 900), "#/notebook", url=url)
        d.wait("#main .nbx-journal")
        check("read-only: no append box, no new note, boxes cannot be ticked",
              d.eval("!document.getElementById('append-line')") and d.eval("!document.querySelector('[data-newnote]')") and d.eval("[...document.querySelectorAll('.nbx-todo input')].every(i => i.disabled)"))
        check("read-only is said in the rail", d.eval("!!document.querySelector('#list-notebook .nb-readonly')"))
        d.close()

        print("\n7 · a note")
        s = Site(size=30000)
        d = drive(browser, s, (1440, 900), ROUTE, url=url)
        d.wait("#main .nb-note-view .md")
        check("one title: the page's own first heading, and it is the first thing in the reading", d.eval("document.querySelectorAll('.nb-note-view .md h1').length") == 1
              and d.eval("document.querySelector('.nb-note-view .md').firstElementChild.tagName") == "H1")
        gl = d.eval("document.querySelector('.nbx-glance').textContent").replace("\xa0", " ")
        check("the at-a-glance line: who updated it, size and read time, links in, open tasks, waiting threads",
              "by agent" in gl and "29 KB" in gl and "min read" in gl and "2 links in" in gl and "1 open task" in gl and "1 waiting thread" in gl, gl)
        pills = d.eval("[...document.querySelectorAll('.nbx-pills .hm-pill')].map(p => p.textContent)")
        check("tags and the status are pills; the raw grid is behind a disclosure", pills == ["live", "project", "helm"] and d.eval("!!document.querySelector('details.nbx-props .props')"), pills)
        outline = d.eval("[...document.querySelectorAll('.nbx-outline [data-nbsec]')].map(a => a.textContent)")
        check("the outline is the page's headings, Latest entries first", outline == ["Latest entries", "Shape", "Decisions", "Open items", "Log"], outline)
        check("a wikilink is a note chip", d.eval("document.querySelector('.md a.hm-ochip.t-note').getAttribute('href')") == "#/note/Docker%2Fhelm-app.md" and d.eval("document.querySelectorAll('.md a.wl').length") == 0)
        check("an append-style page reads its tail: latest entries, newest first", d.eval("document.querySelector('.nbx-latest .le p').textContent").startswith("PR merged") and d.eval("document.querySelectorAll('.nbx-latest .le').length") == 2)
        check("...with the older entries linked", "Older entries in Log" in d.eval("document.querySelector('.nbx-latest .lh a').textContent"))
        check("the tail is one read and history one read", len(s.reads("/api/notes/tail")) == 1 and len(s.reads("/api/notes/history")) == 1)
        d.page.click('.nbx-outline [data-nbsec] >> text=Decisions')
        d.page.wait_for_timeout(600)
        check("an outline link scrolls and keeps the route", d.hash().startswith("#/note/"), d.hash())
        d.close()
        s = Site(size=2000, html=NOTE_HTML.replace("<h2>Log</h2><ul><li>old</li></ul>", ""))
        d = drive(browser, s, (1440, 900), ROUTE, url=url)
        d.wait("#main .nb-note-view .md")
        d.page.wait_for_timeout(300)
        check("a short page that does not end in a log reads no tail", not s.reads("/api/notes/tail") and d.eval("!document.querySelector('.nbx-latest')"))
        d.close()

        print("\n8 · the Connected pane")
        s = Site()
        d = drive(browser, s, (1440, 900), ROUTE, url=url)
        d.wait("#props-body .hm-conn .hm-conn-group")
        groups = d.eval("[...document.querySelectorAll('#props-body .hm-conn-group')].map(g => g.querySelector('.hm-conn-gh').textContent)")
        check("the groups: waiting on you, threads, linked from, links out, days", [g.rstrip("0123456789 pagesage") for g in groups][0].startswith("Waiting on you")
              and any(g.startswith("Threads about it") for g in groups) and any(g.startswith("Linked from") for g in groups) and any(g.startswith("Links out") for g in groups) and any(g.startswith("Days it appears on") for g in groups), groups)
        check("the pane's title is Connected", d.eval("document.getElementById('props-title').textContent") == "Connected")
        wait = d.eval("(() => { const g = document.querySelector('#props-body [data-group=waiting]'); return {chip: g.querySelector('.hm-ochip.t-ask').textContent, open: g.querySelector('a.btn').getAttribute('href')} })()")
        check("an ask that cites the note leads, with its summary and a button that opens it", wait["chip"] == "Install on the Fold" and wait["open"] == "#/conv/t-ask", wait)
        em = d.eval("[...document.querySelectorAll('#props-body [data-group=linked] .hm-conn-line em')].map(e => e.textContent)")
        check("a backlink shows its sentence with the title of this note marked, and no other link marked", em == ["One Helm", "One Helm"], em)
        check("the other page's wikilink is plain words in the sentence", "Other Page" in d.eval("document.querySelector('#props-body [data-group=linked] .hm-conn-line').textContent")
              and d.eval("document.querySelectorAll('#props-body [data-group=linked] .hm-conn-line em').length") == 2)
        check("a daily-note backlink is a day chip", d.eval("!!document.querySelector('#props-body [data-group=linked] .hm-ochip.t-day')"))
        check("settled threads are folded with their count", d.eval("document.querySelector('#props-body .nbx-settled summary').textContent") == "3 settled")
        check("the unresolved name and the counts stay in the page", d.eval("document.querySelector('.nb-unresolved li span').textContent") == "Gone" and d.eval("document.querySelectorAll('.nb-connection-stats > div').length") == 4)
        check("the note page keeps the answerable thread cards (reply, snooze, close...) and the Matched on line",
              d.eval("document.querySelectorAll('#nb-note-threads [data-act]').length") > 0 and "Matched on" in d.eval("document.getElementById('nb-note-threads').textContent"))
        check("a chip in the Connected pane wraps its label instead of cutting it",
              d.eval("[...document.querySelectorAll('#props-body .hm-ochip')].every(c => c.scrollWidth <= c.clientWidth + 1)"))
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\n10 · topic, day, folder")
        s = Site()
        d = drive(browser, s, (1440, 900), "#/topic/" + urllib.parse.quote(REL, safe=""), url=url)
        d.wait("#main .ctx")
        check("the topic keeps the thread cards (answerable here) and draws the rest of Connected, bigger",
              d.eval("document.querySelectorAll('#main .ctx .thread').length") == 2 and d.eval("!!document.querySelector('#nb-topic-conn [data-group=linked]')") and d.eval("!document.querySelector('#nb-topic-conn [data-group=waiting]')"))
        check("the topic has no right pane", d.eval("document.body.classList.contains('props-empty') || !document.body.classList.contains('props-has')"))
        d.eval("location.hash = '#/day/%s'" % TODAY.isoformat())
        d.wait("#main .hm-log")
        log = d.eval("[...document.querySelectorAll('#main .hm-log-entry .hm-ochip-t')].map(x => x.textContent)")
        check("the day is a log, newest first, with the Now line on today", log == ["Second thread", "First thread"] and d.eval("!!document.querySelector('.hm-log-now')"), log)
        check("the carry-over can become a card", d.eval("!!document.querySelector('[data-mkthread]')"))
        check("the day's pages that changed are chips", d.eval("!!document.querySelector('.nbx-changed .hm-ochip.t-note')"))
        d.eval("location.hash = '#/day/%s'" % YEST.isoformat())
        d.wait(".dayhead")
        d.page.wait_for_timeout(400)
        check("a past day has no Now line", d.eval("!document.querySelector('.hm-log-now')"))
        d.eval("location.hash = '#/notebook/Projects'")
        d.wait("#main .nbx-rows")
        titles = lambda: d.eval("[...document.querySelectorAll('.nbx-rows .nbx-row-t')].map(a => a.textContent)")
        check("a folder lists its pages by last update", titles() == ["Zed note", "Alpha note"], titles())
        check("each row says where it lives, how big it is and when it changed", d.eval("document.querySelector('.nbx-rows li:nth-child(2) .nbx-sub-f').textContent") == "Sub" and "KB" in d.eval("document.querySelector('.nbx-rows li').textContent") and "ago" in d.eval("document.querySelector('.nbx-rows li:nth-child(2)').textContent"))
        d.page.click("[data-sort=name]")
        check("sorting by name reorders them", titles() == ["Alpha note", "Zed note"], titles())
        d.page.reload()
        d.wait("#main .nbx-rows")
        check("the sort is remembered", titles() == ["Alpha note", "Zed note"], titles())
        check("a nested folder is drawn as a shelf of sub-folders", d.eval("document.querySelectorAll('.nbx-subshelf .nbx-spine').length") == 1)
        d.eval("location.hash = '#/notebook/Daily'")
        d.wait("#main .month")
        months = d.eval("[...document.querySelectorAll('#main .month')].map(m => m.textContent)")
        check("dailies are grouped by month", len(months) == 2 and any("2026" in m for m in months), months)
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\n11 · a phone")
        s = Site( size=30000)
        for route, sel in (("#/notebook", ".nb-home .nbx-shelf"), (ROUTE, ".nb-note-view .nb-connections"), ("#/day/%s" % TODAY.isoformat(), ".daygrid"),
                           ("#/notebook/Projects", ".nbx-rows"), ("#/topic/" + urllib.parse.quote(REL, safe=""), ".ctx")):
            d = drive(browser, s, PHONE, route, url=url)
            d.wait(sel)
            d.page.wait_for_timeout(500)
            check(route[:22] + ": no sideways scroll", d.eval("document.documentElement.scrollWidth") <= 412 and d.eval("document.querySelector('#view-notebook').scrollWidth") <= 413,
                  (d.eval("document.documentElement.scrollWidth"), d.eval("document.querySelector('#view-notebook').scrollWidth")))
            if route == "#/notebook":
                small = d.eval("[...document.querySelectorAll('.nb-home a, .nb-home button')].filter(a => {const r=a.getBoundingClientRect();return r.width>0&&r.height>0&&r.height<43.9}).map(a => a.textContent.trim().slice(0, 30))")
                check("every visible home control has a 44px target", not small, small)
            if route == ROUTE:
                check("the Connected content is drawn inside the note once (there is no right pane on a phone); the thread cards are answerable above it",
                      d.eval("!!document.querySelector('#nb-conn-inline [data-group=linked]')") and d.eval("getComputedStyle(document.querySelector('#nb-conn-inline')).display") != "none"
                      and d.eval("!document.querySelector('#nb-conn-inline [data-group=waiting]')") and d.eval("document.querySelectorAll('#nb-note-threads [data-act]').length") > 0
                      and d.eval("getComputedStyle(document.querySelector('.nb-connection-stats')).display") == "none")
                pills = d.eval("getComputedStyle(document.querySelector('.nbx-outline ol')).display")
                check("the outline is a row of pills on a phone", pills == "flex", pills)
            check("no page errors", not d.errors, d.errors)
            d.close()

        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
