"""Playwright drive: Connected to this note, from the already-fetched payloads.

Run: python3 tests/test_screens_notebook_connections.py

Covers outgoing-note deduplication, unresolved links as text, canonical attachment
encoding, backlink/thread/daily counts, explicit versus suggested threads, escaping,
the full-topic link, no extra connection requests, and the untouched editor/aside.
"""
from __future__ import annotations

import json
import os
import sys
import time

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
from test_screens_drive import WIDE, Drive  # noqa: E402

passed = failed = 0
REL = "Reference/Route table.md"
ROUTE = "#/note/Reference%2FRoute%20table.md"
NOW = time.time()


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + repr(detail)[:600] if detail else ""))


def fulfill(route, payload):
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


def thread(tid, title, match):
    return {"thread_id": tid, "title": title, "category": "Synthetic", "state": "needs-you",
            "urgency": "this-week", "last_change": NOW, "summary": "Synthetic thread summary",
            "events": [], "links": [], "ask": {"kind": "ack", "prompt": "Acknowledge this synthetic card"},
            "match": match, "match_why": "synthetic topic match" if match == "suggested" else ""}


def install_routes(ctx, requests):
    ctx.on("request", lambda request: requests.append(request.url))
    note = {
        "rel": REL, "folder": "Reference", "stem": "Route table", "title": "Route <table>",
        "mtime": NOW, "size": 300, "daily": False, "frontmatter": {},
        "html": '<p id="sanitized-body">The server-sanitized <strong>body</strong>.</p>',
        "tasks": [], "editable": True, "reason": "", "topic": True,
        "outlinks": [
            {"target": "Reference/Alpha", "rel": "Reference/Alpha.md", "alias": "Alpha alias",
             "embed": False, "line": 2, "title": "Alpha & <Beta>"},
            {"target": "Reference/Alpha", "rel": "Reference/Alpha.md", "alias": "Duplicate alias",
             "embed": False, "line": 3, "title": "Duplicate title"},
            {"target": "Missing <Page>", "rel": None, "alias": "Missing <Page>",
             "embed": False, "line": 4, "title": None},
            {"target": "Missing <Page>", "rel": None, "alias": "A second name",
             "embed": False, "line": 5, "title": None},
            {"target": "attachments/Q4 & final#v1?.pdf", "rel": "attachments/Q4 & final#v1?.pdf",
             "alias": "Quarterly <file>", "embed": False, "line": 6, "title": None},
        ],
        "backlinks": [
            {"rel": "Projects/One.md", "title": "One", "folder": "Projects", "line": 2, "daily": False},
            {"rel": "Projects/Two.md", "title": "Two", "folder": "Projects", "line": 2, "daily": False},
            {"rel": "Projects/One.md", "title": "One", "folder": "Projects", "line": 9, "daily": False},
        ],
    }
    topic = {
        "rel": REL, "title": "Route <table>", "topic": True,
        "keys": {"stem": "Route table", "name": "route table", "cases": []},
        "open": [thread("t-explicit", "Explicit thread", "linked"),
                 thread("t-suggested", "Suggested thread", "suggested")],
        "settled": [dict(thread("t-settled", "Settled suggested", "suggested"), state="done")],
        "settled_total": 3,
        "backlinks": note["backlinks"][:2],
        "daily_mentions": [{"stem": "2026-11-20", "rel": "Daily/2026-11-20.md"},
                           {"stem": "2026-11-21", "rel": "Daily/2026-11-21.md"}],
    }
    raw = {"rel": REL, "text": "# Route table\n\nEditor text\n", "mtime": NOW, "editable": True}
    ctx.route("**/api/notes/note?rel=*", lambda route: fulfill(route, note))
    ctx.route("**/api/topic?rel=*", lambda route: fulfill(route, topic))
    ctx.route("**/api/notes/raw?rel=*", lambda route: fulfill(route, raw))


def main():
    with browser_fixture.static_site() as site, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        requests = []
        d = Drive(browser, site, WIDE, ROUTE,
                  before_goto=lambda ctx: browser_fixture.install_shell_api(ctx) or install_routes(ctx, requests))
        d.wait("#main .nb-connections")

        print("\n1 · outgoing links and local context")
        check("outgoing notes are deduplicated by resolved path",
              d.eval("document.querySelectorAll('.nb-connection-columns section:first-child .nb-connection-list li').length") == 1)
        check("the outgoing note title is preserved as escaped text",
              d.eval("document.querySelector('.nb-connection-list a').textContent") == "Alpha & <Beta>"
              and not d.eval("!!document.querySelector('.nb-connection-list Beta')"))
        check("the outgoing note is a real in-app anchor",
              d.eval("document.querySelector('.nb-connection-list a').getAttribute('href')") == "#/note/Reference%2FAlpha.md")
        check("duplicate unresolved targets appear once, as text only",
              d.eval("document.querySelectorAll('.nb-unresolved li').length") == 1
              and d.eval("document.querySelectorAll('.nb-unresolved a').length") == 0
              and d.eval("document.querySelector('.nb-unresolved li span').textContent") == "Missing <Page>")
        check("attachment paths use canonical whole-path encoding",
              d.eval("document.querySelector('.nb-connection-columns section:nth-child(2) a').getAttribute('href')")
              == "/att/attachments%2FQ4%20%26%20final%23v1%3F.pdf")
        counts = d.eval("[...document.querySelectorAll('.nb-connection-stats > div')].map(x => ({label:x.querySelector('dt').textContent,value:x.querySelector('dd').childNodes[0].textContent.trim()}))")
        expected = [{"label": "Backlinks", "value": "2"}, {"label": "Open threads", "value": "2"},
                    {"label": "Settled threads", "value": "3"}, {"label": "Daily mentions", "value": "2"}]
        check("backlink, open, settled, and daily counts use the fetched payloads", counts == expected, counts)
        check("suggested open threads are distinguished from explicit links",
              d.eval("document.querySelector('.nb-count-note').textContent") == "1 explicit · 1 suggested")
        check("a truncated settled list does not invent an explicit/suggested split",
              d.eval("document.querySelectorAll('.nb-count-note').length") == 1)
        check("the full topic link names the existing route",
              d.eval("document.querySelector('.nb-connections-head > a').getAttribute('href')")
              == "#/topic/Reference%2FRoute%20table.md")
        check("the existing sanitized note HTML path still renders",
              d.eval("document.querySelector('#sanitized-body strong').textContent") == "body")
        check("the Connected pane carries the note's threads and links (it replaced the topic aside)", d.eval("!!document.querySelector('#pane-props #props-body .hm-conn .hm-conn-group')"))
        note_reads = [u for u in requests if "/api/notes/note?" in u]
        topic_reads = [u for u in requests if "/api/topic?" in u]
        check("connections add no N+1 note or topic requests", len(note_reads) == 1 and len(topic_reads) == 1,
              {"note": note_reads, "topic": topic_reads})
        check("no page errors in the read view", not d.errors, d.errors)

        print("\n2 · editing keeps its original mount and drops only the read-only summary")
        d.eval("location.hash = '#/edit/Reference%2FRoute%20table.md'")
        d.wait("#editor-host .cm-editor")
        check("the editor still mounts", d.eval("!!document.querySelector('#editor-host .cm-editor')"))
        check("the read-view connections section is not mounted over the editor",
              d.eval("document.querySelectorAll('#main .nb-connections').length") == 0)
        check("the Connected pane still survives editing", d.eval("!!document.querySelector('#pane-props #props-body .hm-conn .hm-conn-group')"))
        check("no page errors in the editor path", not d.errors, d.errors)
        d.close()
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
