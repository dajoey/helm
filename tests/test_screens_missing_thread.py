"""Playwright drive: a conversation open on a thread the site does not hold stops asking for it.

Run: python3 tests/test_screens_missing_thread.py

The work api's error watcher filed `GET /api/triage/threads/:id 404` (22 in 10 minutes). A tab
left open on a thread that is gone (deleted, or a stale deep link) read it again on every 30 s
tick, and twice per read: once in refresh() and once more in the localConversation() fallback
the conversation's own 404 sent it to. A thread that is not there is not going to appear
because the page asked again.

1. Open a missing thread: the page says it is gone, and neither the open nor three poll ticks
   send GET /api/triage/threads/:id (it was 2 per tick, forever; then one per open, in parallel
   with the conversation read whose 404 already answered it). The open runs with the drives delayed
   and the list's auto-selected thread still reading, so a refresh that outlives the thread it was
   started for cannot pass unnoticed when the host is quiet (it read the new thread's route, and the
   route 404'd, only once the host was loaded). The counts are requests, not answers.
2. R (a deliberate refresh) still asks the conversation feed again.
3. A thread that exists is still re-read on every tick.
Work site, synthetic data (tools/screens/fixture.py).
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
from test_screens_drive import WIDE, Drive  # noqa: E402

passed = failed = 0
MISSING = "t-drive-no-such-thread"
CARDED = "t-media-box-disk"


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def watch(d):
    """Every request the page makes from now on: (method, path, status). The status is filled in when the
    answer lands, so a slow answer (or one that never lands) still counts as a request: these checks are
    about what the page asked for, and a loaded host only changes when the answer arrives."""
    seen = []
    waiting = {}

    def on_request(r):
        row = [r.method, r.url.split("//", 1)[-1].split("/", 1)[-1].split("?")[0], None]
        waiting.setdefault((r.method, r.url), []).append(row)
        seen.append(row)

    def on_response(r):
        for row in waiting.get((r.request.method, r.request.url), []):
            if row[2] is None:
                row[2] = r.status
                break

    d.page.on("request", on_request)
    d.page.on("response", on_response)
    return seen


def thread_reads(seen, tid):
    return [s for s in seen if s[0] == "GET" and s[1] == "api/triage/threads/" + tid]


def conv_reads(seen, tid):
    return [s for s in seen if s[0] == "GET" and s[1] == "api/conversations/" + tid]


def press_r(d):
    d.eval("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    d.page.keyboard.press("r")


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())

        print("\n1 · a thread the site does not hold: said once, not polled")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        # The list auto-selects its first row, so a refresh for THAT thread is still in flight when the open
        # moves to the missing one. A loaded host widens that window to seconds; the client-side delay widens
        # it on every run, so what is counted below cannot depend on what else the host is doing.
        d.slow_network(1500)
        d.wait("#list-needs-you")
        h0 = d.wait_hash(lambda h: h.startswith("#/needs-you/"), timeout=10)   # the auto-selected thread: its read is out
        d.page.clock.install()   # the 30 s poll runs on the drive's clock from here
        seen = watch(d)
        d.page.evaluate("location.hash = '#/threads/%s'" % MISSING)
        d.wait(".conv-timeline")
        # Settled means the page has finished the open and said what it found; a fixed pause counts a page
        # that is still mid-open on a loaded host.
        gone = d.until("document.querySelector('.conv-banner').textContent.includes('no longer')", timeout=15000)
        text = d.eval("document.querySelector('.conv-timeline').textContent + ' ' + document.querySelector('.conv-banner').textContent")
        check("the page says the thread is no longer there", gone and "no longer" in text, (h0, text))
        n0 = len(thread_reads(seen, MISSING))
        d.slow_network(0)   # the rest of the suite runs at the host's own pace
        for _ in range(3):
            d.page.clock.run_for(31000)
            d.page.wait_for_timeout(400)
        n1 = len(thread_reads(seen, MISSING))
        check("three poll ticks: no further read of the missing thread", n1 == n0, (n0, n1))
        check("the open never asked for the thread itself: the conversation feed's 404 already said it is gone", n0 == 0, n0)
        check("the open asked the conversation feed", len(conv_reads(seen, MISSING)) >= 1, conv_reads(seen, MISSING))
        check("no page errors", not d.errors, d.errors)

        print("\n2 · R asks again on purpose")
        c1 = len(conv_reads(seen, MISSING))
        press_r(d)
        for _ in range(50):   # poll up to 5 s: a loaded runner answers late, not never
            if len(conv_reads(seen, MISSING)) > c1:
                break
            d.page.wait_for_timeout(100)
        c2 = len(conv_reads(seen, MISSING))
        check("a deliberate refresh asks the conversation feed again", c2 > c1, (c1, c2))
        check("and still does not spend a 404 on the thread route", len(thread_reads(seen, MISSING)) == 0, thread_reads(seen, MISSING))
        d.close()

        print("\n3 · a thread that exists is still re-read on every tick")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you")
        d.page.clock.install()
        seen = watch(d)
        d.page.evaluate("location.hash = '#/threads/%s'" % CARDED)
        d.wait(".conv-timeline")
        d.page.wait_for_timeout(1500)
        n0 = len(thread_reads(seen, CARDED))
        for _ in range(3):
            d.page.clock.run_for(31000)
            d.page.wait_for_timeout(400)
        n1 = len(thread_reads(seen, CARDED))
        check("three poll ticks read the live thread three times", n1 - n0 >= 3, (n0, n1))
        for _ in range(50):   # the answers land after the requests; a loaded host lands them late
            if all(s[2] is not None for s in thread_reads(seen, CARDED)):
                break
            d.page.wait_for_timeout(100)
        check("no 404 on it", all(s[2] == 200 for s in thread_reads(seen, CARDED)), thread_reads(seen, CARDED))
        check("no page errors", not d.errors, d.errors)
        d.close()

        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
