"""Playwright drives: the sign-in lapses under an open page (2026-09-26).

Run: python3 tests/test_screens_session.py

the owner lost a message while typing it. the app's Authentik proxy session lasts one
hour; when it lapsed, the poll's 302 sent the whole tab through /outpost.goauthentik.io/start
(rd=/). Authentik, still signed in, bounced straight back, so the page "refreshed by itself",
landed on NEEDS YOU, and the text was gone (live logs 14:39:34 lapse, 14:40:07 start, 14:40:08
reload; the same at 14:07:59).

Proves: a lapsed session
is renewed in a hidden frame (the tab never navigates, the typed text stays, the read that met
the lapse is retried); when Authentik itself is signed out (its login page refuses to be
framed) a page with text typed stays put and offers the sign-in, and the sign-in comes back to
the same screen; with nothing typed it signs in again on its own, also back to the same screen.
Browser storage that refuses writes, or is blocked outright (reads throw a SecurityError too),
never stops the page: the send goes, the inbox loads and answers, and the fixed Helm design stays on.
Home site, synthetic data (tools/screens/fixture.py). Caddy's forward_auth and Authentik are
played by request routing; the service worker is blocked so the routing sees every request.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.request
from urllib.parse import parse_qs, quote, urlparse

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
from test_screens_drive import HEADERS, WIDE, Drive  # noqa: E402

passed = failed = 0
TEXT = "half a note on a card, not sent yet"


# The card the drives open: a working thread the fixture holds, its composer takes a note.
FD_ROUTE = "#/threads/t-monitor-dedupe"

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


class SessionDrive(Drive):
    """Drive with the service worker blocked: a worker-fetched request never reaches page.route.

    `before(d)` runs after the page exists and before it loads, so routes it adds see the first
    load's calls too: the shell asks /api/meta while its script runs, before DOMContentLoaded,
    and a route added after goto() missed it (and was added with that fetch in flight)."""

    def __init__(self, browser, site, size, route, before=None):
        self.site = site
        self.ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1,
                                       extra_http_headers=HEADERS, locale="en-US", timezone_id="America/New_York",
                                       service_workers="block")
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.pending = {}         # request -> (path, started), for the stall report
        self.page.on("request", lambda r: self.pending.__setitem__(r, (urlparse(r.url).path, time.time())))
        self.page.on("requestfinished", lambda r: self.pending.pop(r, None))
        self.page.on("requestfailed", lambda r: self.pending.pop(r, None))
        if before:
            before(self)
        self.page.goto(site.web_url + "/" + route, wait_until="domcontentloaded")


class Outpost:
    """Caddy's forward_auth and Authentik, as the browser meets them.

    While `lapsed`, every /api/ request is answered the way the Caddyfile answers it: 302 to
    /outpost.goauthentik.io/start?rd=<uri>. A fetch or an EventSource cannot follow that chain
    (the next hop is auth.example.com, cross-origin), so only a navigation, of the tab or of a
    frame, reaches Authentik. Signed in, Authentik is a pure 302 chain straight back to rd with
    a fresh session; signed out, it draws its login page, which refuses to be framed."""

    def __init__(self, d, signed_in=True, hold=()):
        self.hold = list(hold)    # path prefixes whose FIRST request is parked until release()
        self.parked = []
        self.lapsed = False
        self.signed_in = signed_in
        self.starts = []          # (url, main_frame) of each navigation that reached /start
        self.after = []           # /api/ paths that went through after a renewal
        self.renewed = False
        self.held = []            # requests parked by ?held=1, released by the test
        self.seen = []            # every /api/ path that reached forward_auth
        d.page.route("**/api/**", self.api)
        d.page.route("**/outpost.goauthentik.io/**", self.outpost)

    def api(self, route, request):
        self.seen.append(urlparse(request.url).path)
        if "held=1" in request.url:
            self.held.append(route)   # sent before the lapse, answered after it
            return None
        path = urlparse(request.url).path
        for pre in list(self.hold):
            if path.startswith(pre):
                self.hold.remove(pre)
                self.parked.append((route, request))
                return None
        if self.lapsed:
            u = urlparse(request.url)
            uri = u.path + ("?" + u.query if u.query else "")
            return route.fulfill(status=302, headers={"Location": "/outpost.goauthentik.io/start?rd=" + quote(uri, safe="")})
        if self.renewed:
            self.after.append(urlparse(request.url).path)
        return route.continue_()

    def release(self):
        """Answer the parked first-load requests as forward_auth would NOW (a lapse included)."""
        parked, self.parked = self.parked, []
        for route, request in parked:
            self.api(route, request)

    def outpost(self, route, request):
        if not request.is_navigation_request():
            return route.abort()
        self.starts.append((request.url, request.frame.parent_frame is None))
        if self.signed_in:
            self.lapsed = False
            self.renewed = True
            rd = parse_qs(urlparse(request.url).query).get("rd", ["/"])[0]
            return route.fulfill(status=302, headers={"Location": rd})
        return route.fulfill(status=200, content_type="text/html", headers={"X-Frame-Options": "DENY"},
                             body="<!doctype html><title>authentik</title><h1>Sign in to continue</h1>")

    def top_rd(self):
        tops = [u for u, main in self.starts if main]
        return parse_qs(urlparse(tops[-1]).query).get("rd", [None])[0] if tops else None


def note(d):
    """The composer's text, or None when the tab is no longer on the app."""
    try:
        return d.eval("(document.querySelector('.conv-note') || {}).value ?? null")
    except Exception:
        return None


def stayed(d):
    try:
        return d.eval("window.__stayed") == 1
    except Exception:
        return False


def wait_py(d, pred, timeout=10.0):
    """Wait on the page's clock: the routes above only run while Playwright is being called."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        try:
            d.page.wait_for_timeout(100)
        except Exception:
            time.sleep(0.1)
    return False


def poll(d, leaving_ok=False):
    """What the 60 s timer does, now: the app's refresh (after the load in flight, which a click would join).

    Any call of the page's own can be the first to meet a lapse (its first load's /api/triage/gate
    was, 1 run in 17 on 6039227). Where the scene expects the tab to go and sign in, the poll may
    land on a page already leaving: that navigation is the outcome under test, not an error, so
    those scenes pass leaving_ok=True and only it is let through; the scene's checks judge where
    the tab went. Every other scene still fails on it."""
    try:
        d.page.wait_for_function("!document.querySelector('#refresh').classList.contains('spin')", timeout=10000)
    except Exception:
        pass
    try:
        d.eval("document.querySelector('#refresh') && document.querySelector('#refresh').click()")
    except Exception as e:
        if not (leaving_ok and "Execution context was destroyed" in str(e)):
            raise


OPENS = []   # (scene, seconds to an open card): read with a stall to tell slow from stuck
SCENE = [""]


def scene(title):
    SCENE[0] = title
    print("\n" + title)


def _meta_ms(url):
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/api/meta", headers=HEADERS), timeout=10) as r:
            return "%d in %d ms" % (r.status, (time.time() - t0) * 1000)
    except Exception as e:
        return "%s after %d ms" % (type(e).__name__, (time.time() - t0) * 1000)


def _stalled(d, st, stage):
    """Say why the card did not open: which calls are still out, and how the host is doing."""
    now = time.time()
    out = sorted("%s %.1fs" % (path, now - t0) for path, t0 in list(d.pending.values()))
    try:
        ready = d.eval("!!(window.Helm && window.Helm.site)")
    except Exception as e:
        ready = "eval failed: %s" % e
    try:
        load = "%.1f %.1f %.1f" % os.getloadavg()
    except OSError:
        load = "?"
    print("  [card stalled at %s: pending=%s shell_ready=%s meta web=%s api=%s load=%s opens=%s]" % (
        stage, out[:8], ready, _meta_ms(st.home.web_url), _meta_ms(st.home.api_url), load,
        ["%s %.1fs" % o for o in OPENS[-4:]]))


def open_desk(browser, st, signed_in=True, hold=()):
    """The card, open, with the outpost (forward_auth + Authentik) in front of it from the first call."""
    ops, t0 = [], time.time()
    d = SessionDrive(browser, st.home, WIDE, FD_ROUTE, before=lambda d: ops.append(Outpost(d, signed_in, hold)))
    # A loaded host opens it late. First the shell (it waits on /api/meta), then the timeline.
    if not d.until("!!(window.Helm && window.Helm.site)", timeout=20000):
        _stalled(d, st, "the shell (meta)")
    try:
        d.wait("#view-conv .conv-timeline .conv-msg", timeout=20000)
    except Exception:
        _stalled(d, st, "the timeline")
        raise
    OPENS.append((SCENE[0], time.time() - t0))
    return d, ops[0]


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())

        scene("browser storage refuses writes: the message still goes")
        d, _ = open_desk(browser, st)
        posts = []
        d.page.route("**/api/triage/threads/*/reply", lambda route, request: (posts.append(1), route.continue_()))
        d.eval("void (Storage.prototype.setItem = function () { throw new DOMException('full', 'QuotaExceededError'); })")
        d.page.fill(".conv-note", "sent even when storage is full")
        d.page.press(".conv-note", "Enter")
        wait_py(d, lambda: bool(posts), 10)
        try:
            d.page.wait_for_function("[...document.querySelectorAll('.conv-timeline .conv-msg.mine')].some(n => n.textContent.includes('sent even when storage is full'))", timeout=15000)
            arrived = True
        except Exception:
            arrived = False
        check("the send went out", bool(posts), posts)
        check("...and arrived", arrived)
        d.eval("window.__stayed = 1")
        poll(d)
        d.page.wait_for_timeout(1500)
        check("the refresh did not reload the tab", stayed(d))
        check("a refresh with storage refusing is not an outage (answers stay allowed)",
              not d.eval("document.body.classList.contains('offline')"), d.eval("(document.querySelector('.banner') || {}).textContent || ''"))
        check("no page errors from storage", not [e for e in d.errors if "full" in e or "Quota" in e], d.errors[:3])
        # The fixed shell does not depend on storage: rendering and navigation still work when writes refuse.
        check("the fixed design stays on Helm", d.eval("document.documentElement.dataset.theme") == "helm")
        check("no retired theme or sound control needs storage", d.eval("!document.querySelector('#btn-theme, #btn-sound')"))
        gone = d.eval("""(() => { const t = state.threads.find(x => x.state !== 'done'); if (!t) return false;
                          state.threads = state.threads.filter(x => x !== t); render(); return true; })()""")
        check("a thread leaving the synthetic state still redraws", gone and d.eval("!!document.querySelector('#panes')"))
        d.page.click(".shell-brand")
        check("navigation still works with storage refusing writes", d.wait_hash(lambda h: h.startswith("#/home")).startswith("#/home"), d.hash())
        check("no page errors from the fixed shell's redraw", not [e for e in d.errors if "full" in e or "Quota" in e], d.errors[:3])
        d.close()

        scene("the session lapses while signed in to Authentik: renewed in the background")
        d, op = open_desk(browser, st, signed_in=True)
        d.page.fill(".conv-note", TEXT)
        d.eval("window.__stayed = 1")
        op.lapsed = True
        poll(d)
        wait_py(d, lambda: op.renewed, 10)
        wait_py(d, lambda: any(p.startswith("/api/triage/threads") for p in op.after), 10)
        d.page.wait_for_timeout(500)
        check("Authentik was visited", bool(op.starts), op.starts)
        check("...in a frame, never by the tab", op.starts and not any(main for _, main in op.starts), op.starts)
        check("the page never navigated", stayed(d))
        check("the typed text is still in the box", note(d) == TEXT, note(d))
        check("still on the card", d.hash() == FD_ROUTE, d.hash())
        check("the read that met the lapse was retried and went through",
              any(p.startswith("/api/triage/threads") for p in op.after), op.after[:6])
        check("no signed-out notice", not d.eval("!!document.querySelector('.sso-banner:not([hidden])')"))
        # The renewal's frame is removed once its redirected read lands; on a loaded runner that
        # lags the renewal itself (CI run 565: still present at the 500 ms mark). Wait for it to
        # go, then assert; a frame that never goes still fails the check, with where it points.
        d.until("document.querySelectorAll('iframe').length === 0", timeout=15000)
        check("the hidden frame is gone", d.eval("document.querySelectorAll('iframe').length") == 0,
              d.eval("[...document.querySelectorAll('iframe')].map(f => f.src.slice(0, 80))"))
        check("no page errors", not d.errors, d.errors[:3])
        d.close()

        scene("signed out of Authentik with text typed: the page stays and asks")
        d, op = open_desk(browser, st, signed_in=False)
        d.page.fill(".conv-note", TEXT)
        d.eval("window.__stayed = 1")
        d.eval("void ssoFetch('/api/meta?held=1').then((r) => { window.__held = r.status; })")   # void: evaluate must not wait on it
        wait_py(d, lambda: bool(op.held), 5)
        op.lapsed = True
        poll(d)
        try:
            d.page.wait_for_selector(".sso-banner:not([hidden])", timeout=20000)
            banner = True
        except Exception:
            banner = False
        check("a signed-out notice shows", banner)
        for r in op.held:
            r.continue_()
        d.page.wait_for_function("window.__held === 200", timeout=10000)
        d.page.wait_for_timeout(300)
        check("a call sent before the lapse, answered after it, does not hide the notice",
              d.eval("!!document.querySelector('.sso-banner:not([hidden])')"))
        check("the page did not navigate on its own", stayed(d) and not any(main for _, main in op.starts), op.starts)
        check("the typed text is still in the box", note(d) == TEXT, note(d))
        if banner:
            d.page.click(".sso-banner .sso-signin")
            wait_py(d, lambda: op.top_rd() is not None, 10)
        check("its sign-in comes back to the card", op.top_rd() == "/" + FD_ROUTE, op.top_rd())
        d.close()

        scene("signed out with nothing typed: signs in again on its own, back to the same screen")
        d, op = open_desk(browser, st, signed_in=False)
        d.eval("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
        op.lapsed = True
        poll(d, leaving_ok=True)
        wait_py(d, lambda: op.top_rd() is not None, 20)
        check("the tab went to sign in", op.top_rd() is not None, op.starts)
        check("...with the screen kept for the way back", op.top_rd() == "/" + FD_ROUTE, op.top_rd())
        d.close()

        scene("signed out, nothing typed, and a first-load call meets the lapse before any poll")
        d, op = open_desk(browser, st, signed_in=False, hold=("/api/triage/gate",))
        wait_py(d, lambda: bool(op.parked), 10)
        check("the first load's gate call was held (else this scene is the one above)", bool(op.parked), op.parked)
        d.eval("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
        op.lapsed = True
        op.release()                          # the load's own call meets the lapse and signs in...
        poll(d, leaving_ok=True)              # ...while the drive's poll lands on a page that is leaving
        wait_py(d, lambda: op.top_rd() is not None, 20)
        check("the tab went to sign in, whichever call met the lapse first", op.top_rd() is not None, op.starts)
        check("...with the screen kept for the way back", op.top_rd() == "/" + FD_ROUTE, op.top_rd())
        d.close()

        # A browser that blocks site data for the origin throws on ANY touch of the storage, reads
        # included (a SecurityError from the getter itself). The inbox's top-level state read
        # helm.emerg / helm.cats bare, so app.js died at load and nothing on the page answered.
        # Last scene: it answers the fixture's first ask.
        scene("browser storage is blocked outright (reads throw too): the inbox still loads and answers")
        blocked = """for (const k of ['localStorage', 'sessionStorage'])
                       Object.defineProperty(window, k, { configurable: true, get() { throw new DOMException('blocked', 'SecurityError'); } });"""
        d = SessionDrive(browser, st.home, WIDE, "#/needs-you", before=lambda d: d.page.add_init_script(blocked))
        check("precondition: reading storage throws on this page",
              d.eval("(() => { try { localStorage.getItem('x'); return false; } catch (e) { return e.name === 'SecurityError'; } })()"))
        rendered = d.until("!!document.querySelector('.conv-ask .conv-opt')", timeout=20000)
        check("the first ask's options render", rendered, d.errors[:3])
        if rendered:
            fresh = "document.querySelectorAll('.toast').forEach(t => t.remove())"
            d.eval(fresh)
            d.page.locator(".conv-ask .conv-opt", has_text="Grow the volume by 500 GB").first.click(timeout=8000)
            answered = d.until("[...document.querySelectorAll('.toast')].some(t => /Replied/.test(t.textContent))", timeout=10000)
            check("...and an option answers", answered, d.eval("[...document.querySelectorAll('.toast')].map(t => t.textContent)"))
        check("the fixed Helm design does not depend on storage", d.eval("document.documentElement.dataset.theme") == "helm",
              d.eval("document.documentElement.dataset.theme"))
        check("no retired theme or sound control appears", d.eval("!document.querySelector('#btn-theme, #btn-sound')"))
        before = d.eval("({hash: location.hash, theme: document.documentElement.dataset.theme, dialogs: document.querySelectorAll('.th-pop, .shell-help-overlay').length})")
        d.eval("document.activeElement && document.activeElement.blur && document.activeElement.blur()")
        d.page.keyboard.press("t")
        after = d.eval("({hash: location.hash, theme: document.documentElement.dataset.theme, dialogs: document.querySelectorAll('.th-pop, .shell-help-overlay').length})")
        check("T stays inert even with storage blocked", before == after, (before, after))
        check("no page errors", not d.errors, d.errors[:3])
        d.close()

        browser.close()
    print("\ncard opens: " + "; ".join("%.1fs %s" % (t, name[:40]) for name, t in OPENS))
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
