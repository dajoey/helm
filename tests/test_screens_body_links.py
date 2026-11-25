"""Playwright drive: a bare http(s) address in a report or note is a link.

Run: python3 tests/test_screens_body_links.py

Old the earlier app auto-linked bare URLs, and the client executor's prompt relies on it ("Mentioning
a URL in prose inside note still gets auto-linked"). Here renderBody (web/app.js) linked only
markdown [label](url), so a bare address in a bot report was plain text to copy by hand. This
drives both sites: a thread whose report and note carry bare addresses opens as its
conversation, and each address is an <a target=_blank rel=noopener> with trailing sentence
punctuation left outside it (an agent's note, a system line, links the same way); [label](url)
still links, relative ones too; a javascript: (even split by a tab) or data: address, bare or in
a markdown link, is never an anchor.
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


REPORT = ("Done. See https://example.com/a/b. The run log is at http://example.org/runs?id=7&x=1, "
          "and the PR (https://example.net/pr/12) is merged.\n"
          "Docs: [the playbook](https://example.com/playbook) and <https://example.com/angle>! [this thread](#/threads/t-bare-urls)\n"
          "Not these: javascript:alert(1) and [bad](javascript:alert(2)) and [data](data:text/html,hi) "
          "and [tab](java\tscript:alert(3)) and ftp://example.com/f and `https://example.com/in-code`")
NOTE = "Also look at https://example.com/wiki/Page_(disambiguation) when you can"

# href -> text of every anchor the conversation should show
WANT = {
    "https://example.com/a/b": "https://example.com/a/b",
    "http://example.org/runs?id=7&x=1": "http://example.org/runs?id=7&x=1",
    "https://example.net/pr/12": "https://example.net/pr/12",
    "https://example.com/playbook": "the playbook",
    "#/threads/t-bare-urls": "this thread",
    "https://example.com/angle": "https://example.com/angle",
    "https://example.com/wiki/Page_(disambiguation)": "https://example.com/wiki/Page_(disambiguation)",
}

ANCHORS = """[...document.querySelectorAll('.conv-timeline a')].map(a => ({
  href: a.getAttribute('href') || '', text: a.textContent, target: a.target, rel: a.rel,
  after: (a.nextSibling && a.nextSibling.textContent || '').slice(0, 2)}))"""


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        for name, site in (("home", st.home), ("work", st.home)):
            print(f"\n{name}: bare addresses in a report and a note")
            now = time.time()
            api(site, "/api/triage/threads", {"threads": [{
                "thread_id": "t-bare-urls", "title": "Links in a report", "summary": "Links in a report",
                "category": "Links", "urgency": "whenever", "state": "needs-you", "responder": "sweep:links",
                "ask": {"kind": "ack", "prompt": "Read it", "requires": ["decision"],
                        "options": [{"label": "Got it", "value": "ack", "effect": "Closes the card"}]},
                "sources": [{"source": "links", "id": name}],
                "events": [{"ts": now - 120, "source": "sweep:links", "kind": "bot-result", "body": REPORT},
                           {"ts": now - 60, "source": "sweep:links", "kind": "note", "body": NOTE}]}]}, "PUT")
            d = Drive(browser, site, WIDE, "#/threads/t-bare-urls")
            d.wait(".conv-timeline .conv-msg")
            d.until("document.querySelector('.conv-timeline').textContent.includes('disambiguation')")
            got = d.eval(ANCHORS)
            by = {a["href"]: a for a in got}
            for href, text in WANT.items():
                a = by.get(href)
                check(f"{name}: {href} is a link reading {text!r}", a is not None and a["text"] == text, got)
                check(f"{name}: {href} opens in a new tab, noopener",
                      a is not None and a["target"] == "_blank" and "noopener" in a["rel"], a)
            a = by.get("https://example.com/a/b")
            check(f"{name}: the full stop after a bare address stays outside the link",
                  a is not None and a["after"].startswith("."), a)
            a = by.get("http://example.org/runs?id=7&x=1")
            check(f"{name}: the comma after a bare address stays outside the link",
                  a is not None and a["after"].startswith(","), a)
            a = by.get("https://example.net/pr/12")
            check(f"{name}: a bare address in parentheses leaves the ')' outside",
                  a is not None and a["after"].startswith(")"), a)
            bad = [a for a in got if not a["href"].lower().startswith(("http://", "https://", "#/"))]
            check(f"{name}: no javascript: (even tab-split), data: or ftp: address is an anchor", not bad, bad)
            check(f"{name}: nothing else became a link", sorted(by) == sorted(WANT), sorted(by))
            text = d.eval("document.querySelector('.conv-timeline').textContent")
            check(f"{name}: the refused addresses still read as text",
                  "javascript:alert(1)" in text and "ftp://example.com/f" in text and "[bad](javascript:alert(2))" in text, text[:400])
            check(f"{name}: an address inside `code` stays code, not a link",
                  d.eval("[...document.querySelectorAll('.conv-timeline code')].some(c => c.textContent === 'https://example.com/in-code')"))
            check(f"{name}: no page errors", not d.errors, d.errors)
            d.close()
        browser.close()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
