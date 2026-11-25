"""The web role's security model, offline: app/web.py against a fake upstream.

Run: python3 tests/test_web_role.py   (from the repo root)

The fake api role is an in-process Starlette app reached through httpx.ASGITransport, so
the proxy is exercised exactly as it runs (headers built, bodies capped, allowlist matched)
with no socket. Proves: the token is injected upstream and reaches no response, no log
line and no served file; 401 without X-Forwarded-User (and the env switch); the login
is forwarded as X-Helm-Actor and a client-sent actor or Authorization is dropped; the
allowlist (each allowed route proxies, the producer/bot routes are 404, never 403); static
path confinement; body caps (413); bad JSON (400); the retired console's routes are 404; index.html at /; every /att answer (file, 404,
401) leaves sandboxed and never sniffed whatever the api sent, and the api's download marking passes.
"""
from __future__ import annotations

import io
import json
import os
import asyncio
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "web-role-test-token-3f9c1e"          # unique, so a grep for it is meaningful
os.environ["HELM_APP_TOKEN"] = TOKEN
os.environ["HELM_API_URL"] = "http://api.test"
os.environ.pop("HELM_WEB_REQUIRE_AUTH_HEADER", None)

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response, StreamingResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.web as web  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail else ""))


# ---------------------------------------------------------------- the fake upstream
SEEN = []   # every request the fake api saw: (method, path, headers, body)
RAW = []    # the raw (still percent-encoded) path of each


async def echo(request: Request):
    body = await request.body()
    SEEN.append((request.method, request.url.path + ("?" + request.url.query if request.url.query else ""),
                 {k.lower(): v for k, v in request.headers.items()}, body))
    RAW.append((request.scope.get("raw_path") or b"").decode("latin-1"))
    if request.headers.get("authorization") != "Bearer " + TOKEN:
        return JSONResponse({"ok": False, "detail": "unauthorized"}, status_code=401)
    if request.url.path == "/api/triage/dispatcher-heartbeat":
        return JSONResponse({"detail": "no dispatcher heartbeat published yet"}, status_code=404)
    if request.url.path.startswith("/att/"):
        if request.url.path.endswith(".html"):   # what the api answers for an active type at a read-only site
            return Response(b"<script>x</script>", media_type="application/octet-stream",
                            headers={"Content-Disposition": 'attachment; filename="page.html"'})
        return Response(b"PNGBYTES", media_type="image/png", headers={"Content-Disposition": "inline; filename=x.png"})
    return JSONResponse({"ok": True, "method": request.method, "path": request.url.path,
                         "actor": request.headers.get("x-helm-actor"), "auth": "bearer" if request.headers.get("authorization") else "none",
                         "body": body.decode("utf-8", "replace")[:200]})


fake_api = Starlette(routes=[Route("/{path:path}", echo, methods=["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"])])

web.UPSTREAM_TRANSPORT = httpx.ASGITransport(app=fake_api)

AUTH = {"X-Forwarded-User": "owner"}


def run():
    global SEEN
    # Every log line the role writes goes through web._log; keep each one (and what
    # reached stdout) for the token grep at the end.
    real_stdout = sys.stdout
    log = io.StringIO()
    lines = []
    orig_log = web._log

    def capture_log(msg):
        lines.append(msg)
        orig_log(msg)
    web._log = capture_log

    class Tee(io.TextIOBase):
        def write(self, s):
            log.write(s)
            return real_stdout.write(s)

        def flush(self):
            real_stdout.flush()
    sys.stdout = Tee()
    c = TestClient(web.app, client=("127.0.0.1", 40000))
    responses = []

    def get(path, **kw):
        r = c.get(path, **kw)
        responses.append(r)
        return r

    def post(path, **kw):
        r = c.post(path, **kw)
        responses.append(r)
        return r

    def put(path, **kw):
        r = c.put(path, **kw)
        responses.append(r)
        return r

    print("\nstatic: the shell is served, confined to web/")
    r = get("/", headers=AUTH)
    check("index.html at / (the head names the app)", r.status_code == 200 and b"<title>Helm</title>" in r.content
          and b'<meta name="application-name" content="Helm">' in r.content and "text/html" in r.headers["content-type"])
    h = c.head("/", headers=AUTH)
    check("HEAD / counts the page as served", h.status_code == 200 and h.headers.get("content-length") == str(len(r.content)),
          (h.headers.get("content-length"), len(r.content)))
    check("the shell carries the image Content-Security-Policy (finding 5)", r.headers.get("content-security-policy") == "img-src 'self' data: blob:",
          r.headers.get("content-security-policy"))
    check("index.html at /index.html", get("/index.html", headers=AUTH).status_code == 200)
    r = get("/sw.js", headers=AUTH)
    check("sw.js no-cache and allowed at /", r.status_code == 200 and r.headers["cache-control"] == "no-cache"
          and r.headers.get("service-worker-allowed") == "/" and "javascript" in r.headers["content-type"])
    r = get("/manifest.webmanifest", headers=AUTH)
    check("webmanifest mime", r.headers["content-type"] == "application/manifest+json")
    check("the manifest names the app", json.loads(r.content).get("name") == "Helm", r.content[:80])
    r = get("/manifest.webmanifest/", headers=AUTH)   # resolves to the same file, so it is the same app
    check("the manifest by another spelling is the same app", r.status_code == 200 and json.loads(r.content).get("name") == "Helm",
          (r.status_code, r.content[:80]))
    check("index.html is served as written", get("/", headers=AUTH).content == (web.WEB_ROOT / "index.html").read_bytes())
    check("Newsreader woff2 mime", get("/fonts/newsreader-400.woff2", headers=AUTH).headers["content-type"] == "font/woff2")
    check("Instrument Sans woff2 mime", get("/fonts/instrument-sans-400.woff2", headers=AUTH).headers["content-type"] == "font/woff2")
    check("the fixed favicon is still served from its legacy path", get("/snes/icon-32.png", headers=AUTH).status_code == 200)
    check("ES module under /notebook", get("/notebook/app.js", headers=AUTH).headers["content-type"].startswith("text/javascript"))
    check("vendored CodeMirror", get("/vendor/cm.js", headers=AUTH).status_code == 200)
    check("nosniff on static", get("/app.js", headers=AUTH).headers["x-content-type-options"] == "nosniff")
    check("HEAD works", c.head("/app.js", headers=AUTH).status_code == 200)
    check("missing file 404", get("/nope.js", headers=AUTH).status_code == 404)
    for bad in ["/../app/web.py", "/..%2fapp/web.py", "/%2e%2e/app/web.py", "/notebook/../../app/web.py", "/static/../../helm/store.py",
                "//etc/passwd", "/etc/passwd", "/%00", "/app.js%00.png"]:
        r = get(bad, headers=AUTH)
        check("traversal %s -> 404" % bad, r.status_code == 404 and b"HELM_APP_TOKEN" not in r.content and b"root:" not in r.content, r.status_code)
    # a symlink inside web/ that points outside resolves outside and is 404
    link = os.path.join(str(web.WEB_ROOT), "zz-escape-link")
    try:
        os.symlink(os.path.join(os.path.dirname(str(web.WEB_ROOT)), "app", "web.py"), link)
        r = get("/zz-escape-link", headers=AUTH)
        check("symlink escaping web/ -> 404", r.status_code == 404, r.status_code)
    finally:
        if os.path.islink(link):
            os.unlink(link)
    r = web._static("/etc/passwd")
    check("an absolute path handed to _static is joined under WEB_ROOT, never used as-is",
          (web.WEB_ROOT / "/etc/passwd".lstrip("/")).is_relative_to(web.WEB_ROOT)
          and r.status_code == 404 and b"root:" not in r.body
          and web._static("/app.js").status_code == 200, (r.status_code, r.body[:80]))

    print("\nauth: X-Forwarded-User is required on everything")
    check("static 401 without the header", get("/").status_code == 401 and get("/app.js").status_code == 401)
    check("api 401 without the header", get("/api/triage/threads").status_code == 401)
    check("401 body is JSON", get("/api/meta").json().get("ok") is False)
    check("meta requires it too", get("/api/meta").status_code == 401 and get("/api/meta", headers=AUTH).status_code == 200)
    check("blank header is missing", get("/api/meta", headers={"X-Forwarded-User": "  "}).status_code == 401)
    web.REQUIRE_AUTH_HEADER = False
    check("HELM_WEB_REQUIRE_AUTH_HEADER=0 lets it through", get("/").status_code == 200 and get("/api/meta").status_code == 200)
    web.REQUIRE_AUTH_HEADER = True
    check("switch back on", get("/").status_code == 401)

    print("\nproxy: token injected server-side, actor forwarded, client headers stripped")
    SEEN.clear()
    r = get("/api/triage/threads?include_snoozed=true", headers=AUTH)
    check("GET threads proxies", r.status_code == 200 and r.json()["path"] == "/api/triage/threads", r.text)
    m, p, h, b = SEEN[-1]
    check("upstream carries the bearer", h.get("authorization") == "Bearer " + TOKEN)
    check("query string preserved", p == "/api/triage/threads?include_snoozed=true", p)
    check("actor forwarded on reads (harmless)", h.get("x-helm-actor") == "owner", h)
    r = post("/api/triage/threads/t-1/reply", json={"value": "ok", "note": "n"}, headers={**AUTH, "X-Helm-Actor": "mallory", "Authorization": "Bearer evil"})
    m, p, h, b = SEEN[-1]
    check("POST reply proxies with status", r.status_code == 200 and json.loads(b) == {"value": "ok", "note": "n"}, r.text)
    check("X-Helm-Actor is the header's login, not the client's", h.get("x-helm-actor") == "owner", h.get("x-helm-actor"))
    check("client Authorization never reaches upstream", h.get("authorization") == "Bearer " + TOKEN)
    check("no cookies forwarded", "cookie" not in h)
    check("upstream Content-Type is JSON", h.get("content-type") == "application/json")
    r = post("/api/triage/threads/t-1/close", content=b"", headers=AUTH)
    check("empty POST body becomes {}", r.status_code == 200 and SEEN[-1][3] == b"{}", SEEN[-1][3])
    SEEN.clear()
    web.TOKEN = ""
    try:
        r = get("/api/triage/threads", headers=AUTH)
        check("token missing -> 503, never proxied blind",
              r.status_code == 503 and SEEN == [] and r.json().get("ok") is False, (r.status_code, SEEN[:1]))
    finally:
        web.TOKEN = TOKEN
    r = get("/att/Projects/Pine%20Hollow%20portal/diagram.png", headers=AUTH)
    check("attachment passes through with its type and name", r.status_code == 200 and r.headers["content-type"] == "image/png"
          and r.content == b"PNGBYTES" and "filename=x.png" in r.headers.get("content-disposition", ""), r.headers)
    check("attachment path reaches the api as spelled (still encoded)", RAW[-1] == "/att/Projects/Pine%20Hollow%20portal/diagram.png"
          and SEEN[-1][1] == "/att/Projects/Pine Hollow portal/diagram.png", (RAW[-1], SEEN[-1][1]))
    check("the web role sandboxes /att itself (the upstream sent no CSP): CSP sandbox and nosniff",
          r.headers.get("content-security-policy") == "sandbox" and r.headers.get("x-content-type-options") == "nosniff", r.headers)
    r = get("/att/Files/page.html", headers=AUTH)
    check("an attachment the api marks for download reaches the browser as a download, sandboxed",
          r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
          and r.headers.get("content-disposition", "").startswith("attachment")
          and r.headers.get("content-security-policy") == "sandbox" and r.headers.get("x-content-type-options") == "nosniff", r.headers)
    r = get("/att/" + "x" * 1100, headers=AUTH)
    check("an /att refusal by the allowlist is sandboxed too", r.status_code == 404
          and r.headers.get("content-security-policy") == "sandbox", (r.status_code, r.headers))
    r = get("/att/attachments/x.png")
    check("an /att 401 (no login header) is sandboxed too", r.status_code == 401
          and r.headers.get("content-security-policy") == "sandbox", (r.status_code, r.headers))
    r = get("/api/triage/dispatcher-heartbeat", headers=AUTH)
    check("upstream 404 passes through as 404 (heartbeat never published)", r.status_code == 404)

    print("\nallowlist: the owner's affordances proxy, everything else is 404 (never 403)")
    allowed_get = ["/api/meta", "/api/status", "/api/triage/threads", "/api/triage/threads/t-abc", "/api/triage/threads/index?state=done&page=2",
                   "/api/triage/threads/t-abc/attachments", "/api/triage/threads/t-abc/attachments/att-1.png", "/api/triage/gate?days=14",
                   "/api/triage/events?since=1", "/api/triage/search?q=x", "/api/triage/dispatcher-heartbeat",
                   "/api/search?q=x", "/api/switcher?q=x", "/api/notes/folders", "/api/notes/list?folder=Daily", "/api/notes/note?rel=a.md",
                   "/api/notes/raw?rel=a.md", "/api/notes/meta", "/api/notes/complete?q=a&kind=note", "/api/notes/templates",
                   "/api/notes/changes?since=1&limit=5", "/api/notes/graph?limit=5", "/api/notes/tasks", "/api/notes/atlas", "/api/notes/tail?rel=a.md&n=3", "/api/notes/history?rel=a.md",
                   "/api/topic?rel=a.md", "/api/day?date=2026-09-25", "/att/attachments/x.png"]
    for path in allowed_get:
        r = get(path, headers=AUTH)
        check("GET %s proxies" % path, r.status_code in (200, 404) and (r.status_code == 200 or "heartbeat" in path), r.status_code)
    allowed_post = ["/api/triage/threads/t-1/reply", "/api/triage/threads/t-1/close", "/api/triage/threads/t-1/mute", "/api/triage/threads/t-1/snooze",
                    "/api/triage/threads/t-1/attach", "/api/triage/threads/t-1/reopen", "/api/triage/threads/t-1/park", "/api/triage/threads/t-1/unpark",
                    "/api/triage/threads/t-1/backlog", "/api/triage/threads/t-1/unbacklog", "/api/triage/threads/create",
                    "/api/triage/resort", "/api/day/append", "/api/notes/new", "/api/notes/daily/new",
                    "/api/notes/toggle", "/api/notes/rename"]
    for path in allowed_post:
        r = post(path, json={"x": 1}, headers=AUTH)
        check("POST %s proxies" % path, r.status_code == 200 and r.json()["method"] == "POST", r.text)
    r = post("/api/notes/attach", content=b"\x89PNG raw bytes", headers={**AUTH, "Content-Type": "image/png", "X-Filename": "shot%20one.png"})
    check("POST notes/attach carries the raw body and X-Filename", r.status_code == 200 and SEEN[-1][3] == b"\x89PNG raw bytes"
          and SEEN[-1][2].get("x-filename") == "shot%20one.png" and SEEN[-1][2].get("content-type") == "image/png", SEEN[-1][2])
    r = put("/api/notes/note", json={"rel": "a.md", "text": "hi"}, headers=AUTH)
    check("PUT notes/note proxies", r.status_code == 200 and r.json()["method"] == "PUT", r.text)
    SEEN.clear()
    denied = [("GET", "/api/triage/pending-dispatch"), ("GET", "/api/triage/pending-working"), ("GET", "/api/triage/claims"), ("GET", "/api/triage/threads/t-1/claims"),
              ("GET", "/api/triage/suppressions"), ("GET", "/api/triage/lessons"), ("GET", "/api/triage/rechecks"), ("GET", "/api/triage/settings"),
              ("GET", "/api/triage/labels"), ("GET", "/metrics"), ("GET", "/api/triage/threads/t-1/result"),
              ("GET", "/api/notes/../../triage/threads"), ("GET", "/api/notes/x/y/z/w/v/u"), ("GET", "/att/../app/web.py"),
              ("POST", "/api/triage/threads/t-1/result"), ("POST", "/api/triage/threads/t-1/dispatched"), ("POST", "/api/triage/threads/t-1/note"),
              ("POST", "/api/triage/threads/t-1/claims"), ("POST", "/api/triage/threads/t-1/recheck"), ("POST", "/api/triage/threads/t-1/label"),
              ("POST", "/api/triage/needs-owner-check"), ("POST", "/api/triage/gave-up/restore"), ("POST", "/api/triage/lessons"),
              ("POST", "/api/triage/settings"), ("POST", "/api/triage/labels"), ("POST", "/api/triage/threads/t-1/evil"),
              ("POST", "/api/notes/history"), ("POST", "/api/day"), ("POST", "/api/meta"),
              # routes with no caller in web/: a client's scripts reach the api role with their own bearer
              ("GET", "/api/health"), ("GET", "/api/notes/changes/x"), ("POST", "/api/notes/tail"), ("GET", "/api/inbox"), ("GET", "/api/thread/t-abc"), 
              ("GET", "/api/notes/revision?id=1"), ("POST", "/api/triage/threads/new"), ("POST", "/api/inbox/new"), ("POST", "/api/inbox/t-1/reply"),
              ("PUT", "/api/triage/threads"), ("PUT", "/api/triage/dispatcher-heartbeat"), ("PUT", "/api/notes/new"), ("PUT", "/api/triage/threads/t-1/reply"),
              ("DELETE", "/api/triage/labels/x"), ("DELETE", "/api/notes/note"), ("POST", "/api/notes/delete"), ("PATCH", "/api/triage/threads/t-1")]
    for method, path in denied:
        r = c.request(method, path, headers=AUTH, content=b"{}" if method in ("POST", "PUT", "PATCH") else None)
        responses.append(r)
        check("%s %s -> 404 (not 403)" % (method, path), r.status_code == 404, r.status_code)
    check("nothing denied reached the api", SEEN == [], SEEN[:3])
    r = get("/" + "a" * 5000, headers=AUTH)
    check("a path segment too long for the filesystem is 404, not 500", r.status_code == 404, r.status_code)
    r = get("/api/triage/threads/" + "t" * 4000, headers=AUTH)
    check("an over-long api path is 404", r.status_code == 404, r.status_code)

    print("\ndot segments: judged on the path as sent (httpx would normalise them before the api saw them)")

    def raw_call(method, path):
        """Deliver an un-normalised path the way uvicorn would; TestClient strips dot segments."""
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": [(b"x-forwarded-user", b"owner"), (b"host", b"testserver"), (b"content-type", b"application/json")],
                 "client": ("127.0.0.1", 1), "server": ("testserver", 80)}
        status = {}
        body = b"{}" if method in ("POST", "PUT") else b""

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
        asyncio.run(web.app(scope, receive, send))
        return status.get("code")
    SEEN.clear()
    for method, path in (("POST", "/api/triage/threads/../reply"), ("POST", "/api/triage/threads/t-1/reply/../../resort"),
                         ("GET", "/api/triage/threads/./t-1"), ("GET", "/api/notes/../triage/threads"), ("POST", "/api/notes/./new"),
                         ("GET", "/api/conversations/.."), ("POST", "/api/conversations/../note"), ("GET", "/att/./x.png"), ("GET", "/api/triage/.")):
        check("%s %s is 404 as sent" % (method, path), raw_call(method, path) == 404, raw_call(method, path))
    check("no dot-segment path reached the api", SEEN == [], SEEN[:3])

    print("\n502 detail never echoes a token an exception's text might carry")
    saved_transport = web.UPSTREAM_TRANSPORT

    def boom(req):
        raise RuntimeError("connect failed; headers were Authorization: Bearer " + web.TOKEN)
    web.UPSTREAM_TRANSPORT = httpx.MockTransport(boom)
    try:
        r = get("/api/triage/threads", headers=AUTH)
    finally:
        web.UPSTREAM_TRANSPORT = saved_transport
    check("upstream exception -> 502 with the token redacted from the detail",
          r.status_code == 502 and web.TOKEN not in r.text and "[token]" in r.text, r.text[:200])
    check("404 body says not found", get("/api/triage/pending-dispatch", headers=AUTH).json().get("error") == "not found")

    print("\nbodies: caps and JSON")
    r = post("/api/triage/threads/t-1/mute", content=b"x" * (64 * 1024 + 1), headers={**AUTH, "Content-Type": "application/json"})
    check("64 KB cap on a plain tap -> 413", r.status_code == 413, r.status_code)
    r = post("/api/triage/threads/t-1/reply", content=b'{"a":"' + b"x" * (64 * 1024 + 100) + b'"}', headers={**AUTH, "Content-Type": "application/json"})
    check("a reply may exceed 64 KB (attachments ride inline)", r.status_code == 200, r.status_code)
    r = post("/api/triage/threads/t-1/attach", content=b"x" * (36 * 1024 * 1024 + 1), headers={**AUTH, "Content-Type": "application/json"})
    check("36 MB cap on attach -> 413", r.status_code == 413, r.status_code)
    r = put("/api/notes/note", content=b"x" * (8 * 1024 * 1024 + 1), headers={**AUTH, "Content-Type": "application/json"})
    check("8 MB cap on a note save -> 413", r.status_code == 413, r.status_code)
    r = post("/api/triage/threads/t-1/reply", content=b"{not json", headers={**AUTH, "Content-Type": "application/json"})
    check("bad JSON -> 400", r.status_code == 400 and "JSON" in r.json()["error"], r.text)
    r = put("/api/notes/note", content=b"[1,", headers={**AUTH, "Content-Type": "application/json"})
    check("bad JSON on PUT -> 400", r.status_code == 400, r.status_code)
    r = post("/api/triage/threads/t-1/reply", content=b"{}", headers={**AUTH, "Content-Length": "999999999"})
    check("lying Content-Length over the cap -> 413", r.status_code == 413, r.status_code)

    print("\nthe retired console: its routes are 404")
    for path in ["/api/board", "/api/settings", "/api/settings/models?runner=pi", "/api/conversations/t-1/run-output",
                 "/api/frontdesk/feed", "/api/frontdesk/conversations", "/api/frontdesk/frontdesk.abc123/stream"]:
        check("GET %s -> 404" % path, get(path, headers=AUTH).status_code == 404)
    for path in ["/api/settings/roster", "/api/conversations/t-1/note", "/api/conversations/t-1/control", "/api/frontdesk/frontdesk.abc123/send"]:
        check("POST %s -> 404" % path, post(path, json={}, headers=AUTH).status_code == 404)

    print("\nconversations: the api's own, console none")
    r = get("/api/conversations/t-1", headers=AUTH)
    check("a conversation is the api's Helm-side timeline",
          r.status_code == 200 and r.json().get("path") == "/api/triage/threads/t-1/conversation", r.text[:200])
    check("the conversation read carries the app token upstream", SEEN[-1][2].get("authorization") == "Bearer " + TOKEN)
    saved_window = web.CONV_WINDOW_S
    web.CONV_WINDOW_S = 0
    try:
        r = get("/api/conversations/t-1/stream", headers=AUTH)
    finally:
        web.CONV_WINDOW_S = saved_window
    check("the stream is the api polled, one frame per change",
          r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream") and r.text.count("data:") == 1, r.text[:200])
    saved_up = web.UPSTREAM_TRANSPORT
    for code, want in ((500, 502), (404, 404)):
        web.UPSTREAM_TRANSPORT = httpx.MockTransport(lambda req, code=code: httpx.Response(code, json={"ok": False, "detail": "x"}))
        try:
            r = get("/api/conversations/t-1", headers=AUTH)
        finally:
            web.UPSTREAM_TRANSPORT = saved_up
        check("the api answers %d: the conversation is %d" % (code, want), r.status_code == want, (r.status_code, r.text[:200]))
    r = get("/api/meta", headers=AUTH)
    check("/api/meta's web block names no console", r.status_code == 200 and r.json().get("web") == {"owner": web.OWNER}, r.text[:200])
    check("console routes never touch the api", not any(p.startswith("/settings") or p.startswith("/conversations") for _, p, _, _ in SEEN))

    print("\nthe token: injected upstream, never served, never logged")
    sys.stdout = real_stdout
    logged = log.getvalue()
    check("the token was used upstream at least once", any(h.get("authorization") == "Bearer " + TOKEN for _, _, h, _ in SEEN))
    check("no response body carries the token", not any(TOKEN.encode() in r.content for r in responses))
    check("no response header carries the token", not any(TOKEN in v for r in responses for v in r.headers.values()))
    check("no log line carries the token (%d lines)" % len(lines), lines and not any(TOKEN in m for m in lines), [m for m in lines if TOKEN in m][:2])
    check("no stdout line the role wrote carries the token",
          not any(TOKEN in ln for ln in logged.splitlines() if not ln.startswith("  ")), [ln for ln in logged.splitlines() if TOKEN in ln][:2])
    served = 0
    for dp, _, fns in os.walk(str(web.WEB_ROOT)):
        for fn in fns:
            with open(os.path.join(dp, fn), "rb") as fh:
                data = fh.read()
            served += 1
            if TOKEN.encode() in data or b"HELM_APP_TOKEN" in data or b"Bearer " in data:
                check("served file %s carries no bearer" % fn, False, fn)
    check("every served file was grepped (%d files): no token, no bearer" % served, served > 40)
    # a log line is written for the proxied calls, and it is the redacted form
    check("log lines name the route", "/api/triage/threads/t-1/reply" in logged)
    sys.stdout = Tee()
    orig_log("probe " + TOKEN)
    sys.stdout = real_stdout
    check("_log redacts a token that slips into a message", TOKEN not in log.getvalue().split("probe")[-1] and "probe [token]" in log.getvalue())
    web._log = orig_log

    print("\nmisc")
    web_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "web.py")
    with open(web_env, encoding="utf-8") as fh:
        src = fh.read()
    check("app/web.py reads HELM_PORT, HELM_API_URL, HELM_APP_TOKEN and the login settings at import",
          all(k in src for k in ("HELM_PORT", "HELM_API_URL", "HELM_APP_TOKEN", "HELM_AUTH_HEADER",
                                 "HELM_TRUSTED_PROXIES", "HELM_DEV_LOGIN")))
    check("MAX_BODY as the web role", web.MAX_BODY == 64 * 1024 and web.MAX_ATTACH_BODY == 36 * 1024 * 1024)
    r = c.request("OPTIONS", "/api/triage/threads", headers=AUTH)
    check("OPTIONS -> 404", r.status_code == 404)
    r = c.request("TRACE", "/", headers=AUTH)
    check("unknown method -> 404/405", r.status_code in (404, 405))


if __name__ == "__main__":
    run()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
