"""The web role (HELM_ROLE=web, `app.web:app`): the screens and the same-origin proxy.

Starlette + httpx around one shell: the api role holds the data and this role holds none.

  * Serves `web/` (the shell, the inbox, the notebook, the vendored CodeMirror bundle)
    with strict path confinement: every request resolves under WEB_ROOT or it is 404.
  * Proxies an ALLOWLIST of `/api/*` (and `/att/*`) to the api role at HELM_API_URL,
    adding `Authorization: Bearer $HELM_APP_TOKEN` on the server side. The token never
    reaches the browser: it is not in any served file, never in a response header or
    body, and never in a log line (every log line passes through `_log`, which redacts).
  * Requires the login header named by `HELM_AUTH_HEADER` (default `X-Forwarded-User`,
    which a forward-auth proxy such as Authentik sets) on everything, static files included,
    and believes it only from a peer inside `HELM_TRUSTED_PROXIES` (CIDR list, default
    `127.0.0.1/32`): anywhere else, or with no header at all, the answer is 401.
    The login is forwarded upstream as `X-Helm-Actor`, so a reply or a close records who
    tapped it; anything the client sent as `X-Helm-Actor` or `Authorization` is dropped.
  * Every `/att/*` answer (the file, or any refusal) leaves with `Content-Security-Policy:
    sandbox` and `X-Content-Type-Options: nosniff`, set here whatever the api role sent: an
    attachment opened in a tab runs no script and gets no origin, so a file from the wiki can
    never act through this proxy. The api role also marks the active types for download at a
    folder-readonly site (`app/notebook_api.py`); that `Content-Disposition` passes through.
    The file is relayed chunk by chunk, never held whole, and a HEAD goes upstream as HEAD.
  * No agent console: the home console's routes (`/api/board`, `/api/settings*`, `/api/frontdesk*`,
    a conversation's `note`, `control`, `run-output` and `attachments`) are retired and 404 at
    both sites, like any route outside the allowlist.

Producer/bot routes (`/result`, `PUT /threads`, `/dispatched`, `/pending-*`, `/claims`,
`/suppressions`, `/lessons` ...) are deliberately unreachable from the browser: the page
has exactly the owner's own affordances, nothing more. Anything outside the allowlist is 404,
never 403: nothing to enumerate.

Env (read once, at import):
  HELM_PORT        the port (uvicorn reads it; kept here for /api/meta-style probes)
  HELM_API_URL     the api role's base, default http://127.0.0.1:3038
  HELM_APP_TOKEN   the bearer injected upstream (required for any proxied call)
  HELM_AUTH_HEADER   the login header a forward-auth proxy sets (default X-Forwarded-User)
  HELM_TRUSTED_PROXIES  the peers whose copy of that header is trusted (default 127.0.0.1/32)
  HELM_DEV_LOGIN     a login to act as with no proxy, for a local quick start; refused
                     unless the role binds to 127.0.0.1
  HELM_WEB_REQUIRE_AUTH_HEADER  1 (default) | 0

Conversations are served at both sites from the api role's own
`/api/triage/threads/<id>/conversation` (`helm/conversation.py`, `console: "none"`): the
thread's state, ask and events are Helm's. The stream reads it every few seconds, sends a
frame on change and closes after a minute (the browser's EventSource reconnects).
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import httpx
from starlette.applications import Starlette

from helm import preview as _preview
from helm import site as _site
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

WEB_ROOT = Path(os.environ.get("HELM_WEB_ROOT") or (Path(__file__).resolve().parent.parent / "web")).resolve()
PORT = int(os.environ.get("HELM_PORT", "3037") or 3037)
API_URL = (os.environ.get("HELM_API_URL") or "http://127.0.0.1:3038").rstrip("/")
TOKEN = (os.environ.get("HELM_APP_TOKEN") or "").strip()
REQUIRE_AUTH_HEADER = (os.environ.get("HELM_WEB_REQUIRE_AUTH_HEADER") or "1").strip().lower() not in ("0", "false", "no", "off")
AUTH_HEADER = (os.environ.get("HELM_AUTH_HEADER") or "X-Forwarded-User").strip() or "X-Forwarded-User"
TRUSTED_PROXIES = [p.strip() for p in (os.environ.get("HELM_TRUSTED_PROXIES") or "127.0.0.1/32").split(",") if p.strip()]
DEV_LOGIN = (os.environ.get("HELM_DEV_LOGIN") or "").strip()
BIND_HOST = (os.environ.get("HELM_HOST") or os.environ.get("HELM_BIND") or "127.0.0.1").strip()
OWNER = _site.owner()

# Error tracking (2026-09-26 live sweep, wiki Docker/error-tracker): the api role has reported to
# the error tracker since 09-21; the web role never did, and the page's own JavaScript errors reached
# nobody. Same switch as the api role: no HELM_SENTRY_DSN in the env = off.
try:
    import sentry_sdk
    if os.environ.get("HELM_SENTRY_DSN"):
        from helm.scrub import before_send, event_scrubber
        sentry_sdk.init(dsn=os.environ["HELM_SENTRY_DSN"], environment=os.environ.get("HELM_SENTRY_ENV", "home"),
                        server_name="helm-ui", traces_sample_rate=0.0, send_default_pii=False,
                        event_scrubber=event_scrubber(), before_send=before_send, max_request_body_size="never")   # replies, notes and reports never ride along to the error tracker
    else:
        sentry_sdk = None
except Exception:
    sentry_sdk = None

# The page's error reports (web/errors.js -> POST /api/client-error): one the error tracker issue per
# message and file; the same report is sent once per window, and a storm is capped.
CLIENT_ERROR_MAX_BODY = 16 * 1024
CLIENT_ERROR_MAX_PER_MIN = 20
CLIENT_ERROR_DEDUPE_S = 600.0
_now = time.monotonic   # tests swap in a clock

# Tests swap these for httpx.ASGITransport so the proxy is exercised against an in-process
# fake upstream without a socket. None means a real network transport.
UPSTREAM_TRANSPORT = None

# ---------------------------------------------------------------- static files
ROOT_FILES = {"/": "index.html", "/index.html": "index.html"}
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".webmanifest": "application/manifest+json", ".json": "application/json",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".gif": "image/gif", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        ".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
        # theme assets: fonts and the sound cues (Eorzea's are MP3)
        ".woff2": "font/woff2", ".woff": "font/woff", ".wav": "audio/wav", ".mp3": "audio/mpeg"}

# ---------------------------------------------------------------- the allowlist
# What the browser may reach on the api role. Anything else is 404. GET is the owner's reads
# (the inbox, one thread, attachments, the read API, the notebook's reads); POST is his
# own taps (reply/close/mute/snooze/attach/reopen, the work-pack park/backlog pair,
# filing a card, resort, the notebook's writes); PUT is only the note save.
_TID = r"[A-Za-z0-9._:-]{1,200}"
_ATT = r"[A-Za-z0-9._-]{1,80}"
# Every entry is a call the files under web/ make (grep them); a route the client's
# scripts use goes to the api role with their own bearer, never through here.
ALLOW_GET = re.compile(
    r"^/api/(meta|status)$"
    r"|^/api/triage/(threads|gate|events|search|dispatcher-heartbeat)$"
    r"|^/api/triage/threads/index$"                # the THREADS list: every thread, paged
    rf"|^/api/triage/threads/{_TID}$"
    rf"|^/api/triage/threads/{_TID}/conversation$"   # the Helm-side timeline
    rf"|^/api/triage/threads/{_TID}/attachments(/{_ATT}(/view)?)?$"   # /view: an agent's HTML for a frame (helm/preview.py)
    r"|^/api/(search|switcher|topic|day)$"
    rf"|^/api/thread/{_TID}/refs$"                  # a thread's in-app references (its sources and links)
    r"|^/api/notes/(folders|list|note|raw|meta|complete|resolve|templates|recent|changes|graph|tasks|atlas|tail|history)$"   # the reference's reads the notebook UI makes
    r"|^/att/[^\x00]{1,1024}$")                  # a vault attachment; the api role confines the path
ALLOW_POST = re.compile(
    rf"^/api/triage/threads/{_TID}/(reply|close|mute|snooze|attach|reopen|park|unpark|backlog|unbacklog|retry|wake)$"
    r"|^/api/triage/threads/create$"
    r"|^/api/triage/resort$"
    r"|^/api/day/append$"
    r"|^/api/notes/(new|daily/new|toggle|rename|attach)$")
ALLOW_PUT = re.compile(r"^/api/notes/note$")

# Body caps. A reply or an attachment legitimately carries base64 files (the gateway's own
# limit is 25 MB per file; base64 inflates by 4/3, so the cap clears it or the browser would
# see a confusing 413 from the wrong layer). A note save is a whole note. Everything else is
# a few hundred bytes, and keeping the small cap means a UI bug cannot turn a tap into a
# memory exhaustion.
MAX_BODY = 64 * 1024
MAX_ATTACH_BODY = 36 * 1024 * 1024
MAX_NOTE_BODY = 8 * 1024 * 1024
_BIG_BODY = re.compile(r"/(attach|reply)$")
_RAW_BODY = re.compile(r"^/api/notes/attach$")   # the body IS the file (X-Filename names it)

# Timeouts as the web role: 40 s ordinary, 300 s for a big upload, none for a stream.
TIMEOUT = 40.0
TIMEOUT_UPLOAD = 300.0

# ---------------------------------------------------------------- conversations
CONV_ONE = re.compile(rf"^/api/conversations/({_TID})$")
CONV_STREAM = re.compile(rf"^/api/conversations/({_TID})/stream$")
CONV_POLL_S = 3.0          # the stream: how often it reads the api
CONV_WINDOW_S = 55.0       # ...and how long one stream stays open before the browser reconnects


# ---------------------------------------------------------------- helpers
PRESS_PREFIX = "__use_draft__:"


def _is_press_body(body: bytes) -> bool:
    try:
        v = json.loads(body or b"{}")
    except ValueError:
        return False
    val = v.get("value") if isinstance(v, dict) else None
    return isinstance(val, str) and val.lstrip().lower().startswith(PRESS_PREFIX)


def _press_key() -> str:
    """HELM_WEB_PRESS_KEY: the proof, held only by this role and the api, that a request came from
    the owner's signed-in page. The api accepts a Use-the-draft press only with it (app/service.py:_web_press)."""
    return (os.environ.get("HELM_WEB_PRESS_KEY") or "").strip()


def _redact(msg: str) -> str:
    """The tokens never appear in a message on purpose, but an exception's text can carry
    a request's headers; every log line and every error detail passes through here."""
    if TOKEN:
        msg = msg.replace(TOKEN, "[token]")
    key = _press_key()
    if key:
        msg = msg.replace(key, "[press-key]")
    return msg


def _log(msg: str) -> None:
    """Every log line goes through here, redacted, and journal-friendly."""
    sys.stdout.write("%s %s\n" % (time.strftime("%H:%M:%S"), _redact(msg)))
    sys.stdout.flush()


def _json(status: int, obj, cache: str = "no-store") -> JSONResponse:
    return JSONResponse(obj, status_code=status, headers=_std_headers(cache))


def _err(status: int, error: str, detail: str = "") -> JSONResponse:
    return _json(status, {"ok": False, "error": error, "detail": detail or error})


def _std_headers(cache: str = "no-store") -> dict:
    return {"Cache-Control": cache, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def _client(base_url: str, timeout, transport=None) -> httpx.AsyncClient:
    kw = {"base_url": base_url, "timeout": timeout, "follow_redirects": False}
    if transport is not None:
        kw["transport"] = transport
    return httpx.AsyncClient(**kw)


def _peer_trusted(request: Request) -> bool:
    """Is this request's peer inside HELM_TRUSTED_PROXIES? The login header is believed
    only then: anywhere else it is a value the client made up."""
    peer = getattr(request.client, "host", None) or ""
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for net in TRUSTED_PROXIES:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


def _actor(request: Request) -> str:
    """Who this request acts as, or "" when nobody may be believed.

    HELM_DEV_LOGIN (a local quick start, no proxy in front) wins: it is refused at startup
    unless this role binds to loopback, so it cannot be reached from off the machine."""
    if DEV_LOGIN:
        return DEV_LOGIN
    if not _peer_trusted(request):
        return ""
    return (request.headers.get(AUTH_HEADER) or "").strip()


def _upstream_headers(request: Request, body_ctype: str | None) -> dict:
    """The headers the api role sees. Built fresh: nothing the client sent passes through
    except Accept, the body's Content-Type and (for the raw note upload) X-Filename."""
    h = {"Authorization": "Bearer " + TOKEN, "Accept": request.headers.get("accept") or "application/json"}
    if body_ctype:
        h["Content-Type"] = body_ctype
    actor = _actor(request)
    if actor:
        h["X-Helm-Actor"] = actor
        if _press_key():
            h["X-Helm-Press-Key"] = _press_key()
    if request.headers.get("x-filename"):
        h["X-Filename"] = request.headers["x-filename"]
    if _THREAD_VIEW.match(request.url.path) and request.headers.get("sec-fetch-dest"):
        h["Sec-Fetch-Dest"] = request.headers["sec-fetch-dest"][:20]   # the api answers a top-level navigation with the host page
    if request.url.path.startswith("/att/"):
        # a vault attachment seeks and revalidates (W11): the one Range and the ETag check only
        for name in ("range", "if-none-match"):
            if request.headers.get(name):
                h[name.title()] = request.headers[name][:200]
    return h


def _pass_headers(r: httpx.Response, cache: str = "no-store") -> dict:
    """Response headers that reach the browser: the content type, a download name and the
    api's trace id. Never Set-Cookie, never anything with the word Authorization in it."""
    h = _std_headers(cache)
    if r.headers.get("content-disposition"):
        h["Content-Disposition"] = r.headers["content-disposition"]
    if r.headers.get("x-trace-id"):
        h["X-Trace-ID"] = r.headers["x-trace-id"]
    for name in ("content-range", "accept-ranges", "etag"):   # an attachment's range answer (W11)
        if r.headers.get(name):
            h[name.title().replace("Etag", "ETag")] = r.headers[name]
    return h


def _path_qs(request: Request) -> str:
    """The path as the browser spelled it (raw, still percent-encoded) plus the query, so a
    vault attachment name with an encoded slash or space reaches the api role unchanged.
    The allowlist matched on the decoded path already."""
    raw = request.scope.get("raw_path")
    path = raw.decode("latin-1") if raw else request.url.path
    q = request.url.query
    return path + (("?" + q) if q else "")


async def _read_body(request: Request, cap: int):
    """The request body, or a 413/400 response. The cap is checked on Content-Length first
    and again on the bytes read, so a chunked body cannot walk past it."""
    n = request.headers.get("content-length")
    try:
        if n is not None and int(n) > cap:
            return None, _err(413, "body too large", "%s bytes exceeds the %d byte cap for %s" % (n, cap, request.url.path))
    except ValueError:
        return None, _err(400, "bad Content-Length")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            return None, _err(413, "body too large", "more than the %d byte cap for %s" % (cap, request.url.path))
    return body, None


def _require_json(body: bytes):
    try:
        json.loads(body or b"{}")
    except Exception:
        return _err(400, "body must be JSON")
    return None


# ---------------------------------------------------------------- the api proxy
async def _proxy(request: Request, method: str, body: bytes | None, ctype: str | None = None) -> Response:
    if not TOKEN:
        return _err(503, "web role has no HELM_APP_TOKEN", "the api cannot be reached without the token")
    path_qs = _path_qs(request)
    timeout = TIMEOUT_UPLOAD if (body is not None and len(body) > MAX_BODY) else TIMEOUT
    t0 = time.time()
    try:
        async with _client(API_URL, timeout, UPSTREAM_TRANSPORT) as c:
            r = await c.request(method, path_qs, content=body, headers=_upstream_headers(request, ctype))
    except Exception as e:  # api down, DNS, timeout
        _log("UPSTREAM-ERROR %s %s: %s" % (method, path_qs, _redact("%s: %s" % (type(e).__name__, e))))
        return _err(502, "gateway unreachable", _redact("%s: %s" % (type(e).__name__, e)))
    _log("%s %s -> %d (%.0fms)" % (method, path_qs, r.status_code, (time.time() - t0) * 1000))
    return Response(r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                    headers=_pass_headers(r))


class _Relay(StreamingResponse):
    """A StreamingResponse that always runs its `close` when it ends: finished, cancelled by a
    disconnect, or failed on a send (ASGI 2.4 raises there and skips any background task)."""

    def __init__(self, *a, close, **kw):
        super().__init__(*a, **kw)
        self._close = close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._close()


async def _proxy_att(request: Request, head: bool) -> Response:
    """A vault attachment, relayed chunk by chunk (never held whole: a video or a big download
    would sit in this role's memory), and a HEAD asked upstream as HEAD, answered with headers
    only. The api is asked for no encoding, so the raw bytes and their Content-Length relay as is."""
    if not TOKEN:
        return _err(503, "web role has no HELM_APP_TOKEN", "the api cannot be reached without the token")
    path_qs = _path_qs(request)
    method = "HEAD" if head else "GET"
    headers = dict(_upstream_headers(request, None), **{"Accept-Encoding": "identity"})
    t0 = time.time()
    c = _client(API_URL, TIMEOUT, UPSTREAM_TRANSPORT)
    try:
        r = await c.send(c.build_request(method, path_qs, headers=headers), stream=True)
    except Exception as e:  # api down, DNS, timeout
        await c.aclose()
        _log("UPSTREAM-ERROR %s %s: %s" % (method, path_qs, _redact("%s: %s" % (type(e).__name__, e))))
        return _err(502, "gateway unreachable", _redact("%s: %s" % (type(e).__name__, e)))
    _log("%s %s -> %d (%.0fms)" % (method, path_qs, r.status_code, (time.time() - t0) * 1000))
    out = _pass_headers(r, "private, max-age=300")
    if r.headers.get("content-length"):
        out["Content-Length"] = r.headers["content-length"]
    media = r.headers.get("content-type", "application/json")
    closed = []

    async def close_once():
        if not closed:
            closed.append(1)
            try:
                await r.aclose()
            finally:
                await c.aclose()
    if head:
        await close_once()
        return Response(b"", status_code=r.status_code, media_type=media, headers=out)
    return _Relay(r.aiter_raw(), status_code=r.status_code, media_type=media, headers=out, close=close_once)


# ---------------------------------------------------------------- meta and conversations
async def _meta(request: Request) -> Response:
    """The api's /api/meta, plus what only this role knows about itself."""
    resp = await _proxy(request, "GET", None)
    if resp.status_code != 200:
        return resp
    try:
        d = json.loads(resp.body)
    except Exception:
        return resp
    if isinstance(d, dict):
        d["web"] = {"owner": OWNER}
    return _json(200, d)


async def _helm_conversation(tid: str) -> dict:
    async with _client(API_URL, TIMEOUT, UPSTREAM_TRANSPORT) as c:
        r = await c.get("/api/triage/threads/%s/conversation" % tid,
                        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json"})
    if r.status_code != 200:
        raise LookupError(r.status_code)
    return r.json()


async def _conversation_data(tid: str) -> dict:
    """One conversation for the screens: the api's own (see the module docstring). Raises
    LookupError(status) when the api does not have it."""
    return await _helm_conversation(tid)


async def _conversation(request: Request, tid: str) -> Response:
    if not TOKEN:
        return _err(503, "web role has no HELM_APP_TOKEN", "the api cannot be reached without the token")
    try:
        return _json(200, await _conversation_data(tid))
    except LookupError as e:
        code = e.args[0] if e.args and isinstance(e.args[0], int) else 502
        if code == 404:
            return _err(404, "no such conversation")
        return _err(502, "conversation unreachable", "the api answered %s" % code)
    except Exception as e:
        _log("CONV-ERROR %s: %s" % (tid, _redact("%s: %s" % (type(e).__name__, e))))
        return _err(502, "conversation unreachable", _redact("%s: %s" % (type(e).__name__, e)))


async def _conversation_stream(request: Request, tid: str) -> Response:
    if not TOKEN:
        return _err(503, "web role has no HELM_APP_TOKEN", "the api cannot be reached without the token")

    # A thread the api does not hold is said once, as a 404 (an EventSource that gets a non-200 does
    # not retry), and a stream that finds it gone ends: the page has stopped listening, and the
    # loop would otherwise ask the api for it again each time the 404 memo runs out.
    first, first_err = None, None
    try:
        first = await _conversation_data(tid)
    except LookupError as e:
        if e.args and e.args[0] == 404:
            return _err(404, "no such conversation")
        first_err = e
    except Exception as e:
        first_err = e

    async def frames():
        last, end = None, time.time() + CONV_WINDOW_S
        pending, err = first, first_err
        while True:
            try:
                if err is not None:
                    raise err
                d = pending if pending is not None else await _conversation_data(tid)
                key = json.dumps([d.get(k) for k in ("events", "thread_state", "ask", "pending_notes", "properties", "review_state", "console")],
                                 sort_keys=True, default=str)
                if key != last:
                    last = key
                    yield ("data: " + json.dumps(d) + "\n\n").encode()
            except LookupError as e:
                if e.args and e.args[0] == 404:
                    return   # it was removed while this stream was open
                _log("CONV-STREAM-ERROR %s: %s" % (tid, _redact("%s: %s" % (type(e).__name__, e))))
            except Exception as e:
                _log("CONV-STREAM-ERROR %s: %s" % (tid, _redact("%s: %s" % (type(e).__name__, e))))
            pending, err = None, None
            if time.time() >= end:
                return
            await asyncio.sleep(CONV_POLL_S)
    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------- the installed app
# One shell, one app: the page installs as Helm, and this role serves Helm's own
# manifest and head. There is nothing per install to swap.

# ---------------------------------------------------------------- static
def _static(rel: str, head: bool = False) -> Response:
    """One file under WEB_ROOT. The path is resolved (symlinks followed) and must still sit
    under the root; `..`, encoded traversal, absolute paths and escaping symlinks all land
    outside and are 404. sw.js is never cached and may control the whole origin."""
    if "\x00" in rel:
        return _err(404, "not found")
    rel = rel.lstrip("/")
    try:
        f = (WEB_ROOT / rel).resolve()
        if not f.is_relative_to(WEB_ROOT) or not f.is_file():
            return _err(404, "not found")
    except (OSError, RuntimeError, ValueError):   # a name too long for the filesystem included
        return _err(404, "not found")
    ctype = MIME.get(f.suffix.lower(), "application/octet-stream")
    headers = _std_headers("no-cache")   # small app; no-cache lets a version bump land without stale shells
    if f.name == "index.html" and f.parent == WEB_ROOT:
        # finding 5: a prompt-injected reply must not make the browser fetch an outside address that carries chat
        # text; the renderer allows only this origin, blob: and data:image/, and the shell's CSP matches it.
        headers["Content-Security-Policy"] = "img-src 'self' data: blob:"
    body = None   # set for the two files this role rewrites on the way out
    if f.name == "sw.js" and f.parent == WEB_ROOT:
        headers["Service-Worker-Allowed"] = "/"
        body = f.read_bytes().replace(SW_VERSION_PLACEHOLDER, _sw_version().encode(), 1)
    if body is not None:
        resp = Response(b"" if head else body, media_type=ctype, headers=headers)
        if head:
            resp.headers["Content-Length"] = str(len(body))
        return resp
    data = b"" if head else f.read_bytes()
    resp = Response(data, media_type=ctype, headers=headers)
    if head:
        resp.headers["Content-Length"] = str(f.stat().st_size)
    return resp


# The service worker's VERSION (web/sw.js holds the placeholder): a digest over every file
# under WEB_ROOT, names and contents, so any static change gives installed clients a new
# cache and an unchanged tree keeps the old one. Nobody bumps it by hand, so two open web
# PRs never conflict on it. Symlinks are skipped (one may lead out of the root). Rehashed
# only when a file's name, size or mtime changes.
SW_VERSION_PLACEHOLDER = b"__HELM_SW_VERSION__"
_sw_cache = {"sig": None, "version": ""}
_sw_lock = threading.Lock()


def _sw_version() -> str:
    files = []
    for dp, _, fns in os.walk(WEB_ROOT):
        for fn in fns:
            p = Path(dp) / fn
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            files.append((p.relative_to(WEB_ROOT).as_posix(), st.st_mtime_ns, st.st_size, p))
    files.sort()
    sig = (str(WEB_ROOT), tuple(f[:3] for f in files))
    with _sw_lock:
        if _sw_cache["sig"] == sig:
            return _sw_cache["version"]
    h = hashlib.sha256()
    for rel, _, _, p in files:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        h.update(b"%s\0%d\0" % (rel.encode(), len(data)))
        h.update(data)
    version = "helm-app-" + h.hexdigest()[:12]
    with _sw_lock:
        _sw_cache.update(sig=sig, version=version)
    return version


# ---------------------------------------------------------------- dispatch
# Every /att answer, the file or a refusal: sandboxed (no script, no origin if opened) and
# never sniffed. Set on the way out, so no path through the dispatcher can miss it.
ATT_HEADERS = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}


_THREAD_ATT = re.compile(rf"^/api/triage/threads/{_TID}/attachments/{_ATT}$")
# An agent's HTML opened for a frame. It leaves under helm.preview.VIEW_CSP (set here, whatever the
# api sent): an opaque origin and no network. Never under the plain `sandbox` above, and a refusal
# on this route carries the same policy.
_THREAD_VIEW = re.compile(rf"^/api/triage/threads/{_TID}/attachments/{_ATT}/view$")
VIEW_CSP = _preview.VIEW_CSP

_client_err_window: dict = {}      # dedupe key -> _now() when last sent
_client_err_minute = [0.0, 0]      # [window start, reports sent in it]


def _client_err_reset() -> None:
    _client_err_window.clear()
    _client_err_minute[:] = [0.0, 0]


def _one_line(v, n: int) -> str:
    s = v if isinstance(v, str) else ("" if v is None else str(v))
    s = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", s)   # no ESC/BEL/NUL: a page cannot write terminal sequences into the log
    s = " ".join(s.split())
    return s[:n] + ("…" if len(s) > n else "")


_ERROR_CLASS = re.compile(r"^(?:Uncaught\s+)?([A-Za-z_$][\w$]{0,40}(?:Error|Exception))\b")   # "TypeError: ..." -> "TypeError"


async def _client_error(request: Request) -> Response:
    """One error the page caught (web/errors.js): logged as one CLIENT-ERROR line and handed
    to the error tracker, grouped by message and file. Never proxied; answers 204 whatever happens next."""
    body, bad = await _read_body(request, CLIENT_ERROR_MAX_BODY)
    if bad:
        return bad
    try:
        rep = json.loads(body or b"{}")
    except Exception:
        return _err(400, "bad json", "the report is not JSON")
    if not isinstance(rep, dict):
        return _err(400, "bad json", "the report is not an object")
    kind = _one_line(rep.get("kind") or "error", 20)
    msg = _redact(_one_line(rep.get("msg"), 300)) or "(no message)"
    src = _one_line(rep.get("src"), 300)
    fname = src.split("?", 1)[0].rsplit("/", 1)[-1] or "(inline)"
    line, col = _one_line(rep.get("line"), 8), _one_line(rep.get("col"), 8)
    view = _one_line(rep.get("view"), 120)
    stack = rep.get("stack")
    # The report comes from the page, so its message and its stack can carry whatever the page
    # was showing (a thread's body, a note, an amount). The log and the error tracker keep the exception
    # class and the file:line:col the error came from; the free text and the stack do not leave.
    m = _ERROR_CLASS.match(msg)
    msg = m.group(1) if m else "Error"
    stack = None
    actor = _one_line(_actor(request), 60)
    now = _now()
    key = (actor, kind, msg, fname, line)
    if now - _client_err_window.get(key, -1e18) < CLIENT_ERROR_DEDUPE_S:
        return Response(status_code=204, headers=_std_headers())
    if now - _client_err_minute[0] >= 60.0:
        _client_err_minute[:] = [now, 0]
    if _client_err_minute[1] >= CLIENT_ERROR_MAX_PER_MIN:
        if _client_err_minute[1] == CLIENT_ERROR_MAX_PER_MIN:
            _log("CLIENT-ERROR-STORM more than %d reports this minute; the rest are dropped" % CLIENT_ERROR_MAX_PER_MIN)
            _client_err_minute[1] += 1
        return Response(status_code=204, headers=_std_headers())
    _client_err_minute[1] += 1
    _client_err_window[key] = now
    if len(_client_err_window) > 1024:
        for k in [k for k, t in _client_err_window.items() if now - t >= CLIENT_ERROR_DEDUPE_S]:
            _client_err_window.pop(k, None)
    _log("CLIENT-ERROR %s %s %s: %s @ %s:%s:%s ua=%s" % (actor, view or "-", kind, msg, fname, line, col,
                                                        _one_line(rep.get("ua"), 80)))
    if sentry_sdk is not None:
        try:
            sentry_sdk.capture_message(
                "client %s: %s" % (kind, msg), level="error",
                fingerprint=["web-client", kind, re.sub(r"\d+", "N", msg)[:200], fname],
                tags={"role": "web", "source": "client"},
                extras={"src": _redact(src), "line": line, "col": col, "view": view,
                        "stack": _redact(stack[:4000] if isinstance(stack, str) else ""),
                        "ua": _one_line(rep.get("ua"), 200), "actor": actor})
        except Exception:
            pass
    return Response(status_code=204, headers=_std_headers())


async def handle(request: Request) -> Response:
    try:
        resp = await _dispatch(request)
    except ClientDisconnect:
        return Response(status_code=499)   # the browser left mid-request: nothing to report
    except Exception as e:
        # A JSON 500 that says nothing internal, one redacted log line, and the error tracker.
        _log("WEB-ERROR %s %s: %s" % (request.method, request.url.path, _redact("%s: %s" % (type(e).__name__, e))))
        if sentry_sdk is not None:
            try:
                sentry_sdk.capture_exception(e)
            except Exception:
                pass
        resp = _err(500, "internal error", "the web role failed on this request; it is logged and reported")
    if request.url.path.startswith("/att/") or _THREAD_ATT.match(request.url.path):
        resp.headers.update(ATT_HEADERS)   # a thread's attachment too (ledger GW-075)
    elif _THREAD_VIEW.match(request.url.path):
        # The page itself (?raw=1, framed) leaves under the page policy; the host page, and any
        # refusal, under the host's. Decided here again, not taken from the api's answer.
        raw = _preview.serves_raw(request.query_params.get("raw"), request.headers.get("sec-fetch-dest"))
        resp.headers.update(_preview.VIEW_HEADERS if raw and resp.status_code == 200 else _preview.WRAPPER_HEADERS)
    return resp


async def _dispatch(request: Request) -> Response:
    path = request.url.path
    method = request.method.upper()
    if REQUIRE_AUTH_HEADER and not _actor(request):
        # On by default: the port is reachable only through a forward-auth proxy, and a request
        # without the login header did not come that way. Static files too: the page
        # holds nothing secret, but a screen that renders without a login is a screen
        # that will later act as nobody.
        return _err(401, "unauthorized", "X-Forwarded-User is required")
    if any(seg in (".", "..") for seg in path.split("/")):
        # The allowlist is judged on the path as sent, while the upstream client normalises
        # dot segments away; a path that needs normalising is not one the screens send.
        return _err(404, "not found")

    if method in ("GET", "HEAD"):
        head = method == "HEAD"
        if path in ROOT_FILES:
            return _static(ROOT_FILES[path], head)
        if path.startswith("/api/") or path.startswith("/att/"):
            if path == "/api/meta":
                return await _meta(request)
            m = CONV_ONE.match(path)
            if m:
                return await _conversation(request, m.group(1))
            m = CONV_STREAM.match(path)
            if m:
                return await _conversation_stream(request, m.group(1))
            if ALLOW_GET.match(path):
                if path.startswith("/att/"):
                    return await _proxy_att(request, head)
                return await _proxy(request, "GET", None)
            return _err(404, "not found")
        return _static(path, head)

    if method == "POST":
        if path == "/api/client-error":   # the page's own error reports: handled here, never proxied
            return await _client_error(request)
        if not ALLOW_POST.match(path):
            return _err(404, "not found")
        if _RAW_BODY.match(path):
            # The notebook's attach: the body is the file itself, named by X-Filename.
            body, bad = await _read_body(request, MAX_ATTACH_BODY)
            if bad:
                return bad
            return await _proxy(request, "POST", body, request.headers.get("content-type") or "application/octet-stream")
        cap = MAX_ATTACH_BODY if _BIG_BODY.search(path) else MAX_BODY
        body, bad = await _read_body(request, cap)
        if bad:
            return bad
        bad = _require_json(body)
        if bad:
            return bad
        if path.endswith("/reply") and _is_press_body(body) and request.headers.get("x-helm-press") != "confirmed":
            # A Use-the-draft press is made by the confirm sheet, which sends this header; a cross-site
            # form or an image tag cannot set it, and a script on another origin cannot pass the preflight.
            return _err(403, "a draft press comes only from the draft's confirm sheet", "reload the card and press the button")
        return await _proxy(request, "POST", body or b"{}", "application/json")

    if method == "PUT":
        if not ALLOW_PUT.match(path):
            return _err(404, "not found")
        body, bad = await _read_body(request, MAX_NOTE_BODY)
        if bad:
            return bad
        bad = _require_json(body)
        if bad:
            return bad
        return await _proxy(request, "PUT", body or b"{}", "application/json")

    return _err(404, "not found")


# gzip when the browser asks (2026-09-26 live sweep: nothing was compressed). Never an event stream (it would buffer), never media or a file
# that is already compressed or is an attachment's raw bytes.
GZIP_SKIP = ("text/event-stream", "image/*", "audio/*", "video/*", "font/*",
             "application/octet-stream", "application/zip", "application/gzip", "application/x-gzip", "application/pdf")


def build_app() -> Starlette:
    return Starlette(routes=[Route("/{path:path}", handle, methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])],
                     middleware=[Middleware(GZipMiddleware, minimum_size=1024, exclude_content_types=GZIP_SKIP)])


app = build_app()

# HELM_DEV_LOGIN is a login every request acts as, with no proxy to check it. That is only
# honest for a local quick start, so refuse to start with it set on anything but loopback.
if DEV_LOGIN:
    _host = BIND_HOST or "0.0.0.0"
    if _host in ("localhost",):
        _local = True
    else:
        try:
            _local = ipaddress.ip_address(_host).is_loopback
        except ValueError:
            _local = False
    if not _local:
        sys.stderr.write("HELM_DEV_LOGIN is set but this role binds to %s: refusing to start "
                         "(a dev login is for loopback only)\n" % _host)
        raise SystemExit(2)

if __name__ == "__main__":  # python3 -m app.web: a local run; the image uses helm-entrypoint.sh
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HELM_HOST", "127.0.0.1"), port=PORT)
