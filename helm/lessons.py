"""The lesson ledger's caller-side helpers (contract: docs/contracts/P3c.md §4).

These lived in the dispatcher (dispatcher/tick.py) until it was retired on
2026-09-14 and removed from this repo on 2026-09-25. They are not store code:
the store owns the ledger and the adoption gate; these are what a caller does
with them. Kept here so any executor or pump can use them, and so the learning
suite can prove the ledger end to end without a dispatcher.

- render_lessons: the numbered STANDING RULES block an executor prompt carries.
- is_lesson_item: a pending-dispatch item that is a lesson thread.
- apply_lesson_tap: apply his adopt / adopt-with-edit tap through the store's
  adoption gate, or re-present the four options honestly when the gate refuses.
"""
from __future__ import annotations

LESSONS_HEADER = "STANDING RULES (lessons he adopted; they bind this run):"
MAX_RENDERED_RULE = 300
LESSON_SOURCE = "learning"     # same value as store.LESSON_SOURCE


def render_lessons(rules) -> str:
    """Active lessons as a numbered block. Pure formatter: the deterministic
    order comes from the store read; each rule is clipped to 300 chars; empty
    in gives empty out (a scope with no lessons renders nothing)."""
    lines = []
    for i, r in enumerate(rules or [], 1):
        text = " ".join(str((r or {}).get("rule_text") or "").split())[:MAX_RENDERED_RULE]
        if not text:
            continue
        lines.append("%d. %s" % (i, text))
    if not lines:
        return ""
    return LESSONS_HEADER + "\n" + "\n".join(lines) + "\n\n"


def is_lesson_item(item: dict) -> bool:
    """A pending item is a lesson thread when its sources carry
    source='learning'. pending_dispatch passes sources through, so this is
    pure data, no extra read."""
    return any(isinstance(s, dict) and s.get("source") == LESSON_SOURCE
               for s in ((item or {}).get("sources") or []))


def _clip(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def apply_lesson_tap(post, thread_id: str, responder: str = "helm-learning") -> dict:
    """Apply his adoption tap deterministically (contract P3c §4).

    A lesson thread in the dispatch queue means he tapped adopt or
    adopt-with-edit (the terminal taps settle in the store and never queue).
    No executor is needed: the caller posts the tap to the store's adoption
    gate, which refuses anything without his tap. When the gate refuses (for
    example adopt-with-edit with no note), the thread is reposted as a
    needs-you choice carrying the same four options and the refusal detail:
    never a bare card, never a retry loop.

    `post(path, payload)` returns `(status_code, body_dict)`; any HTTP client
    fits (the tests pass a loopback or TestClient wrapper).
    """
    from helm import store  # the four options live with the gate that checks them

    code, body = post("/api/triage/lessons/from-thread/%s" % thread_id, {})
    if code == 202:
        return {"thread_id": thread_id, "ok": True, "posted": True, "lesson": body}
    detail = _clip((body or {}).get("detail") or "", 300)
    code2, _ = post("/api/triage/threads/%s/result" % thread_id, {
        "outcome": "lesson-adoption-refused",
        "state": "needs-you",
        "note": ("His adoption tap could not be applied: %s. The four options "
                 "are re-presented; send the missing wording as a note with "
                 "the adopt-with-edit tap." % detail),
        "responder": responder,
        "ask": {"kind": "choice",
                "prompt": ("The adoption tap was refused by the store gate. "
                           "Pick how to proceed with this lesson candidate."),
                "options": store._lesson_options(),
                "requires": ["decision"]},
    })
    return {"thread_id": thread_id, "ok": False, "posted": code2 in (202, 409),
            "refused": detail}
