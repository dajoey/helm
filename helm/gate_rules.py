"""The needs-owner gate's rules: send / hold / drop for a card about to reach the owner.

Pure and deterministic -- no model, no network, no clock. The rules were calibrated
against one half of a set of cards and checked on the held-out half: none of the cards
that needed the owner was held back, and most of the cards that could wait were kept
away. A parity test replays the same cards through both halves.

The rule: test-and-feedback cards need the owner, but only once a real build exists --
hence "hold", which waits for the build on the testing channel.

decide(state) -> (decision, rule) with decision in {"send", "hold", "drop", None};
None = no rule fired, and the card is sent as it always was.
"""
from __future__ import annotations

import os
import re

BUILD_READY = re.compile(
    r"\b(live on|is on|now on|published to|republished to|shipped to)( the)? testing|"
    r"testing build \S+ is (out|live|ready)|\) is out\b|is now on the testing channel|is live on testing", re.I)
BUILD_NOT_READY = re.compile(
    r"never reached testing|republish queued|not yet deliverable|premature|pending in-game|no update or|"
    r"not performed|no testable|waiting on (a|the) (new|next) (build|version)", re.I)
STATUS = re.compile(
    r"work completed|tap to close|acknowledge (and|to) close|"
    r"nothing new to show|still waiting on your decision|finished and verified|close when read|nothing needed from you",
    re.I)
# A card saying the work STOPPED is never settled or held by a rule: settling it would quietly
# drop unfinished work (Helm's round-cap and capped-run hand-backs, LESSON 24). Found by Helm's
# dispatcher suites on 2026-09-25 before go-live; v2 of the rules.
STALL = re.compile(r"round cap|hit a cap|could not run|could not close|stopped at|malformed|degraded|"
                   r"review could not|no usable verdict", re.I)
# Status rules read only acknowledge-type cards: a real choice or question is never dropped for its wording.
NOT_ACK = re.compile(r"^\s*ASKED YOU \((choice|text)\)", re.I)
REASK = re.compile(r"^\s*(ASKED YOU \(\w+\):\s*)?(still the same question|same question)", re.I)
TEST_CYCLE = re.compile(r"\btesting\b|in[- ]game|\b\d+\.\d+\.\d+(\.\d+)?\b|verify|build", re.I)
VERSION = re.compile(r"\b(\d+\.\d+\.\d+(?:\.\d+)?)\b")

REASONS = {
    "reask": "it repeats a question with nothing new",
    "build-not-ready": "it asks for an in-game test but the build is not on the testing channel yet",
    "build-ready": "a build is live on testing for him to try",
    "status-test-cycle": "a status update in a test cycle; it waits until there is a build to test",
    "status": "a status update or FYI that only asks to be read",
    "stall": "a run stalled without completing; rerouted to an agent to finish or unblock",
}

STALL_OUTCOMES = {
    "round-cap-review-unavailable",
    "round-cap-duplicate",
    "partial-capped",
    "reported-by-dispatcher",
}


def is_stall(outcome: str, ask_kind: str, ask_prompt: str = "", note: str = "") -> bool:
    """True if this result is a stalled run that should be routed to an agent instead of the owner."""
    if ask_kind != "ack":
        return False
    if outcome in STALL_OUTCOMES:
        return True
    if outcome == "note-only" and bool(STALL.search(f"{ask_prompt} {note}")):
        return True
    return False



def enabled(rule: str | None = None) -> bool:
    """HELM_GATE=off turns the whole gate off; HELM_GATE_OFF=reask,status turns off single rules."""
    if os.environ.get("HELM_GATE", "on").strip().lower() in ("off", "0", "false", "no"):
        return False
    if rule is None:
        return True
    off = {x.strip() for x in os.environ.get("HELM_GATE_OFF", "").split(",") if x.strip()}
    return rule not in off and rule.split("-")[0] not in off


def decide(state: dict) -> tuple[str | None, str | None]:
    card = f"{state.get('card_title') or ''}\n{state.get('card') or ''}"
    body = state.get("card") or ""
    history = " ".join(h.get("text", "") for h in state.get("thread_so_far") or [])
    if REASK.search(body):
        return "drop", "reask"
    if BUILD_NOT_READY.search(body):
        return "hold", "build-not-ready"
    if BUILD_READY.search(body):
        return "send", "build-ready"
    if STALL.search(body):
        return None, None
    if STATUS.search(body) and not NOT_ACK.search(body):
        if TEST_CYCLE.search(card + " " + history):
            return "hold", "status-test-cycle"
        return "drop", "status"
    return None, None


def build_version(state: dict) -> str | None:
    """The build a hold waits for: the newest version named on the card, else in the thread."""
    for text in (state.get("card") or "", state.get("card_title") or "",
                 " ".join(h.get("text", "") for h in reversed(state.get("thread_so_far") or []))):
        found = VERSION.findall(text)
        if found:
            return max(found, key=lambda v: tuple(int(p) for p in v.split(".")))
    return None
