"""The notebook's HTTP surface: the reference's app/api.py, paths and response shapes
unchanged, served in-process. Thin: parse, one call, JSON out.

Inbox routes call helm.store / helm.actions through notebook.helm_adapter and
return the store's answer, including its rejections word for word. Note routes
read the index. The writes into the notebook (save, new note, checkbox, append,
rename, attach) go through notebook.edit, which holds the rules; each of its
refusals comes back with its own status, and at a read-only site (home,
HELM_NOTES_MODE=folder-readonly) every one of them is a 405 with a JSON reason.

Mounting: app/service.py appends `routes` to its own when "notebook" is in
helm.site.modules(). The service's bearer gate then covers every route here,
/att/ included; the web role injects the token for the browser.

    GET /api/notes/meta   the notebook's own facts (stats, truth, mode, profile,
                          root, last_index, last_verify): the reference's old /api/meta
                          notebook fields, under a name that does not collide
                          with Helm's /api/meta.
"""
from __future__ import annotations

import datetime as dt
import json
import mimetypes
import os
import re
import threading
import traceback
from urllib.parse import quote, unquote

from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from starlette.responses import Response, FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from notebook import config, cutover, edit, export, helm_adapter, notes, render, topic
from notebook import day as day_mod


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- Startup: the index and the background passes -----------------------------------------
_started = False
_refused: Exception | None = None
_start_lock = threading.Lock()


def startup() -> None:
    """Once per process, on the first request (or from an on_startup hook):
    store mode on a DB still the folder's runs the cutover first (notebook.cutover
    on_start); folder modes index the folder in full and start the 60 s mtime
    rescan; store mode starts the export check. Idempotent.

    A cutover that refuses raises, and keeps raising: from the lifespan it stops
    the api starting (fail closed), and a lazy start never serves the notebook."""
    global _started, _refused
    with _start_lock:
        if _started:
            return
        if _refused is not None:
            raise _refused
        try:
            cutover.on_start()
        except cutover.NotReady as e:
            _refused = e
            raise
        _started = True
        try:
            if config.mode() == "store":
                export.start_verify()
            else:
                notes.full_index()
                notes.start_rescan()
        except Exception as e:  # noqa: BLE001
            print("notebook startup: %s: %s" % (type(e).__name__, e), flush=True)


def _ncon():
    startup()
    con = notes.db()
    try:
        notes.rescan_if_due(con)
    except Exception as e:  # noqa: BLE001
        print("notebook rescan: %s: %s" % (type(e).__name__, e), flush=True)
    return con


def _hcon():
    return helm_adapter.store()


# --- Plumbing -------------------------------------------------------------------------------
async def _body(request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except ValueError as e:
        raise ApiError(400, "body is not valid JSON: " + str(e))
    if not isinstance(v, dict):
        raise ApiError(400, "body must be a JSON object")
    return v


def _actor(request):
    """Who is acting: the web role forwards the Authentik login as X-Helm-Actor."""
    v = request.headers.get("x-helm-actor", "")
    return v.strip() or None


def _guard(fn):
    async def wrapped(request):
        try:
            return await fn(request)
        except helm_adapter.HelmError as e:
            payload = {"ok": False, "detail": e.detail, "helm_status": e.status}
            return JSONResponse(payload, status_code=e.status if e.status < 500 else 502)
        except edit.EditError as e:
            payload = {"ok": False, "detail": e.detail}
            payload.update(e.extra)
            return JSONResponse(payload, status_code=e.status)
        except ApiError as e:
            return JSONResponse({"ok": False, "detail": e.detail}, status_code=e.status)
        except Exception as e:  # noqa: BLE001
            print("ERROR %s %s: %s" % (request.method, request.url.path, e), flush=True)
            traceback.print_exc()
            return JSONResponse({"ok": False, "error": "%s: %s" % (type(e).__name__, e)},
                                status_code=500)
    wrapped.__name__ = fn.__name__
    wrapped.note_write = getattr(fn, "note_write", False)
    return wrapped


def _note_write(fn):
    """A route that writes the notebook. At a read-only site it is refused before
    the body is read, so the reason is the same for every write and no request
    body is ever parsed to be thrown away."""
    async def wrapped(request):
        if config.read_only():
            return JSONResponse({"ok": False, "detail": edit.READONLY_DETAIL,
                                 "mode": config.mode()}, status_code=405,
                                headers={"Allow": "GET"})
        return await fn(request)
    wrapped.__name__ = fn.__name__
    wrapped.note_write = True
    return wrapped


def _q(request, name, default=""):
    v = request.query_params.get(name)
    return default if v is None else v


def _refuse_hidden(ncon, rel: str) -> None:
    """Where the folder is the truth, a hidden path (notes.hidden: a dot-prefixed
    part, a skipped folder) is never served, even while an index written before
    that rule still lists it; the next index pass drops the row. At store truth
    the store serves what it holds."""
    if notes.hidden(rel) and not notes.store_is_truth(ncon):
        raise ApiError(404, "no note at " + rel)


def _folder_visible(ncon, rel: str) -> bool:
    """Where the folder is the truth, only a path the index may hold now: not hidden,
    and confined (real path inside the root, no hard link). An older index's row for
    anything else is skipped until the next pass drops it, as the note routes do."""
    if notes.store_is_truth(ncon):
        return True
    return not notes.hidden(rel) and notes.confined_path(rel) is not None


def _visible_rows(ncon, rows: list) -> list:
    return [r for r in rows if _folder_visible(ncon, str((r or {}).get("rel") or ""))]


# --- Inbox: the store, in-process ------------------------------------------------------
# Each store call runs on a worker thread, so a slow one holds up only the
# request waiting on it, not every other page and note request behind it.
@_guard
async def get_inbox(request):
    return JSONResponse(await run_in_threadpool(helm_adapter.list_threads))


@_guard
async def post_inbox_new(request):
    body = await _body(request)
    out = await run_in_threadpool(helm_adapter.new_thread, body, _actor(request))
    return JSONResponse(out, status_code=201)


@_guard
async def post_inbox_action(request):
    body = await _body(request)
    tid = request.path_params["thread_id"]
    action = request.path_params["action"]
    out = await run_in_threadpool(helm_adapter.act, tid, action, body, _actor(request))
    return JSONResponse(out, status_code=202)


@_guard
async def get_thread(request):
    tid = request.path_params["thread_id"]
    hcon = _hcon()
    try:
        t = helm_adapter.store_thread(hcon, tid)
    finally:
        hcon.close()
    if t is None:
        raise ApiError(404, "no such thread: " + tid)
    return JSONResponse(t)


@_guard
async def get_thread_refs(request):
    """What the thread's sources and links point at inside the app (topic.refs): the
    conversation's properties draw each as a link to its note or thread."""
    tid = request.path_params["thread_id"]
    ncon, hcon = _ncon(), _hcon()
    try:
        t = helm_adapter.store_thread(hcon, tid, with_events=False)
        if t is None:
            raise ApiError(404, "no such thread: " + tid)
        return JSONResponse({"thread_id": tid,
                             "refs": topic.refs(t, ncon, hcon, lambda r: _folder_visible(ncon, r))})
    finally:
        ncon.close()
        hcon.close()


# --- Search and the switcher -------------------------------------------------------
@_guard
async def get_search(request):
    q = _q(request, "q").strip()
    if not q:
        return JSONResponse({"q": q, "notes": [], "threads": []})
    ncon, hcon = _ncon(), _hcon()
    try:
        return JSONResponse({"q": q, "notes": _visible_rows(ncon, notes.search(ncon, q)),
                             "threads": helm_adapter.search_threads(hcon, q)})
    finally:
        ncon.close()
        hcon.close()


@_guard
async def get_switcher(request):
    q = _q(request, "q").strip()
    ncon, hcon = _ncon(), _hcon()
    try:
        return JSONResponse({"q": q, "notes": _visible_rows(ncon, notes.switcher(ncon, q)),
                             "threads": helm_adapter.switcher_threads(hcon, q) if q else []})
    finally:
        ncon.close()
        hcon.close()


# --- Notebook, read-only -----------------------------------------------------------
@_guard
async def get_meta(request):
    """The notebook's facts (the reference's /api/meta notebook fields)."""
    ncon = _ncon()
    try:
        st = notes.stats(ncon)
        truth = notes.truth(ncon)
        cutover_at = notes.get_meta(ncon, "cutover_at")
    finally:
        ncon.close()
    prof = config.profile()
    return JSONResponse({
        "ok": True, "notes": st, "truth": truth, "mode": config.mode(),
        "read_only": config.read_only(), "profile": prof.name, "root": notes.VAULT,
        "vault": notes.VAULT, "vault_aliases": config.vault_aliases(),
        "daily_root": prof.daily_root, "cutover_at": cutover_at,
        "last_index": notes.LAST_INDEX, "last_verify": export.LAST_VERIFY,
        "rescan_every_s": notes.RESCAN_EVERY_S,
    })


@_guard
async def get_folders(request):
    ncon = _ncon()
    try:
        today = dt.date.today()
        trel = notes.daily_rel(today)
        ttasks = notes.tasks_for(ncon, trel) if notes.exists(ncon, trel) else []
        carry = day_mod.carry_over(ncon, today)
        return JSONResponse({
            "folders": notes.folders(ncon, lambda r: _folder_visible(ncon, r)),
            # Where a note may be filed (the new-note dialog): every allowed folder, an empty one too,
            # where `folders` lists only what there is to read.
            "allowed": notes.folders_allowed(),
            "stats": notes.stats(ncon),
            "today": {"date": today.isoformat(), "rel": trel,
                      "exists": notes.exists(ncon, trel),
                      "open_tasks": sum(1 for t in ttasks if not t["done"])},
            "previous": carry and {"date": carry["date"], "rel": carry["rel"],
                                   "open_tasks": len(carry["tasks"])},
            "read_only": config.read_only(),
            "profile": config.profile().name,
        })
    finally:
        ncon.close()


@_guard
async def get_list(request):
    folder = _q(request, "folder")
    ncon = _ncon()
    try:
        return JSONResponse({"folder": folder, "notes": _visible_rows(ncon, notes.list_folder(ncon, folder))})
    finally:
        ncon.close()


@_guard
async def get_note(request):
    rel = _q(request, "rel").replace("\\", "/")
    ncon = _ncon()
    try:
        _refuse_hidden(ncon, rel)
        note = notes.get(ncon, rel)
        asked = rel
        if note is None:
            # A moved note's old path (an old link, a bookmark, a card) opens the note it moved to.
            moved = notes.follow_redirect(ncon, rel)
            if moved and _folder_visible(ncon, moved):
                rel, note = moved, notes.get(ncon, moved)
        if note is None:
            raise ApiError(404, "no note at " + asked)
        if rel != asked:
            note["redirected_from"] = asked
        if _q(request, "brief") == "1":
            # A hover peek: the title, where it lives and its first paragraph. No render, no backlinks, no tasks.
            return JSONResponse({"rel": note["rel"], "title": note.get("title"), "stem": note.get("stem"), "folder": note.get("folder"),
                                 "mtime": note.get("mtime"), "redirected_from": note.get("redirected_from"),
                                 "excerpt": notes.first_paragraph(note["body"])})
        note["html"] = render.render(note["body"], render.Resolver(ncon))
        note["tasks"] = notes.tasks_for(ncon, rel)
        note["outlinks"] = notes.outlinks(ncon, rel)
        note["backlinks"] = notes.backlinks(ncon, rel, lambda r: _folder_visible(ncon, r))
        note["topic"] = topic.is_topic(rel)
        note["editable"], note["reason"] = edit.editable(rel)
        del note["body"]
        return JSONResponse(note)
    finally:
        ncon.close()


@_guard
async def get_topic(request):
    rel = _q(request, "rel").replace("\\", "/")
    ncon, hcon = _ncon(), _hcon()
    try:
        _refuse_hidden(ncon, rel)
        if not notes.exists(ncon, rel):
            moved = notes.follow_redirect(ncon, rel)
            if not (moved and notes.exists(ncon, moved) and _folder_visible(ncon, moved)):
                raise ApiError(404, "no note at " + rel)
            rel = moved
        return JSONResponse(topic.context(rel, ncon, hcon, lambda r: _folder_visible(ncon, r)))
    finally:
        ncon.close()
        hcon.close()


# /att: what a browser may do with a file from the notes folder. Every answer, the
# file or a refusal, is sandboxed (an opened attachment runs no script and gets no
# origin) and never sniffed. In the wiki profile (written by many
# agents; in every mode, DECISIONS W1) a type a browser would run as a document of this origin is
# downloaded, never shown: by the extension of the name asked for or of the file it
# resolves to, or by the type either one guesses to. Images and the rest stay inline.
ATT_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}
ACTIVE_EXT = frozenset({".html", ".htm", ".shtml", ".xhtml", ".xht", ".svg", ".svgz", ".xml",
                        ".xsl", ".xslt", ".js", ".mjs"})
ACTIVE_TYPES = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml",
                          "application/xml", "application/xslt+xml", "text/javascript",
                          "application/javascript", "application/ecmascript", "text/ecmascript"})


def active_content(*names) -> bool:
    """Would a browser run any of these as a document (HTML, SVG, XML, script)?"""
    for n in names:
        ext = os.path.splitext(n)[1].lower()
        guessed = (mimetypes.guess_type(n)[0] or "").lower()
        if ext in ACTIVE_EXT or guessed in ACTIVE_TYPES or guessed.endswith("+xml"):
            return True
    return False


async def get_attachment(request):
    """GET /att/<rel>. Every response carries ATT_HEADERS, whatever the answer."""
    resp = await _attachment(request)
    resp.headers.update(ATT_HEADERS)
    return resp


@_guard
async def _attachment(request):
    """A non-note file from the folder, by its indexed relative path. The path
    is looked up in the index (never joined from the request) and then
    confined: no `..`, nothing absolute, nothing hidden, and the real path
    (symlinks resolved) must be inside the folder's real path and not hidden."""
    rel = request.path_params["rel"].replace("\\", "/")   # Starlette already decoded the path once
    if notes.confined_path(rel) is None:
        return JSONResponse({"ok": False, "detail": "that path is not inside the notes folder, "
                                                    "or is hidden"}, status_code=403)
    ncon = _ncon()
    try:
        row = ncon.execute("SELECT rel FROM files WHERE rel=?", (rel,)).fetchone()
        if row is None:
            moved = notes.moved_file(ncon, rel)       # a moved file's old path serves it
            row = {"rel": moved} if moved else None
    finally:
        ncon.close()
    if row is None:
        return JSONResponse({"ok": False, "detail": "no such file in the notebook index"},
                            status_code=404)
    # The bytes come from one fd reached by the no-link walk (W10): a swap of the file for
    # a link after any check serves nothing, never the link's target. They are streamed from
    # that fd with one Range and an ETag (W11), so a video seeks and nothing is held whole.
    fd = notes.open_confined(row["rel"])
    if fd is None:
        return JSONResponse({"ok": False, "detail": "file is gone, or is not a plain file inside "
                                                    "the notes folder"}, status_code=404)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return JSONResponse({"ok": False, "detail": "file is gone"}, status_code=404)
    size = st.st_size
    name = row["rel"].rsplit("/", 1)[-1]
    headers = {"Cache-Control": "private, max-age=300", "Accept-Ranges": "bytes",
               "ETag": '"%x-%x-%x"' % (st.st_ino, size, st.st_mtime_ns)}
    if config.untrusted_folder() and active_content(row["rel"]):
        headers["Content-Disposition"] = content_disposition(name)
        media = "application/octet-stream"
    else:
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    inm = request.headers.get("if-none-match", "")
    if inm and (inm.strip() == "*" or headers["ETag"] in [x.strip() for x in inm.split(",")]):
        os.close(fd)
        return Response(status_code=304, headers=headers)
    start, end, status = 0, size - 1, 200
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (request.headers.get("range") or "").strip())
    if m and (m.group(1) or m.group(2)):          # one range; several, or a bad one, is the whole file
        if not m.group(1):
            n = int(m.group(2))
            start = max(0, size - n) if n else size
        else:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else start + ATT_MAX_RANGE - 1
        end = min(end, size - 1)
        if start >= size or start > end:
            os.close(fd)
            h = dict(headers, **{"Content-Range": "bytes */%d" % size})
            return Response(status_code=416, headers=h)
        status = 206
        headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)
    closed = []

    def close_once():
        # The body's finally and the background task both call this: a client that leaves
        # before the body starts never runs the generator, so its finally alone would leak
        # the fd (review 18: 256 fds after 470 aborted GETs on a real uvicorn).
        if not closed:
            closed.append(1)
            try:
                os.close(fd)
            except OSError:
                pass

    def body():
        try:
            pos, left = start, length
            while left > 0:
                chunk = os.pread(fd, min(1 << 16, left), pos)
                if not chunk:
                    break
                pos += len(chunk)
                left -= len(chunk)
                yield chunk
        finally:
            close_once()
    return StreamingResponse(body(), status_code=status, media_type=media, headers=headers,
                             background=BackgroundTask(close_once))


ATT_MAX_RANGE = 8 * 1024 * 1024   # an open-ended Range answers at most this much; the client asks again


def content_disposition(name: str) -> str:
    """A download name for any name: an ASCII fallback plus RFC 5987 filename* (a latin-1-only
    header would 500 on a non-latin-1 name)."""
    fallback = re.sub(r'[^\x20-\x7e]|["\\]', "_", name) or "download"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (fallback, quote(name, safe=""))


# --- Day -------------------------------------------------------------------------------
def _parse_date(s: str) -> dt.date:
    if not s:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        raise ApiError(400, "date must be YYYY-MM-DD")


def _day_payload(d: dt.date) -> dict:
    ncon, hcon = _ncon(), _hcon()
    try:
        resolver = render.Resolver(ncon)
        return day_mod.assemble(d, ncon, hcon, lambda body: render.render(body, resolver),
                                lambda r: _folder_visible(ncon, r))
    finally:
        ncon.close()
        hcon.close()


@_guard
async def get_day(request):
    return JSONResponse(await run_in_threadpool(_day_payload, _parse_date(_q(request, "date"))))


@_guard
@_note_write
async def post_day_append(request):
    """Text onto the end of a daily note, any date, one line or several.
    Appending is the one thing a past day's note accepts."""
    body = await _body(request)
    d = _parse_date(str(body.get("date") or ""))
    ncon = _ncon()
    try:
        out = edit.append_daily(ncon, d, str(body.get("line") or ""),
                                source=_actor(request) or "web",
                                allow_secret=bool(body.get("allow_secret")))
    finally:
        ncon.close()
    payload = _day_payload(d)
    payload["appended"] = out["appended"]
    return JSONResponse(payload, status_code=201)


# --- Notebook, writes ------------------------------------------------------------------
@_guard
async def get_raw(request):
    ncon = _ncon()
    try:
        return JSONResponse(edit.read(ncon, _q(request, "rel")))
    finally:
        ncon.close()


@_guard
@_note_write
async def put_note(request):
    """Save a note. With create: true the note may not exist yet, which is the
    shape a script's write goes through."""
    body = await _body(request)
    rel, text = str(body.get("rel") or ""), str(body.get("text") or "")
    source = str(body.get("source") or _actor(request) or "web")[:80]
    ncon = _ncon()
    try:
        if body.get("create"):
            out = edit.upsert(ncon, rel, text, allow_secret=bool(body.get("allow_secret")),
                              allow_history=bool(body.get("allow_history")), source=source)
            return JSONResponse(out, status_code=201 if out.get("created") else 200)
        return JSONResponse(edit.save(ncon, rel, text, base_mtime=body.get("base_mtime"),
                                      allow_secret=bool(body.get("allow_secret")), source=source))
    finally:
        ncon.close()


def _source(request, body: dict | None = None, default: str = "script") -> str:
    """Who a write says it is from: the body's `source`, else the forwarded login, else
    the per-caller token's principal, else `default`. The principal is logged apart too."""
    from helm import tokens as _tokens
    v = (body or {}).get("source") or _actor(request) or _tokens.PRINCIPAL.get() or default
    return str(v)[:80]


@_guard
@_note_write
async def post_append(request):
    """Append to a note: the bridge's append and a script's `cat >>` (the wiki bridge).
    Body {rel, text, gap?: "blank"|"line", create?, allow_secret?, source?}."""
    body = await _body(request)
    ncon = _ncon()
    try:
        out = edit.append(ncon, str(body.get("rel") or ""), str(body.get("text") or ""),
                          gap=str(body.get("gap") or "blank"), create=bool(body.get("create")),
                          allow_secret=bool(body.get("allow_secret")), source=_source(request, body))
        return JSONResponse(out, status_code=201 if out.get("created") else 200)
    finally:
        ncon.close()


@_guard
@_note_write
async def delete_note(request):
    """Retire a note (the bridge's delete): to Trash/<stamp>/<rel> while the folder is
    the truth. `rel` in the query."""
    ncon = _ncon()
    try:
        return JSONResponse(edit.delete(ncon, _q(request, "rel"), source=_source(request)))
    finally:
        ncon.close()


@_guard
@_note_write
async def post_delete(request):
    """Remove a note and say why (the notebook audit). Body {rel, reason, source?}:
    `reason` is required (400 when blank); a daily note or a dated call / case-cleanup
    note is 403; a note that is not there is 404 with a JSON detail, so a caller can tell
    it from a missing route. The text stays in the history. Answers {rel, deleted: true}."""
    body = await _body(request)
    ncon = _ncon()
    try:
        return JSONResponse(edit.delete_with_reason(ncon, str(body.get("rel") or ""), body.get("reason"),
                                                    source=_source(request, body)))
    finally:
        ncon.close()


def _num(request, name, default, cast=float):
    raw = _q(request, name, "")
    if raw == "":
        return default
    try:
        return cast(raw)
    except ValueError:
        raise ApiError(400, "%s must be a number" % name)


@_guard
async def get_recent(request):
    """Notes by mtime, newest first (the bridge's recent). `limit` (max 500), `since` (epoch)."""
    limit = max(1, min(500, _num(request, "limit", 50, int)))
    since = _num(request, "since", 0.0)
    ncon = _ncon()
    try:
        return JSONResponse({"notes": _visible_rows(ncon, notes.recent(ncon, limit, since))})
    finally:
        ncon.close()


@_guard
async def get_writes(request):
    """Helm's own writes since `since` (epoch), oldest first: rel, kind, source, principal
    and the file's sha256 after. The straggler check compares the folder against it."""
    since = _num(request, "since", 0.0)
    limit = max(1, min(5000, _num(request, "limit", 500, int)))
    ncon = _ncon()
    try:
        return JSONResponse({"writes": notes.writes_since(ncon, since, limit)})
    finally:
        ncon.close()


def _cap(request, name, default, hi):
    return max(1, min(hi, _num(request, name, default, int)))


@_guard
async def get_changes(request):
    """Helm's own writes, newest first, with the last lines each added (the home's Changed today)."""
    limit = _cap(request, "limit", 120, 500)
    since = _num(request, "since", 0.0)
    ncon = _ncon()
    try:
        return JSONResponse({"changes": notes.changes(ncon, since, limit, lambda r: _folder_visible(ncon, r)),
                             "since": since})
    finally:
        ncon.close()


@_guard
async def get_graph(request):
    limit = _cap(request, "limit", 12, 50)
    ncon = _ncon()
    try:
        return JSONResponse(notes.graph(ncon, limit, lambda r: _folder_visible(ncon, r)))
    finally:
        ncon.close()


@_guard
async def get_tasks(request):
    limit = _cap(request, "limit", 20, 100)
    ncon = _ncon()
    try:
        return JSONResponse(notes.open_tasks(ncon, limit, lambda r: _folder_visible(ncon, r)))
    finally:
        ncon.close()


@_guard
async def get_atlas(request):
    if config.profile().fixed:
        return JSONResponse({"hosts": []})
    ncon = _ncon()
    try:
        return JSONResponse({"hosts": notes.atlas(ncon, lambda r: _folder_visible(ncon, r))})
    finally:
        ncon.close()


@_guard
async def get_tail(request):
    rel = _q(request, "rel").replace("\\", "/")
    if not rel:
        raise ApiError(400, "'rel' is required")
    n = _cap(request, "n", 6, 20)
    ncon = _ncon()
    try:
        _refuse_hidden(ncon, rel)
        out = notes.tail(ncon, rel, n) if _folder_visible(ncon, rel) else None
        if out is None:
            raise ApiError(404, "no note at " + rel)
        return JSONResponse(out)
    finally:
        ncon.close()


@_guard
async def get_history(request):
    ncon = _ncon()
    try:
        rel = _q(request, "rel")
        _refuse_hidden(ncon, rel)
        return JSONResponse({"rel": rel, "truth": notes.truth(ncon),
                             "revisions": edit.history(ncon, rel)})
    finally:
        ncon.close()


@_guard
async def get_revision(request):
    try:
        rev_id = int(_q(request, "id"))
    except (TypeError, ValueError):
        raise ApiError(400, "'id' must be a number")
    ncon = _ncon()
    try:
        return JSONResponse(edit.revision(ncon, rev_id))
    finally:
        ncon.close()


@_guard
@_note_write
async def post_new(request):
    body = await _body(request)
    ncon = _ncon()
    try:
        out = edit.new_note(ncon, str(body.get("folder") or ""), str(body.get("title") or ""),
                            template=body.get("template"), source=_actor(request) or "web")
        return JSONResponse(out, status_code=201)
    finally:
        ncon.close()


@_guard
@_note_write
async def post_daily_new(request):
    ncon = _ncon()
    try:
        return JSONResponse(edit.create_daily(ncon, source=_actor(request) or "web"),
                            status_code=201)
    finally:
        ncon.close()


@_guard
@_note_write
async def post_toggle(request):
    body = await _body(request)
    try:
        line = int(body.get("line"))
    except (TypeError, ValueError):
        raise ApiError(400, "'line' must be a number")
    ncon = _ncon()
    try:
        return JSONResponse(edit.toggle_task(ncon, str(body.get("rel") or ""), line,
                                             base_mtime=body.get("base_mtime"), done=body.get("done"),
                                             source=_actor(request) or "web"))
    finally:
        ncon.close()


@_guard
@_note_write
async def post_rename(request):
    body = await _body(request)
    ncon = _ncon()
    try:
        return JSONResponse(edit.rename(ncon, str(body.get("rel") or ""), str(body.get("new_stem") or ""),
                                        dry_run=body.get("dry_run", True) is not False,
                                        source=_actor(request) or "web"))
    finally:
        ncon.close()


def _flag(body: dict, key: str, default: bool) -> bool:
    """A yes/no body field: true/false, 1/0, or "true"/"false"/"yes"/"no"; anything else is a 400."""
    v = body.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "yes", "no", "1", "0"):
        return v.strip().lower() in ("true", "yes", "1")
    raise ApiError(400, "%s is true or false" % key)


@_guard
@_note_write
async def post_move(request):
    """Move a note to another path (the notebook organizer). Body {rel, new_rel, redirect?: true,
    links?: false, prune?: true, dry_run?: false, source?}. With redirect (the default) the old path
    keeps working; with links, pages that link the old path by its path are rewritten; with prune
    (the default) folders the move left empty are removed."""
    body = await _body(request)
    ncon = _ncon()
    try:
        return JSONResponse(edit.move(ncon, str(body.get("rel") or ""), str(body.get("new_rel") or ""),
                                      source=_source(request, body), redirect=_flag(body, "redirect", True),
                                      links=_flag(body, "links", False), dry_run=_flag(body, "dry_run", False),
                                      prune=_flag(body, "prune", True)))
    finally:
        ncon.close()


@_guard
@_note_write
async def post_folder(request):
    """Make a top-level folder in the home notebook. Body {name, source?}. 201 when made."""
    body = await _body(request)
    ncon = _ncon()
    try:
        out = edit.create_folder(ncon, str(body.get("name") or ""), source=_source(request, body))
        return JSONResponse(out, status_code=201 if out.get("created") else 200)
    finally:
        ncon.close()


@_guard
async def get_redirects(request):
    """Moved notes' old paths and where they lead, newest first."""
    ncon = _ncon()
    try:
        try:
            limit = max(1, min(5000, int(_q(request, "limit", "500") or 500)))
        except ValueError:
            raise ApiError(400, "limit is a number") from None
        return JSONResponse({"redirects": notes.redirects(ncon, limit)})
    finally:
        ncon.close()


@_guard
@_note_write
async def post_attach(request):
    """The body is the file; the name rides in the X-Filename header, URL-encoded."""
    data = await request.body()
    name = unquote(request.headers.get("x-filename", ""))
    ncon = _ncon()
    try:
        return JSONResponse(edit.attach(ncon, name, data), status_code=201)
    finally:
        ncon.close()


@_guard
async def get_complete(request):
    ncon = _ncon()
    try:
        return JSONResponse({"q": _q(request, "q"),
                             "items": _visible_rows(ncon, edit.complete(ncon, _q(request, "q"), _q(request, "kind", "note")))})
    finally:
        ncon.close()


@_guard
async def get_resolve(request):
    """The note or file a wikilink target opens, as the rendered link resolves it (not a search)."""
    target = _q(request, "target")
    ncon = _ncon()
    try:
        kind, rel = render.link_target(render.Resolver(ncon), target)
        if rel and not _folder_visible(ncon, rel):
            kind, rel = None, None
        return JSONResponse({"target": target, "kind": kind, "rel": rel})
    finally:
        ncon.close()


@_guard
async def get_templates(request):
    return JSONResponse({"templates": edit.templates(),
                         "by_folder": {k: v[:-3] for k, v in edit.TEMPLATE_FOR.items()}})


routes = [
    Route("/api/inbox", get_inbox, methods=["GET"]),
    Route("/api/inbox/new", post_inbox_new, methods=["POST"]),
    Route("/api/inbox/{thread_id}/{action}", post_inbox_action, methods=["POST"]),
    Route("/api/thread/{thread_id}", get_thread, methods=["GET"]),
    Route("/api/thread/{thread_id}/refs", get_thread_refs, methods=["GET"]),
    Route("/api/search", get_search, methods=["GET"]),
    Route("/api/switcher", get_switcher, methods=["GET"]),
    Route("/api/notes/meta", get_meta, methods=["GET"]),
    Route("/api/notes/folders", get_folders, methods=["GET"]),
    Route("/api/notes/list", get_list, methods=["GET"]),
    Route("/api/notes/note", get_note, methods=["GET"]),
    Route("/api/topic", get_topic, methods=["GET"]),
    Route("/api/day", get_day, methods=["GET"]),
    Route("/api/day/append", post_day_append, methods=["POST"]),
    Route("/api/notes/raw", get_raw, methods=["GET"]),
    # Read-only alias for the guessed verb; counted under /api/notes/raw (service._ROUTE_ALIASES).
    Route("/api/notes/read", get_raw, methods=["GET"]),
    # Read-only aliases for the earlier read vocabulary; each is counted under its
    # canonical route (service._ROUTE_ALIASES). Helm's own parameter names apply (rel, folder, q).
    Route("/api/notes/get", get_raw, methods=["GET"]),
    Route("/api/notes", get_list, methods=["GET"]),
    Route("/api/notes/search", get_search, methods=["GET"]),
    Route("/api/fs/list", get_list, methods=["GET"]),
    Route("/api/space/list", get_list, methods=["GET"]),
    Route("/api/notes/note", put_note, methods=["PUT"]),
    Route("/api/notes/note", delete_note, methods=["DELETE"]),
    Route("/api/notes/delete", post_delete, methods=["POST"]),
    Route("/api/notes/append", post_append, methods=["POST"]),
    Route("/api/notes/recent", get_recent, methods=["GET"]),
    Route("/api/notes/writes", get_writes, methods=["GET"]),
    Route("/api/notes/changes", get_changes, methods=["GET"]),
    Route("/api/notes/graph", get_graph, methods=["GET"]),
    Route("/api/notes/tasks", get_tasks, methods=["GET"]),
    Route("/api/notes/atlas", get_atlas, methods=["GET"]),
    Route("/api/notes/tail", get_tail, methods=["GET"]),
    Route("/api/notes/new", post_new, methods=["POST"]),
    Route("/api/notes/daily/new", post_daily_new, methods=["POST"]),
    Route("/api/notes/toggle", post_toggle, methods=["POST"]),
    Route("/api/notes/rename", post_rename, methods=["POST"]),
    Route("/api/notes/move", post_move, methods=["POST"]),
    Route("/api/notes/folder", post_folder, methods=["POST"]),
    Route("/api/notes/redirects", get_redirects, methods=["GET"]),
    Route("/api/notes/attach", post_attach, methods=["POST"]),
    Route("/api/notes/complete", get_complete, methods=["GET"]),
    Route("/api/notes/resolve", get_resolve, methods=["GET"]),
    Route("/api/notes/templates", get_templates, methods=["GET"]),
    Route("/api/notes/history", get_history, methods=["GET"]),
    Route("/api/notes/revision", get_revision, methods=["GET"]),
    # Read-only alias for the guessed plural; counted under /api/notes/history (service._ROUTE_ALIASES).
    Route("/api/notes/revisions", get_history, methods=["GET"]),
    Route("/att/{rel:path}", get_attachment, methods=["GET"]),
]


def write_routes() -> list:
    """The routes that write the notebook (every one answers 405 in a read-only
    mode). The inbox writes are thread actions, not notebook writes, and stay
    open in every mode."""
    return [r for r in routes if getattr(r.endpoint, "note_write", False)]


def mount(app_routes: list) -> list:
    """`app_routes += notebook_api.routes`, for app/service.py when the notebook
    module is on. Returns the list for chaining."""
    app_routes.extend(routes)
    return app_routes
