"""A stand-in for the notebook routes (app/notebook_api.py, built in parallel), so the
notebook screens can be rendered and judged before that module lands. It answers the reference's
paths with the reference's shapes over the fixture's synthetic notes folder and forwards every
other request, bearer included, to the real api role. fixture.py puts it in front of the
api only when the api answers 404 for /api/notes/folders; shoot.py labels the shots.

    python3 tools/screens/fake_notebook.py <port> <api_url> <notes_root> <mode>
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse

import httpx
from markdown_it import MarkdownIt
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

API = sys.argv[2].rstrip("/") if len(sys.argv) > 2 else "http://127.0.0.1:3038"
ROOT = os.path.abspath(sys.argv[3]) if len(sys.argv) > 3 else os.getcwd()
MODE = sys.argv[4] if len(sys.argv) > 4 else "store"
READONLY = MODE == "folder-readonly"
DAILY_RE = re.compile(r"^Daily/\d{4}/\d{4}-\d{2}/(\d{4}-\d{2}-\d{2})\.md$")
md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


# ---------------------------------------------------------------- the folder
def walk():
    out = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = sorted(d for d in dns if not d.startswith("."))
        for fn in sorted(fns):
            if fn.endswith(".md"):
                rel = os.path.relpath(os.path.join(dp, fn), ROOT).replace(os.sep, "/")
                out.append(rel)
    return out


def files():
    out = []
    att = os.path.join(ROOT, "attachments")
    if os.path.isdir(att):
        for fn in sorted(os.listdir(att)):
            out.append("attachments/" + fn)
    return out


def read(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.isfile(p) or not os.path.abspath(p).startswith(ROOT + os.sep):
        return None
    with open(p, encoding="utf-8") as f:
        text = f.read()
    fm, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return {"rel": rel, "text": text, "body": body, "frontmatter": fm, "mtime": os.path.getmtime(p)}


def stem_of(rel):
    return rel.rsplit("/", 1)[-1][:-3]


def folder_of(rel):
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def title_of(rec):
    m = re.search(r"^#\s+(.+)$", rec["body"], re.M)
    return rec["frontmatter"].get("title") or (m.group(1).strip() if m else stem_of(rec["rel"]))


def is_daily(rel):
    return bool(DAILY_RE.match(rel))


def resolve(target):
    t = target.split("|")[0].split("#")[0].strip()
    for rel in walk():
        if rel[:-3].lower() == t.lower() or stem_of(rel).lower() == t.lower():
            return rel
    return None


def render(body):
    def wl(m):
        target = m.group(2)
        alias = m.group(3) or target.split("/")[-1]
        rel = resolve(target)
        if m.group(1) == "!":
            return '<a class="wl file" href="/att/%s">%s</a>' % (urllib.parse.quote(target), html.escape(alias))
        if rel:
            return '<a class="wl" href="#/note/%s">%s</a>' % (urllib.parse.quote(rel, safe=""), html.escape(alias))
        return '<span class="wl missing">%s</span>' % html.escape(alias)
    # Wikilinks are resolved on the rendered HTML (markdown-it escapes raw HTML in the source).
    out = re.sub(r"(!?)\[\[([^\]|#\n]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]", wl, md.render(body))
    # checkbox lines get a real (numbered) checkbox, as the reference's renderer does
    n = [0]
    lines = body.splitlines()
    def box(m):
        while n[0] < len(lines) and not re.match(r"^\s*[-*+]\s+\[( |x|X)\]", lines[n[0]]):
            n[0] += 1
        line = n[0]; n[0] += 1
        done = m.group(1) != " "
        return '<li class="task%s"><input type="checkbox" class="task-box" data-line="%d"%s> ' % (" done" if done else "", line, " checked" if done else "")
    out = re.sub(r"<li>\[( |x|X)\] ", box, out)
    return out


def tasks_for(rel):
    rec = read(rel)
    out = []
    if rec:
        for i, line in enumerate(rec["text"].splitlines()):
            m = re.match(r"^\s*[-*+]\s+\[( |x|X)\]\s+(.*)$", line)
            if m:
                out.append({"line": i, "done": m.group(1) != " ", "text": m.group(2)})
    return out


def outlinks(rel):
    rec = read(rel)
    out = []
    for m in re.finditer(r"(!?)\[\[([^\]|#\n]+)", rec["text"] if rec else ""):
        out.append({"target": m.group(2), "rel": resolve(m.group(2)), "alias": None, "embed": m.group(1) == "!", "line": 0, "title": m.group(2)})
    return out


def brief(rel):
    rec = read(rel)
    return {"rel": rel, "folder": folder_of(rel), "stem": stem_of(rel), "title": title_of(rec), "mtime": rec["mtime"], "size": len(rec["text"]), "daily": is_daily(rel)}


def folders():
    counts = {}
    for rel in walk():
        f = folder_of(rel)
        top = f.split("/")[0]
        counts[top] = counts.get(top, 0) + 1
        if f != top:
            counts[f] = counts.get(f, 0) + 1
    return [{"folder": f, "count": c} for f, c in sorted(counts.items())]


def daily_rel(d):
    return "Daily/%s/%s/%s.md" % (d.strftime("%Y"), d.strftime("%Y-%m"), d.isoformat())


# ---------------------------------------------------------------- upstream (the api role)
async def upstream(request, method, path_qs, body=None):
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("authorization", "content-type", "x-helm-actor", "accept")}
    async with httpx.AsyncClient(base_url=API, timeout=30) as c:
        r = await c.request(method, path_qs, content=body, headers=headers)
    return r


async def forward(request):
    body = await request.body()
    r = await upstream(request, request.method, request.url.path + ("?" + request.url.query if request.url.query else ""), body or None)
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))


async def threads(request):
    r = await upstream(request, "GET", "/api/triage/threads?include_snoozed=true")
    return (r.json() if r.status_code == 200 else {}).get("threads", [])


# ---------------------------------------------------------------- routes
async def meta(request):
    return JSONResponse({"ok": True, "name": "notebook", "mode": MODE, "vault": ROOT, "truth": "folder" if READONLY else "store",
                         "notes": {"notes": len(walk()), "files": len(files())}, "readonly": READONLY})


async def r_folders(request):
    today = dt.date.today()
    trel = daily_rel(today)
    prev = None
    for rel in sorted(walk(), reverse=True):
        m = DAILY_RE.match(rel)
        if m and m.group(1) < today.isoformat():
            open_t = [t for t in tasks_for(rel) if not t["done"]]
            if open_t:
                prev = {"date": m.group(1), "rel": rel, "open_tasks": len(open_t)}
            break
    return JSONResponse({"folders": folders(), "stats": {"notes": len(walk()), "files": len(files())},
                         "today": {"date": today.isoformat(), "rel": trel, "exists": read(trel) is not None,
                                   "open_tasks": sum(1 for t in tasks_for(trel) if not t["done"])},
                         "previous": prev})


async def r_list(request):
    folder = request.query_params.get("folder", "")
    notes = [brief(rel) for rel in walk() if folder_of(rel) == folder or folder_of(rel).startswith(folder + "/")]
    notes.sort(key=lambda n: (-n["mtime"] if not folder.startswith("Daily") else 0, n["stem"]))
    if folder.startswith("Daily"):
        notes.sort(key=lambda n: n["stem"], reverse=True)
    return JSONResponse({"folder": folder, "notes": notes})


async def r_note(request):
    rel = request.query_params.get("rel", "")
    rec = read(rel)
    if not rec:
        return JSONResponse({"ok": False, "detail": "no note at " + rel}, status_code=404)
    editable = not READONLY and not (is_daily(rel) and DAILY_RE.match(rel).group(1) < dt.date.today().isoformat())
    return JSONResponse({**brief(rel), "frontmatter": rec["frontmatter"], "html": render(rec["body"]), "tasks": tasks_for(rel),
                         "outlinks": outlinks(rel), "topic": rel.startswith("Projects/") or rel.startswith("Reference/"),
                         "editable": editable, "reason": "the wiki is the source of truth" if READONLY else ("history: a past day's note accepts only appends" if not editable else "")})


async def r_raw(request):
    rel = request.query_params.get("rel", "")
    rec = read(rel)
    if not rec:
        return JSONResponse({"ok": False, "detail": "no note at " + rel}, status_code=404)
    return JSONResponse({"rel": rel, "text": rec["text"], "mtime": rec["mtime"], "size": len(rec["text"]), "editable": not READONLY,
                         "reason": "the wiki is the source of truth" if READONLY else "", "daily": is_daily(rel), "folder": folder_of(rel), "stem": stem_of(rel)})


async def r_topic(request):
    rel = request.query_params.get("rel", "")
    rec = read(rel)
    if not rec:
        return JSONResponse({"ok": False, "detail": "no note at " + rel}, status_code=404)
    stem = stem_of(rel)
    ts = await threads(request)
    def cites(t):
        return any(("note:" + rel[:-3]).lower() in str(l.get("url", "")).lower() for l in t.get("links") or [])
    def mentions(t):
        return stem.lower() in (t.get("title", "") + " " + t.get("summary", "")).lower()
    hits = [dict(t, match="suggested" if not cites(t) else None, match_why="mentions the note's name") for t in ts if cites(t) or mentions(t)]
    open_t = [t for t in hits if t["state"] != "done"]
    settled = [t for t in hits if t["state"] == "done"]
    backlinks = [brief(r) for r in walk() if r != rel and ("[[" + rel[:-3]) in read(r)["text"]]
    daily = [{"stem": stem_of(r), "rel": r} for r in walk() if is_daily(r) and ("[[" + rel[:-3]) in read(r)["text"]]
    return JSONResponse({"rel": rel, "title": title_of(rec), "topic": True, "keys": {"stem": stem, "name": stem.lower(), "cases": re.findall(r"\b00\d{6}\b", rec["text"])},
                         "open": open_t, "settled": settled[:10], "settled_total": len(settled), "backlinks": backlinks, "daily_mentions": daily})


KIND = {"owner-reply": ("answered", "you answered"), "owner-note": ("noted", "your note"), "bot-result": ("run", "a run reported"),
        "bot-ask": ("filed", "asked you"), "park": ("park", "you parked it"), "unpark": ("unpark", "you unparked it"),
        "backlog": ("backlog", "you put it in the backlog"), "unbacklog": ("unbacklog", "you pulled it back")}


async def r_day(request):
    d = dt.date.fromisoformat(request.query_params.get("date") or dt.date.today().isoformat())
    return JSONResponse(await day_payload(request, d))


async def day_payload(request, d):
    rel = daily_rel(d)
    rec = read(rel)
    start = dt.datetime.combine(d, dt.time()).timestamp()
    r = await upstream(request, "GET", "/api/triage/events?since=%s&until=%s&limit=500" % (start, start + 86400))
    evs = r.json().get("events", []) if r.status_code == 200 else []
    tl = []
    for e in evs:
        k = e.get("kind")
        if k == "state-change":
            body = e.get("body") or ""
            kind, label = ("settled", "you settled it") if e.get("reply_kind") == "close" or "closed" in body else ("state", "state")
        elif k in KIND:
            kind, label = KIND[k]
        else:
            continue
        detail = "" if k == "bot-ask" else (e.get("body") or "")[:240]
        tl.append({"ts": e["ts"], "last_ts": e["ts"], "thread_id": e["thread_id"], "thread_ids": [e["thread_id"]], "count": 1, "title": e.get("title"),
                   "category": e.get("category"), "state": e.get("state"), "urgency": e.get("urgency"), "kind": kind, "label": label,
                   "detail": detail, "raw": k == "owner-reply", "more": [], "parts": [{"label": label}]})
    tasks = tasks_for(rel) if rec else []
    prev_rel = daily_rel(d - dt.timedelta(days=1))
    carry = None
    if read(prev_rel):
        ct = [t for t in tasks_for(prev_rel) if not t["done"]]
        carry = {"date": (d - dt.timedelta(days=1)).isoformat(), "rel": prev_rel, "tasks": ct}
    mentions = [dict(brief(l["rel"]), title=title_of(read(l["rel"]))) for l in (outlinks(rel) if rec else []) if l["rel"]]
    return {"date": d.isoformat(), "rel": rel, "exists": rec is not None, "mtime": rec["mtime"] if rec else None,
            "html": render(rec["body"]) if rec else "", "frontmatter": rec["frontmatter"] if rec else {}, "tasks": tasks,
            "open_tasks": sum(1 for t in tasks if not t["done"]), "timeline": tl, "calls": [], "carry": carry, "mentions": mentions,
            "today": d == dt.date.today(), "prev": (d - dt.timedelta(days=1)).isoformat(), "next": (d + dt.timedelta(days=1)).isoformat(),
            "changed": [brief(x) for x in walk() if dt.date.fromtimestamp(read(x)["mtime"]) == d][:8] if READONLY else []}


async def r_day_append(request):
    if READONLY:
        return JSONResponse({"ok": False, "detail": "folder-readonly: the wiki is the source of truth; nothing is written here"}, status_code=405)
    body = await request.json()
    d = dt.date.fromisoformat(body.get("date") or dt.date.today().isoformat())
    rel = daily_rel(d)
    p = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(("" if not os.path.getsize(p) else "") + str(body.get("line") or "").rstrip("\n") + "\n")
    out = await day_payload(request, d)
    out["appended"] = body.get("line")
    return JSONResponse(out, status_code=201)


async def r_search(request):
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse({"q": q, "notes": [], "threads": []})
    notes = []
    for rel in walk():
        rec = read(rel)
        i = rec["text"].lower().find(q.lower())
        if i >= 0:
            snip = rec["text"][max(0, i - 60): i + 80].replace("\n", " ")
            snip = re.sub(re.escape(q), lambda m: "\x02" + m.group(0) + "\x03", snip, flags=re.I)
            notes.append(dict(brief(rel), snippet=snip))
    r = await upstream(request, "GET", "/api/triage/search?q=" + urllib.parse.quote(q))
    return JSONResponse({"q": q, "notes": notes, "threads": r.json().get("threads", []) if r.status_code == 200 else []})


async def r_switcher(request):
    q = request.query_params.get("q", "").strip().lower()
    notes = [brief(rel) for rel in walk() if q and q in stem_of(rel).lower()][:8]
    ts = [t for t in await threads(request) if q and q in t.get("title", "").lower()][:8] if q else []
    return JSONResponse({"q": q, "notes": notes, "threads": [{"thread_id": t["thread_id"], "title": t["title"], "category": t.get("category"), "state": t["state"]} for t in ts]})


async def r_complete(request):
    q = request.query_params.get("q", "").lower()
    kind = request.query_params.get("kind", "note")
    items = [{"kind": "note", "label": stem_of(rel), "detail": folder_of(rel), "rel": rel, "insert": stem_of(rel)} for rel in walk() if q in stem_of(rel).lower()]
    if kind == "all":
        items += [{"kind": "file", "label": f.split("/")[-1], "detail": "attachments", "rel": f, "insert": f.split("/")[-1]} for f in files() if q in f.lower()]
    return JSONResponse({"q": q, "items": items[:20]})


async def r_resolve(request):
    t = request.query_params.get("target", "").split("|")[0].split("#")[0].strip().lower()
    hit = next((rel for rel in sorted(walk(), key=len) if t in (stem_of(rel).lower(), rel.lower(), rel[:-3].lower())), None)
    return JSONResponse({"target": t, "kind": "note" if hit else None, "rel": hit})


async def r_templates(request):
    return JSONResponse({"templates": ["Project", "Reference"], "by_folder": {"Projects": "Project"}})


async def r_write(request):
    if READONLY:
        return JSONResponse({"ok": False, "detail": "folder-readonly: the wiki is the source of truth; edit it in the wiki engine"}, status_code=405)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    path = request.url.path
    if path == "/api/notes/note":
        rel = body.get("rel", "")
        rec = read(rel)
        if not rec:
            return JSONResponse({"ok": False, "detail": "no note at " + rel}, status_code=404)
        if body.get("base_mtime") is not None and abs(float(body["base_mtime"]) - rec["mtime"]) > 0.001:
            return JSONResponse({"ok": False, "detail": "the note changed on disk", "mtime": rec["mtime"], "text": rec["text"]}, status_code=409)
        text = str(body.get("text") or "")
        if re.search(r"(?i)(api[_-]?key|password|token)\s*[:=]\s*\S{8,}", text):
            return JSONResponse({"ok": False, "detail": "a new line looks like a credential", "secret": True, "shape": "key=value"}, status_code=400)
        with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
            f.write(text)
        return JSONResponse({"rel": rel, "mtime": os.path.getmtime(os.path.join(ROOT, rel)), "size": len(text), "saved": True})
    if path == "/api/notes/new":
        folder, title = str(body.get("folder") or "Reference"), str(body.get("title") or "").strip()
        if not title or title.lower() == "untitled":
            return JSONResponse({"ok": False, "detail": "a real title, not Untitled"}, status_code=400)
        rel = folder + "/" + re.sub(r"[\\/:*?\"<>|]", "-", title) + ".md"
        p = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("# " + title + "\n\n")
        return JSONResponse({"rel": rel, "mtime": os.path.getmtime(p), "size": 0, "created": True}, status_code=201)
    if path == "/api/notes/daily/new":
        rel = daily_rel(dt.date.today())
        p = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write("# " + dt.date.today().isoformat() + "\n\n")
        return JSONResponse({"rel": rel, "mtime": os.path.getmtime(p), "size": 0, "created": True}, status_code=201)
    if path == "/api/notes/toggle":
        rel, line = body.get("rel", ""), int(body.get("line", -1))
        rec = read(rel)
        lines = rec["text"].splitlines()
        lines[line] = re.sub(r"\[( |x|X)\]", "[x]" if body.get("done") else "[ ]", lines[line], count=1)
        with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return JSONResponse({"rel": rel, "line": line, "done": bool(body.get("done")), "mtime": os.path.getmtime(os.path.join(ROOT, rel))})
    if path == "/api/notes/rename":
        rel, new = body.get("rel", ""), str(body.get("new_stem") or "").strip()
        to = folder_of(rel) + "/" + new + ".md"
        changes = [{"rel": r, "count": read(r)["text"].count("[[" + rel[:-3]), "daily": is_daily(r)} for r in walk() if r != rel and ("[[" + rel[:-3]) in read(r)["text"]]
        out = {"from": rel, "to": to, "new_stem": new, "changes": changes, "total": sum(c["count"] for c in changes), "dry_run": body.get("dry_run", True) is not False}
        if not out["dry_run"]:
            os.replace(os.path.join(ROOT, rel), os.path.join(ROOT, to))
        return JSONResponse(out)
    if path == "/api/notes/attach":
        name = urllib.parse.unquote(request.headers.get("x-filename", "") or "file.bin")
        data = await request.body()
        os.makedirs(os.path.join(ROOT, "attachments"), exist_ok=True)
        with open(os.path.join(ROOT, "attachments", name), "wb") as f:
            f.write(data)
        return JSONResponse({"rel": "attachments/" + name, "name": name, "size": len(data), "image": name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")),
                             "wikilink": "![[" + name + "]]"}, status_code=201)
    return JSONResponse({"ok": False, "detail": "not stubbed"}, status_code=404)


async def r_att(request):
    rel = urllib.parse.unquote(request.path_params["rel"]).replace("\\", "/")
    p = os.path.abspath(os.path.join(ROOT, rel))
    if ".." in rel.split("/") or not p.startswith(ROOT + os.sep) or not os.path.isfile(p):
        return JSONResponse({"ok": False, "detail": "no such file"}, status_code=404)
    return FileResponse(p)


async def r_inbox(request):
    r = await upstream(request, "GET", "/api/triage/threads?include_snoozed=true&include_backlog=true")
    return Response(r.content, status_code=r.status_code, media_type="application/json")


app = Starlette(routes=[
    Route("/api/notes/meta", meta),
    Route("/api/notes/folders", r_folders),
    Route("/api/notes/list", r_list),
    Route("/api/notes/note", r_note, methods=["GET"]),
    Route("/api/notes/note", r_write, methods=["PUT"]),
    Route("/api/notes/raw", r_raw),
    Route("/api/notes/complete", r_complete),
    Route("/api/notes/resolve", r_resolve),
    Route("/api/notes/templates", r_templates),
    Route("/api/notes/{what:path}", r_write, methods=["POST"]),
    Route("/api/topic", r_topic),
    Route("/api/day", r_day),
    Route("/api/day/append", r_day_append, methods=["POST"]),
    Route("/api/search", r_search),
    Route("/api/switcher", r_switcher),
    Route("/api/inbox", r_inbox),
    Route("/att/{rel:path}", r_att),
    Route("/{path:path}", forward, methods=["GET", "POST", "PUT", "DELETE", "HEAD"]),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
