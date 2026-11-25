"""One ordered event list per conversation.

A thread's conversation is its Helm events, served the same way at both sites from the api
role's `/api/triage/threads/<id>/conversation` and, through the web role, `/api/conversations/<id>`
(with its stream). The payload is `thread_id`, `task_id`, `task_status`, `thread_state`, `events`,
`pending_notes`, `ask`, `review_state`, `properties`, `generated_at`, plus `console: "none"`: no
agent console merges task data into it (the home console is retired), so the composer sends every
note and answer to Helm. `task`, `progress` and `run` stay as inputs for a caller that has them.

Event types: `message` (the owner's replies and notes, the agent's reports), `system` (claimed,
parked, state changes, progress), `ask` (a past question, prompt line only: the live ask
is the composer's, never drawn twice), `run` (the live run block).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time

# How a Helm event's kind reads in the timeline (helm-web v43's labels).
KIND_LABEL = {
    "state-change": "state", "filed": "filed", "update": "producer update", "failure-detail": "failure detail",
    "rule-breach": "rule breach", "log-tail": "log tail", "chat-note": "chat note", "park": "parked",
    "unpark": "unparked", "backlog": "to the backlog", "unbacklog": "pulled back", "note": "note",
    "instruction": "instruction", "close": "closed", "claim": "claimed", "release": "released",
    "dispatched": "dispatched", "wake": "woke", "snooze": "snoozed", "reopen": "reopened", "mute": "muted",
}
_ASK_HEAD = re.compile(r"^ASKED YOU\s*\([^)]*\):\s*", re.I)
# How a task's ## Progress mirrors a Helm note: "owner via Helm note (thread <id>): <note>"
# (a progress line that repeats his note is not drawn twice).
_NOTE_MIRROR = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?:owner via Helm note\s*(?:\(thread\s+[^)]*\))?|owner \(thread[^)]*\))\s*[:\-—]*\s*", re.I)
NOTE_DEDUPE_S = 300


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(float(ts or 0), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _eid(prefix: str, ts: float, body: str) -> str:
    return "%s-%s-%s" % (prefix, ts, hashlib.sha256(body.encode()).hexdigest()[:12])


def _opt_words(v) -> str:
    v = "" if v is None else str(v)
    fixed = {"__redirect__": "with a note", "__task__": "handed it over", "ack": "acknowledged", "note-only": "a note only"}
    return fixed.get(v) or " ".join(w for w in re.split(r"[_-]+", v) if w)


_OPT_LINE = re.compile(r"^- (.+?)(?: -- .*)?$")
# An attachment's event is written for the agent that must open it (store.attach); a person
# reads the file's name and size.
_ATTACHED = re.compile(r"^FILE ATTACHED -- OPEN IT BEFORE YOU ANSWER:\s*(.+?) \(([^,()]+), ([^)]+)\)\s*(?:\n(.*))?$", re.S)


def human_attachment(body: str) -> str:
    m = _ATTACHED.match(str(body or "").strip())
    if not m:
        return str(body or "")
    rest = [ln for ln in (m.group(4) or "").splitlines() if ln.strip() and not ln.strip().startswith(("http://", "https://", "/"))]
    return "📎 %s (%s)%s" % (m.group(1), m.group(3), ("\n" + "\n".join(rest)) if rest else "")


def _answer_label(thread: dict, value, before_ts=None) -> str:
    """How an answer reads: the label of the option chosen.

    The store keeps an answer's value only, and settling a card clears its ask, so for an
    old answer the labels come from the question as it was asked: the bot-ask event before
    it lists them ("- label -- effect", store._ask_event_body). A value maps to the one
    label that holds all its words; anything else reads as the value's own words."""
    if str(value or "") in ("__redirect__", "__task__", "note-only", "ack"):
        return _opt_words(value)   # Helm's own answer values read the same on every card
    if str(value or "").startswith("__use_draft__:"):
        return "Use the draft"     # the press of the draft button (store.USE_DRAFT_PREFIX)
    for src in [thread.get("ask") or {}] + [e.get("ask") or {} for e in thread.get("events") or []]:
        for o in src.get("options") or []:
            if o.get("value") == value and o.get("label"):
                return o["label"]
    words = [w for w in re.split(r"[^a-z0-9]+", str(value or "").lower()) if w]
    asked = [e for e in thread.get("events") or []
             if e.get("kind") == "bot-ask" and (before_ts is None or float(e.get("ts") or 0) <= float(before_ts))]
    if words and asked:
        labels = [m.group(1).strip() for m in (_OPT_LINE.match(ln.strip()) for ln in str(asked[-1].get("body") or "").splitlines()[1:]) if m]
        hits = [lb for lb in labels if all(any(t.startswith(w) for t in re.split(r"[^a-z0-9]+", lb.lower())) for w in words)]
        if len(hits) == 1:
            return hits[0]
    return _opt_words(value)


def lit_ask(thread: dict | None):
    """The ask the composer draws: a lit thread's current ask, or None."""
    if not thread or thread.get("state") != "needs-you":
        return None
    a = thread.get("ask") or {}
    return a if a.get("kind") and a.get("kind") != "none" else None


# A relay that answers on the owner's word is recorded under its token principal (helm.actions.door_source);
# the timeline names it for people. Keep in step with web/conversation.js RELAY_AUTHORS.
RELAY_AUTHORS = {"relay-mcp": "Relay", "relay": "Relay"}


def helm_events(thread: dict | None) -> list[dict]:
    """A Helm thread's events, typed for the timeline."""
    if not thread:
        return []
    events = thread.get("events") or []
    last_ask = -1
    if lit_ask(thread):
        for i, e in enumerate(events):
            if e.get("kind") == "bot-ask":
                last_ask = i
    out = []
    for i, e in enumerate(events):
        kind = str(e.get("kind") or "").strip()
        body = str(e.get("body") or "").strip()
        source = str(e.get("source") or "").strip()
        ts = float(e.get("ts") or 0)
        if not body and kind != "bot-ask":
            continue
        if i == last_ask:
            continue          # the live ask is the composer's
        # A stable id for the event (gw-<ts>-<sha256(body)[:12]>) when the store gave none.
        eid = str(e.get("event_id") or _eid("gw", ts, body))
        ev = {"id": eid, "ts": ts, "iso": iso(ts), "kind": kind, "source": source}
        # A machine's hand-off keeps these kinds with the machine's source (store.handoff_source),
        # so it is not written up as the owner's. The owner's own taps are "owner"; a chat session acts on
        # his word ("session"); an old event may carry no source.
        machine = source if source.lower() not in ("", "owner", "session") else ""
        machine = RELAY_AUTHORS.get(machine, machine)   # a relay reads by its readable name
        if kind == "owner-reply" and body.startswith("__use_draft__:"):
            # The press of a report's draft button: say which button, from the report that carried it.
            sha = body[len("__use_draft__:"):]
            btn = next((x["draft"]["button"] for x in events if (x.get("draft") or {}).get("sha") == sha), "Use the draft")
            ev.update(type="message", author=machine or "You", kind="reply", body="You pressed: " + btn)
        elif kind == "owner-reply":
            ev.update(type="message", author=machine or "You", kind="reply",
                      body=("%s answered: " % machine if machine else "You answered: ") + _answer_label(thread, body, ts))
        elif kind == "owner-note" or (kind == "note" and source.lower() == "owner"):
            ev.update(type="message", author=machine or "You", kind="note", body=body)
        elif kind == "bot-result":
            ev.update(type="message", author=source or "agent", kind="report", body=body)
            if e.get("draft"):
                ev["draft"] = e["draft"]      # the checked draft this report carried (store.normalize_draft)
        elif kind in ("owner-attachment", "agent-attachment"):
            mine = kind == "owner-attachment"
            ev.update(type="message", author="You" if mine else (source or "agent"), kind="attachment", body=human_attachment(body))
            if str(e.get("source_id") or "").strip():
                ev["att_id"] = str(e["source_id"]).strip()   # the file's id: the card opens it in place
        elif kind == "bot-ask":
            ev.update(type="ask", author=source or "bot", body=_ASK_HEAD.sub("", body).split("\n")[0].strip())
        else:
            ev.update(type="system", author=source or "helm", body="%s · %s" % (KIND_LABEL.get(kind, kind or "event"), body))
        out.append(ev)
    return out


def progress_events(progress) -> list[dict]:
    """A task's ## Progress lines. A line that mirrors one of the owner's Helm notes is his message."""
    out = []
    for p in progress or []:
        ts = float(p.get("ts") or 0)
        text = str(p.get("text") or "").strip()
        if not text:
            continue
        ev = {"id": str(p.get("id") or _eid("prog", ts, text)), "ts": ts, "iso": p.get("iso") or iso(ts), "kind": "progress"}
        if _NOTE_MIRROR.match(text):
            ev.update(type="message", author="You", body=_NOTE_MIRROR.sub("", text).strip(), mirrored=True)
        else:
            ev.update(type="system", author=str(p.get("author") or "progress"), body=text)
        out.append(ev)
    return out


def run_event(run: dict | None) -> list[dict]:
    if not run:
        return []
    ts = float(run.get("ts") or time.time())
    pane = run.get("pane_id") or run.get("pane") or "run"
    return [{"id": "run-%s-%d" % (pane, int(ts)), "ts": ts, "iso": iso(ts), "type": "run", "kind": "run",
             "author": run.get("runner") or "agent", "body": run.get("body") or "Working in %s" % pane,
             "runner": run.get("runner"), "status": run.get("status"),
             "tool_calls": list(run.get("tool_calls") or []), "output_tail": list(run.get("output_tail") or [])}]


def _clean(body: str) -> str:
    return re.sub(r"\s+", " ", _NOTE_MIRROR.sub("", body or "")).strip().lower()


def merge(thread: dict | None, progress=(), run: dict | None = None, extra=()) -> list[dict]:
    """Every source in one list: oldest first, one entry per event (see merge_events)."""
    return merge_events(helm_events(thread) + progress_events(progress) + run_event(run) + [dict(e) for e in extra or ()])


def merge_events(cands) -> list[dict]:
    """Oldest first, one entry per event.

    A note the owner wrote on the Helm thread and the same note mirrored into the task's progress
    are one message (the Helm one, within NOTE_DEDUPE_S); identical system lines in the same
    second are one line; ids are unique."""
    cands = [dict(c) for c in cands]
    for c in cands:
        if c.get("type") == "message" and c.get("author") == "You" and _NOTE_MIRROR.match(c.get("body") or ""):
            c["body"] = _NOTE_MIRROR.sub("", c["body"]).strip()
            c["mirrored"] = True
    cands.sort(key=lambda c: (float(c.get("ts") or 0), 0 if not c.get("mirrored") else 1, str(c.get("id"))))
    seen_ids, notes, systems, out = set(), [], set(), []
    for c in cands:
        cid = str(c.get("id"))
        if cid in seen_ids:
            continue
        if c.get("type") == "message" and c.get("author") == "You" and c.get("kind") in ("note", "progress"):
            key = _clean(c.get("body", ""))
            if any(k == key and abs(t - float(c["ts"])) < NOTE_DEDUPE_S for k, t in notes):
                continue
            notes.append((key, float(c["ts"])))
        elif c.get("type") == "system":
            key = (_clean(c.get("body", "")), int(float(c.get("ts") or 0)))
            if key in systems:
                continue
            systems.add(key)
        c.pop("mirrored", None)
        seen_ids.add(cid)
        out.append(c)
    return out


def payload(thread: dict | None, *, console: str = "none", task: dict | None = None, progress=(), run: dict | None = None,
            extra=(), properties: dict | None = None, review_state=None, pending_notes=None) -> dict:
    """The conversation (see the module docstring)."""
    tid = (thread or {}).get("thread_id") or (task or {}).get("thread_id")
    return {
        "ok": True, "thread_id": tid,
        "task_id": (task or {}).get("id"), "task_status": (task or {}).get("status"),
        "thread_state": (thread or {}).get("state"),
        "events": merge(thread, progress, run, extra),
        "pending_notes": list(pending_notes or []),
        "ask": lit_ask(thread),
        "review_state": review_state,
        "properties": properties if properties is not None else {},
        "generated_at": iso(time.time()),
        "console": console,
    }
