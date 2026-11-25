"""A thread's link can name its note by the folder the notebook used to live in.

the owner 2026-09-26, on the reference app: "I can't click the thread link that is in my notebooks. That was
sorta the whole point of combining them was that this was all integrated."

the reference's cards cite their minutes by the client path (C:\\...\\Daily\\Calls\\<minutes>.md): the
vault was that folder, so the path was inside it and the link opened the note. At the cutover
a second notebook moved to the server's own folder, and every such link stopped matching: the
conversation's Links row drew it as plain text, and the note's topic hub never found the thread
(a minutes note is not a topic note, so a link is its only join). HELM_NOTES_VAULT_ALIASES names
the old folder(s), `;`-separated; a link under one of them is a link to the note.

Run: PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/test_note_links.py

  1. topic.note_link_target maps a path under an alias (either slash, any case) to the note; a
     path elsewhere, or no alias set, stays unmatched; the vault itself still maps.
  2. the store's join both ways: threads_citing finds a backslash link, and topic.match calls
     it linked.
  3. drive, a second site with the alias set: /api/notes/meta names the aliases; on a deep link
     whose properties draw before the folders arrive (the shell's meta is held back), the
     conversation's Links row becomes a link to the note once they do; a path elsewhere stays
     text; the link opens the note; and the note's topic hub lists the thread.

Playwright and Chromium are required for part 3 (CI installs them); HELM_SKIP_DRIVES=1 skips it
and reports one failed check, so a skipped run never reads as green.
"""
# temp-ok: the stores under test are fixtures this module creates and deletes
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "screens"))

OLD_WIN = "C:\\Users\\someone\\Notes Vault"          # where a fictional client kept the vault
OLD_POSIX = "/home/someone/old-vault/"
ALIASES = OLD_WIN + "\\;" + OLD_POSIX                 # a trailing separator on either is fine

TMP = tempfile.mkdtemp(prefix="helm_test_note_links_")
os.environ["HELM_NOTES_PROFILE"] = "fixed"
os.environ["HELM_NOTES_MODE"] = "store"
os.environ["HELM_NOTES_ROOT"] = os.path.join(TMP, "vault")
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_NOTES_VAULT_ALIASES"] = ALIASES

from notebook import helm_adapter, notes, topic  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail != "" else ""))


MINUTES = "Daily/Meetings/2026-09-23 1332 portal call.md"


def part_targets():
    print("1. a link under an old vault folder names the note")
    t = topic.note_link_target
    win = OLD_WIN + "\\" + MINUTES.replace("/", "\\")
    check("a backslash path under the old client folder maps to the note", t(win) == MINUTES, t(win))
    check("the old folder matches in any case", (t(win.upper()) or "").lower() == MINUTES.lower(), t(win.upper()))
    check("forward slashes under the old folder map too", t(win.replace("\\", "/")) == MINUTES, t(win.replace("\\", "/")))
    check("a second alias (a POSIX folder, given with a trailing slash) maps", t(OLD_POSIX + "Contacts/Pine Hollow.md") == "Contacts/Pine Hollow.md",
          t(OLD_POSIX + "Contacts/Pine Hollow.md"))
    check("the vault itself still maps", t(os.path.join(notes.VAULT, "Contacts", "Pine Hollow.md")) == "Contacts/Pine Hollow.md",
          t(os.path.join(notes.VAULT, "Contacts", "Pine Hollow.md")))
    check("a note: url still maps", t("note:Contacts/Pine Hollow") == "Contacts/Pine Hollow.md")
    check("a path elsewhere on the client is not a note", t("C:\\Users\\someone\\Desktop\\notes.md") is None)
    check("a folder that only starts like the alias is not inside it", t(OLD_WIN + " Backup\\" + MINUTES.replace("/", "\\")) is None)
    saved = os.environ.pop("HELM_NOTES_VAULT_ALIASES")
    try:
        check("with no alias set, the client path is not a note", t(win) is None, t(win))
    finally:
        os.environ["HELM_NOTES_VAULT_ALIASES"] = saved


def part_join():
    print("2. the join both ways in the store")
    win = OLD_WIN + "\\" + MINUTES.replace("/", "\\")
    c = helm_adapter.new_thread({"title": "Send the portal brief", "category": "Calls",
                                 "links": [{"label": "Minutes", "url": win}]})
    helm_adapter.new_thread({"title": "An unrelated card", "links": [{"label": "Log", "url": "C:\\Users\\someone\\Desktop\\x.md"}]})
    hcon = helm_adapter.store()
    try:
        citing = [x["thread_id"] for x in helm_adapter.threads_citing(hcon, MINUTES)]
        check("threads_citing finds the thread whose link is the client path", citing == [c["thread_id"]], citing)
        got = helm_adapter.store_threads(hcon, with_events=False)
        mine = next(x for x in got if x["thread_id"] == c["thread_id"])
        m = topic.match(mine, topic.keys_for(MINUTES, "portal call"))
        check("the topic hub calls that thread linked, not suggested", bool(m) and m[0] == "linked", m)
    finally:
        hcon.close()


def api(site, path, body=None, method=None):
    import fixture
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(site.api_url + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Authorization": "Bearer " + fixture.TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


def part_drive():
    print("3. drive: a second site, the Links row and the topic hub")
    if os.environ.get("HELM_SKIP_DRIVES") == "1":
        check("the Playwright drive ran (HELM_SKIP_DRIVES=1 set: skipped deliberately)", False)
        return
    import fixture
    import shoot
    from playwright.sync_api import sync_playwright

    note = "Contacts/Pine Hollow portal.md"            # a fixture note (tools/screens/fixture.py)
    href = "#/note/" + urllib.parse.quote(note, safe="")
    tid = "t-minutes-client-link"
    elsewhere = "C:\\Users\\someone\\Desktop\\call audio.m4a"
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        site = st.home
        meta = api(site, "/api/notes/meta")
        check("/api/notes/meta names the old folders", meta.get("vault_aliases") == [OLD_WIN + "\\", OLD_POSIX], meta.get("vault_aliases"))
        out = api(site, "/api/triage/threads", {"threads": [{
            "thread_id": tid, "title": "Send the portal brief to the board", "summary": "An action item from the call's minutes.",
            "category": "Calls", "urgency": "whenever", "state": "needs-you", "responder": "agent",
            "ask": {"kind": "ack", "prompt": "Read it and close it", "requires": ["decision"],
                    "options": [{"label": "Got it", "value": "ack", "effect": "Closes the card"}]},
            "links": [{"label": "Minutes", "url": OLD_WIN + "\\" + note.replace("/", "\\")},
                      {"label": "Audio", "url": elsewhere}],
            "sources": [{"source": "meetings", "id": "portal call.md"}]}]}, "PUT")
        check("the thread is filed", out.get("created") == 1, out)
        hub = api(site, "/api/topic?rel=" + urllib.parse.quote(note))
        linked = [t["thread_id"] for t in hub.get("open", []) if t.get("match") == "linked"]
        check("the note's topic hub lists the thread as linked", tid in linked, [(t["thread_id"], t.get("match")) for t in hub.get("open", [])])

        browser = p.chromium.launch(**shoot.launch_args())
        ctx = browser.new_context(viewport={"width": 999, "height": 1392}, extra_http_headers={"X-Forwarded-User": fixture.USER},
                                  locale="en-US", timezone_id="America/New_York")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            # A deep link can draw the properties before the shell has the notebook's folders. The
            # shell's /api/notes/meta is held until they have drawn, so the redraw is what is judged.
            # (A route handler runs only while Python is inside a Playwright call: never time.sleep.)
            # The thread's refs (tests/test_in_app_refs.py) name the note too, so they are held
            # for the whole check: the folders' arrival alone must turn the row into a link.
            held, held_refs = [], []
            page.route("**/api/notes/meta", lambda route: held.append(route))
            page.route("**/api/thread/*/refs", lambda route: held_refs.append(route))
            page.goto(site.web_url + "/#/threads/" + tid, wait_until="domcontentloaded")
            page.wait_for_selector("#props-body .conv-prop-link-item", timeout=20000)
            # Let the conversation's own reads (attachments, the stream's first frame) land and
            # redraw first, so what draws the link after the release is the folders' arrival.
            page.wait_for_timeout(2500)
            check("the properties drew while the notebook's folders were held back",
                  len(held) == 1 and page.query_selector("#props-body a.conv-prop-link-a[href^='#/note/']") is None, len(held))
            for r in held:
                r.continue_()
            page.unroute("**/api/notes/meta")
            try:   # well inside the 30 s poll, the only other redraw left
                page.wait_for_selector("#props-body a.conv-prop-link-a[href^='#/note/']", timeout=5000)
            except Exception:
                pass
            links = page.evaluate("[...document.querySelectorAll('#props-body .conv-prop-link-item')].map(i => "
                                  "({text: i.textContent, href: i.querySelector('a') ? i.querySelector('a').getAttribute('href') : null}))")
            for r in held_refs:
                r.continue_()
            page.unroute("**/api/thread/*/refs")
            minutes = next((x for x in links if "Minutes" in x["text"]), {})
            check("the Minutes link in the properties is a link to the note", minutes.get("href") == href, links)
            audio = next((x for x in links if "Audio" in x["text"]), {})
            check("a client path outside the vault stays text, shown whole", audio.get("href") is None and elsewhere in audio.get("text", ""), links)
            if minutes.get("href") == href:
                page.click("#props-body a.conv-prop-link-a[href^='#/note/']")
                page.wait_for_selector("#main .md", timeout=20000)
                h = page.evaluate("location.hash")
                check("the link opens the note", h == href, h)
                page.wait_for_selector("#props-body a[href='#/conv/%s']" % tid, timeout=20000)
                check("the note's Connected pane links back to the thread", page.query_selector("#props-body a[href='#/conv/%s']" % tid) is not None)
            check("no page errors", not errors, errors[:3])
        finally:
            ctx.close()
            browser.close()


def main():
    try:
        part_targets()
        part_join()
        part_drive()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
