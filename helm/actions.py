"""the owner's actions on a thread, by name, for every door that offers them.

The API role (`app/service.py`), the notebook's inbox routes (`app/notebook_api.py`) and
the web role's forwarded user all end up here, so "reply" means the same thing whichever
screen it came from. Each action is one store call; the store keeps every invariant.

    act(con, thread_id, "reply", {"value": "ok", "note": "..."}, actor="owner")
      -> (202, {...store result...})   or raises store.StoreError

`actor` is who is acting: the authenticated web user (forwarded by the web role as the
X-Helm-Actor header), or None when a bearer-only caller did not say. `caller` is that
bearer's own name (door_actor). With HELM_BEARER_ACTOR_STRICT on, a bearer-only caller is
recorded as itself, never as the owner; off (the default) a missing actor keeps the store's site
default, because a mobile app taps with the shared bearer alone. Without `caller` (the
notebook inbox) the store decides what a missing actor means per site (home: owner; work: a
chat session).
"""
from __future__ import annotations

import os

from helm import store

# Body keys each action reads. Anything else a page sends is dropped here, not forwarded,
# exactly as the original client's ACTIONS did.
ACTIONS = {
    "reply": ("value", "note", "attachments", "by", "actor"),
    "close": ("reason", "force", "by", "actor"),
    "reopen": ("note",),
    "mute": ("note", "force"),
    "snooze": ("wake_at", "wake_at_raw", "hours", "note"),
    "wake": (),
    "park": ("reason", "source"),
    "unpark": ("note", "source"),
    "backlog": ("reason", "source"),
    "unbacklog": ("note", "reason", "source"),
}


def _actor_of(body: dict, actor):
    """`by` (work's name) and `actor` (home's) are the same field; an explicit body value
    wins over the forwarded header, so a page can still say "this was the owner's own tap"."""
    for key in ("by", "actor"):
        v = body.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return actor


def strict_principals() -> set:
    """HELM_BEARER_STRICT_PRINCIPALS: comma-separated per-caller principals that get the
    strict rule even while HELM_BEARER_ACTOR_STRICT is off (e.g. `relay-mcp`, Relay,
    which answers on the owner's word and must never read as the owner's own tap). Under
    the session-default rule only these are strict (a named connector principal). One
    reader: helm.store.strict_principals."""
    return store.strict_principals()


def bearer_strict(caller=None) -> bool:
    """HELM_BEARER_ACTOR_STRICT: a bearer-only caller is never recorded as the owner (home).
    Off by default; turn it on once the owner's own clients carry a per-caller token whose
    principal is owner (docs/RUNBOOK.md, Tokens). A `caller` named in
    HELM_BEARER_STRICT_PRINCIPALS is strict whatever the switch says. Read per call so a
    restart is enough."""
    if os.environ.get("HELM_BEARER_ACTOR_STRICT", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool(caller) and str(caller) in strict_principals()


def door_actor(explicit, login, caller):
    """Who a bearer's reply/close/park/... is recorded as, at the HTTP doors.

    `explicit` is the body's `by`/`actor`, `login` the web role's forwarded login (already
    mapped to owner), `caller` the bearer's own name (the per-caller token's principal, else
    "api" for the shared site token; None where a door has no bearer, e.g. the notebook
    inbox). Returns (name or None, substituted): `substituted` is True when the caller's
    name stands in for a missing or the owner claim.

    Default (strict off): `explicit or login`, and the store applies the site default, so
    the mobile app's bare-bearer reply and close stay the owner's. Strict on, at home: a
    signed-in login is the owner's own screen and keeps its say; a bearer-only caller that names
    nobody, or names owner, is recorded as itself, since nothing verifies that claim and the
    shared token has many holders. A per-caller token whose principal is owner still records
    as owner. Work Helm keeps its rule (only an explicit owner is the owner, everything else a
    session) whatever the switch says, except for a listed strict principal: it names itself
    (the store files that as a session) and door_source records its principal."""
    explicit = str(explicit).strip() if explicit is not None else ""
    if not strict_caller(login, caller):
        return (explicit or login), False
    if not explicit or explicit.lower() == store.BY_OWNER:
        return caller, True
    return explicit, False


def strict_caller(login, caller) -> bool:
    """True when the strict rule applies to this request: a bearer-only caller (no signed-in
    login) that is strict. Under the owner default that is bearer_strict (the switch or the
    list); under the session-default rule only a principal listed in
    HELM_BEARER_STRICT_PRINCIPALS is strict, so every other caller keeps the session
    default (only an explicit owner is the owner, anything else a session). The one test
    door_actor and door_source share."""
    if caller is None or login:
        return False
    if store.rules().by_default_session:
        return str(caller) in strict_principals()
    return bearer_strict(caller)


def door_source(login, caller, who):
    """The event source of a strict caller's reply, or None for the store's usual rule.

    A strict caller's name for itself is unverified (`by: "bot"` or `actor: "Relay"` can be
    any client's claim), so its answer is recorded under its own token principal: never "owner"
    (the store's home default) unless the caller is the owner principal. None where the strict
    rule does not apply, or `who` is the owner's own."""
    if not strict_caller(login, caller) or str(who or "").strip().lower() == store.BY_OWNER:
        return None
    return str(caller)


def act(con, thread_id: str, action: str, body: dict | None, actor=None, caller=None):
    """Run one named action. Returns (http_status, payload)."""
    body = body if isinstance(body, dict) else {}
    if action not in ACTIONS:
        raise store.StoreError(404, "no such action: %s" % action)
    b = {k: body[k] for k in ACTIONS[action] if k in body}
    # The HTTP door: a missing actor means the rule set's default (the owner, or a
    # chat session under the session-default rule), decided here so the store's own
    # default stays the owner.
    given = door_actor(_actor_of(b, None), actor, caller)[0]
    who = store.normalize_by(given)
    if action == "reply":
        atts = b.get("attachments") if isinstance(b.get("attachments"), list) else None
        return 202, store.reply(con, thread_id, b.get("value"), note=b.get("note") or "",
                                attachments=atts, by=who, source=door_source(actor, caller, who))
    if action == "close":
        return 202, store.close_thread(con, thread_id, reason=b.get("reason") or "",
                                       actor=who, force=bool(b.get("force")))
    if action == "reopen":
        return 202, store.reopen_thread(con, thread_id, note=b.get("note") or "")
    if action == "mute":
        return 202, store.mute(con, thread_id, note=b.get("note") or "",
                               force=bool(b.get("force")))
    if action == "snooze":
        return 202, store.snooze(con, thread_id, wake_at_raw=b.get("wake_at_raw"),
                                 wake_at=b.get("wake_at"), hours=b.get("hours"),
                                 note=b.get("note") or "")
    if action == "wake":
        return 200, store.wake_now(con, thread_id)
    if action == "park":
        return 202, store.set_parked(con, thread_id, True, b.get("reason") or "",
                                     b.get("source") or who or "owner")
    if action == "unpark":
        return 202, store.set_parked(con, thread_id, False, b.get("note") or "",
                                     b.get("source") or who or "owner")
    if action == "backlog":
        return 202, store.set_backlog(con, thread_id, True, b.get("reason") or "",
                                      b.get("source") or who or "owner")
    if action == "unbacklog":
        return 202, store.set_backlog(con, thread_id, False,
                                      b.get("note") or b.get("reason") or "",
                                      b.get("source") or who or "owner")
    raise store.StoreError(404, "no such action: %s" % action)  # pragma: no cover


def create(con, body: dict | None, actor=None, caller=None):
    """A hand-written card (POST /threads/create and /threads/new share this).

    `caller` is the bearer caller's name (helm.store.handoff_source), passed on so a
    hand-off made through here is recorded as the caller's, not the owner's."""
    body = body if isinstance(body, dict) else {}
    # The same door rule as act(): a caller that names nobody gets the site default,
    # so a work hand-off card's instruction is filed as the session's, not the owner's.
    who = _actor_of(body, actor)
    # At home that default is the owner, which would hide the caller from handoff_source:
    # a caller that named nobody goes through unnamed, as the HTTP door does.
    if caller and not who and not store.rules().by_default_session:
        return 201, store.create_thread(con, body, actor=None, caller=caller)
    return 201, store.create_thread(con, body, actor=store.normalize_by(who), caller=caller)
