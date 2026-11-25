"""The deployment profile: the rule set the store applies, and the owner principal.

One codebase, one rule set. Everything a deployment can switch is read here, once, from the
environment, so the store and the API consult one object instead of sprinkling
`os.environ` checks.

    HELM_OWNER      the principal allowed to answer cards   (default owner)
    HELM_MODULES    comma list of enabled modules           (default below)
    HELM_NOTES_MODE store | folder-readonly                 (default folder-readonly)

`rules()` is what the store reads. Each flag is documented where it is consulted.
"""
from __future__ import annotations

import os

DEFAULT_MODULES = ("inbox", "conversations", "notebook", "activity")
DEFAULT_NOTES_MODE = "folder-readonly"

# Every switchable rule. A rule is named for what it DOES when on.
RULES = {
    # --- intake (the producer's door and the bot's door) ---------------------------------
    # The four-test (ask.requires; T3 title-fingerprint dedupe) on a new lit card.
    "four_test": True,
    # A choice ask needs at least two options, each readable on its own (self-contained).
    "two_option_min": True,
    "readable_options": True,
    # The bare-ask ban ("what do you want") and the text-ask '?' rule.
    "bare_ask_rules": True,
    # A waiting-external thread must name a blocker (>= 8 chars) with a recheck date.
    "park_contract": True,
    # A result that leaves a question unanswered is refused.
    "answer_gate": True,
    # The needs-owner gate (helm/gate_rules.py) and stall routing.
    "needs_owner_gate": True,
    "stall_routing": True,
    # The card-shape rules (helm/card_rules.py): each rule off | log | reject from HELM_CARD_RULES.
    "card_rules": True,
    # A bot may close a thread only through the mechanical exceptions (P1 section 4).
    "bot_close_rules": True,
    # A dispatched working thread with an event of any kind newer than RESULT_TIMEOUT_S is
    # alive, not a quiet run, whether or not it has reported.
    "beats_are_life": True,
    # Give pre-contract parks a recheck date when the DB is opened (rewrites rows).
    "adopt_legacy_parks": True,
    # Ask learning/classify.py to classify each reply (reply_kind).
    "learning_classifier": True,
    # The intake labels: the triage / needs-human label seed and the default intake label.
    "intake_labels": True,
    # Field caps (option label 60 / value 40 / effect 200, prompt 1000) and the strict id charset.
    "strict_limits": True,
    "strict_ident": True,
    # --- flags the store consults that this deployment leaves off -------------------------
    # They exist for the sites that ran a looser intake; the store asks for them by name, so
    # the names stay and the value is the one this rule set runs with.
    "by_default_session": False,
    "infer_terminal": False,
    "unsent_draft_409": False,
    "stale_wait_reask": False,
    "snoozed_never_dispatched": False,
    "recheck_on_wake": False,
    "put_400_on_any_reject": False,
    "restore_wraps": False,
    "status_work_shape": False,
}

# The principal that may answer a card. Read once, at import: the value names a person in
# stored events, so it must not change under a running process.
OWNER = (os.environ.get("HELM_OWNER") or "owner").strip().lower() or "owner"


def owner() -> str:
    """The owner principal (HELM_OWNER, default `owner`)."""
    return OWNER


def modules() -> list[str]:
    raw = os.environ.get("HELM_MODULES")
    if raw is None or not raw.strip():
        return list(DEFAULT_MODULES)
    return [m.strip() for m in raw.split(",") if m.strip()]


def notes_mode() -> str:
    return (os.environ.get("HELM_NOTES_MODE") or DEFAULT_NOTES_MODE).strip()


class Rules:
    """The rule flags, as attributes. `Rules().four_test` is True."""

    def __init__(self, name: str | None = None, overrides: dict | None = None):
        for key, val in RULES.items():
            setattr(self, key, bool(val))
        for key, val in (overrides or {}).items():
            if key not in RULES:
                raise KeyError("unknown rule %r" % key)
            setattr(self, key, bool(val))

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in RULES}

    def __repr__(self):
        return "Rules()"


_rules: Rules | None = None


def rules() -> Rules:
    """The process-wide rules. Tests call reset()."""
    global _rules
    if _rules is None:
        _rules = Rules()
    return _rules


def reset() -> None:
    """Forget the cached profile."""
    global _rules
    _rules = None


def describe() -> dict:
    """What /api/meta reports: the modules and the notebook mode."""
    return {"modules": modules(), "notes_mode": notes_mode(), "owner": OWNER}
