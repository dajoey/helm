#!/usr/bin/env python3
"""The service worker caches exactly the fixed Helm shell, its local fonts and its favicon.

Run: python3 tests/test_sw_shell.py (needs Playwright's Chromium, but no node).

The SHELL list is evaluated by the worker's own JavaScript with stubbed worker globals. The
required set below is derived from index.html plus the explicitly cached feature assets; it is
not an arbitrary size threshold. Retired theme sheets, scripts, sprites and sounds are forbidden.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "screens"))

if os.environ.get("HELM_SKIP_DRIVES") == "1":
    print("  FAIL the drives were skipped (HELM_SKIP_DRIVES=1): this suite never passes quietly")
    print("0 passed, 1 failed")
    sys.exit(1)

from playwright.sync_api import sync_playwright  # noqa: E402

import shoot  # noqa: E402

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
# The cached fonts are whatever helm.css declares, so adding or dropping a face never needs a second edit here.
FONT_NAMES = sorted(re.findall(r'url\("fonts/([^"]+\.woff2)"\)', (WEB / "helm.css").read_text(encoding="utf-8")))
FONTS = ["/fonts/" + n for n in FONT_NAMES]
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:500] if detail else ""))


# sw.js run as a plain script in a page, with the worker's globals stubbed.
SHELL_JS = """(src) => {
  const stub = { addEventListener() {} };
  return new Function("self", "caches", "fetch", "URL", src + "\\n;return SHELL;")(stub, {}, () => {}, URL);
}"""


def shell_list(browser=None) -> list[str]:
    """SHELL as the worker's own code builds it, using an existing Chromium when supplied."""
    src = (WEB / "sw.js").read_text(encoding="utf-8")

    def read(b):
        page = b.new_page()
        try:
            page.goto("about:blank")
            return page.evaluate(SHELL_JS, src)
        finally:
            page.close()

    if browser is not None:
        out = read(browser)
    else:
        with sync_playwright() as p:
            b = p.chromium.launch(**shoot.launch_args())
            try:
                out = read(b)
            finally:
                b.close()
    return [u for u in out if isinstance(u, str)] if isinstance(out, list) else []


def required_shell() -> list[str]:
    """Every URL the fixed shell must cache, with no legacy theme assets."""
    index = (WEB / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="(/[^"]+)"', index)
    sheets = re.findall(r'@import url\("(/[^"]+\.css)"\)', index) + re.findall(r'<link rel="stylesheet" href="(/[^"]+)"', index)
    extras = [
        "/", "/index.html", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png",
        "/notebook/app.js", "/notebook/lib.js", "/notebook/card.js", "/notebook/editor.js", "/vendor/cm.js",
        "/snes/icon-32.png",
    ]
    return sorted(set(scripts + sheets + extras + FONTS))


def main() -> int:
    shell = shell_list()
    required = required_shell()
    have = set(shell)
    want = set(required)
    check("sw.js evaluates the complete required SHELL (%d entries)" % len(required), have == want,
          {"missing": sorted(want - have), "unexpected": sorted(have - want), "count": len(shell)})
    check("the design-system sheet, the glyph script and every declared local font file are cached",
          "/helm.css" in have and "/glyphs.js" in have and bool(FONTS) and all(u in have for u in FONTS))
    retired = sorted(u for u in have if u.startswith("/themes/") or (u.startswith("/snes/") and u != "/snes/icon-32.png"))
    check("no retired theme, SNES, sprite or sound assets are cached", not retired, retired)
    nofile = [u for u in shell if u != "/" and not (WEB / u.lstrip("/")).is_file()]
    check("every SHELL entry is a file that exists", not nofile, nofile)
    dup = sorted({u for u in shell if shell.count(u) > 1})
    check("no entry twice", not dup, dup)
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
