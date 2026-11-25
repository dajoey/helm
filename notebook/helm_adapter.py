"""Helm, seen from the notebook: the reference's helm_client.py with the HTTP hop and
the read-only opens of triage.db replaced by calls into the same process's
store. The function names are the reference's, so day.py, topic.py and the
API read as they did.

Two doors, still: the live list and every action go through helm.store /
helm.actions, so the store keeps running its snooze-wake and its invariants
on each call and its rejections come back word for word as HelmError(status,
detail). History for the Day view, search and the Topic hub is read from the
same connection.

Who acted: the API role receives the authenticated web user as the
X-Helm-Actor header and passes it here as `actor`; it is stamped on the
reply or close as `by`. With no actor a tap from the notebook is the owner's own
(the reference's rule), and helm.store.normalize_by decides what that means per site.
"""
from __future__ import annotations

import json
import os
import sqlite3

from helm import actions as _actions
from helm import store as _store

from . import config

# Which body keys each action reads (the reference's table). Anything else the page
# sends is dropped here rather than forwarded.
ACTIONS = {
    "reply": ("value", "note"),
    "close": ("reason",),
    "reopen": ("note",),
    "mute": ("note",),
    "snooze": ("wake_at", "note"),
    "park": ("reason", "source"),
    "unpark": ("note", "source"),
    "backlog": ("reason", "source"),
    "unbacklog": ("note", "reason", "source"),
}
NEW_THREAD_KEYS = ("title", "summary", "category", "urgency", "intent", "instruction", "links")


class HelmError(Exception):
    def __init__(self, status: int, detail: str, payload: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.payload = payload or {"ok": False, "detail": detail}


def _wrap(e: _store.StoreError) -> HelmError:
    payload = {"ok": False, "detail": e.detail}
    if e.extra:
        payload.update(e.extra)
    return HelmError(e.status, e.detail, payload)


# --- The store, in-process ------------------------------------------------------
def store(path: str | None = None) -> sqlite3.Connection:
    """The thread store: helm.store.db on HELM_DB (migrations run on open, which
    is additive and idempotent). One connection per request; the caller closes."""
    return _store.db(path or os.environ.get("HELM_DB") or config.helm_db())


def _with_con(con, fn):
    own = con is None
    con = con or store()
    try:
        return fn(con)
    finally:
        if own:
            con.close()


def stamp(actor) -> str:
    """Who an answer or close from the notebook is by. A forwarded web login is a
    person at one of the owner's own screens, and the only person is the owner, so any login
    maps to "owner" (docs/DECISIONS.md #12; the API role's
    `_actor_of` does the same). History then reads "owner" as every
    client expects; a session's answer stays a session's only when a
    bearer caller says so in the body."""
    return "owner"


# --- Live: the list and the actions ---------------------------------------------
def list_threads(include_backlog: bool = True, con=None) -> dict:
    """Helm's own list: live threads only, snooze-wake run inside the read."""
    return _with_con(con, lambda c: _store.list_threads(c, include_backlog=include_backlog))


def new_thread(body: dict, actor=None, con=None, caller=None) -> dict:
    clean = {k: body[k] for k in NEW_THREAD_KEYS if k in body}

    def go(c):
        try:
            return _actions.create(c, clean, actor=actor, caller=caller)[1]
        except _store.StoreError as e:
            raise _wrap(e)
    return _with_con(con, go)


def act(thread_id: str, action: str, body: dict, actor=None, con=None) -> dict:
    """Helm's answer to one action, or HelmError with Helm's status and detail."""
    if action not in ACTIONS:
        raise HelmError(404, "no such action: %s" % action)
    payload = {k: body[k] for k in ACTIONS[action] if k in body}
    if action in ("reply", "close"):
        payload["by"] = stamp(actor)

    def go(c):
        try:
            return _actions.act(c, thread_id, action, payload, actor=actor)[1]
        except _store.StoreError as e:
            raise _wrap(e)
    return _with_con(con, go)


# --- History and search ---------------------------------------------------------------
def _jloads(raw, default):
    try:
        v = json.loads(raw) if raw else default
    except ValueError:
        return default
    return v if v is not None else default


def thread_from_row(r, events=None) -> dict:
    """the reference's shape from a raw threads row (kept for callers that hold one)."""
    keys = r.keys()
    out = {
        "thread_id": r["thread_id"],
        "title": r["title"],
        "summary": r["summary"],
        "category": r["category"] or "Uncategorized",
        "urgency": r["urgency"],
        "state": r["state"],
        "ask": {"kind": r["ask_kind"], "prompt": r["ask_prompt"],
                "options": _jloads(r["ask_options"], [])},
        "responder": r["responder"],
        "sources": _jloads(r["sources"], []),
        "links": _jloads(r["links"], []) if "links" in keys else [],
        "first_seen": r["first_seen"],
        "last_change": r["last_change"],
        "last_note": r["last_note"] if "last_note" in keys else "",
        "wake_at": r["wake_at"] if "wake_at" in keys else 0,
        "parked": bool(r["parked"]) if "parked" in keys else False,
        "parked_reason": r["parked_reason"] if "parked_reason" in keys else "",
        "suppressed": bool(r["suppressed"]),
    }
    if events is not None:
        out["events"] = events
    return out


def _events_of(con, thread_id: str) -> list:
    return [{"ts": e["ts"], "source": e["source"], "source_id": e["source_id"],
             "kind": e["kind"], "body": e["body"], "reply_kind": e.get("reply_kind") or ""}
            for e in _store.list_events(con, thread=thread_id, limit=5000)]


def _suppressed(con, thread_id: str) -> bool:
    r = con.execute("SELECT suppressed FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
    return bool(r and r["suppressed"])


def store_thread(con, thread_id: str, with_events: bool = True):
    t = _store.get_thread(con, thread_id, with_events=with_events)
    if t is not None:
        t["suppressed"] = _suppressed(con, thread_id)  # the reference's thread_from_row carried it
    return t


def thread_exists(con, thread_id: str) -> bool:
    """A live (not tombstoned) thread with this id: what a source or link naming
    another thread must find before it is drawn as a link to it."""
    return con.execute("SELECT 1 FROM threads WHERE thread_id=? AND tombstoned=0",
                       (thread_id,)).fetchone() is not None


def store_threads(con, with_events: bool = False) -> list:
    """Every readable thread, settled ones included (the Topic hub's candidates)."""
    out = _store.list_threads(con, include_done=True, include_snoozed=True,
                              include_backlog=True)["threads"]
    if not with_events:
        for t in out:
            t.pop("events", None)
    return out


def threads_citing(con, rel: str) -> list:
    """The threads whose links carry `note:<rel>` (or the file's path): the
    explicit card <-> note join, checked with one LIKE over the links column so
    a plain note's page never loads every thread."""
    norel = rel[:-3] if rel.lower().endswith(".md") else rel
    # The store's json.dumps escapes non-ASCII ("Caf\u00e9"), so a page with such a name
    # is matched in both spellings; topic.match() confirms the link on each candidate.
    # A Windows path (a card's path to a file on another host) is stored with its
    # backslashes escaped, so the backslash spelling is a needle as well.
    bs = norel.replace("/", "\\")
    needles = []
    for s in (norel, rel, json.dumps(norel)[1:-1], json.dumps(rel)[1:-1], json.dumps(bs)[1:-1]):
        if s not in needles:
            needles.append(s)
    rows = con.execute(
        "SELECT thread_id FROM threads WHERE tombstoned=0 AND ("
        + " OR ".join(["links LIKE ?"] * len(needles)) + ")",
        tuple("%" + s + "%" for s in needles)).fetchall()
    out = []
    for r in rows:
        t = _store.get_thread(con, r["thread_id"], with_events=False)
        if t is not None:
            out.append(t)
    return out


def store_events_between(con, start: float, end: float) -> list:
    """Every event in [start, end) joined to its thread, with the thread's
    current ask_options so an answer can be labelled."""
    rows = _store.list_events(con, since=start, until=end, limit=5000)
    opts = {}
    for e in rows:
        tid = e["thread_id"]
        if tid not in opts:
            r = con.execute("SELECT ask_options FROM threads WHERE thread_id=?", (tid,)).fetchone()
            opts[tid] = r["ask_options"] if r else "[]"
        e["ask_options"] = opts[tid]
    return rows


def store_threads_first_seen_between(con, start: float, end: float) -> list:
    rows = con.execute(
        "SELECT thread_id FROM threads WHERE first_seen >= ? AND first_seen < ? AND tombstoned=0"
        " ORDER BY first_seen", (start, end)).fetchall()
    out = []
    for r in rows:
        t = _store.get_thread(con, r["thread_id"], with_events=False)
        if t is None:
            continue
        first = con.execute("SELECT source, body, kind FROM events WHERE thread_id=? ORDER BY ts LIMIT 1",
                            (r["thread_id"],)).fetchone()
        t["first_source"] = first["source"] if first else ""
        t["first_body"] = first["body"] if first else ""
        t["first_kind"] = first["kind"] if first else ""
        out.append(t)
    return out


def search_threads(con, q: str, limit: int = 30) -> list:
    out = _store.search_threads(con, q, limit=limit)
    for t in out:
        t["suppressed"] = _suppressed(con, t["thread_id"])
    return out


def switcher_threads(con, q: str, limit: int = 10) -> list:
    return _store.switcher_threads(con, q, limit=limit)
