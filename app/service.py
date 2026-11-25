"""Helm standalone service: thin HTTP over the P1 store.

Contract: docs/contracts/P2.md. Every handler is parse -> one store call -> JSON.
The store (helm/store.py) stays free of HTTP. Compatibility target: the live
gateway (the reference client's tree/) that a mobile app and web UI speak today.
"""
import hmac
import json
import math
import os
import sys
import time

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request, ClientDisconnect
from starlette.responses import JSONResponse, Response, HTMLResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route, compile_path

import helm.store as store
from helm import actions as _actions
from helm import site as _site
from helm import tokens as _tokens
from helm import card_rules as _card_rules
from helm import conversation as _conversation
import collections
import re
import threading
import traceback

# Error tracking (2026-09-21, wiki Docker/error-tracker): every unhandled request error the gateway
# already records (_record_unhandled_error) is also sent to the error tracker whose
# sweep hands each NEW issue to an agent as one Helm card. No HELM_SENTRY_DSN in the env = off.
try:
    import sentry_sdk
    if os.environ.get("HELM_SENTRY_DSN"):
        from helm.scrub import before_send, event_scrubber
        sentry_sdk.init(dsn=os.environ["HELM_SENTRY_DSN"], environment=os.environ.get("HELM_SENTRY_ENV", "home"),
                        server_name="helm-gateway", traces_sample_rate=0.0, send_default_pii=False,
                        event_scrubber=event_scrubber(), before_send=before_send)
    else:
        sentry_sdk = None
except Exception:
    sentry_sdk = None

TOKEN = os.environ.get("HELM_APP_TOKEN", "")
DB_PATH = os.environ.get("HELM_DB", "data/helm.db")
OPEN_PATHS = {"/api/meta", "/metrics"}


def _authorized(header: str) -> bool:
    """Constant-time `Authorization: Bearer <token>` check, header only.

    A token in a query string is written verbatim into access logs, proxy logs
    and browser history, so there is no `?token=` fallback. A malformed or
    non-ASCII header is unauthorized, never an exception.
    """
    try:
        provided = header.encode("utf-8")
        expected = ("Bearer " + TOKEN).encode("utf-8")
    except (AttributeError, UnicodeError):
        return False
    return hmac.compare_digest(provided, expected)


def _principal(header: str):
    """"" for the site's HELM_APP_TOKEN, a principal for a per-caller token
    (helm.tokens), None when the bearer matches nothing."""
    if _authorized(header):
        return ""
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return None
    return _tokens.principal_for(header[7:].strip(), TOKEN, _db)


# Metric labels are bounded by the route table. telemetry_gate is the outer
# middleware, so every request reaching the port is counted -- including the
# 401s and 404s of a client that never authenticated. Keying those by raw path
# would let anyone grow these dicts, and /metrics with them, without limit.
UNMATCHED_PATH = "/<unmatched>"
_PATH_PARAM_RE = re.compile(r"(\{[^{}]+\})")
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH",
                            "DELETE", "OPTIONS", "TRACE", "CONNECT"})
_ROUTE_METRIC_KEYS = []

# Read-only aliases for the REST shape agents guess (`GET /api/threads`; Otto's local router
# has `/api/threads/{id}/respond`). Same handler, same bearer gate, GET only. Each is counted
# under its canonical route, so a guess neither opens a series of its own nor lands in
# /<unmatched> and cards the error watch.
_ROUTE_ALIASES = {
    "/api/threads": "/api/triage/threads",
    "/api/threads/{thread_id}": "/api/triage/threads/{thread_id}",
    # `read` is the verb agents guess for a notebook read; the route is /api/notes/raw.
    "/api/notes/read": "/api/notes/raw",
    # The read vocabulary kept from the era before the notebook was built.
    "/api/notes/get": "/api/notes/raw",
    # The REST plural for a note's revision list; the route is /api/notes/history.
    "/api/notes/revisions": "/api/notes/history",
    "/api/notes": "/api/notes/list",
    "/api/notes/search": "/api/search",
    "/api/fs/list": "/api/notes/list",
    "/api/space/list": "/api/notes/list",
}
# An unrouted path under one of these prefixes is a guess at the thread or notes API: its 404
# says where the real route is.
_GUESS_HINTS = {
    "/api/thread": "no such route; threads are GET /api/triage/threads and GET /api/triage/threads/{id}",
    "/api/board": "no such route; threads are GET /api/triage/threads and GET /api/triage/threads/{id}",
    "/api/notes": ("no such route; a note is read with GET /api/notes/raw?rel=<path>.md, "
                   "its revisions are listed with GET /api/notes/history?rel=<path>.md"),
    "/api/fs": "no such route; a folder is listed with GET /api/notes/list?folder=<path>",
    "/api/space": "no such route; a folder is listed with GET /api/notes/list?folder=<path>",
}


def _compile_route_metric_keys(templates):
    """One (matcher, label) pair per registered route path, in declaration order.

    Path parameters collapse to `:id`, which is the label the old id regex
    already produced for `t-` thread ids, so existing series do not churn.
    The matcher is Starlette's own, so a path is counted under a route exactly
    when the router dispatches it there (`{rel:path}` spans slashes, `{n:int}`
    is digits only).
    """
    keys = []
    seen = set()
    for template in templates:
        if template in seen:
            continue
        seen.add(template)
        keys.append((compile_path(template)[0],
                     _PATH_PARAM_RE.sub(":id", _ROUTE_ALIASES.get(template, template))))
    return keys


def _metric_path(path: str) -> str:
    for matcher, label in _ROUTE_METRIC_KEYS:
        if matcher.match(path):
            return label
    return UNMATCHED_PATH

_metrics_lock = threading.Lock()
_request_counts = collections.defaultdict(int)
_request_duration_sum = collections.defaultdict(float)
_request_errors = collections.defaultdict(int)


def _record_http_metric(method: str, path: str, status: int, duration_s: float):
    norm_path = _metric_path(path)
    norm_method = method if method in _KNOWN_METHODS else "OTHER"
    with _metrics_lock:
        _request_counts[(norm_method, norm_path, status)] += 1
        _request_duration_sum[(norm_method, norm_path)] += duration_s
        if status >= 500:
            _request_errors[(norm_method, norm_path, status)] += 1


# Client errors (2026-09-26, the Helm error observability project, component 8). A
# refused request used to leave only uvicorn's access line, so nobody could say WHY a caller
# got its 4xx. telemetry_gate now writes one line per 4xx and counts it by a fixed kind. The
# line carries the trace, method, path, status, kind and the answer's own `detail` -- never
# the Authorization value, the site token or the request body -- and the metric labels are
# the route table and the kinds below, never text.
_CLIENT_ERROR_KINDS = {
    400: "validation_error", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 410: "gone", 413: "too_large",
    415: "unsupported_media_type", 422: "validation_error", 429: "rate_limited",
}
_CLIENT_ERROR_TEXT_MAX = 200
_CLIENT_ERROR_BODY_MAX = 64 * 1024   # a bigger 4xx body is not parsed for its detail
_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_CONTROL_RE = re.compile("[\x00-\x1f\x7f\x85  ]")
_client_errors = collections.defaultdict(int)


def _log_text(text, limit: int = _CLIENT_ERROR_TEXT_MAX) -> str:
    """Redacted, one line, bounded: fit for a log line a client's input can reach."""
    text = str(text)
    if TOKEN:
        text = text.replace(TOKEN, "[REDACTED]")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    return _CONTROL_RE.sub(" ", text).replace('"', "'")[:limit]


def _log_word(text, limit: int = _CLIENT_ERROR_TEXT_MAX) -> str:
    """_log_text with no spaces, for the space-separated fields (trace, path)."""
    return _log_text(text, limit * 3).replace(" ", "%20")[:limit] or "-"


def _client_error_detail(body: bytes) -> str:
    """The reason a 4xx answer already gives its caller: `detail` (or `error`) of a JSON
    body, the first rejected thread's errors for a PUT, or a plain-text body as is."""
    if not body or len(body) > _CLIENT_ERROR_BODY_MAX:
        return ""
    text = body.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
    except ValueError:
        return text.strip()
    if not isinstance(obj, dict):
        return ""
    rejected = obj.get("rejected")
    if isinstance(rejected, list) and rejected and isinstance(rejected[0], dict):
        errs = rejected[0].get("errors")
        errs = "; ".join(str(e) for e in errs) if isinstance(errs, list) else str(errs or "")
        return "threads were NOT stored (rejected=%d): %s" % (len(rejected), errs)
    detail = obj.get("detail") or obj.get("error") or ""
    return detail if isinstance(detail, str) else json.dumps(detail)


# Scanner probes. The api role is bearer-only but reachable from the internet, and
# scanners ask it for /.aws/credentials, /.git/config, /wp and the like. The gate's 401 is the
# right answer, but counted as `GET /<unmatched> 401` and logged per request it read as a new
# error kind in the error watch. An unauthenticated request for a path no route serves is
# therefore counted here, by method only, and leaves no request series and no client_error line.
# An authenticated request for an unrouted path is a real client's mistake and keeps its counted
# 404.
_unauth_probes = collections.defaultdict(int)


def _is_unauth_probe(request) -> bool:
    return bool(getattr(request.state, "unauth_probe", False))


def _record_unauth_probe(method: str):
    norm_method = method if method in _KNOWN_METHODS else "OTHER"
    with _metrics_lock:
        _unauth_probes[norm_method] += 1


_BATCH_IDS_SHOWN = 20


def _batch_ids(threads) -> str:
    """The thread ids of a producer batch for a log line / refusal row: comma-joined, the first
    _BATCH_IDS_SHOWN, then `+N` for the rest. Ids only, never a title or any body text."""
    if not isinstance(threads, list):
        return ""
    ids = [t.get("thread_id") for t in threads if isinstance(t, dict)]
    ids = [_log_word(i, 80).replace(",", "%2C") for i in ids if isinstance(i, str) and i]
    shown = ids[:_BATCH_IDS_SHOWN]
    if len(ids) > len(shown):
        shown.append("+%d" % (len(ids) - len(shown)))
    return ",".join(shown)


def _record_client_error(trace_id: str, method: str, path: str, status: int, detail: str,
                         client: str = "", user_agent: str = "", caller: str = "", threads: str = ""):
    """Count one 4xx and write its single stderr line. client/user_agent/caller (the caller's own
    X-Caller label) and, for a refused PUT batch, the thread ids say WHO sent it and WHAT it carried."""
    kind = _CLIENT_ERROR_KINDS.get(status, "client_error")
    norm_method = method if method in _KNOWN_METHODS else "OTHER"
    with _metrics_lock:
        _client_errors[(norm_method, _metric_path(path), status, kind)] += 1
    sys.stderr.write('helm client_error: trace=%s %s %s status=%d kind=%s detail="%s" client=%s ua="%s" caller="%s"%s\n' % (
        _log_word(trace_id, 64), norm_method, _log_word(path), status, kind, _log_text(detail),
        _log_word(client, 64), _log_text(user_agent), _log_text(caller),
        " threads=" + _log_word(threads, 400) if threads else ""))
    sys.stderr.flush()


def _write_refusal(**fields):
    """Blocking: one refused_requests row. Never raises (a record never costs the caller its answer)."""
    try:
        con = _db()
        try:
            store.record_refusal(con, **fields)
        finally:
            con.close()
    except Exception:
        pass


def _record_unhandled_error(trace_id: str, method: str, path: str, exc: Exception):
    if sentry_sdk is not None:
        try:
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
    try:
        con = _db()
        try:
            tb = traceback.format_exc()
            details = f"{type(exc).__name__}: {str(exc)}\n{tb}"
            store.record_operational_error(con, trace_id, "gateway", f"{method} {path}", details)
        finally:
            con.close()
    except Exception:
        pass


def _db():
    con = store.db(DB_PATH)
    con.execute("PRAGMA foreign_keys=ON")
    store.init_claims(con)
    return con


def _rej(exc):
    # {ok:false, detail} is the superset of both old shapes: home clients read
    # `detail`; the pollers read `ok` too.
    body = {"ok": False, "detail": exc.detail}
    if getattr(exc, "extra", None):
        body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status)


async def _object_body(request, optional=False):
    """(body, None) for a JSON object body, else (None, a 400 response).

    A body that is not JSON, not UTF-8 or not an object is the caller's mistake, never a 500
    (a 500 pages the error tracker and costs the producer its payload). optional=True takes an empty body
    as {}; a body that is present must still be an object.
    """
    raw = await request.body()
    if optional and not raw.strip():
        return {}, None
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        body = None
    if not isinstance(body, dict):
        return None, JSONResponse({"ok": False, "detail": "object body required"}, status_code=400)
    # Who the caller says it is, kept for the refusal record (telemetry_gate) -- not the body.
    who = body.get("actor") or body.get("author")
    request.state.body_actor = who if isinstance(who, str) else ""
    return body, None


def _actor_of(request) -> str | None:
    """Who is acting, as the web role forwards it (X-Helm-Actor, the Authentik login).

    A login is a person at one of the owner's own screens, and the only person is the owner:
    the header maps to the actor "owner" at both sites (work's `by: "owner"`, which
    a page injected for every tap). A bearer-only caller that sends
    nothing gets the site's default (helm.store.normalize_by: owner at home, a chat
    session on a second site). The login itself is kept for the access log only.
    """
    v = request.headers.get("x-helm-actor", "")
    return store.BY_OWNER if v.strip() else None


def _web_press(request):
    """The proof that a request came from the owner's signed-in page, or None.

    The web role (behind Authentik) is the only caller that holds HELM_WEB_PRESS_KEY: it sends it as
    X-Helm-Press-Key next to the login it forwards as X-Helm-Actor. Every other bearer holder has the
    site token and can send any X-Helm-Actor, so the login alone proves nothing; with the key unset
    on this site nobody can press (fail closed). Only a Use-the-draft press asks for this."""
    press_key = os.environ.get("HELM_WEB_PRESS_KEY", "").strip()
    given = request.headers.get("x-helm-press-key", "")
    login = request.headers.get("x-helm-actor", "").strip()
    try:
        ok = bool(press_key) and bool(given) and hmac.compare_digest(given.encode("utf-8"), press_key.encode("utf-8"))
    except UnicodeError:
        ok = False
    return {"via": store.WEB_UI, "login": login} if ok and login else None


def _caller() -> str:
    """The bearer caller's name for a hand-off it makes (helm.store.handoff_source): its
    per-caller token's principal, else "api" for the site's shared token."""
    return _tokens.PRINCIPAL.get() or store.BY_API


# ---------------------------------------------------------------- meta/health


VERSION = "2.0.0-helm-app"


async def meta(request):
    """Liveness and the site profile. Unauthenticated on purpose so a probe never
    needs the token. A superset of both old shapes: home's {ok, service, version,
    time, auth} plus the read view the screens take."""
    live = needs_you = None
    try:
        con = _db()
        try:
            live = con.execute("SELECT COUNT(*) c FROM threads WHERE tombstoned=0 AND suppressed=0"
                               " AND state != 'done'").fetchone()["c"]
            needs_you = con.execute("SELECT COUNT(*) c FROM threads WHERE tombstoned=0 AND"
                                    " suppressed=0 AND state='needs-you' AND wake_at=0").fetchone()["c"]
        finally:
            con.close()
    except Exception:
        pass
    prof = _site.describe()
    return JSONResponse({
        "ok": True,
        "service": "helm",
        "version": VERSION,
        "build": os.environ.get("HELM_BUILD_SHA", ""),
        "time": time.time(),
        "auth": "bearer" if TOKEN else "disabled",
        "modules": prof["modules"],
        "notes_mode": prof["notes_mode"],
        "live": live,
        "needs_you": needs_you,
    })


async def health(request):
    con = _db()
    try:
        h = store.health(con)
    finally:
        con.close()
    return JSONResponse({"ok": True, "health": h})


_STARTED = time.time()


def _heartbeat_blob():
    """The dispatcher's published heartbeat (the pump at home, the poller) plus its age."""
    try:
        with open(DISPATCHER_HB_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {"known": False,
                "detail": "the dispatcher has never published a heartbeat here"}
    if not isinstance(d, dict):
        return {"known": False, "detail": "heartbeat is not an object"}
    d = dict(d)
    d["known"] = True
    try:
        d["age_s"] = max(0.0, time.time() - float(d.get("ts") or 0))
    except (TypeError, ValueError):
        d["age_s"] = None
    return d


async def status(request):
    con = _db()
    try:
        h = store.health(con)
        sup = store.list_suppressions(con)
        quiet = store.quiet_runs(con)
        # The activity page draws the executor runs, what they cost and the dispatcher's log
        # tail from /api/status, so both shapes carry them. The runs arrive on the dispatcher
        # heartbeat (dispatcher_heartbeat_put), not from the app itself.
        runs = store.recent_runs(con, limit=8)
        costs = store.run_costs(con, top=8)
        if store.rules().status_work_shape:
            # Work Helm's /api/status (app/status.py), built from the dispatcher
            # heartbeat a client publishes rather than Task Scheduler,
            # which does not exist here. The runs, their costs and the log tail are
            # what the poller sent on that heartbeat; empty until it sends them.
            act = store.activity(con, limit=60)
    finally:
        con.close()
    if store.rules().status_work_shape:
        return JSONResponse({
            "ok": True,
            "now": time.time(),
            "server": {"started": _STARTED, "uptime_s": time.time() - _STARTED,
                       "pid": os.getpid(), "writeback": False},
            "dispatcher": _heartbeat_blob(),
            "dispatcher_log": _dispatcher_log(), "server_log": [], "tasks": {},
            "health": h, "activity": act, "runs": runs, "costs": costs,
            "suppressions": len(sup),
            "quiet_runs": quiet,
        })
    return JSONResponse({
        "ok": True,
        "generated_at": time.time(),
        "health": h,
        "suppressions": len(sup),
        "quiet_runs": quiet,
        "runs": runs,
        "costs": costs,
        "dispatcher_log": _dispatcher_log(),
        "db_path": os.path.abspath(DB_PATH),
        "db_age_s": max(0.0, time.time() - os.path.getmtime(DB_PATH)) if os.path.exists(DB_PATH) else None,
    })


# ------------------------------------------------------------------ threads


async def threads(request):
    include_done = request.query_params.get("include_done") in ("1", "true", "yes")
    include_snoozed = request.query_params.get("include_snoozed") in ("1", "true", "yes")
    include_backlog = request.query_params.get("include_backlog") in ("1", "true", "yes")
    con = _db()
    try:
        out = store.list_threads(con, include_done=include_done,
                                 include_snoozed=include_snoozed,
                                 include_backlog=include_backlog)
        woke = out["woke"]
    finally:
        con.close()
    return JSONResponse(out)


async def put_threads(request):
    payload, bad = await _object_body(request)
    if bad:
        return bad
    # What the producer sent, for the refusal log (ids only; telemetry_gate reads these).
    request.state.batch_ids = _batch_ids(payload.get("threads"))
    con = _db()
    try:
        out = store.put_threads(con, payload)
        rejected = out.get("rejected") or []
        request.state.first_rejected_id = next(
            (r["thread_id"] for r in rejected if isinstance(r, dict) and isinstance(r.get("thread_id"), str)), "")
        if out.get("rejected_count", 0) > 0 and (
                out.get("accepted") == 0 or store.rules().put_400_on_any_reject):
            return JSONResponse(out, status_code=400)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=200)


async def create_thread(request):
    """Hand-written card. Live route: /api/triage/threads/create (201).

    The store mints the id, skips mute rules, and sets state+ask together via
    intent so invariants cannot arrive half-formed.
    """
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        out = store.create_thread(con, body, actor=_actor_of(request), caller=_caller())
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=201)


def _named_action(action):
    """A route for one of helm.actions' named actions (park, backlog, ...)."""
    async def handler(request):
        thread_id = request.path_params["thread_id"]
        body, bad = await _object_body(request, optional=True)
        if bad:
            return bad
        con = _db()
        try:
            status, out = _actions.act(con, thread_id, action, body, actor=_actor_of(request),
                                       caller=_caller())
        except store.StoreError as exc:
            return _rej(exc)
        finally:
            con.close()
        return JSONResponse(out, status_code=status)
    return handler


async def threads_index(request):
    """GET /api/triage/threads/index?state=&since=&until=&who=&q=&page=&limit=: every thread the
    site has ever held, newest first, light rows, paged and filtered (the THREADS list, L3)."""
    qp = request.query_params

    def num(name):
        v = qp.get(name)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except ValueError:
            raise store.StoreError(400, "'%s' must be epoch seconds" % name)
    states = [s.strip() for s in (qp.get("state") or "").split(",") if s.strip()]
    con = _db()
    try:
        out = store.index_threads(con, states=states, since=num("since"), until=num("until"),
                                  who=qp.get("who"), q=qp.get("q"),
                                  ids=[i.strip() for i in (qp.get("ids") or "").split(",") if i.strip()],
                                  page=qp.get("page") or 1, limit=qp.get("limit") or 100)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    out["ok"] = True
    return JSONResponse(out)


async def thread_get(request):
    """GET /api/triage/threads/{id}: the thread with its events (read API, E1)."""
    con = _db()
    try:
        t = store.get_thread(con, request.path_params["thread_id"])
    finally:
        con.close()
    if t is None:
        return JSONResponse({"ok": False, "detail": "no such thread"}, status_code=404)
    return JSONResponse(t)


async def thread_conversation(request):
    """GET /api/triage/threads/{id}/conversation: the thread as one ordered timeline, `console: "none"` (helm/conversation.py). Both sites' conversations are this, through
    the web role's /api/conversations/{id}."""
    con = _db()
    try:
        t = store.get_thread(con, request.path_params["thread_id"])
    finally:
        con.close()
    if t is None:
        return JSONResponse({"ok": False, "detail": "no such thread"}, status_code=404)
    return JSONResponse(_conversation.payload(t))


async def events_route(request):
    """GET /api/triage/events?since=&until=&thread=&kind=&limit= (read API, E1)."""
    q = request.query_params

    def num(name):
        v = q.get(name)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except ValueError:
            raise store.StoreError(400, "'%s' must be epoch seconds" % name)
    con = _db()
    try:
        out = store.list_events(con, since=num("since"), until=num("until"),
                                thread=q.get("thread"), kind=q.get("kind"),
                                limit=int(q.get("limit") or 200))
    except (ValueError, store.StoreError) as exc:
        return JSONResponse({"ok": False, "detail": str(getattr(exc, "detail", exc))}, status_code=400)
    finally:
        con.close()
    return JSONResponse({"count": len(out), "events": out, "generated_at": time.time()})


async def search_route(request):
    """GET /api/triage/search?q=&limit= (read API, E1): FTS over titles, summaries and bodies."""
    q = request.query_params.get("q", "")
    try:
        limit = int(request.query_params.get("limit") or 30)
    except ValueError:
        return JSONResponse({"ok": False, "detail": "'limit' must be an integer"}, status_code=400)
    con = _db()
    try:
        hits = store.search_threads(con, q, limit=limit)
    finally:
        con.close()
    return JSONResponse({"q": q, "count": len(hits), "threads": hits})


# The store function each route calls, and its name in helm.actions.ACTIONS (the keys it reads).
_THREAD_ACTION_NAMES = {"reply": "reply", "close_thread": "close", "reopen_thread": "reopen", "mute": "mute"}


def _thread_action(fn_name, **extra):
    async def handler(request):
        thread_id = request.path_params["thread_id"]
        body, bad = await _object_body(request, optional=True)
        if bad:
            return bad
        return _thread_action_run(request, fn_name, thread_id, body, extra)
    return handler


def _thread_action_run(request, fn_name, thread_id, body, extra=None):
    """One action on one thread, from its parsed body: everything the single-thread door does once
    the body is read, answered as that door's response. POST .../{id}/{action} and bulk-settle both
    run it, so a bulk item meets the same gates and writes the same events as the same body sent
    to the single door."""
    # Body keys forwarded to the store call: what helm.actions says the action reads, minus the
    # three handled below. Any other key is dropped, as on every other door.
    forwarded = tuple(k for k in _actions.ACTIONS[_THREAD_ACTION_NAMES[fn_name]]
                      if k not in ("by", "actor", "attachments"))
    if fn_name == "reply" and "value" not in body:
        return JSONResponse({"ok": False, "detail": "value is required: the option's value, "
                             "the answer text, or 'note-only'"}, status_code=400)
    con = _db()
    try:
        fn = getattr(store, fn_name)
        kwargs = dict(extra or {})
        for k in forwarded:
            if k in body:
                kwargs[k] = body[k]
        # close_thread and reopen_thread join these into a string.
        for k in ("reason", "note"):
            if k in kwargs and fn_name in ("close_thread", "reopen_thread"):
                if kwargs[k] and not isinstance(kwargs[k], str):
                    return JSONResponse({"ok": False, "detail": "%s must be text" % k}, status_code=400)
        # Who acted: `by` (work's name) or `actor` (home's) in the body, else the
        # web role's forwarded login, else the site default (helm.store.normalize_by).
        # With HELM_BEARER_ACTOR_STRICT a bearer-only caller is recorded as itself (home).
        if fn_name in ("reply", "close_thread"):
            given, _ = _actions.door_actor(body.get("by") or body.get("actor"),
                                           _actor_of(request), _caller())
            who = store.normalize_by(given)
            kwargs.pop("actor", None)
            kwargs["by" if fn_name == "reply" else "actor"] = who
            if fn_name == "reply":
                kwargs["press"] = _web_press(request)
            if fn_name == "reply":
                # home writes "owner" as the event source unless told otherwise: a strict caller is
                # recorded as its principal, whatever name it gave for itself
                src = _actions.door_source(_actor_of(request), _caller(), who)
                if src:
                    kwargs["source"] = src
            if fn_name == "reply" and body.get("value") == store.HAND_TASK_VALUE:
                # A hand-off is never the owner's tap unless the caller said so.
                kwargs["source"] = store.handoff_source(given, _caller())
        raws = {}   # sha256 -> the bytes of a file sent with the answer (written once the rows exist)
        if isinstance(body.get("attachments"), list):
            import base64 as _b64, hashlib as _hl
            norm = []
            for a in body["attachments"]:
                if not isinstance(a, dict):
                    return JSONResponse({"detail": "; ".join(store.normalize_attachment(a)[1])}, status_code=400)
                a = dict(a)
                try:
                    raw = _b64.b64decode(a.get("content_base64") or "")
                except (ValueError, TypeError):
                    return JSONResponse({"detail": "content_base64 must be valid base64 text"}, status_code=400)
                if raw:
                    a["size"] = len(raw)
                    a["sha256"] = _hl.sha256(raw).hexdigest()
                    raws[a["sha256"]] = raw
                if fn_name == "reply" and not str(a.get("uploader") or "").strip() and kwargs.get("by") == store.BY_OWNER:
                    a["uploader"] = "owner"   # a file on the owner's own answer is his (the park gate's owner-attachment)
                clean, errs = store.normalize_attachment(a)
                if errs:
                    return JSONResponse({"detail": "; ".join(errs)}, status_code=400)
                norm.append(clean)
            kwargs["attachments"] = norm
        out = fn(con, thread_id, **kwargs)
        # The store records each file on the answer; its bytes go beside the record, as
        # POST .../attach does, or the agent gets a name it cannot open (ledger GW-067).
        if raws and _safe_segment(thread_id):
            for m in store.attachments_for(con, thread_id):
                raw = raws.get(m.get("sha256"))
                if raw is None or not _safe_segment(thread_id, m["att_id"]):
                    continue
                path = os.path.join(ATTACH_ROOT, thread_id, m["att_id"] + ".bin")
                if not os.path.exists(path):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "wb") as f:
                        f.write(raw)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


async def open_asks_route(request):
    """GET /api/triage/open-asks?limit=&cursor=: one page of the open cards that ask the owner something,
    compact (helm.store.open_asks). ?view=stale&days= pages the backlog cards idle more than `days`
    (default 30) instead (helm.store.stale_backlog). For chat sessions that clear the inbox: a page is
    a few kilobytes where the full list is every thread with its history. Bearer only."""
    qp = request.query_params
    view = (qp.get("view") or "asks").strip().lower()
    con = _db()
    try:
        if view == "asks":
            out = store.open_asks(con, limit=qp.get("limit"), cursor=qp.get("cursor"))
        elif view == "stale":
            out = store.stale_backlog(con, days=qp.get("days"), limit=qp.get("limit"), cursor=qp.get("cursor"))
        else:
            return JSONResponse({"ok": False, "detail": "'view' is asks (the default) or stale"}, status_code=400)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    out["ok"] = True
    return JSONResponse(out)


_BULK_ITEM_SHAPE = ("each item is {thread_id, value | note, reason}: value answers it (an option's value or the "
                    "answer text), note alone is a note-only answer, and reason rides in the note")


def _bulk_settle_item(request, index, item, top_by, taken):
    """One bulk-settle item through the single-thread reply door (_thread_action_run). The answer is
    `value`, or `note` alone as a note-only answer; `reason` rides in the note after it (a blank line
    between). An item with neither value nor note is refused, so a missing answer never settles,
    closes or skips a card. Always answers: {index, thread_id, action, ok, status, result} or
    {index, thread_id, action, ok: false, status, reason}. `taken` holds the threads an earlier item
    of the same request already sent to the door."""
    out = {"index": index, "thread_id": None, "action": None}

    def refuse(reason, status=400):
        out.update(ok=False, status=status, reason=reason)
        return out
    if not isinstance(item, dict):
        return refuse("each item is an object: " + _BULK_ITEM_SHAPE)
    tid = item.get("thread_id")
    if not (isinstance(tid, str) and tid.strip()):
        return refuse("thread_id is required: " + _BULK_ITEM_SHAPE)
    out["thread_id"] = tid
    for k in ("note", "reason"):
        if item.get(k) is not None and not isinstance(item.get(k), str):
            return refuse("%s must be text" % k)
    note = item.get("note") if (item.get("note") or "").strip() else ""
    reason = item.get("reason") if (item.get("reason") or "").strip() else ""
    text = "\n\n".join(x for x in (note, reason) if x)
    if "value" in item:
        door = {"value": item["value"], "note": text}
    elif note:
        door = {"value": "note-only", "note": text}
    else:
        return refuse("nothing to answer: send a value or note; " + _BULK_ITEM_SHAPE)
    out["action"] = "reply"
    if tid in taken:
        return refuse("an earlier item of this request already acted on this thread; send each thread once")
    taken.add(tid)
    for k in ("by", "actor"):
        if k in item:
            door[k] = item[k]
        elif k in top_by:
            door[k] = top_by[k]
    resp = _thread_action_run(request, "reply", tid, door)
    try:
        payload = json.loads(resp.body)
    except ValueError:
        payload = {}
    if resp.status_code < 300:
        out.update(ok=True, status=resp.status_code, result=payload)
        return out
    return refuse(str(payload.get("detail") or "refused (HTTP %d)" % resp.status_code), resp.status_code)


async def bulk_settle(request):
    """POST /api/triage/bulk-settle {items: [{thread_id, value | note, reason}], by?}: answer many
    cards in one call. Each item runs through the single-thread reply door (_bulk_settle_item), with
    the same gates and the same events as that door, one after another, each committed on its own.
    Every item is answered, ok or refused with the reason; nothing is skipped. 200 for any
    well-formed request (`ok` is true only when every item went through); 400 for a request with no
    items or too many."""
    body, bad = await _object_body(request)
    if bad:
        return bad
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return JSONResponse({"ok": False, "detail": "'items' is a non-empty list: " + _BULK_ITEM_SHAPE},
                            status_code=400)
    if len(items) > store.BULK_SETTLE_MAX:
        return JSONResponse({"ok": False, "detail": "at most %d items in one request, got %d"
                             % (store.BULK_SETTLE_MAX, len(items))}, status_code=400)
    top_by = {k: body[k] for k in ("by", "actor") if k in body}
    taken = set()
    results = [_bulk_settle_item(request, i, item, top_by, taken) for i, item in enumerate(items)]
    settled = sum(1 for r in results if r["ok"])
    return JSONResponse({"ok": settled == len(results), "count": len(results), "settled": settled,
                         "refused": len(results) - settled, "results": results})


async def snooze_route(request):
    """the owner defers a card: hidden until wake_at, then back as needs-you."""
    thread_id = request.path_params["thread_id"]
    body, bad = await _object_body(request, optional=True)
    if bad:
        return bad
    con = _db()
    try:
        out = store.snooze(
            con, thread_id,
            wake_at_raw=body.get("wake_at_raw"),
            wake_at=body.get("wake_at"),
            hours=body.get("hours"),
            note=body.get("note", ""))
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


async def result(request):
    thread_id = request.path_params["thread_id"]
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        out = store.post_result(con, thread_id, body)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


async def result_validate(request):
    """Would this candidate result be accepted? The /result door's own checks, run and rolled back.

    200 {"ok": true, "detail": "", "would": <what the door would answer>} or
    200 {"ok": false, "detail": <the door's refusal text>, "status": <its status>,
         "design_refusal": <true when a rule refused it, false for a malformed field>}.
    Writes nothing: no event, no row change, no rule-warning record. It is a different route, so it
    leaves the real /result route's counters alone. An unknown thread is the canonical 404.
    """
    thread_id = request.path_params["thread_id"]
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        try:
            would = store.post_result(con, thread_id, body, dry_run=True)
        except store.StoreError as exc:
            if exc.status == 404:
                return _rej(exc)
            out = {"ok": False, "detail": exc.detail, "status": exc.status,
                   "design_refusal": bool((exc.extra or {}).get("design_refusal"))}
            out.update({k: v for k, v in (exc.extra or {}).items() if k not in out})
            return JSONResponse(out)
        finally:
            con.rollback()
    finally:
        con.close()
    return JSONResponse({"ok": True, "detail": "", "would": would})


async def append_note(request):
    """Safe note/result append (slice A, 2026-09-15): history-only write.

    Never flips state, never rewrites history; see store.append_note for
    the exact may-touch / never-touch contract.
    """
    thread_id = request.path_params["thread_id"]
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        actor = body.get("actor") or body.get("author") or store.BY_OWNER
        note = body.get("note", "")
        if note is None or (isinstance(note, str) and not note.strip()):
            # Agents composing their own post reach for `text` or `body`; `note` still wins.
            for alias in ("text", "body"):
                cand = body.get(alias)
                if cand is not None and not (isinstance(cand, str) and not cand.strip()):
                    note = cand
                    break
        out = store.append_note(
            con, thread_id,
            note=note,
            actor=actor,
            dedupe_key=body.get("dedupe_key", body.get("idempotency_key", "")))
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


# ------------------------------------------------------------- attachments

ATTACH_ROOT = os.environ.get("HELM_ATTACH_DIR", "data/attachments")

from helm import preview as _preview   # noqa: E402  (the rules the web role shares)

VIEW_CSP = _preview.VIEW_CSP


def _agent_gate(con, thread_id, clean, data, extra_count=0, extra_bytes=0):
    """Why an agent's upload is refused, or None. the owner's own files, and bytes the thread already
    holds (a retry), are never held to the agent limits (helm/preview.py)."""
    if not _preview.is_agent(clean.get("uploader")):
        return None
    store._require_thread(con, thread_id)   # a missing thread is the 404 it always was, not a 400 about the file
    if _held(con, thread_id, clean):
        return None
    n, size = store.agent_attachment_totals(con, thread_id)
    errs = _preview.check_agent_upload(clean, data, n + extra_count, size + extra_bytes)
    return "; ".join(errs) if errs else None


def _held(con, thread_id, clean):
    """True when the thread already holds these bytes (a retry): nothing to check, nothing new to store."""
    return con.execute("SELECT 1 FROM attachments WHERE thread_id=? AND sha256=?", (thread_id, clean["sha256"])).fetchone() is not None


async def _agent_scan(con, thread_id, clean, data):
    """The credential tripwire for an agent's text file: about a second on 8 MB, so off the event loop.
    Skipped for the owner's own files and for bytes the thread already holds."""
    if not _preview.is_agent(clean.get("uploader")) or _held(con, thread_id, clean):
        return None
    from starlette.concurrency import run_in_threadpool
    errs = await run_in_threadpool(_preview.scan_agent_upload, clean, data)
    return "; ".join(errs) if errs else None


def _safe_segment(*segments) -> bool:
    """Every id that becomes a path segment must be one, at the join site too.

    store.IDENT_RE is enforced when a thread or attachment is created, but these
    ids arrive here from the URL, and Starlette's default converter is [^/]+,
    which matches '..'. attachment_get stats the joined path BEFORE it asks the
    store anything, so store-side validation alone leaves a file-existence
    oracle -- and a database written before that validation existed can still
    hold an id no producer could send today.
    """
    return all(store.ident_ok(seg or "") for seg in segments)


async def attach(request):
    thread_id = request.path_params["thread_id"]
    ctype = request.headers.get("Content-Type", "")
    if ctype.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception:  # a malformed multipart body is the caller's, whatever the parser raises
            return JSONResponse({"ok": False, "detail": "malformed multipart body"}, status_code=400)
        upload = form.get("file")
        if not hasattr(upload, "read"):  # absent, or sent as a plain text field
            return JSONResponse({"detail": "multipart 'file' or JSON 'content_base64' required"}, status_code=400)
        data = await upload.read()
        meta_in = {"filename": upload.filename, "mime": upload.content_type,
                   "uploader": str(form.get("uploader") or "").strip(),
                   "caption": str(form.get("caption") or "").strip()}
    else:
        body, bad = await _object_body(request)
        if bad:
            return bad
        if isinstance(body.get("files"), list):
            # The mobile app's share-sheet shape: several files in
            # one post, one caption, answered with every record. Each file goes through
            # the same door as a single one.
            return await _attach_many(thread_id, body)
        data = None
        if body.get("content_base64"):
            import base64
            try:
                data = base64.b64decode(body["content_base64"])
            except Exception:  # not base64, or not even text
                return JSONResponse({"ok": False, "detail": "content_base64 is not base64"}, status_code=400)
        meta_in = {"filename": body.get("filename"), "mime": body.get("mime"),
                   "uploader": str(body.get("uploader") or "").strip(),
                   "caption": str(body.get("caption") or "").strip()}
    if data is None:
        return JSONResponse({"detail": "multipart 'file' or JSON 'content_base64' required"}, status_code=400)
    import hashlib
    if not _safe_segment(thread_id):
        # Checked BEFORE the row is written: a refused id must not leave a
        # metadata row whose bytes were never stored (reviewer finding).
        return JSONResponse({"ok": False, "detail": "no such thread"}, status_code=404)
    con = _db()
    try:
        clean, errs = store.normalize_attachment({**meta_in, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()})
        if errs:
            return JSONResponse({"detail": "; ".join(errs)}, status_code=400)
        bad = _agent_gate(con, thread_id, clean, data) or await _agent_scan(con, thread_id, clean, data)
        if bad:
            return JSONResponse({"detail": bad}, status_code=400)
        rec = store.add_attachment(con, thread_id, clean)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    if not _safe_segment(thread_id, rec["att_id"]):
        return JSONResponse({"detail": "no such thread"}, status_code=404)
    os.makedirs(os.path.join(ATTACH_ROOT, thread_id), exist_ok=True)
    path = os.path.join(ATTACH_ROOT, thread_id, rec["att_id"] + ".bin")
    with open(path, "wb") as f:
        f.write(data)
    return JSONResponse(rec, status_code=201)


async def _attach_many(thread_id, body):
    import base64
    import hashlib
    caption = str(body.get("caption") or "").strip()
    uploader = str(body.get("uploader") or "owner").strip()
    recs = []
    if not _safe_segment(thread_id):
        return JSONResponse({"ok": False, "detail": "no such thread"}, status_code=404)
    con = _db()
    try:
        if _preview.is_agent(uploader):
            # An agent's batch is judged whole before anything is stored: the files together must
            # fit the thread's room, and one refused file leaves none of them behind.
            held_n = held_b = 0
            for i, f in enumerate(body["files"]):
                if not isinstance(f, dict) or not f.get("content_base64"):
                    break   # the main loop gives this the same 400 it always has
                try:
                    data = base64.b64decode(f["content_base64"])
                except Exception:
                    break
                clean, errs = store.normalize_attachment({
                    "filename": f.get("filename"), "mime": f.get("mime"), "uploader": uploader,
                    "caption": str(f.get("caption") or caption), "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()})
                if errs:
                    break
                try:
                    bad = _agent_gate(con, thread_id, clean, data, held_n, held_b) or await _agent_scan(con, thread_id, clean, data)
                except store.StoreError as exc:
                    return _rej(exc)
                if bad:
                    return JSONResponse({"ok": False, "detail": "files[%d]: %s" % (i, bad)}, status_code=400)
                held_n += 1
                held_b += len(data)
        for i, f in enumerate(body["files"]):
            if not isinstance(f, dict) or not f.get("content_base64"):
                return JSONResponse({"ok": False, "detail": "files[%d] needs filename, mime and content_base64" % i},
                                    status_code=400)
            try:
                data = base64.b64decode(f["content_base64"])
            except Exception:
                return JSONResponse({"ok": False, "detail": "files[%d].content_base64 is not base64" % i}, status_code=400)
            clean, errs = store.normalize_attachment({
                "filename": f.get("filename"), "mime": f.get("mime"), "uploader": uploader,
                "caption": str(f.get("caption") or caption), "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()})
            if errs:
                return JSONResponse({"ok": False, "detail": "files[%d]: %s" % (i, "; ".join(errs))}, status_code=400)
            try:
                rec = store.add_attachment(con, thread_id, clean)
            except store.StoreError as exc:
                return _rej(exc)
            if not _safe_segment(thread_id, rec["att_id"]):
                return JSONResponse({"ok": False, "detail": "no such thread"}, status_code=404)
            os.makedirs(os.path.join(ATTACH_ROOT, thread_id), exist_ok=True)
            with open(os.path.join(ATTACH_ROOT, thread_id, rec["att_id"] + ".bin"), "wb") as fh:
                fh.write(data)
            recs.append(rec)
    finally:
        con.close()
    return JSONResponse({"accepted": True, "count": len(recs), "attachments": recs}, status_code=201)


async def attachment_get(request):
    thread_id = request.path_params["thread_id"]
    att_id = request.path_params["att_id"]
    if not _safe_segment(thread_id, att_id):
        return JSONResponse({"detail": "no such attachment"}, status_code=404)
    path = os.path.join(ATTACH_ROOT, thread_id, f"{att_id}.bin")
    if not os.path.isfile(path):
        return JSONResponse({"detail": "no such attachment"}, status_code=404)
    con = _db()
    try:
        metas = store.attachments_for(con, thread_id)
    finally:
        con.close()
    meta = next((m for m in metas if m["att_id"] == att_id), None)
    if meta is None:
        return JSONResponse({"detail": "no such attachment"}, status_code=404)
    # Sandboxed and never sniffed, whoever uploaded it; a type a browser would run as a
    # document (HTML, SVG, XML, script) downloads instead of rendering (ledger GW-075; the
    # notebook's /att rule, H5).
    from app.notebook_api import ATT_HEADERS, ACTIVE_TYPES, active_content
    mime = (meta.get("mime") or "application/octet-stream").lower()
    name = str(meta.get("filename") or att_id)
    headers = dict(ATT_HEADERS)
    base = mime.split(";")[0].strip()
    # An SVG stays an image: the card visuals (helm-card-visual.py) are SVG diagrams the screens
    # draw with <img>, where no script runs, and opened on its own the sandbox holds it.
    svg = base == "image/svg+xml"   # by the stored type: a name ending .svg over HTML is HTML
    if not svg and (active_content(name) or base in ACTIVE_TYPES or base.endswith("+xml")):
        mime = "application/octet-stream"
        headers["Content-Disposition"] = 'attachment; filename="%s"' % re.sub(r'[^A-Za-z0-9._ -]', "_", name)[:120]
    return Response(open(path, "rb").read(), media_type=mime, headers=headers)


async def attachment_view(request):
    """An agent's HTML. Only HTML (415 for anything else). `?raw=1` is the page itself, for the card's
    frame, under VIEW_CSP: an opaque origin and no network. Without it (a tab of its own) the answer is
    the host page that frames the same URL. The plain attachment route keeps downloading HTML; this
    is the one place it renders."""
    thread_id = request.path_params["thread_id"]
    att_id = request.path_params["att_id"]
    if not _safe_segment(thread_id, att_id):
        return JSONResponse({"detail": "no such attachment"}, status_code=404)
    path = os.path.join(ATTACH_ROOT, thread_id, f"{att_id}.bin")
    if not os.path.isfile(path):
        return JSONResponse({"detail": "no such attachment"}, status_code=404)
    con = _db()
    try:
        metas = store.attachments_for(con, thread_id)
    finally:
        con.close()
    meta = next((m for m in metas if m["att_id"] == att_id), None)
    if meta is None:
        return JSONResponse({"detail": "no such attachment"}, status_code=404)
    if _preview.preview_kind(meta.get("mime"), meta.get("filename")) != "html":
        return JSONResponse({"detail": "only an HTML file is shown as a page; this one is %s"
                                       % ((meta.get("mime") or "untyped").split(";")[0])},
                            status_code=415, headers=dict(_preview.WRAPPER_HEADERS))
    if not _preview.serves_raw(request.query_params.get("raw"), request.headers.get("sec-fetch-dest")):
        # A tab of its own gets the host page, never the page itself (helm/preview.py).
        return Response(_preview.wrapper_page(meta.get("filename") or att_id), media_type="text/html; charset=utf-8",
                        headers=dict(_preview.WRAPPER_HEADERS))
    with open(path, "rb") as fh:
        data = fh.read()
    return Response(data, media_type="text/html; charset=utf-8", headers=dict(_preview.VIEW_HEADERS))


async def attachments_list(request):
    thread_id = request.path_params["thread_id"]
    con = _db()
    try:
        metas = store.attachments_for(con, thread_id)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse({"attachments": metas})


# ------------------------------------------------------- dispatch

# The dispatcher (host-a) publishes its heartbeat here so the cross-host
# watchdog (host-b) can read its age WITHOUT sharing a failure domain with
# the dispatcher host (contract P3 §6, PLAN rule 10). File-based, one blob:
# this is dispatcher plumbing, not store data, so it never touches the store.
DISPATCHER_HB_PATH = os.environ.get(
    "HELM_DISPATCHER_HB_FILE", "data/dispatcher/heartbeat-published.json")


async def dispatcher_heartbeat_get(request):
    if not os.path.isfile(DISPATCHER_HB_PATH):
        return JSONResponse({"detail": "no dispatcher heartbeat published yet"},
                            status_code=404)
    import json as _json
    try:
        with open(DISPATCHER_HB_PATH, encoding="utf-8") as fh:
            blob = _json.load(fh)
    except (OSError, ValueError):
        return JSONResponse({"detail": "heartbeat unreadable"}, status_code=500)
    return JSONResponse(blob)


def _log_tail_path():
    # Beside the heartbeat: the latest log_tail the poller sent, kept across the heartbeats
    # that carry none.
    return os.path.join(os.path.dirname(os.path.abspath(DISPATCHER_HB_PATH)),
                        "dispatcher-log-tail.json")


def _write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _dispatcher_log():
    try:
        with open(_log_tail_path(), encoding="utf-8") as fh:
            lines = json.load(fh)
    except (OSError, ValueError):
        return []
    return [str(x) for x in lines] if isinstance(lines, list) else []


async def dispatcher_heartbeat_put(request):
    body, bad = await _object_body(request)
    if bad:
        return bad
    if not isinstance(body, dict) or not body.get("ts"):
        return JSONResponse({"detail": "heartbeat body must carry a numeric 'ts'"},
                            status_code=400)
    # Work's the poller may add its runs and log tail (contract: ACTIVITY-RUN-DATA.md). Both
    # are checked before anything is written, and neither stays in the heartbeat blob:
    # the runs go to the store, the log tail beside the heartbeat.
    body = dict(body)
    try:
        runs = store.normalize_runs(body.pop("runs", None))
        log_tail = store.normalize_log_tail(body.pop("log_tail", None))
    except store.StoreError as exc:
        return _rej(exc)
    if runs:
        con = _db()
        try:
            store.record_runs(con, runs)
        finally:
            con.close()
    if log_tail is not None:
        _write_json(_log_tail_path(), log_tail)
    _write_json(DISPATCHER_HB_PATH, body)
    return JSONResponse({"ok": True}, status_code=202)


async def pending_dispatch(request):
    con = _db()
    try:
        if store.rules().recheck_on_wake:
            # The dispatcher polls this every minute and nobody has to have the app open: a snooze
            # that has come due is rechecked here, not only when a screen reads the list.
            try:
                store.wake_due(con)
            except Exception as e:  # noqa: BLE001  the queue read must not depend on the wake sweep
                con.rollback()
                print("pending-dispatch: wake_due failed: %s: %s" % (type(e).__name__, e), flush=True)
        out = store.pending_dispatch(con)
        # Work Helm's envelope also carried the runs that never reported (api.py:270);
        # the poller reads stale[].thread_id / dispatched_at. Additive for the home pump.
        out["stale"] = store.stale_dispatches(con)
    finally:
        con.close()
    return JSONResponse(out)


async def pending_working(request):
    con = _db()
    try:
        out = store.stale_dispatches(con)
    finally:
        con.close()
    return JSONResponse({"pending": out, "generated_at": time.time()})


async def dispatched(request):
    thread_id = request.path_params["thread_id"]
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        out = store.mark_dispatched(
            con, thread_id, reply_ts=body.get("reply_ts"),
            ok=bool(body.get("ok", True)), detail=body.get("detail", ""),
            mode=body.get("mode", "reply"))
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


async def needs_owner_check(request):
    """The four-test as a route (contract: P3b §1.4): every producer calls
    this BEFORE filing. It never writes a thread; 200 either way; the answer is
    data. The one write is telemetry: the card-shape rules' record (rule ids)."""
    body, bad = await _object_body(request)
    if bad:
        return bad
    t = body.get("thread")
    if not isinstance(t, dict):
        return JSONResponse({"detail": "'thread' object is required"}, status_code=400)
    con = _db()
    try:
        clean, errs = store.validate_thread(t)
        # The card-shape rules (helm/card_rules.py): rules in log mode come back as `warnings` and
        # are recorded (once per question) without changing the answer; rules in reject mode are
        # already among `errs`. The record is telemetry, never the thread: nothing here lands a card.
        verdict = store.card_rule_check_raw(t)
        warnings = list(verdict["warn"]) if verdict else []
        if verdict:
            try:
                store.record_card_rules(con, "check", t.get("thread_id"), verdict,
                                        author_fallback=getattr(request.state, "body_actor", ""))
                con.commit()
            except Exception:
                pass  # a record never costs the caller its answer
        if errs:
            # the card does not pass the card-content rules: a refusal is an answer, not a bad request
            return JSONResponse({"needs_you": False, "failed_test": "validation",
                                 "detail": "; ".join(errs[:3]), "warnings": warnings})
        existing = con.execute(
            "SELECT * FROM threads WHERE thread_id=?", (clean["thread_id"],)).fetchone()
        nj_errors = store.needs_owner_gate_errors(con, clean, existing)
        needs_you, failed = not nj_errors, None
        if nj_errors:
            needs_you = False
            # "needs-you refused: T1-only-he-can: <fix>" -> the test name
            parts = nj_errors[0].split(":", 2)
            failed = parts[1].strip() if len(parts) > 2 else None
        return JSONResponse({
            "needs_you": needs_you,
            "failed_test": failed,
            "detail": nj_errors[0] if nj_errors else "all four tests pass",
            "warnings": warnings,
        })
    finally:
        con.close()


async def card_rules_stats(request):
    """How often each card-shape rule fired, per rule, per principal and per author, for a window.
    ?since= and ?until= are epoch seconds (default: the last 7 days; at most CARD_RULES_KEEP_S back);
    ?door= is put,result (default: the asks producers filed), check (pre-flight calls), or all.
    Bearer only; the web role does not proxy it. Counts and rule ids, never card text."""
    q = request.query_params
    now = time.time()
    try:
        since = float(q.get("since") or (now - 7 * 86400))
        until = float(q.get("until") or now)
        if not (math.isfinite(since) and math.isfinite(until)):
            raise ValueError("not finite")
    except ValueError:
        return JSONResponse({"ok": False, "detail": "since and until are epoch seconds"}, status_code=400)
    since = max(since, now - store.CARD_RULES_KEEP_S)
    raw_doors = (q.get("door") or "put,result").strip().lower()
    doors = (list(store.CARD_RULES_DOORS) if raw_doors == "all"
             else [d.strip() for d in raw_doors.split(",") if d.strip()])
    bad = [d for d in doors if d not in store.CARD_RULES_DOORS]
    if bad or not doors:
        return JSONResponse({"ok": False, "detail": "door is a comma list of %s, or all"
                             % ", ".join(store.CARD_RULES_DOORS)}, status_code=400)
    con = _db()
    try:
        out = store.card_rule_stats(con, since, until, doors)
    finally:
        con.close()
    out.update(ok=True, modes=_card_rules.modes(), pins=dict(_card_rules.PINS))
    return JSONResponse(out)


async def categories_merge_case(request):
    """POST /api/triage/categories/merge-case: merge the categories that differ only by case or
    whitespace into the canonical spelling of each (helm.store.merge_category_case). `{}` or
    `{"apply": false}` is a dry run that lists the groups and counts; `{"apply": true}` writes the
    record (old->new and the thread ids) to category-merges/ beside the database first, then rewrites.
    Bearer only; the web role does not proxy it."""
    body, bad = await _object_body(request, optional=True)
    if bad:
        return bad
    apply = body.get("apply", False)
    if not isinstance(apply, bool):
        return JSONResponse({"ok": False, "detail": "'apply' is true or false"}, status_code=400)
    con = _db()
    try:
        out = store.merge_category_case(
            con, apply=apply,
            record_dir=os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), store.CATEGORY_MERGE_DIRNAME))
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    out["ok"] = True
    return JSONResponse(out)


async def rechecks_due(request):
    """Parks whose recheck date has passed, handed out as data (contract P1 §6: the store never
    runs commands). a sweeper evaluates each and posts the verdict back."""
    con = _db()
    try:
        return JSONResponse({"due": store.due_rechecks(con)})
    finally:
        con.close()


async def recheck_route(request):
    thread_id = request.path_params["thread_id"]
    body, bad = await _object_body(request, optional=True)
    if bad:
        return bad
    passed = body.get("passed")
    if passed not in (True, False, None):
        return JSONResponse({"detail": "'passed' must be true, false or null"}, status_code=400)
    con = _db()
    try:
        out = store.apply_recheck(con, thread_id, passed, str(body.get("detail") or "")[:500])
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


async def gate_log_route(request):
    """What the needs-owner gate kept from him (held / settled / released / missed), for the
    held-by-gate view. ?days=14 by default."""
    try:
        days = max(1, min(90, int(request.query_params.get("days", "14"))))
    except ValueError:
        return JSONResponse({"detail": "'days' must be an integer"}, status_code=400)
    con = _db()
    try:
        return JSONResponse({"days": days, "entries": store.gate_log(con, days)})
    finally:
        con.close()


async def retry_route(request):
    """POST /api/triage/threads/{id}/retry: the owner's Retry on a stalled or capped run (L7)."""
    thread_id = request.path_params["thread_id"]
    body, bad = await _object_body(request, optional=True)
    if bad:
        return bad
    con = _db()
    try:
        out = store.retry_dispatch(con, thread_id, by=_actor_of(request) or store.BY_OWNER, note=body.get("note", ""))
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out)


async def gave_up_restore(request):
    body, bad = await _object_body(request, optional=True)
    if bad:
        return bad
    con = _db()
    try:
        out = store.restore_gave_up(con)
    finally:
        con.close()
    if store.rules().restore_wraps:
        # Work Helm's shape: {restored: [...]}; home clients read the bare list.
        return JSONResponse({"restored": out}, status_code=202)
    return JSONResponse(out, status_code=202)


# ------------------------------------------------------------- lessons (P3c)

async def lessons(request):
    """The ledger read + the render view an executor prompt uses (helm.lessons).

    GET /api/triage/lessons                      -> list (all, filters)
    GET /api/triage/lessons?render_for=<scope>   -> the prompt block's rows
    GET /api/triage/lessons?pending_gates=1      -> the honest gap (P3c §5)
    """
    import urllib.parse
    q = urllib.parse.parse_qs(request.url.query)
    con = _db()
    try:
        if q.get("pending_gates"):
            return JSONResponse({"pending_gates": store.lessons_pending_gates(con)})
        if q.get("render_for"):
            rows = store.lessons_for_prompt(con, q["render_for"][0])
            return JSONResponse({"count": len(rows), "lessons": rows})
        rows = store.lessons_list(
            con, scope=(q.get("scope") or [None])[0],
            status=(q.get("status") or [None])[0])
        return JSONResponse({"count": len(rows), "lessons": rows})
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()


async def lesson_get_route(request):
    con = _db()
    try:
        return JSONResponse(store.lesson_get(con, request.path_params["lesson_id"]))
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()


async def lesson_open_route(request):
    """The lesson door (contract: P3c §2): classify elsewhere (deterministic,
    learning/classify.py), file here. Producers POST the candidate + evidence."""
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        out = store.open_lesson_thread(
            con,
            source_thread_id=body.get("source_thread_id") or "",
            event_ids=body.get("event_ids") or [],
            quote=body.get("quote") or "",
            rule_candidate=body.get("rule_candidate") or "",
            scope=body.get("scope") or "global",
            signals=body.get("signals") or [],
        )
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=201)


async def lesson_from_thread_route(request):
    """The adoption application (contract: P3c §3/§4): whoever consumes the
    dispatch queue calls this after his tap (helm.lessons.apply_lesson_tap);
    the store refuses it without one. Never auto-adopts."""
    body, bad = await _object_body(request, optional=True)
    if bad:
        return bad
    con = _db()
    try:
        out = store.create_lesson_from_thread(
            con, request.path_params["thread_id"], actor="owner")
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


async def lesson_retire_route(request):
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        out = store.retire_lesson(
            con, request.path_params["lesson_id"],
            reason=body.get("reason") or "",
            actor=body.get("actor") or "owner")
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


async def lesson_supersede_route(request):
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        out = store.supersede_lesson(
            con, request.path_params["lesson_id"],
            rule_text=body.get("rule_text") or "",
            scope=body.get("scope"),
            actor=body.get("actor") or "owner")
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


async def lesson_gate_implemented_route(request):
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        out = store.mark_gate_implemented(
            con, request.path_params["lesson_id"],
            commit_ref=body.get("commit") or "",
            actor=body.get("actor") or "orchestrator")
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(out, status_code=202)


# ------------------------------------------------------- claims (the NEW surface)

CLAIM_LEASE_S = int(os.environ.get("HELM_CLAIM_LEASE_S", "1800"))


async def claim_create(request):
    """An executor claims a thread. Atomic; no spawn without a claim.

    Body: {executor_id, runtime, caps{timeout_s,max_budget_usd,max_turns,model}}
    Returns 201 {claim_id, lease_deadline} or 409 while a live claim exists.
    """
    thread_id = request.path_params["thread_id"]
    body, bad = await _object_body(request)
    if bad:
        return bad
    executor_id = str(body.get("executor_id") or "").strip()
    runtime = str(body.get("runtime") or "").strip()
    caps = body.get("caps") if isinstance(body.get("caps"), dict) else {}
    if len(executor_id) < 3:
        return JSONResponse({"detail": "executor_id (>=3 chars) is required: who is claiming"}, status_code=400)
    if not runtime:
        return JSONResponse({"detail": "runtime is required (e.g. 'agent-a'): the store records it as data"}, status_code=400)
    con = _db()
    try:
        now = time.time()
        # core-diff item 11d: a claim on a thread the store does not hold is refused. A
        # tombstoned thread is still held: the pump claims it to report the settlement of a task
        # that finished after the card was retired, and post_result accepts it the same way.
        if con.execute("SELECT 1 FROM threads WHERE thread_id=?",
                       (thread_id,)).fetchone() is None:
            return JSONResponse({"ok": False, "detail": "no such thread: " + thread_id}, status_code=404)
        row = con.execute(
            "SELECT claim_id, deadline, released_at, result_at FROM claims "
            "WHERE thread_id=? AND released_at IS NULL AND result_at IS NULL",
            (thread_id,)).fetchone()
        if row and row["deadline"] > now:
            return JSONResponse({
                "detail": f"thread already claimed by a live claim until the lease expires",
                "claim_id": row["claim_id"], "deadline": row["deadline"],
            }, status_code=409)
        if row:
            con.execute("UPDATE claims SET released_at=? WHERE claim_id=?",
                        (now, row["claim_id"]))
        claim_id = f"c-{int(now*1000):013d}-{store.slug(executor_id, 16)}"
        con.execute(
            "INSERT INTO claims (claim_id, thread_id, executor_id, runtime, caps, "
            "claimed_at, deadline) VALUES (?,?,?,?,?,?,?)",
            (claim_id, thread_id, executor_id, runtime,
             json.dumps({"timeout_s": caps.get("timeout_s"),
                         "max_budget_usd": caps.get("max_budget_usd"),
                         "max_turns": caps.get("max_turns"),
                         "model": caps.get("model")}),
             now, now + CLAIM_LEASE_S))
        con.commit()
    finally:
        con.close()
    return JSONResponse({"claim_id": claim_id, "lease_deadline": now + CLAIM_LEASE_S,
                         "executor_id": executor_id, "runtime": runtime}, status_code=201)


async def claim_result(request):
    """Executor reports on its claim. Goes THROUGH post_result so every gate applies."""
    thread_id = request.path_params["thread_id"]
    claim_id = request.path_params["claim_id"]
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        row = con.execute(
            "SELECT deadline, released_at, result_at, executor_id, runtime, caps "
            "FROM claims WHERE claim_id=? AND thread_id=?", (claim_id, thread_id)).fetchone()
        if row is None:
            return JSONResponse({"detail": "no such claim for this thread"}, status_code=404)
        if row["result_at"] is not None:
            return JSONResponse({"detail": "claim already reported; a re-report needs a new claim"}, status_code=409)
        now = time.time()
        expired = row["deadline"] < now
        # The accounting fields ride INTO the result event as data (issue #2).
        accounting = {
            "executor_id": row["executor_id"], "runtime": row["runtime"],
            "model": body.get("model"), "models_billed": body.get("models_billed"),
            "cost_usd": body.get("cost_usd"), "cost_basis": body.get("cost_basis", "unknown"),
            "duration_s": body.get("duration_s"), "num_turns": body.get("num_turns"),
            "capped": bool(body.get("capped", False)),
            "timed_out": bool(body.get("timed_out", False)),
            "permission_denials": body.get("permission_denials"),
            "claim_expired": expired,
        }
        out = store.post_result(con, thread_id, dict(body, executor=accounting))
        con.execute("UPDATE claims SET result_at=?, reported=? WHERE claim_id=?",
                    (now, json.dumps(accounting), claim_id))
        con.commit()
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse({"ok": True, "claim_id": claim_id, "expired": expired,
                         "result": out}, status_code=202)


async def claim_release(request):
    """Release a claim without a result (run failed / gave up / crash cleanup).

    Idempotent; a released claim can never 409 a fresh claim, so a failed run
    retried on a later tick claims anew instead of colliding with its own
    dead claim until the lease expires.
    """
    thread_id = request.path_params["thread_id"]
    claim_id = request.path_params["claim_id"]
    body, bad = await _object_body(request, optional=True)
    if bad:
        return bad
    con = _db()
    try:
        row = con.execute(
            "SELECT released_at, result_at FROM claims WHERE claim_id=? AND thread_id=?",
            (claim_id, thread_id)).fetchone()
        if row is None:
            return JSONResponse({"detail": "no such claim for this thread"}, status_code=404)
        if row["result_at"] is not None:
            return JSONResponse({"detail": "claim already reported; results are final"},
                                status_code=409)
        if row["released_at"] is None:
            con.execute("UPDATE claims SET released_at=? WHERE claim_id=?",
                        (time.time(), claim_id))
            con.commit()
        return JSONResponse({"ok": True, "claim_id": claim_id,
                             "note": str(body.get("note") or "")[:200]}, status_code=202)
    finally:
        con.close()


def _claims_view(con, state, thread_id=None):
    """Claims newest first, annotated live/expired, filtered by `state` (live | expired | any)."""
    now = time.time()
    where, args = ("WHERE thread_id=? ", (thread_id,)) if thread_id else ("", ())
    rows = con.execute(
        "SELECT claim_id, thread_id, executor_id, runtime, claimed_at, deadline, "
        "released_at, result_at FROM claims " + where + "ORDER BY claimed_at DESC LIMIT 200",
        args).fetchall()
    out = []
    for r in rows:
        live = r["released_at"] is None and r["result_at"] is None and r["deadline"] > now
        item = dict(r)
        item["live"] = live
        item["expired"] = r["released_at"] is None and r["result_at"] is None and r["deadline"] <= now
        if state == "live" and not live:
            continue
        if state == "expired" and not item["expired"]:
            continue
        out.append(item)
    return out, now


async def claims_list(request):
    state = request.query_params.get("state", "live")
    con = _db()
    try:
        out, now = _claims_view(con, state)
    finally:
        con.close()
    return JSONResponse({"claims": out, "generated_at": now})


async def thread_claims(request):
    """GET one thread's claims (same shape and `state` as /api/triage/claims, default live).
    An id the store never held is a 404, as it is for a claim on it; a held thread with no
    claims is an empty list."""
    thread_id = request.path_params["thread_id"]
    state = request.query_params.get("state", "live")
    con = _db()
    try:
        if con.execute("SELECT 1 FROM threads WHERE thread_id=?",
                       (thread_id,)).fetchone() is None:
            return JSONResponse({"ok": False, "detail": "no such thread: " + thread_id}, status_code=404)
        out, now = _claims_view(con, state, thread_id)
    finally:
        con.close()
    return JSONResponse({"claims": out, "thread_id": thread_id, "generated_at": now})


# ------------------------------------------------------------------ misc


async def suppressions(request):
    con = _db()
    try:
        out = store.list_suppressions(con)
    finally:
        con.close()
    return JSONResponse({"suppressions": out, "count": len(out)})


async def resort(request):
    body, bad = await _object_body(request, optional=True)
    if bad:
        return bad
    name = str(body.get("category") or "").strip()
    con = _db()
    try:
        if name:
            # One spelling per category: a case-only variant touches the stored spelling.
            name = store.canonical_category(con, name)
            store._touch_category(con, name, time.time())
        else:
            # mobile pull-to-refresh and the web's ↻/R send {}: a refresh resorts every category.
            touched = store._touch_all_categories(con, time.time())
        con.commit()
    finally:
        con.close()
    if not name:
        return JSONResponse({"ok": True, "category": None, "categories": touched}, status_code=202)
    return JSONResponse({"ok": True, "category": name}, status_code=202)


# ------------------------------------------------------------------ settings & labels (P6)


async def get_settings_route(request):
    con = _db()
    try:
        st = store.get_settings(con)
        lbls = store.list_labels(con, include_disabled=True)
    finally:
        con.close()
    return JSONResponse({"settings": st, "labels": lbls})


async def post_settings_route(request):
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        st = store.put_settings(con, body)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse({"ok": True, "settings": st}, status_code=200)


async def list_labels_route(request):
    include_disabled = request.query_params.get("include_disabled") in ("1", "true", "yes")
    con = _db()
    try:
        lbls = store.list_labels(con, include_disabled=include_disabled)
    finally:
        con.close()
    return JSONResponse({"labels": lbls})


async def put_label_route(request):
    body, bad = await _object_body(request)
    if bad:
        return bad
    con = _db()
    try:
        lbl = store.put_label(con, body)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse({"ok": True, "label": lbl}, status_code=200)


async def delete_label_route(request):
    name = request.path_params["name"]
    con = _db()
    try:
        res = store.delete_label(con, name)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(res, status_code=200)


async def thread_label_route(request):
    thread_id = request.path_params["thread_id"]
    body, bad = await _object_body(request)
    if bad:
        return bad
    label = body.get("label", "")
    con = _db()
    try:
        res = store.set_thread_label(con, thread_id, label)
    except store.StoreError as exc:
        return _rej(exc)
    finally:
        con.close()
    return JSONResponse(res, status_code=202)


async def settings_ui_page(request):
    con = _db()
    try:
        st = store.get_settings(con)
        lbls = store.list_labels(con, include_disabled=True)
    finally:
        con.close()

    labels_rows = ""
    for l in lbls:
        status_tag = f'<span class="tag {"enabled" if l["enabled"] else "disabled"}">{"enabled" if l["enabled"] else "disabled"}</span>'
        needs_human_tag = '<span class="tag enabled">yes</span>' if l["convert_to_needs_human"] else '<span class="tag">no</span>'
        labels_rows += f"""<tr>
          <td><strong>{l["name"]}</strong></td>
          <td>{l["description"]}</td>
          <td><code>{l["executor_runtime"]}</code></td>
          <td>{needs_human_tag}</td>
          <td>{status_tag}</td>
          <td><pre style="max-height: 80px; overflow-y: auto; margin:0; font-size: 11px;">{l["prompt_template"]}</pre></td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Helm Intake & Label Settings</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; max-width: 960px; margin: 30px auto; padding: 0 20px; line-height: 1.5; color: #1f2937; background: #f9fafb; }}
    h1, h2 {{ color: #111827; }}
    .intro-banner {{ background: #eff6ff; border-left: 4px solid #2563eb; padding: 16px 20px; margin-bottom: 24px; border-radius: 4px; }}
    .intro-banner p {{ margin: 6px 0; font-size: 15px; color: #1e3a8a; }}
    .card {{ background: #ffffff; border: 1px solid #e5e7eb; border-radius: 6px; padding: 20px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }}
    th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
    th {{ background: #f3f4f6; font-weight: 600; }}
    .tag {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; background: #e5e7eb; color: #374151; }}
    .tag.enabled {{ background: #dcfce7; color: #166534; }}
    .tag.disabled {{ background: #fee2e2; color: #991b1b; }}
    code {{ background: #f3f4f6; padding: 2px 4px; border-radius: 3px; font-family: monospace; }}
  </style>
</head>
<body>
  <h1>Helm Intake & Label Settings</h1>
  
  <div class="intro-banner">
    <p><strong>Intake cards enter via the API or app. Each card is tagged with a label.</strong></p>
    <p><strong>A label defines what instructions the autonomous worker receives and which model runtime executes it.</strong></p>
    <p><strong>Cards labeled 'needs-human' are converted into structured decision cards for the owner with multiple-choice options and no context assumed.</strong></p>
  </div>

  <div class="card">
    <h2>Global Intake Settings</h2>
    <table>
      <thead>
        <tr><th>Setting Key</th><th>Current Value</th></tr>
      </thead>
      <tbody>
        <tr><td><code>default_intake_label</code></td><td>{st.get("default_intake_label", "")}</td></tr>
        <tr><td><code>llm_conversion_enabled</code></td><td>{st.get("llm_conversion_enabled", "")}</td></tr>
        <tr><td><code>needs_human_prompt</code></td><td>{st.get("needs_human_prompt", "")}</td></tr>
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>Configured Labels ({len(lbls)})</h2>
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Description</th>
          <th>Runtime</th>
          <th>Needs Human</th>
          <th>Status</th>
          <th>Prompt Brief Template</th>
        </tr>
      </thead>
      <tbody>
        {labels_rows}
      </tbody>
    </table>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


def build_app():
    routes = [
        Route("/api/meta", meta),
        Route("/api/health", health),
        Route("/api/status", status),
        Route("/api/triage/threads", threads, methods=["GET"]),
        Route("/api/triage/threads", put_threads, methods=["PUT"]),
        Route("/api/threads", threads, methods=["GET"]),
        Route("/api/threads/{thread_id}", thread_get, methods=["GET"]),
        Route("/api/triage/threads/create", create_thread, methods=["POST"]),
        # The name the pollers use for the same door.
        Route("/api/triage/threads/new", create_thread, methods=["POST"]),
        # The read API (E1): thread by id, events in a range, full-text search.
        Route("/api/triage/events", events_route, methods=["GET"]),
        Route("/api/triage/search", search_route, methods=["GET"]),
        Route("/api/triage/threads/{thread_id}/reply",
              _thread_action("reply"), methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/close",
              _thread_action("close_thread"), methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/reopen",
              _thread_action("reopen_thread"), methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/mute",
              _thread_action("mute"), methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/snooze",
              snooze_route, methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/result", result, methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/result/validate", result_validate, methods=["POST"]),
        Route("/api/triage/threads/index", threads_index, methods=["GET"]),   # before {thread_id}
        Route("/api/triage/threads/{thread_id}", thread_get, methods=["GET"]),
        Route("/api/triage/threads/{thread_id}/conversation", thread_conversation, methods=["GET"]),
        Route("/api/triage/threads/{thread_id}/park", _named_action("park"), methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/unpark", _named_action("unpark"), methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/backlog", _named_action("backlog"), methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/unbacklog", _named_action("unbacklog"), methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/note", append_note, methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/attach", attach, methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/attachments", attachments_list),
        Route("/api/triage/threads/{thread_id}/attachments/{att_id}", attachment_get),
        Route("/api/triage/threads/{thread_id}/attachments/{att_id}/view", attachment_view),
        Route("/api/triage/threads/{thread_id}/claims", claim_create, methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/claims", thread_claims, methods=["GET"]),
        Route("/api/triage/threads/{thread_id}/claims/{claim_id}/result", claim_result, methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/claims/{claim_id}/release", claim_release, methods=["POST"]),
        Route("/api/triage/claims", claims_list),
        Route("/api/triage/pending-dispatch", pending_dispatch),
        Route("/api/triage/dispatcher-heartbeat", dispatcher_heartbeat_get),
        Route("/api/triage/dispatcher-heartbeat", dispatcher_heartbeat_put,
              methods=["POST"]),
        Route("/api/triage/pending-working", pending_working),
        Route("/api/triage/threads/{thread_id}/dispatched", dispatched, methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/retry", retry_route, methods=["POST"]),
        Route("/api/triage/threads/{thread_id}/wake", _named_action("wake"), methods=["POST"]),
        Route("/api/triage/needs-owner-check", needs_owner_check, methods=["POST"]),
        Route("/api/triage/card-rules", card_rules_stats, methods=["GET"]),
        Route("/api/triage/categories/merge-case", categories_merge_case, methods=["POST"]),
        Route("/api/triage/open-asks", open_asks_route, methods=["GET"]),
        Route("/api/triage/bulk-settle", bulk_settle, methods=["POST"]),
        Route("/api/triage/gave-up/restore", gave_up_restore, methods=["POST"]),
        Route("/api/triage/rechecks", rechecks_due, methods=["GET"]),
        Route("/api/triage/threads/{thread_id}/recheck", recheck_route, methods=["POST"]),
        Route("/api/triage/gate", gate_log_route, methods=["GET"]),
        Route("/api/triage/lessons", lessons, methods=["GET"]),
        Route("/api/triage/lessons", lesson_open_route, methods=["POST"]),
        Route("/api/triage/lessons/{lesson_id}", lesson_get_route, methods=["GET"]),
        Route("/api/triage/lessons/{lesson_id}/retire", lesson_retire_route, methods=["POST"]),
        Route("/api/triage/lessons/{lesson_id}/supersede", lesson_supersede_route, methods=["POST"]),
        Route("/api/triage/lessons/{lesson_id}/gate-implemented", lesson_gate_implemented_route, methods=["POST"]),
        Route("/api/triage/lessons/from-thread/{thread_id}", lesson_from_thread_route, methods=["POST"]),
        Route("/api/triage/suppressions", suppressions),
        Route("/api/triage/resort", resort, methods=["POST"]),
        Route("/settings", settings_ui_page),
        Route("/api/triage/settings", get_settings_route, methods=["GET"]),
        Route("/api/triage/settings", post_settings_route, methods=["POST"]),
        Route("/api/triage/labels", list_labels_route, methods=["GET"]),
        Route("/api/triage/labels", put_label_route, methods=["POST"]),
        Route("/api/triage/labels/{name}", delete_label_route, methods=["DELETE"]),
        Route("/api/triage/threads/{thread_id}/label", thread_label_route, methods=["POST"]),
        Route("/metrics", metrics_route, methods=["GET"]),
        Route("/api/refusals", refusals_list, methods=["GET"]),
    ]
    if "notebook" in _site.modules():
        # The notebook's routes (app/notebook_api.py), paths and shapes unchanged, behind the
        # same bearer gate. Imported here so a site without the module never loads it.
        from app import notebook_api
        routes += notebook_api.routes

    global _ROUTE_METRIC_KEYS
    _ROUTE_METRIC_KEYS = _compile_route_metric_keys([r.path for r in routes])

    async def telemetry_gate(request, call_next):
        trace_id = request.headers.get("x-trace-id") or f"tr-req-{int(time.time()*1000)}-{os.urandom(4).hex()}"
        start_ts = time.time()
        try:
            response = await call_next(request)
            dur = time.time() - start_ts
            response.headers["X-Trace-ID"] = trace_id
            response.headers["X-Response-Time"] = f"{(dur*1000):.2f}ms"
            if _is_unauth_probe(request):
                _record_unauth_probe(request.method)
                return response
            _record_http_metric(request.method, request.url.path, response.status_code, dur)
            if 400 <= response.status_code < 500:
                # Read the (small) answer for its detail, then hand the same bytes on.
                body = b"".join([chunk async for chunk in response.body_iterator])

                async def _replay():
                    yield body
                response.body_iterator = _replay()
                try:
                    detail = getattr(request.state, "client_error_detail", "") or _client_error_detail(body)
                    client_ip = request.client.host if request.client else ""
                    user_agent = request.headers.get("user-agent", "")
                    caller = request.headers.get("x-caller", "").strip()
                    batch = getattr(request.state, "batch_ids", "")
                    _record_client_error(trace_id, request.method, request.url.path,
                                         response.status_code, detail, client=client_ip,
                                         user_agent=user_agent, caller=caller, threads=batch)
                    # The log line dies with the container; this row does not. Only an
                    # authenticated caller's refusal is kept (principal "" is the legacy
                    # token): a 401 is anyone's bad token, and a row per one would let a
                    # caller with no credential push every real record out of the capped table.
                    params = request.scope.get("path_params") or {}
                    if getattr(request.state, "principal", None) is None:
                        return response
                    route = _metric_path(request.url.path)
                    if route == UNMATCHED_PATH:
                        # The label is one bucket for every unrouted path, so the row alone could
                        # not say what the caller asked for; the path (never the query) is the fact.
                        detail = "%s (path %s)" % (detail, _log_word(request.url.path, 120))
                    await run_in_threadpool(
                        _write_refusal,
                        method=request.method if request.method in _KNOWN_METHODS else "OTHER",
                        route=route,
                        thread_id=_log_text(params.get("thread_id", "")
                                            or getattr(request.state, "first_rejected_id", ""), 200),
                        status=response.status_code,
                        client=client_ip,
                        user_agent=_log_text(user_agent),
                        caller=_log_text(caller),
                        batch=batch,
                        actor=_log_text(getattr(request.state, "body_actor", "")),
                        login=_log_text(request.headers.get("x-helm-actor", "").strip()),
                        principal=getattr(request.state, "principal", ""),
                        detail=_log_text(detail))
                except Exception:
                    pass  # a log line never costs the caller its answer
            return response
        except ClientDisconnect:
            # The client went away mid-request (e.g. a heartbeat sender timing
            # out). Expected network noise, not a gateway error: answer 499
            # quietly instead of propagating. A re-raise reaches Starlette's
            # ServerErrorMiddleware, which answers 500 and re-raises, and Sentry
            # captures the propagated exception -- the error tracker kept filing
            # the client-disconnect noise after the propagate-only attempt (10 events
            # 2026-09-22, trace shows status 500). No metric, no error record,
            # no body nobody will read.
            return Response(status_code=499)
        except Exception as exc:
            dur = time.time() - start_ts
            _record_http_metric(request.method, request.url.path, 500, dur)
            _record_unhandled_error(trace_id, request.method, request.url.path, exc)
            return JSONResponse({
                "detail": "Internal server error",
                "trace_id": trace_id,
                "error": str(exc),
            }, status_code=500, headers={"X-Trace-ID": trace_id})

    async def auth_gate(request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)
        if not TOKEN:
            return JSONResponse({"ok": False, "detail": "server has no HELM_APP_TOKEN configured; refusing open"}, status_code=500)
        header = request.headers.get("Authorization", "")
        principal = _principal(header)
        if principal is None:
            request.state.unauth_probe = _metric_path(request.url.path) == UNMATCHED_PATH
            # Why, for telemetry_gate's client_error line; the header's value is never kept.
            request.state.client_error_detail = (
                "missing Authorization header" if not header else
                "Authorization token matches no caller" if header.startswith("Bearer ") else
                "Authorization header is not Bearer")
            return JSONResponse({"ok": False, "detail": "unauthorized"}, status_code=401)
        login = request.headers.get("x-helm-actor", "").strip()
        if login and request.method.upper() not in ("GET", "HEAD", "OPTIONS"):
            # The web role forwards the Authentik login; the store maps every login to
            # the owner (DECISIONS #12), so the access log is where the login itself is kept.
            sys.stderr.write("helm access: login=%s %s %s\n" % (login[:64], request.method, request.url.path))
            sys.stderr.flush()
        # The principal rides the request in a context var, so every event the
        # store writes for it can say which caller it was (helm.tokens).
        request.state.principal = principal
        reset = _tokens.PRINCIPAL.set(principal)
        try:
            if principal:
                _tokens.touch(_db, principal)
            return await call_next(request)
        finally:
            _tokens.PRINCIPAL.reset(reset)

    import contextlib

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if "notebook" in _site.modules():
            # Index the folder (home) or start the export check as the
            # server comes up, so the first notebook request does not pay for
            # it; notebook_api's lazy path stays as the fallback (test clients
            # that never run the lifespan still work).
            from app import notebook_api
            notebook_api.startup()
        yield

    async def not_found(request, exc):
        # Starlette's own 404 answer (plain text), except that a guess at the thread API
        # carries a hint at the real route.
        for prefix, hint in _GUESS_HINTS.items():
            if request.url.path.startswith(prefix):
                return JSONResponse({"ok": False, "detail": exc.detail, "hint": hint}, status_code=404)
        return PlainTextResponse(exc.detail, status_code=404)

    from starlette.middleware import Middleware
    app = Starlette(routes=routes, middleware=[
        Middleware(BaseHTTPMiddleware, dispatch=telemetry_gate),
        Middleware(BaseHTTPMiddleware, dispatch=auth_gate),
    ], exception_handlers={404: not_found}, lifespan=lifespan)
    return app


async def refusals_list(request):
    """Who sent the requests the api refused (last 14 days), newest first. Bearer only; the web
    role does not proxy it. ?thread_id= and ?since=<epoch> narrow it, ?limit= caps it (100)."""
    q = request.query_params
    try:
        since, limit = float(q.get("since") or 0), int(q.get("limit") or 100)
    except ValueError:
        return JSONResponse({"ok": False, "detail": "since is a number of seconds, limit an integer"},
                            status_code=400)
    con = _db()
    try:
        items = store.list_refusals(con, thread_id=q.get("thread_id", ""), since=since, limit=limit)
    finally:
        con.close()
    return JSONResponse({"ok": True, "refusals": items})


async def metrics_route(request):
    lines = []
    lines.append("# HELP helm_http_requests_total Total number of HTTP requests processed")
    lines.append("# TYPE helm_http_requests_total counter")
    with _metrics_lock:
        for (m, p, s), count in _request_counts.items():
            lines.append(f'helm_http_requests_total{{method="{m}",path="{p}",status="{s}"}} {count}')

    lines.append("# HELP helm_http_request_duration_seconds_sum Total request latency in seconds")
    lines.append("# TYPE helm_http_request_duration_seconds_sum counter")
    with _metrics_lock:
        for (m, p), dur in _request_duration_sum.items():
            lines.append(f'helm_http_request_duration_seconds_sum{{method="{m}",path="{p}"}} {dur:.4f}')

    lines.append("# HELP helm_client_errors_total 4xx answers by route and kind")
    lines.append("# TYPE helm_client_errors_total counter")
    with _metrics_lock:
        for (m, p, s, k), count in _client_errors.items():
            lines.append(f'helm_client_errors_total{{method="{m}",path="{p}",status="{s}",kind="{k}"}} {count}')

    lines.append("# HELP helm_unauthenticated_probes_total Unauthenticated requests for a path no route serves (scanners), answered 401")
    lines.append("# TYPE helm_unauthenticated_probes_total counter")
    with _metrics_lock:
        for m, count in _unauth_probes.items():
            lines.append(f'helm_unauthenticated_probes_total{{method="{m}"}} {count}')

    lines.append("# HELP helm_threads_total Total number of triage threads by state")
    lines.append("# TYPE helm_threads_total gauge")
    try:
        con = _db()
        try:
            rows = con.execute("SELECT state, COUNT(*) as cnt FROM threads WHERE tombstoned=0 GROUP BY state").fetchall()
            for r in rows:
                lines.append(f'helm_threads_total{{state="{r["state"]}"}} {r["cnt"]}')
        finally:
            con.close()
    except Exception:
        pass

    content = "\n".join(lines) + "\n"
    return Response(content, media_type="text/plain; version=0.0.4; charset=utf-8")


app = build_app()
