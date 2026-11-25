"""Today (#/home) and the command palette (Helm redesign, stage 2).

Run: python3 tests/test_screens_home.py

The site with a writable notebook (HELM_NOTES_MODE=folder): an empty address lands on #/home on every site, the brand link goes
there, the page draws the greeting, the one-sentence summary, the line box, the ship's log (scheduled jobs dashed above the brass Now line,
entries below) and the right column (Waiting on you, Running now, Coming up, Recently touched). The first ask is answered in place through
the conversation's own path (the option buttons carry the submit gate). A line written in the box lands on today's journal page and shows in
the log, the field keeps what he typed while the page redraws. Read-only notes hide the box. Work (the reference app) draws the same page without the
The palette opens on Ctrl+K, "/" and a click on the top search box, groups its entries,
never lists a private card or reads a private profile's sessions, moves with the arrows, opens with Enter, closes on Esc (and gives the focus
back); the old search page and Ctrl+O keep working. Phone and wide screens.
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
WIDE_DESK = (1440, 900)


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def until(d, js, timeout=8000):
    return d.until(js, timeout)


def key(d, k):
    d.eval("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    d.page.keyboard.press(k)
    d.page.wait_for_timeout(250)


def pal_items(d):
    return d.eval("Helm.palette.items()")


def main():
    with fixture.Stack(("home",), notes_modes={"home": "folder"}) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())

        print("\nToday: the empty address, the brand, the page (home, wide)")
        d = Drive(browser, st.home, WIDE, "")
        d.wait("#view-home .hm-home .hm-home-hi")
        check("an empty address lands on #/home", d.hash() == "#/home", d.hash())
        check("the body says it is Today", d.eval("document.body.dataset.view") == "home" and d.eval("document.body.dataset.main") == "home")
        check("the rail's Today row is the open one", d.eval("document.querySelector('#shell-rail .nav-sec.on').dataset.sec") == "home")
        check("the brand link goes to Today", d.eval("document.querySelector('.shell-brand').getAttribute('href')") == "#/home")
        check("a greeting with a full stop", d.eval("/^(Good (morning|afternoon|evening)|Late night)/.test(document.querySelector('.hm-home-hi').textContent)"), d.eval("document.querySelector('.hm-home-hi').textContent"))
        check("the date line names the weekday and the time", d.eval("/\\d{1,2}:\\d{2}/.test(document.querySelector('#home-date').textContent)"))
        check("one sentence says how many things wait", d.eval("/waiting on you/.test(document.querySelector('.hm-home-sum').textContent)"), d.eval("document.querySelector('.hm-home-sum').textContent"))
        check("no ALL-CAPS label and no arrow in the page copy", d.eval("!/→|\\b[A-Z]{4,}\\b/.test(document.querySelector('#view-home').innerText.replace(/CANARY-PRIVATE-7f3a/g, ''))"),
              d.eval("document.querySelector('#view-home').innerText.match(/→|\\b[A-Z]{4,}\\b/)"))

        print("\nToday: the ship's log")
        d.wait("#home-log .hm-log-now")
        until(d, "document.querySelector('#home-log .hm-log-entry.fut') && document.querySelector('#home-log .hm-log-entry a.hm-ochip[href^=\\'#/threads/\\'], #home-log .hm-log-entry a.hm-ochip[href^=\\'#/needs-you/\\']')")
        order = d.eval("[...document.querySelectorAll('#home-log > li')].map(l => l.classList.contains('hm-log-now') ? 'now' : l.classList.contains('fut') ? 'fut' : 'past')")
        now_at = order.index("now") if "now" in order else -1
        check("a brass Now line sits in the log", now_at >= 0, order)
        check("every entry has a time in the left gutter and a glyph node",
              d.eval("[...document.querySelectorAll('#home-log .hm-log-entry')].every(e => /\\d/.test(e.querySelector('.hm-log-t').textContent) && e.querySelector('.hm-log-nd svg'))"))
        check("entries link to what they are (a thread chip opens its card)", d.eval("!!document.querySelector('#home-log .hm-log-entry a.hm-ochip[href^=\"#/needs-you/\"], #home-log .hm-log-entry a.hm-ochip[href^=\"#/threads/\"]')"))

        print("\nToday: the right column")
        d.wait("#home-wait .hm-home-ask")
        asks = d.eval("needsYou().map(t => t.title)")
        check("Waiting on you shows the first ask, in full, with its options",
              d.eval("document.querySelector('#home-wait .hm-home-ask-t').textContent") == asks[0] and d.eval("document.querySelectorAll('#home-wait .hm-home-opt').length") >= 2,
              (asks, d.eval("document.querySelector('#home-wait .hm-home-ask-t').textContent")))
        check("the other asks are rows", d.eval("document.querySelectorAll('#home-wait .hm-home-row').length") >= 1)
        check("every option button carries the submit gate", d.eval("[...document.querySelectorAll('#home-wait .hm-home-opt')].every(b => b.hasAttribute('data-submit'))"))
        check("Coming up and Recently touched are drawn", d.eval("!!document.querySelector('#home-soon .hm-row') && !!document.querySelector('#home-touched')"))
        check("the rail still stands", d.eval("!!document.querySelector('#shell-rail')"))

        print("\nToday: the line box writes to today's journal and keeps what he typed")
        d.wait("#home-line .hm-home-in")
        check("the box is shown where the notebook is writable", d.eval("!!document.querySelector('#home-line')"))
        d.page.fill(".hm-home-in", "A line for the log")
        d.eval("Helm.emit('threads')")   # a redraw while he types: the field must survive
        d.page.wait_for_timeout(300)
        check("a redraw does not take the typed text away", d.eval("document.querySelector('.hm-home-in').value") == "A line for the log", d.eval("document.querySelector('.hm-home-in').value"))
        d.page.click(".hm-home-add")
        check("the line appears in the log as his own writing", until(d, "[...document.querySelectorAll('#home-log .hm-log-read')].some(n => n.textContent.includes('A line for the log'))"), d.eval("document.querySelector('#home-log').innerText.slice(0, 400)"))
        check("the box is empty again", d.eval("document.querySelector('.hm-home-in').value") == "")
        html = d.page.evaluate("async () => { const x = new Date(); const day = x.getFullYear() + '-' + String(x.getMonth() + 1).padStart(2, '0') + '-' + String(x.getDate()).padStart(2, '0'); return (await (await fetch('/api/day?date=' + day)).json()).html; }")
        check("today's journal page holds it", "A line for the log" in html, html[:200])

        print("\nToday: the first ask is answered in place")
        first = d.eval("document.querySelector('#home-wait .hm-home-ask').dataset.id")
        d.page.click("#home-wait .hm-home-ask .hm-home-opt:last-child")
        check("the card moves on after the answer (a reply went through the conversation's path)",
              until(d, "!document.querySelector('#home-wait .hm-home-ask') || document.querySelector('#home-wait .hm-home-ask').dataset.id !== %r" % first, 12000), first)
        check("the thread left the needs-you set", until(d, "!needsYou().some(t => t.thread_id === %r)" % first, 8000))
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\nToday: phone")
        d = Drive(browser, st.home, PHONE, "")
        d.wait("#view-home .hm-home .hm-home-hi")
        check("phone: lands on #/home with the tab bar's Today tab on", d.hash() == "#/home" and d.eval("document.querySelector('#tabbar a.tab.on').dataset.tab") == "home", d.hash())
        d.page.wait_for_timeout(600)
        check("phone: no horizontal scroll", d.eval("document.documentElement.scrollWidth <= innerWidth + 1"), d.eval("document.documentElement.scrollWidth"))
        check("phone: Waiting on you comes before the log", d.eval("document.querySelector('#home-wait').getBoundingClientRect().top < document.querySelector('#home-log').getBoundingClientRect().top"))
        check("phone: the option buttons are at least 44 px", d.eval("[...document.querySelectorAll('#home-wait .hm-home-opt')].every(b => b.getBoundingClientRect().height >= 44)"))
        check("phone: the line box input is 16 px", d.eval("getComputedStyle(document.querySelector('.hm-home-in')).fontSize") == "16px")
        d.page.click("#q")
        check("phone: a tap on the search box opens the palette, Close on top", until(d, "Helm.palette.isOpen()") and d.eval("getComputedStyle(document.querySelector('.hm-pal-close')).display") != "none")
        pr = d.eval("(() => { const c = document.querySelector('.hm-pal-close').getBoundingClientRect(), i = document.querySelector('#pal-q').getBoundingClientRect(); return [c.top, i.top]; })()")
        check("phone: Close is above the list and the input at the bottom", pr[0] < pr[1], pr)
        d.page.click(".hm-pal-close")
        check("phone: Close closes it", until(d, "!Helm.palette.isOpen()"))
        check("no page errors", not d.errors, d.errors)
        d.close()

        print("\nThe palette (wide)")
        d = Drive(browser, st.home, WIDE_DESK, "#/home")
        d.wait("#view-home .hm-home .hm-home-hi")
        reqs = []
        d.page.on("request", lambda r: reqs.append(r.url))
        d.page.click(".hm-home-in")
        d.page.keyboard.press("Control+k")
        check("Ctrl+K opens the palette from a field too, the input has the focus", until(d, "Helm.palette.isOpen()") and d.eval("document.activeElement.id") == "pal-q")
        until(d, "Helm.palette.items().some(i => i.group === 'Notes and threads')")
        names = d.eval("[...document.querySelectorAll('#hm-pal .hm-pal-group')].map(g => g.textContent)")
        check("it groups Actions, Notes and threads, Sections and Manage", all(g in names for g in ("Actions", "Notes and threads", "Sections")), names)
        d.page.keyboard.press("Escape")
        check("Esc closes it and gives the focus back", until(d, "!Helm.palette.isOpen()") and d.eval("document.activeElement.classList.contains('hm-home-in')"), d.eval("document.activeElement.className"))
        d.eval("document.activeElement.blur()")
        d.page.keyboard.press("/")
        check("/ opens it", until(d, "Helm.palette.isOpen()") and d.eval("document.activeElement.id") == "pal-q")
        d.page.keyboard.press("Escape")
        until(d, "!Helm.palette.isOpen()")
        d.page.click("#q")
        check("a click on the top search box opens it", until(d, "Helm.palette.isOpen()"))
        items = pal_items(d)
        d.page.keyboard.type("Media box")
        check("typing finds the thread by its title", until(d, "Helm.palette.items().some(i => i.group === 'Notes and threads' && /Media box disk/.test(i.name))"), pal_items(d))
        d.page.keyboard.press("Control+a"); d.page.keyboard.type("shelf")
        d.page.keyboard.press("Control+a"); d.page.keyboard.type("Media box")
        until(d, "Helm.palette.items().some(i => i.group === 'Notes and threads' && /Media box disk/.test(i.name))")
        check("the last row searches everything (the search page keeps working)", pal_items(d)[-1]["href"].startswith("#/search?q="), pal_items(d)[-1])
        sel0 = d.eval("document.querySelector('#hm-pal .hm-pal-item.sel').dataset.i")
        d.page.keyboard.press("ArrowDown")
        sel1 = d.eval("document.querySelector('#hm-pal .hm-pal-item.sel').dataset.i")
        check("ArrowDown moves the selection and aria-selected follows", sel0 == "0" and sel1 == "1" and d.eval("document.querySelector('#pal-opt-1').getAttribute('aria-selected')") == "true", (sel0, sel1))
        d.page.keyboard.press("ArrowUp")
        d.page.keyboard.press("Enter")
        check("Enter opens the selected thread", until(d, "location.hash.startsWith('#/needs-you/t-media-box-disk') || location.hash.startsWith('#/threads/t-media-box-disk')"), d.hash())
        check("the palette is closed after it navigates", d.eval("!Helm.palette.isOpen()"))

        print("\nThe palette never shows a private item")
        reqs.clear()
        d.page.keyboard.press("Control+k")
        until(d, "Helm.palette.isOpen()")
        d.page.keyboard.type("CANARY")
        d.page.wait_for_timeout(1200)
        shown = [i for i in pal_items(d) if i["group"] != "Search"]
        check("a private card is not listed whatever it is called", not [i for i in shown if "CANARY" in i["name"] or "Private" in i["name"]], shown)
        d.page.keyboard.press("Control+a"); d.page.keyboard.type("disk")
        d.page.wait_for_timeout(800)
        d.page.keyboard.press("Escape")

        print("\nThe palette's actions and the older ways keep working")
        d.page.keyboard.press("Control+k")
        until(d, "Helm.palette.isOpen()")
        d.page.keyboard.type("file a card")
        d.page.wait_for_timeout(300)
        d.page.keyboard.press("Enter")
        check("an action runs (File a card opens the sheet)", until(d, "!document.querySelector('#sheet').classList.contains('hidden')"))
        d.page.click("#c-cancel")
        until(d, "document.querySelector('#sheet').classList.contains('hidden')")
        d.eval("document.activeElement.blur()")
        d.page.fill("#q", "monitor")
        d.page.press("#q", "Enter")
        check("typing in the top box and Enter still opens the search page", until(d, "location.hash.startsWith('#/search')") and until(d, "document.querySelector('#view-search .hit, #view-search .sec-rows, #view-search h3') !== null"), d.hash())
        d.eval("document.activeElement.blur && document.activeElement.blur()")
        d.page.keyboard.press("Control+o")
        check("Ctrl+O still opens the note switcher", until(d, "!!document.querySelector('.nb-overlay #sw-q')"))
        d.page.keyboard.press("Escape")
        until(d, "!document.querySelector('.nb-overlay')")
        check("no page errors", not d.errors, d.errors)
        d.close()

        browser.close()

    print("\nToday with the notebook read-only (the staging and fixture default)")
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        d = Drive(browser, st.home, WIDE_DESK, "#/home")
        d.wait("#view-home .hm-home .hm-home-hi")
        d.page.wait_for_timeout(600)
        check("read-only notes: no line box, the page still draws", d.eval("!document.querySelector('#home-line') && !!document.querySelector('#home-log .hm-log-now')"))
        check("no page errors", not d.errors, d.errors)
        d.close()
        browser.close()

    print("\n%d passed, %d failed" % (passed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
