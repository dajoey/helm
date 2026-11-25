"""The Topic hub: what the inbox holds about one note. the reference's app/topic.py.

A thread is joined to a note either explicitly, by a `note:` link or a link
whose path is the note's file, or (work profile only) by inference from
names, in which case the match is marked suggested so a wrong guess is
visible as one. The explicit join is checked for every note at both sites:
that is how a card and the page it cites find each other.
"""
from __future__ import annotations

import re

from . import config, helm_adapter, notes

MIN_KEY = 4
MAX_SETTLED = 40


def _norm_path(p: str) -> str:
    return p.replace("\\", "/").strip().lower().rstrip("/")


def note_link_target(url: str):
    """The notebook-relative path a link url points at, if it is a `note:` url
    or a path inside the notes folder or a folder it used to live in
    (HELM_NOTES_VAULT_ALIASES); else None."""
    u = (url or "").strip()
    if not u:
        return None
    if u.lower().startswith("note:"):
        rel = u[5:].strip().strip("/").replace("\\", "/")
        return rel if rel.lower().endswith(".md") else rel + ".md"
    n = _norm_path(u)
    for folder in [notes.VAULT] + config.vault_aliases():
        vault = _norm_path(folder) if folder else ""
        if vault and n.startswith(vault + "/"):
            return u.replace("\\", "/").strip()[len(vault) + 1:]
    return None


URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def ref_values(thread: dict) -> list:
    """The strings a thread names things by: each source's id (a bare string source is its
    own id) and each link's url, in order, without repeats."""
    vals = []
    for s in thread.get("sources") or []:
        vals.append(s.get("id") if isinstance(s, dict) else s)
    for l in thread.get("links") or []:
        vals.append(l.get("url") if isinstance(l, dict) else l)
    out = []
    for v in vals:
        v = str(v if v is not None else "").strip()
        if v and v not in out:
            out.append(v)
    return out


def refs(thread: dict, ncon, hcon, visible=lambda rel: True) -> dict:
    """What a thread's sources and links point at inside the app, keyed by the string as the
    thread holds it: {"note": rel} or {"thread": thread_id} (the owner 2026-09-26: "if it's a
    reference to something within the app, it should link to it"). A web address is left
    alone (it is a link already). A path or note: url is checked against the notebook, which
    answers with the note's own spelling, so a link whose case differs still opens it; another
    thread's id is a thread; else a note's exact name ("<minutes>.md", the meetings source's
    id) or notebook-relative path is that note, as a wikilink would resolve it. `visible`
    drops a note the notebook would not serve (the home fence)."""
    out = {}
    own = thread.get("thread_id")
    for v in ref_values(thread):
        if len(v) > 500 or URL_RE.match(v):
            continue
        tgt = note_link_target(v)
        if tgt:
            rel = notes.resolve_note(ncon, tgt)
            if rel and visible(rel):
                out[v] = {"note": rel}
            continue
        if v != own and helm_adapter.thread_exists(hcon, v):
            out[v] = {"thread": v}
            continue
        rel = notes.resolve_note(ncon, v)   # a name, or a notebook-relative path
        if rel and visible(rel):
            out[v] = {"note": rel}
    return out


def name_key(stem: str) -> str:
    """The contact's name with the usual suffixes stripped (work heuristic)."""
    prof = config.profile()
    if not prof.heuristics:
        return ""
    head = re.split(r"\s+-\s+|,\s+|:\s+", stem, maxsplit=1)[0].strip().lower()
    changed = True
    while changed:
        changed = False
        for suf in prof.name_suffixes:
            if head.endswith(" " + suf):
                head = head[: -len(suf) - 1].rstrip()
                changed = True
    return head if len(head) >= MIN_KEY else ""


def keys_for(rel: str, title: str) -> dict:
    stem = notes.stem_of(rel)
    prof = config.profile()
    cases = set()
    if prof.heuristics and prof.case_re:
        cases = set(prof.case_re.findall(stem)) | set(prof.case_re.findall(title or ""))
    return {"rel": rel, "stem": stem, "title": (title or "").strip(),
            "cases": cases, "name": name_key(stem)}


def _word_in(needle: str, hay: str) -> bool:
    if not needle or not hay:
        return False
    return re.search(r"(?<!\w)" + re.escape(needle.lower()) + r"(?!\w)", hay.lower()) is not None


def linked(thread: dict, rel: str) -> bool:
    """Does the thread carry a link to this note?"""
    rel_l = rel.lower()
    norel_l = rel_l[:-3] if rel_l.endswith(".md") else rel_l
    for l in thread.get("links") or []:
        tgt = note_link_target(str((l or {}).get("url") or ""))
        if tgt and tgt.lower() in (rel_l, norel_l, norel_l + ".md"):
            return True
    return False


def match(thread: dict, keys: dict):
    """('linked', why) or ('suggested', why) or None. Suggestions are the work
    profile's heuristics; the home profile only ever joins on a link."""
    if linked(thread, keys["rel"]):
        return ("linked", "links to this note")
    if not config.profile().heuristics:
        return None
    cat, title, summary = thread.get("category") or "", thread.get("title") or "", \
        thread.get("summary") or ""
    stem = keys["stem"]
    if len(stem) >= MIN_KEY and (_word_in(stem, cat) or _word_in(stem, title)):
        return ("suggested", "name appears in the category or title")
    if len(keys["title"]) >= MIN_KEY and keys["title"].lower() in summary.lower():
        return ("suggested", "note title appears in the summary")
    for c in keys["cases"]:
        if c in cat or c in title or c in summary:
            return ("suggested", "same case number " + c)
    if keys["name"] and (_word_in(keys["name"], cat) or _word_in(keys["name"], title)):
        return ("suggested", "'%s' appears in the category or title" % keys["name"])
    return None


def is_topic(rel: str) -> bool:
    return notes.folder_of(rel) in notes.topic_folders()


def context(rel: str, ncon, hcon, visible=lambda rel: True) -> dict:
    note = notes.get(ncon, rel)
    title = note["title"] if note else notes.stem_of(rel)
    keys = keys_for(rel, title)
    open_t, settled = [], []
    if is_topic(rel):
        # A topic note looks at every thread, so the suggestions can be made.
        candidates = helm_adapter.store_threads(hcon, with_events=False)
    else:
        # Any other note only needs the threads that cite it by link.
        candidates = helm_adapter.threads_citing(hcon, rel)
    for t in candidates:
        m = match(t, keys)
        if not m:
            continue
        t["match"] = m[0]
        t["match_why"] = m[1]
        (settled if t["state"] == "done" else open_t).append(t)
    open_t.sort(key=lambda t: (0 if t["match"] == "linked" else 1,
                               0 if t["state"] == "needs-you" else 1,
                               -(t["last_change"] or 0)))
    settled.sort(key=lambda t: -(t["last_change"] or 0))
    for t in open_t:
        t["events"] = helm_adapter._events_of(hcon, t["thread_id"])
    for t in open_t + settled[:MAX_SETTLED]:
        t["refs"] = refs(t, ncon, hcon, visible)
    needles = [keys["stem"], keys["title"]]
    if keys["name"]:
        needles.append(keys["name"])
    mentions = [m for m in notes.daily_mentions(ncon, needles) if m["rel"] != rel and visible(m["rel"])]
    return {
        "rel": rel,
        "title": title,
        "topic": is_topic(rel),
        "keys": {"stem": keys["stem"], "name": keys["name"], "cases": sorted(keys["cases"])},
        "open": open_t,
        "settled": settled[:MAX_SETTLED],
        "settled_total": len(settled),
        "backlinks": notes.backlinks(ncon, rel, visible),
        "daily_mentions": mentions,
    }
