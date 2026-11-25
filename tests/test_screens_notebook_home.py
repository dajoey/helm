"""Playwright drive: the composed notebook home and its root-folder route.

Run: python3 tests/test_screens_notebook_home.py

Synthetic API payloads exercise the real notebook module and shipped static assets:
recent/day data and independent failures, no N+1 reads, a late refresh losing to
navigation, root pages at #/notebook?folder=, and actual phone main-pane selection.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
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

import notebook_browser_fixture as browser_fixture  # noqa: E402
import shoot  # noqa: E402
from test_screens_drive import PHONE, WIDE, Drive  # noqa: E402

passed = failed = 0
NOW = time.time()
TODAY = dt.date.today()
YESTERDAY = TODAY - dt.timedelta(days=1)


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + repr(detail)[:600] if detail else ""))


def fulfill(route, payload, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))


def payloads():
    return {
        "folders": {
            "folders": [{"folder": "", "count": 1}, {"folder": "Projects", "count": 4},
                        {"folder": "Reference", "count": 8}],
            "stats": {"notes": 137, "files": 18, "links": 450, "links_resolved": 402},
            "today": {"date": TODAY.isoformat(), "rel": "Daily/today.md", "exists": True, "open_tasks": 2},
            "previous": {"date": YESTERDAY.isoformat(), "rel": "Daily/yesterday.md", "open_tasks": 2},
            "allowed": ["Projects", "Reference"], "read_only": False, "profile": "work",
        },
        "meta": {"ok": True, "notes": {"notes": 137, "files": 18, "links": 450, "links_resolved": 402},
                 "vault": "C:/synthetic/wiki", "mode": "store"},
        "recent": {"notes": [
            {"rel": "Projects/Alpha.md", "folder": "Projects", "stem": "Alpha",
             "title": "Alpha <Overview>", "mtime": NOW - 60, "size": 100, "daily": False},
            {"rel": "Root.md", "folder": "", "stem": "Root", "title": "Root page",
             "mtime": NOW - 3600, "size": 80, "daily": False},
        ]},
        "day": {
            "date": TODAY.isoformat(), "rel": "Daily/today.md", "exists": True, "editable": True,
            "mtime": NOW, "html": "", "frontmatter": {}, "open_tasks": 2,
            "tasks": [{"line": 2, "done": False, "text": "Review the notebook <draft>"},
                      {"line": 3, "done": False, "text": "Send the second task"},
                      {"line": 4, "done": True, "text": "Already finished"}],
            "timeline": [
                {"ts": NOW - 300, "thread_id": "t-old", "title": "Oldest thread", "kind": "noted", "label": "your note", "count": 1},
                {"ts": NOW - 200, "thread_id": "t-middle", "title": "Middle thread", "kind": "answered", "label": "you answered", "count": 1},
                {"ts": NOW - 100, "thread_id": "t-new", "title": "Newest thread", "kind": "run", "label": "a run reported", "count": 2},
            ],
            "calls": [], "mentions": [], "changed": [], "today": True,
            "carry": {"date": YESTERDAY.isoformat(), "rel": "Daily/yesterday.md",
                      "tasks": [{"line": 2, "done": False, "text": "Carry this forward"},
                                {"line": 3, "done": False, "text": "Second carried task"}]},
            "prev": YESTERDAY.isoformat(), "next": (TODAY + dt.timedelta(days=1)).isoformat(),
        },
        "root": {"folder": "", "notes": [{"rel": "Root.md", "folder": "", "stem": "Root",
                                          "title": "Root <Index>", "mtime": NOW - 3600,
                                          "size": 80, "daily": False}]},
    }


def install_home_routes(ctx, flags, pending, requests):
    data = payloads()
    ctx.on("request", lambda request: requests.append(request.url))

    def recent(route):
        if flags.get("hold"):
            pending.append(route)
        elif flags.get("recent_fail"):
            fulfill(route, {"detail": "synthetic recent outage"}, 503)
        else:
            fulfill(route, data["recent"])

    def day(route):
        if flags.get("day_fail"):
            fulfill(route, {"detail": "synthetic day outage"}, 503)
        else:
            fulfill(route, data["day"])

    ctx.route("**/api/notes/recent?limit=12", recent)
    ctx.route("**/api/day?date=*", day)
    ctx.route("**/api/notes/folders", lambda route: fulfill(route, data["folders"]))
    ctx.route("**/api/notes/meta", lambda route: fulfill(route, data["meta"]))
    ctx.route("**/api/notes/list?folder=", lambda route: fulfill(route, data["root"]))
    note = {"rel": "Reference/Route table.md", "folder": "Reference", "stem": "Route table",
            "title": "Late guard note", "mtime": NOW, "size": 30, "daily": False,
            "frontmatter": {}, "html": "<p>Late guard note</p>", "tasks": [], "outlinks": [],
            "backlinks": [], "editable": True, "reason": "", "topic": True}
    topic = {"rel": note["rel"], "title": note["title"], "topic": True,
             "keys": {"stem": "Route table", "name": "route table", "cases": []},
             "open": [], "settled": [], "settled_total": 0, "backlinks": [], "daily_mentions": []}
    ctx.route("**/api/notes/note?rel=*", lambda route: fulfill(route, note))
    ctx.route("**/api/topic?rel=*", lambda route: fulfill(route, topic))
    return data


def release_recent(flags, pending, data):
    flags["hold"] = False
    for route in pending:
        fulfill(route, data["recent"])
    pending.clear()


def main():
    with browser_fixture.static_site() as site, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        flags, pending, requests = {}, [], []
        data = {}
        d = Drive(browser, site, WIDE, "#/notebook",
                  before_goto=lambda ctx: browser_fixture.install_shell_api(ctx) or data.update(install_home_routes(ctx, flags, pending, requests)))
        d.wait("#main .nb-home")

        print("\n1 · aggregate, recent, today, and folder data")
        stats = d.eval("document.querySelector('.nb-stats').textContent")
        check("aggregate notes, files, and resolved links come from folders", all(x in stats for x in ("137", "18", "402", "450")), stats)
        check("the recent endpoint supplies the visible titles",
              d.eval("[...document.querySelectorAll('.nb-recent-title')].map(a => a.textContent)") == ["Alpha <Overview>", "Root page"])
        check("API text is escaped, not interpreted as markup",
              d.eval("document.querySelector('.nb-recent-title').textContent") == "Alpha <Overview>"
              and not d.eval("!!document.querySelector('.nb-recent-title Overview')"))
        check("recent pages are real anchors",
              d.eval("document.querySelector('.nb-recent-title').getAttribute('href')") == "#/note/Projects%2FAlpha.md")
        check("today shows open tasks and carry-over",
              d.eval("document.querySelectorAll('.nb-today .nb-task-list li').length") == 4
              and "Carry this forward" in d.eval("document.querySelector('.nb-today').textContent"))
        check("the latest timeline entry appears first",
              d.eval("document.querySelector('.nb-timeline-list a').textContent") == "Newest thread")
        check("the canonical recent read is exactly limit=12",
              any(urllib.parse.urlsplit(u).path == "/api/notes/recent" and urllib.parse.parse_qs(urllib.parse.urlsplit(u).query) == {"limit": ["12"]}
                  for u in requests), requests)
        paths = [urllib.parse.urlsplit(u).path for u in requests]
        for feed in ("/api/notes/recent", "/api/day", "/api/notes/changes", "/api/notes/graph", "/api/notes/tasks"):
            check("the home reads %s once (all feeds in parallel, one call each)" % feed, paths.count(feed) == 1, paths.count(feed))
        bad = [u for u in requests if "/api/notes/note?" in u or "/api/topic?" in u or "/api/notes/list?" in u]
        check("home makes no per-note, per-folder, or topic N+1 requests", not bad, bad)

        print("\n2 · root-folder navigation through the explicit empty folder query")
        check("the root folder uses the canonical empty folder query",
              d.eval("document.querySelector('.nb-folder-list a').getAttribute('href')") == "#/notebook?folder=")
        d.page.click('.nb-folder-list a[href="#/notebook?folder="]')
        d.wait("#main .nb-root-folder")
        check("the root route lists root pages", d.hash() == "#/notebook?folder="
              and "Root <Index>" in d.eval("document.querySelector('.nb-root-folder').textContent"), d.hash())
        check("root does not offer a create control that would file elsewhere",
              d.eval("document.querySelector('.nb-root-folder [data-newnote]')") is None)
        check("root still requests the canonical empty folder query",
              any(urllib.parse.urlsplit(u).path == "/api/notes/list" and urllib.parse.urlsplit(u).query == "folder=" for u in requests), requests[-8:])

        print("\n3 · recent and day fail independently")
        d.eval("location.hash = '#/notebook'")
        d.wait("#main .nb-home")
        flags["recent_fail"] = True
        d.page.click("[data-nbrefresh]")
        d.wait("#main .nb-recent .nb-inline-error")
        check("recent failure leaves today visible", d.eval("!!document.querySelector('.nb-today .nb-timeline-list')"))
        flags["recent_fail"] = False
        flags["day_fail"] = True
        d.page.click("[data-nbrefresh]")
        d.wait("#main .nb-today .nb-inline-error")
        check("day failure leaves recent pages visible", d.eval("document.querySelectorAll('.nb-recent-list li').length") == 2)
        flags["day_fail"] = False

        print("\n4 · a late home response cannot replace a note")
        flags["hold"] = True
        d.page.click("[data-nbrefresh]")
        deadline = time.time() + 8
        while not pending and time.time() < deadline:
            d.page.wait_for_timeout(100)
        check("the home recent response is held", bool(pending))
        d.eval("location.hash = '#/note/Reference%2FRoute%20table.md'")
        d.wait("#main .nb-connections")
        release_recent(flags, pending, data)
        d.page.wait_for_timeout(600)
        check("the note remains after the stale home response lands",
              d.eval("!!document.querySelector('#main .nb-connections') && !document.querySelector('#main .nb-home')"))
        check("no page errors on the home paths", not d.errors, d.errors)
        d.close()

        print("\n5 · phone layout and reduced motion with the main pane selected")
        flags, pending, requests = {}, [], []
        d = Drive(browser, site, PHONE, "#/notebook", reduced_motion="reduce",
                  before_goto=lambda ctx: browser_fixture.install_shell_api(ctx) or install_home_routes(ctx, flags, pending, requests))
        d.wait("#main .nb-home")
        d.page.wait_for_selector("#main .nb-home", state="visible")
        check("phone home selects the main pane, not the left rail", d.eval("document.body.dataset.pane") == "main")
        check("the home itself fits the phone without horizontal overflow",
              d.eval("document.querySelector('.nb-home').scrollWidth <= document.querySelector('.nb-home').clientWidth + 1"),
              d.eval("({scroll: document.querySelector('.nb-home').scrollWidth, client: document.querySelector('.nb-home').clientWidth})"))
        targets = d.eval("[...document.querySelectorAll('.nb-home a,.nb-home button')].filter(a => {const r=a.getBoundingClientRect();return r.width>0&&r.height>0}).map(a => ({text:a.textContent.trim(),height:a.getBoundingClientRect().height}))")
        check("every visible home control has a 44px phone target", bool(targets) and all(t["height"] >= 43.9 for t in targets),
              [t for t in targets if t["height"] < 43.9])
        durations = d.eval("getComputedStyle(document.querySelector('.nb-recent-title')).transitionDuration")
        # The shell's reduced-motion sheet caps every transition at .01ms (!important).
        check("reduced motion makes the recent-title transition effectively instant",
              all(float(x.strip().removesuffix("s")) <= 0.001 for x in durations.split(",")), durations)
        check("reduced motion also removes the hover transform",
              d.eval("getComputedStyle(document.querySelector('.nb-recent-title')).transform") == "none")
        check("no page errors at phone size", not d.errors, d.errors)
        d.close()
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
