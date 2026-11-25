"""Anything a thread names inside the app is a link to it.

the owner 2026-09-26, looking at a thread's properties on the reference app (its FROM row named the call's
minutes file as text, its THREAD row was the id in link colours and not a link): "if it's a
reference to something within the app, it should link to it."

The notebook resolves what a thread names (topic.refs, GET /api/thread/{id}/refs): each
source's id and each link's url is a note (a path or note: url under the notebook, answered
in the note's own spelling; else a note's exact name, as a wikilink resolves it), another
thread (its id), or nothing. The conversation's properties and the topic hub's card draw each
hit as a link, and the THREAD row is the thread's own link.

Run: PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/test_in_app_refs.py

  1. the refs route: the minutes source names its note, a source and a link naming
     another thread name it, a note: link in another case names the note as held, and the
     thread itself, a case number, a bare string and a web address name nothing; 404 for an
     unknown thread; the web role serves it; the topic hub's card carries the same refs.
  2. the home fence: a page the home notebook does not serve (the wiki's work/) is never a ref.
  3. drive: FROM links the minutes note and the other thread and shows a bare
     string source's text; Links links the other thread and the note as held; THREAD is the
     thread's own link; the FROM thread link opens that conversation; the note's topic hub card
     links its sources too.

Playwright and Chromium are required for part 3 (CI installs them); HELM_SKIP_DRIVES=1 skips it
and reports one failed check, so a skipped run never reads as green.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "screens"))

import fixture  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail != "" else ""))


def call(url, body=None, method=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def api(site, path, body=None, method=None):
    return call(site.api_url + path, body, method, {"Authorization": "Bearer " + fixture.TOKEN})


NOTE = "Contacts/Pine Hollow portal.md"          # fixture notes (tools/screens/fixture.py)
OTHER = "t-registrar-cert"                         # a fixture thread
TID = "t-refs-from-minutes"


def file_thread(site):
    return api(site, "/api/triage/threads", {"threads": [{
        "thread_id": TID, "title": "Send the portal brief to the board", "summary": "An action item from the call's minutes.",
        "category": "Calls", "urgency": "whenever", "state": "needs-you", "responder": "agent",
        "ask": {"kind": "ack", "prompt": "Read it and close it", "requires": ["decision"],
                "options": [{"label": "Got it", "value": "ack", "effect": "Closes the card"}]},
        "links": [{"label": "Related", "url": OTHER},
                  {"label": "Portal page", "url": "note:Contacts/Pine Hollow portal"},
                  {"label": "Runbook", "url": "https://kb.example.com/portal"}],
        "sources": [{"source": "meetings", "id": "Pine Hollow portal.md", "note": "action item from the minutes"},
                    {"source": "helm", "id": OTHER},
                    {"source": "helm", "id": TID},
                    {"source": "crm", "id": "00100002"},
                    {"source": "monitor", "id": "%/%"},
                    {"source": "monitor", "id": "2026-09/2026_09-25"},
                    "a bare string source"]}]}, "PUT")


def part_api(st):
    print("1. the refs route")
    site = st.home
    code, out = file_thread(site)
    check("the thread is filed", code == 200 and out.get("created") == 1, (code, out))
    code, r = api(site, "/api/thread/%s/refs" % TID)
    refs = r.get("refs", {})
    check("the route answers", code == 200 and r.get("thread_id") == TID, (code, r))
    check("the minutes source names its note", refs.get("Pine Hollow portal.md") == {"note": NOTE}, refs)
    check("a source naming another thread names it", refs.get(OTHER) == {"thread": OTHER}, refs)
    check("a note: link in another case names the note as it is held", refs.get("note:Contacts/Pine Hollow portal") == {"note": NOTE}, refs)
    check("the thread itself, a case number, a bare string and a web address name nothing",
          not ({TID, "00100002", "a bare string source", "https://kb.example.com/portal"} & set(refs)), refs)
    check("an id holding % or _ is matched literally, never as a wildcard (review 26)",
          not ({"%/%", "2026-09/2026_09-25"} & set(refs)), refs)
    code, _ = api(site, "/api/thread/t-no-such-thread/refs")
    check("an unknown thread is a 404", code == 404, code)
    code, w = call(site.web_url + "/api/thread/%s/refs" % TID, headers={"X-Forwarded-User": fixture.USER})
    check("the web role serves it", code == 200 and w.get("refs") == refs, (code, w))
    code, hub = api(site, "/api/topic?rel=" + urllib.parse.quote(NOTE))
    card = next((t for t in hub.get("open", []) if t["thread_id"] == TID), {})
    check("the note's topic hub card for the thread carries the same refs", card.get("refs") == refs, card.get("refs"))


def part_visible():
    print("2a. topic.refs drops what the notebook would not serve (review 26)")
    import sqlite3
    from notebook import topic
    ncon = sqlite3.connect(":memory:")
    ncon.row_factory = sqlite3.Row
    ncon.execute("CREATE TABLE notes (rel TEXT PRIMARY KEY, stem TEXT NOT NULL)")
    ncon.executemany("INSERT INTO notes VALUES (?, ?)", [("work/Case review.md", "Case review"), ("Reference/Ticket numbers.md", "Ticket numbers")])
    hcon = sqlite3.connect(":memory:")
    hcon.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY, tombstoned INTEGER NOT NULL DEFAULT 0)")
    t = {"thread_id": "t-x", "sources": [{"source": "notebook", "id": "Case review"}, {"source": "notebook", "id": "Ticket numbers"}],
         "links": [{"url": "note:work/Case review"}]}
    refs = topic.refs(t, ncon, hcon, visible=lambda rel: not rel.startswith("work/"))
    check("a note the predicate refuses is not a ref, by name or by note: url", "Case review" not in refs and "note:work/Case review" not in refs, refs)
    check("a note it allows is", refs.get("Ticket numbers") == {"note": "Reference/Ticket numbers.md"}, refs)


def part_fence():
    print("2. the home fence")
    base = tempfile.mkdtemp(prefix="helm-refs-home-")
    home = fixture.Site("home", base)
    home.build()
    home.start()
    try:
        code, out = api(home, "/api/triage/threads", {"threads": [{
            "thread_id": "t-refs-home", "title": "Two sources", "category": "Notebook", "urgency": "whenever", "state": "needs-you",
            "ask": {"kind": "ack", "prompt": "Read it", "requires": ["decision"], "options": [{"label": "Got it", "value": "ack", "effect": "Closes it"}]},
            "sources": [{"source": "notebook", "id": "Case review 00100002"}, {"source": "notebook", "id": "Ticket numbers"}]}]}, "PUT")
        code, r = api(home, "/api/thread/t-refs-home/refs")
        refs = r.get("refs", {})
        check("a home page is a ref", refs.get("Ticket numbers") == {"note": "Reference/Ticket numbers.md"}, (code, refs))
    finally:
        home.stop()


def part_drive(st):
    print("3. drive: the properties pane's refs")
    if os.environ.get("HELM_SKIP_DRIVES") == "1":
        check("the Playwright drive ran (HELM_SKIP_DRIVES=1 set: skipped deliberately)", False)
        return
    import shoot
    from playwright.sync_api import sync_playwright

    site = st.home
    note_href = "#/note/" + urllib.parse.quote(NOTE, safe="")
    with sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        ctx = browser.new_context(viewport={"width": 999, "height": 1392}, extra_http_headers={"X-Forwarded-User": fixture.USER},
                                  locale="en-US", timezone_id="America/New_York")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(site.web_url + "/#/threads/" + TID, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("#props-body .conv-prop-from a[href^='#/threads/']", timeout=20000)
            except Exception:
                pass
            rows = page.evaluate("""() => { const out = {};
                for (const r of document.querySelectorAll('#props-body .conv-prop-row')) {
                  const k = (r.querySelector('.conv-prop-label') || r.firstElementChild).textContent.trim().toLowerCase();
                  out[k] = {text: r.textContent, links: [...r.querySelectorAll('a')].map(a => [a.textContent, a.getAttribute('href')])};
                } return out; }""")
            frm, links, thread = rows.get("from", {}), rows.get("links", {}), rows.get("thread", {})
            fl = dict((t.replace("☉ ", ""), h) for t, h in frm.get("links", []))
            check("FROM links the minutes source to its note", fl.get("meetings Pine Hollow portal.md") == note_href, frm)
            check("FROM links the other thread's source to that thread", fl.get("helm " + OTHER) == "#/threads/" + OTHER, frm)
            check("FROM shows a bare string source's text", "a bare string source" in frm.get("text", ""), frm.get("text"))
            check("FROM leaves the case number and the thread itself as text",
                  "crm 00100002" in frm.get("text", "") and "crm 00100002" not in fl and ("helm " + TID) not in fl, frm)
            ll = dict((t.replace("☉ ", ""), h) for t, h in links.get("links", []))
            check("Links links the other thread", ll.get("Related") == "#/threads/" + OTHER, links)
            check("Links opens the note as it is held", ll.get("Portal page") == note_href, links)
            check("THREAD is the thread's own link", thread.get("links") == [[TID, "#/threads/" + TID]], thread)
            if fl.get("helm " + OTHER):
                page.click("#props-body .conv-prop-from a[href='#/threads/%s']" % OTHER)
                page.wait_for_function("document.querySelector('#view-conv .conv-title') && "
                                       "document.querySelector('#view-conv .conv-title').textContent.includes('Vendor certificate')", timeout=20000)
                check("the FROM thread link opens that conversation", page.evaluate("location.hash").startswith("#/threads/" + OTHER))
            # The notebook's own card for the thread (the note's topic page, #/topic/<note>) links its sources too.
            page.goto(site.web_url + "/" + note_href.replace("#/note/", "#/topic/"), wait_until="domcontentloaded")
            page.wait_for_selector("#main #t-%s" % TID, timeout=20000)
            card_links = page.evaluate("[...document.querySelectorAll('#main #t-%s .linkrow a')].map(a => a.getAttribute('href'))" % TID)
            check("the topic hub's card links the other thread in its links", "#/conv/" + OTHER in card_links, card_links)
            page.click("#main #t-%s .trow" % TID)    # the from line is drawn when the card is open
            try:
                page.wait_for_selector("#main #t-%s .src a[href='#/conv/%s']" % (TID, OTHER), timeout=10000)
            except Exception:
                pass
            src = page.evaluate("[...document.querySelectorAll('#main .src')].map(e => ({text: e.textContent, "
                                "links: [...e.querySelectorAll('a')].map(a => a.getAttribute('href'))}))")
            mine = next((x for x in src if "a bare string source" in x["text"]), {})
            check("the topic page's card links the other thread and the minutes note in its from line",
                  "#/conv/" + OTHER in mine.get("links", []) and note_href in mine.get("links", []), src)
            check("no page errors", not errors, errors[:3])
        finally:
            ctx.close()
            browser.close()


CARD = "t_a1b2c3d4"
PAGE = "Reference/Ticket numbers"
PROSE = ("Waiting on card %s and on thread %s. Notes are in [[%s]] and [[%s|the ticket list]]. "
         "Not these: t-shirt, t_12345678x and `%s [[%s]]` in code.\n"
         "```\n%s %s [[%s]]\n```" % (CARD, OTHER, PAGE, PAGE, CARD, PAGE, CARD, OTHER, PAGE))


def part_text(st_home, st_work):
    """A card id, a thread id and a notebook page named in prose are links (the app's text linker, web/app.js)."""
    print("4. drive: references in prose")
    if os.environ.get("HELM_SKIP_DRIVES") == "1":
        check("the Playwright drive ran (HELM_SKIP_DRIVES=1 set: skipped deliberately)", False)
        return
    import shoot
    from playwright.sync_api import sync_playwright
    sys.path.insert(0, HERE)
    from test_screens_drive import Drive
    note_href = "#/note/" + urllib.parse.quote(PAGE + ".md", safe="")
    anchors = """[...document.querySelectorAll('%s a.body-ref')].map(a => ({text: a.textContent, href: a.getAttribute('href')}))"""
    with sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        for name, site in (("home", st_home), ("work", st_work)):
            tid = "t-refs-prose"
            api(site, "/api/triage/threads", {"threads": [{
                "thread_id": tid, "title": "Prose with references", "summary": "References in prose", "category": "Refs", "urgency": "whenever",
                "state": "needs-you", "responder": "sweep:refs",
                "ask": {"kind": "ack", "prompt": "Read it", "requires": ["decision"], "options": [{"label": "Got it", "value": "ack", "effect": "Closes the card"}]},
                "events": [{"ts": 1700000000, "source": "sweep:refs", "kind": "bot-result", "body": PROSE}]}]}, "PUT")
            d = Drive(browser, site, (999, 1392), "#/threads/" + tid)
            d.wait(".conv-timeline .conv-msg")
            d.until("document.querySelector('.conv-timeline').textContent.includes('Waiting on card')")
            # The thread list loads apart from the conversation: a thread id is a link once it is known.
            d.until("[...document.querySelectorAll('.conv-timeline a.body-ref')].some(a => a.getAttribute('href') === '#/threads/%s')" % OTHER, 45000)
            got = d.eval(anchors % ".conv-timeline")
            by = {a["href"]: a for a in got}
            check(name + ": a card id stays text (the app holds no board)",
                  "#/board/" + CARD not in by and CARD in d.eval("document.querySelector('.conv-timeline').textContent"), got)
            check(name + ": a thread id he has is a link to its conversation", by.get("#/threads/" + OTHER, {}).get("text") == OTHER or any(a["text"] == OTHER for a in got), got)
            check(name + ": [[Page]] opens the notebook page", any(a["text"] == PAGE and a["href"] == note_href for a in got), got)
            check(name + ": [[Page|label]] reads as its label and opens the same page", any(a["text"] == "the ticket list" and a["href"] == note_href for a in got), got)
            check(name + ": an unknown t-word and a longer id stay text", all(a["text"] not in ("t-shirt", "t_12345678x") for a in got), got)
            in_code = d.eval("[...document.querySelectorAll('.conv-timeline code, .conv-timeline pre')].map(c => c.querySelectorAll('a').length)")
            check(name + ": nothing inside `code` or a code block is linked, and the code is still there",
                  in_code and sum(in_code) == 0 and len(in_code) >= 2, in_code)
            check(name + ": no page errors", not d.errors, d.errors)
            d.close()
        browser.close()


def main():
    with fixture.Stack(("home",)) as st:
        part_api(st)
        part_drive(st)
    with fixture.Stack(("home",)) as st2:
        part_text(st2.home, st2.home)
    part_visible()
    part_fence()
    print("%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
