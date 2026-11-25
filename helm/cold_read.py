"""The cold-read gate (T5): the card's visible text names the exact step, and the step is his.

Fleet rule: every needs-you card names the exact step or decision in what
the owner reads without expanding anything -- the subject, the recap, the question, and each option's
label and effect -- and never hands him a step an agent can reach (a command, a line typed into
an app, a setting, a file, a login in his vault, a web page).

Two layers, in order:

  pre-checks   Deterministic, no model, always run before any call: a placeholder question that
               names no step ("waiting on something only you can do"), and visible text that
               ends mid-word or mid-sentence (a clip artifact -- a sentence cut at a boundary
               cannot name a step).

  judge        A model call on the visible text with a fixed rubric: two yes/no questions plus
               the named step. Hard timeout; an unreachable, unconfigured or unparseable judge
               FAILS OPEN (the card passes) with one logged line, so a model outage never
               blocks a real card. Verdicts are cached by the visible-text hash, so a producer
               retrying the same question does not pay for the call twice.

The judge never sees ask.context: that is folded behind Details on the card. HELM_COLD_READ=off
disables the whole gate; the judge is inert unless HELM_COLD_READ_JUDGE_URL / _MODEL / _KEY are
set (the deterministic pre-checks still run -- they send nothing anywhere).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request

# Every refusal names the fix in plain words (the producer reads this back from the door).
FIX_UNNAMED = (
    "name the step in the question: say where and what he is to do -- the subject, the question "
    "and the option text he reads without expanding anything must carry it, not only the folded "
    "context")
FIX_AGENT_REACHABLE = (
    "an agent can reach this step: do it, or ask permission for an agent to do it -- never hand "
    "him a command, a line typed into an app, a setting, a file, a login in his vault, or a web "
    "page")
FIX_TRUNCATED = (
    "the visible text ends mid-sentence: a cut sentence names no step -- rewrite it whole, cut "
    "at a sentence end, or drop the clause")

# --- deterministic pre-checks ---------------------------------------------------------------------

# A question that refers to the step without naming it. The three-failure template card asked
# "... is waiting on something only you can do ... Are you doing it, changing the plan, or
# cancelling the task?" and all four needs-you tests passed it.
_PLACEHOLDER = re.compile(
    r"\bsomething\s+(?:only\s+)?(?:you|he|i)\s+(?:can|have\s+to|must)\s+do\b"
    r"|\bsomething\s+(?:only\s+)?(?:you|he)\s+(?:have|has)\s+to\s+do\b"
    r"|\bthe\s+thing\s+it\s+waits\s+for\b"
    r"|\byour\s+call\s+on\s+(?:this|it)\b", re.I)

# A clip artifact: terminal punctuation with a space before it ("verifies it stuck past the ."),
# or the text dangling on a bare lowercase article or possessive with nothing after it
# ("... it ran past the"). Case-sensitive on purpose: a label can name a thing with a
# capital letter ("Action A", "Do A"), and a complete clause can end on a preposition or
# conjunction ("worth waiting for", "ask which"), so those never match; only a dangling
# article cannot end well-formed visible text.
_TRUNC_PUNCT = re.compile(r"\s[.,;:]\s*$")
_TRUNC_DANGLING = re.compile(
    r"\s(?:the|a|an|his|her|its|their|our|your|my)\s*$")


def _option_pairs(options):
    out = []
    for o in options or []:
        if isinstance(o, dict):
            out.append((str(o.get("label") or ""), str(o.get("effect") or "")))
    return out


def _truncated(text):
    text = str(text or "").rstrip()
    return bool(text) and bool(_TRUNC_PUNCT.search(text) or _TRUNC_DANGLING.search(text))


def precheck_errors(subject, prompt, options):
    """Every deterministic reason this card's visible text cannot be read cold."""
    errors = []
    for name, text in (("question", prompt), ("subject", subject)):
        m = _PLACEHOLDER.search(str(text or ""))
        if m:
            errors.append("the %s names no step (%r): %s" % (name, m.group(0), FIX_UNNAMED))
            break
    pairs = _option_pairs(options)
    for field, text in ([("subject", subject), ("question", prompt)]
                        + [("option label", l) for l, _ in pairs]
                        + [("option effect", e) for _, e in pairs]):
        if _truncated(text):
            errors.append("the %s ends mid-sentence: %s" % (field, FIX_TRUNCATED))
            break
    return errors


# --- the model judge ------------------------------------------------------------------------------

JUDGE_FAILURES = 0          # every failure (unconfigured, unreachable, unparseable)
_CACHE: dict = {}           # visible-text hash -> verdict; bounded, in-process
_CACHE_MAX = 1024
_WARNED: set = set()


def _warn_once(reason):
    if reason not in _WARNED:
        _WARNED.add(reason)
        print("cold-read judge %s" % reason, file=sys.stderr, flush=True)


def _judge_cfg():
    return {"url": (os.environ.get("HELM_COLD_READ_JUDGE_URL") or "").strip(),
            "model": (os.environ.get("HELM_COLD_READ_JUDGE_MODEL") or "").strip(),
            "key": (os.environ.get("HELM_COLD_READ_JUDGE_KEY") or "").strip(),
            "timeout_s": float(os.environ.get("HELM_COLD_READ_JUDGE_TIMEOUT_S") or 15.0)}


def visible_payload(subject, prompt, options, recap=""):
    """Exactly what the judge sees: the visible text, never ask.context."""
    return {"subject": str(subject or ""), "recap": str(recap or ""),
            "question": str(prompt or ""),
            "options": [{"label": l, "effect": e} for l, e in _option_pairs(options)]}


_RUBRIC = """You judge a notification card sent to one owner, the owner. The CARD VISIBLE TEXT below \
is all he reads without expanding anything (anything folded behind a Details fold is not shown to \
you either).

subject: %(subject)s
recap: %(recap)s
question: %(question)s
options:
%(options)s

Answer two yes/no questions about THE STEP this card asks of the owner:
1. "named": do the subject, question and options together name the EXACT step or decision -- both \
WHERE and WHAT (which app or screen to act in, what to approve and its cost, what to look at)? A \
placeholder ("something only you can do") or a step named only in text the owner must expand is NOT named.
2. "only_owner_can": what must the owner supply if the owner taps the recommended (or only) option? \
Answer true only when it is the owner's alone: their pick or approval among options whose outcomes \
agents then execute (approving a spend, a deletion or other irreversible change, a risk, \
or another what-or-whether that is theirs), their funds, their account or credential, their \
physical presence (look at it, taste it, listen to it, try it by hand), or a person only \
the owner can contact. Answer false when the act asked of the owner's own hands is one an agent can do \
instead: run a command, type or paste a line into an app or chat, click or toggle in a \
UI, install, pair, sign in, edit a file, fetch from their vault, open a web page. "You \
tell the in-app agent...", "sign in and...", "paste this..." name THE OWNER'S hands doing an \
agent's job -- that is false even though the owner also picks the option.

Reply with JSON only, nothing else:
{"named": true|false, "step": "<the step in at most 20 words, empty string when not named>", \
"only_owner_can": true|false}"""

_SYSTEM = "You judge notification cards for one owner. Reply with JSON only."


def _post_judge(payload):
    """One judge call over HTTP. Raises on any failure; never returns a half-verdict.

    Tests replace this function, so the rubric, the payload shape and the fail-open path are
    exercised without a live model; the replay and the check route run it for real.
    """
    cfg = _judge_cfg()
    if not (cfg["url"] and cfg["model"] and cfg["key"]):
        raise RuntimeError("not configured: HELM_COLD_READ_JUDGE_URL/_MODEL/_KEY")
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": _RUBRIC % payload}],
        "temperature": 0, "max_tokens": 700}).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"], data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % cfg["key"]})
    with urllib.request.urlopen(req, timeout=cfg["timeout_s"]) as r:
        data = json.loads(r.read().decode("utf-8"))
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    m = re.search(r"\{.*\}", content, re.S)
    verdict = json.loads(m.group(0)) if m else None
    if not isinstance(verdict, dict) or not isinstance(verdict.get("named"), bool) \
            or not isinstance(verdict.get("only_owner_can"), bool):
        raise ValueError("judge reply is not the rubric's JSON")
    return verdict


def judge(subject, prompt, options, recap=""):
    """The verdict on the visible text, or None when the judge must fail open."""
    global JUDGE_FAILURES
    payload = visible_payload(subject, prompt, options, recap)
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    if key in _CACHE:
        return _CACHE[key]
    try:
        verdict = _post_judge(payload)
    except Exception as e:                      # any failure fails open, never blocks a card
        JUDGE_FAILURES += 1
        _warn_once("failed open (%s); a card passed unchecked" % str(e)[:120])
        return None
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = verdict
    return verdict


# --- the gate -------------------------------------------------------------------------------------

def cold_read_errors(kind, subject, prompt, options, recap=""):
    """Every reason this ask fails the cold-read gate, at any door ([] when it is clean)."""
    if (os.environ.get("HELM_COLD_READ") or "").strip().lower() == "off":
        return []
    if str(kind or "none") not in ("choice", "text"):
        return []
    errors = precheck_errors(subject, prompt, options)
    if errors:
        return errors
    verdict = judge(subject, prompt, options, recap)
    if verdict is None:
        return []
    if not verdict.get("named"):
        return [FIX_UNNAMED]
    if not verdict.get("only_owner_can"):
        return [FIX_AGENT_REACHABLE]
    return []
