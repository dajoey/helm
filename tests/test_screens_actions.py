"""Playwright drives of the three-pane app, part two: ACTIVITY acting in place, and the
alarms a dead dispatcher or a stuck queue raise on the ACTIVITY row .

Run: python3 tests/test_screens_actions.py

The app runs on loopback from synthetic data (tools/screens/fixture.py). Every check drives the
page; the api is only read to confirm.
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
sys.path.insert(0, HERE)

if os.environ.get("HELM_SKIP_DRIVES") == "1":
    print("  FAIL the drives were skipped (HELM_SKIP_DRIVES=1): this suite never passes quietly")
    print("0 passed, 1 failed")
    sys.exit(1)

from playwright.sync_api import sync_playwright  # noqa: E402

import fixture  # noqa: E402
import shoot  # noqa: E402
from test_screens_drive import PHONE, WIDE, Drive, api  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def fresh(d):
    """Clear the toasts on screen, so the next toast read is the next action's."""
    d.eval("document.querySelectorAll('.toast').forEach(t => t.remove())")


def toast(d, timeout=10000):
    """The next toast's text ('' if none came). Call fresh(d) before the action."""
    try:
        d.page.wait_for_selector(".toast", timeout=timeout)
        return d.eval("document.querySelector('.toast').textContent")
    except Exception:
        return ""


def wait_for(d, pred_js, timeout=15000):
    try:
        d.page.wait_for_function(pred_js, timeout=timeout)
        return True
    except Exception:
        return False


def mins(text):
    """The whole minutes at the start of an elapsed reading ('41m 3s' -> 41), else -1."""
    m = re.match(r"\s*(\d+)m\b", text or "")
    return int(m.group(1)) if m else -1


MONITOR_CARD = "t-monitor-dedupe"


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())

        # ------------------------------------------------------------ : the ACTIVITY row's alarm, from any section
        print("\na dead dispatcher or a stuck queue marks the ACTIVITY row while another section is open ")
        w = st.home
        pip = "#count-activity"
        is_bad = "(document.querySelector(%s) || {classList: {contains: () => false}}).classList.contains('bad')" % json.dumps(pip)
        tip_js = "(document.querySelector(%s) || {}).title || ''" % json.dumps(pip)
        # The fixture's Monitor card holds an answer from 1.5 h ago that nothing has taken: answers backing up.
        api(w, "/api/triage/dispatcher-heartbeat", {"ts": time.time(), "host": "client", "tick": 41})
        q = api(w, "/api/status")["health"]["queue"]
        check("precondition: one answer has waited over 30 min, nothing gave up or went quiet", q["pending"] and q["oldest_wait_s"] > 1800 and not q["gave_up"] and not q["stale"], q)
        d = Drive(browser, w, WIDE, "#/needs-you")
        d.page.clock.install()   # the 60 s poll runs on the page's clock, so the drive can move it
        d.page.reload(wait_until="domcontentloaded")
        d.wait("#count-activity")
        check("an answer waiting 1.5 h puts 'bad' on the ACTIVITY row from NEEDS YOU", d.until(is_bad, 15000), d.eval("document.querySelector('#count-activity').className"))
        check("the section open is NEEDS YOU, not ACTIVITY", d.hash().startswith("#/needs-you"), d.hash())
        check("its title says answers are backing up", "backing up" in d.eval(tip_js), d.eval(tip_js))
        pip_on = "!!document.querySelector('.nav-sec[data-sec=activity] .nav-pip')"
        check("a red pip sits on the ACTIVITY row, and only there", d.eval(pip_on) and d.eval("document.querySelectorAll('.nav-pip').length") == 1)
        # The answer is taken, so the queue is clear; the dispatcher then dies.
        api(w, "/api/triage/threads/t-monitor-dedupe/dispatched",
            {"mode": "reply", "ok": True, "reply_ts": time.time()})
        api(w, "/api/triage/dispatcher-heartbeat", {"ts": time.time() - 4 * 3600, "host": "client", "tick": 42})
        q = api(w, "/api/status")["health"]["queue"]
        check("precondition: the queue is clear", not q["pending"] and not q["gave_up"] and not q["stale"], q)
        d.page.clock.run_for(61_000)
        check("a heartbeat 4 h old keeps it 'bad' and says the dispatcher stopped ticking",
              d.until("(%s).includes('stopped ticking') && !(%s).includes('backing up')" % (tip_js, tip_js), 15000) and d.eval(is_bad), d.eval(tip_js))
        api(w, "/api/triage/dispatcher-heartbeat", {"ts": time.time(), "host": "client", "tick": 43})
        d.page.clock.run_for(30_000)
        d.page.wait_for_timeout(1500)
        check("the row is not re-read faster than every 60 s", d.eval(is_bad))
        d.page.clock.run_for(31_000)
        check("a fresh heartbeat clears it at the next poll", d.until("!" + is_bad, 15000), d.eval(tip_js))
        check("...and its alarm text and pip go with it (the title falls back to the count)", not d.eval(pip_on) and d.eval("/^Activity, \\d+$/.test(document.querySelector(%s).title)" % json.dumps(pip)), d.eval(tip_js))
        api(w, "/api/triage/threads", {"threads": [{"thread_id": "t-pip-gave-up", "title": "Refresh the billing export", "summary": "Export refresh",
                                                     "category": "Contacts", "urgency": "this-week", "state": "needs-you",
                                                     "ask": {"kind": "choice", "prompt": "Refresh the export now?", "requires": ["decision"],
                                                             "options": [{"label": "Refresh it now", "value": "go", "effect": "A new export tonight"},
                                                                         {"label": "Leave it", "value": "wait", "effect": "Nothing changes"}]},
                                                     "sources": [{"source": "drive", "id": "pip"}]}]}, "PUT")
        api(w, "/api/triage/threads/t-pip-gave-up/reply", {"value": "go"})
        for _ in range(5):
            api(w, "/api/triage/threads/t-pip-gave-up/dispatched", {"mode": "start"})
        check("precondition: the answer gave up", api(w, "/api/status")["health"]["queue"]["gave_up"] >= 1)
        d.page.clock.run_for(61_000)
        check("an answer that gave up puts 'bad' on the row at the next poll", d.until(is_bad, 15000))
        check("...and its title says so", "given up on" in d.eval(tip_js), d.eval(tip_js))
        check("the count on the row is still today's events", d.eval("/^\\d+$/.test(document.querySelector(%s).textContent)" % json.dumps(pip)),
              d.eval("document.querySelector(%s).textContent" % json.dumps(pip)))
        d.page.click(".nav-sec[data-sec=threads]")
        d.wait_hash(lambda h: h.startswith("#/threads"))
        check("the mark survives the nav being redrawn (THREADS opened)", d.hash().startswith("#/threads") and d.eval(is_bad), (d.hash(), d.eval("document.querySelector('#count-activity').className")))
        check("no page errors", not d.errors, d.errors)
        d.close()
        api(w, "/api/triage/threads/t-pip-gave-up/close", {})

        # ------------------------------------------------------------ L7: ACTIVITY acts in place
        print("\nACTIVITY: rows act in place; quiet runs retry")
        api(w, "/api/triage/threads", {"threads": [{"thread_id": "t-quiet-run", "title": "Rebuild the portal cache", "summary": "Cache rebuild",
                                                     "category": "Contacts", "urgency": "this-week", "state": "needs-you",
                                                     "ask": {"kind": "choice", "prompt": "Rebuild the cache tonight?", "requires": ["decision"],
                                                             "options": [{"label": "Rebuild it tonight", "value": "go", "effect": "A short slow period"},
                                                                         {"label": "Leave it until Monday", "value": "wait", "effect": "Nothing changes"}]},
                                                     "sources": [{"source": "drive", "id": "quiet"}]}]}, "PUT")
        api(w, "/api/triage/threads/t-quiet-run/reply", {"value": "go"})
        for _ in range(5):
            api(w, "/api/triage/threads/t-quiet-run/dispatched", {"mode": "start"})
        d = Drive(browser, w, WIDE, "#/activity")
        d.wait("#view-activity .act-quiet")
        d.page.wait_for_function("document.querySelector('#view-activity .act-quiet').textContent.includes('Rebuild the portal cache')", timeout=15000)
        check("ACTIVITY's tiles are links, not a dashboard", d.eval("document.querySelectorAll('#view-activity a.act-tile').length") >= 4)
        props = d.eval("[...document.querySelectorAll('#props-body .act-kv a.act-link')].map(a => a.getAttribute('href'))")
        check("its counts link to the THREADS view that lists them", any("state=working" in h for h in props) and any("state=done" in h for h in props), props)
        check("a run that gave up is listed under Runs that went quiet", "gave up after 5 attempts" in d.eval("document.querySelector('#view-activity .act-quiet').textContent"))
        fresh(d)
        d.page.click("#view-activity .act-quiet-row:has-text('Rebuild the portal cache') .act-btn[data-act=retry]")
        t = toast(d)
        th = api(w, "/api/triage/threads/t-quiet-run")
        check("Retry from ACTIVITY re-offers the run with the owner's instruction", t.startswith("Sent back") and any(e["kind"] == "instruction" for e in th["events"]), (t, [e["kind"] for e in th["events"]]))
        lit_rows = d.eval("[...document.querySelectorAll('#list-activity .lrow')].filter(r => r.querySelector('.act-btn[data-act=answer]')).map(r => r.dataset.id)")
        check("a lit thread's newest row offers Answer", len(lit_rows) > 0, lit_rows)
        tid = lit_rows[0] if lit_rows else ""
        if tid:
            d.page.click("#list-activity .lrow[data-id='%s'] .act-btn[data-act=snooze]" % tid)
            d.wait(".act-pop")
            fresh(d)
            d.page.click(".act-pop .act-pop-item:has-text('Tomorrow')")
            t = toast(d)
            th = api(w, "/api/triage/threads/" + tid)
            check("Snooze from an ACTIVITY row", t.startswith("Snoozed") and th.get("wake_at", 0) > time.time(), (t, th.get("state"), th.get("wake_at")))
        check("no page errors", not d.errors, d.errors)
        d.close()

        # ------------------------------------------------------------ , : where cards came from, self-filing alert
        print("\nACTIVITY: where cards came from, and an alert when runs file their own work ")
        created_sel = "#view-activity section.act-created"
        alert_sel = "#view-activity .act-alert"
        cr = api(w, "/api/status")["health"]["created"]
        check("precondition: the seeded site is not self-filing", cr["replacement_rate"] < 0.5, cr)
        api(w, "/api/triage/threads", {"threads": [{"thread_id": "t-typed-by-owner", "title": "Ask the vendor about the renewal", "summary": "Renewal",
                                                     "category": "Contacts", "urgency": "this-week", "state": "working",
                                                     "ask": {"kind": "ack", "prompt": "Read?"},
                                                     "events": [{"ts": time.time() - 120, "source": "owner", "kind": "update", "body": "filed from the composer"}]}]}, "PUT")
        d = Drive(browser, w, WIDE, "#/activity")
        d.wait("#view-activity .act-quiet")
        try:
            d.page.wait_for_selector(created_sel, timeout=15000)
        except Exception:  # noqa: BLE001 -- the checks below say what is missing
            pass
        t = d.eval("(document.querySelector(%s) || {}).textContent || ''" % json.dumps(created_sel))
        cr = api(w, "/api/status")["health"]["created"]
        check("a 'Where cards came from, last 7 days' block is on the page", "where cards came from, last 7 days" in t.lower(), t[:300])
        keys = d.eval("[...document.querySelectorAll(%s)].map(e => e.textContent)" % json.dumps(created_sel + " .act-kv .k"))
        check("it counts what was created and names each source, owner as 'you'",
              "created" in keys and "you" in keys and "owner" not in keys and all(s in keys for s in cr["by_source"] if s != "owner"), (keys, cr["by_source"]))
        check("no alert while the replacement rate is low", not d.eval("!!document.querySelector(%s)" % json.dumps(alert_sel)), cr)
        check("no page errors", not d.errors, d.errors)
        d.close()

        # Runs that file their own work: threads whose first event is a run's ("bot"), more than the runs.
        now = time.time()
        api(w, "/api/triage/threads", {"threads": [{"thread_id": "t-run-host", "title": "Rotate the portal certificate", "summary": "Cert",
                                                     "category": "Contacts", "urgency": "whenever", "state": "working", "ask": {"kind": "ack", "prompt": "Read?"},
                                                     "events": [{"ts": now - 600, "source": "email-sweep", "kind": "update", "body": "found in mail"},
                                                                {"ts": now - 500, "source": "bot", "kind": "bot-result", "body": "rotated it"}]}]}, "PUT")
        cr = api(w, "/api/status")["health"]["created"]
        need = max(cr["runs"], 1) * 2
        seeded = [{"thread_id": "t-self-filed-%d" % i, "title": "Follow-up the run filed for itself %d" % i, "summary": "Self-filed",
                   "category": "Contacts", "urgency": "whenever", "state": "working", "ask": {"kind": "ack", "prompt": "Read?"},
                   "events": [{"ts": now - 300 + i, "source": "bot", "kind": "update", "body": "a run filed this"}]}
                  for i in range(need - cr["by_run"])]
        api(w, "/api/triage/threads", {"threads": seeded}, "PUT")
        cr = api(w, "/api/status")["health"]["created"]
        check("precondition: the site is now self-filing (rate >= 1)", cr["runs"] and cr["replacement_rate"] >= 1, cr)
        d = Drive(browser, w, WIDE, "#/activity")
        d.wait("#view-activity .act-quiet")
        try:
            d.page.wait_for_selector(alert_sel, timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        a = d.eval("(document.querySelector(%s) || {}).textContent || ''" % json.dumps(alert_sel))
        check("an alert says runs are creating their own work", "Runs are creating their own work" in a, a[:300])
        check("it names how many of the cards runs filed", ("%d of the %d cards" % (cr["by_run"], cr["created"])) in a, (a[:300], cr))
        check("at a rate of 1 or more it is bad, not a warning", d.eval("(document.querySelector(%s) || {classList: {contains: () => false}}).classList.contains('bad')" % json.dumps(alert_sel)))
        check("the alert is at the top of the page, before the tiles",
              d.eval("(() => { const a = document.querySelector(%s), g = document.querySelector('#view-activity .act-grid');"
                     " return !!(a && g && (a.compareDocumentPosition(g) & Node.DOCUMENT_POSITION_FOLLOWING)); })()" % json.dumps(alert_sel)))
        t = d.eval("(document.querySelector(%s) || {}).textContent || ''" % json.dumps(created_sel))
        check("the block says how many were created by a run", ("%d of %d" % (cr["by_run"], cr["created"])) in t and "bot" in t, t[:300])
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
