"""Playwright drives of the three-pane app, part three.

Run: python3 tests/test_screens_keys.py

Every retained helm-web key still does its job in the three-pane app (? help, / search,
N file a card, E and C expand and collapse, F answers first, Ctrl+O the switcher, Ctrl+D
today's note, Enter and Ctrl+Enter send, Ctrl/Cmd+Enter and Enter in the title file a new card
on both sites); retired T is inert. A draft and a fold survive reloads, the mutating buttons carry
the submit gate, a lit card takes attachments, and the fixed design draws. Home site (the new-card
keys on both sites), synthetic data (tools/screens/fixture.py).
"""
from __future__ import annotations

import os
import sys

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

import fixture  # noqa: E402
import shoot  # noqa: E402
from test_screens_drive import HEADERS, PHONE, WIDE, Drive  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def key(d, k):
    """A key pressed with nothing focused (keys never fire while typing)."""
    d.eval("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    d.page.keyboard.press(k)
    d.page.wait_for_timeout(250)


def until(d, js, timeout=8000):
    try:
        d.page.wait_for_function(js, timeout=timeout)
        return True
    except Exception:
        return False


MONITOR_CARD = "t-monitor-dedupe"


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        print("\nL10 · the keys")
        d = Drive(browser, st.home, WIDE, "#/threads/" + MONITOR_CARD)
        d.wait(".conv-timeline .conv-msg")
        was = d.eval("({theme: document.documentElement.dataset.theme, hash: location.hash, view: document.body.dataset.view, dialogs: document.querySelectorAll('.th-pop, .shell-help-overlay, #sheet:not(.hidden)').length})")
        key(d, "t")
        now = d.eval("({theme: document.documentElement.dataset.theme, hash: location.hash, view: document.body.dataset.view, dialogs: document.querySelectorAll('.th-pop, .shell-help-overlay, #sheet:not(.hidden)').length})")
        check("T is inert: no theme picker, navigation or other dialog", now == was and now["theme"] == "helm", (was, now))
        check("T raises no page error", not d.errors, d.errors)
        key(d, "?")
        check("? opens the keys help", until(d, "!!document.querySelector('.shell-help-overlay')"))
        d.page.keyboard.press("Escape")
        check("Escape closes it", until(d, "!document.querySelector('.shell-help-overlay')"))
        key(d, "/")
        # Helm stage 2: "/" opens the command palette (its input is the search box now); Esc closes it and the keys below carry on.
        check("/ opens the command palette with its search input focused", until(d, "Helm.palette && Helm.palette.isOpen()") and d.eval("document.activeElement && document.activeElement.id") == "pal-q")
        d.page.keyboard.press("Escape")
        check("Esc closes the palette", until(d, "!Helm.palette.isOpen()"))
        key(d, "n")
        check("N opens file-a-card", until(d, "!document.querySelector('#sheet').classList.contains('hidden')"))
        check("on a wide screen with a mouse the title takes focus", d.eval("document.activeElement && document.activeElement.id") == "c-title",
              d.eval("document.activeElement && document.activeElement.id"))
        d.page.click("#c-cancel")
        until(d, "document.querySelector('#sheet').classList.contains('hidden')")
        key(d, "c")
        check("C collapses every fold in the conversation", d.eval("[...document.querySelectorAll('#view-conv details')].every(x => !x.open)"))
        key(d, "e")
        check("E expands them", d.eval("[...document.querySelectorAll('#view-conv details')].every(x => x.open)"))

        print("\nL10 · drafts survive a reload")
        d.page.fill(".conv-note", "a draft that must survive the reload")
        d.page.wait_for_timeout(300)
        d.page.reload(wait_until="domcontentloaded")
        d.wait(".conv-timeline .conv-msg")
        d.page.wait_for_timeout(500)
        check("the draft survived the reload", d.eval("document.querySelector('.conv-note').value") == "a draft that must survive the reload",
              d.eval("document.querySelector('.conv-note').value"))
        d.close()

        # helm-web dropped the drafts and folds of threads no list held on every
        # read; the new web kept them forever, so helm.drafts and helm.fold only ever grew.
        print("\nL10 · drafts and folds of threads no list holds are dropped")
        seed = ("(() => { localStorage.setItem('helm.drafts', JSON.stringify({'t-gone:note': 'orphan', 't-portal-name:note': 'live',"
                " 't-parks-retry:note': 'done, on THREADS', 'c-title': 'the sheet', 'frontdesk:x': 'front desk'}));"
                "localStorage.setItem('helm.fold', JSON.stringify({'t-gone|run:1': 1, 't-monitor-dedupe|run:1': 1})) })()")
        drafts_js = "JSON.parse(localStorage.getItem('helm.drafts') || '{}')"
        folds_js = "JSON.parse(localStorage.getItem('helm.fold') || '{}')"
        d = Drive(browser, st.home, WIDE, "#/threads")
        d.wait("#list-threads .lrow")
        d.eval(seed)
        d.page.reload(wait_until="domcontentloaded")
        d.wait("#list-threads .lrow[data-id='t-parks-retry']")
        key(d, "r")
        gone = until(d, "!('t-gone:note' in %s)" % drafts_js)
        check("a draft for a thread no list holds is dropped on a read", gone, d.eval(drafts_js))
        check("its fold is dropped too", "t-gone|run:1" not in d.eval(folds_js), d.eval(folds_js))
        kept = d.eval(drafts_js)
        check("a live thread's draft is kept", kept.get("t-portal-name:note") == "live", kept)
        check("a done thread's draft is kept while THREADS lists it", kept.get("t-parks-retry:note") == "done, on THREADS", kept)
        check("drafts that are not a thread's are kept", kept.get("c-title") == "the sheet" and kept.get("frontdesk:x") == "front desk", kept)
        check("a live thread's fold is kept", d.eval(folds_js).get("t-monitor-dedupe|run:1") == 1, d.eval(folds_js))
        check("no page errors", not d.errors, d.errors)
        d.close()
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you .lrow")
        d.eval(seed)
        d.page.reload(wait_until="domcontentloaded")
        d.wait("#list-needs-you .lrow")
        key(d, "r")
        gone = until(d, "!('t-parks-retry:note' in %s)" % drafts_js)
        check("a done thread's draft is dropped once no list holds it", gone, d.eval(drafts_js))
        check("the live thread's draft is still kept", d.eval(drafts_js).get("t-portal-name:note") == "live", d.eval(drafts_js))
        check("no page errors", not d.errors, d.errors)
        d.close()
        d = Drive(browser, st.home, WIDE, "#/threads/" + MONITOR_CARD)
        d.wait(".conv-timeline .conv-msg")

        print("\nL10 · Enter and Ctrl+Enter send")
        d.page.fill(".conv-note", "Sent with Enter")
        d.page.focus(".conv-note")
        d.page.keyboard.press("Enter")
        check("Enter sends the note", until(d, "document.querySelector('.conv-timeline').textContent.includes('Sent with Enter')", 15000))
        check("the note box is empty after the send", until(d, "document.querySelector('.conv-note').value === ''", 15000))
        # a tap dims the composer until its re-read lands (): type the next note after that
        until(d, "!document.querySelector('.conv-foot').classList.contains('busy')", 15000)
        d.page.fill(".conv-note", "line one")
        d.page.focus(".conv-note")
        d.page.keyboard.press("Shift+Enter")
        d.page.keyboard.type("line two")
        check("Shift+Enter is a new line, not a send", d.eval("document.querySelector('.conv-note').value") == "line one\nline two")
        d.page.keyboard.press("Control+Enter")
        check("Ctrl+Enter sends", until(d, "document.querySelector('.conv-timeline').textContent.includes('line two')", 15000))
        check("the send button carries the submit gate", d.eval("document.querySelector('.conv-send').hasAttribute('data-submit')"))
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\nL10 · F, Ctrl+O, Ctrl+D; a lit card's gate and attachments")
        d = Drive(browser, st.home, WIDE, "#/threads")
        d.wait("#list-threads .lrow")
        key(d, "f")
        check("F goes to answers first: NEEDS YOU, first ask open", until(d, "location.hash.startsWith('#/needs-you/')"), d.hash())
        d.wait(".conv-live-ask .conv-opt")
        check("every option button carries the submit gate", d.eval("[...document.querySelectorAll('.conv-live-ask .conv-opt')].every(b => b.hasAttribute('data-submit'))"))
        check("a lit card takes attachments", d.eval("!!document.querySelector('.conv-attach') && !document.querySelector('.conv-attach').classList.contains('hidden')"))
        key(d, "Control+o")
        check("Ctrl+O opens the switcher", until(d, "!!document.querySelector('.nb-overlay #sw-q')"))
        d.page.keyboard.press("Escape")
        until(d, "!document.querySelector('.nb-overlay')")
        key(d, "Control+d")
        check("Ctrl+D opens today's note", until(d, "location.hash.startsWith('#/day/')"), d.hash())
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\nL10 · the fixed design draws the three panes")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait(".conv-live-ask .conv-opt")
        check("the fixed Helm identity is on", d.eval("document.documentElement.dataset.theme") == "helm")
        check("three panes on screen", all(d.eval("(() => { const r = document.querySelector('%s').getBoundingClientRect(); return r.width > 150 && r.height > 600; })()" % s)
                                           for s in ("#pane-left", "#pane-main", "#pane-props")))
        check("no retired theme or sound controls", d.eval("!document.querySelector('#btn-theme, #btn-sound')"))
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\nL9 · the phone (412x915): the pages stack and fit")
        fits = "document.documentElement.scrollWidth <= 413"
        d = Drive(browser, st.home, PHONE, "#/activity")
        d.wait("#list-activity .lrow")
        check("ACTIVITY: the list is the pane on a phone", until(d, "document.body.dataset.pane === 'left'"))
        d.page.click("#list-activity .act-sumlink")
        check("ACTIVITY: Summary opens the page (heartbeat, quiet runs)", until(d, "document.body.dataset.pane === 'main' && !!document.querySelector('#view-activity .act-quiet')"), d.hash())
        d.page.click("#shell-back")
        check("ACTIVITY: back returns to the list", until(d, "document.body.dataset.pane === 'left' && location.hash === '#/activity'"), d.hash())
        check("ACTIVITY: no sideways scroll", d.eval(fits), d.eval("document.documentElement.scrollWidth"))
        d.page.click("#list-activity .lrow >> nth=0")
        check("ACTIVITY: a row opens its conversation as the next pane", until(d, "document.body.dataset.pane === 'main' && location.hash.startsWith('#/activity/')"), d.hash())
        d.close()
        d = Drive(browser, st.home, PHONE, "#/threads")
        d.wait("#list-threads .lrow")
        check("THREADS: filters and rows fit the phone", d.eval(fits), d.eval("document.documentElement.scrollWidth"))
        d.close()

        # the owner 2026-09-09, "I can't select the timing or folder anymore": the sheet focused
        # the title, the phone keyboard opened over the pickers. helm-web v13 stopped focusing it.
        print("\nL9 · the phone: file-a-card leaves the keyboard shut")
        d = Drive(browser, st.home, PHONE, "#/threads")
        d.wait("#list-threads .lrow")
        d.page.click("#add")
        check("+ opens file-a-card on the phone", until(d, "!document.querySelector('#sheet').classList.contains('hidden')"))
        d.page.wait_for_timeout(300)
        check("the title does not take focus on the phone", d.eval("(document.activeElement && document.activeElement.id) !== 'c-title'"),
              d.eval("document.activeElement && document.activeElement.id"))
        d.page.click("#c-cancel")
        d.close()
        tctx = browser.new_context(viewport={"width": 999, "height": 1392}, has_touch=True, is_mobile=True,
                                   extra_http_headers=HEADERS)
        tp = tctx.new_page()
        tp.goto(st.home.web_url + "/#/threads", wait_until="domcontentloaded")
        tp.wait_for_selector("#list-threads .lrow", timeout=20000)
        coarse = tp.evaluate("matchMedia('(pointer: coarse)').matches")
        tp.click("#add")
        tp.wait_for_timeout(300)
        check("a wide touch screen (pointer: coarse) leaves the title unfocused too",
              coarse and tp.evaluate("(document.activeElement && document.activeElement.id) !== 'c-title'"),
              (coarse, tp.evaluate("document.activeElement && document.activeElement.id")))
        tctx.close()

        # / the earlier app and the reference app filed the new-card form on Ctrl/Cmd+Enter from any
        # field and on Enter in the title; the three-pane sheet had no key handler, so neither filed.
        for site_name in ("home",):
            print("\nL10 · %s: the new-card sheet files from the keyboard" % site_name)
            d = Drive(browser, st.sites[site_name], WIDE, "#/threads")
            d.wait("#list-threads .lrow")
            posts = []
            d.page.on("request", lambda r: posts.append(r.post_data or "")
                      if r.method == "POST" and r.url.endswith("/api/triage/threads/create") else None)
            shut = "document.querySelector('#sheet').classList.contains('hidden')"

            def open_sheet():
                d.page.wait_for_timeout(800)   # past the submit gate's 700 ms window from the last filing
                if not d.eval(shut):           # a key that did not file left the sheet up: shut it, go on
                    d.page.click("#c-cancel")
                d.page.click("#add")
                until(d, "!" + shut)

            def filed(title, n):
                gone = until(d, shut, 15000)
                d.page.wait_for_timeout(400)
                return gone and len(posts) == n and title in posts[-1]

            open_sheet()
            d.page.fill("#c-title", "")
            d.page.focus("#c-title")
            d.page.keyboard.press("Enter")
            d.page.wait_for_timeout(500)
            check("%s: Enter in an empty title files nothing and keeps the sheet" % site_name,
                  not posts and not d.eval(shut), (posts, d.eval(shut)))
            d.page.wait_for_timeout(400)   # that press took the gate for 700 ms; typing a title takes longer
            d.page.fill("#c-title", "Filed with Enter in the title %s" % site_name)
            d.page.focus("#c-title")
            d.page.keyboard.press("Enter")
            check("%s: Enter in the title files the card, once" % site_name,
                  filed("Filed with Enter in the title", 1), posts)

            open_sheet()
            d.page.fill("#c-title", "Filed with Ctrl+Enter %s" % site_name)
            d.page.fill("#c-details", "line one")
            d.page.focus("#c-details")
            d.page.keyboard.press("End")
            d.page.keyboard.press("Enter")
            d.page.keyboard.type("line two")
            d.page.wait_for_timeout(300)
            check("%s: Enter in the details is a new line, not a filing" % site_name,
                  d.eval("document.querySelector('#c-details').value") == "line one\nline two" and len(posts) == 1 and not d.eval(shut),
                  (d.eval("document.querySelector('#c-details').value"), posts))
            d.page.keyboard.press("Control+Enter")
            d.page.keyboard.press("Control+Enter")
            check("%s: Ctrl+Enter in the details files the card, once for two quick presses" % site_name,
                  filed("Filed with Ctrl+Enter", 2), posts)

            open_sheet()
            d.page.fill("#c-title", "Filed with Cmd+Enter %s" % site_name)
            other = "#c-link" if site_name == "work" else "#c-details"
            d.page.focus(other)
            d.page.keyboard.press("Meta+Enter")
            check("%s: Cmd+Enter from another field (%s) files the card, once" % (site_name, other),
                  filed("Filed with Cmd+Enter", 3), posts)
            check("%s: the File it button still carries the submit gate" % site_name, d.eval("document.querySelector('#c-file').hasAttribute('data-submit')"))
            check("%s: no page errors" % site_name, not d.errors, d.errors)
            d.close()

        browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a crashed drive is a failed drive, never a quiet exit
        import traceback
        traceback.print_exc()
        check("the drives ran to the end (%s)" % type(e).__name__, False, e)
    print(f"{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
