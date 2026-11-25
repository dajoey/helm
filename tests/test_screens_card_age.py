"""Playwright drive: a card says how old its question is, from the store's times, never from prose.

Run: python3 tests/test_screens_card_age.py

A card that had waited two weeks read exactly like one filed a minute ago: the list row showed
last_change (which moves on any touch), and the card said nothing about age. Now the row shows
when the question was put (catchup.lit_at), and the open card carries a strip under the title:
how long ago it was asked, how many times, how many agent updates since and when, and when the owner
last wrote on it. Past a day the age turns amber; past a week, red.
Home site, synthetic data (tools/screens/fixture.py); the card is aged by writing the store.
"""
from __future__ import annotations

import os
import re
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
from helm import store  # noqa: E402
from test_screens_drive import PHONE, WIDE, Drive  # noqa: E402

passed = failed = 0
OLD = "t-media-box-disk"


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def age_the_card(db):
    """Put the question 14 days back, with three agent updates since and a reply from the owner 9 days ago."""
    con = store.db(db)
    now = time.time()
    con.execute("UPDATE threads SET ask_lit_at=? WHERE thread_id=?", (now - 14 * 86400, OLD))
    for i, days in enumerate((10, 5, 0.1)):
        store.add_event(con, OLD, now - days * 86400, "bot", "", "bot-result", "checked, still waiting %d" % i)
    store.add_event(con, OLD, now - 9 * 86400, "owner", "", "owner-note", "not yet")
    con.commit()
    con.close()


def open_card(d, tid):
    """Click a row and wait until the open card IS that row's: on a wide screen the page has already opened the first card."""
    sel = '#list-needs-you .lrow[data-id="%s"]' % tid
    want = d.eval("(document.querySelector('%s .lask') || {}).textContent || ''" % sel)
    d.page.click(sel)
    d.wait(".conv-live-ask")
    d.page.wait_for_function("(w) => ((document.querySelector('.conv-live-prompt') || {}).textContent || '').trim() === w.trim()", arg=want, timeout=15000)


def strip(d):
    return d.eval("(() => { const s = document.querySelector('.conv-live-ask .conv-catchup');"
                  " return s ? {text: s.textContent, stale: !!s.querySelector('.conv-cu-stale'), old: !!s.querySelector('.conv-cu-old'),"
                  " w: s.scrollWidth, cw: s.clientWidth} : null; })()")


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        age_the_card(st.home.db)

        print("\n1 · the list row shows when the question was put")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you .lrow.needs-you")
        age = d.eval("(document.querySelector('#list-needs-you .lrow[data-id=\"%s\"] .lage') || {}).textContent" % OLD)
        check("the aged card reads 14d, not the time of its last touch", age == "14d ago", age)
        others = d.eval("[...document.querySelectorAll('#list-needs-you .lrow:not([data-id=\"%s\"]) .lage')].map(e => e.textContent)" % OLD)
        check("the other cards are young", all(a in ("just now",) or a.endswith("m ago") or a.endswith("h ago") for a in others), others)

        print("\n2 · the open card carries the strip")
        open_card(d, OLD)
        s = strip(d)
        check("a strip is there", s is not None, s)
        if s:
            check("it says asked 14d ago", "asked 14d ago" in s["text"], s["text"])
            check("14 days is red", s["stale"], s)
            m = re.search(r"(\d+) updates since, last (\d+)h ago", s["text"])
            check("the agent updates since are counted (the three written here plus the fixture's own report)",
                  bool(m) and int(m.group(1)) >= 3, s["text"])
            check("the owner's last word is dated", "you last wrote 9d ago" in s["text"], s["text"])
        d.close()

        print("\n3 · a young card is not alarmed")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you .lrow.needs-you")
        young = d.eval("(document.querySelector('#list-needs-you .lrow.needs-you:not([data-id=\"%s\"])') || {}).dataset.id" % OLD)
        open_card(d, young)
        s = strip(d)
        check("it has a strip that says asked", s is not None and "asked" in s["text"], s)
        check("and is neither amber nor red", s is not None and not s["stale"] and not s["old"], s)
        d.close()

        print("\n4 · on a phone the strip wraps instead of overflowing")
        d = Drive(browser, st.home, PHONE, "#/needs-you")
        d.wait("#list-needs-you .lrow.needs-you")
        open_card(d, OLD)
        s = strip(d)
        check("no horizontal overflow", s is not None and s["w"] <= s["cw"] + 1, s)
        check("no page errors", not d.errors, d.errors)
        print("\n4b · a thread that is not lit for him is aged from its last change")
        ages = d.eval("[askedAt({state: 'needs-you', last_change: 100, catchup: {lit_at: 50}}),"
                      " askedAt({state: 'working', last_change: 100, catchup: {lit_at: 50}}),"
                      " askedAt({state: 'done', last_change: 100, catchup: {lit_at: 50}}),"
                      " askedAt({state: 'needs-you', last_change: 100})]")
        check("needs-you counts from the question; answered, working and done from their last change; no catchup falls back",
              ages == [50, 100, 100, 100], ages)
        d.close()

        print("\n5 · a producer's subject and recap replace the machine title and the boilerplate summary")
        con = store.db(st.home.db)
        key = con.execute("SELECT ask_lit_key FROM threads WHERE thread_id=?", (OLD,)).fetchone()[0]
        import json as _json
        con.execute("UPDATE threads SET ask_meta=? WHERE thread_id=?",
                    (_json.dumps({"subject": "Media box disk", "recap": "The recorder volume is nearly full and will stop recording soon. "
                                  "Only you can choose between deleting old recordings and growing the volume.", "key": key}), OLD))
        con.commit()
        con.close()
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you .lrow.needs-you")
        title = d.eval("(document.querySelector('#list-needs-you .lrow[data-id=\"%s\"] .ltitle') || {}).textContent" % OLD)
        check("the row is named by the subject", title == "Media box disk", title)
        open_card(d, OLD)
        recap = d.eval("(document.querySelector('.conv-live-ask .conv-recap-body') || {}).textContent")
        check("the recap is on the card", recap and recap.startswith("The recorder volume is nearly full"), recap)
        check("it replaces the boilerplate About line", d.eval("!document.querySelector('.conv-live-ask .conv-about')"))
        check("the context is folded behind Details when a recap leads", d.eval("(() => { const x = document.querySelector('.conv-live-ask details.conv-ask-ctx'); return !!x && !x.open && x.querySelector('summary').textContent === 'Details'; })()"))
        d.page.click(".conv-live-ask details.conv-ask-ctx > summary")
        check("opening it shows the context", d.eval("document.querySelector('.conv-live-ask details.conv-ask-ctx').open && document.querySelector('.conv-ask-ctx-body').textContent.length > 20"))
        check("the strip names the subject too", "Media box disk" in (strip(d) or {}).get("text", ""), strip(d))
        d.close()
        browser.close()
    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
