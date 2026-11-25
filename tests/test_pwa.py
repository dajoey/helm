"""Helm installs as an app (2026-09-27, the owner: "Helm and the reference app
need to be PWA-capable"; the reference app was an earlier, separate deployment, retired since).

Run: python3 tests/test_pwa.py   (from the repo root)

Every path sits behind the proxy's forward_auth and Authentik, the manifest and
its icons included: a request without the session is a 302 to auth.example.com. A browser fetches
the manifest a page links WITHOUT cookies unless the link says crossorigin="use-credentials", so
the manifest came back as Authentik's redirect: Chrome's install check read "no manifest" on a
first visit, and a later visit installed only when the service worker happened to hold a copy
(live 2026-09-27: /manifest.webmanifest, /sw.js and the icons all 302 without the session).
Back when the reference app still ran beside Helm, that deployment served Helm's manifest too,
so the two installed apps were both "Helm", and Helm's maskable icons were the reference's
lever (web/icons/ was the old app's set): a phone launcher, which prefers the maskable icon,
showed the lever for Helm. The icon checks below keep that lesson.

Proves, in Chromium (the full build: the headless shell has no install check) behind a
fake forward_auth that passes only requests carrying the session cookie and sends the rest to a
sign-in page on another origin, on a FIRST visit (the service worker blocked, so no cached copy
can stand in for the manifest):
  - the manifest request carries the session, and the install check finds nothing missing;
  - the app installs as Helm, with its own name, description and icons;
  - every icon is a PNG of its declared size, both purposes are present, the maskable ones are
    full-bleed (no transparent corner for a launcher's mask to show);
  - the head names the app (title, application-name, the iOS title, favicon, touch icon),
    and the window title the page keeps ("(n) Helm") says Helm.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urljoin, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "screens"))

if os.environ.get("HELM_SKIP_DRIVES") == "1":
    print("  FAIL the drives were skipped (HELM_SKIP_DRIVES=1): this suite never passes quietly")
    print("0 passed, 1 failed")
    sys.exit(1)

from playwright.sync_api import sync_playwright  # noqa: E402

import fixture  # noqa: E402

passed = failed = 0
COOKIE = ("authentik_proxy_test", "signed-in")
APP = {"home": "Helm", "helm": "Helm"}   # both names resolve to the app's own name


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  <- {detail!r}"[:600])


class SignIn(BaseHTTPRequestHandler):
    """Authentik's sign-in page: another origin, no CORS headers, so a fetch cannot read it."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"<!doctype html><title>authentik</title><h1>Sign in to continue</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Gate:
    """Caddy's forward_auth in front of one site's web role, as Authentik answers it.

    A request that carries the session cookie goes through with X-Forwarded-User added
    (whatever the client sent in that header is dropped); any other request is a 302 to the
    sign-in page on another origin. `seen` records (method, path, had-session, Sec-Fetch-Dest)."""

    def __init__(self, upstream: str, signin: str):
        self.seen = []
        up = urlparse(upstream)
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def relay(self):
                ok = ("%s=%s" % COOKIE) in (self.headers.get("Cookie") or "")
                gate.seen.append((self.command, self.path, ok, self.headers.get("Sec-Fetch-Dest") or ""))
                if not ok:
                    self.send_response(302)
                    self.send_header("Location", signin + "/application/o/authorize/?rd=" + quote(self.path, safe=""))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else None
                skip = ("host", "cookie", "x-forwarded-user", "accept-encoding", "connection")
                headers = {k: v for k, v in self.headers.items() if k.lower() not in skip}
                headers["X-Forwarded-User"] = fixture.USER
                conn = http.client.HTTPConnection(up.hostname, up.port, timeout=120)
                try:
                    conn.request(self.command, self.path, body=body, headers=headers)
                    r = conn.getresponse()
                    self.send_response(r.status)
                    for k, v in r.getheaders():
                        if k.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while self.command != "HEAD":   # relayed as it comes: an event stream never ends
                        chunk = r.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (OSError, http.client.HTTPException):   # the browser left, or a stream was cut at close
                    pass
                finally:
                    conn.close()

            do_GET = do_HEAD = do_POST = do_PUT = relay

        self.server = ThreadingHTTPServer(("127.0.0.1", fixture.free_port()), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def get(self, path):
        """GET as the signed-in browser would: through the gate, with the session."""
        req = urllib.request.Request(self.url + path, headers={"Cookie": "%s=%s" % COOKIE})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()


def png_size(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


CORNERS_JS = """async (src) => {
  const im = new Image(); im.src = src; await im.decode();
  const c = document.createElement("canvas"); c.width = im.naturalWidth; c.height = im.naturalHeight;
  const g = c.getContext("2d"); g.drawImage(im, 0, 0);
  const w = c.width - 1, h = c.height - 1;
  return [[0, 0], [w, 0], [0, h], [w, h]].map(([x, y]) => g.getImageData(x, y, 1, 1).data[3]);
}"""


def head_tag(html: str, pattern: str):
    m = re.search(pattern, html)
    return m.group(1) if m else None


def drive_site(p, st, name, signin, icons_by_site):
    site = getattr(st, "home" if name == "helm" else name)
    app = APP[name]
    print(f"\n{name}: installs as {app}, first visit, behind forward_auth")
    gate = Gate(site.web_url, signin)
    profile = tempfile.mkdtemp(prefix="helm-pwa-")
    ctx = None
    try:
        # A persistent profile: the install check refuses an incognito one ("in-incognito").
        ctx = p.chromium.launch_persistent_context(profile, channel="chromium", headless=True, service_workers="block",
                                                   viewport={"width": 999, "height": 900}, locale="en-US",
                                                   timezone_id="America/New_York")
        ctx.add_cookies([{"name": COOKIE[0], "value": COOKIE[1], "url": gate.url}])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(gate.url + "/", wait_until="load")
        page.wait_for_function("document.querySelector('#you-n') && /^\\d+$/.test(document.querySelector('#you-n').textContent)", timeout=20000)
        cdp = ctx.new_cdp_session(page)
        m = cdp.send("Page.getAppManifest")
        errors = cdp.send("Page.getInstallabilityErrors").get("installabilityErrors")
        manifest_reqs = [s for s in gate.seen if s[3] == "manifest"]
        check("the browser asked for the manifest", manifest_reqs, gate.seen)
        check("every manifest request carried the session (crossorigin=use-credentials)",
              manifest_reqs and all(s[2] for s in manifest_reqs), manifest_reqs)
        check("nothing the page loaded went out without the session", all(s[2] for s in gate.seen),
              [s for s in gate.seen if not s[2]])
        check("Chromium read the manifest", (m.get("url") or "").endswith("/manifest.webmanifest") and not m.get("errors") and m.get("data"),
              {k: m.get(k) for k in ("url", "errors")})
        check("the install check finds nothing missing", errors == [], errors)
        title = page.title()
        check(f"the window title names {app}", re.fullmatch(r"(\(\d+\) )?%s" % app, title) is not None, title)
        try:
            manifest = json.loads(m.get("data") or "{}")
        except ValueError:
            manifest = {}
        check(f"name and short_name are {app}", manifest.get("name") == app and manifest.get("short_name") == app,
              (manifest.get("name"), manifest.get("short_name")))
        check("standalone, scoped to the site root", manifest.get("display") == "standalone" and manifest.get("scope") == "/"
              and manifest.get("start_url") == "/" and manifest.get("id") == "/", {k: manifest.get(k) for k in ("display", "scope", "start_url", "id")})
        icons = manifest.get("icons") or []
        purposes = {pp for i in icons for pp in (i.get("purpose") or "any").split()}
        check("icons of both purposes (any, maskable)", {"any", "maskable"} <= purposes, purposes)
        check("a 192 and a 512 icon", {"192x192", "512x512"} <= {i.get("sizes") for i in icons}, [i.get("sizes") for i in icons])
        mf_url = m.get("url") or gate.url + "/manifest.webmanifest"
        got = icons_by_site.setdefault(name, {})
        for i in icons:
            src = urljoin(mf_url, i.get("src", ""))
            path = urlparse(src).path
            status, ctype, data = gate.get(path)
            want = tuple(int(x) for x in (i.get("sizes") or "0x0").split("x"))
            check(f"icon {path} is a {i.get('sizes')} PNG", status == 200 and ctype == "image/png" and png_size(data) == want,
                  (status, ctype, png_size(data)))
            got[path] = hashlib.sha256(data).hexdigest()
            if "maskable" in (i.get("purpose") or ""):
                alphas = page.evaluate(CORNERS_JS, path)
                check(f"maskable {path} is full-bleed (opaque corners)", alphas == [255, 255, 255, 255], alphas)
        for sc in manifest.get("shortcuts") or []:
            u = urlparse(urljoin(mf_url, sc.get("url", "")))
            check(f"shortcut {sc.get('name')!r} opens a screen of this app", u.netloc == urlparse(gate.url).netloc and u.path == "/"
                  and u.fragment.startswith("/"), sc.get("url"))
        shortcut_urls = [sc.get("url") for sc in manifest.get("shortcuts") or []]
        if name == "helm":
            check("Helm advertises its working Chat shortcut", "/#/chat" in shortcut_urls, shortcut_urls)
        else:
            check("module-off installs do not advertise Chat", "/#/chat" not in shortcut_urls, shortcut_urls)
        # The head: what a phone's home screen and a desktop's app list read besides the manifest.
        status, _, body = gate.get("/")
        html = body.decode("utf-8", "replace")
        if name == "home":   # module off: today's page, byte for byte, and today's manifest
            check("module off: index.html is served exactly as it is on disk", body == open(os.path.join(ROOT, "web", "index.html"), "rb").read())
            check("module off: the manifest is served exactly as it is on disk",
                  gate.get("/manifest.webmanifest")[2] == open(os.path.join(ROOT, "web", "manifest.webmanifest"), "rb").read())
        check("the manifest link asks for credentials",
              re.search(r'<link rel="manifest" href="/manifest\.webmanifest" crossorigin="use-credentials">', html) is not None)
        check(f"<title> is {app}", head_tag(html, r"<title>([^<]*)</title>") == app, head_tag(html, r"<title>([^<]*)</title>"))
        for meta in ("application-name", "apple-mobile-web-app-title"):
            v = head_tag(html, r'<meta name="%s" content="([^"]*)">' % meta)
            check(f"meta {meta} is {app}", v == app, v)
        if name != "work":   # the work header keeps the word it always had; home says Helm, or Helm with the module on
            brand = page.evaluate("[document.querySelector('.shell-name').textContent, document.querySelector('.shell-brand').title,"
                                  " document.querySelector('.hx-drawer-title') && document.querySelector('.hx-drawer-title').textContent]")
            check(f"the header's title cell, its tooltip and the drawer's title say {app}",
                  brand[0] == app and brand[1].startswith(app + " ") and brand[2] == app, brand)
            page.goto(gate.url + "/#/manage/appearance" if name == "helm" else gate.url + "/#/needs-you")
            page.keyboard.press("?")
            page.wait_for_timeout(300)
            sheet = page.evaluate("(document.querySelector('.shell-help-overlay') || {}).innerText || ''")
            check("the ? key sheet opens", sheet.lower().startswith("keys"), sheet[:40])
            other = "Helm" if name == "helm" else "Helm"
            check(f"the key sheet and the page never call the app {other}", other not in sheet.replace("Helm thread", ""), [l for l in sheet.splitlines() if other in l][:3])
        for rel in ("icon", "apple-touch-icon"):
            href = head_tag(html, r'<link rel="%s" href="([^"]+)"' % rel)
            ok = False
            if href:
                s2, ct2, d2 = gate.get(href)
                ok = s2 == 200 and ct2.startswith("image/")
                got[href] = hashlib.sha256(d2).hexdigest()
            check(f"{rel} {href} is served as an image", ok, href)
    finally:
        if ctx is not None:
            ctx.close()
        gate.server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)


def main():
    signin = ThreadingHTTPServer(("127.0.0.1", fixture.free_port()), SignIn)
    signin.daemon_threads = True
    threading.Thread(target=signin.serve_forever, daemon=True).start()
    # "localhost" is another origin than the gate's 127.0.0.1, as auth.example.com is for the app.
    signin_url = "http://localhost:%d" % signin.server_address[1]
    icons = {}
    with fixture.Stack(("home",)) as st, sync_playwright() as p:
        for name in ("home",):
            try:
                drive_site(p, st, name, signin_url, icons)
            except Exception as e:  # a crash is a failure with its reason, never a quiet stop
                check(f"{name}: the drive ran to the end", False, "%s: %s" % (type(e).__name__, e))
    signin.shutdown()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
