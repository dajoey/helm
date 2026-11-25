"""Playwright drive of ACTIVITY's run blocks on a second site.

Run: python3 tests/test_screens_activity_runs.py

Old the earlier app showed the executor runs, what they cost, the threads that cost most and the
dispatcher's log. The site is remote now and the client's the poller sends them on its
heartbeat (tests/test_work_activity_runs.py covers the api). This drives a second site:
before any run data arrives each block says the client has not sent it yet; after a
heartbeat carrying runs and a log tail the four blocks show them, and a top thread opens
its conversation. The home site has no such blocks (its dispatcher is the producer).
"""
from __future__ import annotations

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

import fixture  # noqa: E402
import shoot  # noqa: E402
from test_screens_drive import WIDE, Drive, api  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


BLOCKS = ("act-runs", "act-costs", "act-top", "act-log")
NOT_SENT = "the poller has not sent run data yet"


def block_text(d, cls):
    return d.eval("(document.querySelector('#view-activity section.%s') || {}).textContent || ''" % cls)


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        w = st.home

        print("\nbefore the client sends run data")
        d = Drive(browser, w, WIDE, "#/activity")
        d.wait("#view-activity .act-quiet")
        try:
            d.page.wait_for_selector("#view-activity section.act-log", timeout=15000)
        except Exception:  # noqa: BLE001 -- the checks below say what is missing
            pass
        for cls in BLOCKS:
            t = block_text(d, cls)
            check("the %s block is on the page and says nothing was sent yet" % cls, NOT_SENT in t, t[:200])
        d.close()

        print("\nafter a heartbeat with runs and a log tail")
        api(w, "/api/triage/threads", {"threads": [{"thread_id": "t-costly-run", "title": "Rebuild the portal cache", "summary": "Cache rebuild",
                                                     "category": "Contacts", "urgency": "this-week", "state": "working",
                                                     "ask": {"kind": "ack", "prompt": "Read?"},
                                                     "sources": [{"source": "drive", "id": "costly"}]}]}, "PUT")
        now = time.time()
        runs = [{"name": "t-costly-run-17900000%02d" % i, "ts": now - 600 * (i + 1), "thread_id": "t-costly-run",
                 "outcome": "result", "turns": 12, "cost_usd": 1.5, "input_tokens": 1000, "output_tokens": 2000,
                 "cache_read_tokens": 90000, "duration_s": 130} for i in range(3)]
        log = ["2026-09-27 01:00:%02d tick %d: 0 pending" % (i, i) for i in range(20)]
        api(w, "/api/triage/dispatcher-heartbeat", {"ts": now, "host": "client", "tick": 9, "runs": runs, "log_tail": log})

        d = Drive(browser, w, WIDE, "#/activity")
        d.wait("#view-activity .act-quiet")
        d.until("!!document.querySelector('#view-activity section.act-runs .act-quiet-row')")
        t = block_text(d, "act-runs")
        check("Executor runs lists the runs with their thread's title",
              d.eval("document.querySelectorAll('#view-activity section.act-runs .act-quiet-row').length") == 3
              and "Rebuild the portal cache" in t and "$1.50" in t and "12 turns" in t, t[:300])
        t = block_text(d, "act-costs")
        check("What the runs cost adds them up (7 days)", "3 runs, $4.50 nominal, 12 turns each" in t, t[:300])
        check("and names tokens", "re-read context" in t and "written" in t, t[:300])
        t = block_text(d, "act-top")
        check("Top threads names the thread and its cost", "Rebuild the portal cache" in t and "3 runs · $4.50" in t, t[:300])
        lines = d.eval("[...document.querySelectorAll('#view-activity section.act-log .act-logtail > div')].map(e => e.textContent)")
        check("Dispatcher log shows the last 14 lines", lines == log[-14:], lines)
        for cls in BLOCKS:
            t = block_text(d, cls)
            check("the %s block no longer says nothing was sent" % cls, t and NOT_SENT not in t, t[:200])
        h = d.hash()
        if d.eval("!!document.querySelector('#view-activity section.act-top a.act-quiet-title')"):
            d.page.click("#view-activity section.act-top a.act-quiet-title")
            h = d.wait_hash(lambda h: h.startswith("#/activity/"))
        check("a top thread opens its conversation", h == "#/activity/t-costly-run", h)
        check("no page errors", not d.errors, d.errors)
        d.close()
        browser.close()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
