"""Helm triage store: threads, events, categories, suppressions.

Pure store logic and validation. No HTTP, no model call, no tracker. Everything
here is deterministic and testable against a throwaway SQLite file, which is
the point: when the list is wrong you need to know whether to look at the
triage pass or at the store, and only one of those two can be tested offline.

The rule the whole design serves: a card must never lie about its state.

Five invariants enforce it, each written after a specific failure:

  1. PUT is an upsert. It removes only what the caller explicitly names in
     `tombstones`. A pass that crashes halfway must not delete answered work.
  2. A thread that reached `done` is never dragged back by a PUT. The triage
     pass re-derives from the same cards every run and has no idea what was
     already dealt with.
  3. An answered question is never re-armed underneath the user. A later PUT
     may refresh the wording; it may not rewrite an ask that was already
     tapped.
  4. A lit thread carries BOTH a report and a question. `needs-you` with no
     ask renders as a bare Close button over finished work, and a bot result
     with no prose note renders as a single status word.
  5. Acking an `ack` ask settles it on the tap. An ack exists for exactly
     "reading it is genuinely the whole job", so there is nothing left for an
     executor to do with one; before this it dispatched anyway, and since a
     bot may never mark a thread done, the executor could only hand the same
     ack straight back. That looped forever until the person tapped the
     unrelated Close button, which every "Got it" tap looked like it should
     have already done.

Validation is hand-written rather than declared as pydantic models on purpose.
Every rejection has to be readable by the model that produced the payload and
specific enough for it to fix itself on the retry, which a terse type error is
not.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
import zoneinfo

from helm import tokens as _tokens

DB_PATH = os.environ.get("HELM_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "triage.db")

from helm import site as _site
from helm import card_rules as _card_rules
from helm import cold_read as _cold_read

URGENCY_ORDER = {"now": 0, "this-week": 1, "whenever": 2}
VALID_URGENCY = set(URGENCY_ORDER)
# "backlog" (the earlier app, 2026-09-19) is what needs attention but not now: nothing is
# pushing it, nobody owes an answer, and it comes back only when pulled back by hand.
# Valid at both sites (one superset of states); list_threads hides it unless asked.
VALID_STATE = {"needs-you", "working", "waiting-external", "backlog", "done"}
VALID_ASK = {"none", "ack", "choice", "text"}

# Who a close or an answer came from (the earlier app, 2026-09-24): the owner's own tap, or a chat
# session acting on his word. At work only an explicit "owner" is the owner; at home a missing
# actor is the owner, because every home client is one of his own screens.
BY_OWNER = _site.OWNER
BY_SESSION = "session"
# A bearer-only caller with no per-caller token (the site's shared HELM_APP_TOKEN).
BY_API = "api"
HANDOFF_SOURCE_MAX = 64
_HANDOFF_SOURCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}")


def rules():
    """The site's rule flags (helm/site.py). Read per call so tests can switch sites."""
    return _site.rules()


def normalize_by(by):
    """Who acted, per site. Work: only an explicit 'owner' is the owner, anything else is a
    session. Home: a missing value is the owner; a named actor is kept as given (bot, a
    web user's login) so the home close rules can still tell them apart."""
    s = str(by or "").strip()
    if rules().by_default_session:
        return BY_OWNER if s.lower() == BY_OWNER else BY_SESSION
    if not s:
        return BY_OWNER
    return s


def strict_principals() -> set:
    """HELM_BEARER_STRICT_PRINCIPALS: per-caller principals always recorded under their own name
    (helm.actions.strict_caller). Read per call so a restart is enough."""
    raw = os.environ.get("HELM_BEARER_STRICT_PRINCIPALS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def handoff_source(given, caller):
    """The event source of a hand-off (`__task__`) at home, or None for the usual rule.

    A missing actor is the owner at home because his phone taps with the bearer only, but a
    hand-off is never a tap: his phone never sends one and his web sheet carries the
    login (X-Helm-Actor). So a hand-off from a caller that did not say it was the owner is
    the caller's own: a named actor as itself, a bearer-only one as its token's
    principal (`caller`, else BY_API). Written as the owner's, a watcher's brief reached
    the agent as "the owner's Note" (2026-09-27). `caller` is None on a direct store call, which
    stays the owner's own tap; work keeps its session rule. A caller listed in
    HELM_BEARER_STRICT_PRINCIPALS is its own, whatever it claims: a connector's
    connector (`relay`) files hand-offs the client must be able to tell from the owner's."""
    if caller and str(caller) in strict_principals():
        return str(caller)
    if rules().by_default_session:
        return None
    s = str(given or "").strip()
    if s.lower() == BY_OWNER:
        return None
    if s:
        # A named actor is held to a token principal's rule (helm.tokens): 2-64 of
        # [A-Za-z0-9._-], clipped to length first. Anything else is the caller's own.
        s = s[:HANDOFF_SOURCE_MAX]
        if _HANDOFF_SOURCE_RE.fullmatch(s):
            return s
        return str(caller) if caller else BY_API
    return str(caller) if caller else None


def classify_reply(value, note="", attachments=None):
    """The learning classifier's reply_kind, or "" where the module is off.
    Imported lazily so a site that never learns never loads it."""
    if not rules().learning_classifier:
        return ""
    from learning import classify as clf
    return clf.classify_reply(value, note=note, attachments=attachments)


# --- Use the draft ------------------------------------------------------------------
# A report may carry a checked draft (a `use_draft.py check` object, SPEC 3.2): a mail reply, a
# ticket comment, a wiki page, a CRM case note or a chat message, with a button
# label. Helm stores it on the report event and shows the button while that report is the newest and
# no press has used it. The press is an ordinary reply of `__use_draft__:<sha>`, by the owner: the
# dispatcher turns it into one `use_draft.py apply`, with no model run. Helm never writes the draft
# anywhere itself; it only keeps the object, shows it and records the tap.
USE_DRAFT_PREFIX = "__use_draft__:"
# The marker a press carries. The press is the owner's only approval of a write to an outside system
# (a CRM, a chat, a ticket tracker, a wiki or a mailbox), and every bearer holder can send `by: owner`
# and any `X-Helm-Actor`, so neither proves who pressed. What does: the web role (behind Authentik) is
# the only caller that also holds HELM_WEB_PRESS_KEY, sends it as `X-Helm-Press-Key`, and the api then
# stores the event with `via` = WEB_UI and `via_login` = the signed-in user. Nothing else writes `via`.
WEB_UI = "web-ui"


def is_press_value(value) -> bool:
    """True for any text that reads as a Use-the-draft press, however it is cased or padded."""
    return isinstance(value, str) and value.lstrip().lower().startswith(USE_DRAFT_PREFIX)
DRAFT_DESTS = ("mail", "tickets", "wiki", "chat")
MAX_DRAFT_TEXT = 20000
MAX_DRAFT_DETAIL = 8000
MAX_DRAFT_SPEC = 60000
MAX_DRAFT_FIELDS = 20
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def draft_sha(dest, spec) -> str:
    """sha256 of the canonical dest and spec, as SPEC 3.2 defines it: anyone can recompute it."""
    return hashlib.sha256(json.dumps({"dest": dest, "spec": spec}, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def normalize_draft(d):
    """-> (clean checked draft, errors). Keeps the known fields only; the sha must be the spec's own."""
    if not isinstance(d, dict):
        return None, ["draft: is %s, expected an object (the checked draft from use_draft.py check)" % type(d).__name__]
    errs = []

    def text(v, name, mx, required=False):
        if v is None or v == "":
            if required:
                errs.append("draft: %s is required" % name)
            return ""
        if not isinstance(v, str):
            errs.append("draft: %s must be text" % name)
            return ""
        v = _scrub_surrogates(v)   # display text only: the spec stays byte-exact, the sha covers it
        if len(v) > mx:
            errs.append("draft: %s is %d chars, max %d" % (name, len(v), mx))
        return v

    if d.get("version") != 1:
        errs.append("draft: version must be 1 (got %r)" % (d.get("version"),))
    dest = d.get("dest")
    if dest not in DRAFT_DESTS:
        errs.append("draft: dest %r is not one of %s" % (dest, list(DRAFT_DESTS)))
    spec = d.get("spec")
    if not isinstance(spec, dict) or not spec:
        errs.append("draft: spec must be a non-empty object")
        spec = None
    else:
        try:
            # allow_nan=False: a NaN or Infinity in the spec would be stored, then fail every response that holds it
            too_long = len(json.dumps(spec, ensure_ascii=False, allow_nan=False)) > MAX_DRAFT_SPEC
        except (ValueError, TypeError, RecursionError):
            errs.append("draft: spec holds a value JSON cannot carry (NaN or Infinity)")
            spec, too_long = None, False
        if too_long:
            errs.append("draft: spec is over %d chars" % MAX_DRAFT_SPEC)
    sha = d.get("sha")
    if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
        errs.append("draft: sha must be 64 lowercase hex characters")
    elif spec is not None and dest in DRAFT_DESTS:
        try:
            sha_ok = sha == draft_sha(dest, spec)
        except UnicodeEncodeError:   # a lone surrogate: no UTF-8 form to hash
            errs.append("draft: spec holds text that is not valid Unicode (a lone surrogate)")
            sha_ok = True
        if not sha_ok:
            errs.append("draft: sha is not the sha256 of this dest and spec, so the button would not mean the text on it")
    button = text(d.get("button"), "button", MAX_ASK_LABEL + 20, required=True)
    checked_at = text(d.get("checked_at"), "checked_at", 64)
    view = d.get("view")
    clean_view = {}
    if not isinstance(view, dict):
        errs.append("draft: view must be an object")
    else:
        clean_view["title"] = text(view.get("title"), "view.title", 300, required=True)
        clean_view["text"] = text(view.get("text"), "view.text", MAX_DRAFT_TEXT, required=True)
        clean_view["detail"] = text(view.get("detail"), "view.detail", MAX_DRAFT_DETAIL)
        url = text(view.get("url"), "view.url", MAX_LINK_FIELD)
        if url and not re.match(r"https?://", url, re.I):
            errs.append("draft: view.url must be an http(s) link")
            url = ""
        clean_view["url"] = url
        fields = view.get("fields", [])
        clean_fields = []
        if not isinstance(fields, list) or len(fields) > MAX_DRAFT_FIELDS:
            errs.append("draft: view.fields must be a list of at most %d" % MAX_DRAFT_FIELDS)
        else:
            for f in fields:
                if not isinstance(f, dict) or not isinstance(f.get("label"), str) or not isinstance(f.get("value"), str):
                    errs.append("draft: each view field is {label, value} text")
                    break
                clean_fields.append({"label": text(f["label"], "field label", 80),
                                     "value": text(f["value"], "field value", 1000)})
        clean_view["fields"] = clean_fields
    if errs:
        return None, errs
    return {"version": 1, "dest": dest, "spec": spec, "sha": sha, "button": button,
            "view": {"title": clean_view["title"], "fields": clean_view["fields"], "text": clean_view["text"],
                     "detail": clean_view["detail"], "url": clean_view["url"]},
            "checked_at": checked_at}, []


def newest_report_draft(con, thread_id):
    """-> (draft or None, used). The newest report (kind bot-result, any source) when it carries a
    draft, and whether a later press of the owner's already used it. A newer report with no draft
    replaces it, so there is nothing to use."""
    # Events Helm wrote itself (a wake, a park expiry, a give-up) are not a run's report: they neither
    # replace the draft nor take its button away.
    r = con.execute("SELECT ts, draft FROM events WHERE thread_id=? AND kind='bot-result' AND source NOT IN (%s)"
                    " ORDER BY ts DESC, rowid DESC LIMIT 1" % ",".join("?" * len(_BOARD_SOURCES)),
                    (thread_id,) + _BOARD_SOURCES).fetchone()
    if not r or not r["draft"]:
        return None, False
    try:
        d = json.loads(r["draft"])
    except ValueError:
        return None, False
    # Any press event counts as used, marked or not: a press made before the marker existed must not
    # bring the button back (a second write), and an unmarked one cannot make a write anyway because the
    # reader requires `via`. At worst a stray event hides a button.
    used = con.execute("SELECT 1 FROM events WHERE thread_id=? AND kind='owner-reply' AND source=? AND body=?"
                       " AND ts>=? LIMIT 1",
                       (thread_id, BY_OWNER, USE_DRAFT_PREFIX + str(d.get("sha")), r["ts"])).fetchone()
    return d, used is not None


def draft_summary(d):
    """What a list row carries of a draft: enough to know there is one and which (sha, where, button),
    not the spec or the text. The thread GET carries the whole draft."""
    if not isinstance(d, dict):
        return d
    return {"version": d.get("version"), "dest": d.get("dest"), "sha": d.get("sha"), "button": d.get("button"),
            "checked_at": d.get("checked_at"), "view": {"title": (d.get("view") or {}).get("title", "")}}


def use_draft_of(con, thread_id, state=None):
    """The draft the card offers a button for: the newest report's, while no press has used it and the
    card is not settled (a press on a settled card would reopen it)."""
    if state is None:
        r = con.execute("SELECT state FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        state = r["state"] if r else None
    if state == "done":
        return None
    d, used = newest_report_draft(con, thread_id)
    return None if used else d


def _require_press_proof(value, by, press):
    """The refusals that need no database: proof, caller, shape. Run before the write lock is taken."""
    if not (isinstance(press, dict) and press.get("via") == WEB_UI and str(press.get("login") or "").strip()):
        raise StoreError(403, "a Use the draft press comes only from the owner's signed-in page, through its confirm "
                              "sheet: the press is his approval of the write, and a script or a chat session "
                              "cannot give it for him, whatever token it holds")
    if by != BY_OWNER:
        raise StoreError(403, "only the owner's own tap uses a draft: the press is his approval of the write, "
                              "and a chat session cannot give it for him")
    if not value.startswith(USE_DRAFT_PREFIX):
        raise StoreError(400, "a press is exactly %s<sha> with nothing before it" % USE_DRAFT_PREFIX)


def _refuse_bad_use_draft(con, thread_id, value, by, press, row_state=None):
    """A `__use_draft__:<sha>` reply is the owner's approval of one write: the owner's own tap on the signed-in
    page, on the draft the card shows right now, once. `press` is the proof the api role checked
    (`{"via": WEB_UI, "login": ...}`) and is None for every other caller, whatever `by` says."""
    _require_press_proof(value, by, press)
    sha = value[len(USE_DRAFT_PREFIX):]
    if row_state == "done":
        raise StoreError(409, "this card is settled, so its draft is no longer offered. Reload the card")
    d, used = newest_report_draft(con, thread_id)
    if d is None:
        raise StoreError(409, "this thread has no draft to use: the newest report carries none. Reload the card")
    if used:
        raise StoreError(409, "this draft was already used: one press, one attempt. Reload the card")
    if d.get("sha") != sha:
        raise StoreError(409, "that is not the draft on the card now (a newer report replaced it). Reload the card")


# --- Unsent drafts (work rule pack, 2026-09-24) -------------------------------------
# A run's report that holds a draft nobody has sent yet. A chat session that settles
# such a thread on the owner's word has to say where the draft went; otherwise the draft is
# lost in the same motion that closed the card.
_DRAFT_WORD = re.compile(r"\bdraft(?:s|ed)?\b", re.I)
_UNSENT = re.compile(
    r"\bnot (?:yet )?(?:been )?(?:sent|posted|pasted)\b"
    r"|\bnothing (?:was |has been |got )?(?:sent|posted|pasted)\b"
    r"|\bnever (?:been |got |was )?(?:sent|posted|pasted)\b"
    r"|\bunsent\b|\bunposted\b|\bready to (?:send|paste|post)\b|\bsay the word\b"
    r"|\bowner pastes\b|\b(?:for you|waiting on you) to (?:send|paste|post)\b"
    r"|\bpaste (?:it|this|the text)\b", re.I)
# A report that says it drafted nothing ("Nothing was sent, posted, drafted, written or changed",
# "not drafted", "no draft") holds no draft. These phrases are removed before the draft word is
# looked for. The list form needs the draft word to end its list item (a comma, "or"/"and", a stop
# or the end): a ':' or ';' can introduce the draft text itself, so it does not end the item and
# "Nothing was sent, draft:\n<text>" still reads as a draft.
_NO_DRAFT = re.compile(
    r"\bnothing\s+(?:(?:was|is|has\s+been|got|gets|were)\s+)?"
    r"(?:[a-z]+(?:\s*,\s*(?:or\s+|and\s+)?|\s+(?:or|and)\s+))*"
    r"drafts?(?:ed)?(?=\s*(?:,|\.|\)|$|\s(?:or|and)\b))"
    r"|\b(?:not|never|no|without)\s+(?:yet\s+)?(?:(?:been|any|a|an)\s+)?drafts?(?:ed)?\b"
    r"|\bno\s+drafts?\s+(?:was|were|has\s+been|have\s+been)\b"
    r"|\b(?:did\s+not|didn'?t|do\s+not|don'?t|have\s+not|haven'?t|has\s+not|hasn'?t)\s+"
    r"(?:\w+\s+)?draft(?:ed)?\b", re.I)
# Phrases that deny delivery, removed before looking for a disposition.
_NEGATED = re.compile(
    r"\b(?:nothing|not|never)\s+(?:yet\s+)?(?:was\s+|been\s+|has\s+been\s+|got\s+)?"
    r"(?:sent|posted|pasted|filed|published|emailed|delivered|submitted)\b"
    r"|\bunsent\b|\bunposted\b", re.I)
# Words that say where a draft went, or that it is not going anywhere.
_DISPOSED = re.compile(
    r"\b(?:sent|posted|pasted|filed|published|pushed|applied|emailed|replied|submitted|delivered|"
    r"dropp?ed|drop it|won'?t send|not sending|decided not to|by choice|no longer needed|"
    r"not needed|superseded|moot|obsolete|abandon(?:ed)?|discard(?:ed)?)\b", re.I)


def unsent_draft(con, thread_id):
    """The newest run report on the thread, when it holds an unsent draft."""
    r = con.execute("SELECT body FROM events WHERE thread_id=? AND kind IN ('bot-result','draft')"
                    " ORDER BY ts DESC LIMIT 1", (thread_id,)).fetchone()
    body = (r["body"] or "") if r else ""
    if r and _DRAFT_WORD.search(_NO_DRAFT.sub(" ", body)) and _UNSENT.search(body):
        return r["body"]
    return None


def says_where_it_went(text):
    """True when the text says a draft was sent, posted, dropped or made moot."""
    return bool(_DISPOSED.search(_NEGATED.sub(" ", text or "")))


def _refuse_unsent_settle(con, thread_id, by, text):
    """A chat session may not settle a thread whose last report is an unsent
    draft unless its reason says where the draft went. Off at home."""
    if not rules().unsent_draft_409:
        return
    if by == BY_OWNER or not unsent_draft(con, thread_id) or says_where_it_went(text):
        return
    raise StoreError(409, "this thread's last report holds a draft that was never sent, and "
                          "the reason given does not say where it went. Leave the thread for "
                          "the owner, or close it with a reason that names what happened to the "
                          "draft: sent, posted, pasted, dropped, superseded or moot. 'Handed "
                          "over in chat' is not one of those")

MIN_RESULT_NOTE = 80
MAX_NOTE = 8000
MAX_SNOOZE_S = 400 * 86400
# How long an undated wait stays untouched before it comes back to be asked about.
DEFAULT_WAIT_S = 7 * 86400
MAX_LINKS = 12
MAX_LINK_FIELD = 800
MAX_TITLE = 300
MAX_ASK_PROMPT = 1000
MAX_ASK_LABEL = 60
MAX_ASK_VALUE = 40
MAX_ASK_EFFECT = 200

# A card written by hand, from the app, rather than derived by a producer.
# The prefix is on the thread_id so the two can never be confused: a producer
# owns the ids it mints and may tombstone them, and nothing it sends will ever
# name one of these.
HAND_ID_PREFIX = "owner-"
# The reply value a hand-written card carries when it is handed straight to an
# executor. Deliberately not "__redirect__": nothing was redirected, no options
# were ever offered, and the run is told so plainly instead of being handed a
# rejection with nothing to reject.
HAND_TASK_VALUE = "__task__"
# The reply value of a recheck: a run's snooze has come due (work rule pack). It is offered
# through pending-dispatch like an answer, but nobody answered: there is no owner-reply event.
RECHECK_VALUE = "__recheck__"
WAKE_SOURCE = "helm-wake"
# A parked thread's recheck came due and passed (apply_recheck): same hand-off as a wake.
RECHECK_SOURCE = "helm-recheck"
# What is meant to happen next, which is the only thing a hand-written card
# has to decide. It sets state and ask together, because those two are what
# the store's own invariants are about, and letting a form post them
# separately is how you get a lit card with no question.
VALID_INTENT = {"mine", "agent", "waiting"}

# Durable-queue behaviour. A dispatched run that never reports is re-offered
# after RESULT_TIMEOUT_S; retries back off exponentially and then give up
# LOUDLY, because a silent infinite retry and a working queue look identical.
MAX_DISPATCH_ATTEMPTS = 5
RESULT_TIMEOUT_S = 30 * 60
DISPATCH_BACKOFF_S = 60
DISPATCH_BACKOFF_MAX_S = 15 * 60

# --- The park contract (contract: P1 §1; LESSONS 4, 5, 6, 10) ----------------
#
# `waiting-external` used to be a hole in the state machine rather than a state.
# Neither queue can see a parked thread, so it was invisible to every agent
# while rendering to the owner as "waiting on someone else" with no button; on
# one live board twelve of fourteen visible cards sat in it simultaneously.
# Bots reached it honestly: forbidden to close a thread and (rightly) blocked
# from a lit card with no ask, parking was the only accepted state left, and
# they wrote notes promising "his final close tap" that the state never offers.
#
# So a park is now a claim with an owner and an expiry. `blocked_on` says WHO
# or WHAT is being waited on; `recheck_at` says when the system looks again, on
# its own clock; the optional `gate_cmd` is a command a sweeper can run to
# answer "has it cleared yet" without a human or a model. Waiting on nobody is
# not waiting, it is dropping, and waiting on the owner himself is a needs-you
# ask wearing a park's clothes.
MIN_BLOCKED_ON = 8
DEFAULT_RECHECK_S = 6 * 3600
MAX_RECHECK_S = 30 * 86400
MAX_GATE_CMD = 500
# A park aimed at the person the list is for. Matched only when that is the
# WHOLE value: "the owner's manager" and "the user group admin" are real third
# parties and must not be swept up by this.
_WAITS_ON_OWNER = re.compile(r"^\W*(owner|the user|user|the owner|owner|you|me|myself)\W*$",
                             re.I)

def _host_tz():
    """The host's real zone (TZ, else /etc/localtime), so DST is applied per date.
    `datetime.now().astimezone().tzinfo` is only the offset in force right now."""
    names = [os.environ.get("TZ", "").lstrip(":")]
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            names.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in names:
        if name:
            try:
                return zoneinfo.ZoneInfo(name)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
                continue
    return dt.datetime.now().astimezone().tzinfo


_LOCAL_TZ = _host_tz()


class StoreError(Exception):
    """A rejection the caller is expected to read and correct itself from.

    Carries an HTTP status so api.py can map it without knowing why.
    """

    def __init__(self, status: int, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra or {}


def _designed(status: int, detail: str, extra: dict | None = None) -> StoreError:
    """A StoreError for a refusal the door makes on purpose (a rule, a floor, a limit, a policy), as
    opposed to a malformed field. Its body carries design_refusal:true, so a watcher can tell "the
    payload was refused" from "the api is broken" without matching on the text."""
    return StoreError(status, detail, dict(extra or {}, design_refusal=True))


# --- Connection --------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS threads (
  thread_id   TEXT PRIMARY KEY,
  title       TEXT NOT NULL DEFAULT '',
  summary     TEXT NOT NULL DEFAULT '',
  category    TEXT NOT NULL DEFAULT '',
  urgency     TEXT NOT NULL DEFAULT 'whenever',
  state       TEXT NOT NULL DEFAULT 'needs-you',
  ask_kind    TEXT NOT NULL DEFAULT 'none',
  ask_prompt  TEXT NOT NULL DEFAULT '',
  ask_options TEXT NOT NULL DEFAULT '[]',
  responder   TEXT,
  sources     TEXT NOT NULL DEFAULT '[]',
  first_seen  REAL NOT NULL DEFAULT 0,
  last_change REAL NOT NULL DEFAULT 0,
  suppressed  INTEGER NOT NULL DEFAULT 0,
  tombstoned  INTEGER NOT NULL DEFAULT 0,
  dispatched_at     REAL NOT NULL DEFAULT 0,
  dispatch_attempts INTEGER NOT NULL DEFAULT 0,
  last_dispatch_try REAL NOT NULL DEFAULT 0,
  last_note   TEXT NOT NULL DEFAULT '',
  wake_at     REAL NOT NULL DEFAULT 0,
  wake_note   TEXT NOT NULL DEFAULT '',
  wake_ask    TEXT NOT NULL DEFAULT '',
  wake_mode   TEXT NOT NULL DEFAULT '',
  wake_set_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  event_id   TEXT PRIMARY KEY,
  thread_id  TEXT NOT NULL,
  ts         REAL NOT NULL,
  source     TEXT NOT NULL DEFAULT '',
  source_id  TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL DEFAULT '',
  body       TEXT NOT NULL DEFAULT '',
  reply_kind TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id, ts);

CREATE TABLE IF NOT EXISTS categories (
  name       TEXT PRIMARY KEY,
  first_seen REAL NOT NULL DEFAULT 0,
  last_seen  REAL NOT NULL DEFAULT 0
);

-- Metadata only. The bytes live wherever the API put them; a store that also
-- writes files can fail for reasons its tests cannot reach (contract: P1 §6).
CREATE TABLE IF NOT EXISTS attachments (
  att_id    TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  ts        REAL NOT NULL DEFAULT 0,
  filename  TEXT NOT NULL DEFAULT '',
  mime      TEXT NOT NULL DEFAULT '',
  size      INTEGER NOT NULL DEFAULT 0,
  sha256    TEXT NOT NULL DEFAULT '',
  url       TEXT NOT NULL DEFAULT '',
  uploader  TEXT NOT NULL DEFAULT 'owner',
  caption   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_att_thread ON attachments(thread_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_att_dupe ON attachments(thread_id, sha256);

CREATE TABLE IF NOT EXISTS suppressions (
  id          TEXT PRIMARY KEY,
  producer    TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  sample      TEXT NOT NULL DEFAULT '',
  note        TEXT NOT NULL DEFAULT '',
  created     REAL NOT NULL DEFAULT 0,
  active      INTEGER NOT NULL DEFAULT 1,
  hits        INTEGER NOT NULL DEFAULT 0,
  last_hit    REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_supp_rule ON suppressions(producer, fingerprint);

-- The lesson ledger (contract: P3c §3). In the main DDL rather than a
-- migration: CREATE TABLE IF NOT EXISTS means an existing DB adopts it on its
-- next open with no step, and no ported test sees a schema change. A lesson is
-- a correction the owner adopted: durable, scoped, rendered into executor prompts
-- until retired, and never silently mutated -- every change lands in changelog
-- as a report, not a status word (LESSON 11).
CREATE TABLE IF NOT EXISTS lessons (
  id             TEXT PRIMARY KEY,
  created_ts     REAL NOT NULL,
  source_thread  TEXT NOT NULL,
  rule_text      TEXT NOT NULL,
  scope          TEXT NOT NULL DEFAULT 'global',
  status         TEXT NOT NULL DEFAULT 'active',      -- active | retired
  superseded_by  TEXT NOT NULL DEFAULT '',
  gate_status    TEXT NOT NULL DEFAULT '',            -- '' | pending | implemented
  changelog      TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_lessons_scope ON lessons(scope, status);

CREATE TABLE IF NOT EXISTS labels (
  name                   TEXT PRIMARY KEY,
  description            TEXT NOT NULL DEFAULT '',
  prompt_template        TEXT NOT NULL,
  webhook_url            TEXT NOT NULL DEFAULT '',
  executor_runtime       TEXT NOT NULL DEFAULT 'agent',
  convert_to_needs_human INTEGER NOT NULL DEFAULT 0,
  enabled                INTEGER NOT NULL DEFAULT 1,
  created_at             REAL NOT NULL,
  updated_at             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS operational_errors (
  error_id   TEXT PRIMARY KEY,
  ts         REAL NOT NULL,
  trace_id   TEXT NOT NULL,
  component  TEXT NOT NULL,
  event      TEXT NOT NULL,
  details    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operational_errors_ts ON operational_errors(ts);

-- Who sent a request the api refused. The access line and the client_error line live in the
-- container's stdout and die with it; a deploy recreates the container, so the caller of a 4xx
-- could not be named afterwards. One row per authenticated 4xx, never a 401 (record_refusal), kept
-- REFUSALS_KEEP_S and at most REFUSALS_MAX rows. Never the body, only its `actor`.
CREATE TABLE IF NOT EXISTS refused_requests (
  id         INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  method     TEXT NOT NULL DEFAULT '',
  route      TEXT NOT NULL DEFAULT '',
  thread_id  TEXT NOT NULL DEFAULT '',
  status     INTEGER NOT NULL DEFAULT 0,
  client     TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT '',
  actor      TEXT NOT NULL DEFAULT '',
  login      TEXT NOT NULL DEFAULT '',
  principal  TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '',
  caller     TEXT NOT NULL DEFAULT '',
  batch      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_refused_requests_ts ON refused_requests(ts);
CREATE INDEX IF NOT EXISTS idx_refused_requests_thread ON refused_requests(thread_id);

-- The card-shape rules (helm/card_rules.py) in log mode: one row per (door, thread, rule, question)
-- an ask broke, and one rule='none' row per question that broke none, so a window has a denominator.
-- Never card text: the rule id, the thread id, who sent it, when, and a hash of the question.
-- Kept CARD_RULES_KEEP_S and at most CARD_RULES_MAX rows (record_card_rules).
CREATE TABLE IF NOT EXISTS card_rule_warnings (
  id         INTEGER PRIMARY KEY,
  ts         REAL NOT NULL,
  door       TEXT NOT NULL DEFAULT '',
  thread_id  TEXT NOT NULL DEFAULT '',
  rule       TEXT NOT NULL DEFAULT '',
  mode       TEXT NOT NULL DEFAULT '',
  principal  TEXT NOT NULL DEFAULT '',
  author     TEXT NOT NULL DEFAULT '',
  ask_key    TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_card_rule_warnings_once
  ON card_rule_warnings(door, thread_id, rule, ask_key);
CREATE INDEX IF NOT EXISTS idx_card_rule_warnings_ts ON card_rule_warnings(ts);

-- The executor runs work's the poller reports on its heartbeat: old
-- the store read them off a client's disk once; it reads them from its own now. One row per run
-- folder, keyed by its name so a resend is a no-op; kept RUNS_KEEP_S (record_runs).
CREATE TABLE IF NOT EXISTS dispatcher_runs (
  name              TEXT PRIMARY KEY,
  ts                REAL NOT NULL DEFAULT 0,
  thread_id         TEXT NOT NULL DEFAULT '',
  outcome           TEXT NOT NULL DEFAULT '',
  turns             INTEGER NOT NULL DEFAULT 0,
  cost_usd          REAL NOT NULL DEFAULT 0,
  input_tokens      INTEGER NOT NULL DEFAULT 0,
  output_tokens     INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  duration_s        REAL NOT NULL DEFAULT 0,
  received          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dispatcher_runs_ts ON dispatcher_runs(ts);
"""

_MIGRATIONS = (
    ("dispatched_at", "REAL NOT NULL DEFAULT 0"),
    ("dispatch_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("last_dispatch_try", "REAL NOT NULL DEFAULT 0"),
    ("last_note", "TEXT NOT NULL DEFAULT ''"),
    ("wake_at", "REAL NOT NULL DEFAULT 0"),
    ("wake_note", "TEXT NOT NULL DEFAULT ''"),
    ("wake_ask", "TEXT NOT NULL DEFAULT ''"),
    # Who set the snooze, for a rule pack's recheck on wake: 'recheck' when a run set it
    # through /result, 'reminder' when the owner set it from the page, '' when it predates the column
    # (wake_due then reads the history). wake_set_at is when, for the wake line's date.
    ("wake_mode", "TEXT NOT NULL DEFAULT ''"),
    ("wake_set_at", "REAL NOT NULL DEFAULT 0"),
    # Orthogonal to state, not a replacement for it. `waiting-external` was
    # doing two jobs at once: a real wait on someone who might answer any day,
    # and a dropped or dependency-blocked thread with nothing actually moving.
    # Both rendered identically ("waiting on someone else. nothing is needed
    # from you"), so a backlog of the second kind read as a backlog of the
    # first, and there was no way to tell them apart without opening every
    # card and reading its history. `parked` names the second kind explicitly
    # instead of leaving it to look like the first.
    ("parked", "INTEGER NOT NULL DEFAULT 0"),
    ("parked_reason", "TEXT NOT NULL DEFAULT ''"),
    # A thread describes a decision; `links` says where the actual thing lives
    # so a card can be opened instead of hunted for. Set by a producer (PUT,
    # same treatment as title/summary: a resync always reflects what it sent)
    # or by an executor's result (see post_result, which preserves the
    # existing value when the key is simply absent, unlike `ask`). Either way
    # it is frozen by _answer_is_in_flight the same as ask_kind/ask_options,
    # so a routine producer resync landing right after a bot-result cannot
    # silently erase the link that result just reported.
    ("links", "TEXT NOT NULL DEFAULT '[]'"),
    # The park contract (see the constants block above). Additive, so a store
    # written before these existed opens unchanged; parks already in it carry
    # recheck_at=0 and are adopted by adopt_legacy_parks() rather than being
    # left inert, because a gate is not a migration.
    ("blocked_on", "TEXT NOT NULL DEFAULT ''"),
    ("recheck_at", "REAL NOT NULL DEFAULT 0"),
    ("recheck_count", "INTEGER NOT NULL DEFAULT 0"),
    ("gate_cmd", "TEXT NOT NULL DEFAULT ''"),
    ("parked_since", "REAL NOT NULL DEFAULT 0"),
    # The self-contained gate's one escape hatch (contract: P1 §3). A lettered
    # list is legible on a phone exactly when the list is ON the phone, so the
    # card carries its own copy. Additive and optional: every ask written
    # before it existed reads back as ''.
    ("ask_context", "TEXT NOT NULL DEFAULT ''"),
    # The bot-close rules' two assertions (contract: P1 §4). They live on the
    # row, not only in the result body, because "the answer was applied and
    # nothing is owed" is a claim about the ROUND, and a run that applies the
    # answer in one result and closes in the next must not have to re-assert
    # what it already said. They are cleared the moment either could stop being
    # true: a new reply from him, or a new question from a run.
    ("answer_applied", "INTEGER NOT NULL DEFAULT 0"),
    ("nothing_owed", "INTEGER NOT NULL DEFAULT 0"),
    ("label", "TEXT NOT NULL DEFAULT ''"),
    # The needs-owner gate (helm/gate_rules.py, the owner 2026-09-24). The question a
    # gate-held or gate-settled card carried, kept so a release or a reopen
    # brings back HIS question rather than a generic "say what you want".
    ("gate_hold_ask", "TEXT NOT NULL DEFAULT ''"),
    # When the CURRENT question (kind, prompt, options) was first put to the owner. The two triggers
    # in init() keep it, so every writer of the ask columns gets it for free and a resync, a
    # context edit or a dispatch touch cannot move it. last_change cannot serve: it moves on
    # bookkeeping, so a card that had waited for days announced itself again as if it were new.
    ("ask_lit_at", "REAL NOT NULL DEFAULT 0"),
    # The question ask_lit_at was stamped for (kind, prompt, options joined). It is never cleared,
    # so a snooze, a gate hold or a recheck that empties the ask and later puts the SAME question
    # back does not restamp it: the card is still as old as the question.
    ("ask_lit_key", "TEXT NOT NULL DEFAULT ''"),
    # What the producer said about the live question: {"subject", "recap", "key"}. `key` is the
    # question it was written for (the same key as ask_lit_key), and the read model shows it only
    # while that is still the live question, so a recap never outlives the question it describes.
    ("ask_meta", "TEXT NOT NULL DEFAULT ''"),
    # The question (same key as ask_lit_key) that a snooze wake re-lit AFTER an agent had already
    # reported on the thread while it was snoozed: that question may be answered or overtaken. The
    # read model shows ask.stale only while this is still the live question, so a new question
    # is never marked.
    ("ask_stale_key", "TEXT NOT NULL DEFAULT ''"),
)


def init(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    have = {r["name"] for r in con.execute("PRAGMA table_info(threads)")}
    for col, decl in _MIGRATIONS:
        if col not in have:
            con.execute("ALTER TABLE threads ADD COLUMN " + col + " " + decl)
    have_events = {r["name"] for r in con.execute("PRAGMA table_info(events)")}
    if "reply_kind" not in have_events:
        con.execute("ALTER TABLE events ADD COLUMN reply_kind TEXT NOT NULL DEFAULT ''")
    if "draft" not in have_events:
        # The checked draft a report carries (JSON, "Use the draft"); empty on every other event.
        con.execute("ALTER TABLE events ADD COLUMN draft TEXT NOT NULL DEFAULT ''")
    if "via" not in have_events:
        # How an event arrived, set only by the store from a proof a bearer caller cannot give
        # (`WEB_UI` for a press of the signed-in page); empty on every other event.
        con.execute("ALTER TABLE events ADD COLUMN via TEXT NOT NULL DEFAULT ''")
    if "via_login" not in have_events:
        con.execute("ALTER TABLE events ADD COLUMN via_login TEXT NOT NULL DEFAULT ''")
    have_refused = {r["name"] for r in con.execute("PRAGMA table_info(refused_requests)")}
    for col in ("caller", "batch"):
        # The caller's own X-Caller label, and the thread ids of a refused PUT batch (additive).
        if col not in have_refused:
            con.execute("ALTER TABLE refused_requests ADD COLUMN " + col + " TEXT NOT NULL DEFAULT ''")
    con.execute("CREATE INDEX IF NOT EXISTS idx_threads_label ON threads(label)")
    _init_ask_lit(con)
    _tokens.init(con)
    if _fts_available(con):
        # An existing database adopts the index on its next open; nothing else changes.
        n_fts = con.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
        n_threads = con.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        if n_fts == 0 and n_threads > 0:
            rebuild_search_index(con)
    cnt = con.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
    if cnt == 0 and rules().intake_labels:
        now = time.time()
        con.execute(
            "INSERT OR IGNORE INTO labels (name, description, prompt_template, webhook_url, executor_runtime, convert_to_needs_human, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("triage", "Default intake triage for new tasks",
             "Triage this intake request, perform any immediate analysis needed, and produce an actionable result or structured ask.",
             "", "agent", 0, 1, now, now)
        )
        con.execute(
            "INSERT OR IGNORE INTO labels (name, description, prompt_template, webhook_url, executor_runtime, convert_to_needs_human, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("needs-human", "Converts raw issue into structured decision options for the owner",
             "Review the input and structure it as a decision for the owner. The deliverable MUST be a self-contained needs-you card with kind choice or ack, clear options, and no assumed context.",
             "", "agent", 1, 1, now, now)
        )
    scnt = con.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    if scnt == 0 and rules().intake_labels:
        now = time.time()
        for k, v in [
            ("default_intake_label", "triage"),
            ("llm_conversion_enabled", "1"),
            ("needs_human_prompt", "Convert raw intake tasks into clear, actionable needs-you choices for the owner with multiple-choice options and no context assumed."),
        ]:
            con.execute("INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)", (k, v, now))
    con.commit()


_NOW_SQL = "(julianday('now') - 2440587.5) * 86400.0"
_ASK_KEY_SQL = "(NEW.ask_kind || char(31) || NEW.ask_prompt || char(31) || NEW.ask_options)"


def _ask_key(kind, prompt, options_json) -> str:
    """The Python twin of _ASK_KEY_SQL."""
    return "%s\x1f%s\x1f%s" % (kind, prompt, options_json)


def _ask_run_times(con, thread_id, kind, prompt):
    """Timestamps, oldest first, of the unbroken run of bot-ask events that put THIS question,
    counted back from the newest. One different question in between ends the run, so a question
    that was changed and changed back counts from the change."""
    head = "ASKED YOU (%s): %s" % (kind, (prompt or "").strip())
    run = []
    for e in con.execute("SELECT ts, body FROM events WHERE thread_id=? AND kind='bot-ask'"
                         " ORDER BY ts DESC, rowid DESC", (thread_id,)):
        body = e["body"] or ""
        if body != head and not body.startswith(head + "\n"):   # a prompt may itself run over several lines
            break
        run.append(e["ts"])
    return sorted(run)


def _init_ask_lit(con) -> None:
    """Keep threads.ask_lit_at: stamped when a live ask is inserted, and again only when the
    QUESTION (kind, prompt, options) differs from the one last stamped. Context, summary, state and
    last_change edits leave it alone, and so does an ask that is emptied (snooze, gate hold,
    recheck) and put back unchanged. It answers "how long has the owner been holding THIS question"."""
    con.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_ask_lit_ins AFTER INSERT ON threads"
        " WHEN NEW.ask_kind != 'none'"
        " BEGIN UPDATE threads SET ask_lit_at = " + _NOW_SQL + ", ask_lit_key = " + _ASK_KEY_SQL +
        " WHERE thread_id = NEW.thread_id; END")
    con.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_ask_lit_upd AFTER UPDATE OF ask_kind, ask_prompt, ask_options"
        " ON threads WHEN NEW.ask_kind != 'none' AND " + _ASK_KEY_SQL + " IS NOT NEW.ask_lit_key"
        " BEGIN UPDATE threads SET ask_lit_at = " + _NOW_SQL + ", ask_lit_key = " + _ASK_KEY_SQL +
        " WHERE thread_id = NEW.thread_id; END")
    # Cards that predate the column. A live ask counts from the first of the unbroken run of ask
    # events that put it; an ask held away right now (snoozed or gate-held) is read from the copy
    # kept for its return, so it comes back with its real age. With no record the card's last_change
    # is the fallback: the question cannot be younger than the last thing that happened, so the
    # error is toward "newer", never toward claiming it is older than it is.
    for r in con.execute("SELECT thread_id, ask_kind, ask_prompt, ask_options, wake_ask, gate_hold_ask,"
                         " last_change FROM threads WHERE ask_lit_at = 0 AND (ask_kind != 'none'"
                         " OR wake_ask != '' OR gate_hold_ask != '')").fetchall():
        if r["ask_kind"] != "none":
            kind, prompt, opts = r["ask_kind"], r["ask_prompt"], r["ask_options"]
        else:
            held = _jloads(r["wake_ask"] or r["gate_hold_ask"], {}) or {}
            if not isinstance(held, dict):
                continue      # a leftover that is not a stored ask is not a held question
            kind, prompt = str(held.get("kind") or "none"), str(held.get("prompt") or "")
            opts = json.dumps(held.get("options") or [])
            if kind == "none":
                continue
        run = _ask_run_times(con, r["thread_id"], kind, prompt)
        con.execute("UPDATE threads SET ask_lit_at=?, ask_lit_key=? WHERE thread_id=?",
                    (run[0] if run else (r["last_change"] or 0), _ask_key(kind, prompt, opts), r["thread_id"]))


def db(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(p, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    init(con)
    return con


def _jloads(raw, default):
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return v if v is not None else default


# --- Events: append-only, content-addressed ----------------------------------
def event_id(thread_id, ts, source, kind, body) -> str:
    raw = "|".join([str(thread_id), str(ts), str(source), str(kind), str(body)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _after_latest(con, thread_id, now=None) -> float:
    """A timestamp for the next event on a thread that never falls behind the
    thread's latest one. reply() and post_result() date their events a few
    milliseconds apart to keep them in order, so two calls within the same
    millisecond (a fast test runner; a result landing on the heels of the tap
    that queued it) would otherwise interleave by wall clock, and a "since the
    last report" comparison (the answer gate, unconsumed attachments) would read
    the older event as the newer one. Seen once on a CI runner: the note that
    asked a question outdated the report that answered it by 0.1 ms."""
    now = time.time() if now is None else now
    r = con.execute("SELECT MAX(ts) FROM events WHERE thread_id=?", (thread_id,)).fetchone()
    last = r[0] if r is not None and r[0] is not None else 0.0
    return now if now > last else last + 0.001


def add_event(con, thread_id, ts, source, source_id, kind, body, reply_kind="", draft="",
              via="", via_login="") -> None:
    """INSERT OR IGNORE on a content hash, so re-running a pass is idempotent
    rather than additive. `ts` is part of the hash, so the same observation
    re-sent with a new timestamp still appends: the producer sends stable
    timestamps for stable observations.

    A per-caller token's principal (helm.tokens) rides in `source_id` when the
    event carries none, so history says which caller wrote it without touching
    the `source` column every rule reads.

    `via` and `via_login` are never taken from a caller's payload: only a store function that has
    verified the proof (see `reply`'s `press`) passes them."""
    if not source_id:
        source_id = _tokens.PRINCIPAL.get("") or ""
    eid = event_id(thread_id, ts, source, kind, body)
    cur = con.execute(
        "INSERT OR IGNORE INTO events (event_id, thread_id, ts, source, source_id, kind, body, reply_kind,"
        " draft, via, via_login) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (eid, str(thread_id), float(ts),
         str(source or ""), str(source_id or ""), str(kind or ""), str(body or ""), str(reply_kind or ""),
         str(draft or ""), str(via or ""), str(via_login or "")[:64]))
    if cur.rowcount == 1 and body:
        _fts_add(con, thread_id, "event", eid, str(body))


# --- Full-text search: an FTS5 index over titles, summaries and event bodies,
# maintained on write. Additive: created on open when missing, rebuilt when empty, and
# absent altogether on a SQLite without FTS5 (search_threads then scans in Python).
FTS_AVAILABLE = None


def _fts_available(con) -> bool:
    """Make sure this connection's database has the index table; remember whether
    the SQLite build can make one at all (one failed CREATE settles it)."""
    global FTS_AVAILABLE
    if FTS_AVAILABLE is False:
        return False
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5("
                    "thread_id UNINDEXED, kind UNINDEXED, ref UNINDEXED, text)")
        FTS_AVAILABLE = True
    except sqlite3.OperationalError:
        FTS_AVAILABLE = False
    return FTS_AVAILABLE


def _fts_add(con, thread_id, kind, ref, text) -> None:
    if not FTS_AVAILABLE:
        return
    con.execute("INSERT INTO search_fts (thread_id, kind, ref, text) VALUES (?,?,?,?)",
                (str(thread_id), kind, str(ref or ""), text))


def _fts_thread(con, thread_id, title, summary, category) -> None:
    """(Re)index one thread's own text. Called after every write of title/summary."""
    if not FTS_AVAILABLE:
        return
    con.execute("DELETE FROM search_fts WHERE thread_id=? AND kind='thread'", (str(thread_id),))
    _fts_add(con, thread_id, "thread", thread_id,
             " ".join(x for x in (title, summary, category) if x))


def rebuild_search_index(con) -> int:
    """Rebuild search_fts from threads + events. Returns the row count, or -1 without FTS5."""
    if not _fts_available(con):
        return -1
    con.execute("DELETE FROM search_fts")
    con.execute("INSERT INTO search_fts (thread_id, kind, ref, text)"
                " SELECT thread_id, 'thread', thread_id, title || ' ' || summary || ' ' || category"
                " FROM threads")
    con.execute("INSERT INTO search_fts (thread_id, kind, ref, text)"
                " SELECT thread_id, 'event', event_id, body FROM events WHERE body != ''")
    con.commit()
    return con.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]


def _fts_query(q) -> str:
    """Every token required, as a prefix: 'pine hol' -> "pine"* AND "hol"*."""
    toks = [t for t in str(q or "").split() if t]
    return " AND ".join('"%s"*' % t.replace('"', '""') for t in toks)


# --- Validation --------------------------------------------------------------
def slug(s: str, maxlen: int = 40) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (s or "").strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:maxlen].strip("-")


# --- Terminal inference (work rule pack, 2026-09-21) ---------------------------------
# Producers keep omitting `terminal` on the option that means "do nothing", which sent
# the thread to `working` and woke an executor with nothing to do. At work an unset
# `terminal` is inferred from the label and effect; an explicit one is obeyed;
# `resolved` is never inferred.
_NO_WORK_LABEL = ("leave it", "nothing", "no action", "no further", "close this out",
                  "close it out", "out of scope", "not a fault", "false alarm",
                  "drop it", "drop the", "intentional", "already done")

_NO_WORK_EFFECT = ("nothing further", "no further action", "do nothing", "nothing else",
                   "thread can close", "can close", "close it", "stop tracking",
                   "nothing is scheduled", "no writes", "no change", "settles this card",
                   "already satisfied", "as satisfied", "is satisfied", "already covered")

_TAKEOVER_LABEL = ("i've got it", "ive got it", "got it from here", " myself",
                   "i'll check", "i'll handle", "i'll do", "i'll send", "i'll run",
                   "i'll write", "hold as is", "seen,", "no reply", "not needed")

_ACK_LABEL = ("ok", "okay", "got it", "noted", "seen", "understood", "acknowledged",
              "fine", "sounds good")


def declares_no_work(label, effect):
    """True when an option says to do nothing, so it should settle rather than dispatch.

    A label where the owner takes the work over ("I've got it from here", "Posting it
    myself") settles on the label alone. Otherwise read both halves: "leave it"
    with an effect that still asks for a sweep is a request for work, and an
    option with no effect text describes no work by definition.
    """
    lab = (label or "").strip().lower()
    if any(p in lab for p in _TAKEOVER_LABEL):
        return True
    if not any(p in lab for p in _NO_WORK_LABEL):
        return False
    eff = (effect or "").strip().lower()
    if not eff:
        return True
    return any(p in eff for p in _NO_WORK_EFFECT)


def _option_limits():
    """(max label, max value, max effect) per site. Work Helm capped nothing but the
    value slug (48); a copied work database has to validate under its own limits."""
    if rules().strict_limits:
        return MAX_ASK_LABEL, MAX_ASK_VALUE, MAX_ASK_EFFECT
    return MAX_NOTE, 48, MAX_NOTE


def _split_piped(text):
    """("label", "value", "effect") from the CLI's "Label | value | effect" form, or None when
    the text carries no pipe. Parts past the third stay in the effect rather than being dropped."""
    if "|" not in text:
        return None
    bits = [b.strip() for b in text.split("|")]
    return bits[0], bits[1], " | ".join(b for b in bits[2:] if b)


def normalize_options(raw):
    """Coerce ask.options into [{label,value,effect}]. Returns (options, errors).

    A language model will hand us an array of bare strings sooner or later.
    Forgive the shape we can repair; refuse the shape we cannot. A silent
    accept here produces a dead screen, which is strictly worse than a loud
    400 the producer can read and correct itself from.
    """
    options, errors = [], []
    if raw is None:
        return options, errors
    if not isinstance(raw, list):
        return options, ["ask.options is " + type(raw).__name__ + ", expected a list"]
    infer = rules().infer_terminal
    max_label, max_value, max_effect = _option_limits()
    for i, o in enumerate(raw):
        piped = _split_piped(o) if isinstance(o, str) else None
        if isinstance(o, dict) and not o.get("value") and not o.get("effect"):
            piped = _split_piped(str(o.get("label") or ""))
        if piped:
            # The "Label | value | effect" form a client sends as one string. The CLI splits it
            # before sending; a run that posts to the API directly did not, and the whole string
            # became the label with an empty effect.
            label, v, eff = piped
            declared = o.get("terminal") if isinstance(o, dict) else None
            opt = {"label": label, "value": slug(v or label, maxlen=max_value), "effect": eff,
                   "terminal": (bool(declared) if declared is not None
                                else (declares_no_work(label, eff) if infer else False))}
            if isinstance(o, dict):
                opt["resolved"] = bool(o.get("resolved"))
        elif isinstance(o, str):
            label = o.strip()
            opt = {"label": label, "value": slug(label, maxlen=max_value), "effect": "",
                   "terminal": declares_no_work(label, "") if infer else False}
        elif isinstance(o, dict):
            label = str(o.get("label") or "").strip()
            val = o.get("value")
            val = str(val).strip() if val not in (None, "") else slug(label, maxlen=max_value)
            # `terminal` means picking this IS the whole action: settle it and
            # stop asking. It must not wake an executor, because there is
            # nothing for one to do and it would come back reporting that it
            # could not do what the option promised.
            #
            # `resolved` means picking this option is an assertion that the
            # underlying work is actually finished, not just that Helm should
            # stop asking about it. It is a separate fact from `terminal` on
            # purpose: "leave it, I'll write it myself" and "it's his to
            # finish" are terminal too, but the card is not done, and moving
            # it to Done on a guess is exactly the kind of lie about state
            # this store exists to refuse. Whoever authors the option sets
            # this explicitly; write-back never infers it from the label or
            # effect text.
            eff = str(o.get("effect") or "")
            declared = o.get("terminal")
            if infer and declared is None:
                terminal = declares_no_work(label, eff)
            else:
                terminal = bool(declared)
            opt = {"label": label, "value": val, "effect": eff,
                   "terminal": terminal, "resolved": bool(o.get("resolved"))}
        else:
            errors.append("ask.options[%d] is %s, expected string or object"
                          % (i, type(o).__name__))
            continue
        if not opt["label"]:
            errors.append("ask.options[%d] has an empty label" % i)
            continue
        if len(opt["label"]) > max_label:
            errors.append("ask.options[%d].label is %d chars, max %d"
                          % (i, len(opt["label"]), max_label))
            continue
        if not opt["value"]:
            errors.append("ask.options[%d] label %r yields an empty value" % (i, opt["label"]))
            continue
        if is_press_value(opt["value"]):
            errors.append("ask.options[%d].value is a Use the draft press: an option cannot stand in for "
                          "the draft button (the press is made only from the draft's own button)" % i)
            continue
        if len(opt["value"]) > max_value:
            errors.append("ask.options[%d].value is %d chars, max %d"
                          % (i, len(opt["value"]), max_value))
            continue
        if len(opt["effect"]) > max_effect:
            errors.append("ask.options[%d].effect is %d chars, max %d"
                          % (i, len(opt["effect"]), max_effect))
            continue
        options.append(opt)
    # A single option that only acknowledges is an ack wearing a choice's clothes
    #: the one tap has to settle the thread or it can never be answered.
    if infer and len(options) == 1 and not options[0]["terminal"]:
        if options[0]["label"].strip().lower().rstrip(".!") in _ACK_LABEL:
            options[0]["terminal"] = True
    return options, errors


def normalize_links(raw):
    """Coerce links into [{label,url}]. Returns (links, errors).

    A link is how a thread points at the actual page, card, report or file it
    is about, so it can be opened instead of hunted for by hand. `url` is the
    only required part and is not restricted to http(s): a Windows path or a
    drive share is just as valid a destination, and the UI decides how to
    render it (a real anchor for a web address, visible plain text for a
    path) rather than this rejecting one shape or the other. `label` defaults
    to the url itself when omitted, the same fallback normalize_options uses
    for `value`.
    """
    links, errors = [], []
    if raw is None:
        return links, errors
    if not isinstance(raw, list):
        return links, ["links is " + type(raw).__name__ + ", expected a list"]
    if len(raw) > MAX_LINKS:
        return links, ["links has %d entries, max %d" % (len(raw), MAX_LINKS)]
    for i, item in enumerate(raw):
        if isinstance(item, str):
            url, label = item.strip(), ""
        elif isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            label = str(item.get("label") or "").strip()
        else:
            errors.append("links[%d] is %s, expected string or object"
                          % (i, type(item).__name__))
            continue
        if not url:
            errors.append("links[%d] has no url" % i)
            continue
        if len(url) > MAX_LINK_FIELD:
            errors.append("links[%d].url is %d chars, max %d" % (i, len(url), MAX_LINK_FIELD))
            continue
        links.append({"label": (label or url)[:MAX_LINK_FIELD], "url": url})
    return links, errors


def park_fields(body, now=None):
    """The park contract as pure data. Returns ((blocked_on, recheck_at, gate_cmd),
    errors).

    Returns errors rather than raising so the producer's door can reject one bad
    thread in a payload and still apply the rest (put_threads' partial success),
    while the bot's door raises the first error as a 400. Every message names the
    field and what to send instead, because the reader is usually a model that has
    to correct its own payload without a second round trip.
    """
    now = time.time() if now is None else now
    errors = []

    blocked_on = str(body.get("blocked_on") or "").strip()
    if len(blocked_on) < MIN_BLOCKED_ON:
        errors.append(
            "state 'waiting-external' requires 'blocked_on' (at least %d characters, got "
            "%d): name WHO or WHAT this waits on -- a person, a vendor case, an agent, a "
            "scheduled run, an external date. If the answer is the owner, this is NOT "
            "waiting-external: use 'needs-you' with an ask ('ack' if reading it is the "
            "whole job). If the answer is nobody, the work is not blocked and should "
            "continue." % (MIN_BLOCKED_ON, len(blocked_on)))
    elif _WAITS_ON_OWNER.match(blocked_on):
        errors.append(
            "'blocked_on' is %r, which says this waits on the person the list is FOR. "
            "'waiting-external' renders as \"waiting on someone else\" with no button, "
            "so a park aimed at him waits on him invisibly, forever. Use state "
            "'needs-you' with ask.kind 'ack' (he reads it and closes) or 'choice' (he "
            "decides)." % blocked_on)

    raw = body.get("recheck_at")
    recheck_at = 0.0
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # Default rather than reject: a run that named a real blocker did the
        # honest thing, and a missing date should not cost it the whole result.
        recheck_at = now + DEFAULT_RECHECK_S
    elif isinstance(raw, bool):
        errors.append("'recheck_at' is a boolean; send epoch seconds or an ISO-8601 date "
                      "(e.g. '2026-10-01' or '2026-10-01T09:00:00'), or omit it to accept "
                      "the %dh default" % (DEFAULT_RECHECK_S // 3600))
    else:
        parsed = _parse_wake_at(raw)
        if parsed is None:
            errors.append("'recheck_at' is %r, which is neither epoch seconds nor an "
                          "ISO-8601 date. Send '2026-10-01' or '2026-10-01T09:00:00' or a "
                          "number, or omit it to accept the %dh default"
                          % (raw, DEFAULT_RECHECK_S // 3600))
        elif parsed > now + MAX_RECHECK_S:
            errors.append("'recheck_at' is %.1f days out and the ceiling is %d: that is "
                          "abandonment wearing a park's clothes. Pick a real date inside "
                          "%d days, or hand it back as 'needs-you' and let him decide"
                          % ((parsed - now) / 86400.0, MAX_RECHECK_S // 86400,
                             MAX_RECHECK_S // 86400))
        else:
            # A date already in the past would hide the thread from its own
            # sweeper's next pass being meaningful; it is honoured as "check
            # again soon" rather than rejected, since nothing about it is a lie.
            recheck_at = parsed if parsed > now else now + DEFAULT_RECHECK_S

    gate_cmd = ""
    raw_gate = body.get("gate")
    if raw_gate is None:
        raw_gate = body.get("gate_cmd")
    if raw_gate is not None:
        if not isinstance(raw_gate, str):
            errors.append("'gate' is %s, expected a single shell command as a string that "
                          "a sweeper can run to test whether the blocker still holds "
                          "(exit 0 = cleared)" % type(raw_gate).__name__)
        else:
            cmd = raw_gate.strip()
            if cmd[:5].upper() == "GATE:":
                cmd = cmd[5:].strip()
            if not cmd:
                errors.append("'gate' was sent empty. Omit the key entirely if there is no "
                              "command that can answer 'has it cleared yet'; an empty one "
                              "reads as a check that always passes")
            elif "\n" in cmd or "\r" in cmd or "\x00" in cmd:
                errors.append("'gate' must be a single line: a sweeper runs it as one "
                              "command and a multi-line script has nowhere to be reviewed")
            elif len(cmd) > MAX_GATE_CMD:
                errors.append("'gate' is %d chars, max %d. Put the logic in a script and "
                              "name the script here" % (len(cmd), MAX_GATE_CMD))
            else:
                gate_cmd = cmd

    return (blocked_on, recheck_at, gate_cmd), errors


def require_park(body, now=None):
    """park_fields, raising the first error as the 400 a caller gets."""
    park, errors = park_fields(body, now)
    if errors:
        raise StoreError(400, "; ".join(errors))
    return park


# --- The self-contained gate (contract: P1 §3; LESSON 17) --------------------
#
# An option is written by something that is looking at the email, the ticket,
# the previous card or its own last message, and then rendered on a phone where
# none of that exists. "Go with the first one" is a complete thought at the
# moment it is authored and an unanswerable riddle ninety seconds later.
#
# The test is the screenshot: the prompt and its buttons, alone, with nothing
# above or below them. Deterministic patterns only -- this must never need a
# model to decide whether a button is legible (LESSON 33).
_EXTERNAL_REF = (
    (re.compile(r"\bas (discussed|described|mentioned|noted|agreed|stated|above|below|"
                r"before|previously|per)\b", re.I),
     "refers to a conversation that is not on the card"),
    # "check"/"read" are deliberately NOT in this verb list: "I go and read the
    # logs" is an ACTION the option performs, not a reference the reader has to
    # chase. Only the pointing verbs count.
    (re.compile(r"\b(see|per|refer to) (the )?(above|below|attached|attachment|"
                r"e-?mail|thread|ticket|card|doc|document|report|link|message|chat|"
                r"screenshot|log|transcript)s?\b", re.I),
     "sends the reader somewhere else to find out what the option means"),
    (re.compile(r"\b(the|my|his|her|their|that) (first|second|third|last|previous|earlier|"
                r"other|former|latter|original) (one|option|choice|suggestion|plan|idea|"
                r"approach|version|answer|reply)\b", re.I),
     "counts or points at something that is not visible here"),
    (re.compile(r"\b(that) (one|thing)\b", re.I),
     "points at a thing the card never names"),
    # "this one" is deliberately absent: on a card, "this" is the card, which is
    # the one piece of context the reader always has. Only a label that is
    # NOTHING but a pointer ("this one" alone) is refused, by _DEICTIC_ONLY.
    (re.compile(r"\b(we|you|i|they|he|she) (agreed|discussed|decided|said|talked about|"
                r"settled on|went over)\b", re.I),
     "refers to a conversation that is not on the card"),
    (re.compile(r"\bsame as (before|last time|above|the previous|the last)\b", re.I),
     "assumes the reader remembers what happened last time"),
    (re.compile(r"\blike last time\b", re.I),
     "assumes the reader remembers what happened last time"),
    (re.compile(r"\bdo (what|as) (he|she|they|it|the note|the e-?mail|the doc|the ticket|"
                r"the card) (said|says|asked|wanted)\b", re.I),
     "defers to text that is not on the card"),
)
# The whole label is a pointer, with nothing pointed at.
_DEICTIC_ONLY = {"it", "that", "this", "them", "those", "these", "same", "the same",
                 "other", "the other", "above", "below", "that one", "this one",
                 "the one", "both", "either", "neither"}
# The whole label is a position in a list the reader cannot see, spelled out in
# words ("the second", "first"). No letter or number to look up, so no amount of
# card context can rescue it: it counts through a list that was never rendered.
_INDEX_ONLY = re.compile(
    r"^(?:the )?(?:first|second|third|fourth|fifth|last|latter|former)(?: one)?$", re.I)
# The whole label is one or more list TOKENS -- "A", "#2", "option 3", "choice b",
# "C+D", "A and B", "options C & D". These are the labels the brief names: a
# producer that has a lettered list in front of it writes the letters, and the
# phone gets a row of buttons reading "C+D" with no C and no D anywhere on it.
# They are recoverable, and only these are: if the SAME card carries the list
# (ask.context), the letters are legible and the ask is allowed to stand.
_TOKEN_LIST_ONLY = re.compile(
    r"^(?:options?|choices?)?\s*"
    r"#?\s*([0-9]{1,2}|[a-z])"
    r"((?:\s*(?:\+|&|/|,|\band\b|\bor\b|\bplus\b)\s*#?\s*(?:[0-9]{1,2}|[a-z]))*)\s*$", re.I)
_TOKEN_SPLIT = re.compile(r"\s*(?:\+|&|/|,|\band\b|\bor\b|\bplus\b)\s*", re.I)
# A letter or number named INSIDE a longer sentence: "Take option 2",
# "Go with C+D but tell them first".
_OPTION_REF = re.compile(r"\b(?:options?|choices?)\s*#?\s*([0-9]{1,2}|[a-z])\b", re.I)
# How a list defines its own entries, in the shapes a run actually writes them:
# "A) renew", "A. renew", "A - renew", "(A) renew", "A: renew", "A = renew".
# A definition needs a body: "A)" alone on a line defines nothing.
_CONTEXT_DEF = re.compile(
    r"(?:^|[\n;|])\s*\(?\s*([0-9]{1,2}|[a-z])\s*[\)\.\:\-–=]\s*(\S[^\n;|]{2,})",
    re.I)
MAX_ASK_CONTEXT = 1500
# The floor under ask.context for a lit choice/text question (ledger GW-062).
# the owner, 2026-09-06: "When you send me a question on helm with no context, I
# can't answer it. You are sending me questions on helm that assumes I've read
# the entire history and all of your thoughts along the way. I haven't." The
# old gateway refused it at 40 chars; an ack is exempt, reading it is the job.
MIN_ASK_CONTEXT = 40


def ask_context_min_mode():
    """'enforce' refuses a thin ask with a 400; 'log' (the default) accepts it and
    writes one 'ask-context-min would-refuse' line to stderr, so a week of the
    live log names every producer that still sends one before it is switched on
    (HELM_ASK_CONTEXT_MIN=enforce)."""
    mode = (os.environ.get("HELM_ASK_CONTEXT_MIN") or "log").strip().lower()
    return mode if mode in ("log", "enforce") else "log"


def _thin_context_refuses(door, thread_id, detail):
    """True when this thin ask must be refused; in log mode, logs and returns False."""
    if ask_context_min_mode() == "enforce":
        return True
    print("ask-context-min would-refuse door=%s thread=%s: %s"
          % (door, str(thread_id)[:120], detail.split(":")[0]), file=sys.stderr, flush=True)
    return False


# --- The cold read: ask.subject and ask.recap --------------------------------
# A card is read by someone who has not thought about it in a month. subject names the thing
# ("LazyFishSitter 0.1.5.0"); recap says what it is and why it needs him in two sentences. Neither
# may carry a time word (the card shows its own age from the store's clock; anything an agent wrote
# about time cannot be checked) or machine text (a SHA, a path, a task id). Limits are hard; the
# soft rules log first and refuse only under HELM_ASK_RECAP=enforce, the context floor's rollout,
# so a producer that has not caught up loses nothing on the day this ships.
MAX_ASK_SUBJECT = 40
MAX_ASK_RECAP = 280
_TIME_WORDS = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|this (?:morning|afternoon|evening|week)|last (?:night|week|month)"
    r"|\d+\s*(?:seconds?|minutes?|hours?|days?|weeks?)\s+ago|just now|recently)\b", re.I)
_MACHINE_TEXT = re.compile(
    r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b|/home/|/opt/|/etc/|\.py\b|\bt-[a-z]+-[\w-]{6,}|\btasks-\d{8}", re.I)


def ask_recap_mode():
    """'log' (default) or 'enforce' (HELM_ASK_RECAP=enforce)."""
    return "enforce" if (os.environ.get("HELM_ASK_RECAP") or "").strip().lower() == "enforce" else "log"


def ask_meta_fields(ask):
    """(subject, recap, hard_errors) from an ask object. Whitespace is collapsed."""
    ask = ask if isinstance(ask, dict) else {}
    subject = " ".join(str(ask.get("subject") or "").split())
    recap = " ".join(str(ask.get("recap") or "").split())
    errors = []
    if len(subject) > MAX_ASK_SUBJECT:
        errors.append("ask.subject is %d chars, max %d: it names the thing, like 'LazyFishSitter 0.1.5.0', "
                      "not what happened to it" % (len(subject), MAX_ASK_SUBJECT))
    if len(recap) > MAX_ASK_RECAP:
        errors.append("ask.recap is %d chars, max %d: two sentences, what this is and why it needs him; "
                      "the rest belongs in ask.context" % (len(recap), MAX_ASK_RECAP))
    return subject, recap, errors


def ask_recap_issues(kind, subject, recap):
    """The soft rules for a needs-you ask, as 400-style sentences ([] when it is clean)."""
    issues = []
    if kind in ("choice", "text") and not recap:
        issues.append("ask.recap is missing: he reads this card cold, so say in two sentences what it is "
                      "and why it needs him, and name the thing in ask.subject")
    for name, text in (("subject", subject), ("recap", recap)):
        m = _TIME_WORDS.search(text or "")
        if m:
            issues.append("ask.%s says %r: the card shows its own age, and nothing written about time can "
                          "be checked. Say what is true, not when" % (name, m.group(0)))
        if _MACHINE_TEXT.search(text or ""):
            issues.append("ask.%s carries machine text (a commit id, a path or a task id): he cannot read "
                          "those, so say what it is in words" % name)
    return issues


def _recap_refuses(door, thread_id, issues):
    """True when these issues must refuse the ask; in log mode, logs and returns False."""
    if ask_recap_mode() == "enforce":
        return True
    print("ask-recap would-refuse door=%s thread=%s: %s"
          % (door, str(thread_id)[:120], issues[0].split(":")[0]), file=sys.stderr, flush=True)
    return False


def _ask_meta_to_store(existing_json, key, subject, recap, requires=()):
    """The ask_meta to write with an ask whose question key is `key`. New words replace the old;
    none sent keeps the old only while they were written for this same question. `requires`
    (the four-test's T1 markers) follows that rule on its own: sent replaces, not sent keeps the
    markers stated for this same question, and a different question never inherits them."""
    old = _jloads(existing_json, {}) or {}
    same = old.get("key") == key
    if not (subject or recap) and same:
        subject, recap = old.get("subject") or "", old.get("recap") or ""
    requires = list(requires or ())
    if not requires and same:
        requires = [m for m in (old.get("requires") or []) if isinstance(m, str)]
    if not (subject or recap or requires):
        return ""
    meta = {"subject": subject, "recap": recap, "key": key}
    if requires:
        meta["requires"] = requires
    return json.dumps(meta)


def thin_context_error(kind, context):
    """The 400 detail for a needs-you question that carries no background, or ''.

    Only choice and text asks: they want a decision, and a decision needs to
    know what it is about. Whitespace does not count toward the floor.
    """
    if kind not in ("choice", "text"):
        return ""
    n = len(" ".join(str(context or "").split()))
    if n >= MIN_ASK_CONTEXT:
        return ""
    return ("ask.context is %d chars, min %d for a needs-you %s ask: he reads this card "
            "alone, without the history or your notes, so it has to carry its own "
            "background. Write 2-4 plain sentences: what this is about, what happened, "
            "and what rides on his answer; if he has to do something by hand, add the "
            "steps (how_to). Use ask.kind 'ack' if reading it is the whole job"
            % (n, MIN_ASK_CONTEXT, kind))


def context_tokens(context):
    """The list entries this card DEFINES on itself, lowercased.

    `ask.context` is the one legitimate way to keep a lettered list: put the
    list on the card. Then "C+D" is readable in the screenshot, because C and D
    are in the screenshot. A letter mentioned but never defined does not count
    -- that is the same riddle with extra words in front of it.
    """
    text = str(context or "")
    return {m.group(1).lower() for m in _CONTEXT_DEF.finditer(text)}


def _tokens_of(label):
    """The list tokens a label is made ENTIRELY of, or None if it is real text."""
    m = _TOKEN_LIST_ONLY.match(label or "")
    if not m:
        return None
    raw = (m.group(1) + (m.group(2) or "")).strip()
    return [t.lower() for t in _TOKEN_SPLIT.split(raw) if t.strip()]


def self_contained_errors(options, prompt="", context=""):
    """Every reason this choice ask would not survive being screenshotted alone.

    Returned as a list so the producer's door can attach them to one rejected
    thread while the rest of a payload lands, and so a producer can call this
    itself before sending anything.

    `context` is the card's own copy of whatever list the options refer to. It
    is the ONLY thing that licenses a lettered label, and only for the letters
    it actually defines, because it is the only thing that is still there when
    the card is looked at alone.
    """
    errors = []
    if not str(prompt or "").strip():
        errors.append("ask.kind is 'choice' but ask.prompt is empty: the prompt is what the "
                      "buttons are answering. A row of options with no question on the same "
                      "card cannot be answered by someone who is only looking at the card")
    context = str(context or "")
    if len(context) > MAX_ASK_CONTEXT:
        errors.append("ask.context is %d chars, max %d: it renders on the card, so it has to "
                      "be readable there" % (len(context), MAX_ASK_CONTEXT))
    defined = context_tokens(context)
    # A context that itself points offstage ("A) as discussed with the vendor")
    # has moved the riddle rather than solved it.
    for rx, why in _EXTERNAL_REF:
        m = rx.search(context)
        if m:
            errors.append("ask.context %s (%r). The context is what makes the letters "
                          "readable on the card; it cannot itself refer to something that is "
                          "not on the card" % (why, m.group(0)))
            break

    seen = {}
    for i, o in enumerate(options or []):
        label = str((o or {}).get("label") or "").strip()
        effect = str((o or {}).get("effect") or "").strip()
        key = label.lower()
        if key in seen:
            errors.append("ask.options[%d] label %r repeats ask.options[%d]: two buttons that "
                          "read identically cannot be told apart. Say what is different about "
                          "each one" % (i, label, seen[key]))
        else:
            seen[key] = i
        if key in _DEICTIC_ONLY:
            errors.append("ask.options[%d] label %r is a pointer word with nothing on the card "
                          "to point at. Write what picking it DOES, e.g. 'Renew the cert "
                          "today'" % (i, label))
            continue
        if _INDEX_ONLY.match(label):
            errors.append("ask.options[%d] label %r is a position in a list the reader cannot "
                          "see. Label it with the action, not its number: an option has to be "
                          "readable in a screenshot of this card alone" % (i, label))
            continue
        # Labels that are nothing but list tokens, including combinations.
        toks = _tokens_of(label)
        if toks is not None:
            missing = [t for t in toks if t not in defined]
            if missing:
                errors.append(
                    "ask.options[%d] label %r names %s from a list that exists only outside "
                    "this card. Either write what the option DOES (\"Renew the cert and "
                    "rotate the key\"), or put the list itself in ask.context (\"A) renew "
                    "... B) rotate ...\") so the letters are readable in a screenshot of "
                    "this card alone"
                    % (i, label, ", ".join(repr(t.upper()) for t in missing)))
                continue
            # Licensed by the card's own list: nothing further to check on a
            # label that is only those tokens.
            continue
        # A letter or number named inside a longer sentence.
        m = _OPTION_REF.search(label) or _OPTION_REF.search(effect)
        if m and m.group(1).lower() not in defined:
            where = "label" if _OPTION_REF.search(label) else "effect"
            errors.append(
                "ask.options[%d] %s %r names an option by number or letter (%r) instead of by "
                "what it does, and no list on this card defines it. Restate the thing itself, "
                "or put the list in ask.context so %r is readable here"
                % (i, where, label if where == "label" else effect, m.group(0),
                   m.group(1).upper()))
            continue
        for rx, why in _EXTERNAL_REF:
            m = rx.search(label)
            where = "label"
            if not m:
                m = rx.search(effect)
                where = "effect"
            if m:
                errors.append("ask.options[%d] %s %r %s (%r). Options must survive being "
                              "screenshotted alone: restate the thing itself instead of "
                              "referring to it"
                              % (i, where, label if where == "label" else effect, why,
                                 m.group(0)))
                break
    return errors


# --- The needs-you four-test (contract: P3b §1; LESSONS 3, 14, 15, 33) --------
#
# The fleet's filing rule, as code: an ask pages the owner ONLY if ALL FOUR hold --
# only-he-can, still-true-now, he-doesnt-know, agent-cannot-fix. Any test
# failing means the work is NOT his: the ladder continues or the thread settles
# elsewhere. This is the pitfall report's fix made structural (71.5% of the
# retired board's cards were system-generated); a rule a prompt has to remember
# is a rule the next prompt forgets (LESSON 2: topology beats prompt changes).
#
# Every test is deterministic -- markers, timestamps, token fingerprints,
# budgets. No embeddings, no model: freshness/dedup/urgency are FACTS about the
# data, and facts get code (LESSON 33).
REQUIRES_MARKERS = ("decision", "funds", "credential", "presence", "contact")

# T4's ladder context, at the doors that have one. None = the caller is not the
# ladder (a producer PUT): T4 is embodied there by the ported invariants rather
# than re-derived, so a producer is never refused for "retry budget" it does
# not have.
MAX_RETRY_ATTEMPTS_CONTEXT = 5

# How old a closed duplicate may be and still count as "he has seen this
# concern recently". Same horizon as the park contract's expiry: a month.
DUPE_WINDOW_S = 30 * 86400

# The refusal detail per test: names the test AND the fix, because the reader
# is a producer that has to correct its own payload (P1 §6).
_NJ_FIX = {
    "T1-only-he-can": (
        "send ask.requires naming which only-he-can capability the next step "
        "needs, one of %s. If the next step is none of those, the work is not "
        "his: keep climbing the ladder (retry, park with a blocker, or settle)"
        % ", ".join(REQUIRES_MARKERS)),
    "T2-still-true-now": (
        "he has replied since this ask was generated, so the ask speaks about a "
        "round he already answered. Do not re-ask: act on his reply (it re-queues "
        "the work) or report on it"),
    "T3-he-doesnt-know": (
        "he already has an ask open (or recently settled) covering this same "
        "concern, so a second card adds noise, not information. Route the work "
        "through the existing thread's stable id"),
    "T4-agent-cannot-fix": (
        "the ladder is not exhausted: retry budget or a self-fix path remains "
        "(other executor, re-check, wait for a dependency). Only genuinely "
        "agent-unfixable work pages him"),
}


def needs_owner(thread, context=None):
    """The four-test. Returns (needs_you: bool, failed_test: str).

    Pure decision over supplied data; SQL only through the optional `con` in
    the context, and only to complete a dedupe scan the caller did not. Tests
    run in order T1..T4 and the first failure is returned -- a producer fixing
    its payload wants the one reason, not four.
    """
    context = context or {}
    t = thread if isinstance(thread, dict) else {}
    ask = t.get("ask") or {}
    if not isinstance(ask, dict):
        ask = {}
    tid = str(t.get("thread_id") or "")
    ask_ts = float(context.get("ask_ts") or 0)
    now_ = float(context.get("now") or time.time())

    # T1 only-he-can: the next step requires his decision, their funds, his
    # account/credential, their physical presence, or a person only he can
    # contact. Evidence is EXPLICIT markers the producer states; absent
    # markers mean T1 fails -- a producer that cannot name the capability is
    # a producer that has not established it (LESSON 15's evidence rule).
    requires = ask.get("requires")
    if isinstance(requires, str):
        requires = [requires]
    markers = {str(m).strip().lower() for m in (requires or []) if str(m).strip()}
    markers &= set(REQUIRES_MARKERS)   # unknown markers die at validation (§1.3)
    if not markers:
        return False, "T1-only-he-can"

    # T2 still-true-now: no newer event contradicts the ask. His reply, his
    # note, or his settle on THIS thread after the ask's generation means the
    # ask describes a round he already acted on.
    owner_ts = float(context.get("owner_event_ts") or 0)
    if ask_ts and owner_ts > ask_ts:
        return False, "T2-still-true-now"

    # T3 he-doesnt-know: no open/recently-closed duplicate ask, no
    # answered-decision covering it, no ops-log hit. Dedupe by stable id and
    # deterministic title fingerprint -- the same normalizer the mute surface
    # uses (numbers/units/stopwords stripped), proven against re-derived
    # concerns whose titles differ only by measurement.
    dupes = context.get("duplicates")
    if dupes is None and context.get("con") is not None:
        con = context["con"]
        fp = fingerprint(str(t.get("title") or ""))
        dupes = []
        for r in con.execute(
                "SELECT thread_id, title, state, ask_kind, ask_prompt, last_change"
                " FROM threads WHERE tombstoned=0 AND suppressed=0").fetchall():
            if r["thread_id"] != tid and fingerprint(r["title"]) == fp:
                dupes.append(dict(r))
    for d in dupes or []:
        state = d.get("state")
        if state == "needs-you" and (d.get("ask_kind") or "none") != "none":
            return False, "T3-he-doesnt-know"   # open ask OR answered decision
        if state == "done" and (now_ - float(d.get("last_change") or 0)) <= DUPE_WINDOW_S:
            return False, "T3-he-doesnt-know"   # recently settled
    if context.get("ops_log_hits"):
        return False, "T3-he-doesnt-know"

    # T4 agent-cannot-fix: no retry budget left AND no self-fix path applies.
    # `ladder` None = the caller is not the ladder; the test passes for it
    # (the ported invariants embody T4 at that door, contract §1.2).
    ladder = context.get("ladder")
    if ladder is not None:
        budget_left = int(ladder.get("retry_budget_left") or 0)
        self_fix = ladder.get("self_fix_paths") or []
        if budget_left > 0 or self_fix:
            return False, "T4-agent-cannot-fix"

    return True, ""


def assert_needs_owner(thread, context=None):
    """The four-test as a rejection. Raises StoreError(400) naming the failed
    test and the fix; returns quietly when the ask may page him."""
    ok, failed = needs_owner(thread, context)
    if ok:
        return
    raise StoreError(400, "needs-you refused: %s: %s" % (failed, _NJ_FIX[failed]),
                     {"failed_test": failed})


def _nj_ask_of(thread_payload):
    """The four-test's view of an ask payload: requires markers included."""
    ask = thread_payload.get("ask") if isinstance(thread_payload.get("ask"), dict) else {}
    return {"kind": ask.get("kind"), "prompt": ask.get("prompt"),
            "options": ask.get("options") or [], "requires": ask.get("requires")}


def needs_owner_gate_errors(con, c, existing, now=None):
    """The four-test plus the cold-read gate (T5), at the producer's door.

    Returns [] (may light) or [detail]. Called by put_threads only for a NEW
    ask reaching the board (contract
    §1.2): a fresh lit thread, or re-lighting one whose stored ask is 'none'.
    A thread already carrying an ask under the same stable id is a licensed
    resync -- P1 invariant 3 still protects whatever he has answered -- so it
    is not re-interrogated.
    """
    now = time.time() if now is None else now
    tid = c["thread_id"]
    owner = con.execute(
        "SELECT MAX(ts) FROM events WHERE thread_id=? AND (kind='owner-reply'"
        " OR kind='owner-note' OR (kind='state-change' AND source='owner'))",
        (tid,)).fetchone()[0] or 0
    fp = fingerprint(c["title"])
    dups = []
    for r in con.execute(
            "SELECT thread_id, title, state, ask_kind, ask_prompt, last_change"
            " FROM threads WHERE tombstoned=0 AND suppressed=0 AND thread_id != ?",
            (tid,)).fetchall():
        if fingerprint(r["title"]) == fp:
            dups.append(dict(r))
    thread = {"thread_id": tid, "title": c["title"], "ask": _nj_ask_of({"ask": {
        "kind": c["ask_kind"], "prompt": c["ask_prompt"],
        "options": c["ask_options"], "requires": c.get("requires")}})}
    ctx = {"existing": dict(existing) if existing is not None else None,
           "owner_event_ts": float(owner), "ask_ts": now, "duplicates": dups}
    ok, failed = needs_owner(thread, ctx)
    if ok:
        # T5 cold-read (helm/cold_read.py): the visible text names the exact step, and the
        # step is his. Deterministic pre-checks first (no model); the judge fails open.
        cold = _cold_read.cold_read_errors(
            c.get("ask_kind") or "none", str(c.get("ask_subject") or ""),
            str(c.get("ask_prompt") or ""), c.get("ask_options") or [],
            str(c.get("ask_recap") or ""))
        if cold:
            return ["needs-you refused: T5-cold-read: %s" % cold[0]]
        return []
    return ["needs-you refused: %s: %s" % (failed, _NJ_FIX[failed])]


# --- The bare-ask rules (contract: P3b §4; LESSONS 15, 11, 13, 3) -------------
#
# The retired board's worst card was lit, optionless, and asked nothing: "what
# do you want". Multiple choice or nothing (LESSON 15); a note that is a
# statement is not an ask (LESSON 11); reading it is a tap, not a decision
# (LESSON 13); Helm asks only what needs the owner (LESSON 3).
BANNED_ASK_PHRASE = "what do you want"

# A prompt that is NOTHING but a demand for direction, whole-prompt match.
# Deliberately small (contract §open-questions 8): under-matching leaves one
# odd text ask standing; over-matching eats a real question, which is the
# failure P1 §2 exists for. Same direction as the '?' detector.
_PURE_DIRECTION = re.compile(
    r"^(?:what|how)\s+(?:do|should|would|shall)\s+(?:you|we|i)\s+"
    r"(?:want|do|proceed|handle|act)(?:\s+(?:about|with|now|next|this))?\??$"
    r"|^what\s+(?:now|next)\??$"
    r"|^your\s+call\??$"
    r"|^(?:awaiting|await)\s+(?:your\s+)?(?:decision|instructions|input)\.?$"
    r"|^tell\s+me\s+what\s+to\s+do\??$"
    r"|^(?:how|what)\s+next\??$", re.I)


def bare_ask_errors(kind, prompt, options, context=""):
    """Every reason this ask is the optionless card, at any door."""
    errors = []
    hay = " | ".join([str(prompt or ""), str(context or "")] +
                     [str((o or {}).get("label") or "") + " " +
                      str((o or {}).get("effect") or "") for o in (options or [])])
    if BANNED_ASK_PHRASE in hay.lower():
        errors.append(
            "this ask lights the exact card the optionless-ask rule exists to stop: "
            "the phrase %r is banned in every stored ask (prompt, option, context, "
            "at either door). Multiple choice or nothing: give each option as an "
            "action he can read alone, or send an ack if reading it is the whole job"
            % BANNED_ASK_PHRASE)
    return errors


# One card, one decision (the owner, 2026-10-03; Playbooks/Talking To the owner section 6). A tap
# answers one question, so a card that puts several behind it leaves the rest to be
# answered as free text. The shape has four deterministic tells; none needs a model.
_DECISION_COUNT = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
    r"(?:(?:open|separate|different)\s+)?(?:calls|decisions|questions|asks|choices)\b", re.I)
_ANSWER_BY_NUMBER = re.compile(
    r"\b(?:reply|answer|respond|tell me|say)\b[^.\n]{0,60}\bby (?:number|line|item)\b", re.I)
_NUMBERED_LINE = re.compile(r"^\s*\d+[.)]?\s", re.M)


def one_decision_errors(kind, prompt, options, context=""):
    """Every reason this choice ask carries more than one decision, at any door."""
    if kind != "choice":
        return []
    prompt, context = str(prompt or ""), str(context or "")
    tells = []
    if _DECISION_COUNT.search(prompt):
        tells.append("the prompt announces a count of decisions")
    if _ANSWER_BY_NUMBER.search(context):
        tells.append("the context sends answers outside the buttons ('by number')")
    recommended = [ln for ln in context.splitlines()
                   if _NUMBERED_LINE.match(ln) and "recommended" in ln.lower()]
    if len(recommended) >= 2:
        tells.append("%d numbered lines in the context each carry their own "
                     "recommended answer" % len(recommended))
    if len(re.findall(r"\?(?=\s|$)", prompt)) >= 2:
        tells.append("the prompt asks more than one question")
    if not tells:
        return []
    return ["this card carries more than one decision (%s). One card, one decision "
            "(Playbooks/Talking To the owner section 6): decide the items that are not his "
            "and report them, and send each decision that is his as its own card"
            % "; ".join(tells)]


def pure_direction_demand(prompt):
    """True when the whole prompt is a demand for direction with no substance.

    The shape the rule is for: a run at the end of its rope typing 'how should
    we proceed?' -- which looks like a question and is actually the thinking
    being offloaded. A REAL question ('Which certificate did you mean, the
    relay or the site one?') does not match and stands.
    """
    return bool(_PURE_DIRECTION.match(str(prompt or "").strip()))


def rewrite_to_ack_summary(outcome, note):
    """The deterministic rewrite of a pure direction-demand into an honest
    card: what stopped, where, and that any reply reopens it. Never a
    question, never 'what do you want'."""
    clip = " ".join(str(note or "").split())[:200]
    return ("Work stopped at %s: %s — next options unknown. Reply anything to "
            "reopen." % (str(outcome or "an impasse")[:40], clip))


# --- The bot-close rules (contract: P1 §4; LESSONS 13, 18, 12, 10) -----------
#
# Only the owner closes. A thread closed by a bot is work that disappeared off the
# list without anyone agreeing it was finished, and the whole point of the list
# is that it tells the truth about what is outstanding. There are exactly two
# mechanical exceptions, and both are cases where HE already did the closing and
# the store is only catching up with him:
#
#   ack-tapped        -- the ask was "read this" and he tapped it.
#   answered-decision -- he answered a choice/text ask, the run applied that
#                        answer, and nothing is left owed to him.
#
# Everything else is a 400. A redirect on an ack card is not an ack (LESSON 13):
# it is him sending the run off to do something else, which is the opposite of
# "we are finished here".
#
# Bodies that are a tap, not a decision. "close" and "ack" are buttons; an empty
# reply carries a note only; "__redirect__" is him asking for something ELSE,
# which is the loudest possible sign that something is still owed.
NON_DECISION_REPLIES = {"ack", "close", "note-only", "__redirect__", ""}


def _generation_start(con, thread_id) -> float:
    """When this thread's CURRENT life began.

    A thread id is stable across passes, so a concern that was retired and
    later refiled under the same id would otherwise inherit the previous
    life's reply and close itself on an answer to a question nobody asked
    this time round.
    """
    row = con.execute(
        "SELECT MAX(ts) FROM events WHERE thread_id=? AND kind='state-change'"
        "   AND (body LIKE 'retired%' OR body LIKE 'auto-retired%'"
        "        OR body LIKE 'reopened by you%')", (thread_id,)).fetchone()
    return float(row[0] or 0)


def newest_ask_ts(con, thread_id, since=0.0) -> float:
    """When the live question was last asked, from the event log.

    `ask_prompt` is a mutable column: the next result overwrites it, and then
    "did he reply AFTER the ask" is unanswerable one generation back. The
    bot-ask event is the durable record of the question having been put.
    """
    row = con.execute(
        "SELECT MAX(ts) FROM events WHERE thread_id=? AND kind='bot-ask' AND ts > ?",
        (thread_id, since)).fetchone()
    return float(row[0] or 0)


def _nothing_owed_close(con, row, gen, replies, answer_applied, nothing_owed) -> bool:
    """H7: nothing was ever asked of him on this thread (no question was ever lit on the row,
    none is held for a wake or by the gate, no bot-ask event in this generation) and he never
    replied in this generation, and the run asserts both answer_applied and nothing_owed: there
    is no question of his a close could bury, so the run may close it. ask_lit_at is the row's
    durable record that a question was lit: a PUT-lit ack writes no bot-ask event, and a park
    blanks the ask columns, but ask_lit_at is never cleared."""
    if not (answer_applied is True and nothing_owed is True):
        return False
    if replies or str(row["ask_kind"] or "none") in ("ack", "choice", "text"):
        return False
    if float(row["ask_lit_at"] or 0) > 0 or (row["wake_ask"] or "") not in ("", "{}") \
            or (row["gate_hold_ask"] or "") not in ("", "{}"):
        return False
    return newest_ask_ts(con, row["thread_id"], gen) == 0


def bot_close_reason(con, row, answer_applied=None, nothing_owed=None,
                     no_action=None):
    """(reason, refusal) -- which mechanical exception permits this close.

    Returns (None, detail) when none of them does, with a detail that names the
    two ways through and the one the caller is closest to.
    """
    tid = row["thread_id"]
    if row["state"] == "done":
        # Already closed, by him or by a tap that settled. A late result from a
        # run that was still in flight is not a bot closing anything, and
        # refusing it would make a finished run look failed.
        return "already-closed", ""
    gen = _generation_start(con, tid)
    replies = con.execute(
        "SELECT ts, body FROM events WHERE thread_id=? AND kind='owner-reply' AND ts > ?"
        " ORDER BY ts ASC", (tid, gen)).fetchall()
    kind = row["ask_kind"]

    # Contract: P7 §2 — the terminal no-action verdict. The run asserts, as a
    # literal boolean recorded as data (never read out of prose, LESSON 12),
    # that his instruction required no thread action beyond recording it. The
    # instruction itself is the evidence: a reply from him newer than the
    # current ask — INCLUDING a note-only reply, which is binding (LESSON 15);
    # the observed wedge (2026-09-13: owner-20260912-pip claimed 18 times,
    # t-p3-e2e-scratch-20260912 burned 6 rounds) was exactly a note-shaped
    # instruction the executor rightly read as "record, don't act" and had no
    # honest way to settle. The answer gate and the note floor have ALREADY run
    # by the time this is called, and are not exempted.
    if no_action:
        ask_ts = newest_ask_ts(con, tid, gen)
        if any(r["ts"] > ask_ts for r in replies):
            return "no-action-settle", ""
        if _nothing_owed_close(con, row, gen, replies, answer_applied, nothing_owed):
            return "nothing-owed", ""
        return None, (
            "'no_action' settles a thread on the strength of HIS reply (a "
            "note-only reply is binding), but this thread has no reply from him "
            "newer than the question on it — there is nothing his words "
            "instructed that 'no action' could settle. Report with state "
            "'needs-you' and an ask instead")

    if kind == "ack":
        if any((r["body"] or "").strip().lower() == "ack" for r in replies):
            return "ack-tapped", ""
        return None, (
            "a bot may not set 'done'; only the owner closes a thread. This one carries an 'ack' "
            "ask that he has not tapped"
            + (" (his last reply was %r, which is him sending you somewhere else, not "
               "agreeing this is finished)" % (replies[-1]["body"] or "")[:40]
               if replies else "")
            + ". Report with state 'needs-you' and keep the ack: the close is his tap")

    if kind in ("choice", "text"):
        ask_ts = newest_ask_ts(con, tid, gen)
        decided = [r for r in replies
                   if r["ts"] > ask_ts
                   and (r["body"] or "").strip().lower() not in NON_DECISION_REPLIES]
        if not decided:
            return None, (
                "a bot may not set 'done'; only the owner closes a thread. The mechanical "
                "exception for an answered decision does not apply: there is no reply from "
                "him newer than the question currently on this card (a bare 'ack'/'close' "
                "tap or a note with no choice in it is not a decision). Report with state "
                "'needs-you' and leave the question up")
        if not (answer_applied and nothing_owed):
            return None, (
                "a bot may not set 'done' on an answered decision unless it states, "
                "explicitly, that it finished the job: send answer_applied: true AND "
                "nothing_owed: true in this result (got answer_applied=%r, "
                "nothing_owed=%r). Completion is never read out of the note's prose -- if "
                "either is not true, this is 'needs-you' with an ask or "
                "'waiting-external' with a blocker"
                % (answer_applied, nothing_owed))
        return "answered-decision", ""

    if _nothing_owed_close(con, row, gen, replies, answer_applied, nothing_owed):
        return "nothing-owed", ""

    return None, (
        "a bot may not set 'done'; only the owner closes a thread. This one has no ask on it for "
        "him to have answered or acknowledged (state %r, ask %r), so there is nothing that "
        "could have closed it but you. If the work really is finished, put the ack ask IN "
        "this post -- state 'needs-you' with ask.kind 'ack' -- and his tap closes it"
        % (row["state"], kind))


def _bool_flag(body, key):
    """An assertion, or None when the caller did not make one.

    Strings are refused on purpose: "true", "yes" and "done" are prose, and
    reading completion out of prose is the failure this exists to stop
    (LESSON 12).
    """
    if key not in body or body[key] is None:
        return None
    v = body[key]
    if isinstance(v, bool):
        return v
    raise StoreError(400, "%r is %r; it must be a literal true or false, not a string or a "
                          "number. It is an assertion you are making about the work, and it "
                          "is recorded as one" % (key, v))


def _ask_event_body(kind, prompt, options, context=""):
    """The question, written into history the way a reader reads it."""
    lines = ["ASKED YOU (" + kind + "): " + (prompt or "").strip()]
    if context.strip():
        lines.append(context.strip())
    for o in options or []:
        label = str((o or {}).get("label") or "").strip()
        effect = str((o or {}).get("effect") or "").strip()
        lines.append("- " + label + ((" -- " + effect) if effect else ""))
    return "\n".join(lines)


# --- The answer gate (contract: P1 §2; LESSON 16) ----------------------------
#
# The binding note rides with an answer and is read by whoever executes it. A
# QUESTION inside that note had nowhere to be answered: the run did the work,
# reported what it did, and the question was never mentioned again -- so the card
# looked dealt with while the one thing actually asked for was dropped.
#
# Detection is a regex, not a model (LESSON 33): the sentence ends in '?', that
# is the whole test. Over-triggering costs a run one sentence; under-triggering
# loses the question.
MIN_ANSWER = 12
_QUESTION = re.compile(r"[^.!?\n]*\?")


def questions_in(text):
    """Every question-shaped fragment in a note, verbatim, for quoting back."""
    return [q.strip() for q in _QUESTION.findall(str(text or "")) if q.strip()]


def normalize_answers(raw):
    """Coerce `answers` into the block rendered above the report.

    Accepts a string (the answer, as prose), a {question, answer} object, or a
    list of either. Returns (text, errors). The pair form is preferred when more
    than one thing was asked, because an unlabelled wall of prose answering three
    questions is indistinguishable from one answering none of them.
    """
    errors = []
    if raw is None:
        return "", errors

    def one(item, where):
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            q = str(item.get("question") or "").strip()
            a = str(item.get("answer") or "").strip()
            if not a:
                errors.append("answers%s has no 'answer': send {question, answer} with the "
                              "actual reply in 'answer'" % where)
                return ""
            return ("Q: " + q + "\nA: " + a) if q else a
        errors.append("answers%s is %s, expected a string or {question, answer}"
                      % (where, type(item).__name__))
        return ""

    if isinstance(raw, list):
        if not raw:
            errors.append("'answers' is an empty list, which answers nothing. Send the "
                          "reply as a string, or [{question, answer}, ...]")
            return "", errors
        parts = [p for p in (one(it, "[%d]" % i) for i, it in enumerate(raw)) if p]
        return "\n\n".join(parts), errors
    if isinstance(raw, (str, dict)):
        return one(raw, ""), errors
    errors.append("'answers' is %s, expected a string or a list of {question, answer}"
                  % type(raw).__name__)
    return "", errors


def unanswered_questions(con, thread_id):
    """Questions in notes the owner has written since the last report landed.

    "Since the last report" is what makes a question consumable: once a result
    lands, the notes it was answering are behind it, and a later run is not asked
    to re-answer them. Notes with no question mark contribute nothing.
    """
    last_report = con.execute(
        "SELECT MAX(ts) FROM events WHERE thread_id=? AND kind='bot-result'",
        (thread_id,)).fetchone()[0] or 0
    rows = con.execute(
        "SELECT body FROM events WHERE thread_id=? AND kind='owner-note' AND ts > ?"
        " ORDER BY ts ASC", (thread_id, last_report)).fetchall()
    out = []
    for r in rows:
        out.extend(questions_in(r["body"]))
    return out


# --- Identifiers that become path segments -----------------------------------
#
# A thread_id is not free text. It is a producer-supplied string that the API
# layer joins straight onto the attachment root as a directory name, and an
# att_id is joined onto that as a file name. A closed character set here is the
# only thing between a producer token and a write outside that root: Starlette's
# default path converter is [^/]+, which matches '..' happily, and a client that
# does not collapse dot-segments before sending (curl --path-as-is, any raw HTTP
# client) hands it through intact. The sibling `label` field has been checked
# against LABEL_NAME_RE since P6; this was simply forgotten.
#
# Deliberately an allowlist, not a hunt for '..' and '/': traversal has more
# spellings than anyone enumerates correctly, and every id the board actually
# mints -- 't-quota-floor-guard', 'sweep-stack-status-20260919',
# 'sched-<job>-failure-<date>', 'lesson-<12 hex>', _mint_hand_id's
# 'owner-<date>-<slug>' -- is already inside this set. \Z rather than $ because
# '$' in Python also matches before a trailing newline.
IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
IDENT_RULE = "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
# Work Helm never checked its ids, so a work database carries ids like
# 'call-1180', 'cc-2026-09-03-helm-...' and, in principle, dots and colons. At work
# the id must only be safe as a path segment: no separator, no NUL, no leading dot.
WORK_IDENT_RE = re.compile(r"[A-Za-z0-9][^/\\\x00-\x20\x7f]{0,199}\Z")
MAX_IDENT_BYTES = 200   # a directory name under the attachment root; well under the 255 limit


def ident_ok(ident: str) -> bool:
    """Is this id safe to become a path segment, per the site's strictness?"""
    if rules().strict_ident:
        return bool(IDENT_RE.match(ident or ""))
    s = ident or ""
    return (bool(WORK_IDENT_RE.match(s)) and ".." not in s
            and len(s.encode("utf-8")) <= MAX_IDENT_BYTES)


EVENT_TS_MAX = 4102444800.0  # 2100-01-01 UTC: past that is milliseconds, not seconds


def event_ts_error(ts):
    """Why a producer event's `ts` cannot be stored, or None. A falsy ts means
    'now' (the caller's default); anything else must be epoch seconds, as a
    number or a numeric string, finite and between 1970 and 2100. An ISO
    string used to reach float() in add_event mid-write and 500 the payload."""
    if not ts:
        return None
    if isinstance(ts, bool) or not isinstance(ts, (int, float, str)):
        return "must be epoch seconds (a number), got " + type(ts).__name__
    try:
        v = float(ts)
    except (ValueError, OverflowError):  # OverflowError: an int too big for a float
        return "must be epoch seconds (a number), got %r; send e.g. 1759572000.5" % str(ts)[:40]
    if not math.isfinite(v) or v < 0 or v > EVENT_TS_MAX:
        return "must be epoch seconds between 1970 and 2100, got %r (milliseconds?)" % str(ts)[:40]
    return None


def validate_thread(t):
    """Pure validation and normalization. Returns (clean, errors).

    Anything outside a valid set is an error rather than a silent coercion:
    a wrong urgency quietly rewritten to 'whenever' is a lie the store then
    serves forever.

    thread_id is REQUIRED. The reference implementation fell back to a fresh
    uuid4() when it was missing, which means a producer that forgets it emits
    a complete set of duplicates on every single pass.
    """
    errors = []
    if not isinstance(t, dict):
        return None, ["thread is " + type(t).__name__ + ", expected object"]

    tid = str(t.get("thread_id") or "").strip()
    if not tid:
        errors.append("thread_id is required and must be stable across passes for the same "
                      "underlying concern; without it every pass creates duplicates")
    elif not ident_ok(tid) and not rules().strict_ident:
        errors.append("thread_id %r is not safe as a path segment: no '/', '\\', NUL, '..' or a "
                      "leading dot, at most 200 chars (it names the thread's attachment folder)"
                      % tid[:80])
    elif not ident_ok(tid):
        errors.append("thread_id %r is not an identifier: it must match %s -- letters, "
                      "digits, '-' and '_', starting with a letter or a digit. The id is "
                      "used verbatim as a directory name under the attachment root, so a "
                      "separator, a leading dot or a '..' would put a producer's uploads "
                      "somewhere on the disk nobody chose. Use the shape the rest of the "
                      "board uses: 't-quota-floor-guard', 'sweep-stack-status-20260919'"
                      % (tid[:80], IDENT_RULE))

    urgency = str(t.get("urgency") or "whenever")
    if urgency not in VALID_URGENCY:
        errors.append("urgency %r not one of %s" % (urgency, sorted(VALID_URGENCY)))

    state = str(t.get("state") or "needs-you")
    if state not in VALID_STATE:
        errors.append("state %r not one of %s" % (state, sorted(VALID_STATE)))

    ask = t.get("ask")
    if ask is None:
        ask = {}
    if not isinstance(ask, dict):
        errors.append("ask is " + type(ask).__name__ + ", expected object")
        ask = {}

    kind = str(ask.get("kind") or "none")
    if kind not in VALID_ASK:
        errors.append("ask.kind %r not one of %s" % (kind, sorted(VALID_ASK)))

    # The four-test's T1 evidence (contract: P3b §1.3). Producers state which
    # only-he-can capability the next step needs, as data. Unknown markers are
    # a loud 400 naming the set, never a silent T1 failure on a typo'd value.
    requires = ask.get("requires")
    if isinstance(requires, str):
        requires = [requires]
    if requires is not None and not isinstance(requires, list):
        errors.append("'ask.requires' is %s, expected a marker or a list of markers "
                      "from %s (contract: P3b §1.3: state which only-he-can "
                      "capability the next step needs)"
                      % (type(requires).__name__, list(REQUIRES_MARKERS)))
        requires = None
    if requires is not None:
        bad = [m for m in requires
               if not isinstance(m, str) or m.strip().lower() not in REQUIRES_MARKERS]
        if bad:
            errors.append("'ask.requires' has unknown marker(s) %r; valid: %s. A "
                          "marker names the only-he-can capability the NEXT step "
                          "needs -- decision, funds, credential, presence, or "
                          "contact. No markers at all is legal but the four-test "
                          "will refuse the ask (T1)"
                          % (bad[:3], list(REQUIRES_MARKERS)))

    rl = rules()
    ask_prompt = str(ask.get("prompt") or "")
    if rl.strict_limits and len(ask_prompt) > MAX_ASK_PROMPT:
        errors.append("ask.prompt is %d chars, max %d" % (len(ask_prompt), MAX_ASK_PROMPT))
    ask_context = str(ask.get("context") or "")
    if len(ask_context) > MAX_ASK_CONTEXT:
        errors.append("ask.context is %d chars, max %d: it renders on the card, so it has to "
                      "be readable there" % (len(ask_context), MAX_ASK_CONTEXT))

    options, opt_errors = normalize_options(ask.get("options"))
    errors.extend(opt_errors)
    if kind == "choice":
        # Home: at least two readable options (P1 §3). Work: any usable option
        # (a one-option ack card is legal there; core-diff item 9).
        #
        # A card that is not lit (working, waiting, done) may carry the one terminal option
        # create_thread stamps on a hand-written card ("Done"): a fetched card is PUT or
        # checked back with it, and put_threads keeps the stored ask of an in-flight card
        # whatever the body says. A lit one-option choice is still a producer's own question.
        own_terminal_ack = (state != "needs-you" and len(options) == 1
                            and options[0].get("terminal") is True)
        if rl.two_option_min and len(options) < 2 and not own_terminal_ack:
            errors.append("ask.kind is 'choice' requires at least 2 options, got %d" % len(options))
        elif not options:
            errors.append("ask.kind is 'choice' but no usable options survived normalization")
        elif rl.readable_options:
            errors.extend(self_contained_errors(options, ask_prompt, ask_context))

    # The bare-ask rules, producer's door (contract: P3b §4): R1 banned phrase
    # (mirrored from the bot's door so neither door can store it), R2 a text
    # statement is not an ask, R3 a pure direction-demand is refused with the
    # fix named -- a producer can correct its payload. Home only.
    if rl.bare_ask_rules:
        errors.extend(bare_ask_errors(kind, ask_prompt, options, ask_context))
        errors.extend(one_decision_errors(kind, ask_prompt, options, ask_context))
    # The context floor, producer's door (ledger GW-062). Both sites.
    if state == "needs-you":
        thin = thin_context_error(kind, ask_context)
        if thin and _thin_context_refuses("put", tid, thin):
            errors.append(thin)
    ask_subject, ask_recap, meta_errors = ask_meta_fields(ask)
    errors.extend(meta_errors)
    if state == "needs-you" and not meta_errors:
        recap_issues = ask_recap_issues(kind, ask_subject, ask_recap)
        if recap_issues and _recap_refuses("put", tid, recap_issues):
            errors.extend(recap_issues)
    # The card-shape rules (helm/card_rules.py), producer's door. log: the card goes through and
    # put_threads records the rule ids; reject: the rule's sentence refuses it. The check route
    # reads the same verdict, so a producer sees in advance exactly what the PUT will do.
    card_check = None
    if state == "needs-you" and kind != "none" and rl.card_rules:
        card_check = card_rule_check(kind, ask_prompt, ask_context, options, ask_subject, ask_recap,
                                     _card_rules.author_of(ask.get("author"), t.get("author"),
                                                           t.get("responder")))
        errors.extend(_card_rules.reject_errors(card_check["reject"]))
    if state == "needs-you" and rl.bare_ask_rules:
        if kind == "text":
            prompt_s = str(ask.get("prompt") or "").strip()
            if not prompt_s:
                errors.append("ask.kind is 'text' but ask.prompt is empty")
            elif "?" not in prompt_s:
                errors.append(
                    "state 'needs-you' with a text ask whose prompt has no question "
                    "mark: a statement is not an ask. Use ask.kind 'ack' if reading "
                    "it is the whole job, or 'choice' with options that say what "
                    "picking them does (contract: P3b §4 R2)")
            elif pure_direction_demand(prompt_s):
                errors.append(
                    "the text ask %r is a demand for direction, not a question: it "
                    "offloads the thinking instead of asking anything. Either ask "
                    "the real question (name the options inline), or report with "
                    "ask.kind 'ack' and let the store write the summary itself "
                    "(contract: P3b §4 R3)" % prompt_s[:80])

    links, link_errors = normalize_links(t.get("links"))
    errors.extend(link_errors)

    # Invariant 4a, applied at the producer's door as well as at the bot's.
    if state == "needs-you" and kind == "none":
        errors.append("state is 'needs-you' but ask.kind is 'none': a lit thread with no "
                      "question renders as a bare Close button, which forces the work to be "
                      "discarded in order to clear it. Use 'ack' if reading it is genuinely "
                      "the whole job, or set state to 'waiting-external'")

    # The park contract, at the producer's door (contract: P1 §1). A producer
    # resyncs the same concern every pass, so if it parks a thread it must keep
    # re-stating who it waits on; a pass that stops naming the blocker is how a
    # park quietly becomes a drop.
    park = ("", 0.0, "")
    if state == "waiting-external" and rl.park_contract:
        park, park_errors = park_fields(t)
        errors.extend(park_errors)

    label = str(t.get("label") or "").strip().lower()
    if label and not LABEL_NAME_RE.match(label):
        errors.append("label must match ^[a-z0-9][a-z0-9_-]{1,31}$")

    events = t.get("events") or []
    if not isinstance(events, list):
        errors.append("events must be a list of event objects, got " + type(events).__name__)
    else:
        for n, ev in enumerate(events):
            ts_err = event_ts_error(ev.get("ts")) if isinstance(ev, dict) else None
            if ts_err:
                errors.append("events[%d].ts %s" % (n, ts_err))
            if isinstance(ev, dict) and (is_press_value(ev.get("body")) or is_press_value(ev.get("value"))):
                errors.append("events[%d] is a Use the draft press: a press is made only by the owner's signed-in "
                              "page, through the reply route, and cannot be written into a thread" % n)

    if errors:
        return None, errors

    return {
        "label": label,
        "blocked_on": park[0],
        "recheck_at": park[1],
        "recheck_given": (t.get("recheck_at") is not None
                          and not (isinstance(t.get("recheck_at"), str)
                                   and not t.get("recheck_at").strip())),
        "gate_cmd": park[2],
        "thread_id": tid,
        "title": str(t.get("title") or ""),
        "summary": str(t.get("summary") or ""),
        "category": str(t.get("category") or ""),
        "urgency": urgency,
        "state": state,
        "ask_kind": kind,
        "ask_prompt": str(ask.get("prompt") or ""),
        "ask_options": options,
        "ask_context": ask_context,
        "ask_subject": ask_subject,
        "ask_recap": ask_recap,
        # The four-test's T1 evidence: the door's evidence for the gate that fires in
        # put_threads, and stored with the question in ask_meta so a stored card re-checks
        # (contract: P3b §1.3).
        "requires": list(dict.fromkeys(
            m.strip().lower() for m in (requires or [])
            if isinstance(m, str) and m.strip().lower() in REQUIRES_MARKERS)),
        "responder": t.get("responder"),
        # The card-shape verdict (card_rule_check), recorded by the door that stores the thread.
        "card_rules": card_check,
        "sources": t.get("sources") or [],
        "links": links,
        "first_seen": t.get("first_seen"),
        "events": t.get("events") or [],
    }, []


# --- Mute fingerprints -------------------------------------------------------
# Deliberately NOT the title. Titles embed live measurements ("E: at 11.4%
# free" one day, "at 7.8%" the next), so title-based dedupe is already
# defeated in practice. Strip every number and unit before hashing.
_PRODUCER = re.compile(r"^\s*\[([^\]]{1,64})\]\s*")
_NUM_UNIT = re.compile(
    r"\b\d[\d,._]*\s*(?:%|gib|gb|mib|mb|kib|kb|tib|tb|ms|s|m|h|d|days?|hours?|mins?)\b", re.I)
_BARE_NUM = re.compile(r"\b\d[\d,._:/-]*\b")
_PUNCT = re.compile(r"[^a-z0-9\s]+")
_STOP = {"the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to", "of",
         "and", "or", "in", "on", "at", "for", "with", "by", "from", "as", "it", "its",
         "this", "that", "these", "those", "has", "have", "had", "not", "no", "but"}


def normalize_subject(title: str) -> str:
    t = _PRODUCER.sub("", title or "").lower()
    t = _NUM_UNIT.sub(" ", t)
    t = _BARE_NUM.sub(" ", t)
    t = _PUNCT.sub(" ", t)
    return " ".join(w for w in t.split() if w and w not in _STOP)


def fingerprint(title: str) -> str:
    return hashlib.sha256(normalize_subject(title).encode("utf-8")).hexdigest()[:20]


def producer_of(title: str, sources) -> str:
    m = _PRODUCER.match(title or "")
    if m:
        return m.group(1).strip().lower()
    if isinstance(sources, list) and sources:
        first = sources[0]
        if isinstance(first, dict):
            return str(first.get("source") or "unknown").lower()
    return "unknown"


# --- Reads -------------------------------------------------------------------
def _event_row(e, summary=False):
    """One event as the read API shows it. A owner-reply also carries its chosen `value` outright,
    and a report that carried a checked draft carries it as `draft`."""
    out = {"ts": e["ts"], "source": e["source"], "source_id": e["source_id"],
           "kind": e["kind"], "body": e["body"], "reply_kind": e["reply_kind"]}
    if e["kind"] == "owner-reply":
        out["value"] = e["body"]
    if e["via"]:
        # How it arrived, set by the store alone: `web-ui` is a press of the owner's signed-in page.
        out["via"] = e["via"]
        out["via_login"] = e["via_login"]
    if e["draft"]:
        try:
            out["draft"] = draft_summary(json.loads(e["draft"])) if summary else json.loads(e["draft"])
        except ValueError:
            pass
    return out


def _row_to_thread(con, r, with_events=True, summary=False):
    out = {
        "thread_id": r["thread_id"],
        "title": r["title"],
        "summary": r["summary"],
        "category": r["category"] or "Uncategorized",
        "urgency": r["urgency"],
        "state": r["state"],
        "ask": {
            "kind": r["ask_kind"],
            "prompt": r["ask_prompt"],
            "options": _jloads(r["ask_options"], []),
            # Rendered with the options, because it is the thing that makes
            # lettered options readable (contract: P1 §3).
            "context": r["ask_context"],
        },
        "responder": r["responder"],
        "sources": _jloads(r["sources"], []),
        "links": _jloads(r["links"], []),
        "first_seen": r["first_seen"],
        "last_change": r["last_change"],
        "last_note": r["last_note"],
        "wake_at": r["wake_at"],
        "parked": bool(r["parked"]),
        "parked_reason": r["parked_reason"],
        # The two assertions a run has made about this round (contract: P1 §4).
        # Rendered because they are claims someone made, and a claim that is
        # only checked and never shown is a claim nobody can dispute.
        "answer_applied": bool(r["answer_applied"]),
        "nothing_owed": bool(r["nothing_owed"]),
        # The park's machine-checkable exit, rendered with the thread so a
        # reader can tell a real wait from a dropped one without opening
        # anything (contract: P1 §1).
        "blocked_on": r["blocked_on"],
        "recheck_at": r["recheck_at"],
        "recheck_count": r["recheck_count"],
        "gate_cmd": r["gate_cmd"],
        "label": r["label"] if "label" in r.keys() else "",
    }
    if with_events:
        out["events"] = [_event_row(e, summary) for e in con.execute(
            "SELECT ts, source, source_id, kind, body, reply_kind, draft, via, via_login FROM events"
            " WHERE thread_id=? ORDER BY ts ASC", (r["thread_id"],))]
    # The draft the card offers a button for (null when none, a press already used it, or the card is
    # settled). List rows carry a summary of it; the thread GET carries the whole draft.
    out["use_draft"] = use_draft_of(con, r["thread_id"], r["state"])
    if summary:
        out["use_draft"] = draft_summary(out["use_draft"])
    last_rk = con.execute(
        "SELECT reply_kind FROM events WHERE thread_id=? AND reply_kind != '' ORDER BY ts DESC LIMIT 1",
        (r["thread_id"],)
    ).fetchone()
    out["reply_kind"] = last_rk[0] if last_rk and last_rk[0] else ""
    out["catchup"] = _catchup(con, r)
    if (r["ask_kind"] or "none") != "none" and r["ask_stale_key"] and r["ask_stale_key"] == r["ask_lit_key"]:
        out["ask"]["stale"] = True
    # The T1 markers and the cold read (subject, recap) the producer stated for the live question
    # (contract: P3b §1.3); each key is absent when none was stated, and a changed question never
    # shows its predecessor's. They ride inside `ask` so a stored card re-submitted or re-checked as
    # read carries what the card rules look for (catchup carries the same words for the readers).
    meta = _jloads(r["ask_meta"], {}) or {}
    if (r["ask_kind"] or "none") != "none" and meta.get("key") == r["ask_lit_key"]:
        if meta.get("requires"):
            out["ask"]["requires"] = list(meta["requires"])
        for field in ("subject", "recap"):
            if meta.get(field):
                out["ask"][field] = str(meta[field])
    return out


# Store-written bot-results are the board talking, not an agent reporting on the question.
_BOARD_SOURCES = ("helm-wake", "helm-gave-up", "helm-park-expiry", "helm-gate")


def _catchup(con, r):
    """How fresh the live question is, as ABSOLUTE times and counts, never prose or ages: the
    reader works out "3 days ago" from now, so a card cannot claim it was asked this morning.
      lit_at         when the current question was first put (0 when there is none)
      opened_at      when the thread itself was opened
      times_asked    how many times that same question was put (1 = once)
      checked_at     when an agent last reported on the thread (lit_at when none has)
      reports_since  how many agent reports landed since the question was put
      owner_last_at   the last time the owner did anything on a thread with a live question (0 = never)
      subject, recap what the producer said about THIS question ("" when nothing, or when the
                     question has changed since: they never outlive the question they describe)"""
    tid = r["thread_id"]
    lit = float(r["ask_lit_at"] or 0) if (r["ask_kind"] or "none") != "none" else 0.0
    base = {"lit_at": 0.0, "opened_at": float(r["first_seen"] or 0), "times_asked": 0,
            "checked_at": 0.0, "reports_since": 0, "owner_last_at": 0.0,
            "subject": "", "recap": ""}
    if lit <= 0:
        return base      # no live question: nothing below is asked of a done thread (a list read stays cheap)
    owner = con.execute("SELECT MAX(ts) FROM events WHERE thread_id=? AND source='owner'", (tid,)).fetchone()[0]
    base["owner_last_at"] = float(owner or 0)
    n, newest = con.execute(
        "SELECT COUNT(*), MAX(ts) FROM events WHERE thread_id=? AND kind='bot-result' AND ts>=?"
        " AND source NOT IN (%s)" % ",".join("?" * len(_BOARD_SOURCES)),
        (tid, lit) + _BOARD_SOURCES).fetchone()
    run = _ask_run_times(con, tid, r["ask_kind"], r["ask_prompt"])
    base.update(lit_at=lit, checked_at=max(lit, float(newest or 0)), reports_since=int(n or 0),
                times_asked=max(1, len(run)))
    meta = _jloads(r["ask_meta"], {}) or {}
    if meta.get("key") and meta.get("key") == r["ask_lit_key"]:
        base.update(subject=str(meta.get("subject") or ""), recap=str(meta.get("recap") or ""))
    return base


def categories_of(threads):
    """Computed at read time from the live set, never read from the table.
    A category is a grouping of what is actually there right now."""
    buckets = {}
    for t in threads:
        buckets.setdefault(t.get("category") or "Uncategorized", []).append(t)
    out = []
    for name, items in buckets.items():
        items = sorted(items, key=lambda x: (URGENCY_ORDER.get(x.get("urgency"), 9),
                                             -(x.get("last_change") or 0)))
        out.append({"name": name, "count": len(items),
                    "max_urgency": items[0].get("urgency") or "whenever",
                    "top_title": items[0].get("title") or ""})
    return sorted(out, key=lambda c: (URGENCY_ORDER.get(c["max_urgency"], 9), -c["count"]))


_OUTCOME_PREFIX_RE = re.compile(r"^\[[^\]]{1,40}\]\s*")


def _newest_report(con, thread_id):
    """(ts, body) of the newest report a run posted on the thread, leaving out the store's own
    wake and give-up lines. (0.0, "") when there is none."""
    r = con.execute("SELECT ts, body FROM events WHERE thread_id=? AND kind='bot-result'"
                    " AND source NOT IN ('helm-wake', 'helm-gave-up') ORDER BY ts DESC, rowid DESC LIMIT 1",
                    (thread_id,)).fetchone()
    return (r["ts"], r["body"] or "") if r else (0.0, "")


def _snooze_set_by_run(con, r):
    """(True, when) when the snooze on this due row was set by a run through /result, else
    (False, 0.0): a rule pack rechecks those and only those (the owner's own snooze from the
    page wakes as a plain reminder). The row says so itself; one that predates the column is read
    from its history: a run's report newer than the owner's last snooze means a run set it."""
    mode = r["wake_mode"] or ""
    if mode == "recheck":
        return True, float(r["wake_set_at"] or 0) or _newest_report(con, r["thread_id"])[0]
    if mode == "reminder":
        return False, 0.0
    report_ts = _newest_report(con, r["thread_id"])[0]
    owner = con.execute("SELECT MAX(ts) FROM events WHERE thread_id=? AND kind='state-change'"
                       " AND source='owner' AND body LIKE 'snoozed%'", (r["thread_id"],)).fetchone()[0] or 0
    return (report_ts > 0 and report_ts >= owner), report_ts


def _reported_while_snoozed(con, r) -> bool:
    """True when an agent reported on the thread after the owner snoozed it, so the question held for
    the wake may no longer be the live one. The snooze counted is the first of the run since the
    question was last put (a re-snooze does not hide what came in between); with no snooze line
    in the history the row's own wake_set_at stands in."""
    tid = r["thread_id"]
    asked = con.execute("SELECT MAX(ts) FROM events WHERE thread_id=? AND kind='bot-ask'", (tid,)).fetchone()[0] or 0
    since = con.execute("SELECT MIN(ts) FROM events WHERE thread_id=? AND kind='state-change' AND source='owner'"
                        " AND body LIKE 'snoozed%' AND ts>=?", (tid, asked)).fetchone()[0]
    if since is None:
        since = float(r["wake_set_at"] or 0)
    if since <= 0:
        return False
    return con.execute(
        "SELECT 1 FROM events WHERE thread_id=? AND kind='bot-result' AND ts>? AND source NOT IN (%s) LIMIT 1"
        % ",".join("?" * len(_BOARD_SOURCES)), (tid, since) + _BOARD_SOURCES).fetchone() is not None


def _recheck_note(con, r):
    """The note a run left for its own recheck, or "". A stored note that is only a copy of the
    newest report (what every run's snooze stored before the work pack stopped falling back to
    the report) is no note at all."""
    note = (r["wake_note"] or "").strip()
    if not note:
        return ""
    newest = _newest_report(con, r["thread_id"])[1].strip()
    if note in (newest, _OUTCOME_PREFIX_RE.sub("", newest).strip()):
        return ""
    return note


def wake_due(con) -> int:
    """Hand back every thread whose snooze has expired.

    Runs inside the read, so opening the app is what wakes a deferred thread.
    No cron on another host that can silently stop.

    Work rule pack (recheck_on_wake): a snooze a RUN set through /result is not handed back with
    a report, it is rechecked. The thread goes to `working` with one dated line in its history and
    is queued in pending-dispatch like an answer (reply_value `__recheck__`, an 'instruction'
    event from helm-wake carrying the run's own dated note, or nothing): the run finds out what
    is true that day and lights the card itself. The stashed ask stays on the row until the run
    reports, or until gave-up/restore puts it back. A snooze the owner set from the page, and every
    snooze at home, wakes as the plain reminder it always was.
    """
    now = time.time()
    rows = con.execute(
        "SELECT thread_id, wake_note, wake_ask, wake_mode, wake_set_at FROM threads"
        " WHERE wake_at > 0 AND wake_at <= ? AND tombstoned=0 AND suppressed=0"
        " AND state != 'done'", (now,)).fetchall()
    for r in rows:
        if rules().recheck_on_wake:
            by_run, set_ts = _snooze_set_by_run(con, r)
            if by_run:
                when = dt.datetime.fromtimestamp(set_ts or now, _LOCAL_TZ).strftime("%Y-%m-%d")
                run_note = _recheck_note(con, r)
                add_event(con, r["thread_id"], now, WAKE_SOURCE, "", "bot-result",
                          "Snoozed on %s for a recheck. Rechecking now." % when)
                add_event(con, r["thread_id"], now + 0.001, WAKE_SOURCE, "", "instruction", run_note)
                con.execute(
                    "UPDATE threads SET state='working', wake_at=0, wake_note='', wake_mode='', wake_set_at=0,"
                    " dispatch_attempts=0, last_dispatch_try=0, last_change=? WHERE thread_id=?",
                    (now, r["thread_id"]))
                continue
        ask = _jloads(r["wake_ask"], {}) or {}
        kind = str(ask.get("kind") or "ack")
        if kind not in VALID_ASK or kind == "none":
            kind = "ack"  # never wake into a bare Close button
        options, _ = normalize_options(ask.get("options"))
        prompt, options_json = str(ask.get("prompt") or ""), json.dumps(options)
        superseded = _reported_while_snoozed(con, r)
        note = (r["wake_note"] or "").strip() or (
            "Bringing this back, as you asked. %s" % (
                "Something was reported on it while it was deferred, so this question may already "
                "be answered; check the history before deciding." if superseded else
                "Nothing has changed on it since you deferred it; this is the reminder itself."))
        add_event(con, r["thread_id"], now, "helm-wake", "", "bot-result", note)
        con.execute(
            "UPDATE threads SET state='needs-you', ask_kind=?, ask_prompt=?, ask_options=?,"
            " ask_context=?, ask_stale_key=?, wake_at=0, wake_note='', wake_ask='', wake_mode='',"
            " wake_set_at=0, last_change=? WHERE thread_id=?",
            (kind, prompt, options_json, str(ask.get("context") or ""),
             _ask_key(kind, prompt, options_json) if superseded else "", now, r["thread_id"]))
    stale = []
    if rules().stale_wait_reask:
        # Work rule pack (09-24): a wait with no date, not parked, and no change
        # for a week comes back to be asked about.
        stale = con.execute(
            "SELECT thread_id, last_change FROM threads WHERE state='waiting-external'"
            " AND wake_at = 0 AND parked = 0 AND tombstoned=0 AND suppressed=0"
            " AND last_change <= ?", (now - DEFAULT_WAIT_S,)).fetchall()
        for r in stale:
            since = dt.datetime.fromtimestamp(r["last_change"], _LOCAL_TZ).strftime("%Y-%m-%d")
            add_event(con, r["thread_id"], now, "helm-wake", "", "bot-result",
                      "Checking back: this has been waiting with no date since %s, and nothing has "
                      "changed on it for a week. Tap Got it if it is finished, snooze it if it is "
                      "still waiting on someone, or park it if nothing will move." % since)
            con.execute(
                "UPDATE threads SET state='needs-you', ask_kind='ack',"
                " ask_prompt='Still waiting on this?', ask_options='[]', ask_context='',"
                " last_change=? WHERE thread_id=?", (now, r["thread_id"]))
    if rows or stale:
        con.commit()
    return len(rows) + len(stale)


def due_rechecks(con):
    """Parked threads whose recheck date has passed. Reads only, writes nothing.

    Separate from the transition on purpose: the sweeper may have to RUN a gate
    command between finding a thread and deciding what to do with it, and the
    store does not run commands (contract: P1 §6). This hands out the command as
    data; apply_recheck() takes the verdict back.

    Snoozed rows (wake_at > 0) are excluded: they already have a promised return
    date, honoured by wake_due(), and one exit per thread is the whole point.
    """
    now = time.time()
    rows = con.execute(
        "SELECT thread_id, title, blocked_on, gate_cmd, recheck_at, recheck_count,"
        " parked_since, gate_hold_ask FROM threads"
        " WHERE state='waiting-external' AND recheck_at > 0 AND recheck_at <= ?"
        "   AND wake_at = 0 AND tombstoned=0 AND suppressed=0"
        " ORDER BY recheck_at ASC", (now,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        held = d.pop("gate_hold_ask") or ""
        # For a gate hold, the card text lets the sweeper tell WHICH plugin the version belongs to.
        d["hint"] = (d["title"] + "\n" + (json.loads(held).get("prompt", "") if held else ""))[:1000]
        out.append(d)
    return out


# What a 30-day-old park becomes. It is a real ask, with real options, because
# the alternative -- re-parking it for another month -- is how a thread nobody
# is chasing outlives the reason it was opened. Each option says what picking it
# does, and each survives being read on its own (contract: P1 §3).
def _expiry_ask(blocked_on, days):
    return {
        "kind": "choice",
        "prompt": ("This has been parked %d days waiting on %s and it still has not "
                   "cleared. What do you want to do with it?" % (days, blocked_on)),
        "options": [
            {"label": "Keep waiting and chase it again",
             "value": "keep-waiting",
             "effect": "Hands this to a run that chases the blocker again and re-parks "
                       "it with a fresh recheck date.",
             "terminal": False, "resolved": False},
            {"label": "Stop tracking it and file it away",
             "value": "stop-tracking",
             "effect": "Settles the thread here. Nothing is written anywhere else and no "
                       "work is claimed as finished.",
             "terminal": True, "resolved": False},
            {"label": "It already happened, close it out",
             "value": "already-happened",
             "effect": "Settles the thread and records the underlying work as finished.",
             "terminal": True, "resolved": True},
        ],
    }


def apply_recheck(con, thread_id, gate_passed=None, detail=""):
    """The recheck transition, given the sweeper's verdict on the gate.

    gate_passed:
      True / None -- no gate, or the blocker cleared: the thread returns to the
                     work queue ('working') with an instruction event (source
                     helm-recheck) that pending_dispatch offers on the next pass.
      False       -- the blocker demonstrably still holds: re-armed for another
                     look, and counted. Unless the park has now run 30 days, in
                     which case it expires into a real ask instead of quietly
                     renewing itself forever.

    Every branch writes an event, because a thread that keeps rechecking without
    resolving is a real signal and belongs in the history rather than in a
    counter nobody reads.
    """
    row = _require_thread(con, thread_id)
    if row["state"] != "waiting-external" or (row["wake_at"] or 0) > 0:
        raise StoreError(400, "only a parked thread can be rechecked: this one is %r%s. A "
                              "snoozed thread comes back through its own wake date instead"
                              % (row["state"], " and snoozed" if (row["wake_at"] or 0) > 0
                                 else ""))
    now = time.time()
    n = int(row["recheck_count"] or 0) + 1
    blocked_on = row["blocked_on"] or "(unspecified)"
    detail = str(detail or "").strip()

    if str(row["gate_cmd"] or "").startswith(GATE_CMD_PREFIX):
        version = row["gate_cmd"][len(GATE_CMD_PREFIX):]
        held_s = now - float(row["parked_since"] or now)
        if gate_passed is False and held_s < GATE_MAX_HOLD_S:
            # Quietly re-armed: an hourly event for a week would bury the history.
            con.execute("UPDATE threads SET recheck_at=?, recheck_count=?, last_change=last_change"
                        " WHERE thread_id=?", (now + GATE_RECHECK_S, n, thread_id))
            con.commit()
            return {"thread_id": thread_id, "outcome": "still-held", "recheck_count": n,
                    "waiting_for": version, "recheck_at": now + GATE_RECHECK_S}
        if gate_passed is True:
            why = "released by gate: %s is on the testing channel%s" % (
                version, (" (" + detail + ")") if detail else "")
        elif gate_passed is None:
            why = ("released by gate: could not confirm %s on the testing channel%s, so it is shown "
                   "rather than held blind" % (version, (" (" + detail + ")") if detail else ""))
        else:
            why = ("released by gate: held %d days and %s never appeared on the testing channel; "
                   "shown now" % (int(held_s // 86400), version))
        _gate_release(con, thread_id, row, now, why)
        con.commit()
        return {"thread_id": thread_id, "outcome": "released", "recheck_count": n, "waiting_for": version}

    if gate_passed is False:
        age_days = int((now - (row["parked_since"] or now)) // 86400)
        if (now - (row["parked_since"] or now)) >= MAX_RECHECK_S:
            ask = _expiry_ask(blocked_on, age_days)
            body = ("This park has run %d days waiting on %s and %d recheck(s) have not "
                    "cleared it%s. A park is not allowed to outlive its own deadline, so "
                    "it comes back to you as a decision instead of renewing itself again."
                    % (age_days, blocked_on, n, (": " + detail) if detail else ""))
            add_event(con, thread_id, now, "helm-park-expiry", "", "bot-result", body)
            con.execute(
                "UPDATE threads SET state='needs-you', ask_kind=?, ask_prompt=?,"
                " ask_options=?, blocked_on='', recheck_at=0, gate_cmd='', parked_since=0,"
                " recheck_count=?, last_change=? WHERE thread_id=?",
                (ask["kind"], ask["prompt"], json.dumps(ask["options"]), n, now, thread_id))
            con.commit()
            return {"thread_id": thread_id, "outcome": "expired", "recheck_count": n,
                    "blocked_on": blocked_on, "parked_days": age_days}
        next_at = now + DEFAULT_RECHECK_S
        add_event(con, thread_id, now, "helm-recheck", "", "state-change",
                  "recheck #%d: still blocked on %s%s. Next look %s."
                  % (n, blocked_on, (" -- " + detail) if detail else "",
                     dt.datetime.fromtimestamp(next_at, _LOCAL_TZ).strftime("%Y-%m-%d %H:%M")))
        con.execute("UPDATE threads SET recheck_at=?, recheck_count=?, last_change=?"
                    " WHERE thread_id=?", (next_at, n, now, thread_id))
        con.commit()
        return {"thread_id": thread_id, "outcome": "still-blocked", "recheck_count": n,
                "blocked_on": blocked_on, "recheck_at": next_at}

    why = ("recheck #%d due -- was parked waiting on %s%s. Returning it to the work "
           "queue to verify whether that blocker still holds."
           % (n, blocked_on, (" -- " + detail) if detail else ""))
    add_event(con, thread_id, now, RECHECK_SOURCE, "", "state-change", why)
    # pending_dispatch keys on an instruction/owner-reply event newer than dispatched_at, so the
    # requeue is an 'instruction' event, as wake_due writes. Being the newest one it also
    # supersedes any earlier answer, which must not be handed to the run again. dispatched_at
    # is left alone: the event, not a zeroed timestamp, is what gets offered.
    add_event(con, thread_id, now + 0.001, RECHECK_SOURCE, "", "instruction", why)
    con.execute(
        "UPDATE threads SET state='working', recheck_at=0, recheck_count=?, gate_cmd=gate_cmd,"
        " dispatch_attempts=0, last_dispatch_try=0, last_change=?"
        " WHERE thread_id=?", (n, now, thread_id))
    con.commit()
    return {"thread_id": thread_id, "outcome": "requeued", "recheck_count": n,
            "blocked_on": blocked_on}


def sweep_rechecks(con, run_gate=None):
    """Every due recheck, in one pass. Returns what happened to each.

    `run_gate` is injected: `(thread_id, cmd) -> (passed: bool, detail: str)`.
    That is how the shell stays outside the store (contract: P1 §6) and how both
    branches stay testable without a subprocess. With no runner, or a park with
    no gate command, the date alone is the exit and the thread returns to the
    work queue -- the conservative direction, because it costs one cheap run
    whereas never rechecking costs the whole promise.

    A runner that raises is treated as "still blocked" with the exception in the
    detail, never as a pass: a broken checker must not buy silence (LESSON 24).
    """
    out = []
    for d in due_rechecks(con):
        passed, detail = None, ""
        if d["gate_cmd"] and run_gate is not None:
            try:
                passed, detail = run_gate(d["thread_id"], d["gate_cmd"])
            except Exception as exc:  # noqa: BLE001 - a broken gate fails closed
                passed, detail = False, ("gate runner raised %s: %s"
                                         % (type(exc).__name__, exc))
        out.append(apply_recheck(con, d["thread_id"], passed, detail))
    return out


def adopt_legacy_parks(con):
    """Give an exit to parks written before the park contract existed.

    A gate is not a migration: rows parked under the old rules carry
    recheck_at=0 and would sit inert forever while every new park is policed.
    They are staggered a few minutes apart rather than dumped into one pass,
    which would spawn a dozen concurrent runs the first time the sweeper ticks.
    """
    if not rules().adopt_legacy_parks:
        # A work database's undated waits are exactly that: undated. Giving them a
        # placeholder blocker and a recheck clock would rewrite rows.
        return 0
    rows = con.execute(
        "SELECT thread_id FROM threads WHERE state='waiting-external' AND recheck_at=0"
        "   AND wake_at=0 AND tombstoned=0 AND suppressed=0"
        " ORDER BY last_change ASC").fetchall()
    if not rows:
        return 0
    now = time.time()
    base = now + 120
    for i, r in enumerate(rows):
        con.execute(
            "UPDATE threads SET recheck_at=?, parked_since=CASE WHEN parked_since>0"
            " THEN parked_since ELSE ? END,"
            " blocked_on=CASE WHEN blocked_on='' THEN ? ELSE blocked_on END"
            " WHERE thread_id=?",
            (base + i * 180, now, "(adopted: parked before the recheck contract existed)",
             r["thread_id"]))
    con.commit()
    return len(rows)


def list_threads(con, include_done=False, include_snoozed=False, include_backlog=False):
    """Suppressed and tombstoned threads are never returned; no flag exposes
    them. Snoozed threads are omitted entirely rather than dimmed: a deferred
    thing should not be visible until it needs you. Backlog is omitted the
    same way and for the same reason, behind its own flag so it can still be
    read deliberately."""
    woke = wake_due(con)
    sql = "SELECT * FROM threads WHERE tombstoned=0 AND suppressed=0"
    if not include_done:
        sql += " AND state != 'done'"
    if not include_backlog:
        sql += " AND state != 'backlog'"
    if not include_snoozed:
        sql += " AND wake_at = 0"
    rows = con.execute(sql).fetchall()
    threads = [_row_to_thread(con, r, summary=True) for r in rows]
    threads.sort(key=lambda t: (URGENCY_ORDER.get(t["urgency"], 9), -(t["last_change"] or 0)))
    return {
        "generated_at": time.time(),
        "woke": woke,
        "categories": categories_of(threads),
        "threads": threads,
    }


# --- The open asks, compact and paged, for a chat session (GET /api/triage/open-asks) ------------
# A chat session clearing the inbox needs only what each card asks; the full list carries every
# thread with its context and history. A page is a keyset cursor (an opaque token naming the last
# row handed out), so a card settled between two pages costs no other row a place.
OPEN_ASKS_LIMIT = 50
OPEN_ASKS_LIMIT_MAX = 200
STALE_DAYS = 30
STALE_DAYS_MAX = 3650
BULK_SETTLE_MAX = 200
_URGENCY_RANK_SQL = "CASE urgency WHEN 'now' THEN 0 WHEN 'this-week' THEN 1 WHEN 'whenever' THEN 2 ELSE 9 END"
_OPEN_ASK_WHERE = ("tombstoned=0 AND suppressed=0 AND state='needs-you' AND ask_kind != 'none'"
                   " AND wake_at=0")
_STALE_WHERE = "tombstoned=0 AND suppressed=0 AND state='backlog' AND last_change < ?"


def _encode_cursor(key):
    return base64.urlsafe_b64encode(json.dumps(key, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor, view, shape):
    """The key a cursor names, checked against the view and its field types; StoreError 400 else."""
    try:
        s = str(cursor)
        key = json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        key = None
    ok = (isinstance(key, list) and len(key) == len(shape) + 1 and key[0] == view
          and all(isinstance(v, t) and not isinstance(v, bool) for v, t in zip(key[1:], shape)))
    if not ok:
        raise StoreError(400, "'cursor' is not one this view handed out; start again without it")
    return key[1:]


def _page_limit(limit):
    if limit is None or limit == "":
        return OPEN_ASKS_LIMIT
    try:
        n = int(limit)
    except (TypeError, ValueError):
        raise StoreError(400, "'limit' must be a whole number")
    if n < 1:
        raise StoreError(400, "'limit' must be at least 1")
    return min(n, OPEN_ASKS_LIMIT_MAX)


def _stale_days(days):
    if days is None or days == "":
        return STALE_DAYS
    try:
        n = int(days)
    except (TypeError, ValueError):
        raise StoreError(400, "'days' must be a whole number of days")
    if not 1 <= n <= STALE_DAYS_MAX:
        raise StoreError(400, "'days' must be between 1 and %d" % STALE_DAYS_MAX)
    return n


def _days(seconds):
    return round(max(0.0, seconds) / 86400.0, 1)


def open_asks(con, limit=None, cursor=None, now=None):
    """One page of the open cards that ask the owner something: lit (needs-you with an ask), not snoozed,
    muted or retired. Ordered by urgency, then oldest first, then id. Like the full list, the read
    first hands back the snoozes that came due (wake_due), so it shows what the inbox shows.

    Each row: thread_id, title, kind, prompt, options [{label, value, effect}], urgency, category,
    age_days (since first seen) and idle_days (since the last change). The answer also carries total
    (every open ask), next_cursor (None on the last page) and stale_total (the stale view's total)."""
    limit = _page_limit(limit)
    key = _decode_cursor(cursor, "asks", (int, (int, float), str)) if cursor else None
    wake_due(con)
    now = time.time() if now is None else now
    total = con.execute("SELECT COUNT(*) FROM threads WHERE " + _OPEN_ASK_WHERE).fetchone()[0]
    sql = ("SELECT * FROM (SELECT thread_id, title, category, urgency, ask_kind, ask_prompt, ask_options,"
           " first_seen, last_change, " + _URGENCY_RANK_SQL + " AS urank FROM threads WHERE "
           + _OPEN_ASK_WHERE + ")")
    args = []
    if key:
        sql += " WHERE (urank, first_seen, thread_id) > (?, ?, ?)"
        args = list(key)
    rows = con.execute(sql + " ORDER BY urank, first_seen, thread_id LIMIT ?", args + [limit + 1]).fetchall()
    out = []
    for r in rows[:limit]:
        opts = [o for o in _jloads(r["ask_options"], []) if isinstance(o, dict)]
        out.append({
            "thread_id": r["thread_id"], "title": r["title"], "kind": r["ask_kind"], "prompt": r["ask_prompt"],
            "options": [{"label": str(o.get("label") or ""), "value": str(o.get("value") or ""),
                         "effect": str(o.get("effect") or "")} for o in opts],
            "urgency": r["urgency"], "category": r["category"] or "",
            "age_days": _days(now - r["first_seen"]) if r["first_seen"] else None,
            "idle_days": _days(now - r["last_change"]) if r["last_change"] else None,
        })
    nxt = None
    if len(rows) > limit:
        last = rows[limit - 1]
        nxt = _encode_cursor(["asks", int(last["urank"]), float(last["first_seen"] or 0), last["thread_id"]])
    stale_total = con.execute("SELECT COUNT(*) FROM threads WHERE " + _STALE_WHERE,
                              (now - STALE_DAYS * 86400.0,)).fetchone()[0]
    return {"view": "asks", "generated_at": now, "total": total, "count": len(out), "limit": limit,
            "next_cursor": nxt, "stale_total": stale_total, "threads": out}


def stale_backlog(con, days=None, limit=None, cursor=None, now=None):
    """One page of the backlog cards idle more than `days` (default STALE_DAYS): thread_id, title,
    category, idle_days, idlest first. Read only: it asks nothing and closes nothing."""
    days = _stale_days(days)
    limit = _page_limit(limit)
    key = _decode_cursor(cursor, "stale", ((int, float), str)) if cursor else None
    now = time.time() if now is None else now
    cutoff = now - days * 86400.0
    total = con.execute("SELECT COUNT(*) FROM threads WHERE " + _STALE_WHERE, (cutoff,)).fetchone()[0]
    sql = "SELECT thread_id, title, category, last_change FROM threads WHERE " + _STALE_WHERE
    args = [cutoff]
    if key:
        sql += " AND (last_change, thread_id) > (?, ?)"
        args += list(key)
    rows = con.execute(sql + " ORDER BY last_change, thread_id LIMIT ?", args + [limit + 1]).fetchall()
    out = [{"thread_id": r["thread_id"], "title": r["title"], "category": r["category"] or "",
            "idle_days": _days(now - (r["last_change"] or 0))} for r in rows[:limit]]
    nxt = None
    if len(rows) > limit:
        last = rows[limit - 1]
        nxt = _encode_cursor(["stale", float(last["last_change"] or 0), last["thread_id"]])
    return {"view": "stale", "days": days, "generated_at": now, "total": total, "count": len(out),
            "limit": limit, "next_cursor": nxt, "threads": out}


def activity(con, limit=60, since=None):
    """Recent events across every thread, newest first.

    Joined to the thread title, because an event body on its own does not say
    what it was about. Includes suppressed and tombstoned threads: this is the
    record of what happened, and hiding a muted thread's history here would
    make a mute look like nothing occurred.
    """
    sql = ("SELECT e.ts, e.kind, e.source, e.body, e.thread_id,"
           "       t.title, t.state, t.urgency, t.category"
           "  FROM events e JOIN threads t ON t.thread_id = e.thread_id")
    args = []
    if since:
        sql += " WHERE e.ts > ?"
        args.append(float(since))
    sql += " ORDER BY e.ts DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in con.execute(sql, args)]


# Origin accounting. The one metric the homelab Helm could not compute while
# its board was eating itself: of the threads created in a window, how many
# were created by the system's own execution of other threads? A thread has
# no creator column; the source of its FIRST event is the creator, and the
# only source an executor run can write under is "bot" (post_result). Every
# other source is a human at a keyboard (owner, chat, agent) or
# a deterministic producer (helm, email-sweep, scheduled-task).
CREATED_WINDOW_S = 7 * 86400
RUN_SOURCES = {"bot"}


def origins(con, since=None):
    """Where the threads created since `since` came from, and how many runs
    happened in the same window, so the replacement rate (new threads created
    by runs, per run) is one division and not a correlation."""
    since = float(since if since is not None else time.time() - CREATED_WINDOW_S)
    rows = con.execute(
        "SELECT t.thread_id,"
        " (SELECT e.source FROM events e WHERE e.thread_id=t.thread_id"
        "   ORDER BY e.ts ASC, e.event_id ASC LIMIT 1) AS src"
        " FROM threads t WHERE t.first_seen > ?", (since,)).fetchall()
    by_source = {}
    by_run = 0
    for r in rows:
        src = r["src"] or "(no events)"
        by_source[src] = by_source.get(src, 0) + 1
        if src in RUN_SOURCES:
            by_run += 1
    runs = con.execute(
        "SELECT COUNT(*) n FROM events WHERE kind='bot-result' AND source IN (%s) AND ts > ?"
        % ",".join("?" * len(RUN_SOURCES)), (*sorted(RUN_SOURCES), since)).fetchone()["n"]
    return {
        "since": since,
        "created": len(rows),
        "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1])),
        "by_run": by_run,
        # Advisory definition: new items created by runs, per run. Below 1.0
        # decays, above 1.0 explodes; 0.5 is where to start worrying.
        "runs": runs,
        "replacement_rate": (by_run / runs) if runs else 0.0,
    }


def health(con):
    """Counts and queue state. Every number carries its denominator."""
    rows = con.execute(
        "SELECT state, COUNT(*) n FROM threads"
        " WHERE tombstoned=0 AND suppressed=0 GROUP BY state").fetchall()
    by_state = {r["state"]: r["n"] for r in rows}
    total = sum(by_state.values())
    snoozed = con.execute(
        "SELECT COUNT(*) n FROM threads WHERE tombstoned=0 AND suppressed=0"
        " AND wake_at > 0").fetchone()["n"]
    # A settled thread keeps its `parked` flag (only set_parked clears it), so
    # parked is counted over live threads: in_flight subtracts done separately.
    parked = con.execute(
        "SELECT COUNT(*) n FROM threads WHERE tombstoned=0 AND suppressed=0"
        " AND parked=1 AND state != 'done'").fetchone()["n"]
    next_wake = con.execute(
        "SELECT MIN(wake_at) w FROM threads WHERE wake_at > 0 AND tombstoned=0"
        " AND suppressed=0").fetchone()["w"]
    q = pending_dispatch(con)
    return {
        "threads_total": total,
        "by_state": by_state,
        "needs_you": by_state.get("needs-you", 0),
        "snoozed": snoozed,
        "parked": parked,
        "backlog": by_state.get("backlog", 0),
        # Live, not done, not deliberately set aside. The number that answers
        # "how much is actually moving", as opposed to threads_total, which
        # answers "how much is on the board at all" and counts done and
        # parked right alongside the rest.
        "in_flight": total - by_state.get("done", 0) - parked - by_state.get("backlog", 0),
        "next_wake": next_wake,
        # Parks whose recheck date has already passed. A number that only ever
        # grows means the sweeper is not running, which is the failure mode a
        # park contract without an actor always degrades into.
        "rechecks_due": len(due_rechecks(con)),
        "created": origins(con),
        "retired": con.execute(
            "SELECT COUNT(*) n FROM threads WHERE tombstoned=1 OR suppressed=1"
        ).fetchone()["n"],
        "queue": {"pending": q["count"], "gave_up": q["gave_up"],
                  "stale": len(stale_dispatches(con)),
                  # The oldest unactioned answer. Queue AGE is the signal that
                  # matters: attempts stay at 0 when a poller dies before its
                  # first call, so a broken pump looks healthier than a busy one.
                  "oldest_wait_s": (time.time() - q["pending"][0]["reply_ts"])
                                   if q["pending"] else 0},
    }


def list_suppressions(con):
    return [dict(r) for r in con.execute(
        "SELECT * FROM suppressions WHERE active=1 ORDER BY created DESC")]


def _reply_value(con, thread_id, ts):
    r = con.execute("SELECT kind, body, source FROM events WHERE thread_id=? AND kind IN ('owner-reply', 'instruction')"
                    " AND ts=? LIMIT 1", (thread_id, ts)).fetchone()
    if not r:
        return ""
    if r["kind"] == "instruction":
        return RECHECK_VALUE if r["source"] in (WAKE_SOURCE, RECHECK_SOURCE) else HAND_TASK_VALUE
    return r["body"]


def _reply_via(con, thread_id, ts):
    """How the dispatched answer arrived: `web-ui` for a press of the owner's signed-in page, else ''."""
    r = con.execute("SELECT via FROM events WHERE thread_id=? AND kind='owner-reply' AND ts=? LIMIT 1",
                    (thread_id, ts)).fetchone()
    return (r["via"] or "") if r else ""


def _reply_note_and_source(con, thread_id, ts):
    """Derive the dispatched reply note and its machine-readable source from the event log.

    the owner's reply note is recorded as a 'owner-note' event at ts + 0.001 in reply().
    A stall hand-off is recorded as an 'instruction' event at ts with source 'helm-stall'.
    A recheck (a run's snooze that came due) is an 'instruction' event with source 'helm-wake';
    its body is the snooze's own dated note, or empty.
    A machine's hand-off keeps the owner-reply kind with the machine's source
    (handoff_source), and that source is what the pump is told.
    Deriving from the event log preserves the owner's answer note even if subsequent notes
    land on the thread before dispatch, keeping last_note as a display mirror.
    """
    r = con.execute("SELECT kind, body, source FROM events WHERE thread_id=? AND kind IN ('owner-reply', 'instruction')"
                    " AND ts=? LIMIT 1", (thread_id, ts)).fetchone()
    if not r:
        return "", ""
    if r["kind"] == "instruction":
        source = r["source"] or "helm-stall"
        return r["body"] or "", source
    # For owner-reply: derive the note from the event log for this reply.
    note_row = con.execute(
        "SELECT body FROM events WHERE thread_id=? AND kind='owner-note'"
        " AND ts >= ? AND ts <= ? + 0.005 ORDER BY ts ASC LIMIT 1",
        (thread_id, ts, ts)).fetchone()
    note = (note_row["body"] or "") if note_row else ""
    return note, r["source"] or "owner"


# "Has an answer nothing has acted on": the newest owner-reply or instruction is newer than
# dispatched_at. One definition for pending_dispatch, stale_dispatches and quiet_runs, so the
# queue and the "lost runs" list can never both claim the same thread.
_REPLY_TS_SQL = ("(SELECT MAX(ts) FROM events e WHERE e.thread_id=t.thread_id"
                 " AND e.kind IN ('owner-reply', 'instruction'))")
_UNACTIONED_SQL = "COALESCE(%s, 0) > COALESCE(t.dispatched_at, 0)" % _REPLY_TS_SQL


def pending_dispatch(con, max_attempts=MAX_DISPATCH_ATTEMPTS):
    """Answers given that nothing has acted on yet.

    Pending means the newest owner-reply or instruction event is newer than dispatched_at,
    which keys idempotency to the REPLY rather than to the thread: a second
    answer on the same thread is new work, the same answer never is.

    A dispatched run that never produced a result is re-offered after
    RESULT_TIMEOUT_S, behind an exponential backoff, and gives up loudly after
    max_attempts rather than retrying forever in silence.
    """
    now = time.time()
    rows = con.execute(
        "SELECT t.*, " + _REPLY_TS_SQL + " AS reply_ts"
        # A settled thread never dispatches, however it got settled: a terminal
        # option, an explicit close, or a mute. Closing it is the instruction.
        # Nor does a backlogged one: putting something in the backlog is the
        # instruction, the same as closing it (work rule pack).
        " FROM threads t WHERE t.tombstoned=0 AND t.suppressed=0"
        "   AND t.state != 'done' AND t.state != 'backlog' AND " + _UNACTIONED_SQL).fetchall()
    out, gave_up = [], 0
    hide_snoozed = rules().snoozed_never_dispatched
    for r in rows:
        rts = r["reply_ts"]
        if hide_snoozed and (r["wake_at"] or 0) > now:
            # Answer a card, then snooze it before the tick consumes the answer:
            # the run must not fire and reset wake_at (work rule, 09-15).
            continue
        if (r["dispatch_attempts"] or 0) >= max_attempts:
            gave_up += 1
            continue
        # Backoff applies to RETRIES only. A new answer is new work, not a
        # retry, so it must not be held behind the timer left by the previous
        # answer's successful dispatch. attempts == 0 means nothing has failed.
        attempts = r["dispatch_attempts"] or 0
        if attempts > 0:
            backoff = min(DISPATCH_BACKOFF_S * (2 ** (attempts - 1)),
                          DISPATCH_BACKOFF_MAX_S)
            if (r["last_dispatch_try"] or 0) + backoff > now:
                continue  # still inside the backoff window from the failed attempt
        r_note, r_note_src = _reply_note_and_source(con, r["thread_id"], rts)
        out.append({
            "thread_id": r["thread_id"], "title": r["title"], "summary": r["summary"],
            "category": r["category"], "urgency": r["urgency"],
            "ask_prompt": r["ask_prompt"],
            "ask_options": _jloads(r["ask_options"], []),
            "ask_context": r["ask_context"],
            "responder": r["responder"], "sources": _jloads(r["sources"], []),
            "reply_ts": rts, "reply_value": _reply_value(con, r["thread_id"], rts),
            "reply_via": _reply_via(con, r["thread_id"], rts),
            "reply_note": r_note, "reply_note_source": r_note_src, "state": r["state"],
            # The files ride with the answer, because they ARE part of it
            # (contract: P1 §5). An executor that cannot see them would report
            # back asking for the thing it was already sent.
            "attachments": attachments_for(con, r["thread_id"]),
            "attempts": r["dispatch_attempts"] or 0,
            "history": [dict(e) for e in con.execute(
                "SELECT ts, source, source_id, kind, body FROM events WHERE thread_id=?"
                " ORDER BY ts ASC", (r["thread_id"],))],
        })
    out.sort(key=lambda x: x["reply_ts"])
    return {"count": len(out), "gave_up": gave_up, "max_attempts": max_attempts,
            "pending": out}


# The thread's newest event of any kind is older than the cutoff (stale_dispatches).
_SILENT_SQL = ("COALESCE((SELECT MAX(e.ts) FROM events e"
               " WHERE e.thread_id = t.thread_id), 0) < ?")


def stale_dispatches(con):
    """Runs that were dispatched and never reported back.

    The test is "nothing reported SINCE the dispatch", not "the dispatch is
    old". Age alone is wrong, and confidently so: a thread that ran, reported
    properly, and was then answered again is back in 'working' with a fresh
    reply while `dispatched_at` still points at the previous round. Measuring
    age flagged four such threads as dead runs when all four had filed real
    reports, which is the view lying about state, the one thing it exists not
    to do.

    "Reported since the dispatch" must not hide a thread for good, though. A run
    can report and the thread still never leave 'working' (a close that was
    never recorded: the pet-selection thread sat that way for 85 hours while
    this list was empty). So a row that DID report is listed too, but only
    while it is still 'working' AND its newest event of any kind is older than
    RESULT_TIMEOUT_S. A thread that reported and moved on is out of 'working',
    and one answered again has a fresh reply, so neither qualifies.
    `reported` says which arm listed the row.

    At home (rule beats_are_life) both arms need that silence (H6): a run that has not reported yet but whose
    thread carries a recent event of any kind (a bot link's progress notes, its
    "beats") is alive, not quiet.

    A thread with an unactioned answer (what pending_dispatch offers) is never
    listed by either arm: it has not been dispatched since that answer, so there
    is no run to have gone quiet, and dispatched_at still dates the previous round.
    """
    now = time.time()
    cutoff = now - RESULT_TIMEOUT_S
    rows = con.execute(
        "SELECT thread_id, title, dispatched_at, dispatch_attempts,"
        "   EXISTS (SELECT 1 FROM events e WHERE e.thread_id = t.thread_id"
        "             AND e.kind = 'bot-result' AND e.ts > t.dispatched_at) AS reported"
        " FROM threads t"
        " WHERE tombstoned=0 AND suppressed=0 AND state='working' AND dispatched_at > 0"
        "   AND dispatched_at < ?"
        # An answer still waiting its turn (a held dispatcher) has not been dispatched
        # yet, so it cannot be a dispatch that never reported: pending_dispatch owns it.
        "   AND NOT " + _UNACTIONED_SQL
        + ("   AND " + _SILENT_SQL if rules().beats_are_life else
           "   AND (NOT EXISTS (SELECT 1 FROM events e WHERE e.thread_id = t.thread_id"
           "                      AND e.kind = 'bot-result' AND e.ts > t.dispatched_at) OR " + _SILENT_SQL + ")"),
        (cutoff, cutoff)).fetchall()
    return [dict(r, reported=bool(r["reported"])) for r in rows]


def mark_dispatched(con, thread_id, reply_ts=None, ok=True, detail="", mode="reply"):
    """The poller reporting what happened when it woke an executor.

    The asymmetry between modes is deliberate. A reply-mode success resets the
    attempt counter, because the answer genuinely reached someone. Any failure
    burns an attempt, so a thread that keeps consuming runs without ever
    reporting marches toward the cap and gives up loudly instead of spinning.
    """
    _require_thread(con, thread_id)
    now = time.time()
    if mode == "start":
        # Claim the attempt BEFORE the executor is spawned. If the poller dies
        # mid-run, this is the only record that anything was tried: without it
        # the answer looks untouched, re-dispatches on the very next tick, and
        # can do so forever while an executor that already did the work is
        # still running. Claiming first turns that into one bounded retry.
        con.execute("UPDATE threads SET dispatch_attempts=dispatch_attempts+1,"
                    " last_dispatch_try=? WHERE thread_id=?", (now, thread_id))
        con.commit()
        row = con.execute("SELECT dispatch_attempts FROM threads WHERE thread_id=?",
                          (thread_id,)).fetchone()
        return {"accepted": True, "thread_id": thread_id, "claimed": True,
                "attempts": row["dispatch_attempts"]}
    if ok and mode == "reply":
        if reply_ts is None:
            raise StoreError(400, "'reply_ts' is required on a successful reply dispatch, "
                                  "because idempotency keys to the reply and not to the thread")
        # Monotonic (MAX), never backward: if the executor's own report
        # already reached post_result() before the poller got here (see
        # post_result's matching comment), dispatched_at may already be
        # ahead of this specific reply_ts, and this call must not undo that.
        con.execute("UPDATE threads SET dispatched_at=MAX(COALESCE(dispatched_at, 0), ?),"
                    " dispatch_attempts=0, last_dispatch_try=? WHERE thread_id=?",
                    (float(reply_ts), now, thread_id))
    elif ok:
        con.execute("UPDATE threads SET dispatched_at=?, dispatch_attempts=dispatch_attempts+1,"
                    " last_dispatch_try=? WHERE thread_id=?", (now, now, thread_id))
    else:
        # No increment here. "start" mode already claimed this attempt before
        # the executor was ever spawned (see the mode == "start" branch
        # above), so a failed "reply" report is the SAME attempt reporting
        # in, not a second one. Incrementing again here double-charged every
        # failed run against the 5-attempt budget (one on start, one on
        # failure), which is what let three real timeouts on
        # 2026-09-01 burn all 5 attempts and silently wedge
        # t3-ssh-audit-example-20260901 in 'working' for two days.
        con.execute("UPDATE threads SET last_dispatch_try=? WHERE thread_id=?",
                    (now, thread_id))
        add_event(con, thread_id, now, "helm", "", "state-change",
                  "dispatch FAILED: " + (str(detail) or "no detail given"))
    con.commit()
    row = con.execute("SELECT dispatch_attempts FROM threads WHERE thread_id=?",
                      (thread_id,)).fetchone()
    return {"accepted": True, "thread_id": thread_id, "ok": bool(ok),
            "attempts": row["dispatch_attempts"]}


def quiet_runs(con, limit=20):
    """Runs the owner can Retry from ACTIVITY, in flight only: dispatched and never
    reported back (stale_dispatches), or out of attempts with an answer nothing acted on
    (what pending_dispatch counts as gave_up). Each row says which, and since when."""
    out = [{"thread_id": r["thread_id"], "title": r["title"],
            "why": "reported back but is still working" if r["reported"] else "never reported back",
            "since": r["dispatched_at"], "attempts": r["dispatch_attempts"]} for r in stale_dispatches(con)]
    seen = {o["thread_id"] for o in out}
    rows = con.execute(
        "SELECT t.thread_id, t.title, t.dispatch_attempts, t.last_dispatch_try, t.dispatched_at"
        " FROM threads t WHERE t.tombstoned=0 AND t.suppressed=0 AND t.state='working'"
        "   AND t.dispatch_attempts >= ? AND " + _UNACTIONED_SQL,
        (MAX_DISPATCH_ATTEMPTS,)).fetchall()
    for r in rows:
        if r["thread_id"] in seen:
            continue
        out.append({"thread_id": r["thread_id"], "title": r["title"],
                    "why": "gave up after %d attempts" % r["dispatch_attempts"],
                    "since": r["last_dispatch_try"], "attempts": r["dispatch_attempts"]})
    return sorted(out, key=lambda o: o["since"] or 0)[:limit]


# --- Executor runs and costs (work: the poller's heartbeat carries them) ----------------------
RUNS_PER_HEARTBEAT = 20
LOG_TAIL_LINES = 40
LOG_LINE_CHARS = 500
RUNS_KEEP_S = 30 * 86400
COST_WINDOW_S = 7 * 86400
_RUN_INTS = ("turns", "input_tokens", "output_tokens", "cache_read_tokens")
_RUN_FLOATS = ("cost_usd", "duration_s")


def _num(v, cast, field):
    if v is None or v == "":
        return cast(0)
    if isinstance(v, bool):
        raise StoreError(400, "run field '%s' must be a number" % field)
    try:
        out = cast(v)
    except (TypeError, ValueError, OverflowError):
        raise StoreError(400, "run field '%s' must be a number" % field)
    if cast is int:
        # checked before any float conversion: math.isfinite(10**400) itself raises OverflowError,
        # and SQLite INTEGER is 64-bit, so past it the driver raises
        if not -(2 ** 63) <= out < 2 ** 63:
            raise StoreError(400, "run field '%s' must fit in a 64-bit integer" % field)
    elif not math.isfinite(out):   # json.loads takes NaN and Infinity; a sum would carry them
        raise StoreError(400, "run field '%s' must be a finite number" % field)
    return out


def _clean_text(s):
    """A run folder name with a non-UTF-8 byte arrives as a lone surrogate, which SQLite refuses
    to bind (a 500 on every heartbeat that carries it). Keep it visible as an escape instead."""
    return s.encode("utf-8", "backslashreplace").decode("utf-8")


def normalize_runs(raw, now=None):
    """The heartbeat's optional `runs` (contract: ACTIVITY-RUN-DATA.md), checked before
    anything is written. None means the heartbeat carried none (an old the poller)."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise StoreError(400, "runs must be a list")
    if len(raw) > RUNS_PER_HEARTBEAT:
        raise StoreError(400, "runs carries %d; the limit is %d per heartbeat" % (len(raw), RUNS_PER_HEARTBEAT))
    now = time.time() if now is None else now
    out = []
    for r in raw:
        if not isinstance(r, dict) or not isinstance(r.get("name"), str) or not r["name"].strip():
            raise StoreError(400, "every run needs a 'name' (its run folder)")
        row = {"name": _clean_text(r["name"].strip())[:200],
               "ts": _num(r.get("ts"), float, "ts") or now,
               "thread_id": _clean_text(str(r.get("thread_id") or ""))[:200],
               "outcome": _clean_text(str(r.get("outcome") or ""))[:40]}
        for k in _RUN_INTS:
            row[k] = _num(r.get(k), int, k)
        for k in _RUN_FLOATS:
            row[k] = _num(r.get(k), float, k)
        out.append(row)
    return out


def normalize_log_tail(raw):
    """The heartbeat's optional `log_tail`: at most LOG_TAIL_LINES strings. A line longer
    than LOG_LINE_CHARS is clipped rather than refused (it would cost the heartbeat)."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise StoreError(400, "log_tail must be a list of lines")
    if len(raw) > LOG_TAIL_LINES:
        raise StoreError(400, "log_tail carries %d lines; the limit is %d" % (len(raw), LOG_TAIL_LINES))
    return [str(x if x is not None else "")[:LOG_LINE_CHARS] for x in raw]


def record_runs(con, runs, now=None):
    """Store runs by name (a resend replaces, never adds) and drop what is past RUNS_KEEP_S."""
    now = time.time() if now is None else now
    cols = ("name", "ts", "thread_id", "outcome") + _RUN_INTS + _RUN_FLOATS
    for r in runs or []:
        con.execute(
            "INSERT INTO dispatcher_runs (%s, received) VALUES (%s, ?)"
            " ON CONFLICT(name) DO UPDATE SET %s" % (
                ", ".join(cols), ", ".join("?" * len(cols)),
                ", ".join("%s=excluded.%s" % (c, c) for c in cols[1:])),
            tuple(r[c] for c in cols) + (now,))
    con.execute("DELETE FROM dispatcher_runs WHERE ts < ?", (now - RUNS_KEEP_S,))
    con.commit()
    return len(runs or [])


def _titles(con, tids):
    tids = [t for t in set(tids) if t]
    if not tids:
        return {}
    rows = con.execute("SELECT thread_id, title FROM threads WHERE thread_id IN (%s)"
                       % ",".join("?" * len(tids)), tids).fetchall()
    return {r["thread_id"]: r["title"] for r in rows}


def recent_runs(con, limit=8):
    """The newest runs, each with its thread's title when the thread is here."""
    rows = [dict(r) for r in con.execute(
        "SELECT name, ts, thread_id, outcome, turns, cost_usd, input_tokens, output_tokens,"
        " cache_read_tokens, duration_s FROM dispatcher_runs ORDER BY ts DESC, name DESC LIMIT ?",
        (limit,))]
    titles = _titles(con, [r["thread_id"] for r in rows])
    for r in rows:
        r["title"] = titles.get(r["thread_id"], "")
    return rows


def run_costs(con, top=8, now=None):
    """What the runs consumed: every kept run (RUNS_KEEP_S) and the last COST_WINDOW_S,
    plus the threads that cost most in that window. `cost_usd` is the CLI's nominal
    figure (API list price), not a bill. {"all": None} until a run has been reported."""
    now = time.time() if now is None else now
    agg = ("COUNT(*) runs, COALESCE(SUM(cost_usd), 0) cost_usd, COALESCE(SUM(turns), 0) turns,"
           " COALESCE(SUM(input_tokens), 0) input, COALESCE(SUM(output_tokens), 0) output,"
           " COALESCE(SUM(cache_read_tokens), 0) cache_read")

    def shape(r):
        d = dict(r)
        d["turns_per_run"] = (d["turns"] / d["runs"]) if d["runs"] else 0.0
        d["tokens"] = d["input"] + d["output"] + d["cache_read"]
        return d

    # A window on read too: record_runs only prunes when a heartbeat carries runs, so a
    # a quiet client would otherwise leave rows past RUNS_KEEP_S in "last 30 days".
    total = shape(con.execute("SELECT " + agg + " FROM dispatcher_runs WHERE ts >= ?",
                              (now - RUNS_KEEP_S,)).fetchone())
    if not total["runs"]:
        return {"all": None}
    since = now - COST_WINDOW_S
    window = shape(con.execute("SELECT " + agg + " FROM dispatcher_runs WHERE ts >= ?", (since,)).fetchone())
    ranked = [shape(r) for r in con.execute(
        "SELECT thread_id, " + agg + " FROM dispatcher_runs WHERE ts >= ? AND thread_id != ''"
        " GROUP BY thread_id ORDER BY cost_usd DESC, thread_id LIMIT ?", (since, top))]
    titles = _titles(con, [r["thread_id"] for r in ranked])
    for r in ranked:
        r["title"] = titles.get(r["thread_id"]) or r["thread_id"]
    return {"window_days": COST_WINDOW_S // 86400, "kept_days": RUNS_KEEP_S // 86400,
            "all": total, "window": window, "top": ranked}


def wake_now(con, thread_id):
    """the owner's Wake now on a snoozed thread: the snooze ends here, exactly as
    if its time had come, so it comes back through wake_due() carrying the same ask."""
    row = _require_thread(con, thread_id)
    if (row["wake_at"] or 0) <= 0 or row["state"] == "done":
        raise StoreError(409, "this thread is not snoozed")
    if row["suppressed"]:
        raise StoreError(409, "this thread is muted; unmute it before waking it")
    con.execute("UPDATE threads SET wake_at=? WHERE thread_id=?", (time.time() - 0.001, thread_id))
    con.commit()
    wake_due(con)
    con.commit()
    return {"ok": True, "thread_id": thread_id, "woke": True}


def retry_dispatch(con, thread_id, by=None, note=""):
    """the owner's Retry on a run that stalled or gave up.

    pending_dispatch() offers a thread when its newest owner-reply or instruction is newer
    than dispatched_at and it has attempts left. A run that never reported, or one that
    burned its budget, sits in 'working' with nothing new to offer, and no screen could
    move it. Retry is the owner saying "run it again": an instruction event now, carrying his
    words, so there is new work to dispatch; and a fresh attempt budget, because he is the
    one lifting the cap. Only a thread in flight can be retried: a lit, parked or settled
    one has its own buttons, and quietly re-dispatching those would act on a stale answer.
    """
    row = _require_thread(con, thread_id)
    if row["state"] != "working":
        raise StoreError(409, "only a thread in flight can be retried; this one is '%s'" % row["state"])
    if row["suppressed"]:
        raise StoreError(409, "this thread is muted; unmute it before retrying it")
    now = time.time()
    body = str(note or "").strip() or "Retry: the last run did not report back; run it again."
    add_event(con, thread_id, now, normalize_by(by), "", "instruction", body)
    con.execute("UPDATE threads SET dispatch_attempts=0, last_dispatch_try=0, last_change=? WHERE thread_id=?",
                (now, thread_id))
    con.commit()
    return {"ok": True, "thread_id": thread_id, "retried": True}


def _restore_unrun_recheck(con, r, rts, now):
    """gave-up/restore for a recheck that never ran: the ask stashed when it was snoozed comes
    back, under a dated note, never the old report. `r` is the thread row, `rts` the wake."""
    tid = r["thread_id"]
    stash = _jloads(r["wake_ask"], {}) or {}
    if not isinstance(stash, dict):
        stash = {}
    kind = str(stash.get("kind") or "ack")
    if kind not in VALID_ASK or kind == "none":
        kind = "ack"
    options, _ = normalize_options(stash.get("options"))
    if kind == "choice" and not options:
        kind = "ack"
    prompt = str(stash.get("prompt") or "") or "Pick up where this left off."
    due = dt.datetime.fromtimestamp(rts, _LOCAL_TZ).strftime("%Y-%m-%d")
    last_ts = _newest_report(con, tid)[0]
    note = "The recheck due %s did not run." % due
    note += (" The last real update is from %s." % dt.datetime.fromtimestamp(last_ts, _LOCAL_TZ).strftime("%Y-%m-%d")
             if last_ts else " Nothing has been reported on it before.")
    add_event(con, tid, now, "helm-gave-up", "", "bot-result", note)
    con.execute("UPDATE threads SET state='needs-you', ask_kind=?, ask_prompt=?, ask_options=?,"
                " ask_context=?, wake_ask='', last_note=?, last_change=? WHERE thread_id=?",
                (kind, prompt, json.dumps(options), str(stash.get("context") or ""), note, now, tid))


def restore_gave_up(con, max_attempts=MAX_DISPATCH_ATTEMPTS):
    """Undo a silent give-up: put a capped-out thread back in front of the owner.

    pending_dispatch() stops OFFERING a thread once dispatch_attempts crosses
    max_attempts, on purpose, so a broken run cannot spin forever (see its own
    docstring). What it never did was undo anything: a thread that hit the cap
    right after being answered was left wearing whatever state the reply had
    set, almost always 'working', with its ORIGINAL ask still sitting
    unchanged on the row (reply() never clears it) but not shown, because
    'working' is not 'needs-you'. On screen that is indistinguishable from
    real work in progress, forever. It is exactly how
    t3-ssh-audit-example-20260901 wedged for two days: three real 900s
    timeouts on 2026-09-01 burned the whole budget (helped by a since-fixed
    bug in mark_dispatched() that charged each failure twice), pending_
    dispatch() correctly stopped offering it back out, and nothing ever
    offered it back to a human either.

    This does, every time it is called, the same repair that was done for t3
    by hand: consume the reply that gave up (so a tick landing mid-restore
    cannot see attempts==0 with that reply still "unconsumed" and dispatch it
    again before this finishes), then restore state='needs-you' with the SAME
    ask that was there the whole time, plus a note saying why it is back.

    Guards against one race a naive version of this would not: if a brand new
    reply has landed since the LAST real dispatch attempt
    (reply_ts > last_dispatch_try), nothing that failed ever saw it, so it is
    left alone for pending_dispatch() to offer normally rather than being
    swallowed here as if it were the stale one.
    """
    now = time.time()
    rows = con.execute(
        "SELECT t.*, (SELECT MAX(ts) FROM events e WHERE e.thread_id=t.thread_id"
        "   AND e.kind IN ('owner-reply', 'instruction')) AS reply_ts"
        " FROM threads t WHERE t.tombstoned=0 AND t.suppressed=0"
        "   AND t.state != 'done'").fetchall()
    restored = []
    for r in rows:
        rts = r["reply_ts"]
        if not rts or rts <= (r["dispatched_at"] or 0):
            continue  # no unconsumed reply: nothing to have given up on
        if (r["dispatch_attempts"] or 0) < max_attempts:
            continue  # still has retries left; pending_dispatch() owns this one
        if rts > (r["last_dispatch_try"] or 0):
            continue  # a fresh reply arrived after the last real attempt; not stale
        tid = r["thread_id"]
        attempts = r["dispatch_attempts"] or 0
        # Same first step as the by-hand t3 fix: consume the reply that gave
        # up, exactly as a successful dispatch would, before touching state.
        mark_dispatched(con, tid, reply_ts=rts, ok=True, mode="reply")
        if rules().recheck_on_wake:
            woke_by = con.execute(
                "SELECT source FROM events WHERE thread_id=? AND kind IN ('owner-reply', 'instruction')"
                " AND ts=? LIMIT 1", (tid, rts)).fetchone()
            if woke_by and woke_by["source"] == WAKE_SOURCE:
                _restore_unrun_recheck(con, r, rts, now)
                restored.append({"thread_id": tid, "title": r["title"], "attempts": attempts})
                continue
        if r["ask_kind"] not in VALID_ASK or r["ask_kind"] == "none":
            # Never restore into a bare Close button (invariant 4). Only
            # reachable if the ask was cleared by something other than the
            # normal reply cycle (e.g. a snooze) while a dispatch was in
            # flight; the ordinary case needs no repair here at all, because
            # reply() never clears the ask and it is still sitting on the row.
            con.execute("UPDATE threads SET ask_kind='ack',"
                        " ask_prompt='Pick up where this left off.' WHERE thread_id=?", (tid,))
        note = (
            "The dispatcher tried %d time(s) to act on your answer and never got a "
            "usable result back, so it gave up rather than retry forever. Restoring "
            "this to needs-you with your original question unchanged; nothing it "
            "promised has been done yet. Pick an option to try again."
            % attempts)
        add_event(con, tid, now, "helm-gave-up", "", "bot-result", note)
        con.execute("UPDATE threads SET state='needs-you', last_note=?, last_change=?"
                    " WHERE thread_id=?", (note, now, tid))
        restored.append({"thread_id": tid, "title": r["title"], "attempts": attempts})
    if restored:
        con.commit()
    return restored


# --- Writes ------------------------------------------------------------------
def _answer_is_in_flight(con, existing) -> bool:
    """Invariant 3. Three cases, each with a real reason.

    A finished item that reappeared carrying its ORIGINAL prompt and buttons
    is on screen indistinguishable from never having been touched, which is
    exactly the lie this whole thing exists to stop telling.
    """
    if existing["state"] == "working":
        return True  # something is acting on the answer right now
    try:
        if (existing["wake_at"] or 0) > time.time():
            return True  # deliberately hidden until a promised date
    except (IndexError, KeyError):
        pass  # pre-migration row
    if existing["ask_kind"] != "none":
        return False
    kinds = {r["kind"] for r in con.execute(
        "SELECT DISTINCT kind FROM events WHERE thread_id=?", (existing["thread_id"],))}
    # ask cleared, a reply given, a report back: the work is done and only
    # Close remains.
    return "owner-reply" in kinds and "bot-result" in kinds


def _touch_category(con, name, now):
    if not name:
        return
    con.execute("INSERT INTO categories (name, first_seen, last_seen) VALUES (?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
                (name, now, now))


def _touch_all_categories(con, now):
    """A category-less resort (a client refresh): every known category. Returns the count."""
    return con.execute("UPDATE categories SET last_seen=?", (now,)).rowcount


# --- One spelling per category -------------------------------------------------------------------
# A category that differs from a stored one only by case or whitespace is the same category, and
# every door that sets one stores the stored canonical spelling (canonical_category). The canonical
# spelling of a group is the most-used one (live threads carrying it), ties going to the oldest
# (first seen on a thread or in the categories table), then the name. merge_category_case merges the
# duplicates a database already holds. Any other difference ("Gardens" beside "Garden") is kept.
CATEGORY_MERGE_DIRNAME = "category-merges"


def category_key(name):
    """What two spellings of one category share: whitespace runs collapsed, the ends trimmed and the
    case folded. 'Garden', ' garden ' and 'GARDEN' share a key; 'Gardens' and 'Garden-Shed' do not."""
    return " ".join(str(name or "").split()).casefold()


def category_spellings(con):
    """{key: [{name, live, rows, first_seen}, ...]} for every spelling the site holds, on a thread or
    in the categories table, each list canonical first. `live` counts the untombstoned threads that
    carry the spelling (the ranking), `rows` every thread row (what a merge rewrites)."""
    spell = {}
    for r in con.execute("SELECT category AS name, SUM(CASE WHEN tombstoned=0 THEN 1 ELSE 0 END) AS live,"
                         " COUNT(*) AS n, MIN(CASE WHEN first_seen > 0 THEN first_seen END) AS fs"
                         " FROM threads WHERE category != '' GROUP BY category"):
        spell[r["name"]] = {"name": r["name"], "live": int(r["live"] or 0), "rows": int(r["n"] or 0),
                            "first_seen": float(r["fs"]) if r["fs"] else None}
    for r in con.execute("SELECT name, first_seen FROM categories WHERE name != ''"):
        s = spell.setdefault(r["name"], {"name": r["name"], "live": 0, "rows": 0, "first_seen": None})
        fs = float(r["first_seen"] or 0)
        if fs > 0 and (s["first_seen"] is None or fs < s["first_seen"]):
            s["first_seen"] = fs
    groups = {}
    for s in spell.values():
        k = category_key(s["name"])
        if k:
            groups.setdefault(k, []).append(s)
    for items in groups.values():
        items.sort(key=lambda s: (-s["live"], s["first_seen"] if s["first_seen"] is not None else float("inf"),
                                  s["name"]))
    return groups


def category_canon_map(con):
    """{key: canonical spelling} for every category the site holds (category_spellings)."""
    return {k: items[0]["name"] for k, items in category_spellings(con).items()}


def canonical_category(con, name, cache=None):
    """The spelling to store for `name`: the canonical spelling of a stored category with the same
    key, else `name` as given ('' stays ''). `cache` is a category_canon_map shared across one batch;
    it learns each new spelling, so a batch's own duplicates collapse to the first one."""
    name = str(name or "")
    key = category_key(name)
    if not key:
        return name
    if cache is None:
        cache = category_canon_map(con)
    canon = cache.get(key)
    if canon is None:
        cache[key] = name
        return name
    return canon


def category_case_groups(con):
    """The stored categories that differ only by case or whitespace, one entry per group:
    {canonical, canonical_threads, merge: [{name, threads}]}, `threads` counting every thread row
    with that spelling. Sorted by key; [] when every category has one spelling."""
    out = []
    for key, items in sorted(category_spellings(con).items()):
        if len(items) < 2:
            continue
        out.append({"canonical": items[0]["name"], "canonical_threads": items[0]["rows"],
                    "merge": [{"name": s["name"], "threads": s["rows"]} for s in items[1:]]})
    return out


def _merge_record_dir(con, record_dir):
    if record_dir:
        return record_dir
    row = con.execute("PRAGMA database_list").fetchone()
    db_file = (row["file"] if row is not None else "") or DB_PATH
    return os.path.join(os.path.dirname(os.path.abspath(db_file)), CATEGORY_MERGE_DIRNAME)


def _write_merge_record(path_dir, record, now, final=None):
    """Write the record (0600, fsynced, renamed into place) and return its path; `final` rewrites an
    existing record in place. Raises StoreError 500 when it cannot be written: the caller changes
    nothing without it."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    try:
        if final is None:
            os.makedirs(path_dir, mode=0o700, exist_ok=True)
            n = 0
            while True:
                final = os.path.join(path_dir, "category-merge-%s%s.json" % (stamp, "-%d" % n if n else ""))
                if not os.path.exists(final):
                    break
                n += 1
        tmp = final + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=1, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, final)
        return final
    except OSError as e:
        raise StoreError(500, "the category merge record could not be written (%s: %s); nothing was "
                              "changed" % (type(e).__name__, e.strerror or e))


def merge_category_case(con, apply=False, record_dir=None, now=None):
    """Merge the categories that differ only by case or whitespace into each group's canonical
    spelling (category_case_groups).

    Dry run (the default) answers {dry_run: true, groups, pairs, threads_to_rewrite} and writes
    nothing. With apply, under the write lock: the record {merges: [{from, to, thread_ids}], ...} is
    written first to `record_dir` (default: category-merges/ beside the database), then every thread
    row (tombstoned ones too) is rewritten, its search row refreshed, and the categories table keeps
    one row per group (the oldest first_seen, the newest last_seen). last_change and events are
    untouched: a spelling is not news. If the record cannot be written nothing changes. The record's
    `status` reads "not committed yet" until the rewrite commits, then "applied"; a rewrite that fails
    is rolled back and its record marked "rolled back"."""
    now = time.time() if now is None else now
    began = False
    if apply and not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
        began = True
    try:
        groups = category_case_groups(con)
        merges = []
        for g in groups:
            for v in g["merge"]:
                ids = [r["thread_id"] for r in con.execute(
                    "SELECT thread_id FROM threads WHERE category=? ORDER BY thread_id", (v["name"],))]
                merges.append({"from": v["name"], "to": g["canonical"], "thread_ids": ids})
        out = {"dry_run": not apply, "groups": groups, "pairs": len(merges),
               "threads_to_rewrite": sum(len(m["thread_ids"]) for m in merges)}
        if not apply:
            return out
        if not merges:
            if began:
                con.rollback()
            out.update(threads_rewritten=0, record=None)
            return out
        record = {"kind": "helm category case merge", "status": "not committed yet: written before the rewrite",
                  "written_at": dt.datetime.fromtimestamp(now, _LOCAL_TZ).isoformat(timespec="seconds"),
                  "ts": now, "pairs": len(merges), "threads": out["threads_to_rewrite"],
                  "merges": merges}
        path = _write_merge_record(_merge_record_dir(con, record_dir), record, now)
        try:
            rewritten = _rewrite_category_merges(con, merges)
            con.commit()
        except BaseException as e:
            if con.in_transaction:
                con.rollback()
            _mark_merge_record(path, record, now, "rolled back (%s): no thread changed" % type(e).__name__)
            raise
        _mark_merge_record(path, record, now, "applied")
        print("category merge applied: %d spellings, %d thread rows, record %s" % (len(merges), rewritten, path),
              file=sys.stderr, flush=True)
        out.update(threads_rewritten=rewritten, record=path)
        return out
    except BaseException:
        if began and con.in_transaction:
            con.rollback()
        raise


def _mark_merge_record(path, record, now, status):
    """Rewrite the record's status in place. Best effort: the rows are already settled either way, and
    a record still saying "not committed yet" is checked against the threads it names."""
    record = dict(record, status=status)
    if status == "applied":
        record["applied_at"] = dt.datetime.fromtimestamp(time.time(), _LOCAL_TZ).isoformat(timespec="seconds")
    try:
        _write_merge_record(os.path.dirname(path), record, now, final=path)
    except StoreError as e:
        print("category merge record %s: status not updated to %r: %s" % (path, status, e.detail),
              file=sys.stderr, flush=True)


def _rewrite_category_merges(con, merges):
    """The rows a merge rewrites: thread categories, their search rows, the categories table. Inside the
    caller's transaction; returns the thread rows rewritten."""
    rewritten = 0
    for m in merges:
        rewritten += con.execute("UPDATE threads SET category=? WHERE category=?",
                                 (m["to"], m["from"])).rowcount
        for tid in m["thread_ids"]:
            r = con.execute("SELECT title, summary, category FROM threads WHERE thread_id=?",
                            (tid,)).fetchone()
            if r is not None:
                _fts_thread(con, tid, r["title"], r["summary"], r["category"])
        old = con.execute("SELECT first_seen, last_seen FROM categories WHERE name=?",
                          (m["from"],)).fetchone()
        if old is not None:
            con.execute(
                "INSERT INTO categories (name, first_seen, last_seen) VALUES (?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET"
                " first_seen=CASE WHEN categories.first_seen <= 0 OR (excluded.first_seen > 0 AND"
                " excluded.first_seen < categories.first_seen) THEN excluded.first_seen"
                " ELSE categories.first_seen END,"
                " last_seen=MAX(categories.last_seen, excluded.last_seen)",
                (m["to"], old["first_seen"] or 0, old["last_seen"] or 0))
            con.execute("DELETE FROM categories WHERE name=?", (m["from"],))
    return rewritten


def _matching_suppression(con, title, sources):
    prod = producer_of(title, sources)
    fp = fingerprint(title)
    return con.execute(
        "SELECT * FROM suppressions WHERE producer=? AND fingerprint=? AND active=1",
        (prod, fp)).fetchone()


def put_threads(con, payload):
    """The producer's write. Upsert by thread_id; retire only what is named
    in `tombstones`.

    Partial success on purpose: every thread is validated before anything is
    written, a bad thread costs only itself, and the response names each
    rejection and why. One malformed item must never throw away a whole pass,
    and the producer can read the error and correct itself, whereas a human
    would just get a screen that fails to load.
    """
    now = time.time()
    raw_threads = payload.get("threads") or []
    tombstones = payload.get("tombstones") or []
    if not isinstance(raw_threads, list):
        raise StoreError(400, "threads must be a list")
    if not isinstance(tombstones, list):
        raise StoreError(400, "tombstones must be a list of thread_id strings")

    clean, rejected, refused_by_rule = [], [], []
    for i, t in enumerate(raw_threads):
        c, errs = validate_thread(t)
        if errs:
            rejected.append({
                "index": i,
                "thread_id": (t or {}).get("thread_id") if isinstance(t, dict) else None,
                "title": (t or {}).get("title") if isinstance(t, dict) else None,
                "errors": errs,
            })
            if isinstance(t, dict) and any(e.startswith("card rule ") for e in errs):
                refused_by_rule.append(t)
        else:
            clean.append((i, c))

    # One spelling per category: a case- or whitespace-only variant of a stored category is stored
    # as its canonical spelling (canonical_category), before any row is compared or written.
    if clean:
        canon = category_canon_map(con)
        for _i, c in clean:
            c["category"] = canonical_category(con, c["category"], canon)

    created = updated = retired = suppressed_on_arrival = 0
    preserved_done = preserved_answered = preserved_backlog = 0
    lit_idx = set()     # the clean threads whose incoming ask was stored lit: what reached the board

    for idx, c in clean:
        tid = c["thread_id"]
        existing = con.execute("SELECT * FROM threads WHERE thread_id=?", (tid,)).fetchone()

        if existing is None:
            rule = _matching_suppression(con, c["title"], c["sources"])
            if rule is not None:
                con.execute(
                    "INSERT INTO threads (thread_id, title, summary, category, urgency, state,"
                    " ask_kind, ask_prompt, ask_options, responder, sources, first_seen,"
                    " last_change, suppressed) VALUES (?,?,?,?,?,'done','none','','[]',?,?,?,?,1)",
                    (tid, c["title"], c["summary"], c["category"], c["urgency"],
                     c["responder"], json.dumps(c["sources"]), c["first_seen"] or now, now))
                _fts_thread(con, tid, c["title"], c["summary"], c["category"])
                con.execute("UPDATE suppressions SET hits=hits+1, last_hit=? WHERE id=?",
                            (now, rule["id"]))
                add_event(con, tid, now, "helm", "", "state-change",
                          "auto-retired on arrival by mute rule: " + (rule["note"] or "(no note)"))
                suppressed_on_arrival += 1
                continue

            # The needs-you four-test, producer's door (contract: P3b §1.2).
            # Fires only for a NEW ask reaching the board: a fresh lit thread.
            # A resync of a thread that already carries an ask under this
            # stable id is licensed (below); preservation branches never
            # reach a gate at all.
            if (rules().four_test and c["state"] == "needs-you"
                    and c["ask_kind"] in ("choice", "text")):
                nj_errors = needs_owner_gate_errors(con, c, None, now)
                if nj_errors:
                    rejected.append({
                        "index": idx,
                        "thread_id": tid,
                        "title": c["title"],
                        "errors": nj_errors,
                    })
                    continue

            # The needs-owner gate, producer's door: the same rules as post_result, for a NEW lit card.
            p_gate = None
            if rules().needs_owner_gate and c["state"] == "needs-you" and c["ask_kind"] != "none":
                g_decision, g_rule, g_version = gate_decision(
                    con, tid, c["title"], c["ask_kind"], c["ask_prompt"], c["ask_options"],
                    c["ask_context"], c["summary"])
                if g_decision:
                    from helm import gate_rules as _gr
                    p_gate = {"decision": g_decision, "rule": g_rule,
                              "reason": _gr.REASONS.get(g_rule, g_rule), "waiting_for": g_version,
                              "ask": json.dumps({"kind": c["ask_kind"], "prompt": c["ask_prompt"],
                                                 "options": c["ask_options"], "context": c["ask_context"]})}
                    c = dict(c)
                    if g_decision == "hold":
                        c["state"] = "waiting-external"
                        c["blocked_on"] = "a testable build %s on the testing channel (held by gate)" % g_version
                        c["recheck_at"] = now + GATE_RECHECK_S
                        c["gate_cmd"] = GATE_CMD_PREFIX + g_version
                    else:
                        c["state"] = "done"
            if p_gate is None:
                lit_idx.add(idx)

            con.execute(
                "INSERT INTO threads (thread_id, title, summary, category, urgency, state,"
                " ask_kind, ask_prompt, ask_options, ask_context, responder, sources, links,"
                " first_seen, last_change, blocked_on, recheck_at, gate_cmd, parked_since, label,"
                " ask_meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, c["title"], c["summary"], c["category"], c["urgency"], c["state"],
                 c["ask_kind"], c["ask_prompt"], json.dumps(c["ask_options"]),
                 c["ask_context"], c["responder"],
                 json.dumps(c["sources"]), json.dumps(c["links"]), c["first_seen"] or now, now,
                 c["blocked_on"], c["recheck_at"], c["gate_cmd"],
                 now if c["state"] == "waiting-external" else 0.0,
                 c.get("label", ""),
                 _ask_meta_to_store("", _ask_key(c["ask_kind"], c["ask_prompt"], json.dumps(c["ask_options"])),
                                    c["ask_subject"], c["ask_recap"], c["requires"])))
            _fts_thread(con, tid, c["title"], c["summary"], c["category"])
            created += 1
            if c["ask_kind"] in ("choice", "text"):
                src = producer_of(c["title"], c["sources"]) or "producer"
                add_event(con, tid, now, src, str(c.get("responder") or ""),
                          "bot-ask", _ask_event_body(c["ask_kind"], c["ask_prompt"], c["ask_options"], c["ask_context"]))
            if p_gate:
                if p_gate["decision"] == "hold":
                    g_body = ("held by gate (%s): %s. It comes back by itself when %s is on the testing "
                              "channel, or in 7 days; reopen it to see it now."
                              % (p_gate["rule"], p_gate["reason"], p_gate["waiting_for"]))
                else:
                    g_body = ("settled by gate (%s): %s. Not shown to the owner; reopen it to bring the "
                              "question back." % (p_gate["rule"], p_gate["reason"]))
                add_event(con, tid, now + 0.003, "helm-gate", "", "state-change", g_body)
                con.execute("UPDATE threads SET ask_kind='none', ask_prompt='', ask_options='[]',"
                            " ask_context='', gate_hold_ask=? WHERE thread_id=?", (p_gate["ask"], tid))
        elif existing["tombstoned"]:
            # A retired id is gone for writers as it is for readers (get_thread / the lists filter
            # tombstoned=0). Updating the row would answer `updated: 1` for a card nobody can see, so
            # the producer is told, in the rejection, to mint a new id for a new question.
            rejected.append({
                "index": idx,
                "thread_id": tid,
                "title": c["title"],
                "errors": ["thread %s was retired (tombstoned) and a retired thread stays retired: "
                           "it is not shown, so nothing written to it can reach the board. Use a new "
                           "thread_id for a new question" % tid],
            })
            continue
        else:
            new_state = c["state"]
            ask_kind, ask_prompt = c["ask_kind"], c["ask_prompt"]
            ask_options = json.dumps(c["ask_options"])
            ask_context = c["ask_context"]
            links = json.dumps(c["links"])
            meta_subject, meta_recap, meta_requires = c["ask_subject"], c["ask_recap"], c["requires"]
            incoming_lit = True     # cleared when an invariant keeps the stored thread as it was
            ask_was_changed = (
                existing["ask_kind"] != c["ask_kind"] or
                (existing["ask_prompt"] or "").strip() != (c["ask_prompt"] or "").strip() or
                _jloads(existing["ask_options"], []) != (c["ask_options"] or []) or
                (existing["ask_context"] or "").strip() != (c["ask_context"] or "").strip()
            )

            if existing["state"] == "done":
                new_state = "done"  # Invariant 2
                preserved_done += 1
                incoming_lit = False
            elif existing["state"] == "backlog":
                # Invariant 2 again, for the other state a human sets by hand
                # (work rule pack): a resync must not walk it back to needs-you.
                new_state = "backlog"
                preserved_backlog += 1
                incoming_lit = False
            elif _answer_is_in_flight(con, existing):
                new_state = existing["state"]  # Invariant 3
                ask_kind = existing["ask_kind"]
                ask_prompt = existing["ask_prompt"]
                ask_options = existing["ask_options"]
                ask_context = existing["ask_context"]
                meta_subject = meta_recap = ""   # the incoming words describe a question that was not stored
                meta_requires = []
                # A bot-result's links must not be clobbered by the next
                # routine resync any more than its ask_options are; see the
                # migrations comment on this column.
                links = existing["links"]
                preserved_answered += 1
                incoming_lit = False
            elif (rules().four_test and c["state"] == "needs-you"
                  and c["ask_kind"] in ("choice", "text")
                  and (existing["ask_kind"] or "none") == "none"):
                # The four-test, producer's door (contract: P3b §1.2): this
                # branch is reached only when no earlier invariant applies, so
                # the thread is live and no answer is in flight. Re-lighting a
                # thread whose stored ask is 'none' is a NEW ask reaching the
                # board -- the un-licensed case. Licensed resyncs (stored ask
                # != 'none') fall through to the plain UPDATE below.
                nj_errors = needs_owner_gate_errors(con, c, existing, now)
                if nj_errors:
                    rejected.append({
                        "index": idx,
                        "thread_id": tid,
                        "title": c["title"],
                        "errors": nj_errors,
                    })
                    continue

            # Park fields follow the state that is actually being STORED, not
            # the one the producer sent: if an invariant held the thread where
            # it was, its existing claim (or lack of one) stands, and a thread
            # that is no longer parked must not keep wearing a blocker it is no
            # longer waiting on. parked_since is preserved across resyncs so a
            # producer cannot reset the 30-day expiry clock by resending.
            if new_state == "waiting-external":
                if c["state"] == "waiting-external":
                    blocked_on, recheck_at = c["blocked_on"], c["recheck_at"]
                    gate_cmd = c["gate_cmd"]
                    # A resync restating the same claim with no explicit date
                    # keeps the stored recheck_at: recomputing the default
                    # would push it out on every pass, so a producer that
                    # resyncs hourly would never see recheck or the expiry.
                    if (not c.get("recheck_given")
                            and existing["state"] == "waiting-external"
                            and (existing["recheck_at"] or 0) > 0
                            and existing["blocked_on"] == blocked_on):
                        recheck_at = existing["recheck_at"]
                else:
                    blocked_on = existing["blocked_on"]
                    recheck_at = existing["recheck_at"]
                    gate_cmd = existing["gate_cmd"]
                parked_since = existing["parked_since"] or now
            else:
                blocked_on, recheck_at, gate_cmd, parked_since = "", 0.0, "", 0.0

            new_label = c.get("label")
            if not new_label and "label" in existing.keys():
                new_label = existing["label"] or ""
            # A resync of a live needs-you card that restates what it already says changes nothing,
            # so last_change stays: the phone and the page treat last_change as "something happened".
            new_meta = _ask_meta_to_store(existing["ask_meta"], _ask_key(ask_kind, ask_prompt, ask_options),
                                          meta_subject, meta_recap, meta_requires)
            quiet_resync = (
                existing["state"] == new_state == "needs-you"
                and not ask_was_changed
                and existing["title"] == c["title"] and existing["summary"] == c["summary"]
                and existing["category"] == c["category"] and existing["urgency"] == c["urgency"]
                and (existing["responder"] or "") == (c["responder"] or "")
                and _jloads(existing["sources"], []) == c["sources"]
                and _jloads(existing["links"], []) == c["links"]
                and (existing["label"] if "label" in existing.keys() else "") == (new_label or "")
                and ask_prompt == existing["ask_prompt"] and ask_options == existing["ask_options"]
                and new_meta == (existing["ask_meta"] or "")
                and not c["events"])
            con.execute(
                "UPDATE threads SET title=?, summary=?, category=?, urgency=?, state=?,"
                " ask_kind=?, ask_prompt=?, ask_options=?, ask_context=?, responder=?,"
                " sources=?, links=?,"
                " blocked_on=?, recheck_at=?, gate_cmd=?, parked_since=?,"
                " label=?, last_change=?, ask_meta=? WHERE thread_id=?",
                (c["title"], c["summary"], c["category"], c["urgency"], new_state,
                 ask_kind, ask_prompt, ask_options, ask_context, c["responder"],
                 json.dumps(c["sources"]), links, blocked_on, recheck_at, gate_cmd,
                 parked_since, new_label, existing["last_change"] if quiet_resync else now,
                 new_meta, tid))
            _fts_thread(con, tid, c["title"], c["summary"], c["category"])
            updated += 1
            if incoming_lit:
                lit_idx.add(idx)
            if (new_state != "done" and not _answer_is_in_flight(con, existing)
                    and c["ask_kind"] in ("choice", "text") and ask_was_changed):
                src = producer_of(c["title"], c["sources"]) or "producer"
                add_event(con, tid, now, src, str(c.get("responder") or ""),
                          "bot-ask", _ask_event_body(c["ask_kind"], c["ask_prompt"], c["ask_options"], c["ask_context"]))

        _touch_category(con, c["category"], now)
        for ev in c["events"]:
            if not isinstance(ev, dict):
                continue
            add_event(con, tid, ev.get("ts") or now, ev.get("source"), ev.get("source_id"),
                      ev.get("kind") or "update", ev.get("body") or "")

    for tid in tombstones:
        tid = str(tid).strip()
        if not tid:
            continue
        row = con.execute("SELECT thread_id FROM threads WHERE thread_id=?", (tid,)).fetchone()
        if row is None:
            continue
        con.execute("UPDATE threads SET tombstoned=1, state='done', last_change=?"
                    " WHERE thread_id=?", (now, tid))
        add_event(con, tid, now, "helm", "", "state-change", "retired: no longer true")
        retired += 1

    # The card-shape rules: what each producer sent that reached the board, once per question. A
    # thread a gate refused, a card muted, settled or held on arrival, and a resync an invariant kept
    # from changing the stored thread are not counted: the owner never saw that question. A card a
    # reject-mode rule refused is counted for the rules that refused it (mode reject).
    author_fb = payload.get("actor") or payload.get("author")
    for idx, c in clean:
        if c.get("card_rules") and idx in lit_idx:
            record_card_rules(con, "put", c["thread_id"], c["card_rules"], author_fallback=author_fb, now=now)
    for t in refused_by_rule:
        record_card_rules_refused(con, "put", t, author_fallback=author_fb, now=now)

    con.commit()

    result = {
        "ok": not rejected,
        # Threads actually stored. Pre-P3b every validated thread was always
        # written, so this equals len(clean) exactly for every ported caller;
        # the four-test's gate rejections (P3b §1.2) now reject inside the
        # write loop, and inflating 'accepted' with them would make a payload
        # of all-rejected read as accepted at the API door.
        "accepted": created + updated + suppressed_on_arrival,
        "rejected_count": len(rejected),
        "created": created,
        "updated": updated,
        "retired": retired,
        "suppressed_on_arrival": suppressed_on_arrival,
        "preserved_done": preserved_done,
        "preserved_backlog": preserved_backlog,
        "preserved_answered": preserved_answered,
    }
    if rejected:
        result["rejected"] = rejected
        result["detail"] = ("these threads were NOT stored; every other thread in the payload "
                            "was applied. Fix these and re-PUT just them.")
    return result


def _mint_hand_id(con, title, now):
    """A readable, date-stamped, unique id for a card written by hand.

    Minted here rather than supplied, which is the exact opposite of the rule
    for a producer's thread_id (validate_thread requires one, because a
    producer resyncs the same concern every pass and a missing id means a
    fresh set of duplicates every time). Nothing ever resends a hand-written
    card, so there is nothing for a stable id to key to, and asking the person
    typing it to invent one would be a field with no purpose. The date is in
    the id so writing the same title again next week is a new card rather than
    a collision, and the numeric suffix covers the same title twice in one day.
    """
    day = dt.datetime.fromtimestamp(now).strftime("%Y%m%d")
    base = HAND_ID_PREFIX + day + "-" + (slug(title, 40) or "note")
    tid, n = base, 2
    while con.execute("SELECT 1 FROM threads WHERE thread_id=?", (tid,)).fetchone():
        tid = "%s-%d" % (base, n)
        n += 1
    return tid


def create_thread(con, body, actor=None, caller=None):
    """One card, written by hand from the app. Not a producer's PUT.

    A PUT is a resync: it re-derives the whole picture every pass, keyed by
    ids the producer owns, and its invariants exist to stop it from
    overwriting decisions it knows nothing about. A hand-written card shares
    none of that. It arrives once, from the person the list is for, and the
    only thing it has to decide is what happens next. So it gets its own
    entry point instead of a flag on put_threads, and three deliberate
    differences from one:

      * The id is minted here (see _mint_hand_id).
      * Mute rules are NOT consulted. put_threads auto-retires an arriving
        thread that matches one, because a producer re-derives the same
        nagging item forever and the rule is how you make it stop. A card
        someone just typed is not that, and silently filing it as done
        would look exactly like the app dropping it.
      * `intent` sets state and ask together, so the shape the store's
        invariants care about cannot be posted half-formed. A form is not
        going to author a well-made ask, and invariant 4 would (rightly)
        reject a lit card without one.

    intent:
      mine     -- yours to deal with. Lit, with one option: Done.
      agent    -- yours, but hand it straight to a background run. Same card,
                  plus the instruction filed as an answer, which is what the
                  dispatcher watches for. `instruction` is required: without
                  it a run has nothing to act on and can only hand it back.
      waiting  -- on someone else. Not lit, so it carries no question; it
                  sits in the list until you park it, close it, or it moves.

    `caller` is the HTTP caller's name when it did not say it was the owner (see
    handoff_source): a hand-off from a watcher is recorded as the watcher's.
    """
    if not isinstance(body, dict):
        raise StoreError(400, "body must be a JSON object")

    title = str(body.get("title") or "").strip()
    if not title:
        raise StoreError(400, "'title' is required: it is the line you will read on the list")
    if len(title) > MAX_TITLE:
        raise StoreError(400, "title is %d chars, max %d; put the detail in 'summary'"
                              % (len(title), MAX_TITLE))

    summary = str(body.get("summary") or "").strip()
    category = str(body.get("category") or "").strip()
    urgency = str(body.get("urgency") or "whenever").strip()
    if urgency not in VALID_URGENCY:
        raise StoreError(400, "urgency %r not one of %s" % (urgency, sorted(VALID_URGENCY)))

    intent = str(body.get("intent") or "mine").strip()
    if intent not in VALID_INTENT:
        raise StoreError(400, "intent %r not one of %s" % (intent, sorted(VALID_INTENT)))

    instruction = str(body.get("instruction") or "").strip()
    if intent == "agent" and not instruction:
        raise StoreError(400, "intent 'agent' needs an 'instruction': it is the whole brief "
                              "for the run, and a run with nothing to act on can only hand "
                              "the card straight back to you")
    if len(instruction) > MAX_NOTE:
        raise StoreError(400, "instruction is %d chars, max %d" % (len(instruction), MAX_NOTE))

    links, link_errors = normalize_links(body.get("links"))
    if link_errors:
        raise StoreError(400, "; ".join(link_errors))

    # Born snoozed: `wake_at` files the card already hidden, in the one insert, so no read between a
    # create and a follow-up snooze can ever see it lit. Checked before anything is written.
    wake_raw = body.get("wake_at")
    wake_at_val, wake_text = 0.0, ""
    if wake_raw is not None and wake_raw != "":
        if intent != "mine":
            raise StoreError(400, "'wake_at' snoozes a card for you to deal with; intent %r cannot be "
                                  "created snoozed. Use intent 'mine'" % intent)
        wake_at_val = _parse_wake_at(wake_raw) if isinstance(wake_raw, (str, int, float)) \
            and not isinstance(wake_raw, bool) else None
        if not wake_at_val or wake_at_val <= 0:
            raise StoreError(400, "'wake_at' must be epoch seconds or ISO-8601")
        if wake_at_val <= time.time():
            raise StoreError(400, "'wake_at' is in the past, which would hide this forever")
        if wake_at_val - time.time() > MAX_SNOOZE_S:
            raise StoreError(400, "'wake_at' is more than 400 days out: that is abandonment "
                                  "wearing a deferral's clothes")
        wake_text = dt.datetime.fromtimestamp(wake_at_val, _LOCAL_TZ).strftime("%Y-%m-%d %H:%M")

    park = ("", 0.0, "")
    if intent == "waiting":
        # A wait someone typed by hand is still a wait, and the same contract
        # applies (P1 §1): the form asks who it waits on, because a card that
        # waits on nobody is a card that was dropped and looks like one that is
        # moving. Loud at creation rather than discovered weeks later. (Work:
        # an undated wait is legal; the 7-day re-ask is its exit.)
        if rules().park_contract:
            park = require_park(body)
        state, ask_kind, ask_prompt, options = "waiting-external", "none", "", []
    else:
        state, ask_kind = "needs-you", "choice"
        ask_prompt = "Is this finished?"
        options = [{"label": "Done", "value": "done",
                    "effect": "Settles it here and marks the work finished. "
                              "Nothing is written anywhere else.",
                    "terminal": True, "resolved": True}]

    label = str(body.get("label") or "").strip().lower()
    if label:
        lbl = get_label(con, label)
        if lbl is None:
            raise StoreError(400, f"label {label!r} does not exist in labels table")
        if not lbl["enabled"]:
            raise StoreError(400, f"label {label!r} is disabled")
    elif rules().intake_labels:
        settings = get_settings(con)
        def_lbl = settings.get("default_intake_label", "").strip().lower()
        if def_lbl and get_label(con, def_lbl):
            label = def_lbl

    now = time.time()
    category = canonical_category(con, category)
    tid = _mint_hand_id(con, title, now)
    wake_note = wake_ask = wake_mode = ""
    wake_set_at = 0.0
    if wake_at_val:
        # The same row snooze() would leave: hidden, the question it comes back with stashed.
        wake_ask = json.dumps({"kind": ask_kind, "prompt": ask_prompt, "options": options, "context": ""})
        wake_note = str(body.get("wake_note") or "").strip() or "Snoozed - back %s" % dt.datetime.fromtimestamp(
            wake_at_val, _LOCAL_TZ).strftime("%b %d %H:%M")
        wake_mode, wake_set_at = "reminder", now
        state, ask_kind, ask_prompt, options = "waiting-external", "none", "", []
    con.execute(
        "INSERT INTO threads (thread_id, title, summary, category, urgency, state,"
        " ask_kind, ask_prompt, ask_options, responder, sources, links, first_seen,"
        " last_change, blocked_on, recheck_at, gate_cmd, parked_since, label,"
        " wake_at, wake_note, wake_ask, wake_mode, wake_set_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        # responder stays NULL: it records who last reported on a thread (an
        # executor stamps its own name there), and nothing has yet.
        (tid, title, summary, category, urgency, state, ask_kind, ask_prompt,
         json.dumps(options), None, json.dumps([]), json.dumps(links), now, now,
         park[0], park[1], park[2], now if state == "waiting-external" else 0.0, label,
         wake_at_val, wake_note, wake_ask, wake_mode, wake_set_at))
    _fts_thread(con, tid, title, summary, category)
    _touch_category(con, category, now)
    # Only a hand-off: the owner's phone files `mine` cards with the bearer alone.
    src = (handoff_source(actor, caller) if intent == "agent" else None) or BY_OWNER
    add_event(con, tid, now, src, "", "state-change",
              "you wrote this card yourself"
              + (", filed as waiting on someone else" if intent == "waiting" else ""))
    if wake_at_val:
        add_event(con, tid, now, BY_OWNER, "", "state-change", "snoozed until " + wake_text)
    con.commit()

    handed_off = False
    if intent == "agent":
        # The same path any answer takes, on purpose: the dispatcher picks up
        # a reply newer than the last dispatch and knows nothing about where
        # the card came from, so there is no second queue to keep working.
        reply(con, tid, HAND_TASK_VALUE, note=instruction, by=actor,
              source=None if src == BY_OWNER else src)
        handed_off = True

    row = con.execute("SELECT * FROM threads WHERE thread_id=?", (tid,)).fetchone()
    out = {"created": True, "thread_id": tid, "state": row["state"],
           "intent": intent, "handed_off": handed_off, "label": row["label"]}
    if wake_at_val:
        out.update(state="snoozed", wake_at=wake_at_val, hidden_until=wake_at_val)
    return out


def _require_thread(con, thread_id):
    row = con.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
    if row is None:
        raise StoreError(404, "no such thread: " + str(thread_id))
    return row


# --- Attachments are instructions (contract: P1 §5; LESSON 19, supporting 15)-
#
# Sending a file IS the instruction. "Here's the invoice" plus a tap on Done is
# not a person closing a thread, it is a person handing over the thing the work
# needs and expecting it to be used; settling on that tap throws the file away
# in the same motion that received it, silently, and nobody finds out until the
# work comes back wrong. Same treatment as a binding note (LESSON 15).
#
# The store keeps metadata only. Bytes are the API's business (contract: P1 §6).
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_FILENAME = 255
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def _human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def normalize_attachment(meta):
    """Coerce one attachment's metadata. Returns (clean, errors).

    Hand-written rather than schema-driven for the reason the rest of this file
    is: the producer has to be able to read the rejection and fix its own
    payload without a second round trip.
    """
    errors = []
    if not isinstance(meta, dict):
        return None, ["attachment is " + type(meta).__name__ + ", expected an object with "
                      "filename, mime, size and sha256"]
    filename = str(meta.get("filename") or meta.get("name") or "").strip()
    if not filename:
        errors.append("attachment has no 'filename': history names the file, because "
                      "'1 attachment' tells the reader nothing about what arrived")
    if len(filename) > MAX_FILENAME:
        errors.append("attachment filename is %d chars, max %d" % (len(filename),
                                                                   MAX_FILENAME))
    if "\x00" in filename or "/" in filename or "\\" in filename:
        errors.append("attachment filename %r contains a path separator or a NUL: send the "
                      "bare file name; where the bytes live is the 'url'" % filename)
    mime = str(meta.get("mime") or meta.get("content_type") or "").strip()
    if not mime:
        errors.append("attachment %r has no 'mime': the reader decides whether they can "
                      "open it before they tap anything" % filename)
    size = meta.get("size", meta.get("bytes"))
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = -1
    if size <= 0:
        errors.append("attachment %r has size %r: a zero-byte file is a failed upload "
                      "wearing an instruction's clothes" % (filename, meta.get("size")))
    elif size > MAX_ATTACHMENT_BYTES:
        errors.append("attachment %r is %s, over the %s limit"
                      % (filename, _human_size(size), _human_size(MAX_ATTACHMENT_BYTES)))
    sha = str(meta.get("sha256") or "").strip().lower()
    if not _SHA256_HEX.match(sha or ""):
        errors.append("attachment %r has sha256 %r, which is not 64 hex characters. It is "
                      "what tells a re-sent file from a second one" % (filename, sha))
    # Same reason as thread_id (see IDENT_RE): the att_id a caller sends is
    # stored verbatim and is the file name the bytes are read back under, one
    # level below the thread's own directory. Omit it and the store mints one.
    att_id = str(meta.get("att_id") or "").strip()
    if att_id and not ident_ok(att_id):
        errors.append("attachment %r has att_id %r, which is not an identifier: it must "
                      "match %s. It becomes the file name the bytes are stored and served "
                      "under, so a separator or a leading dot reads and writes outside the "
                      "thread's own folder. Leave it out and the store mints one"
                      % (filename, att_id[:80], IDENT_RULE))
    if errors:
        return None, errors
    return {
        "att_id": att_id or uuid.uuid4().hex,
        "ts": float(meta.get("ts") or time.time()),
        "filename": filename,
        "mime": mime,
        "size": size,
        "sha256": sha,
        "url": str(meta.get("url") or ""),
        # An attachment that does not say who sent it is an agent's, not the owner's.
        # Defaulting to "owner" made every worker-attached diagram look like his
        # answer and consumed the card it was attached to (7 instances 2026-09-19).
        "uploader": str(meta.get("uploader") or "agent"),
        "caption": str(meta.get("caption") or ""),
    }, []


def add_attachment(con, thread_id, meta):
    """Record a file that arrived on this thread, and say so in history.

    The event body names the file, its size and where it is, and tells the
    reader to OPEN it before answering. "Attachment added" would be the
    status-word failure (LESSON 11): history is event bodies, and a body that
    describes nothing is a line nobody can act on.
    """
    _require_thread(con, thread_id)
    clean, errors = normalize_attachment(meta)
    if errors:
        raise StoreError(400, "; ".join(errors))
    dupe = con.execute("SELECT * FROM attachments WHERE thread_id=? AND sha256=?",
                       (thread_id, clean["sha256"])).fetchone()
    if dupe is not None:
        # The same bytes twice is a retry, not a second instruction.
        return dict(dupe, duplicate=True)
    con.execute(
        "INSERT INTO attachments (att_id, thread_id, ts, filename, mime, size, sha256, url,"
        " uploader, caption) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (clean["att_id"], thread_id, clean["ts"], clean["filename"], clean["mime"],
         clean["size"], clean["sha256"], clean["url"], clean["uploader"], clean["caption"]))
    body = ("FILE ATTACHED -- OPEN IT BEFORE YOU ANSWER: %s (%s, %s)%s%s"
            % (clean["filename"], clean["mime"], _human_size(clean["size"]),
               ("\n" + clean["caption"]) if clean["caption"] else "",
               ("\n" + clean["url"]) if clean["url"] else ""))
    # The kind follows the uploader: only the owner's own file is a owner-attachment,
    # which is the kind the park answer gate accepts as his reply.
    kind = "owner-attachment" if clean["uploader"] == "owner" else "agent-attachment"
    add_event(con, thread_id, clean["ts"], clean["uploader"], clean["att_id"],
              kind, body)
    con.commit()
    return dict(clean, thread_id=thread_id, duplicate=False)


def attachments_for(con, thread_id):
    return [dict(r) for r in con.execute(
        "SELECT * FROM attachments WHERE thread_id=? ORDER BY ts ASC", (thread_id,))]


def agent_attachment_totals(con, thread_id):
    """(count, bytes) of the files agents left on this thread: what an agent's next upload is
    measured against (helm/preview.py). the owner's own files are not counted."""
    row = con.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM attachments"
                      " WHERE thread_id=? AND LOWER(uploader)<>'owner'", (thread_id,)).fetchone()
    return int(row[0]), int(row[1])


def unconsumed_attachments(con, thread_id):
    """Files that arrived in the round nothing has acted on yet.

    "Unconsumed" is measured against the newest bot-result, the same test the
    answer gate uses on a note: a file is consumed when something has reported
    back after it, not when it has merely been looked at.
    """
    last_report = con.execute(
        "SELECT MAX(ts) FROM events WHERE thread_id=? AND kind='bot-result'",
        (thread_id,)).fetchone()[0] or 0
    return [dict(r) for r in con.execute(
        "SELECT * FROM attachments WHERE thread_id=? AND ts > ? ORDER BY ts ASC",
        (thread_id, last_report))]


def _attachment_hold(pending):
    """The one sentence every close-tap gives back when files are waiting."""
    names = ", ".join("%s (%s)" % (a["filename"], _human_size(a["size"])) for a in pending)
    return ("this reply carries %d file(s) nothing has acted on yet -- %s. A file sent with "
            "an answer is an instruction, so it goes to whoever does the work instead of "
            "being closed away with the card" % (len(pending), names))


def reply(con, thread_id, value, note="", attachments=None, by=None, source=None, press=None):
    """An answer, with an optional note that is binding on whoever executes it.

    The note is not a comment field. It rides with the answer and wins over
    the option text on material contradiction. So does a file: `attachments`
    may be sent inline with the reply, and a reply carrying one never settles
    (contract: P1 §5).

    `by` is who answered (work rule pack): the owner, or a chat session answering on
    his word. Home callers never send it and the event source stays "owner",
    except for a hand-off from a machine, whose `source` (handoff_source) is
    written instead so pending-dispatch never calls its brief the owner's note.

    `press` is the proof that this tap came from the owner's signed-in page (`{"via": WEB_UI, "login": ...}`,
    built by the api role from the web role's key, never from a request body). A `__use_draft__:` value
    is refused without it, and the event it makes carries it.
    """
    row = _require_thread(con, thread_id)
    # A direct store call with no `by` is the owner's own tap (the earlier app's default too);
    # the HTTP doors apply the site's default for a missing value before calling.
    by = BY_OWNER if by is None else normalize_by(by)
    # The event source: home history always reads "owner" (no session concept there);
    # on a second site a session's answer is written as the session's.
    src = source or (by if rules().by_default_session else BY_OWNER)
    via = "" if by == BY_OWNER else " from a chat session"
    if value is None or (isinstance(value, str) and not value.strip()):
        raise StoreError(400, "'value' is required: which option was chosen")
    if not isinstance(value, str):
        value = json.dumps(value)
    note = str(note or "")
    if len(note) > MAX_NOTE:
        raise StoreError(400, "note is %d chars, max %d" % (len(note), MAX_NOTE))

    pressed = is_press_value(value)
    if pressed:
        # One press, one attempt: take the write lock before the check and keep it to the insert, so
        # a second press cannot slip in between (it waits, then finds the draft used).
        _require_press_proof(value, by, press)   # no database needed: a refusal never takes the write lock
        began_tx = not con.in_transaction
        if began_tx:
            con.execute("BEGIN IMMEDIATE")
        try:
            # The card may have been closed since the row was read above: ask again, now that we hold the lock.
            cur = con.execute("SELECT state FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
            _refuse_bad_use_draft(con, thread_id, value, by, press, cur["state"] if cur else row["state"])
        except StoreError:
            if began_tx:   # roll back only what this call began: the caller's own writes stay
                con.rollback()
            raise

    reply_k = classify_reply(value, note=note, attachments=attachments)

    # The work rule pack: a chat session may not settle over an unsent draft
    # (409) unless its note says where the draft went. the owner's own tap always may.
    chosen = next((o for o in _jloads(row["ask_options"], [])
                   if o.get("value") == value), None)
    if (row["ask_kind"] == "ack" and value == "ack") or bool(chosen and chosen.get("terminal")):
        _refuse_unsent_settle(con, thread_id, by, " ".join(
            [note, value, str((chosen or {}).get("label") or "")]))

    now = _after_latest(con, thread_id)
    # Inline files, recorded BEFORE the settle predicates below read them: a
    # file sent with the tap is part of the tap (contract: P1 §5).
    added = [add_attachment(con, thread_id, m) for m in (attachments or [])]
    add_event(con, thread_id, now, src, "", "owner-reply", value, reply_kind=reply_k,
              via=WEB_UI if pressed else "", via_login=str(press["login"]).strip() if pressed else "")
    if note.strip():
        add_event(con, thread_id, now + 0.001, src, "", "owner-note", note)
    # Contract: P1 §4. A fresh answer resets the two assertions: whatever an
    # earlier result claimed it had applied, it cannot have applied THIS.
    con.execute("UPDATE threads SET answer_applied=0, nothing_owed=0 WHERE thread_id=?",
                (thread_id,))

    # Contract: P1 §5, LESSON 19. Every close-tap below asks this first. A file
    # nothing has acted on yet turns a settling tap into working: the answer
    # goes out carrying the files, instead of the files going in the bin with
    # the card. Sending a file and settling in the same act is the one
    # combination that silently throws the file away.
    pending_files = unconsumed_attachments(con, thread_id)

    # An ack ask exists for exactly one reason: "reading it is genuinely the
    # whole job" (see validate_thread's rejection text and build_prompt's own
    # instructions to the executor). There is nothing an executor could do
    # with a plain "Got it" that the ack didn't already cover, so settle it
    # here, the same as a terminal choice option, rather than waking one that
    # can only report back that nothing needed doing and hand back the same
    # ack question. A bot may never mark a thread done, and that is exactly
    # why this had no exit before: the executor was correctly forbidden from
    # closing it, so an ack-only card could ONLY loop until this tap did it
    # instead. Keyed to the literal value "ack", not to ask_kind alone, so a
    # redirect (value "__redirect__", sent with a note instead) still reaches
    # an executor to interpret.
    if row["ask_kind"] == "ack" and value == "ack" and not pending_files:
        add_event(con, thread_id, now + 0.002, src, "", "state-change",
                  "settled" + via + " by acknowledging: the ask was only ever \"read this\"")
        con.execute("UPDATE threads SET state='done', ask_lit_key='', ask_kind='none', ask_prompt='',"
                    " ask_options='[]', ask_context='', wake_at=0, last_note=?, last_change=?"
                    " WHERE thread_id=?", (note, now, thread_id))
        con.commit()
        # An ack is never an assertion that the work is finished, only that
        # reading it was the whole job, so it is never `resolved`.
        return {"accepted": True, "thread_id": thread_id, "state": "done",
                "terminal": True, "resolved": False, "note_recorded": bool(note.strip()),
                "reply_kind": reply_k}

    # A terminal option is the whole action. Settle it here rather than
    # queueing an executor that has nothing to do and would come back saying
    # it could not deliver what the option promised.
    if chosen and chosen.get("terminal") and not pending_files:
        add_event(con, thread_id, now + 0.002, src, "", "state-change",
                  "settled" + via + " by choosing " + repr(chosen.get("label") or value))
        con.execute("UPDATE threads SET state='done', ask_lit_key='', ask_kind='none', ask_prompt='',"
                    " ask_options='[]', ask_context='', wake_at=0, last_note=?, last_change=?"
                    " WHERE thread_id=?", (note, now, thread_id))
        con.commit()
        return {"accepted": True, "thread_id": thread_id, "state": "done",
                "terminal": True, "resolved": bool(chosen.get("resolved")),
                "note_recorded": bool(note.strip()), "reply_kind": reply_k}

    # wake_at is cleared here for the same reason the two settling branches
    # above clear it: he has just engaged with this thread, so the deferral he
    # (or a run) put on it is spent. Leaving it set hid a thread that was
    # actively being worked -- list_threads filters on wake_at, not on state --
    # and then let wake_due() slam it back to 'needs-you' on the old date,
    # overwriting the live work with the stale question it was deferred with.
    con.execute("UPDATE threads SET state='working', wake_at=0, wake_note='', wake_ask='',"
                " wake_mode='', wake_set_at=0, last_note=?, last_change=? WHERE thread_id=?", (note, now, thread_id))
    con.commit()
    out = {"accepted": True, "thread_id": thread_id, "state": "working",
           "terminal": False, "note_recorded": bool(note.strip()), "reply_kind": reply_k}
    if pending_files:
        would_have_settled = ((row["ask_kind"] == "ack" and value == "ack")
                              or bool(chosen and chosen.get("terminal")))
        out["attachments"] = [a["filename"] for a in pending_files]
        if would_have_settled:
            # Said out loud in the return AND in history: a tap that did not do
            # what the button said must never look like it did.
            reason = _attachment_hold(pending_files)
            out["settlement_suppressed"] = True
            out["reason"] = reason
            add_event(con, thread_id, now + 0.002, src, "", "state-change",
                      "not settled on this tap: " + reason)
            con.commit()
    return out


def close_thread(con, thread_id, reason="", actor=None, force=False, by=None):
    """The user closes it. Always succeeds: a tap that worked must not look
    like it failed because a downstream write-back did.

    `actor` defaults to "owner", so the UI's Close button and every ported
    caller are unchanged. Anything else is refused (contract: P1 §4): this is
    the human close path, and a bot that believes a thread is finished has
    exactly two mechanical exceptions, both of which run through post_result
    where they can be checked against what he actually tapped.

    `force` is the one way past an unconsumed attachment (contract: P1 §5).
    The tap still works when it is meant; it just cannot happen by accident in
    the same breath as sending a file.
    """
    row = _require_thread(con, thread_id)
    pending_files = unconsumed_attachments(con, thread_id)
    if pending_files and not force:
        raise StoreError(400, "not closed: " + _attachment_hold(pending_files)
                              + ". Answer the card so the files reach a run, or close it "
                                "with force=true if you really do want them dropped")
    # `by` is work's spelling of `actor`; either names who closed it. A direct
    # store call naming nobody is the owner's own tap.
    given = actor if actor is not None else by
    actor = BY_OWNER if given is None else normalize_by(given)
    act_s = str(actor or "owner").strip().lower()
    now = time.time()
    if not rules().bot_close_rules:
        # The work rule pack: the owner closes, or a chat session closes on his word. A
        # session's close is logged as the session's and refused while the thread
        # holds an unsent draft its reason does not account for.
        _refuse_unsent_settle(con, thread_id, actor, reason)
        who = "closed by you" if actor == BY_OWNER else "closed from a chat session"
        body = who + ((": " + reason) if reason else "")
        add_event(con, thread_id, now, actor, "", "state-change", body, reply_kind="close")
        con.execute("UPDATE threads SET state='done', ask_lit_key='', ask_kind='none', ask_prompt='',"
                    " ask_options='[]', ask_context='', wake_at=0, last_change=? WHERE thread_id=?",
                    (now, thread_id))
        con.commit()
        return {"accepted": True, "thread_id": thread_id, "state": "done", "reply_kind": "close"}
    if act_s == "bot":
        last_owner = con.execute(
            "SELECT rowid, ts, kind, body, reply_kind FROM events "
            "WHERE thread_id=? AND (source='owner' OR kind IN ('owner-reply', 'owner-note')) "
            "ORDER BY rowid DESC LIMIT 1", (thread_id,)
        ).fetchone()
        if not last_owner:
            raise StoreError(400, "actor %r may not close a thread with no replies from the owner" % actor)
        is_note = (
            last_owner["kind"] == "owner-note"
            or (last_owner["kind"] == "owner-reply" and (
                (last_owner["body"] or "").strip().lower() in ("note-only", "")
                or str(last_owner["reply_kind"] or "").startswith("note-")
            ))
        )
        # A tapped option he chose is a decision the run may have carried out; the
        # same bar as post_result's answered-decision exception applies below.
        is_decision_tap = (
            last_owner["kind"] == "owner-reply" and not is_note
            and (last_owner["body"] or "").strip().lower() not in NON_DECISION_REPLIES
        )
        if not (is_note or is_decision_tap):
            raise StoreError(400, "actor %r may only close a thread when the last the owner event was a note "
                                  "or a tapped option (got %r)" % (actor, last_owner["body"]))
        # The same bar as bot_close_reason at the /result done door: a close wipes the ask,
        # so a question put to him after his last word, with no reply from him newer than
        # it, is not the bot's to bury. (b) and (c) below can be manufactured by the same
        # bot that asked, so they cannot be what stands between it and his question. The
        # bot-ask event is the record, not the row: a parked result with no ask on it
        # blanks the row's ask without his having answered anything.
        if (newest_ask_ts(con, thread_id, _generation_start(con, thread_id))
                > float(last_owner["ts"] or 0)):
            raise StoreError(400, "a bot may not close a thread over a question he has not "
                                  "answered: there is no reply from him newer than the question "
                                  "currently on this card (a bare 'ack'/'close' tap or a note "
                                  "with no choice in it is not a decision). Report with state "
                                  "'needs-you' and leave the question up")
        bot_res = con.execute(
            "SELECT rowid, ts FROM events WHERE thread_id=? AND kind='bot-result' AND rowid > ? "
            "ORDER BY rowid DESC LIMIT 1", (thread_id, last_owner["rowid"])
        ).fetchone()
        if not bot_res:
            raise StoreError(400, "actor %r may only close a thread after a bot-result following the owner's note" % actor)
        if not row["answer_applied"]:
            raise StoreError(400, "actor %r may only close a thread when answer_applied is true" % actor)
        if is_decision_tap and not row["nothing_owed"]:
            raise StoreError(400, "actor %r may only close a thread after a tapped option when nothing_owed "
                                  "is true" % actor)

        body = "closed by the agent" + ((": " + reason) if reason else "")
        add_event(con, thread_id, now, "bot", "", "state-change", body, reply_kind="close")
        con.execute("UPDATE threads SET state='done', ask_lit_key='', ask_kind='none', ask_prompt='',"
                    " ask_options='[]', ask_context='', wake_at=0, last_change=? WHERE thread_id=?",
                    (now, thread_id))
        con.commit()
        return {"accepted": True, "thread_id": thread_id, "state": "done", "reply_kind": "close",
                "closed_by": "bot"}

    elif act_s != "owner":
        raise StoreError(400, "actor %r may not close a thread: only the owner closes. A run that "
                              "believes a thread is finished reports it with post_result -- "
                              "either on an ack he has tapped, or on a decision he answered "
                              "with answer_applied: true and nothing_owed: true -- or it "
                              "hands the close back to him as an ack ask in the same post"
                              % actor)
    body = "closed by you" + ((": " + reason) if reason else "")
    add_event(con, thread_id, now, "owner", "", "state-change", body, reply_kind="close")
    con.execute("UPDATE threads SET state='done', ask_lit_key='', ask_kind='none', ask_prompt='',"
                " ask_options='[]', ask_context='', wake_at=0, last_change=? WHERE thread_id=?",
                (now, thread_id))
    con.commit()
    return {"accepted": True, "thread_id": thread_id, "state": "done", "reply_kind": "close"}


def reopen_thread(con, thread_id, note=""):
    """Reverse a settle. The one place that is allowed to defy invariant 2.

    Invariant 2 stops a PRODUCER pass from dragging a done thread back to
    life without knowing what was already decided there. This is not that:
    it is you, directly, saying the settle itself was wrong (a mis-tap, most
    often). A terminal reply and an explicit close both clear ask_kind /
    ask_prompt / ask_options the moment they fire, so by the time a thread
    can be reopened its original options are already gone. This does not
    pretend to reconstruct them; it asks again, as a plain text question,
    which is the honest version of "I don't know what the options were
    anymore" rather than a guess dressed up as a memory.
    """
    row = _require_thread(con, thread_id)
    gate_held = (row["state"] == "waiting-external"
                 and str(row["gate_cmd"] or "").startswith(GATE_CMD_PREFIX))
    if row["state"] != "done" and not gate_held:
        raise StoreError(400, "only a 'done' thread can be reopened; this one is %r"
                              % row["state"])
    now = time.time()
    body = "reopened by you" + ((": " + note) if note else "")
    add_event(con, thread_id, now, "owner", "", "state-change", body)
    if row["suppressed"]:
        # A muted thread is hidden from the board and the dispatch queue by suppressed=1, and
        # its mute rule keeps retiring restatements. Bringing it back means both go: there is no
        # other unmute, and a "needs-you" that stays invisible is a lost card.
        con.execute("UPDATE threads SET suppressed=0 WHERE thread_id=?", (thread_id,))
        con.execute("UPDATE suppressions SET active=0 WHERE producer=? AND fingerprint=?",
                    (producer_of(row["title"], _jloads(row["sources"], [])),
                     fingerprint(row["title"])))
    if row["gate_hold_ask"]:
        # The gate kept this from him and he wanted it: the question comes back as it was,
        # and the miss is recorded, because a gate that is never told it was wrong never is.
        _gate_release(con, thread_id, row, now + 0.001,
                      "gate miss: the owner brought back a card the gate had %s"
                      % ("held" if gate_held else "settled"))
        con.commit()
        return {"accepted": True, "thread_id": thread_id, "state": "needs-you", "gate_miss": True}
    con.execute(
        "UPDATE threads SET state='needs-you', ask_kind='text',"
        " ask_prompt='Reopened. The original options are gone; say what you want done.',"
        " ask_options='[]', ask_context='', wake_at=0, answer_applied=0, nothing_owed=0,"
        " last_change=? WHERE thread_id=?",
        (now, thread_id))
    con.commit()
    return {"accepted": True, "thread_id": thread_id, "state": "needs-you"}


def mute(con, thread_id, note="", force=False):
    """Retire this thread AND stop asking about this kind of thing.

    This is the endpoint that actually drains a backlog rather than
    reshuffling it, so it is reversible and auditable by construction: the
    rule is listed with a hit count, never a silent black hole.

    It also settles the thread, which makes it a close tap, which makes it
    subject to the attachment rule (contract: P1 §5): muting a card he has just
    sent a file to would drop the file and the rule that produced it in one go.
    """
    row = _require_thread(con, thread_id)
    pending_files = unconsumed_attachments(con, thread_id)
    if pending_files and not force:
        raise StoreError(400, "not muted: " + _attachment_hold(pending_files)
                              + ". Mute it with force=true if the files are genuinely no "
                                "longer wanted")
    now = time.time()
    sources = _jloads(row["sources"], [])
    prod = producer_of(row["title"], sources)
    fp = fingerprint(row["title"])
    con.execute(
        "INSERT INTO suppressions (id, producer, fingerprint, sample, note, created, active)"
        " VALUES (?,?,?,?,?,?,1) ON CONFLICT(producer, fingerprint) DO UPDATE SET"
        " active=1, note=excluded.note",
        (uuid.uuid4().hex, prod, fp, row["title"], str(note or ""), now))
    con.execute("UPDATE threads SET state='done', suppressed=1, ask_lit_key='', ask_kind='none',"
                " ask_prompt='', ask_options='[]', ask_context='', wake_at=0,"
                " last_change=? WHERE thread_id=?",
                (now, thread_id))
    add_event(con, thread_id, now, "owner", "", "state-change",
              "muted: " + (str(note) or "stop asking about this kind of thing"))
    con.commit()
    return {"accepted": True, "thread_id": thread_id, "producer": prod,
            "fingerprint": fp, "subject": normalize_subject(row["title"])}


def _parse_wake_at(raw):
    """Epoch seconds or ISO-8601. A naive date is read in host local time,
    because that is the clock 'in a week' means to a person."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        try:
            return float(raw) if math.isfinite(raw) else None   # NaN is no date; it stored as NULL
        except OverflowError:                                    # an int beyond any float
            return None
    s = str(raw).strip()
    try:
        f = float(s)
        return f if math.isfinite(f) else None
    except ValueError:
        pass
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_LOCAL_TZ)
    return d.timestamp()


def snooze(con, thread_id, wake_at_raw=None, note="", hours=None, wake_at=None):
    """Defer a thread to a date, from the UI, in one action.

    Deferral is a button rather than a convention somebody has to remember,
    because the half-done version of it (park the thread and separately
    remember to bring it back) turns a deferral into a silent drop. The live
    ask is stashed so it comes back carrying the same question, and cleared
    so a hidden thread is not also lit underneath.
    """
    row = _require_thread(con, thread_id)
    if row["state"] == "done":
        raise StoreError(400, "thread is closed; reopening is a re-file, not a snooze")

    now = time.time()
    label = None
    if hours is not None:
        try:
            hours_f = float(hours)
        except (TypeError, ValueError):
            raise StoreError(400, "'hours' must be a number (the UI sends 1, 24, 168 or 720)")
        if hours_f <= 0:
            raise StoreError(400, "'hours' must be positive")
        wake_at_val = now + hours_f * 3600.0
        if wake_at_val - now > MAX_SNOOZE_S:
            raise StoreError(400, f"'hours' is more than {MAX_SNOOZE_S // 3600}h out - a snooze is a reminder, not an archive")
        whole_hours = int(hours_f) if hours_f == int(hours_f) else None
        label = {1: "1h", 24: "1d", 168: "1w", 720: "1mo"}.get(whole_hours) or f"{hours_f:g}h"
    elif wake_at_raw is not None or wake_at is not None:
        target = wake_at_raw if wake_at_raw is not None else wake_at
        wake_at_val = _parse_wake_at(target) or 0.0
        if wake_at_val <= 0:
            raise StoreError(400, "'wake_at' is required, as epoch seconds or ISO-8601")
        if wake_at_val <= now:
            raise StoreError(400, "'wake_at' is in the past, which would hide this forever")
        if wake_at_val - now > MAX_SNOOZE_S:
            raise StoreError(400, "'wake_at' is more than 400 days out: that is abandonment "
                                  "wearing a deferral's clothes")
    else:
        raise StoreError(400, "'hours' is required and must be a number (the UI sends 1, 24, 168 or 720)")

    # Preserve the ask the card will return WITH. A thread that is already snoozed has had its
    # live ask cleared into wake_ask, so re-snoozing it must keep that stash: reading the cleared
    # live row would replace the real question with a generic ack.
    stash = _jloads(row["wake_ask"], {}) if float(row["wake_at"] or 0) > 0 else {}
    resnooze = isinstance(stash, dict) and bool(stash) and (row["ask_kind"] or "none") == "none"
    if resnooze:
        kept = stash
    else:
        ask_kind = row["ask_kind"]
        if ask_kind not in VALID_ASK or ask_kind == "none":
            ask_kind = "ack"
        ask_prompt = row["ask_prompt"] or f"Bringing this back: {row['title']}"
        options, _ = normalize_options(_jloads(row["ask_options"], []) or [])
        if ask_kind == "choice" and not options:
            ask_kind = "ack"
        kept = {
            "kind": ask_kind,
            "prompt": ask_prompt,
            "options": options,
            "context": row["ask_context"]
        }
    # A run's recheck (mode, when it was set, its own note) survives the owner pushing the date out.
    keep_recheck = resnooze and (row["wake_mode"] or "") == "recheck"

    clean_note = str(note or "").strip()
    if label:
        wake_note = clean_note or f"Snoozed {label} - back {dt.datetime.fromtimestamp(wake_at_val, _LOCAL_TZ).strftime('%b %d %H:%M')}"
        event_body = f"snoozed by the owner for {label}" + (f" - {clean_note}" if clean_note else "")
    else:
        wake_note = clean_note or f"Snoozed - back {dt.datetime.fromtimestamp(wake_at_val, _LOCAL_TZ).strftime('%b %d %H:%M')}"
        event_body = f"snoozed until {dt.datetime.fromtimestamp(wake_at_val, _LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}" + (f": {clean_note}" if clean_note else "")

    wake_mode, wake_set_at = "reminder", now
    if keep_recheck:
        wake_mode = "recheck"
        wake_set_at = float(row["wake_set_at"] or 0) or now
        if not clean_note and (row["wake_note"] or "").strip():
            wake_note = row["wake_note"]

    add_event(con, thread_id, now, "owner", "", "state-change", event_body)
    con.execute(
        "UPDATE threads SET state='waiting-external', ask_kind='none', ask_prompt='',"
        " ask_options='[]', ask_context='', wake_at=?, wake_note=?, wake_ask=?,"
        " wake_mode=?, wake_set_at=?, last_change=? WHERE thread_id=?",
        (wake_at_val, wake_note, json.dumps(kept), wake_mode, wake_set_at, now, thread_id))
    con.commit()
    return {
        "accepted": True,
        "thread_id": thread_id,
        "state": "snoozed",
        "wake_at": wake_at_val,
        "hidden_until": wake_at_val,
        "wakes": dt.datetime.fromtimestamp(wake_at_val, _LOCAL_TZ).isoformat()
    }


def set_parked(con, thread_id, parked, reason="", source="owner"):
    """Label a waiting-external thread as deliberately set aside, or bring it
    back into the plain in-flight list.

    Deliberately kept out of put_threads(): a producer resync only ever
    resends title/summary/category/etc, and those already overwrite
    unconditionally on every PUT (see put_threads' UPDATE), so if `parked`
    lived there too, an ordinary resync that simply forgot to repeat it would
    silently un-park something a human, or a reviewed sweep, had deliberately
    set aside. Routing it through its own endpoint means the only way this
    flag ever changes is a direct, reasoned call, never a side effect of
    something else refreshing.

    Restricted to `waiting-external` on the way IN, not on the way out: a
    thread has to actually be a live, unanswered wait before it can be
    called parked, but unparking a thread that has since moved on (answered,
    settled, reopened) should never itself be an error, since the fix here is
    just to stop it from misleadingly registering as a park.

    A reason is required to park something, and not to un-park it, on
    purpose: parking without saying why is exactly the failure this exists to
    prevent, a pile of things nobody can tell apart from a real wait. Bringing
    something back into view is never the half that needs explaining.
    """
    row = _require_thread(con, thread_id)
    parked = bool(parked)
    reason = str(reason or "").strip()
    if parked:
        if row["state"] != "waiting-external":
            raise StoreError(400, "only a 'waiting-external' thread can be parked (this one is "
                                  "%r); answer it, or use snooze for a hidden recheck instead"
                                  % row["state"])
        if not reason:
            raise StoreError(400, "'reason' is required to park a thread: say what it is "
                                  "actually waiting on and why nothing is pushing it, so a "
                                  "later look can tell a real wait from a dropped one")
        if len(reason) < MIN_RESULT_NOTE:
            raise StoreError(400, "'reason' is %d chars, minimum %d. A one-line reason is as "
                                  "unreviewable as no reason at all" % (len(reason), MIN_RESULT_NOTE))
    if len(reason) > MAX_NOTE:
        raise StoreError(400, "'reason' is %d chars, max %d" % (len(reason), MAX_NOTE))

    now = time.time()
    add_event(con, thread_id, now, source, "", "park" if parked else "unpark",
              reason or "brought back into the plain in-flight list")
    con.execute("UPDATE threads SET parked=?, parked_reason=? WHERE thread_id=?",
                (1 if parked else 0, reason, thread_id))
    con.commit()
    return {"accepted": True, "thread_id": thread_id, "parked": parked, "reason": reason}


def set_backlog(con, thread_id, backlog, reason="", source="owner"):
    """Move a lit thread into the backlog, or pull one back out (the earlier app, 09-19).

    The backlog is what needs attention but not now: a finding to process
    later, work deferred by choice, a question raised where nobody was at the
    keyboard. Only a `needs-you` thread can go in, because deferring is an
    answer to a question, and a thread with no live question has nothing to
    defer. Coming back out is unrestricted, the way unparking is. A reason is
    required going in (one line; the floor is deliberately lower than a park's)
    and not coming out. Coming back out sets `needs-you`: the question is what
    it was, and it is live again.
    """
    row = _require_thread(con, thread_id)
    backlog = bool(backlog)
    reason = str(reason or "").strip()
    if backlog:
        if row["state"] == "backlog":
            raise StoreError(400, "this thread is already in the backlog")
        if row["state"] != "needs-you":
            raise StoreError(400, "only a 'needs-you' thread can go to the backlog (this one "
                                  "is %r). A thread with no live question has nothing to "
                                  "defer: use park for a real wait, snooze for a dated "
                                  "recheck, or close it" % row["state"])
        if not reason:
            raise StoreError(400, "'reason' is required: one line on what this is and why it "
                                  "is not now, so a later look can tell a deliberate deferral "
                                  "from something that just fell out of view")
    if len(reason) > MAX_NOTE:
        raise StoreError(400, "'reason' is %d chars, max %d" % (len(reason), MAX_NOTE))

    now = time.time()
    if backlog:
        add_event(con, thread_id, now, source, "", "backlog", reason)
        con.execute("UPDATE threads SET state='backlog', last_change=? WHERE thread_id=?",
                    (now, thread_id))
    else:
        add_event(con, thread_id, now, source, "", "unbacklog",
                  reason or "pulled back out of the backlog")
        con.execute("UPDATE threads SET state='needs-you', last_change=? WHERE thread_id=?",
                    (now, thread_id))
    con.commit()
    return {"accepted": True, "thread_id": thread_id, "backlog": backlog,
            "state": "backlog" if backlog else "needs-you", "reason": reason}


# --- The read API (the pollers and the screens) --------------------------------------
def get_thread(con, thread_id, with_events=True):
    """One thread with its events, or None. Tombstoned rows are gone for readers."""
    r = con.execute("SELECT * FROM threads WHERE thread_id=? AND tombstoned=0",
                    (thread_id,)).fetchone()
    if r is None:
        return None
    return _row_to_thread(con, r, with_events=with_events)


def list_events(con, since=None, until=None, thread=None, kind=None, limit=200):
    """Events in a range, ordered by time, joined to their thread's title and state."""
    sql = ("SELECT e.event_id, e.ts, e.source, e.source_id, e.kind, e.body, e.reply_kind, e.via, e.via_login,"
           " t.thread_id, t.title, t.category, t.state, t.urgency"
           " FROM events e JOIN threads t ON t.thread_id = e.thread_id WHERE t.tombstoned=0")
    args = []
    if since is not None:
        sql += " AND e.ts >= ?"
        args.append(float(since))
    if until is not None:
        sql += " AND e.ts < ?"
        args.append(float(until))
    if thread:
        sql += " AND e.thread_id = ?"
        args.append(str(thread))
    if kind:
        sql += " AND e.kind = ?"
        args.append(str(kind))
    sql += " ORDER BY e.ts ASC, e.rowid ASC LIMIT ?"
    args.append(max(1, min(int(limit or 200), 5000)))
    return [dict(r) for r in con.execute(sql, args)]


# The THREADS list: every thread the site has ever held, done ones included,
# newest first, in light rows, paged and filtered server-side so a thousand rows stay fast.
INDEX_STATES = ("needs-you", "working", "waiting-external", "backlog", "done", "muted")
INDEX_LIMIT_MAX = 500
INDEX_PAGE_MAX = 10_000_000


def _index_row(r) -> dict:
    """One light row of the index: what a list needs to draw and sort, no events."""
    return {
        "thread_id": r["thread_id"], "title": r["title"], "category": r["category"] or "Uncategorized",
        "urgency": r["urgency"], "state": "muted" if r["suppressed"] else r["state"],
        "muted": bool(r["suppressed"]), "ask_kind": r["ask_kind"], "responder": r["responder"] or "",
        "first_seen": r["first_seen"], "last_change": r["last_change"], "wake_at": r["wake_at"],
        "parked": bool(r["parked"]), "blocked_on": r["blocked_on"] or "", "recheck_at": r["recheck_at"],
        "last_kind": r["last_kind"] or "", "last_source": r["last_source"] or "",
        "agent": r["agent"] or "", "events": r["n_events"] or 0,
    }


def index_threads(con, states=None, since=None, until=None, who=None, q=None, page=1, limit=100, ids=None):
    """Every thread the site has ever held (tombstoned ones excepted), newest first, in light
    rows, one page at a time.

    states: a subset of INDEX_STATES; "muted" is a suppressed thread, the others are live
      states of unsuppressed threads. Empty means every one.
    since / until: epoch seconds on last_change (a date range).
    who: a substring of the responder or of any event's source (an agent or a runner).
    q: full-text search (every token, prefix), the FTS5 index when it exists and the same
      scan search_threads falls back to otherwise.
    ids: only these thread ids (the gate's held-by-gate view lists what it kept by id).
    Returns total (every thread held), matched (after the filters), the facets by_state and
    agents (over every thread, so the chips keep their numbers while a filter is on), and
    the page. An unknown state or a bad page raises StoreError(400)."""
    states = [str(s) for s in (states or []) if str(s)]
    bad = [s for s in states if s not in INDEX_STATES]
    if bad:
        raise StoreError(400, "unknown state %s; states are %s" % (", ".join(bad), ", ".join(INDEX_STATES)))
    try:
        page = max(1, int(page or 1))
        limit = max(1, min(int(limit or 100), INDEX_LIMIT_MAX))
    except (TypeError, ValueError):
        raise StoreError(400, "'page' and 'limit' must be integers")
    if page > INDEX_PAGE_MAX:  # the OFFSET must fit SQLite's integer, or the query raises (a 500)
        raise StoreError(400, "'page' is too large (max %d)" % INDEX_PAGE_MAX)
    where = ["t.tombstoned=0"]
    args = []
    if states:
        parts = []
        if "muted" in states:
            parts.append("t.suppressed=1")
        live = [s for s in states if s != "muted"]
        if live:
            parts.append("(t.suppressed=0 AND t.state IN (%s))" % ",".join("?" * len(live)))
            args.extend(live)
        where.append("(" + " OR ".join(parts) + ")")
    if since is not None:
        where.append("t.last_change >= ?")
        args.append(float(since))
    if until is not None:
        where.append("t.last_change < ?")
        args.append(float(until))
    who = str(who or "").strip()
    if who:
        like = "%" + who.lower() + "%"
        where.append("(LOWER(COALESCE(t.responder,'')) LIKE ? OR EXISTS (SELECT 1 FROM events e"
                     " WHERE e.thread_id=t.thread_id AND LOWER(e.source) LIKE ?))")
        args.extend([like, like])
    ids = [str(i) for i in (ids or []) if str(i)]
    if ids:
        where.append("t.thread_id IN (%s)" % ",".join("?" * len(ids)))
        args.extend(ids)
    q_ids = None
    toks = [x for x in str(q or "").split() if x]
    if toks:
        q_ids = None
        if _fts_available(con):
            try:
                q_ids = {r[0] for r in con.execute(
                    "SELECT DISTINCT thread_id FROM search_fts WHERE search_fts MATCH ?", (_fts_query(q),))}
            except sqlite3.OperationalError:
                q_ids = None
        if q_ids is None:
            q_ids = set()
            low = [x.lower() for x in toks]
            for r in con.execute(
                    "SELECT t.thread_id, t.title, t.summary, t.category,"
                    " (SELECT group_concat(body, ' ') FROM events e WHERE e.thread_id=t.thread_id) AS ev"
                    " FROM threads t WHERE t.tombstoned=0"):
                hay = ((r["title"] or "") + " " + (r["summary"] or "") + " " + (r["category"] or "")
                       + " " + (r["ev"] or "")).lower()
                if all(x in hay for x in low):
                    q_ids.add(r["thread_id"])
        if not q_ids:
            where.append("0")
        else:
            con.execute("CREATE TEMP TABLE IF NOT EXISTS _index_q (thread_id TEXT PRIMARY KEY)")
            con.execute("DELETE FROM _index_q")
            con.executemany("INSERT OR IGNORE INTO _index_q VALUES (?)", [(i,) for i in q_ids])
            where.append("t.thread_id IN (SELECT thread_id FROM _index_q)")
    cond = " AND ".join(where)
    matched = con.execute("SELECT COUNT(*) n FROM threads t WHERE " + cond, args).fetchone()["n"]
    total = con.execute("SELECT COUNT(*) n FROM threads t WHERE t.tombstoned=0").fetchone()["n"]
    by_state = {s: 0 for s in INDEX_STATES}
    for r in con.execute("SELECT CASE WHEN suppressed=1 THEN 'muted' ELSE state END AS s, COUNT(*) n"
                         " FROM threads WHERE tombstoned=0 GROUP BY s"):
        by_state[r["s"]] = by_state.get(r["s"], 0) + r["n"]
    agents = [{"agent": r["a"], "count": r["n"]} for r in con.execute(
        "SELECT responder AS a, COUNT(*) n FROM threads WHERE tombstoned=0 AND COALESCE(responder,'') != ''"
        " GROUP BY responder ORDER BY n DESC, a ASC LIMIT 24")]
    rows = con.execute(
        "SELECT t.*,"
        " (SELECT kind FROM events e WHERE e.thread_id=t.thread_id ORDER BY e.ts DESC, e.rowid DESC LIMIT 1) AS last_kind,"
        " (SELECT source FROM events e WHERE e.thread_id=t.thread_id ORDER BY e.ts DESC, e.rowid DESC LIMIT 1) AS last_source,"
        " (SELECT source FROM events e WHERE e.thread_id=t.thread_id AND e.source NOT IN ('owner','helm','helm-gate','')"
        "   ORDER BY e.ts DESC, e.rowid DESC LIMIT 1) AS agent,"
        " (SELECT COUNT(*) FROM events e WHERE e.thread_id=t.thread_id) AS n_events"
        " FROM threads t WHERE " + cond + " ORDER BY t.last_change DESC, t.first_seen DESC, t.thread_id"
        " LIMIT ? OFFSET ?", args + [limit, (page - 1) * limit]).fetchall()
    if q_ids is not None and q_ids:
        con.execute("DELETE FROM _index_q")
    pages = max(1, (matched + limit - 1) // limit)
    return {"generated_at": time.time(), "total": total, "matched": matched, "page": page,
            "pages": pages, "limit": limit, "by_state": by_state, "agents": agents,
            "threads": [_index_row(r) for r in rows]}


def search_threads(con, q, limit=30):
    """Threads whose title, summary, category or any event body carries every token,
    settled ones included; needs-you first, then newest. The FTS5 index (search_fts)
    does the matching when it exists; see init()."""
    toks = [t.lower() for t in str(q or "").split() if t]
    if not toks:
        return []
    cap = max(1, min(int(limit or 30), 200))
    if _fts_available(con):
        try:
            ids = [r[0] for r in con.execute(
                "SELECT DISTINCT thread_id FROM search_fts WHERE search_fts MATCH ?",
                (_fts_query(q),))]
        except sqlite3.OperationalError:
            ids = None  # an unparseable query (stray operators): fall back to the scan
        if ids is not None:
            if not ids:
                return []
            # A subquery rather than IN (?,?,...): a broad query can match more
            # ids than SQLite's bound-parameter cap on older builds.
            rows = [r for r in con.execute(
                "SELECT * FROM threads WHERE tombstoned=0 AND thread_id IN"
                " (SELECT thread_id FROM search_fts WHERE search_fts MATCH ?)", (_fts_query(q),))]
            rows.sort(key=lambda r: (0 if r["state"] == "needs-you" else 1, -(r["last_change"] or 0)))
            return [_row_to_thread(con, r, summary=True) for r in rows[:cap]]
    rows = con.execute(
        "SELECT t.*, (SELECT group_concat(body, ' ') FROM events e WHERE e.thread_id=t.thread_id)"
        " AS ev FROM threads t WHERE t.tombstoned=0").fetchall()
    hits = []
    for r in rows:
        hay = ((r["title"] or "") + " " + (r["summary"] or "") + " " + (r["category"] or "")
               + " " + (r["ev"] or "")).lower()
        if all(t in hay for t in toks):
            hits.append(r)
    hits.sort(key=lambda r: (0 if r["state"] == "needs-you" else 1, -(r["last_change"] or 0)))
    return [_row_to_thread(con, r, summary=True) for r in hits[:cap]]


def switcher_threads(con, q, limit=10):
    """Title-only matches for a quick switcher: id, title, category, state."""
    toks = [t.lower() for t in str(q or "").split() if t]
    if not toks:
        return []
    rows = con.execute("SELECT thread_id, title, category, state, last_change FROM threads"
                       " WHERE tombstoned=0").fetchall()
    hits = [r for r in rows if all(t in (r["title"] or "").lower() for t in toks)]
    hits.sort(key=lambda r: (0 if r["state"] == "needs-you" else 1, -(r["last_change"] or 0)))
    return [dict(r) for r in hits[:limit]]


def append_note(con, thread_id, note, actor="", dedupe_key=""):
    """Additive note append that provably never flips thread state.

    Contract (Helm intake rebuild slice A, 2026-09-15):
    MAY touch: exactly one new `events` row (kind 'note') plus the
      `threads.last_note` display mirror (surfaced read-only by
      GET /threads; pending-dispatch reply_note derives from reply events).
    NEVER touches: state, ask_kind/prompt/options/context, blocked_on,
      recheck_at, gate_cmd, parked_since, parked, parked_reason, wake_*,
      dispatched_at, dispatch_attempts, last_dispatch_try, last_change,
      tombstoned, suppressed, responder, urgency, category, title, summary,
      sources, links, label, or any other thread column; never deletes or
      rewrites an existing event. Dispatch safety: pending_dispatch keys
      only on owner-reply events newer than dispatched_at and
      stale_dispatches keys on working + dispatched_at + absence of
      bot-result, so a kind='note' event cannot re-arm dispatch on any
      state.
    Idempotency: dedupe_key rides in events.source_id. A repeat carrying
      the same (thread_id, kind='note', source_id) returns deduped=True
      with no new row; distinct appends always append, so concurrent
      writers neither duplicate nor drop entries.
    """
    row = _require_thread(con, thread_id)
    if not isinstance(note, str):
        raise StoreError(400, "'note' is required and must be a string")
    note = note.strip()
    if not note:
        raise StoreError(400, "'note' must not be empty")
    if len(note) > MAX_NOTE:
        raise StoreError(400, "'note' is %d chars, max %d" % (len(note), MAX_NOTE))
    actor = str(actor or "").strip() or "bot"
    dedupe_key = str(dedupe_key or "")
    now = time.time()
    if dedupe_key:
        dup = con.execute(
            "SELECT ts FROM events WHERE thread_id=? AND kind='note' AND source_id=?",
            (thread_id, dedupe_key)).fetchone()
        if dup is not None:
            count = con.execute(
                "SELECT COUNT(*) FROM events WHERE thread_id=?", (thread_id,)).fetchone()[0]
            return {"accepted": True, "thread_id": thread_id, "state": row["state"],
                    "event_ts": dup["ts"], "deduped": True, "history_count": count}
    add_event(con, thread_id, now, actor, dedupe_key, "note", note)
    con.execute("UPDATE threads SET last_note=? WHERE thread_id=?", (note, thread_id))
    con.commit()
    count = con.execute(
        "SELECT COUNT(*) FROM events WHERE thread_id=?", (thread_id,)).fetchone()[0]
    return {"accepted": True, "thread_id": thread_id, "state": row["state"],
            "event_ts": now, "deduped": False, "history_count": count}


def record_operational_error(con, trace_id, component, event, details):
    """Persist an unhandled error or severe failure to operational_errors table."""
    import uuid
    err_id = "err-%s" % uuid.uuid4().hex[:12]
    now = time.time()
    con.execute(
        "INSERT INTO operational_errors (error_id, ts, trace_id, component, event, details) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (err_id, now, str(trace_id or ""), str(component or "gateway"), str(event or "error"), str(details or ""))
    )
    con.commit()
    return err_id


REFUSALS_KEEP_S = 14 * 86400
REFUSALS_MAX = 5000
_REFUSAL_FIELD_MAX = 200


def record_refusal(con, method, route, thread_id, status, client="", user_agent="", actor="",
                   login="", principal="", detail="", caller="", batch="", now=None):
    """One row for a request the api refused, then prune: older than REFUSALS_KEEP_S goes, and
    the newest REFUSALS_MAX stay. Every text field is cut to _REFUSAL_FIELD_MAX."""
    def cut(v):
        return str(v or "")[:_REFUSAL_FIELD_MAX]
    now = time.time() if now is None else now
    con.execute(
        "INSERT INTO refused_requests (ts, method, route, thread_id, status, client, user_agent,"
        " actor, login, principal, detail, caller, batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, cut(method), cut(route), cut(thread_id), int(status), cut(client), cut(user_agent),
         cut(actor), cut(login), cut(principal), cut(detail), cut(caller), cut(batch)))
    con.execute("DELETE FROM refused_requests WHERE ts < ?", (now - REFUSALS_KEEP_S,))
    con.execute("DELETE FROM refused_requests WHERE id <= (SELECT MAX(id) FROM refused_requests) - ?",
                (REFUSALS_MAX,))
    con.commit()


# --- The card-shape rules (helm/card_rules.py): the verdict, the record, the stats --------------
CARD_RULES_KEEP_S = 31 * 86400
CARD_RULES_MAX = 50000
CARD_RULES_DOORS = ("check", "put", "result")


def card_rule_check(kind, prompt, context, options, subject, recap, author=""):
    """The verdict for one ask: {fired, warn, reject, key, author}. `warn` are the rules in log mode,
    `reject` those in reject mode; a rule in off mode is in neither and is not recorded."""
    fired = _card_rules.evaluate(kind, prompt, context, options, subject, recap)
    mode_map = _card_rules.modes()
    on = [r for r in fired if mode_map.get(r) != "off"]
    warn, reject = _card_rules.split(on, mode_map)
    return {"fired": on, "warn": warn, "reject": reject, "author": author,
            "key": _card_rules.question_key(kind, prompt, options)}


def card_rule_check_raw(t):
    """card_rule_check for a raw thread payload, read leniently: the check route answers warnings
    even for a card the validator refuses. None when no rule applies (another site, no lit ask)."""
    if not rules().card_rules or not isinstance(t, dict):
        return None
    ask = t.get("ask") if isinstance(t.get("ask"), dict) else {}
    kind = str(ask.get("kind") or "none")
    if str(t.get("state") or "needs-you") != "needs-you" or kind not in VALID_ASK or kind == "none":
        return None
    options, _errs = normalize_options(ask.get("options"))
    subject, recap, _meta = ask_meta_fields(ask)
    return card_rule_check(kind, str(ask.get("prompt") or ""), str(ask.get("context") or ""), options,
                           subject, recap,
                           _card_rules.author_of(ask.get("author"), t.get("author"), t.get("responder")))


def record_card_rules(con, door, thread_id, verdict, author_fallback="", principal=None, now=None):
    """One row per rule the ask broke (rule 'none' when it broke none), at most once per
    (door, thread, rule, question): a resync of the same question adds nothing. Never commits, so a
    door whose write is rolled back leaves no row. Prunes rows older than CARD_RULES_KEEP_S and
    keeps the newest CARD_RULES_MAX. Writes one stderr line per new warning: rule id, door, thread,
    principal -- no card text."""
    if not verdict:
        return 0
    # A raw thread id from the check route may carry anything; the row and the log line get one
    # printable token, so no id can forge a second line.
    thread_id = re.sub(r"[^\x21-\x7e]+", "_", str(thread_id or ""))
    now = time.time() if now is None else now
    principal = _tokens.PRINCIPAL.get("") if principal is None else principal
    author = verdict.get("author") or _card_rules.author_of(author_fallback)
    mode_map = _card_rules.modes()
    rules_ = list(verdict.get("fired") or []) or ["none"]
    added = 0
    for rule in rules_:
        mode = mode_map.get(rule, "") if rule != "none" else ""
        cur = con.execute(
            "INSERT OR IGNORE INTO card_rule_warnings (ts, door, thread_id, rule, mode, principal,"
            " author, ask_key) VALUES (?,?,?,?,?,?,?,?)",
            (now, door, str(thread_id or "")[:200], rule, mode, str(principal or "")[:64], author,
             verdict.get("key") or ""))
        if cur.rowcount and rule != "none":
            added += 1
            print("card-rule %s door=%s thread=%s rule=%s principal=%s author=%s"
                  % (mode or "log", door, str(thread_id or "")[:120], rule, principal or "-", author or "-"),
                  file=sys.stderr, flush=True)
    con.execute("DELETE FROM card_rule_warnings WHERE ts < ?", (now - CARD_RULES_KEEP_S,))
    con.execute("DELETE FROM card_rule_warnings WHERE id <= (SELECT MAX(id) FROM card_rule_warnings) - ?",
                (CARD_RULES_MAX,))
    return added


def record_card_rules_refused(con, door, t, author_fallback="", principal=None, now=None):
    """The row a reject-mode refusal earns: only the rules that refused the card, mode reject, so a
    rule that refuses a card still reads as fired in the stats. `t` is the raw thread payload."""
    verdict = card_rule_check_raw(t)
    if not verdict or not verdict["reject"]:
        return 0
    verdict = dict(verdict, fired=list(verdict["reject"]))
    return record_card_rules(con, door, t.get("thread_id"), verdict, author_fallback=author_fallback,
                             principal=principal, now=now)


def card_rule_stats(con, since, until, doors=("put", "result")):
    """Warning counts for asks recorded in [since, until] at the given doors.

    An ask is one (door, thread, question). `asks` counts every ask checked, `asks_warned` those that
    broke at least one rule; `by_rule`, `by_principal` and `by_author` break the same asks down.
    """
    doors = [d for d in doors if d in CARD_RULES_DOORS] or list(CARD_RULES_DOORS)
    marks = ",".join("?" * len(doors))
    rows = con.execute(
        "SELECT door, thread_id, ask_key, rule, principal, author FROM card_rule_warnings"
        " WHERE ts >= ? AND ts <= ? AND door IN (%s)" % marks,
        [float(since), float(until)] + doors).fetchall()
    asks = {}
    for r in rows:
        a = asks.setdefault((r["door"], r["thread_id"], r["ask_key"]),
                            {"principal": r["principal"], "author": r["author"], "rules": set()})
        if r["rule"] != "none":
            a["rules"].add(r["rule"])

    def group(key):
        out = {}
        for a in asks.values():
            g = out.setdefault(a[key], {"asks": 0, "asks_warned": 0,
                                        "by_rule": {r: 0 for r in _card_rules.RULE_IDS}})
            g["asks"] += 1
            g["asks_warned"] += 1 if a["rules"] else 0
            for r in a["rules"]:
                g["by_rule"][r] = g["by_rule"].get(r, 0) + 1   # a retired rule id still counts
        return out

    by_rule = {r: 0 for r in _card_rules.RULE_IDS}
    for a in asks.values():
        for r in a["rules"]:
            by_rule[r] = by_rule.get(r, 0) + 1
    by_door = {}
    for (door, _t, _k), a in asks.items():
        d = by_door.setdefault(door, {"asks": 0, "asks_warned": 0})
        d["asks"] += 1
        d["asks_warned"] += 1 if a["rules"] else 0
    return {
        "since": float(since), "until": float(until), "doors": doors,
        "asks": len(asks), "asks_warned": sum(1 for a in asks.values() if a["rules"]),
        "by_rule": by_rule, "by_door": by_door,
        "by_principal": group("principal"), "by_author": group("author"),
    }


def list_refusals(con, thread_id="", since=0.0, limit=100):
    """Refusals newest first, optionally for one thread id and/or newer than `since`."""
    # Pruning runs on write only, so an idle table can still hold rows past the window.
    since = max(float(since or 0), time.time() - REFUSALS_KEEP_S)
    sql, args = "SELECT * FROM refused_requests WHERE ts >= ?", [since]
    if thread_id:
        # The id of the request's path, or any card of a refused PUT batch (exact id, never a prefix).
        sql += " AND (thread_id = ? OR instr(',' || batch || ',', ',' || ? || ',') > 0)"
        args += [str(thread_id), str(thread_id)]
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), REFUSALS_MAX)))
    return [dict(r) for r in con.execute(sql, args)]


# --- The needs-owner gate. Rules in helm/gate_rules.py. A card the rules drop is settled
# with a visible reason; a card they hold parks until its build is on the testing channel
# (re-checked by a sweeper) or 7 days pass; everything else is lit exactly as before.
# Hand cards are never gated.
GATE_RECHECK_S = 3600
GATE_MAX_HOLD_S = 7 * 86400
GATE_EXEMPT_PREFIXES = (HAND_ID_PREFIX,)
GATE_CMD_PREFIX = "helm-gate:build "
_GATE_HISTORY_KINDS = ("bot-ask", "owner-reply", "owner-note", "bot-result", "update")


def _gate_state(con, thread_id, title, ask_body, current_note):
    """What the rules read: the card as it would be shown, and the thread so far."""
    hist = [{"text": str(r["body"] or "")[:300]} for r in con.execute(
        "SELECT body FROM events WHERE thread_id=? AND kind IN (%s) AND body != '__task__'"
        " ORDER BY ts" % ",".join("?" * len(_GATE_HISTORY_KINDS)),
        (thread_id,) + _GATE_HISTORY_KINDS).fetchall()]
    hist.append({"text": str(current_note or "")[:300]})
    return {"card_title": title or "", "card": ask_body, "thread_so_far": hist[-8:]}


def gate_decision(con, thread_id, title, ask_kind, ask_prompt, ask_options, ask_context,
                  current_note=""):
    """(decision, rule, version) for an ask about to light, or (None, None, None).
    A hold with no version to check is sent: never held blind."""
    from helm import gate_rules
    if ask_kind == "none" or not gate_rules.enabled():
        return None, None, None
    if any(str(thread_id).startswith(p) for p in GATE_EXEMPT_PREFIXES):
        return None, None, None
    state = _gate_state(con, thread_id, title,
                        _ask_event_body(ask_kind, ask_prompt, ask_options, ask_context), current_note)
    decision, rule = gate_rules.decide(state)
    if decision in (None, "send") or not gate_rules.enabled(rule):
        return None, None, None
    version = gate_rules.build_version(state) if decision == "hold" else None
    if decision == "hold" and not version:
        return None, None, None
    return decision, rule, version


def gate_log(con, days=14):
    """Every thread the gate acted on in the last `days`, newest first: the held-by-gate view."""
    since = time.time() - days * 86400
    rows = con.execute(
        "SELECT e.thread_id, MAX(e.ts) AS ts FROM events e JOIN threads t ON t.thread_id = e.thread_id"
        " WHERE e.source='helm-gate' AND e.kind='state-change' AND e.ts >= ?"
        " AND t.tombstoned=0 AND t.suppressed=0"
        " GROUP BY e.thread_id ORDER BY ts DESC", (since,)).fetchall()
    out = []
    for r in rows:
        ev = con.execute("SELECT body, ts FROM events WHERE thread_id=? AND source='helm-gate'"
                         " AND kind='state-change' ORDER BY ts DESC LIMIT 1", (r["thread_id"],)).fetchone()
        t = con.execute("SELECT title, state, gate_cmd, blocked_on FROM threads WHERE thread_id=?",
                        (r["thread_id"],)).fetchone()
        body = ev["body"] if ev else ""
        decision = ("miss" if body.startswith("gate miss") else "released" if body.startswith("released")
                    else "held" if body.startswith("held") else "settled" if body.startswith("settled")
                    else "other")
        out.append({"thread_id": r["thread_id"], "ts": ev["ts"] if ev else r["ts"], "decision": decision,
                    "reason": body, "title": t["title"] if t else "", "state": t["state"] if t else "",
                    "waiting_for": (t["gate_cmd"] or "")[len(GATE_CMD_PREFIX):] if t else ""})
    return out


def _gate_release(con, thread_id, row, now, why):
    """Light a gate-held (or gate-settled) thread with the question it was holding."""
    held = json.loads(row["gate_hold_ask"] or "{}") or {}
    kind = held.get("kind") or "text"
    prompt = held.get("prompt") or "This was held by the gate. Say what you want done."
    add_event(con, thread_id, now, "helm-gate", "", "state-change", why)
    add_event(con, thread_id, now + 0.001, "helm-gate", "", "bot-ask",
              _ask_event_body(kind, prompt, held.get("options") or [], held.get("context") or ""))
    con.execute(
        "UPDATE threads SET state='needs-you', ask_kind=?, ask_prompt=?, ask_options=?, ask_context=?,"
        " blocked_on='', recheck_at=0, gate_cmd='', parked_since=0, gate_hold_ask='', wake_at=0,"
        " answer_applied=0, nothing_owed=0, last_change=? WHERE thread_id=?",
        (kind, prompt, json.dumps(held.get("options") or []), held.get("context") or "", now, thread_id))


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _scrub_surrogates(value):
    """The same value with each lone UTF-16 surrogate in its strings replaced by U+FFFD.

    A client that cuts a string in the middle of an emoji sends one (a valid pair is two
    code points joined by json.loads and never matches). SQLite cannot encode it, so left in
    it is a 500 that loses the whole report; the rest of the text is worth keeping.
    """
    if isinstance(value, str):
        return _LONE_SURROGATE.sub("�", value)
    if isinstance(value, list):
        return [_scrub_surrogates(v) for v in value]
    if isinstance(value, dict):
        return {_scrub_surrogates(k): _scrub_surrogates(v) for k, v in value.items()}
    return value


def _require_reply_consumed(con, thread_id, body):
    """H9: `expect_reply_ts` is the newest owner-reply/instruction the poster has read. When a
    newer one exists, the post would land over words nobody has read yet: 409, nothing changes.
    Absent (or null) keeps the old behaviour: no check."""
    if not isinstance(body, dict) or body.get("expect_reply_ts") is None:
        return
    exp = body.get("expect_reply_ts")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp != exp:
        raise StoreError(400, "'expect_reply_ts' is %r; it must be a number: the ts of the newest "
                              "owner-reply or instruction this result answers (0 when none)" % (exp,))
    newest = con.execute(
        "SELECT MAX(ts) FROM events WHERE thread_id=? AND kind IN ('owner-reply', 'instruction')",
        (thread_id,)).fetchone()[0] or 0
    if float(newest) > float(exp):
        raise _designed(409, "unconsumed reply", {"reply_ts": float(newest)})


def post_result(con, thread_id, body, dry_run=False):
    """A report back from whatever acted on an answer.

    Every rule below exists because its absence shipped a card that lied.

    dry_run=True runs every check the real door runs and returns what it would answer, without
    writing anything: no event, no row change, no commit, no rule-warning record. A refusal is
    raised exactly as on the real door (the validate route reads it).
    """
    door = "validate" if dry_run else "result"
    row = _require_thread(con, thread_id)
    _require_reply_consumed(con, thread_id, body)
    now = _after_latest(con, thread_id)

    # The draft's spec is covered by its sha, so it stays byte-exact (normalize_draft refuses a lone
    # surrogate in it); normalize_draft cleans the draft's display text itself. Everything else is cleaned here.
    body = {k: (v if k == "draft" else _scrub_surrogates(v)) for k, v in body.items()}
    responder = body.get("responder")
    if responder is not None and not isinstance(responder, str):
        raise StoreError(400, "'responder' is %s, expected a string" % type(responder).__name__)

    outcome = str(body.get("outcome") or "").strip()
    if not outcome:
        raise StoreError(400, "'outcome' is required: a short status word")

    state = str(body.get("state") or "needs-you")
    if state == "snoozed":
        stored_state = "waiting-external"
    elif state in ("needs-you", "waiting-external", "done"):
        stored_state = state
    else:
        raise StoreError(400, "state %r not one of ['needs-you','waiting-external',"
                              "'snoozed','done']" % state)


    note = body.get("note")
    if note is None:
        note = body.get("outcome_note") or body.get("report")
    if not isinstance(note, str):
        raise StoreError(400, "'note' is required and must be a string: it is the ONLY thing "
                              "rendered in the thread's history. 'outcome' alone renders as "
                              "one word and says nothing. Write it to the reader: what you "
                              "did, what you found, and what they need in order to decide")
    note = note.strip()
    if len(note) < MIN_RESULT_NOTE:
        raise _designed(400, "'note' is %d chars, minimum %d. Received: %r. If work was "
                              "drafted, the full draft goes in the note verbatim; it has "
                              "nowhere else to go, and a note that merely says a draft exists "
                              "is a failed run" % (len(note), MIN_RESULT_NOTE, note[:120]))
    if len(note) > MAX_NOTE:
        raise _designed(400, "'note' is %d chars, max %d" % (len(note), MAX_NOTE))
    if outcome.startswith("{") and "note" in outcome:
        raise StoreError(400, "'outcome' looks like a stringified dict with the real report "
                              "trapped inside it. Send a short status word in 'outcome' and "
                              "the report in 'note'")

    # The answer gate (contract: P1 §2, LESSON 16). The note is binding, so a
    # question inside it is an instruction like any other part of it. No state
    # excuses skipping it: parking or closing a thread does not answer what was
    # asked, it just stops the card showing that nobody did.
    rl = rules()
    asked = unanswered_questions(con, thread_id) if rl.answer_gate else []
    answers_text, answer_errors = normalize_answers(body.get("answers"))
    if answer_errors:
        raise StoreError(400, "; ".join(answer_errors))
    if asked and len(answers_text) < MIN_ANSWER:
        raise _designed(
            400,
            "the note you are reporting on asks %d question(s) and this result carries no "
            "usable 'answers' field (%d chars, minimum %d). Answer them in 'answers' -- it "
            "is rendered at the TOP of the history entry, above your report, because it is "
            "the part that was actually asked for. Questions asked: %s"
            % (len(asked), len(answers_text), MIN_ANSWER,
               " | ".join(repr(q) for q in asked[:5])),
            {"questions": asked})
    if len(answers_text) > MAX_NOTE:
        raise _designed(400, "'answers' is %d chars, max %d" % (len(answers_text), MAX_NOTE))

    # Optional, and deliberately NOT reset to empty when simply absent, unlike
    # `ask` below: a run that has nothing new to report should not erase a
    # link a previous run or the producer already attached. Passing an
    # explicit empty list DOES clear it; the two are told apart by whether the
    # key was sent at all, not by its value.
    links_provided = "links" in body
    links, link_errors = normalize_links(body.get("links"))
    if link_errors:
        raise StoreError(400, "; ".join(link_errors))

    # A checked draft attached to this report ("Use the draft"). Absent or null = none.
    draft_json = ""
    if body.get("draft") is not None:
        clean_draft, draft_errors = normalize_draft(body.get("draft"))
        if draft_errors:
            raise StoreError(400, "; ".join(draft_errors))
        draft_json = json.dumps(clean_draft, ensure_ascii=False)

    ask = body.get("ask")
    ask_kind, ask_prompt, ask_options, ask_context = "none", "", [], ""
    ask_subject = ask_recap = ""
    rewritten_ask = False
    card_check = None
    if ask is not None:
        if not isinstance(ask, dict):
            raise StoreError(400, "'ask' is " + type(ask).__name__ + ", expected an object")
        ask_kind = str(ask.get("kind") or "none")
        if ask_kind not in VALID_ASK:
            raise StoreError(400, "ask.kind %r not one of %s" % (ask_kind, sorted(VALID_ASK)))
        ask_prompt = str(ask.get("prompt") or "")
        if rl.strict_limits and len(ask_prompt) > MAX_ASK_PROMPT:
            raise _designed(400, "ask.prompt is %d chars, max %d" % (len(ask_prompt), MAX_ASK_PROMPT))
        ask_context = str(ask.get("context") or "")
        if len(ask_context) > MAX_ASK_CONTEXT:
            raise _designed(400, "ask.context is %d chars, max %d: it renders on the card, so it has to "
                                  "be readable there" % (len(ask_context), MAX_ASK_CONTEXT))
        ask_subject, ask_recap, meta_errors = ask_meta_fields(ask)
        if meta_errors:
            raise StoreError(400, "; ".join(meta_errors))
        ask_options, opt_errors = normalize_options(ask.get("options"))
        if opt_errors:
            raise StoreError(400, "; ".join(opt_errors))
        if ask_kind == "choice":
            if rl.two_option_min and len(ask_options) < 2:
                raise _designed(400, "ask.kind is 'choice' requires at least 2 options, got %d" % len(ask_options))
            if not ask_options:
                raise _designed(400, "ask.kind is 'choice' but no usable options survived "
                                      "normalization")
            # Contract: P1 §3, at the bot's door. A run has just been reading a
            # log, a ticket and its own scratch notes, which is precisely the
            # state of mind that writes "go with the first one".
            sc_errors = self_contained_errors(ask_options, ask_prompt, ask_context) if rl.readable_options else []
            if sc_errors:
                raise _designed(400, "; ".join(sc_errors))
        # The bare-ask rules, bot's door (contract: P3b §4). R1 first: the
        # banned phrase is a hard 400 at this door too. R2: a text ask that is
        # a statement is refused. R3: a pure direction-demand is REWRITTEN to
        # the ack-style summary -- the run is at the end of its rope and
        # cannot correct its payload; the store writes the honest card.
        ba_errors = (bare_ask_errors(ask_kind, ask_prompt, ask_options, ask_context)
                     + one_decision_errors(ask_kind, ask_prompt, ask_options, ask_context)
                     if rl.bare_ask_rules else [])
        if ba_errors:
            raise _designed(400, "; ".join(ba_errors))
        if rl.bare_ask_rules and stored_state == "needs-you" and ask_kind == "text":
            prompt_s = ask_prompt.strip()
            if not prompt_s:
                raise _designed(400, "ask.kind 'text' with an empty prompt: either ask "
                                      "the real question or use kind 'ack' (contract: "
                                      "P3b §4 R2)")
            if "?" not in prompt_s:
                raise _designed(
                    400,
                    "the text ask %r has no question mark: a statement is not an ask. "
                    "Use kind 'ack' with that statement as the prompt (reading it is "
                    "the whole job), or ask the real question (contract: P3b §4 R2)"
                    % prompt_s[:80])
            if pure_direction_demand(prompt_s):
                ask_kind = "ack"
                ask_prompt = rewrite_to_ack_summary(outcome, note)
                ask_options, ask_context, rewritten_ask = [], "", True
        # The context floor, bot's door (ledger GW-062). After the R3 rewrite:
        # a direction-demand has already become an ack and is exempt.
        if state == "needs-you":
            thin = thin_context_error(ask_kind, ask_context)
            if thin and _thin_context_refuses(door, thread_id, thin):
                raise _designed(400, thin)
            recap_issues = ask_recap_issues(ask_kind, ask_subject, ask_recap)
            if recap_issues and _recap_refuses(door, thread_id, recap_issues):
                raise _designed(400, "; ".join(recap_issues))
            # The card-shape rules, bot's door: reject mode refuses (and the refusal is counted, and
            # committed, because nothing else is written yet); log mode records further down, once
            # the card is known to be lit (not held, settled, snoozed or stalled by the paths below).
            if rl.card_rules and ask_kind != "none":
                card_check = card_rule_check(
                    ask_kind, ask_prompt, ask_context, ask_options, ask_subject, ask_recap,
                    _card_rules.author_of(ask.get("author"), body.get("author"), body.get("actor"),
                                          row["responder"]))
                if card_check["reject"]:
                    if not dry_run:
                        record_card_rules(con, "result", thread_id, dict(card_check, fired=card_check["reject"]),
                                          now=now)
                        con.commit()
                    raise _designed(400, "; ".join(_card_rules.reject_errors(card_check["reject"])))

    # The bot-close rules (contract: P1 §4, LESSONS 13, 18, 12). Placed after the
    # ask is parsed because one legitimate outcome is to hand the close back to
    # him as a tap, which needs the ack ask that came in this same post.
    answer_applied = _bool_flag(body, "answer_applied")
    nothing_owed = _bool_flag(body, "nothing_owed")
    # Contract: P7 §2 — the terminal no-action verdict. A literal boolean
    # assertion (strings/numbers 400 via _bool_flag, same as the other two),
    # valid ONLY as a settle: a no-action verdict that does not close the
    # thread is ambiguous — either the run judged nothing was owed (done) or it
    # didn't (one of the other states, without the flag).
    no_action = _bool_flag(body, "no_action")
    if no_action and state != "done":
        raise _designed(400, "'no_action' is the terminal settle verdict and is "
                              "only valid with state 'done' (got state %r). If the run "
                              "did act, drop the flag; if it deliberately did not, this "
                              "result settles the thread" % state)
    eff_applied = bool(row["answer_applied"]) if answer_applied is None else answer_applied
    eff_owed = bool(row["nothing_owed"]) if nothing_owed is None else nothing_owed
    close_requested = False
    closed_by = ""
    if state == "done" and not rl.bot_close_rules:
        # Work Helm's rule: a bot may not set 'done' here, except on an ack
        # ask the human has already replied to.
        owner_replied = con.execute(
            "SELECT 1 FROM events WHERE thread_id=? AND kind='owner-reply' LIMIT 1",
            (thread_id,)).fetchone() is not None
        if not (row["ask_kind"] == "ack" and owner_replied):
            raise _designed(400, "a bot may not set 'done' here; only a human closes a thread. "
                                  "Use 'needs-you', 'waiting-external' or 'snoozed'")
        closed_by = "ack-replied"
        ask_kind, ask_prompt, ask_options, ask_context = "none", "", [], ""
    elif state == "done":
        if ask_kind in ("choice", "text"):
            raise _designed(400, "state is 'done' but this result asks a new %r question. A "
                                  "card that is asking something is 'needs-you', not done: "
                                  "closing it throws the question away before he sees it"
                                  % ask_kind)
        closed_by, refusal = bot_close_reason(con, row, eff_applied, eff_owed,
                                              no_action=no_action)
        if closed_by == "nothing-owed" and ask_kind == "ack":
            # H7 never overrides a run that put the close in his hands: its ack in this post stands.
            closed_by, refusal = None, ""
        if closed_by is None:
            if ask_kind == "ack":
                # The ack ask is IN this post, which is the honest version of a
                # bot close on a thread he has never been asked about (the
                # ever-parked case): the run says it is finished, and the card
                # lights up with one tap that ends it. Nothing closes without
                # him, and nothing is left in limbo either.
                state = stored_state = "needs-you"
                close_requested = True
            else:
                raise _designed(400, refusal)
        else:
            # A settled card is never also lit: the mechanical close clears the
            # question it is closing on.
            ask_kind, ask_prompt, ask_options, ask_context = "none", "", [], ""

    # Invariant 4a.
    if stored_state == "needs-you" and state != "snoozed" and ask_kind == "none":
        raise _designed(400, "state is 'needs-you' but no 'ask' was given. A lit thread with "
                              "no question renders as a bare Close button, which forces your "
                              "work to be discarded in order to clear it. Include an ask "
                              "(use kind 'ack' if reading it is genuinely the whole job), or "
                              "use 'waiting-external' or 'snoozed'")

    # The park contract at the bot's door (contract: P1 §1). A snooze is exempt:
    # wake_at plus the in-store wake sweeper IS its machine-checkable exit
    # (LESSON 6), and demanding a second one would be ceremony.
    blocked_on, recheck_at, gate_cmd = "", 0.0, ""
    if state == "waiting-external" and rl.park_contract:
        try:
            blocked_on, recheck_at, gate_cmd = require_park(body, now)
        except StoreError as exc:
            raise _designed(exc.status, exc.detail, exc.extra) from exc
    elif state == "waiting-external":
        # Work: an undated wait is legal; a blocker is kept when one was named.
        blocked_on = str(body.get("blocked_on") or "").strip()[:500]

    wake_at = 0.0
    wake_note = wake_ask = ""
    wake_mode, wake_set_at = "", 0.0
    if state == "snoozed":
        wake_at = _parse_wake_at(body.get("wake_at")) or 0.0
        if wake_at <= 0:
            raise _designed(400, "state 'snoozed' requires 'wake_at' as epoch seconds or "
                                  "ISO-8601. A deferral is a promise, so it needs a date")
        if wake_at <= now:
            raise _designed(400, "'wake_at' is in the past, which would hide this forever")
        if wake_at - now > MAX_SNOOZE_S:
            raise _designed(400, "'wake_at' is more than 400 days out: that is abandonment "
                                  "wearing a deferral's clothes")
        if rl.recheck_on_wake:
            # Work: only the note the run wrote for its own recheck. The report is not it; a
            # snooze that stored the report had the whole report posted again on the wake date.
            wake_note = str(body.get("wake_note") or "").strip()
            wake_mode, wake_set_at = "recheck", now
        else:
            wake_note = str(body.get("wake_note") or note)
        wa = body.get("wake_ask")
        if rl.recheck_on_wake and wa is not None and not isinstance(wa, dict):
            wa = None       # a question is an object; anything else would break the wake and the restore
        if wa is None and ask_kind != "none":
            wa = {"kind": ask_kind, "prompt": ask_prompt, "options": ask_options,
                  "context": ask_context}
        if wa is None and rl.recheck_on_wake and (row["wake_ask"] or "") not in ("", "{}"):
            # A recheck that snoozes again without restating the question keeps the one
            # stashed when it was first snoozed, instead of overwriting it with nothing.
            wake_ask = row["wake_ask"]
        else:
            wake_ask = json.dumps(wa or {})
        # A hidden thread must not also be lit underneath.
        ask_kind, ask_prompt, ask_options, ask_context = "none", "", [], ""

    # Invariant 5: a live deferral survives a bot result (LESSON: the snooze
    # that would not stick). Every other door already refuses to overwrite a
    # decision he made -- put_threads preserves 'done' (Invariant 2) and an
    # answer in flight (Invariant 3) -- but this one wrote wake_at=0
    # unconditionally, so ANY result landing on a snoozed thread un-hid it.
    # A producer that re-asks whenever its card is not lit (which a snooze
    # makes true by definition) therefore brought the card back within its own
    # poll interval, every interval: on 2026-09-17 the owner snoozed
    # a mirror card of a parked task at 21:21:09 and it was lit again
    # at 21:22:01, snoozed it again at 21:22:49 and it was back at 21:23:01.
    #
    # The report itself is never dropped -- the event is written below exactly
    # as it would have been, so the history is whole -- but the thread stays
    # hidden until the date he picked, and the question rides along in
    # wake_ask so what comes back is the NEWEST question, not a stale one.
    #
    # 'done' is deliberately not preserved against: settling a thread needs no
    # wake, and wake_due() skips done rows anyway. An explicit state
    # 'snoozed' falls through too: re-deferring is a caller saying so on
    # purpose, which is the one thing that may move the date.
    deferred_until = float(row["wake_at"] or 0)
    snooze_kept = False
    if deferred_until > now and state in ("needs-you", "waiting-external"):
        stored_state = row["state"]
        wake_at = deferred_until
        wake_mode, wake_set_at = (row["wake_mode"] or ""), float(row["wake_set_at"] or 0)
        if rl.recheck_on_wake and wake_mode == "recheck":
            # A run's snooze keeps the run's own dated note through a later report; a note in
            # this body replaces it.
            wake_note = str(body.get("wake_note") or row["wake_note"] or "").strip()
        else:
            wake_note = note
        if ask_kind != "none":
            wake_ask = json.dumps({"kind": ask_kind, "prompt": ask_prompt,
                                   "options": ask_options, "context": ask_context})
            # A new question means something is owed again, exactly as it does
            # on the lit path below; the deferral changes when he is asked, not
            # whether the answer is still outstanding.
            eff_applied = eff_owed = False
        else:
            wake_ask = row["wake_ask"] or ""
        # Hidden and lit are mutually exclusive, and a deferred thread wears
        # the park claim it already had rather than one this result invented.
        ask_kind, ask_prompt, ask_options, ask_context = "none", "", [], ""
        blocked_on = row["blocked_on"] or ""
        recheck_at = float(row["recheck_at"] or 0)
        gate_cmd = row["gate_cmd"] or ""
        snooze_kept = True

    gate = None
    gate_hold_ask = ""
    if rl.needs_owner_gate and not snooze_kept and stored_state == "needs-you" and ask_kind != "none":
        g_decision, g_rule, g_version = gate_decision(
            con, thread_id, row["title"], ask_kind, ask_prompt, ask_options, ask_context, note)
        if g_decision:
            from helm import gate_rules as _gr
            gate = {"decision": g_decision, "rule": g_rule, "reason": _gr.REASONS.get(g_rule, g_rule)}
            gate_hold_ask = json.dumps({"kind": ask_kind, "prompt": ask_prompt,
                                        "options": ask_options, "context": ask_context})
            if g_decision == "hold":
                gate["waiting_for"] = g_version
                stored_state = state = "waiting-external"
                blocked_on = "a testable build %s on the testing channel (held by gate)" % g_version
                recheck_at = now + GATE_RECHECK_S
                gate_cmd = GATE_CMD_PREFIX + g_version
            else:
                stored_state = state = "done"
            close_requested = False

    stall_instruction = ""
    stall_loop_note = ""
    if rl.stall_routing and not snooze_kept and stored_state == "needs-you" and gate is None:
        from helm import gate_rules as _gr
        if _gr.enabled("stall") and _gr.is_stall(outcome, ask_kind, ask_prompt, note):
            cutoff = now - 7 * 86400
            prev_stalls = con.execute(
                "SELECT body, ts FROM events WHERE thread_id=? AND source='helm-stall' AND kind='instruction' AND ts >= ? ORDER BY ts ASC",
                (thread_id, cutoff)).fetchall()
            if prev_stalls:
                first_stall_info = prev_stalls[0]["body"][:200].replace("\n", " ")
                stall_loop_note = f"an agent already tried once: {first_stall_info}\n\n{note}"
                note = stall_loop_note
            else:
                stored_state = state = "working"
                ask_kind, ask_prompt, ask_options, ask_context = "none", "", [], ""
                snippet = note[:400]
                stall_instruction = f"Stalled run ({outcome}): {snippet}\n\nFinish the work or name the blocker."

    # A retired thread may still be settled (a task that finished after the card was retired reports
    # its outcome), but no result may leave it in a state a reader would show: the row is hidden
    # (tombstoned), so a lit or parked card written here is invisible. Checked on the FINAL state, after
    # the close, snooze, gate and stall paths have all had their say, and before anything is written.
    if row["tombstoned"] and stored_state != "done":
        raise _designed(409, "thread %s was retired (tombstoned): a result may settle it (state "
                              "'done' with no ask) but cannot light, park or hand it back, because a "
                              "retired thread is not shown and the card would be invisible. Use a new "
                              "thread_id for a new question" % thread_id)

    def _answer():
        """What the door answers; read at call time, so it is the same dict whether the write happened."""
        out = {"accepted": True, "thread_id": thread_id, "state": stored_state,
               "note_len": len(note), "hidden_until": wake_at or None,
               "answered": bool(answers_text)}
        if snooze_kept:
            # Said out loud: the result landed and is in the history, but it did
            # NOT light the card. A caller that keeps re-asking because the card
            # is not lit can read this and stop.
            out["snooze_kept"] = True
            out["state_requested"] = state
        if rewritten_ask:
            # Contract: P3b §4 R3: the demand-for-direction the run sent never
            # became a card; this ack-style summary did. Said out loud so no
            # caller mistakes the stored ask for the one it sent.
            out["rewritten_ask"] = True
        if gate:
            out["gate"] = gate
        if closed_by:
            out["closed_by"] = closed_by
        if close_requested:
            # Never reported as 'done': the caller asked to close and did not.
            out["close_requested"] = True
            out["closes_on"] = "his ack tap"
        if recheck_at:
            # Echoed back so the caller can see the exit it just committed to,
            # including the one it got by default rather than by asking.
            out["blocked_on"] = blocked_on
            out["recheck_at"] = recheck_at
            out["rechecks"] = dt.datetime.fromtimestamp(recheck_at, _LOCAL_TZ).isoformat()
            if gate_cmd:
                out["gate_cmd"] = gate_cmd
        return out

    if dry_run:
        return _answer()

    short = outcome.replace("\n", " ")[:40]
    event_body = ("[" + short + "] " + note) if short and short not in note[:60] else note
    if answers_text:
        # Rendered FIRST, above the report and above the outcome prefix. History
        # is event bodies, and the answer to what he asked is what he opens the
        # card to read; below a paragraph of run narration it may as well be
        # missing (LESSONS 16, 11).
        event_body = ("ANSWERS TO YOUR QUESTION(S):\n" + answers_text + "\n\n" + event_body)
    # A strict per-caller principal (HELM_BEARER_STRICT_PRINCIPALS, H5) is recorded as itself on
    # its results too, whatever responder it names (the row keeps the responder).
    caller = _tokens.PRINCIPAL.get("") or ""
    result_source_id = caller if caller and caller in strict_principals() else str(body.get("responder") or "")
    add_event(con, thread_id, now, "bot", result_source_id,
              "bot-result", event_body, draft=draft_json)
    if ask_kind != "none":
        # Contract: P1 §4. ask_prompt is a mutable column -- the next result
        # overwrites it -- so without this event "did he reply after the ask"
        # is unanswerable one generation back, and a stale reply could close a
        # question it never saw. Additive: no reader switches on an exhaustive
        # set of kinds.
        # Same timestamp as the result that carried it, deliberately: a reply is
        # "newer than the ask" only if it arrived after the result went out, and
        # a synthetic offset here would make a fast tap look older than the
        # question it answered.
        add_event(con, thread_id, now, "bot", str(body.get("responder") or ""),
                  "bot-ask", _ask_event_body(ask_kind, ask_prompt, ask_options, ask_context))
    if close_requested:
        add_event(con, thread_id, now + 0.002, "bot", str(body.get("responder") or ""),
                  "state-change",
                  "the run reports this is finished and asked to close it. A bot may not "
                  "close a thread, so it is here with one tap on it: acknowledging ends it")
    if gate:
        if gate["decision"] == "hold":
            gate_body = ("held by gate (%s): %s. It comes back by itself when %s is on the testing "
                         "channel, or in 7 days; reopen it to see it now."
                         % (gate["rule"], gate["reason"], gate["waiting_for"]))
        else:
            gate_body = ("settled by gate (%s): %s. Not shown to the owner; reopen it to bring the "
                         "question back." % (gate["rule"], gate["reason"]))
        add_event(con, thread_id, now + 0.003, "helm-gate", "", "state-change", gate_body)
        ask_kind, ask_prompt, ask_options, ask_context = "none", "", [], ""

    # A landed result means the run worked, so the retry budget resets. It
    # must also advance dispatched_at past every reply that exists as of
    # right now, not just the one this run was originally spawned for.
    # mark_dispatched()'s ok=False branch deliberately never touches
    # dispatched_at (a genuine failure has to stay retryable), so when the
    # poller gives up at the 900s mark and the run finishes anyway in the
    # background, this call is the ONLY thing left that ever will. Skipping
    # it left a late-but-successful report indistinguishable from an
    # unconsumed reply, and pending_dispatch() re-offered it on the very
    # next tick using whatever ask happened to be live by then, sometimes a
    # stale, already-settled reply to a completely different question (an
    # 'ack' that had already closed the thread, resurrected and redispatched
    # against a newer ask once an older in-flight run's late report
    # overwrote it back to 'needs-you'). Monotonic (MAX), never backward: a
    # concurrent mark_dispatched() call may already be ahead of this one.
    newest_reply = con.execute(
        "SELECT MAX(ts) FROM events WHERE thread_id=? AND kind IN ('owner-reply', 'instruction')",
        (thread_id,)).fetchone()[0] or 0
    # NULL, not '[]', when the key was simply absent (see the note above the
    # normalize_links call): COALESCE only falls back on a real SQL NULL, so
    # an explicit empty list still clears it while an omitted key leaves
    # whatever link was already there untouched.
    links_param = json.dumps(links) if links_provided else None
    # A thread that is no longer parked must not keep wearing a blocker it is
    # not waiting on any more: every non-park state clears the claim, so the
    # park fields on a row are always about the park it is in RIGHT NOW.
    # parked_since is kept while a park continues, so re-parking the same thread
    # every few hours cannot reset its 30-day expiry clock.
    # Flip rules for the two assertions. A NEW question means something is owed
    # again by definition, whatever the result claimed one line earlier; a
    # handed-back close is not an applied answer either.
    if ask_kind in ("choice", "text") or close_requested:
        eff_applied = eff_owed = False

    parked_since = 0.0
    if snooze_kept:
        # Whatever clock the row was already on keeps running: a result landing
        # under a deferral must not start one, nor reset one.
        parked_since = row["parked_since"] or 0
    elif state == "waiting-external":
        parked_since = (row["parked_since"] or 0) if row["state"] == "waiting-external" else 0
        parked_since = parked_since or now
    if card_check and stored_state == "needs-you" and ask_kind != "none":
        record_card_rules(con, "result", thread_id, card_check, now=now)
    if not snooze_kept:
        # A thread that lights normally, or is gated afresh, never carries an old held question.
        con.execute("UPDATE threads SET gate_hold_ask=? WHERE thread_id=?", (gate_hold_ask, thread_id))
    con.execute(
        "UPDATE threads SET state=?, ask_kind=?, ask_prompt=?, ask_options=?, ask_context=?,"
        " wake_at=?, wake_mode=?, wake_set_at=?,"
        " wake_note=?, wake_ask=?, responder=COALESCE(?, responder),"
        " links=COALESCE(?, links), dispatch_attempts=0,"
        " blocked_on=?, recheck_at=?, gate_cmd=?, parked_since=?,"
        " answer_applied=?, nothing_owed=?,"
        " dispatched_at=MAX(COALESCE(dispatched_at, 0), ?), last_change=?, ask_meta=?,"
        " ask_lit_key=CASE WHEN ?='done' THEN '' ELSE ask_lit_key END WHERE thread_id=?",
        (stored_state, ask_kind, ask_prompt, json.dumps(ask_options), ask_context,
         wake_at, wake_mode, wake_set_at, wake_note,
         wake_ask, body.get("responder") or None, links_param,
         blocked_on, recheck_at, gate_cmd, parked_since,
         1 if eff_applied else 0, 1 if eff_owed else 0, newest_reply, now,
         _ask_meta_to_store(row["ask_meta"], _ask_key(ask_kind, ask_prompt, json.dumps(ask_options)),
                            ask_subject, ask_recap), stored_state, thread_id))
    if stall_instruction:
        instruction_ts = now + 0.001
        add_event(con, thread_id, instruction_ts, "helm-stall", "", "instruction", stall_instruction)
        con.execute("UPDATE threads SET last_note=? WHERE thread_id=?", (stall_instruction, thread_id))
    elif stall_loop_note:
        con.execute("UPDATE threads SET last_note=? WHERE thread_id=?", (stall_loop_note, thread_id))
    con.commit()
    return _answer()

# --- The lesson ledger (contract: P3c §3; LESSONS 10, 11, 18, 39, 41) ---------
#
# the owner's corrections used to evaporate: they landed in a reply note, steered
# that one run, and were gone. The ledger is where an adopted correction
# becomes durable state -- one row, one scope, a changelog that reports every
# mutation, and a render read the dispatcher prepends to executor prompts.
#
# The adoption gate is the whole design (contract §2/§3): only HIS tap makes a
# lesson. The classifier (learning/classify.py) proposes; nothing adopts.

LESSON_SOURCE = "learning"
LESSON_MIN_RULE = MIN_ANSWER        # 12: the one-word-floor reading (P1 §2)
LESSON_MAX_RULE = 2000
VALID_LESSON_SCOPE_KEYS = ("global",)
MAX_RENDER_LESSONS = 20             # LESSON 34: the fixed head is the cost
GATE_MARKER = "GATE:"               # contract §5: a promise to change code

LESSON_ADOPT_VALUES = ("adopt", "adopt-with-edit")


def _lesson_scope_ok(scope: str) -> bool:
    if scope == "global":
        return True
    # A lane/skill key is a slug: letters, digits, dash, 1..48 chars.
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", scope or ""))


def lesson_thread_id(rule_candidate: str) -> str:
    """The stable id for a candidate: same words, same thread (contract §2).

    Triaged once: a re-derived candidate re-opens the SAME lesson thread,
    which the four-test's T3 and invariant 3 both already protect."""
    fp = fingerprint(rule_candidate or "")
    return "lesson-" + hashlib.sha256(fp.encode("utf-8")).hexdigest()[:12]


def _lesson_options():
    """The four options the work order names, each self-contained (P1 §3):
    every label says what picking it DOES, so the card survives a screenshot."""
    return [
        {"label": "Adopt it as a standing rule",
         "value": "adopt",
         "effect": "Stores this as an active lesson; it renders into every "
                   "executor prompt for its scope from now on.",
         "terminal": False, "resolved": False},
        {"label": "Adopt it with my wording edits",
         "value": "adopt-with-edit",
         "effect": "Stores this as an active lesson using the exact wording "
                   "you send in a note with this tap; the note is binding.",
         "terminal": False, "resolved": False},
        {"label": "Not the lesson - the real one differs",
         "value": "not-the-lesson",
         "effect": "Closes this candidate and adopts nothing; send the real "
                   "lesson in a note and it gets filed as a new candidate.",
         "terminal": True, "resolved": False},
        {"label": "No lesson - that was a one-off",
         "value": "one-off",
         "effect": "Closes this candidate and adopts nothing; the correction "
                   "applied to that thread only.",
         "terminal": True, "resolved": False},
    ]


def open_lesson_thread(con, source_thread_id, event_ids, quote, rule_candidate,
                       scope="global", signals=(), now=None):
    """Open the lesson thread for a classified correction (contract §2).

    Files through put_threads -- never a raw INSERT -- so every producer-door
    gate fires on the store's own filing exactly as it does for everyone else:
    the P3b four-test (T1 passes on requires:['decision'], which is true here:
    adopting is his judgment call), the P1 §3 self-contained gate, the P3b §4
    bare-ask rules. The evidence rides the card itself: ask.context carries
    the candidate + his words verbatim + the event ids + the matched signals.
    """
    now = time.time() if now is None else now
    rule_candidate = str(rule_candidate or "").strip()
    quote = str(quote or "").strip()
    if len(rule_candidate) < LESSON_MIN_RULE:
        raise StoreError(400, "rule_candidate is %d chars, minimum %d: a "
                              "one-word rule is not a lesson (send his words "
                              "or clip them to the sentence that corrects)"
                              % (len(rule_candidate), LESSON_MIN_RULE))
    if not quote:
        raise StoreError(400, "quote is required: the thread must carry his "
                              "words verbatim, so the adoption decision is "
                              "made against the evidence, not a summary")
    if not isinstance(event_ids, (list, tuple)) or not event_ids:
        raise StoreError(400, "event_ids is required (a non-empty list): the "
                              "lesson's evidence must be traceable to the "
                              "events it came from")
    scope = str(scope or "global").strip()
    if not _lesson_scope_ok(scope):
        raise StoreError(400, "scope %r is not 'global' or a slug key "
                              "(lowercase letters/digits/dashes, 1-48 chars)"
                              % scope)

    evidence = ("RULE CANDIDATE:\n%s\n\nHIS WORDS (verbatim):\n%s\n\n"
                "SOURCE: thread %s, events %s\nSIGNALS: %s"
                % (rule_candidate, quote[:1200], source_thread_id,
                   ", ".join(str(e) for e in event_ids[:8]) or "(none)",
                   ", ".join(signals) or "(none)"))
    if len(evidence) > MAX_ASK_CONTEXT:
        # Clip his words, never the candidate: the decision is about the rule.
        overhead = len(evidence) - MAX_ASK_CONTEXT
        quote = quote[:max(LESSON_MIN_RULE, len(quote) - overhead - 40)]
        evidence = ("RULE CANDIDATE:\n%s\n\nHIS WORDS (verbatim):\n%s\n\n"
                    "SOURCE: thread %s, events %s\nSIGNALS: %s"
                    % (rule_candidate, quote, source_thread_id,
                       ", ".join(str(e) for e in event_ids[:8]) or "(none)",
                       ", ".join(signals) or "(none)"))

    tid = lesson_thread_id(rule_candidate)
    payload = {
        "thread_id": tid,
        "title": "Lesson candidate: " + rule_candidate[:80],
        "summary": ("A correction from you was classified as a lesson "
                    "candidate. Decide once whether it becomes a standing "
                    "rule for executors."),
        "category": "Learning",
        "urgency": "this-week",
        "state": "needs-you",
        "responder": "dispatcher-learning",
        "sources": [{"source": LESSON_SOURCE, "id": tid, "note": ""}],
        "events": [{
            "ts": now, "source": LESSON_SOURCE, "source_id": "",
            "kind": "lesson-candidate",
            "body": json.dumps({
                "candidate": rule_candidate, "quote": quote,
                "source_thread": str(source_thread_id or ""),
                "event_ids": [str(e) for e in event_ids],
                "signals": list(signals), "scope": scope,
            }, ensure_ascii=False),
        }],
        "ask": {"kind": "choice",
                "prompt": ("Should this correction become a standing rule for "
                           "future executor runs?"),
                "options": _lesson_options(),
                "requires": ["decision"]},
        "ask_context": evidence,
    }
    # put_threads normalizes ask context from ask["context"].
    payload["ask"]["context"] = payload.pop("ask_context")

    out = put_threads(con, {"threads": [payload]})
    rej = [r for r in out.get("rejected", []) if r.get("thread_id") == tid]
    if rej:
        raise StoreError(400, "lesson thread refused by the producer's door "
                              "(the same gates every producer faces): "
                              + "; ".join(rej[0].get("errors") or []))
    return dict(out, lesson_thread_id=tid, candidate=rule_candidate)


def _lesson_thread_row(con, thread_id):
    row = con.execute("SELECT * FROM threads WHERE thread_id=?",
                      (thread_id,)).fetchone()
    if row is None:
        raise StoreError(404, "no lesson thread %r: open it first with "
                              "open_lesson_thread" % thread_id)
    srcs = _jloads(row["sources"], [])
    # a source may be a bare string (a producer's own id): only dicts carry "source"
    if not any(isinstance(s, dict) and s.get("source") == LESSON_SOURCE
               for s in srcs):
        raise StoreError(400, "thread %r is not a lesson thread (its sources "
                              "carry no %r entry); create_lesson_from_thread "
                              "adopts from lesson threads only"
                              % (thread_id, LESSON_SOURCE))
    return row


def _newest_lesson_candidate_event(con, thread_id):
    row = con.execute(
        "SELECT ts, body FROM events WHERE thread_id=? AND kind='lesson-candidate'"
        " ORDER BY ts DESC LIMIT 1", (thread_id,)).fetchone()
    if row is None:
        raise StoreError(400, "lesson thread %r carries no lesson-candidate "
                              "event to adopt from" % thread_id)
    try:
        d = json.loads(row["body"])
    except ValueError:
        raise StoreError(400, "lesson thread %r's candidate event is not "
                              "readable JSON; it cannot be adopted" % thread_id)
    return float(row["ts"]), d


def _lesson_changelog(con, lesson_id) -> list:
    r = con.execute("SELECT changelog FROM lessons WHERE id=?",
                    (lesson_id,)).fetchone()
    return _jloads(r["changelog"], []) if r else []


def _lesson_append_changelog(con, lesson_id, entry: dict) -> None:
    entries = _lesson_changelog(con, lesson_id)
    entries.append(dict(entry, ts=time.time()))
    con.execute("UPDATE lessons SET changelog=? WHERE id=?",
                (json.dumps(entries, ensure_ascii=False), lesson_id))


def _lesson_decision_tap(con, row):
    """The adoption evidence: his decision reply, this generation, one of the
    two adopt values. Returns (reply_ts, value) or raises the 400 that IS the
    never-auto-adopts rule as code (contract §3)."""
    tid = row["thread_id"]
    gen = _generation_start(con, tid)
    ask_ts = newest_ask_ts(con, tid, gen)
    replies = con.execute(
        "SELECT ts, body FROM events WHERE thread_id=? AND kind='owner-reply'"
        " AND ts > ? ORDER BY ts ASC", (tid, gen)).fetchall()
    taps = [r for r in replies
            if r["ts"] > ask_ts
            and (r["body"] or "").strip().lower() in LESSON_ADOPT_VALUES]
    if not taps:
        raise StoreError(400, "no adoption tap on lesson thread %r: adopting "
                              "requires his tap (adopt or adopt-with-edit "
                              "newer than the ask). The classifier proposes; "
                              "only he decides -- nothing may adopt for him"
                              % tid)
    return float(taps[-1]["ts"]), (taps[-1]["body"] or "").strip().lower()


def _newest_owner_note_after(con, thread_id, ts):
    r = con.execute(
        "SELECT body FROM events WHERE thread_id=? AND kind='owner-note'"
        " AND ts > ? ORDER BY ts DESC LIMIT 1", (thread_id, ts)).fetchone()
    return (r["body"] or "") if r else ""


def _insert_lesson(con, lesson_id, now, source_thread, rule_text, scope,
                   changelog_entry) -> dict:
    con.execute(
        "INSERT INTO lessons (id, created_ts, source_thread, rule_text, scope,"
        " status, superseded_by, gate_status, changelog)"
        " VALUES (?,?,?,?,?,'active','','',?)",
        (lesson_id, now, source_thread, rule_text, scope,
         json.dumps([changelog_entry], ensure_ascii=False)))
    return {"id": lesson_id, "created_ts": now, "source_thread": source_thread,
            "rule_text": rule_text, "scope": scope, "status": "active",
            "superseded_by": "", "gate_status": "", "changelog": [changelog_entry]}


def create_lesson_from_thread(con, thread_id, actor="owner"):
    """The adoption gate (contract §3). HIS tap decides; this only files it.

    Exactly the answered-decision shape (P1 §4): his reply (the tap) is the
    decision, this post reports it applied and nothing owed, and the thread
    settles on that -- the one mechanical close where the closing was already
    his (LESSON 18). No tap -> 400: that refusal is the never-auto-adopts rule.
    """
    if str(actor or "owner").strip().lower() != "owner":
        raise StoreError(400, "actor %r may not adopt a lesson: adoption is "
                              "his tap on the lesson thread, never a "
                              "programmatic act (contract: P3c §3)" % actor)
    row = _lesson_thread_row(con, thread_id)
    if row["state"] == "done" and (row["ask_kind"] or "none") == "none":
        # Idempotent re-post after a settle: return the existing lesson rather
        # than a confusing 400 (a retry is not a second adoption attempt).
        existing = con.execute(
            "SELECT * FROM lessons WHERE source_thread=? ORDER BY created_ts"
            " DESC LIMIT 1", (thread_id,)).fetchone()
        if existing is not None:
            return _lesson_row_out(existing, already=True)
    tap_ts, tap_value = _lesson_decision_tap(con, row)
    cand_ts, cand = _newest_lesson_candidate_event(con, thread_id)
    candidate = str(cand.get("candidate") or "").strip()
    scope = str(cand.get("scope") or "global").strip() or "global"
    if len(candidate) < LESSON_MIN_RULE:
        raise StoreError(400, "the stored candidate is under the %d-char "
                              "floor; re-open the lesson thread with the "
                              "full words" % LESSON_MIN_RULE)

    rule_text = candidate
    edited = ""
    if tap_value == "adopt-with-edit":
        # The binding note (LESSON 15): his edited wording IS the rule.
        edited = _newest_owner_note_after(con, thread_id, tap_ts - 0.002).strip()
        if len(edited) < LESSON_MIN_RULE:
            raise StoreError(400, "adopt-with-edit needs his edited wording "
                                  "in a note sent with or after the tap (%d "
                                  "chars found, minimum %d): without it there "
                                  "is nothing to adopt but the original "
                                  "candidate, which is what 'adopt' is for"
                                  % (len(edited), LESSON_MIN_RULE))
        rule_text = edited
    if len(rule_text) > LESSON_MAX_RULE:
        rule_text = rule_text[:LESSON_MAX_RULE]

    now = time.time()
    tid = row["thread_id"]
    existing = con.execute(
        "SELECT * FROM lessons WHERE source_thread=? ORDER BY created_ts DESC"
        " LIMIT 1", (tid,)).fetchone()
    if existing is not None and existing["status"] == "active":
        if fingerprint(existing["rule_text"]) == fingerprint(rule_text):
            # Same words: the same lesson. Read it back, settle if needed.
            lesson = _lesson_row_out(existing, already=True)
        else:
            # His re-edit after a reopen: the tap is the authority, and one
            # live lesson per thread -- supersede the old row (contract §3).
            lesson = supersede_lesson(con, existing["id"], rule_text,
                                      scope=scope, actor=actor,
                                      _from_create=True)
    else:
        # No row yet, or only a retired one (he retired it, reopened the
        # thread and tapped adopt again): the tap is the authority and a
        # retired row is not a live lesson, so neither "same words" nor
        # supersede (active-only) applies. File a fresh active row and keep
        # the retired one as history.
        lesson_id = "lsn-" + uuid.uuid4().hex[:12]
        entry = {"event": "adopted", "actor": "owner", "tap": tap_value,
                 "tap_ts": tap_ts, "signals": list(cand.get("signals") or []),
                 "source_thread": tid,
                 "quote": str(cand.get("quote") or "")[:1200],
                 "edited": bool(edited)}
        if existing is not None:
            entry["readopts"] = existing["id"]
        if _is_gate_rule(rule_text):
            entry["gate"] = "pending"
        lesson = _insert_lesson(con, lesson_id, now, tid, rule_text, scope, entry)
    if _is_gate_rule(lesson["rule_text"]):
        # Adopting a GATE: lesson is a promise to change code (contract §5).
        row_gate = con.execute(
            "SELECT gate_status FROM lessons WHERE id=?", (lesson["id"],)).fetchone()
        if row_gate and row_gate["gate_status"] == "":
            con.execute("UPDATE lessons SET gate_status='pending' WHERE id=?",
                        (lesson["id"],))
            lesson["gate_status"] = "pending"

    # Settle the thread through post_result: answered-decision (P1 §4) -- his
    # tap is the decision; this post applies it and owes nothing more.
    outcome = ("lesson adopted (his edited wording)" if edited
               else "lesson adopted")
    note = ("%s: %r [scope %s, lesson %s]. The rule now renders into executor "
            "prompts for its scope until retired." % (outcome, rule_text[:200],
                                                      lesson["scope"], lesson["id"]))
    res = post_result(con, tid, {
        "outcome": outcome, "state": "done",
        "note": note,
        "responder": "dispatcher-learning",
        "answer_applied": True, "nothing_owed": True,
        "links": [{"label": "lesson " + lesson["id"],
                   "url": "lesson://" + lesson["id"]}],
    })
    con.commit()
    out = dict(lesson)
    out["settled"] = res.get("closed_by") == "answered-decision"
    out["thread_state"] = res.get("state")
    return out


def _is_gate_rule(rule_text: str) -> bool:
    return str(rule_text or "").strip().lower().startswith(GATE_MARKER.lower())


def _lesson_row_out(r, already=False) -> dict:
    d = {"id": r["id"], "created_ts": r["created_ts"],
         "source_thread": r["source_thread"], "rule_text": r["rule_text"],
         "scope": r["scope"], "status": r["status"],
         "superseded_by": r["superseded_by"] or "",
         "gate_status": r["gate_status"] or "",
         "changelog": _jloads(r["changelog"], [])}
    if already:
        d["already"] = True
    return d


def lessons_list(con, scope=None, status=None, render_for=None):
    """The ledger read (contract §3). render_for is the dispatcher's view:
    active lessons for ('global', key) only, capped at MAX_RENDER_LESSONS."""
    if render_for is not None:
        key = str(render_for or "").strip()
        rows = con.execute(
            "SELECT * FROM lessons WHERE status='active' AND scope IN (?,?)"
            " ORDER BY created_ts, id", ("global", key)).fetchall()
        return [_lesson_row_out(r) for r in rows[:MAX_RENDER_LESSONS]]
    rows = con.execute(
        "SELECT * FROM lessons WHERE (?1 IS NULL OR scope=?1)"
        " AND (?2 IS NULL OR status=?2) ORDER BY created_ts, id",
        (scope, status)).fetchall()
    return [_lesson_row_out(r) for r in rows]


def lesson_get(con, lesson_id):
    r = con.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    if r is None:
        raise StoreError(404, "no lesson %r" % lesson_id)
    return _lesson_row_out(r)


def lessons_for_prompt(con, scope):
    """The render read (contract §4): active only, global or the exact key,
    (created_ts, id) order, capped -- the fixed head stays fixed (LESSON 34)."""
    return lessons_list(con, render_for=scope)


def retire_lesson(con, lesson_id, reason, actor="owner"):
    """active -> retired (contract §3). A reason is required and the change is
    reported in the changelog; retiring a retired lesson is a LOUD 400, not a
    quiet no-op -- a no-op here would make 'retired' unauditable."""
    r = con.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    if r is None:
        raise StoreError(404, "no lesson %r to retire" % lesson_id)
    if r["status"] != "active":
        raise StoreError(400, "lesson %r is already %s (retired %s): retiring "
                              "again would be a no-op wearing an audit trail"
                              % (lesson_id, r["status"],
                                 "by " + (r["superseded_by"] or "him")
                                 if r["superseded_by"] else ""))
    reason = str(reason or "").strip()
    if len(reason) < LESSON_MIN_RULE:
        raise StoreError(400, "a retirement reason is required (min %d chars): "
                              "a retired lesson never renders again, so the "
                              "changelog must say why it stopped being true"
                              % LESSON_MIN_RULE)
    con.execute("UPDATE lessons SET status='retired' WHERE id=?", (lesson_id,))
    _lesson_append_changelog(con, lesson_id,
                             {"event": "retired", "actor": actor,
                              "reason": reason[:500]})
    con.commit()
    return lesson_get(con, lesson_id)


def supersede_lesson(con, lesson_id, rule_text, scope=None, actor="owner",
                     _from_create=False):
    """Replace a lesson ATOMICALLY (contract §3): validate the new rule and
    scope BEFORE touching the old row, then one commit creates the new active
    lesson and flips the old to retired with superseded_by set. A failed
    supersede leaves the old row untouched and nothing new -- probed."""
    old = con.execute("SELECT * FROM lessons WHERE id=?",
                      (lesson_id,)).fetchone()
    if old is None:
        raise StoreError(404, "no lesson %r to supersede" % lesson_id)
    if old["status"] != "active":
        raise StoreError(400, "lesson %r is %s, not active: only an active "
                              "lesson can be superseded" % (lesson_id, old["status"]))
    rule_text = str(rule_text or "").strip()
    if len(rule_text) < LESSON_MIN_RULE:
        raise StoreError(400, "the replacement rule is %d chars, minimum %d: "
                              "superseding with a non-rule would leave the "
                              "fleet rendering nothing where it used to "
                              "render guidance" % (len(rule_text), LESSON_MIN_RULE))
    new_scope = str(scope if scope is not None else old["scope"]).strip()
    if not _lesson_scope_ok(new_scope):
        raise StoreError(400, "scope %r is not 'global' or a slug key" % new_scope)
    now = time.time()
    new_id = "lsn-" + uuid.uuid4().hex[:12]
    entry = {"event": "supersedes", "actor": actor, "supersedes": old["id"],
             "old_rule": old["rule_text"][:200], "source_thread": old["source_thread"]}
    if _is_gate_rule(rule_text):
        entry["gate"] = "pending"
    # One commit for the whole swap: the new row lands and the old flips in
    # the same transaction (sqlite3 holds the implicit txn until commit), so
    # a crash mid-supersede rolls back BOTH halves -- never two live lessons.
    _insert_lesson(con, new_id, now, old["source_thread"], rule_text, new_scope, entry)
    con.execute("UPDATE lessons SET status='retired', superseded_by=? WHERE id=?",
                (new_id, lesson_id))
    _lesson_append_changelog(con, lesson_id,
                             {"event": "superseded-by", "actor": actor,
                              "by": new_id, "new_rule": rule_text[:200]})
    con.commit()
    return lesson_get(con, new_id)


def mark_gate_implemented(con, lesson_id, commit_ref, actor="orchestrator"):
    """Record that a GATE lesson landed as code (contract §5). The ledger
    records the implementation commit; the listing stops showing it."""
    r = con.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    if r is None:
        raise StoreError(404, "no lesson %r" % lesson_id)
    if not _is_gate_rule(r["rule_text"]):
        raise StoreError(400, "lesson %r is not a GATE: lesson (rule does not "
                              "start with %s); there is nothing to implement"
                              % (lesson_id, GATE_MARKER))
    if r["gate_status"] == "implemented":
        raise StoreError(400, "lesson %r's gate is already implemented (%s)"
                              % (lesson_id, "see changelog"))
    con.execute("UPDATE lessons SET gate_status='implemented' WHERE id=?",
                (lesson_id,))
    _lesson_append_changelog(con, lesson_id,
                             {"event": "gate-implemented", "actor": actor,
                              "commit": str(commit_ref or "")[:80]})
    con.commit()
    return lesson_get(con, lesson_id)


def lessons_pending_gates(con):
    """The honest gap, visible (contract §5; LESSON 23): every ACTIVE lesson
    adopted as a GATE that has not landed as code yet. A retired pending gate
    leaves the listing -- a withdrawn lesson is a withdrawn promise."""
    rows = con.execute(
        "SELECT * FROM lessons WHERE status='active' AND gate_status='pending'"
        " ORDER BY created_ts, id").fetchall()
    return [_lesson_row_out(r) for r in rows]


# --- Claims (executor-agnostic dispatch surface, P2) --------------------------

_CLAIMS_DDL = """
CREATE TABLE IF NOT EXISTS claims (
  claim_id    TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL,
  executor_id TEXT NOT NULL,
  runtime     TEXT NOT NULL,
  caps        TEXT NOT NULL DEFAULT '{}',
  claimed_at  REAL NOT NULL,
  deadline    REAL NOT NULL,
  released_at REAL,
  result_at   REAL,
  reported    TEXT
);
CREATE INDEX IF NOT EXISTS claims_thread ON claims(thread_id);
"""


def init_claims(con: sqlite3.Connection) -> None:
    con.executescript(_CLAIMS_DDL)
    con.commit()

# --- Labels & Intake Routing (Contract P6) -----------------------------------

LABEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
RESERVED_LABELS = {"__proto__", "admin", "api", "settings", "null", "undefined"}
VALID_LABEL_RUNTIMES = {"agent", "webhook", "none"}
MIN_PROMPT_TEMPLATE = 20
MAX_PROMPT_TEMPLATE = 10000


def validate_label(data: dict) -> tuple[dict | None, list[str]]:
    errors = []
    if not isinstance(data, dict):
        return None, ["label data must be a JSON object"]
    name = str(data.get("name") or "").strip()
    if not name:
        errors.append("name is required")
    elif not LABEL_NAME_RE.match(name):
        errors.append("name must match ^[a-z0-9][a-z0-9_-]{1,31}$ (lowercase alphanumeric with dashes/underscores, 2-32 chars)")
    elif name in RESERVED_LABELS:
        errors.append(f"name {name!r} is reserved")

    prompt = str(data.get("prompt_template") or "").strip()
    if not prompt:
        errors.append("prompt_template is required (minimum 20 characters)")
    elif len(prompt) < MIN_PROMPT_TEMPLATE:
        errors.append(f"prompt_template is {len(prompt)} chars, minimum {MIN_PROMPT_TEMPLATE}")
    elif len(prompt) > MAX_PROMPT_TEMPLATE:
        errors.append(f"prompt_template is {len(prompt)} chars, maximum {MAX_PROMPT_TEMPLATE}")

    runtime = str(data.get("executor_runtime") or "agent").strip().lower()
    if runtime not in VALID_LABEL_RUNTIMES:
        errors.append(f"executor_runtime {runtime!r} not one of {sorted(VALID_LABEL_RUNTIMES)}")

    webhook = str(data.get("webhook_url") or "").strip()
    if webhook and not (webhook.startswith("http://") or webhook.startswith("https://")):
        errors.append("webhook_url must be empty or a valid http/https URL")

    desc = str(data.get("description") or "").strip()
    convert_human = 1 if data.get("convert_to_needs_human") in (1, True, "1", "true") else 0
    enabled = 0 if data.get("enabled") in (0, False, "0", "false") else 1

    clean = {
        "name": name,
        "description": desc,
        "prompt_template": prompt,
        "webhook_url": webhook,
        "executor_runtime": runtime,
        "convert_to_needs_human": convert_human,
        "enabled": enabled,
    }
    if errors:
        return None, errors
    return clean, []


def list_labels(con: sqlite3.Connection, include_disabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM labels"
    if not include_disabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY name ASC"
    rows = con.execute(sql).fetchall()
    return [{
        "name": r["name"],
        "description": r["description"],
        "prompt_template": r["prompt_template"],
        "webhook_url": r["webhook_url"],
        "executor_runtime": r["executor_runtime"],
        "convert_to_needs_human": bool(r["convert_to_needs_human"]),
        "enabled": bool(r["enabled"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    } for r in rows]


def get_label(con: sqlite3.Connection, name: str) -> dict | None:
    if not name:
        return None
    r = con.execute("SELECT * FROM labels WHERE name=?", (name.strip().lower(),)).fetchone()
    if r is None:
        return None
    return {
        "name": r["name"],
        "description": r["description"],
        "prompt_template": r["prompt_template"],
        "webhook_url": r["webhook_url"],
        "executor_runtime": r["executor_runtime"],
        "convert_to_needs_human": bool(r["convert_to_needs_human"]),
        "enabled": bool(r["enabled"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def put_label(con: sqlite3.Connection, data: dict) -> dict:
    clean, errors = validate_label(data)
    if errors:
        raise StoreError(400, "; ".join(errors))
    now = time.time()
    existing = con.execute("SELECT created_at FROM labels WHERE name=?", (clean["name"],)).fetchone()
    created_at = existing["created_at"] if existing else now
    con.execute(
        "INSERT INTO labels (name, description, prompt_template, webhook_url, executor_runtime, "
        "convert_to_needs_human, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "description=excluded.description, "
        "prompt_template=excluded.prompt_template, "
        "webhook_url=excluded.webhook_url, "
        "executor_runtime=excluded.executor_runtime, "
        "convert_to_needs_human=excluded.convert_to_needs_human, "
        "enabled=excluded.enabled, "
        "updated_at=excluded.updated_at",
        (clean["name"], clean["description"], clean["prompt_template"], clean["webhook_url"],
         clean["executor_runtime"], clean["convert_to_needs_human"], clean["enabled"],
         created_at, now)
    )
    con.commit()
    return get_label(con, clean["name"])


def delete_label(con: sqlite3.Connection, name: str) -> dict:
    name = name.strip().lower()
    lbl = get_label(con, name)
    if lbl is None:
        raise StoreError(404, f"no such label: {name}")
    ref = con.execute(
        "SELECT COUNT(*) FROM threads WHERE label=? AND state != 'done' AND tombstoned=0",
        (name,)
    ).fetchone()[0]
    now = time.time()
    if ref > 0:
        con.execute("UPDATE labels SET enabled=0, updated_at=? WHERE name=?", (now, name))
        con.commit()
        return {"name": name, "deleted": False, "disabled": True, "active_threads": ref}
    con.execute("DELETE FROM labels WHERE name=?", (name,))
    con.commit()
    return {"name": name, "deleted": True, "disabled": False}


def get_settings(con: sqlite3.Connection) -> dict[str, str]:
    rows = con.execute("SELECT key, value FROM settings").fetchall()
    out = {r["key"]: r["value"] for r in rows}
    defaults = {
        "default_intake_label": "triage",
        "llm_conversion_enabled": "1",
        "needs_human_prompt": "Convert raw intake tasks into clear, actionable needs-you choices for the owner with multiple-choice options and no context assumed.",
    }
    for k, v in defaults.items():
        if k not in out:
            out[k] = v
    return out


def put_settings(con: sqlite3.Connection, updates: dict) -> dict[str, str]:
    if not isinstance(updates, dict):
        raise StoreError(400, "settings must be an object")
    now = time.time()
    for k, v in updates.items():
        if not isinstance(k, str) or not k.strip():
            continue
        v_str = str(v)
        con.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (k.strip(), v_str, now)
        )
    con.commit()
    return get_settings(con)


def set_thread_label(con: sqlite3.Connection, thread_id: str, label: str) -> dict:
    row = _require_thread(con, thread_id)
    lbl_str = label.strip().lower()
    if lbl_str:
        lbl = get_label(con, lbl_str)
        if lbl is None:
            raise StoreError(400, f"label {lbl_str!r} does not exist in labels table")
        if not lbl["enabled"]:
            raise StoreError(400, f"label {lbl_str!r} is disabled")
    now = time.time()
    con.execute("UPDATE threads SET label=?, last_change=? WHERE thread_id=?", (lbl_str, now, thread_id))
    add_event(con, thread_id, now, "system", "", "state-change",
              f"label set to {lbl_str!r}" if lbl_str else "label cleared")
    con.commit()
    return {"thread_id": thread_id, "label": lbl_str, "accepted": True}

