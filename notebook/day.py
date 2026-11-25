"""The Day view: one calendar date, assembled from the daily note and the
inbox's history. the reference's app/day.py, ported. The timeline is rendered from
Helm's store and never written into the note.

The timeline is built from the structured event fields: `kind`,
`source` and `reply_kind` decide what a row is and who did it. Kinds the store
writes: bot-ask, owner-reply, owner-note, bot-result, state-change, instruction,
note, park, unpark, backlog, unbacklog, owner-attachment, agent-attachment,
lesson-candidate. Sources: owner, session, bot, helm, helm-wake,
helm-gate, helm-stall, helm-recheck, helm-park-expiry, helm-gave-up, a web
login, or a producer's name (old rows may carry a retired agent's source:
an earlier build's principal; they read as a run's). Only a `state-change` by a person has no
structured sub-kind (a snooze, a mute, a reopen and a settle are all
state-change from `owner`), so for those rows the reference's English-body parsing
stays, as the fallback it was meant to be.

At home the Day view is the thread timeline plus the wiki pages whose mtime
falls on that day (`changed`); the work profile adds the day's call threads.
"""
from __future__ import annotations

import datetime as dt
import json
import re


from . import config, helm_adapter, notes

SETTLED_CHOICE_RE = re.compile(r"^settled (?:from a chat session )?by choosing\s+(['\"])(.*)\1\s*$", re.S)
EXCERPT = 240
BURST_S = 120

# Who acted, per timeline kind, when the entry did not say (a burst never
# crosses actors, so an answer and the report a run sends back a minute
# later stay two rows).
ACTOR = {
    "answered": "you", "noted": "you", "settled": "you", "snoozed": "you", "reopened": "you",
    "muted": "you", "park": "you", "unpark": "you", "backlog": "you", "unbacklog": "you",
    "attached": "you", "lesson": "you",
    "run": "bot", "woke": "bot", "capped": "bot", "dispatch": "bot", "asked": "bot",
    "gate": "helm", "recheck": "helm", "instruction": "helm",
    "filed": "producer", "detail": "producer", "retired": "producer", "state": "producer",
}
# Which part of a burst gives the row its headline.
RANK = {"filed": 9, "settled": 9, "snoozed": 8, "capped": 8, "park": 7, "unpark": 7,
        "backlog": 7, "unbacklog": 7, "reopened": 7, "muted": 7, "woke": 7, "gate": 7,
        "recheck": 7, "run": 6, "asked": 6, "instruction": 6, "answered": 5, "noted": 4,
        "attached": 4, "lesson": 4, "detail": 3, "dispatch": 3, "retired": 2, "state": 1}
PRODUCER_NOUN = {
    "failure-detail": "the failure detail", "analysis": "an analysis", "finding": "a finding",
    "rule-breach": "a rule breach", "log-tail": "a log tail", "draft": "a draft",
    "due": "a due date", "correction": "a correction", "chat-note": "a chat note",
    "note": "a note",
}
HELM_SOURCES = {"helm", "helm-wake", "helm-gate", "helm-stall", "helm-recheck",
                "helm-park-expiry", "helm-gave-up", "system"}


def day_bounds(d: dt.date):
    # The end is the next local midnight, not start + 24 h: a DST day is 23 or 25 hours long.
    start = dt.datetime.combine(d, dt.time()).timestamp()
    return start, dt.datetime.combine(d + dt.timedelta(days=1), dt.time()).timestamp()


def excerpt(s: str, n: int = EXCERPT) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def humanize(value: str) -> str:
    """An option id as words: snooze_again -> snooze again."""
    return " ".join(p for p in re.split(r"[_\-]+", value or "") if p)


def option_label(ask_options_raw, value: str):
    """(label, found). Helm clears a thread's options the moment it is
    answered, so for most past answers the wording is gone and the id is all
    there is; the caller says so."""
    try:
        opts = json.loads(ask_options_raw or "[]")
    except ValueError:
        opts = []
    for o in opts:
        if isinstance(o, dict) and o.get("value") == value:
            return o.get("label") or value, True
    return humanize(value), False


def settled_detail(body: str) -> str:
    m = SETTLED_CHOICE_RE.match(body.strip())
    if m:
        return m.group(2)
    return body.split(":", 1)[1].strip() if ":" in body else ""


def who(source: str) -> str:
    """The actor a source stands for: you, a chat session, Helm itself, a bot
    run (an old row's retired agent source too), or a producer by name. A web
    login (home) is you."""
    s = (source or "").strip()
    if s in ("", "owner"):
        return "you"
    if s == "session":
        return "session"
    if s == "bot":
        return "bot"
    if s in HELM_SOURCES:
        return "helm"
    return "producer"


def _person_label(source: str, you: str, session: str, other: str) -> str:
    w = who(source)
    if w == "you":
        return you
    if w == "session":
        return session
    if w == "bot":
        return "a run " + other
    return source + " " + other


def _fallback_state_change(base: dict, body: str, source: str):
    """the reference's English-body parsing, for a person's state-change rows, which
    carry no structured sub-kind. Kept as the fallback only."""
    low = body.lower()
    w = who(source)
    if low.startswith("write-back") or low.startswith("you wrote this card yourself"):
        return None
    # The settles. The source says who, the body only says how: a web login at
    # home settles as itself even though the store's wording says "session".
    if low.startswith("settled from a chat session") or low.startswith("closed from a chat session") \
            or low.startswith("settled by") or low.startswith("closed by"):
        session = w == "session" or (w not in ("you", "bot", "producer")
                                     and "chat session" in low)
        if session:
            return dict(base, kind="settled", label="a chat session closed it", actor="session",
                        detail=excerpt(settled_detail(body)))
        # An agent's own close: "closed by the agent", or an old row's retired wording.
        if w == "bot" or low.startswith("closed by the agent"):
            return dict(base, kind="settled", label="a run closed it", actor="bot",
                        detail=excerpt(settled_detail(body)))
        if "by acknowledging" in low:
            return dict(base, kind="settled", label="you acknowledged it", actor="you", detail="")
        return dict(base, kind="settled", actor="you",
                    label="you settled it" if low.startswith("settled") else "you closed it",
                    detail=excerpt(settled_detail(body)))
    if low.startswith("not settled on this tap"):
        return dict(base, kind="state", label="not settled on that tap", actor="you",
                    detail=excerpt(body.split(":", 1)[1] if ":" in body else body))
    if low.startswith("snoozed"):
        detail = body.split(":", 1)[1] if ":" in body else body[8:]
        return dict(base, kind="snoozed", label="you snoozed it", actor="you", detail=excerpt(detail))
    if low.startswith("reopened by you") or low.startswith("reopened"):
        return dict(base, kind="reopened", label="you reopened it", actor="you",
                    detail=excerpt(body.split(":", 1)[1] if ":" in body else ""))
    if low.startswith("muted"):
        return dict(base, kind="muted", label="you muted it", actor="you", detail=excerpt(body[6:]))
    if low.startswith("dispatch failed"):
        return dict(base, kind="dispatch", label="the run could not start", actor="bot",
                    detail=excerpt(body.split(":", 1)[1] if ":" in body else body))
    if low.startswith("retired") or low.startswith("auto-retired"):
        return dict(base, kind="retired", label="retired", actor="producer",
                    detail=excerpt(body.split(":", 1)[1] if ":" in body else body))
    if low.startswith("label "):
        return dict(base, kind="state", label="label changed", actor="helm", detail=excerpt(body))
    return dict(base, kind="state", label="state changed", actor=who(source) if source else "producer",
                detail=excerpt(body))


def timeline_entry(e: dict):
    """One event to one timeline line, or None for the noise (producer
    resyncs, write-back records, the card's own creation echo)."""
    kind, body, source = e["kind"], e["body"] or "", e["source"] or ""
    reply_kind = e.get("reply_kind") or ""
    base = {"ts": e["ts"], "thread_id": e["thread_id"], "title": e["title"],
            "category": e["category"], "state": e["state"], "urgency": e["urgency"]}
    if kind == "update":
        return None

    if kind == "owner-reply":
        v = body
        actor = "session" if who(source) == "session" else "you"
        if v == "__redirect__":
            label, detail = "you answered with a note", ""
        elif v == "__task__":
            label, detail = "you handed it over", ""
        elif v == "ack":
            label, detail = "you acknowledged it", ""
        else:
            detail, found = option_label(e.get("ask_options"), v)
            label = "a chat session answered" if actor == "session" else "you answered"
            return dict(base, kind="answered", label=label, detail=detail, raw=not found, actor=actor)
        if actor == "session":
            label = "a chat session answered"
        return dict(base, kind="answered", label=label, detail=detail, actor=actor)

    if kind == "owner-note":
        actor = "session" if who(source) == "session" else "you"
        label = "a chat session noted" if actor == "session" else "you noted"
        return dict(base, kind="noted", label=label, detail=excerpt(body), actor=actor)

    if kind == "note":
        w = who(source)
        if w == "you":
            return dict(base, kind="noted", label="you noted", detail=excerpt(body), actor="you")
        return dict(base, kind="detail", label=(source or "someone") + " added a note",
                    detail=excerpt(body), actor="producer" if w == "producer" else w)

    if kind == "bot-result":
        if source == "helm-wake":
            return dict(base, kind="woke", label="snooze brought it back", detail=excerpt(body), actor="bot")
        if source == "helm-park-expiry":
            return dict(base, kind="woke", label="the park's recheck date came", detail=excerpt(body),
                        actor="bot")
        if source == "helm-gave-up":
            return dict(base, kind="capped", label="handed back at the round cap",
                        detail=excerpt(body), actor="bot")
        return dict(base, kind="run", label="a run reported", detail=excerpt(body), actor="bot")

    if kind == "bot-ask":
        label = "the gate released a question" if source == "helm-gate" else "a run asked you"
        return dict(base, kind="asked", label=label, detail=excerpt(body), actor="bot")

    if kind == "instruction":
        return dict(base, kind="instruction", label="the stall router sent an instruction",
                    detail=excerpt(body), actor="helm")

    if kind == "state-change":
        # The structured signals first.
        if reply_kind == "close":
            # A close's source is whoever closed it: the owner, a web login (you), a chat
            # session, or a bot through its mechanical exception (an old row's retired agent too).
            reason = body.split(":", 1)[1].strip() if ":" in body else ""
            w = who(source)
            label, actor = {"session": ("a chat session closed it", "session"),
                            "bot": ("a run closed it", "bot")}.get(w, ("you closed it", "you"))
            return dict(base, kind="settled", actor=actor, label=label, detail=excerpt(reason))
        if source == "helm-gate":
            return dict(base, kind="gate", label="the gate held it", detail=excerpt(body), actor="helm")
        if source == "helm-recheck":
            return dict(base, kind="recheck", label="a recheck came due", detail=excerpt(body),
                        actor="helm")
        if who(source) == "bot":
            return dict(base, kind="run", label="the run asked to close it", detail=excerpt(body),
                        actor="bot")
        # A person's state-change carries no sub-kind: the English body decides.
        return _fallback_state_change(base, body, source)

    if kind in ("park", "unpark", "backlog", "unbacklog"):
        w = who(source)
        verb = {"park": "parked it", "unpark": "unparked it",
                "backlog": "put it in the backlog", "unbacklog": "pulled it back"}[kind]
        label = ("you " + verb) if w in ("you", "producer") else \
            ("a chat session " + verb if w == "session" else source + " " + verb)
        return dict(base, kind=kind, label=label, detail=excerpt(body),
                    actor="you" if w in ("you", "producer") else w)

    if kind == "owner-attachment":
        return dict(base, kind="attached", label="you attached a file", detail=excerpt(body), actor="you")
    if kind == "agent-attachment":
        return dict(base, kind="attached", label=(source or "a run") + " attached a file",
                    detail=excerpt(body), actor="bot")
    if kind == "lesson-candidate":
        return dict(base, kind="lesson", label="a lesson was proposed", detail=excerpt(body),
                    actor="you" if who(source) == "you" else "bot")

    noun = PRODUCER_NOUN.get(kind) or ("a " + kind.replace("-", " "))
    label = (source + " added " + noun) if source else (noun + " added")
    return dict(base, kind="detail", label=label, detail=excerpt(body), actor="producer")


def _part(e: dict) -> dict:
    return {"kind": e["kind"], "label": e["label"], "detail": e.get("detail") or "",
            "raw": bool(e.get("raw"))}


def _actor(e: dict) -> str:
    return e.get("actor") or ACTOR.get(e["kind"], "producer")


def merge_bursts(entries: list, gap: float = BURST_S) -> list:
    """One row per burst: the entries on one thread by one actor, each within
    `gap` seconds of the one before. The part that settles the most is the
    headline; the others ride along as `more`, so an answer, its note and the
    settle Helm records for one click read as one thing that happened."""
    by_thread: dict = {}
    for e in entries:
        by_thread.setdefault(e["thread_id"], []).append(e)
    rows = []
    for tid, evs in by_thread.items():
        evs.sort(key=lambda x: x["ts"])
        bursts, cur = [], None
        for e in evs:
            actor = _actor(e)
            if cur and cur["actor"] == actor and e["ts"] - cur["last_ts"] <= gap:
                cur["parts"].append(e)
                cur["last_ts"] = e["ts"]
            else:
                cur = {"actor": actor, "parts": [e], "last_ts": e["ts"]}
                bursts.append(cur)
        for b in bursts:
            parts = b["parts"]
            head = max(parts, key=lambda p: (RANK.get(p["kind"], 0), p["ts"]))
            more, seen = [], {head.get("detail") or ""}
            shown, labels = [], set()
            for p in parts:
                if p["label"] not in labels:
                    labels.add(p["label"])
                    shown.append(_part(p))
                if p is head:
                    continue
                d = p.get("detail") or ""
                # An answer whose wording Helm dropped says nothing the settle
                # line does not say better.
                if p.get("raw") and head["kind"] == "settled":
                    continue
                if d and d not in seen:
                    seen.add(d)
                    more.append(d)
            rows.append({
                "ts": parts[0]["ts"], "last_ts": b["last_ts"], "thread_id": tid,
                "thread_ids": [tid], "count": 1,
                "title": head["title"], "category": head["category"], "state": head["state"],
                "urgency": head["urgency"], "kind": head["kind"], "label": head["label"],
                "detail": head.get("detail") or "", "raw": bool(head.get("raw")),
                "actor": b["actor"], "more": more, "parts": shown,
            })
    rows.sort(key=lambda r: r["ts"])
    return rows


def fold_repeats(rows: list, gap: float = BURST_S) -> list:
    """Rows that say the same thing about threads with the same title, each
    within `gap` of the last, become one row with a count: eight identical
    failure cards closed in a sweep are one line, not eight."""
    out, last_by_key = [], {}
    for r in rows:
        key = (r["kind"], r["label"], r["title"])
        prev = last_by_key.get(key)
        if prev and r["ts"] - prev["last_ts"] <= gap:
            prev["count"] += 1
            prev["thread_ids"].append(r["thread_id"])
            prev["last_ts"] = r["last_ts"]
            continue
        last_by_key[key] = r
        out.append(r)
    for r in out:
        r.pop("last_ts", None)
    return out


def _filed_label(t: dict) -> str:
    """Who filed the card: the first event's source, structurally. A card whose
    first event the owner wrote (create_thread) is his; a producer's first event
    names the producer, else the sources column does. A bare-string source (older
    work cards) is the source's own id, not its producer, so it names no filer."""
    first_src = t.get("first_source") or ""
    if first_src == "owner" and t.get("first_kind") == "state-change" \
            and (t.get("first_body") or "").startswith("you wrote this card"):
        return "you wrote it"
    if (t.get("first_body") or "").startswith("you wrote this card"):
        return "you wrote it"
    src = first_src or next((s["source"] for s in t.get("sources") or []
                             if isinstance(s, dict) and s.get("source")), "")
    return ("filed by " + src) if src else "filed"


def build_timeline(hcon, start: float, end: float) -> list:
    out = []
    for t in helm_adapter.store_threads_first_seen_between(hcon, start, end):
        out.append({"ts": t["first_seen"], "thread_id": t["thread_id"], "title": t["title"],
                    "category": t["category"], "state": t["state"], "urgency": t["urgency"],
                    "kind": "filed", "label": _filed_label(t), "detail": "", "actor": "producer"})
    for e in helm_adapter.store_events_between(hcon, start, end):
        entry = timeline_entry(e)
        if entry:
            out.append(entry)
    return fold_repeats(merge_bursts(out))


def calls_of_day(hcon, start: float, end: float) -> list:
    """The day's call threads with their recording links (work profile: the
    "Calls" category). Empty where the profile has no such category."""
    prof = config.profile()
    if not prof.calls_category:
        return []
    out = []
    for t in helm_adapter.store_threads_first_seen_between(hcon, start, end):
        if (t["category"] or "").lower() != prof.calls_category.lower():
            continue
        recording = None
        for l in t["links"]:
            m = prof.recording_re.search(str((l or {}).get("url") or "")) if prof.recording_re else None
            if m:
                recording = {"url": l["url"], "n": int(m.group(1)), "label": l.get("label") or ""}
                break
        out.append({"ts": t["first_seen"], "thread_id": t["thread_id"], "title": t["title"],
                    "state": t["state"], "recording": recording})
    return out


def carry_over(ncon, d: dt.date, back_days: int = 7):
    """The most recent earlier daily note within a week, with its still-open
    checkbox lines. None when there is none or nothing is open."""
    for i in range(1, back_days + 1):
        prev = d - dt.timedelta(days=i)
        rel = notes.daily_rel(prev)
        if notes.exists(ncon, rel):
            open_tasks = [t for t in notes.tasks_for(ncon, rel) if not t["done"]]
            return {"date": prev.isoformat(), "rel": rel, "tasks": open_tasks}
    return None


def assemble(d: dt.date, ncon, hcon, render_fn, visible=lambda rel: True) -> dict:
    rel = notes.daily_rel(d)
    note = notes.get(ncon, rel)
    start, end = day_bounds(d)
    tasks = notes.tasks_for(ncon, rel) if note else []
    mentions = [l for l in notes.outlinks(ncon, rel) if l["rel"] and not l["embed"]] if note else []
    seen, uniq = set(), []
    for m in mentions:
        if m["rel"] not in seen:
            seen.add(m["rel"])
            uniq.append({"rel": m["rel"], "title": m["title"]})
    changed = [n for n in notes.changed_between(ncon, start, end)
               if n["rel"] != rel and visible(n["rel"])]
    return {
        "date": d.isoformat(),
        "rel": rel,
        "exists": note is not None,
        "editable": note is not None and not config.read_only(),
        "mtime": note["mtime"] if note else None,
        "html": render_fn(note["body"]) if note else "",
        "frontmatter": note["frontmatter"] if note else {},
        "tasks": tasks,
        "open_tasks": sum(1 for t in tasks if not t["done"]),
        "timeline": build_timeline(hcon, start, end),
        "calls": calls_of_day(hcon, start, end),
        "carry": carry_over(ncon, d),
        "mentions": uniq,
        "changed": changed,
        "today": d == dt.date.today(),
        "prev": (d - dt.timedelta(days=1)).isoformat(),
        "next": (d + dt.timedelta(days=1)).isoformat(),
    }
