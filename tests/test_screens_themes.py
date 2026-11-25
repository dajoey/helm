"""Playwright drives of the one fixed Helm design (formerly the theme-picker suite).

Run: python3 tests/test_screens_themes.py

At 999 x 1392 and 412 x 915 the fixed identity, palette and local fonts apply, helm.css is the
last imported sheet, no retired controls or assets load, and the existing three-pane/phone layouts
still work. T is deliberately inert: no picker, navigation, error or other state change. Notebook
note links retain their old non-boxed presentation check. Home site, synthetic data.
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
from test_screens_drive import PHONE, WIDE, Drive  # noqa: E402

passed = failed = 0
THEME = "document.documentElement.dataset.theme"
IMPORTS = """(() => { for (const s of document.styleSheets) { try { return [...s.cssRules].filter(r => r.type === CSSRule.IMPORT_RULE).map(r => new URL(r.href, location.href).pathname); } catch (e) {} } return []; })()"""
RETIRED_RESOURCES = """performance.getEntriesByType('resource').map(e => new URL(e.name, location.href).pathname).filter(p => p.startsWith('/themes/') || (p.startsWith('/snes/') && p !== '/snes/icon-32.png'))"""


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


def until(d, js, timeout=8000):
    try:
        d.page.wait_for_function(js, timeout=timeout)
        return True
    except Exception:
        return False


def fixed_contract(d, size):
    tag = "%dx%d" % size
    check("%s: html has the one fixed identity" % tag, d.eval(THEME) == "helm" and not d.eval("document.documentElement.classList.contains('snes')"))
    imports = d.eval(IMPORTS)
    check("%s: helm.css is imported once and loaded last" % tag,
          imports and imports[-1] == "/helm.css" and imports.count("/helm.css") == 1, imports)
    check("%s: no theme or sound controls exist" % tag, d.eval("!document.querySelector('#btn-theme, #btn-sound, .th-pop')"))
    retired = d.eval(RETIRED_RESOURCES)
    check("%s: no retired theme/SNES script, sheet, sprite or sound loaded" % tag, not retired, retired)
    look = d.eval("""(() => { const b = getComputedStyle(document.body), m = getComputedStyle(document.querySelector('#pane-main'));
      return {bodyFont: b.fontFamily, bodyBg: b.backgroundColor, bodyColor: b.color, mainBg: m.backgroundColor,
              uiFont: getComputedStyle(document.querySelector('.shell-name')).fontFamily}; })()""")
    check("%s: Instrument Sans body and the Newsreader brand apply" % tag,
          "Instrument Sans" in look["bodyFont"] and "Newsreader" in look["uiFont"], look)
    check("%s: the Chart Room palette paints body and main pane" % tag,
          look["bodyBg"] not in ("", "rgba(0, 0, 0, 0)", "transparent") and look["bodyBg"] == look["mainBg"] and look["bodyColor"] != look["bodyBg"], look)
    fonts = d.eval("""(async () => { await document.fonts.ready; return {
      body: document.fonts.check('15px "Instrument Sans"'), ui: document.fonts.check('600 20px "Newsreader"'),
      faces: [...document.fonts].map(f => f.family).filter((v, i, a) => a.indexOf(v) === i)}; })()""")
    check("%s: both local font families are registered and ready" % tag, fonts["body"] and fonts["ui"]
          and "Instrument Sans" in fonts["faces"] and "Newsreader" in fonts["faces"], fonts)


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        for size in (WIDE, PHONE):
            tag = "%dx%d" % size
            print("\nfixed Helm design · " + tag)
            d = Drive(browser, st.home, size, "#/needs-you")
            d.wait(".conv-live-ask .conv-opt" if size == WIDE else "#list-needs-you .lrow")
            d.page.wait_for_timeout(300)
            fixed_contract(d, size)
            if size == WIDE:
                check("999: three panes remain on screen", all(d.eval("(() => { const r = document.querySelector('%s').getBoundingClientRect(); return r.width > 150 && r.height > 600; })()" % s)
                                                                  for s in ("#pane-left", "#pane-main", "#pane-props")))
                vis = d.eval("(() => { const b = document.querySelector('.conv-live-ask .conv-opt'); if (!b) return false; const r = b.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && r.width > 60; })()")
                check("999: the first ask's options remain on screen", vis)
                d.eval("location.hash = '#/topic/Contacts%2FPine%20Hollow%20portal.md'")
                boxed = None
                if until(d, "!!document.querySelector('.linkrow.note, .linkrow.is-note')", 10000):
                    boxed = d.eval("[...document.querySelectorAll('.linkrow')].filter(r => { const c = getComputedStyle(r); return parseFloat(c.borderTopWidth) > 0 && c.borderTopStyle !== 'none' && parseFloat(c.paddingTop) >= 8; }).length")
                check("999: a note link is not drawn as a note page", boxed == 0, boxed)
                d.eval("location.hash = '#/needs-you'")
                d.wait(".conv-live-ask .conv-opt")
            wide = d.eval("document.scrollingElement.scrollWidth - innerWidth")
            check("%s: no sideways scroll" % tag, wide <= 1, wide)

            before = d.eval("({hash: location.hash, theme: document.documentElement.dataset.theme, view: document.body.dataset.view, pane: document.body.dataset.pane, dialogs: document.querySelectorAll('.th-pop, #sheet:not(.hidden), .shell-help-overlay').length})")
            errors_before = list(d.errors)
            d.eval("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
            d.page.keyboard.press("t")
            d.page.wait_for_timeout(350)
            after = d.eval("({hash: location.hash, theme: document.documentElement.dataset.theme, view: document.body.dataset.view, pane: document.body.dataset.pane, dialogs: document.querySelectorAll('.th-pop, #sheet:not(.hidden), .shell-help-overlay').length})")
            check("%s: keyboard T does nothing" % tag, before == after, (before, after))
            check("%s: keyboard T raises no error" % tag, d.errors == errors_before == [], d.errors)
            d.close()
        browser.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
