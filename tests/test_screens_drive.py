"""Playwright drives of the three-pane app: the shell and navigation, L1-L4.

Run: PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/test_screens_drive.py

The deployment comes up on loopback from synthetic data (tools/screens/fixture.py) plus 120 more
done threads PUT through the api, and headless Chromium drives the pages at 999x1392 (a desktop
window) and 412x915 (phone), through the web role with the login header set:

  L1  the three panes are the main screen; NEEDS YOU opens on load and its first
      ask is in the middle pane with its options in the viewport, no scrolling;
  L2  the left nav lists the site's sections in order, each with a count;
  L3  THREADS lists every thread ever held, newest first, paged (more), filtered by state, date,
      agent and full text, and a row opens its conversation;
  L4  a thread opens as its conversation from every entry point (a NEEDS YOU row, a THREADS row,
      an ACTIVITY row, a search hit, a notebook backlink, a front-desk feed row, the old deep
      links), the properties beside it, and the owner can write on a done thread;
  the phone stack (nav, list, conversation, properties, back), and the old routes.

Playwright and Chromium are required (CI installs them); set HELM_SKIP_DRIVES=1 to skip this
suite deliberately, which reports one failed check so a skipped run never reads as green.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "screens"))

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail else ""))


if os.environ.get("HELM_SKIP_DRIVES") == "1":
    check("the Playwright drives ran (HELM_SKIP_DRIVES=1 set: skipped deliberately)", False)
    print(f"{passed} passed, {failed} failed")
    sys.exit(1)

import fixture  # noqa: E402
import shoot  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HEADERS = fixture.HEADERS
WIDE = (999, 1392)
PHONE = (412, 915)
ADJ = ["quiet", "stale", "spare", "nightly", "weekly", "flaky", "slow", "loud", "cold", "warm", "dusty", "brisk",
       "odd", "even", "late", "early", "wide", "narrow", "deep", "flat", "sharp", "round", "plain", "fancy",
       "green", "amber", "grey", "blue", "red", "gold", "silver", "copper", "iron", "stone", "glass", "paper",
       "north", "south", "east", "west"]
NOUN = ["mirror", "backup", "digest", "monitor", "volume", "sweep", "lantern", "portal", "ticket", "registrar",
        "recorder", "gateway", "runner", "pane", "queue", "lease", "goal", "board", "wiki", "notebook"]
N_EXTRA = 120


def api(site, path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(site.api_url + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Authorization": "Bearer " + fixture.TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


def seed_history(site):
    """120 more threads, filed and closed a month ago: the THREADS list has something to page."""
    now = time.time()
    threads = []
    for i in range(N_EXTRA):
        title = "%s %s %d" % (ADJ[i % len(ADJ)], NOUN[(i // len(ADJ)) % len(NOUN)], i)
        if i % 30 == 0:
            title += " about the restic prune"
        threads.append({"thread_id": "t-hist-%03d" % i, "title": title, "summary": "About " + title, "category": "History",
                        "urgency": "whenever", "state": "needs-you", "responder": "sweep:history",
                        "ask": {"kind": "ack", "prompt": "Read it and close it", "requires": ["decision"],
                                "options": [{"label": "Got it", "value": "ack", "effect": "Closes the card"}]},
                        "sources": [{"source": "history", "id": str(i)}],
                        "events": [{"ts": now - 86400 * 31 - i * 60, "source": "sweep:history", "kind": "bot-result", "body": "Report %d: nothing to do." % i}]})
    out = api(site, "/api/triage/threads", {"threads": threads}, "PUT")
    assert out.get("created") == N_EXTRA, out
    for t in threads:
        api(site, "/api/triage/threads/%s/reply" % t["thread_id"], {"value": "ack"})


class Drive:
    def __init__(self, browser, site, size, route, reduced_motion="no-preference", before_goto=None):
        self.site = site
        self.ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1,
                                       extra_http_headers=HEADERS, locale="en-US", timezone_id="America/New_York",
                                       reduced_motion=reduced_motion)
        self.page = self.ctx.new_page()
        self.errors = []
        self.closing = False   # set by close(): a route handler still alive then is serving a page that is going away
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        if before_goto:
            before_goto(self.ctx)   # e.g. ctx.route(...): in place before the page's first request
        self.page.goto(site.web_url + "/" + route, wait_until="domcontentloaded")

    def wait(self, sel, timeout=20000):   # 20 s: the host is shared with other sessions' test runs
        try:
            self.page.wait_for_selector(sel, timeout=timeout)
        except Exception:
            # say where the page was when the wait gave up, so a failure reads on its own
            print("  [wait for %s gave up at %s; main=%s pane=%s props=%r body=%r errors=%s]" % (
                sel, self.hash(), self.eval("document.body.dataset.main"), self.eval("document.body.dataset.pane"),
                self.eval("document.querySelector('#props-title').textContent"),
                self.eval("document.querySelector('#props-body').innerHTML.slice(0, 160)"), self.errors[:3]))
            raise
        return self.page.query_selector(sel)

    def hash(self):
        return self.page.evaluate("location.hash")

    def wait_hash(self, pred, timeout=20.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            h = self.hash()
            if pred(h):
                return h
            self.page.wait_for_timeout(100)
        return self.hash()

    def slow_network(self, latency_ms):
        """Every request from now on takes latency_ms longer, as on a loaded host."""
        cdp = self.ctx.new_cdp_session(self.page)
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": latency_ms,
                                                      "downloadThroughput": -1, "uploadThroughput": -1})

    def until(self, js, timeout=20000):
        """Wait for a page condition; False when it never comes, so the check after it still fails."""
        try:
            self.page.wait_for_function(js, timeout=timeout)
            return True
        except Exception:
            return False

    def eval(self, js):
        return self.page.evaluate(js)

    def close(self):
        # A route handler that is still in flight when the page closes must not turn a suite that already
        # reported its checks into a crash: the browser's close re-raises the handler's TargetClosedError
        # and the suite's own "N passed, M failed" line never prints (run_all then counts 0 passed, 1 failed).
        # Unroute first, ignoring what a handler still running throws, and let the suites see closing=True.
        self.closing = True
        try:
            self.page.unroute_all(behavior="ignoreErrors")
        except Exception:   # already closed, or nothing routed: closing the context is all that is left
            pass
        self.ctx.close()


def rect(page, sel):
    return page.evaluate("(s) => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height, bottom: r.bottom}; }", sel)


def wait_conv(d):
    """Wait for the body's main-view flag to read "conv" (it can flip a beat after the title shows on a loaded
    runner). The check that follows still asserts the flag; a flag that never flips fails that check."""
    try:
        d.page.wait_for_function("document.body.dataset.main === 'conv'", timeout=10000)
    except Exception:
        pass


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        seed_history(st.home)
        browser = p.chromium.launch(**shoot.launch_args())

        # ------------------------------------------------------------ L1: the three panes, answers first
        for name, site in (("home", st.home),):
            print(f"\nL1 · {name} · the three panes at 999x1392, answers first")
            d = Drive(browser, site, WIDE, "#/needs-you")
            h = d.wait_hash(lambda h: h.startswith("#/needs-you/"))
            check(f"{name}: NEEDS YOU opens on load and its first ask opens in the middle pane", h == "#/needs-you/t-media-box-disk", h)
            d.wait(".conv-live-ask .conv-opt")
            for pane in ("#pane-left", "#pane-main", "#pane-props"):
                r = rect(d.page, pane)
                check(f"{name}: {pane} is on screen", r and r["w"] > 150 and r["h"] > 600, r)
            left, main, props = rect(d.page, "#pane-left"), rect(d.page, "#pane-main"), rect(d.page, "#pane-props")
            check(f"{name}: nav left, conversation middle, properties right", left["x"] < main["x"] < props["x"] and props["x"] + props["w"] <= 999.5, (left, main, props))
            first = d.eval("[...document.querySelectorAll('#shell-rail .nav-sec .nav-label')].slice(0, 2).map(e => e.textContent.trim())")
            check(f"{name}: Today, then Needs you, lead the rail", first == ["Today", "Needs you"], first)
            opts = d.eval("[...document.querySelectorAll('.conv-live-ask .conv-opt')].map(b => { const r = b.getBoundingClientRect(); return [b.textContent.trim().slice(0, 30), r.top, r.bottom]; })")
            check(f"{name}: the ask's options are in the viewport without scrolling", len(opts) == 3 and all(0 <= o[1] and o[2] <= 1392 for o in opts), opts)
            scrolled = d.eval("document.scrollingElement.scrollTop + document.querySelector('.conv-foot').scrollTop")
            check(f"{name}: nothing was scrolled to get there", scrolled == 0, scrolled)
            note = rect(d.page, ".conv-note")
            check(f"{name}: the note box and Send note only are in the viewport too", note and note["bottom"] <= 1392 and rect(d.page, ".conv-send")["bottom"] <= 1392, note)
            title = d.eval("document.querySelector('.conv-title').textContent")
            check(f"{name}: the middle pane is the lit card's conversation", "Media box disk" in title, title)
            check(f"{name}: the right pane holds the properties (card, urgency, category)", d.eval("[...document.querySelectorAll('#props-body .conv-prop-label')].map(e => e.textContent)").count("Urgency") == 1)
            check(f"{name}: no page errors", not d.errors, d.errors)
            rails = d.eval("(() => { const s = getComputedStyle(document.documentElement); return {left: s.getPropertyValue('--pane-left').trim(), props: s.getPropertyValue('--pane-props').trim()}; })()")
            check(f"{name}: the <=1100 fixed rail tokens are 244 px and 250 px", rails == {"left": "244px", "props": "250px"}, rails)
            check(f"{name}: the rendered rails match those tokens", abs(left["w"] - 244) < 2 and abs(props["w"] - 250) < 2, (left, props))
            # answer it: the option tap lands (v10: a pointer pair), the card leaves NEEDS YOU, the conversation shows the answer.
            # A loaded host (load ~35) takes more than 1.5 s to refresh after the tap: play one here, 2 s on every
            # request from now on, so the checks below wait on what they check, never on a clock.
            d.slow_network(2000)
            d.page.click(".conv-live-ask .conv-opt >> nth=0")
            t0 = time.time()
            while (t := api(site, "/api/triage/threads/t-media-box-disk"))["state"] != "working" and time.time() - t0 < 20:
                time.sleep(0.2)
            check(f"{name}: tapping an option answered the card (state {t['state']})", t["state"] == "working", t["state"])
            d.until("document.querySelector('.conv-sub').textContent.startsWith('working')")
            check(f"{name}: the conversation re-read after the answer (the service reads the thread's new state)", d.eval("document.querySelector('.conv-sub').textContent").startswith("working"), d.eval("document.querySelector('.conv-sub').textContent"))
            d.until("document.querySelector('#count-needs-you').textContent === '2'")
            check(f"{name}: NEEDS YOU count dropped to 2", d.eval("document.querySelector('#count-needs-you').textContent") == "2", d.eval("document.querySelector('#count-needs-you').textContent"))
            d.close()

        # ------------------------------------------------------------ L2: the nav sections with counts
        print("\nL2 · the left nav, each section with its count")
        for name, site, want in (("home", st.home, ["Today", "Needs you", "Threads", "Notebook", "Activity"]),):
            d = Drive(browser, site, WIDE, "#/threads")
            d.wait("#list-threads .lrow")
            d.page.wait_for_timeout(1500)
            labels = d.eval("[...document.querySelectorAll('#shell-rail .nav-sec .nav-label')].map(e => e.textContent)")
            check(f"{name}: sections in order", labels == want, labels)
            counts = d.eval("Object.fromEntries([...document.querySelectorAll('#shell-rail .nav-sec')].map(a => [a.dataset.sec, a.querySelector('.nav-count').textContent]))")
            check(f"{name}: every section that has something waiting carries a count", all(counts.get(k.lower().replace(" ", "-").replace("front-desk", "frontdesk")) not in ("", None) for k in want if k not in ("Today", "Notebook")), counts)   # the notebook's size is not a badge (nothing waits on anyone)
            held = 10 + N_EXTRA
            check(f"{name}: NEEDS YOU = 2 (one answered above), THREADS: the model holds every thread ever held ({held}) and the badge says 99+", counts.get("needs-you") == "2" and d.eval("Helm.count('threads')") == held and counts.get("threads") == "99+", counts)
            d.close()

        # The fixed nav must retain a visible keyboard focus mark with reduced motion enabled.
        print("\nL2 · fixed navigation, reduced motion: keyboard focus on the open section still shows")
        d = Drive(browser, st.home, WIDE, "#/threads", reduced_motion="reduce")
        d.wait("#shell-rail .nav-sec.on")
        d.page.wait_for_timeout(500)
        look = "(() => { const s = getComputedStyle(document.querySelector('#shell-rail .nav-sec.on')); return [s.outlineStyle, s.outlineWidth, s.outlineColor, s.backgroundColor, s.boxShadow]; })()"
        unfocused = d.eval(look)
        on_open = "document.activeElement && document.activeElement.matches('#shell-rail .nav-sec.on:focus-visible')"
        for _ in range(40):
            if d.eval(on_open):
                break
            d.page.keyboard.press("Tab")
        check("Tab reaches the open section with :focus-visible", d.eval(on_open), d.eval("document.activeElement && document.activeElement.outerHTML.slice(0, 120)"))
        focused = d.eval(look)
        check("the open section has a static two-pixel focus outline with no animation", focused != unfocused and focused[0] == "solid" and focused[1] == "2px", (unfocused, focused))
        d.close()

        # ------------------------------------------------------------ L3: THREADS
        print("\nL3 · THREADS: every thread, newest first, paged, filtered, searched")
        d = Drive(browser, st.home, WIDE, "#/threads")
        d.wait("#list-threads .lrow")
        d.page.wait_for_timeout(800)
        total = int(d.eval("Helm.count('threads')"))
        rows = d.eval("document.querySelectorAll('#list-threads .lrow').length")
        check("the count is every thread the site has held (done ones included)", total == 10 + N_EXTRA, total)
        check("the first page holds 100 rows", rows == 100, rows)
        states = d.eval("[...document.querySelectorAll('#list-threads .lrow')].map(r => r.className)")
        check("done threads are in the list", any(" done" in s for s in states))
        first_title = d.eval("document.querySelector('#list-threads .lrow .ltitle').textContent")
        check("newest first: the card answered a moment ago leads", "Media box disk" in first_title, first_title)
        d.page.click("#list-threads .lmore .btn")
        d.page.wait_for_function("document.querySelectorAll('#list-threads .lrow').length > 100", timeout=10000)
        rows2 = d.eval("document.querySelectorAll('#list-threads .lrow').length")
        check("more: the second page appended", rows2 == total, rows2)
        d.page.click("#list-threads .chip:has-text('done')")
        h = d.wait_hash(lambda h: "state=done" in h)
        check("the done chip filters (and the address carries it)", "state=done" in h, h)
        d.page.wait_for_function("document.querySelectorAll('#list-threads .lrow').length > 0 && [...document.querySelectorAll('#list-threads .lrow')].every(r => r.className.includes(' done'))", timeout=10000)
        n_done = d.eval("document.querySelectorAll('#list-threads .lrow').length")
        check("every row is done", n_done >= 100, n_done)
        d.page.click("#list-threads .chip:has-text('clear filters')")
        d.wait_hash(lambda h: "state=" not in h)
        d.page.fill("#list-threads input[type=search]", "restic")
        d.page.press("#list-threads input[type=search]", "Enter")
        d.wait_hash(lambda h: "q=restic" in h)
        # a slow runner: wait for the re-read to land (the status line names the match count), not for a row count
        # (a soft wait: a run that never gets there fails this check, not the rest of the drive)
        d.until("(() => { const s = document.querySelector('#list-threads .lmore span'); return s && /^\\d+ of \\d+/.test(s.textContent) && !s.textContent.includes('reading') && +s.textContent.split(' ')[0] < 20 && +s.textContent.split(' ')[0] > 0; })()", timeout=30000)
        titles = d.eval("[...document.querySelectorAll('#list-threads .lrow .ltitle')].map(e => e.textContent)")
        check("full-text search: only the restic threads", titles and all("restic" in t for t in titles),
              (titles[:5], d.eval("location.hash"), d.eval("document.querySelector('#list-threads .lmore span').textContent")))
        d.page.click("#list-threads .chip:has-text('clear filters')")
        d.wait_hash(lambda h: "q=" not in h)
        d.page.fill("#list-threads .lwho", "sweep:history")
        d.page.press("#list-threads .lwho", "Enter")
        d.wait_hash(lambda h: "who=" in h)
        # the list stamps the filters it drew (data-q) and whether a read is out: wait for the who= render
        d.page.wait_for_function("(() => { const h = document.querySelector('#list-threads'); return h.dataset.q.includes('who=') && !h.dataset.loading; })()", timeout=30000)
        n_who = int(d.eval("document.querySelector('#list-threads .lmore span').textContent.split(' ')[0]"))
        check("the agent / runner filter", n_who == N_EXTRA, (n_who, d.eval("location.hash"), d.eval("document.querySelector('#list-threads').dataset.q")))
        d.page.click("#list-threads .chip:has-text('clear filters')")
        d.wait_hash(lambda h: "who=" not in h)
        tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
        d.page.fill("#list-threads input[type=date] >> nth=0", tomorrow)
        d.page.dispatch_event("#list-threads input[type=date] >> nth=0", "change")
        d.page.wait_for_function("location.hash.includes('from=')", timeout=10000)
        d.page.wait_for_function("document.querySelector('#list-threads .lempty') !== null", timeout=10000)
        check("a date range from tomorrow matches nothing", d.eval("document.querySelector('#list-threads .lempty').textContent").lower().startswith("no thread"))
        d.page.click("#list-threads .chip:has-text('clear filters')")
        d.wait_hash(lambda h: "from=" not in h)
        d.wait("#list-threads .lrow.done")
        d.page.click("#list-threads .lrow.done >> nth=0")
        h = d.wait_hash(lambda h: h.startswith("#/threads/"))
        d.wait(".conv-timeline .conv-msg, .conv-timeline .conv-sys")
        check("a row opens its conversation in the middle pane, the list staying beside it", h.startswith("#/threads/") and d.eval("document.querySelectorAll('#list-threads .lrow').length") > 0, h)
        check("the opened row is marked", d.eval("document.querySelector('#list-threads .lrow.on') !== null"))
        check("no page errors", not d.errors, d.errors)
        d.close()

        # CI run 42 (2026-09-26): on a loaded runner the restic search never filtered. "clear filters" sends a
        # read; the search was typed while it was out; the read landed and redrew the list, and the redraw
        # rebuilt the search box empty, so Enter searched for nothing. Play that on a slow network.
        print("\nL3 · THREADS on a slow network: what is typed survives a read landing")
        d = Drive(browser, st.home, WIDE, "#/threads")
        d.wait("#list-threads .lrow")
        d.until("!document.querySelector('#list-threads').dataset.loading")
        d.page.click("#list-threads .lmore .btn")
        d.until("document.querySelectorAll('#list-threads .lrow').length > 100 && !document.querySelector('#list-threads').dataset.loading")
        d.slow_network(1500)
        # A refresh re-reads both open pages, one after the other; a filter set while it is on its first
        # must not have the refresh's second read land on top of the new filter's first page.
        d.eval("Helm.refresh()")
        d.until("document.querySelector('#list-threads').dataset.loading === '1'")
        d.page.click("#list-threads .chip:has-text('done')")
        d.until("(() => { const h = document.querySelector('#list-threads'); return h.dataset.q === 'state=done' && !h.dataset.loading; })()")
        d.page.wait_for_timeout(4000)
        n_rows = d.eval("document.querySelectorAll('#list-threads .lrow').length")
        stat = d.eval("document.querySelector('#list-threads .lmore span').textContent")
        check("a filter set during a refresh shows the filter's first page, not the refresh's second", n_rows == 100 and all(" done" in c for c in d.eval("[...document.querySelectorAll('#list-threads .lrow')].map(r => r.className)")), (n_rows, stat))
        d.page.click("#list-threads .chip:has-text('clear filters')")
        d.until("document.querySelector('#list-threads').dataset.loading === '1'")
        d.page.fill("#list-threads input[type=search]", "restic")
        out = d.eval("document.querySelector('#list-threads').dataset.loading")
        check("the search was typed while the clear-filters read was out", out == "1", out)
        d.until("!document.querySelector('#list-threads').dataset.loading && document.querySelector('#list-threads').dataset.q === ''")
        box = d.eval("(() => { const q = document.querySelector('#list-threads input[type=search]'); return [q.value, document.activeElement === q]; })()")
        check("the read landed and the search box still holds what was typed, with the focus", box == ["restic", True], box)
        d.page.keyboard.press("Enter")
        d.until("(() => { const h = document.querySelector('#list-threads'); return h.dataset.q === 'q=restic' && !h.dataset.loading; })()")
        stat = d.eval("document.querySelector('#list-threads .lmore span').textContent")
        titles = d.eval("[...document.querySelectorAll('#list-threads .lrow .ltitle')].map(e => e.textContent)")
        n = int(stat.split(" ")[0]) if stat[:1].isdigit() else 0
        check("Enter searched for it: the count is the restic threads", 0 < n < 20, (stat, d.eval("location.hash")))
        check("and the list is only the restic threads", titles and all("restic" in t for t in titles), titles[:5])
        d.page.click("#list-threads .chip:has-text('clear filters')")
        d.until("document.querySelector('#list-threads').dataset.loading === '1'")
        d.page.fill("#list-threads .lwho", "sweep:history")
        d.until("!document.querySelector('#list-threads').dataset.loading && document.querySelector('#list-threads').dataset.q === ''")
        who = d.eval("document.querySelector('#list-threads .lwho').value")
        check("the agent box, too, keeps what was typed across a read", who == "sweep:history", who)
        check("no page errors", not d.errors, d.errors)
        d.close()

        # ------------------------------------------------------------ L4: every entry point opens the conversation
        print("\nL4 · every card is a conversation, from every entry point")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait_hash(lambda h: h.startswith("#/needs-you/"))
        d.wait("#list-needs-you .lrow")
        d.page.click("#list-needs-you .lrow >> nth=1")
        h = d.wait_hash(lambda h: h.startswith("#/needs-you/t-"))
        d.page.wait_for_function("document.querySelector('.conv-title').textContent.length > 3", timeout=10000)
        check("a NEEDS YOU row", h.startswith("#/needs-you/") and d.eval("document.querySelector('.conv-title').textContent") != "", h)
        # The activity list is "today" in the page's own zone: the events at or after its local midnight. The
        # fixture's events are all in the past and the suite's own earlier writes are ~40 s old, so a run that
        # crosses midnight in that gap would find none. The read therefore follows a write of its own, and the
        # page is told its "today" began just before that write: the midnight-crossed case, on any clock.
        mark = time.time()
        api(st.home, "/api/triage/threads/t-media-box-disk/note", {"note": "L4: something moved just now", "actor": "drive"})
        d.page.route("**/api/triage/events?*", lambda route: route.continue_(
            url=re.sub(r"since=[^&]*", "since=%r" % mark, route.request.url)))
        d.page.goto(st.home.web_url + "/#/activity")
        d.wait("#list-activity .lrow")
        d.page.click("#list-activity .lrow >> nth=0")
        h = d.wait_hash(lambda h: h.startswith("#/activity/"))
        d.wait("#view-conv:not(.hidden) .conv-title")
        wait_conv(d)
        check("an ACTIVITY row", h.startswith("#/activity/") and d.eval("document.body.dataset.main") == "conv", h)
        d.page.goto(st.home.web_url + "/#/search?q=portal")
        d.wait(".sres .lrow")
        d.page.click(".sres .lrow >> nth=0")
        h = d.wait_hash(lambda h: "/t-" in h and not h.startswith("#/search"))
        d.wait("#view-conv:not(.hidden) .conv-title")
        wait_conv(d)
        check("a search hit", d.eval("document.body.dataset.main") == "conv", h)
        d.page.goto(st.home.web_url + "/#/note/Contacts%2FPine%20Hollow%20portal.md")
        d.wait("#props-body a[href^='#/conv/']")   # the Connected pane's thread chip (it replaced the topic aside)
        d.page.click("#props-body a[href^='#/conv/'] >> nth=0")
        h = d.wait_hash(lambda h: h.startswith("#/needs-you/") or h.startswith("#/threads/"))
        wait_conv(d)
        check("a notebook backlink (the topic hub's thread) resolves to the conversation", (h.startswith("#/needs-you/") or h.startswith("#/threads/")) and d.eval("document.body.dataset.main") == "conv", h)
        d.page.goto(st.home.web_url + "/#/needs-you/t-portal-name")
        d.wait("#props-body a[href^='#/note/']")
        d.page.click("#props-body a[href^='#/note/']")
        h = d.wait_hash(lambda h: h.startswith("#/note/"))
        d.wait("#main .md")
        check("a note: link in the properties opens the note", h.startswith("#/note/Contacts"), h)
        for old, want in (("#t=t-monitor-dedupe", "#/threads/t-monitor-dedupe"), ("#conv-t-monitor-dedupe", "#/threads/t-monitor-dedupe"), ("#/conv/t-backup-restore-check", "#/needs-you/t-backup-restore-check"),
                          ("#/inbox/t-monitor-dedupe", "#/threads/t-monitor-dedupe"), ("#/thread/t-monitor-dedupe", "#/threads/t-monitor-dedupe"), ("#/inbox", "#/needs-you"), ("#/backlog", "#/threads?state=backlog"),
                          # the retired console's pages: an old bookmark lands on NEEDS YOU
                          ("#/agents", "#/needs-you"), ("#settings", "#/needs-you"), ("#/agents/frontdesk", "#/needs-you"),
                          ("#/board", "#/needs-you"), ("#/frontdesk/feed", "#/needs-you"), ("#/settings/seats", "#/needs-you"),
                          # Helm stage 2: an empty address is Today
                          ("", "#/home"), ("#/", "#/home"), ("#/home", "#/home")):
            d.page.goto(st.home.web_url + "/" + old)
            h = d.wait_hash(lambda h: h.startswith(want))
            check(f"the old link {old} lands on {want}", h.startswith(want), h)
        check("no page errors", not d.errors, d.errors)
        d.close()
        # writing on a done thread
        d = Drive(browser, st.home, WIDE, "#/threads/t-digest-cron?state=done")
        d.wait(".conv-timeline .conv-msg")
        check("a done thread's conversation shows its report", "Fixed" in d.eval("document.querySelector('.conv-timeline').textContent"))
        d.page.fill(".conv-note", "Also add a Monitor monitor for the cron leg")
        d.page.click(".conv-send")
        d.page.wait_for_timeout(1500)
        t = api(st.home, "/api/triage/threads/t-digest-cron")
        check("writing on a done thread reopens it with the note", t["state"] == "needs-you" and any("Monitor monitor" in (e.get("body") or "") for e in t["events"]), t["state"])
        d.close()
        d = Drive(browser, st.home, WIDE, "#/threads/t-parks-retry?state=done")
        d.wait(".conv-timeline .conv-msg, .conv-timeline .conv-sys")
        d.page.fill(".conv-note", "Look at the parks pass again next week")
        d.page.click(".conv-send")
        d.page.wait_for_selector(".toast", timeout=10000)
        toast = d.eval("document.querySelector('.toast').textContent")
        check("writing on a done thread is accepted", "didn't send" not in toast, toast)
        t = api(st.home, "/api/triage/threads/t-parks-retry")
        check("home: ...and reopens it with the note, through Helm's own route", t["state"] == "needs-you"
              and any("parks pass again" in (e.get("body") or "") for e in t["events"]), t["state"])
        d.close()

        # ------------------------------------------------------------ the phone stack
        print("\nphone · 412x915: nav, list, conversation, properties, back")
        d = Drive(browser, st.home, PHONE, "#/needs-you")
        d.wait("#list-needs-you .lrow")
        d.page.wait_for_timeout(500)
        check("on a phone NEEDS YOU shows the list, not a conversation", d.hash() == "#/needs-you" and d.eval("document.body.dataset.pane") == "left", d.hash())
        check("the back button is hidden on the list pane", d.eval("document.querySelector('#shell-back').classList.contains('hidden')"))
        # a card with options (the home done-thread note above reopened a card at the top of the list)
        d.page.click("#list-needs-you .lrow[data-id='t-backup-restore-check']")
        d.wait_hash(lambda h: h.startswith("#/needs-you/"))
        d.wait(".conv-live-ask .conv-opt")
        check("a row opens the conversation as the next pane", d.eval("document.body.dataset.pane") == "main" and rect(d.page, "#pane-main")["w"] >= 400)
        check("the option buttons fit the phone", all(o <= 412 for o in d.eval("[...document.querySelectorAll('.conv-live-ask .conv-opt')].map(b => b.getBoundingClientRect().right)")))
        # the route paints a moment after the hash changes: wait for the pane, not the address
        pane_is = lambda p: d.page.wait_for_function("document.body.dataset.pane === '%s'" % p, timeout=10000)
        d.page.click("#view-conv .conv-props-toggle")
        h = d.wait_hash(lambda h: h.endswith("/props"))
        pane_is("props")
        d.page.wait_for_function("document.querySelectorAll('#props-body .conv-prop-row').length > 3", timeout=10000)
        check("Details opens the properties pane", d.eval("document.body.dataset.pane") == "props" and d.eval("document.querySelectorAll('#props-body .conv-prop-row').length") > 3, (h, d.eval("document.body.dataset.pane")))
        d.page.click("#shell-back")
        h = d.wait_hash(lambda h: not h.endswith("/props"))
        pane_is("main")
        check("back: properties -> conversation", d.eval("document.body.dataset.pane") == "main", (h, d.eval("document.body.dataset.pane")))
        d.page.click("#shell-back")
        h = d.wait_hash(lambda h: h == "#/needs-you")
        pane_is("left")
        check("back: conversation -> list", d.eval("document.body.dataset.pane") == "left", (h, d.eval("document.body.dataset.pane")))
        d.page.click("#rail-open")
        d.page.click("#rail-drawer .nav-sec[data-sec='notebook']")
        d.wait_hash(lambda h: h.startswith("#/notebook"))
        pane_is("main")
        check("a page section opens as the middle pane on the phone", d.eval("document.body.dataset.pane") == "main")
        check("no page errors", not d.errors, d.errors)
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
