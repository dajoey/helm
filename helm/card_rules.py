"""Card-shape rules: the lines that make an ask readable on its own, checked at the needs-owner gate.

The gate already refuses over-length fields, bundled decisions and offstage references. These seven
rules are the rest of the card standard that a machine can read: a card that breaks one still
reaches the owner and costs him a read that returns nothing.

    time-word         a time word in the subject, recap, prompt or an option
    machine-text      a commit id, a file path or a task/thread id anywhere he reads
    no-recommended    a choice where no option is marked (Recommended)
    no-subject-recap  a choice or text ask without ask.subject or ask.recap
    bare-verb-label   an option label that is only a verb phrase, naming no thing
    empty-context     a choice or text ask whose ask.context is empty
    bookkeeping-jargon  a word of an agent's goal bookkeeping (frozen success rule, sealed records,
                        post-fix, round N, criterion D1) anywhere he reads

Each rule has a mode, read from HELM_CARD_RULES on every call (no restart to change it):

    HELM_CARD_RULES="time-word=reject,empty-context=off"     anything unnamed is `log`
    HELM_CARD_RULES="*=off"                                  `*` sets the default

    off     the rule is not evaluated
    log     the card goes through unchanged; one row per (door, thread, rule, question) is kept in
            card_rule_warnings (rule id, thread id, principal, author, time -- never card text), and
            the check route answers `warnings: [rule ids]`
    reject  the card is refused with the rule's sentence, which names the rule and its line of the
            standard and never quotes the card

`PINS` ties every rule to the line of the standard it enforces (line numbers of the notebook page
as read on 2026-10-14). The functions here are pure: the store owns the table and the doors.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys

RULE_IDS = ("time-word", "machine-text", "no-recommended", "no-subject-recap",
            "bare-verb-label", "empty-context", "bookkeeping-jargon")
MODES = ("off", "log", "reject")
DEFAULT_MODE = "log"
ENV = "HELM_CARD_RULES"

# The line of the standard each rule enforces.
PINS = {
    "time-word": (
        "Playbooks/Talking To the owner line 283 (an ask never carries a time word); the word list is "
        "Playbooks/Helm Card Freshness rule 2: subject, recap, prompt or options"),
    "machine-text": (
        "Playbooks/Talking To the owner lines 107-109 (a SHA, a file path, a thread id never appears in a "
        "prompt, a context or an option) and 153 (anywhere the owner reads); line 139 keeps option `value` "
        "a machine token, so values are not read"),
    "no-recommended": (
        "Playbooks/Talking To the owner lines 111-113: recommend one, mark it (Recommended) in the label"),
    "no-subject-recap": (
        "Playbooks/Talking To the owner line 283: every ask carries ask.subject and ask.recap; "
        "Playbooks/Helm Card Freshness rule 4 exempts an ack"),
    "bare-verb-label": (
        "Playbooks/Talking To the owner lines 83-93 (options are named things, not verbs; Approve / Deny / "
        "Amend only for a yes-or-no) and 154 (no all-verb options except a true retry-or-abandon)"),
    "empty-context": (
        "Playbooks/Talking To the owner line 152: an empty context is banned on a card (section 3, lines "
        "95-99, is what the context carries)"),
    "bookkeeping-jargon": (
        "Playbooks/Talking To the owner lines 95-99 (what the context carries: plain words about what was "
        "built and what happened, readable by someone who knows none of the producer's internals) and "
        "section 6 (a card he cannot answer cold is a card he reads for nothing)"),
}

# What a producer reads when a rule is in reject mode. Never quotes the card.
FIX = {
    "time-word": "a time word sits in the subject, recap, prompt or an option. Helm shows the card's "
                 "age from its own clock; say what is true, not when",
    "machine-text": "a commit id, a file path or a task or thread id sits in text he reads. Say what it "
                    "is in words; the machine text belongs in the task's progress notes",
    "no-recommended": "no option is marked (Recommended). Put (Recommended) in the label of the option "
                      "you would pick, and say why in half a sentence",
    "no-subject-recap": "ask.subject or ask.recap is missing. Name the thing in the subject (40 chars) "
                        "and say in the recap, in two sentences, what this is and why it needs him",
    "bare-verb-label": "an option label is only a verb ('Go ahead', 'Cancel it'). Name what the option "
                       "IS, so two options differ at a glance; Approve / Deny is for a yes-or-no only",
    "empty-context": "ask.context is empty. Give each option a short paragraph: what is happening, what "
                     "it costs or blocks, and what picking it does",
    "bookkeeping-jargon": "the card uses goal bookkeeping words (frozen rule or wording, success "
                          "criteria, sealed records, post-fix, longitudinal, round N, planning round, goal "
                          "review, criterion ids like D1 or F2), which he has never seen. Name what was "
                          "built, say in plain words what the checklist item says and what actually "
                          "happened, and say what each option changes on his machines ('nothing changes; "
                          "it only decides how the goal is filed' is a valid answer)",
}

# --- time-word ------------------------------------------------------------------------------------
# Freshness rule 2's list, verbatim, with "N days ago" read as any count of any unit.
_TIME_WORD = re.compile(
    r"\b(?:today|tonight|tomorrow|yesterday|this morning|last night|last week|recently|just now"
    r"|(?:\d+|a|an|a few|few|several|one|two|three|four|five|six|seven)\s+"
    r"(?:minutes?|hours?|days?|weeks?|months?)\s+ago)\b", re.I)

# --- machine-text ---------------------------------------------------------------------------------
_URL = re.compile(r"\b(?:https?|ftp)://\S+", re.I)
_MACHINE = (
    # a commit id: 7-40 lowercase hex carrying both a digit and a letter (git prints lowercase)
    re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"),
    # an absolute or home path with at least two segments: /home/x, ~/scripts/x.py, ./tools/run
    re.compile(r"(?:^|(?<=[\s(\[\"'`=:,]))(?:~|\.{1,2})?/(?:[\w.@+-]+/)+[\w.@+-]*"),
    # a Windows path: C:\Scripts\File-IssueCard.ps1
    re.compile(r"\b[A-Za-z]:\\[\w.\\-]+"),
    # a relative path ending in a file name: web/app.js, helm/store.py
    re.compile(r"\b[\w.-]+/[\w./-]*\.(?:py|sh|js|mjs|ts|go|rs|json|ya?ml|toml|md|txt|log|conf|cfg|ini"
               r"|sql|db|ps1|service|css|html|kt|java)\b"),
    # a bare file name with a code or config extension: store.py, home.env, deploy.sh
    re.compile(r"\b[\w-]+\.(?:py|sh|ps1|json|ya?ml|toml|md|conf|cfg|ini|service|sql|db|log|env|kt)\b"),
    # a task or goal id, or a Helm thread id
    re.compile(r"\b(?:tasks|goals)-20\d{6}-[\w-]+"),
    re.compile(r"\bt-ghwatch[\w-]*"),
    re.compile(r"\bt-[a-z][a-z0-9]*(?:-[a-z0-9]+)*-\d{6,}[\w-]*"),
)

# --- bookkeeping-jargon ---------------------------------------------------------------------------
# Goal bookkeeping: the vocabulary of acceptance checklists an agent wrote when it created a
# goal. Each pattern needs the bookkeeping sense, not the bare English word ("the lake is frozen",
# "round up the invoices", "press F5" stay quiet).
_BOOKKEEPING = (
    # a frozen rule, wording, criterion or checklist, in either order
    re.compile(r"\bfrozen\s+(?:\w+\s+){0,2}(?:rules?|criteri\w+|wording|bar|checklist)\b", re.I),
    re.compile(r"\b(?:rules?|criteri\w+|wording|checklist)\s+(?:\w+\s+){0,3}frozen\b", re.I),
    re.compile(r"\bsuccess\s+(?:criteri(?:a|on)|rules?)\b", re.I),
    re.compile(r"\bsealed\s+(?:records?|docs?|documents?)\b", re.I),
    re.compile(r"\b(?:post|pre)[- ]fix\b", re.I),
    re.compile(r"\blongitudinal\b", re.I),
    # round 3, planning round, review round (an agent's goal rounds)
    re.compile(r"\bround\s+\d+\b|\b(?:planning|review)\s+round\b", re.I),
    re.compile(r"\bgoal\s+review(?:er)?\b", re.I),
    re.compile(r"\bnoticed\s+(?:\w+\s+)?sessions?\b", re.I),
    # a criterion id (D1, F12): only beside a bookkeeping noun, so "press F5" and "vitamin D3" stay quiet
    re.compile(r"\b(?:criterion|criteria|check|item|rule|bar)\s+[DF]\d{1,2}\b"),
    re.compile(r"\b[DF]\d{1,2}\s+(?:numbers?|criterion|check|rule|bar|item|passes|pass|fails?|failed)\b"),
)

# --- bare-verb-label ------------------------------------------------------------------------------
_WORDS = re.compile(r"[a-z0-9]+(?:['\u2019-][a-z0-9]+)*")
_RECOMMENDED = re.compile(r"\brecommended\b", re.I)
# Words that only say what to DO.
_VERBS = frozenset("""
go proceed continue start begin run rerun re-run retry try redo repeat cancel abort abandon stop halt
pause hold wait skip ignore defer postpone snooze delay confirm agree allow block keep leave drop close
delete remove discard merge ship deploy release push revert rollback roll undo fix change edit modify
update adjust do use apply finish complete resume restart reset escalate park file ack acknowledge
dismiss review check investigate look handle choose pick let make send publish install upgrade
downgrade enable disable grant give take move mark resolve settle punt rethink replan reconsider decide
promote rotate renew buy pay order sign log login pair reboot rebuild build test verify archive restore
rename schedule assign notify report ping ask tell reply answer mute unmute unblock unpark wake migrate
swap switch flip toggle turn set reopen open save sync refresh reload retire replace rewrite add create
""".split())
# Yes-or-no words: the standard allows Approve / Deny / Amend for a yes-or-no on one named action.
_APPROVAL = frozenset("approve approved deny denied amend reject rejected yes no accept decline".split())
# Words that carry no thing: pronouns, articles, particles, and nouns that name nothing in particular.
_FILLER = frozenset("""
it this that them these those all everything anything something nothing the a an my your our its
plan task job work thing things change changes request fix step run card item option one ahead now
anyway as is again up on off out over back with please for me you to and or just still yet already
in into at by of
else then here there first instead going forward along so way course same later not ok okay sure fine
none
""".split())
# Words that make a label a bare instruction even with no verb: "OK", "Sure".
_TRIGGERS = _VERBS | _APPROVAL | frozenset("later now ok okay sure fine none nothing".split())
# The null option: cancel, close, hold, not now. The standard wants the real alternative to sit BESIDE
# cancel (section 5), so a label made only of these is the expected cancel, not a bare verb.
_NULL = frozenset("""
cancel abort abandon stop halt drop close none nothing skip ignore dismiss leave hold wait pause defer
postpone snooze later not now discard mute keep
""".split())
# "now" alone makes nothing a cancel: "Fix it now" is a bare verb, "Not now" is a cancel through "not".
_NULL_WORD = _NULL - {"now"}
_RETRY = frozenset("retry rerun re-run try redo".split())
_DETERMINERS = frozenset("the this that these those a an my your our its".split())


def _label_words(label):
    return _WORDS.findall(_RECOMMENDED.sub(" ", str(label or "")).lower())


def _content(words):
    """The words that carry meaning: filler dropped, and a verb used as a noun after a determiner
    ("skip this CHECK", "deploy the FIX") dropped too, since it names nothing in particular."""
    out, prev = [], ""
    for w in words:
        if w not in _FILLER and not (prev in _DETERMINERS and w in _VERBS):
            out.append(w)
        prev = w
    return out


def is_bare_verb(label):
    """True when the label is only a verb phrase: every word says what to do, none names a thing."""
    words = _label_words(label)
    if not words or not all(w in _VERBS or w in _APPROVAL or w in _FILLER for w in words):
        return False
    return any(w in _TRIGGERS for w in words)


def _only(label, allowed):
    content = _content(_label_words(label))
    return bool(content) and all(w in allowed for w in content)


_NOT_TIME = frozenset("now yet".split())
_NOT_SKIP = frozenset("for just right at".split())


def _null_word_at(words, i):
    """Whether words[i] makes the label a cancel. "not" does only before a time word, with the small
    fillers of a time phrase skipped ("Not now", "Not yet", "Not for now", "Not just yet"); it negates
    a defer ("Not later", "Do it now, not later"), so a null word after "not" is no cancel either."""
    w = words[i]
    if w == "not":
        j = i + 1
        while j < len(words) and words[j] in _NOT_SKIP:
            j += 1
        return j < len(words) and words[j] in _NOT_TIME
    j = i - 1
    while j >= 0 and words[j] in _NOT_SKIP:
        j -= 1
    return w in _NULL_WORD and not (j >= 0 and words[j] == "not")


def _is_null(label):
    """The cancel beside the real alternative: "Not now", "Later", "None - close this", "Do nothing",
    "Skip this check and close the task". Judged on the raw words too, because most null words are
    also filler and would leave no content to judge."""
    words = _label_words(label)
    if words and all(w in _NULL or w in _FILLER or w == "do" for w in words) and any(
            _null_word_at(words, i) for i in range(len(words))):
        return True
    return _only(label, _NULL)


def _bare_verb_offender(labels):
    """True when some label is a bare verb that is neither a yes-or-no (Approve / Deny / Amend, lines
    92-93), the cancel beside the real alternative (section 5), nor the retry of a retry-or-abandon
    card (line 154: every option is a retry or a cancel)."""
    retry_or_abandon = (any(_only(lb, _RETRY | _FILLER) for lb in labels)
                        and all(_only(lb, _RETRY) or _is_null(lb) for lb in labels))
    for lb in labels:
        if not is_bare_verb(lb) or _only(lb, _APPROVAL) or _is_null(lb):
            continue
        if retry_or_abandon and _only(lb, _RETRY):
            continue
        return True
    return False


# --- the rules ------------------------------------------------------------------------------------
def _option_dicts(options):
    return [o for o in (options or []) if isinstance(o, dict)]


_CLIP = 2000


def evaluate(kind, prompt="", context="", options=None, subject="", recap=""):
    """Every rule this ask breaks, in RULE_IDS order, regardless of mode. [] for no ask."""
    kind = str(kind or "none")
    if kind == "none":
        return []
    # Each field is clipped past its hard limit (prompt 1000, context 1500): a longer one is refused
    # by the validator anyway, and the clip keeps every regex here linear in practice.
    opts = _option_dicts(options)
    labels = [str(o.get("label") or "")[:_CLIP] for o in opts]
    effects = [str(o.get("effect") or "")[:_CLIP] for o in opts]
    subject, recap = str(subject or "").strip()[:_CLIP], str(recap or "").strip()[:_CLIP]
    prompt, context = str(prompt or "")[:_CLIP], str(context or "")[:_CLIP]
    fired = []
    if any(_TIME_WORD.search(t) for t in [subject, recap, prompt] + labels + effects):
        fired.append("time-word")
    read = [_URL.sub(" ", t) for t in [subject, recap, prompt, context] + labels + effects]
    if any(p.search(t) for t in read for p in _MACHINE):
        fired.append("machine-text")
    if kind == "choice" and len(labels) >= 2 and not any(_RECOMMENDED.search(lb) for lb in labels):
        fired.append("no-recommended")
    if kind in ("choice", "text") and (not subject or not recap):
        fired.append("no-subject-recap")
    if kind == "choice" and _bare_verb_offender(labels):
        fired.append("bare-verb-label")
    if kind in ("choice", "text") and not " ".join(context.split()):
        fired.append("empty-context")
    if any(p.search(t) for t in [subject, recap, prompt, context] + labels + effects for p in _BOOKKEEPING):
        fired.append("bookkeeping-jargon")
    return fired


_WARNED: set = set()


def modes():
    """{rule id: mode} from HELM_CARD_RULES. A malformed entry is skipped with one stderr line and
    never changes another rule's mode."""
    raw = os.environ.get(ENV) or ""
    pairs = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
    default, out = DEFAULT_MODE, {}
    for p in pairs:
        name, _, mode = p.partition("=")
        name, mode = name.strip().lower(), mode.strip().lower()
        if mode not in MODES or (name not in RULE_IDS and name not in ("*", "all")):
            if p not in _WARNED:
                _WARNED.add(p)
                print("card-rules config ignored: %r (rules %s, modes %s)" % (p[:60], ",".join(RULE_IDS),
                      "|".join(MODES)), file=sys.stderr, flush=True)
            continue
        if name in ("*", "all"):
            default = mode
        else:
            out[name] = mode
    return {r: out.get(r, default) for r in RULE_IDS}


def split(fired, mode_map=None):
    """(log ids, reject ids) for the rules that fired; off rules drop out."""
    mode_map = mode_map or modes()
    return ([r for r in fired if mode_map.get(r) == "log"],
            [r for r in fired if mode_map.get(r) == "reject"])


def reject_errors(reject_ids):
    """The 400-style sentences for rules in reject mode. Rule id and standard line, never card text."""
    return ["card rule %s: %s (%s)" % (r, FIX[r], PINS[r]) for r in reject_ids]


def question_key(kind, prompt, options):
    """A short hash of the question (kind, prompt, option labels). Kept on the warning row so a resync
    of the same question counts once; it is not card text and cannot be read back into it."""
    labels = "\x1e".join(str(o.get("label") or "") for o in _option_dicts(options))
    raw = "%s\x1f%s\x1f%s" % (kind, prompt, labels)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_AUTHOR_OK = re.compile(r"[^A-Za-z0-9._:@-]+")


def author_of(*candidates):
    """The first non-empty producer name among the candidates, as a short token (40 chars)."""
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return _AUTHOR_OK.sub("-", c.strip())[:40]
    return ""
