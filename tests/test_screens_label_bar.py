"""The NEEDS YOU label bar stays small however many labels there are.

Run: PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/test_screens_label_bar.py

The bar used to be one chip per label on any open thread: twenty labels filled a third of the
pane before the first card. Every label stays; only how it is reached changes:

  * five labels or fewer: every label is a chip, as before (nothing to hide);
  * more: ALL, the three labels with the most waiting on him (alphabetical, each with its count), a
    LABELS chip that opens a type-to-filter picker of every label with its count (labels with
    nothing waiting sit dimmed under a divider), and FIRE ONLY;
  * a label picked from the picker shows as one active chip ending in "x"; tapping it clears it, and
    a filter in force always has its chip, even when only one label is left;
  * a card's label is quiet text beside its urgency badge, not a second box; a click on a wide
    screen filters by it, and on a phone a tap there opens the thread like any other tap.

Synthetic data (tools/screens/fixture.py) plus fourteen fictional labels.
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


# (label, how many wait on the owner, how many are in flight). Training and Legacy have nothing waiting.
SEED = [("Contacts", 6, 0), ("Tooling", 4, 0), ("Calls", 3, 0), ("Tickets", 2, 0), ("Servers", 1, 0),
        ("Security", 1, 0), ("Personal", 1, 0), ("Training", 0, 1), ("Legacy", 0, 1)]
URG = ["now", "this-week", "whenever"]
TOP3 = ["Calls", "Contacts", "Tooling"]   # the three with the most waiting, alphabetical


def seed_labels(site):
    now = time.time()
    threads = []
    for label, n_you, n_work in SEED:
        for i in range(n_you):
            tid = "t-lbl-%s-%d" % (label.lower(), i)
            threads.append({"thread_id": tid, "title": "%s question %d" % (label, i + 1), "summary": "About " + label, "category": label,
                            "urgency": URG[i % 3], "state": "needs-you", "responder": "sweep:labels",
                            "ask": {"kind": "ack", "prompt": "Read it and close it", "requires": ["decision"],
                                    "context": "A synthetic card for the label bar's test: nothing to do but read it and close it.",
                                    "options": [{"label": "Got it", "value": "ack", "effect": "Closes the card"}]},
                            "sources": [{"source": "labels", "id": tid}],
                            "events": [{"ts": now - 600 - i, "source": "sweep:labels", "kind": "bot-result", "body": "Report: nothing to do."}]})
        for i in range(n_work):
            tid = "t-lbl-%s-w%d" % (label.lower(), i)
            threads.append({"thread_id": tid, "title": "%s job %d" % (label, i + 1), "summary": "About " + label, "category": label,
                            "urgency": "whenever", "state": "working", "responder": "agent:worker", "ask": {"kind": "none"},
                            "sources": [{"source": "labels", "id": tid}],
                            "events": [{"ts": now - 300, "source": "agent:worker", "kind": "bot-result", "body": "Claimed."}]})
    out = api(site, "/api/triage/threads", {"threads": threads}, "PUT")
    assert out.get("created") == len(threads), out


BAR = "#list-needs-you .lhead"


def bar_labels(d):
    return d.eval("[...document.querySelectorAll('%s > .chip[data-label]')].map(c => c.dataset.label)" % BAR)


def row_ids(d):
    return d.eval("[...document.querySelectorAll('#list-needs-you .lrow')].map(r => r.dataset.id)")


def row_labels(d):
    return d.eval("[...document.querySelectorAll('#list-needs-you .lrow')].map(r => (r.querySelector('.lcat') || {}).textContent)")


def pick_rows(d):
    return d.eval("[...document.querySelectorAll('%s .lpick-row')].map(r => [r.dataset.label, +r.dataset.n])" % BAR)


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())

        print("\nfew labels · nothing to hide: every label is a chip, no picker")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you .lrow")
        few = bar_labels(d)
        check("with five labels or fewer every one is a chip", 2 <= len(few) <= 5, few)
        check("and there is no LABELS chip", d.eval("!document.querySelector('%s .chip.more')" % BAR))
        d.close()

        print("\none label left · a filter in force always has a chip to clear it")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you .lrow")
        # every open thread but the Fleet ones loses its label; Fleet's gets a trailing space, as a producer's might
        d.eval("state.threads.forEach(t => { t.category = t.category === 'Fleet' ? 'Fleet ' : ''; }); render()")
        n_rows = len(row_ids(d))
        n_fleet = d.eval("state.threads.filter(t => t.state === 'needs-you' && !isSnoozed(t) && t.category === 'Fleet ').length")
        check("with a single label no label chips are drawn", bar_labels(d) == [], bar_labels(d))
        d.page.click("#list-needs-you .lrow .lcat")
        d.page.wait_for_timeout(300)
        check("a click on a card's label still leaves a chip to clear it", d.eval("!!document.querySelector('%s > .chip.on[data-label=\"Fleet\"]')" % BAR), bar_labels(d))
        check("the label's own cards stay in the list (a stray space in the label doesn't hide them)", len(row_ids(d)) == n_fleet and n_fleet > 0, (len(row_ids(d)), n_fleet))
        check("and the others leave it", n_fleet < n_rows, (n_fleet, n_rows))
        d.page.click("%s > .chip.on[data-label='Fleet']" % BAR)
        d.page.wait_for_timeout(300)
        check("and it clears the filter", d.eval("state.categories.size") == 0 and len(row_ids(d)) == n_rows, (len(row_ids(d)), n_rows))
        d.close()

        seed_labels(st.home)
        all_labels = sorted(set(few) | {s[0] for s in SEED})

        print("\nmany labels · the bar stays small (home, 999x1392)")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you .lrow")
        d.page.wait_for_timeout(400)
        check("the bar holds only the three labels with the most waiting", bar_labels(d) == TOP3, bar_labels(d))
        n_chips = d.eval("document.querySelectorAll('%s > .chip').length" % BAR)
        check("ALL, three labels, LABELS and FIRE ONLY: six chips, not one per label", n_chips == 6, n_chips)
        check("each pinned chip says how many wait", d.eval("[...document.querySelectorAll('%s > .chip[data-label]')].map(c => c.querySelector('.chip-n') && c.querySelector('.chip-n').textContent)" % BAR) == ["3", "6", "4"])
        check("a label with nothing waiting is not in the bar", "Training" not in bar_labels(d))
        h = d.eval("document.querySelector('%s').getBoundingClientRect().height" % BAR)
        print("  [bar height %.0f px]" % h)
        check("the bar is short enough that cards stay in view", h < 120, h)
        check("the first card starts near the top of the pane", d.eval("document.querySelector('#list-needs-you .lrow').getBoundingClientRect().top - document.querySelector('#list-needs-you').getBoundingClientRect().top") < 150)
        check("the picker is closed until asked for", d.eval("!document.querySelector('.lpick')"))
        n_all = len(row_ids(d))

        print("\nthe picker")
        d.page.click("%s > .chip.more" % BAR)
        check("LABELS opens the picker", d.until("!!document.querySelector('.lpick')"))
        check("the picker lists every label", sorted(r[0] for r in pick_rows(d)) == all_labels, pick_rows(d))
        rows = pick_rows(d)
        counts = [n for _, n in rows]
        check("sorted by how many are waiting", counts == sorted(counts, reverse=True), counts)
        check("Contacts leads with 6", rows[0] == ["Contacts", 6], rows[:2])
        check("labels with nothing waiting are dimmed under a divider and say 0", {r[0] for r in rows if r[1] == 0} >= {"Training", "Legacy"}
              and d.eval("!!document.querySelector('.lpick .lgroup')"), rows)
        check("a snoozed card is not counted (Notebook's only card sleeps)", ["Notebook", 0] in rows, rows)
        check("on a wide screen the search box takes focus", d.eval("document.activeElement && document.activeElement.closest('.lpick') !== null"))
        d.page.keyboard.type("tick")
        check("typing filters the list", [r[0] for r in pick_rows(d)] == ["Tickets"], pick_rows(d))
        d.page.keyboard.press("Control+a")
        d.page.keyboard.type("se")
        d.eval("render()")   # the 60 s poll
        d.page.wait_for_timeout(200)
        check("a redraw keeps what was typed and the focus", d.eval("document.querySelector('.lpick input').value") == "se"
              and d.eval("document.activeElement === document.querySelector('.lpick input')"))
        d.page.keyboard.press("Escape")
        check("Escape closes the picker and stays on NEEDS YOU", d.until("!document.querySelector('.lpick')") and d.hash().startswith("#/needs-you"), d.hash())

        d.page.click("%s > .chip.more" % BAR)
        d.until("!!document.querySelector('.lpick')")
        d.page.click("#list-needs-you .lrow:nth-of-type(3)")
        check("opening a card closes the picker", d.until("!document.querySelector('.lpick')"))
        d.page.click("%s > .chip.more" % BAR)
        d.until("!!document.querySelector('.lpick')")
        d.page.fill(".lpick input", "serv")
        d.page.click(".lpick .lpick-row[data-label='Servers']")
        d.page.wait_for_timeout(300)
        check("picking a label filters the list to it", row_ids(d) == ["t-lbl-servers-0"], row_ids(d))
        check("and closes the picker", d.eval("!document.querySelector('.lpick')"))
        check("the pick shows as one active chip ending in a cross", d.eval("(() => { const c = document.querySelector('%s > .chip.on[data-label=\"Servers\"]'); return !!c && c.textContent.trim().endsWith('×'); })()" % BAR))
        check("the bar did not grow", d.eval("document.querySelectorAll('%s > .chip').length" % BAR) == 7, d.eval("document.querySelectorAll('%s > .chip').length" % BAR))
        d.page.click("%s > .chip.on[data-label='Servers']" % BAR)
        d.page.wait_for_timeout(300)
        check("tapping the active chip clears the filter", len(row_ids(d)) == n_all and bar_labels(d) == TOP3, (len(row_ids(d)), n_all, bar_labels(d)))

        d.page.click("%s > .chip[data-label='Contacts']" % BAR)
        d.page.wait_for_timeout(300)
        check("a pinned chip filters too", set(row_labels(d)) == {"Contacts"} and len(row_ids(d)) == 6, row_labels(d))
        d.page.click("%s > .chip:has-text('ALL')" % BAR)
        d.page.wait_for_timeout(300)
        check("ALL clears it", len(row_ids(d)) == n_all)

        print("\nthe counts")
        d.page.click("%s > .chip:has-text('FIRE ONLY')" % BAR)
        d.page.wait_for_timeout(300)
        members = d.eval("(() => { const c = document.querySelector('%s > .chip[data-label=\"Contacts\"] .chip-n'); return c && c.textContent; })()" % BAR)
        check("under FIRE ONLY a chip counts only the NOW cards (2 of Contacts' 6)", members == "2", members)
        d.page.click("%s > .chip:has-text('FIRE ONLY')" % BAR)
        d.page.wait_for_timeout(300)
        check("and FIRE ONLY off counts them all again", d.eval("document.querySelector('%s > .chip[data-label=\"Contacts\"] .chip-n').textContent" % BAR) == "6")

        print("\nthe card's label")
        check("a card's label is plain text beside its urgency badge, not a second box",
              d.eval("(() => { const r = document.querySelector('#list-needs-you .lrow .lmeta'); const l = r.querySelector('.lcat');"
                     " return !!l && !l.classList.contains('badge') && l.previousElementSibling && l.previousElementSibling.classList.contains('badge'); })()"))
        check("no card in the list carries a label box", d.eval("!document.querySelector('#list-needs-you .cat-badge')"))
        check("the label is still there on every card", all(row_labels(d)), row_labels(d))
        before = d.hash()
        lab = d.eval("document.querySelector('#list-needs-you .lrow .lcat').textContent")
        d.page.click("#list-needs-you .lrow .lcat")
        d.page.wait_for_timeout(300)
        check("a click on a card's label filters by it", set(row_labels(d)) == {lab} and len(row_ids(d)) >= 1, (lab, row_labels(d)))
        check("without opening the card", d.hash() == before, (before, d.hash()))
        d.close()

        print("\nthe same on the phone (412x915)")
        d = Drive(browser, st.home, PHONE, "#/needs-you")
        d.wait("#list-needs-you .lrow")
        check("the phone's bar is the same short bar", bar_labels(d) == TOP3, bar_labels(d))
        d.page.click("#list-needs-you .lrow .lcat")
        h = d.wait_hash(lambda h: h.startswith("#/needs-you/"))
        check("a tap on a card's label opens the card, as any tap on the row does", h.startswith("#/needs-you/"), h)
        check("and sets no filter", d.eval("state.categories.size") == 0)
        d.close()

        print("\nthe same bar again, wide (999x1392)")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you .lrow")
        d.page.wait_for_timeout(400)
        wl = bar_labels(d)
        check("the bar is capped here too: three labels and LABELS", len(wl) == 3 and d.eval("!!document.querySelector('%s > .chip.more')" % BAR), wl)
        d.close()

        print("\nthe fixed design draws the bar and the picker")
        d = Drive(browser, st.home, WIDE, "#/needs-you")
        d.wait("#list-needs-you .lrow")
        d.page.click("%s > .chip.more" % BAR)
        ok = d.until("!!document.querySelector('.lpick .lpick-row')")
        r = d.eval("(() => { const p = document.querySelector('.lpick'); if (!p) return null; const b = p.getBoundingClientRect(); const l = document.querySelector('#list-needs-you').getBoundingClientRect();"
                   " return {w: b.width, lw: l.width, left: b.left - l.left, h: b.height}; })()")
        check("the picker opens inside the pane and fits it", ok and r and r["left"] >= -1 and r["w"] <= r["lw"] + 1, r)
        check("the page had no errors", not d.errors, d.errors[:2])
        d.close()

        browser.close()

    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
