#!/usr/bin/env python3
"""footer_parser: the one reader of a card body's trailing footer block (stdlib only, no third-party imports).

A card body ends in a block of `Label: value` lines (the stamps the filing door writes: Helm thread, Ask thread,
Old ref, Risk, From task, Review, Filed by, Requested from, and the cause labels). Everything that reads one of
those lines (a filer's trailer: cause, review and context lines) goes through
this module, so no spelling, indent, case, encoding or newline difference lets text act as a stamp for one reader
and not for another.

Contract:
- A footer line is `<label>: <value>`. The line is read after Unicode NFKC and zero-width removal (so NBSP, a
  full-width colon, full-width letters and zero-width characters spell the same line); leading whitespace is
  allowed; the label is `[A-Za-z][A-Za-z ]{1,24}`, compared case-folded with runs of spaces collapsed; the colon is
  followed by whitespace and then a non-space value on the SAME line (`Helm thread:` with the id on the next line is
  no footer).
- The footer block is the maximal run of final footer lines, trailing blank lines ignored. A footer-looking line
  anywhere else (mid-body, before a blank line, before prose that follows) is inert.
- The value is returned as the body wrote it (zero-width characters removed, surrounding whitespace stripped); the
  caller applies its own value rule (a thread id's full match, `required`).

Functions: parse_line, block_span, block, last, values, strip_reserved, parse_footers.
"""
from __future__ import annotations

import re
import unicodedata

_LABEL = re.compile(r"[A-Za-z][A-Za-z ]{1,24}")

# The labels the fleet knows, in the spelling the door writes them (parse_footers keys).
CANONICAL = ("Helm thread", "Ask thread", "Old ref", "Risk", "From task", "Review", "Reviewer", "Filed by",
             "Requested from", "Cause", "Cause gen", "Recurrence of", "Cause source", "Root")
_CANON = {" ".join(x.split()).casefold(): x for x in CANONICAL}


def norm_label(label: str) -> str:
    """The comparison form of a label: NFKC, zero-width removed, spaces collapsed, case-folded."""
    text = unicodedata.normalize("NFKC", _drop_cf(str(label or "")))
    return " ".join(text.split()).casefold()


def _drop_cf(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _split(line: str) -> tuple[str, str] | None:
    """(label as written with spaces collapsed, raw value) when `line` is a footer line, else None."""
    clean = _drop_cf(str(line or ""))
    for i, ch in enumerate(clean):
        if unicodedata.normalize("NFKC", ch) != ":":
            continue
        label = unicodedata.normalize("NFKC", clean[:i]).lstrip()
        rest = clean[i + 1:]
        if not _LABEL.fullmatch(label) or not rest[:1].isspace() or not rest.strip():
            return None
        return " ".join(label.split()), rest.strip()
    return None


def parse_line(line: str) -> tuple[str, str] | None:
    """(normalized_label, raw_value) when `line` is a footer line, else None."""
    got = _split(line)
    return (got[0].casefold(), got[1]) if got else None


def block_span(lines: list[str]) -> tuple[int, int] | None:
    """(start, end) line indexes, end inclusive, of the trailing footer block of `lines`; None when there is none."""
    end = len(lines) - 1
    while end >= 0 and not _drop_cf(lines[end]).strip():
        end -= 1
    start = end
    while start >= 0 and parse_line(lines[start]) is not None:
        start -= 1
    start += 1
    return (start, end) if start <= end else None


def _lines(body) -> list[str]:
    return str(body or "").split("\n")


def block(body) -> list[tuple[str, str]]:
    """[(normalized_label, raw_value), ...] of the trailing footer block, in order; [] when the body has none."""
    lines = _lines(body)
    span = block_span(lines)
    if not span:
        return []
    pairs = (parse_line(ln) for ln in lines[span[0]:span[1] + 1])
    return [p for p in pairs if p is not None]


def last(body, label: str) -> str | None:
    """The raw value of the LAST `label` line of the trailing block, else None."""
    want = norm_label(label)
    found = None
    for k, v in block(body):
        if k == want:
            found = v
    return found


def values(body, label: str) -> list[str]:
    """Every raw value of `label` in the trailing block, in order."""
    want = norm_label(label)
    return [v for k, v in block(body) if k == want]


def strip_reserved(body, labels) -> str:
    """`body` with the lines of the trailing block whose label is in `labels` removed; the rest is the original
    text, untouched. Lines outside the block are never removed."""
    text = str(body or "")
    drop = {norm_label(x) for x in labels}
    lines = text.split("\n")
    span = block_span(lines)
    if not span or not drop:
        return text
    keep = []
    for i, ln in enumerate(lines):
        got = parse_line(ln) if span[0] <= i <= span[1] else None
        if not (got and got[0] in drop):
            keep.append(ln)
    return "\n".join(keep)


def parse_footers(body) -> dict:
    """{label: value} of the trailing block (a later line of one label wins). The label is spelled canonically for
    the labels the fleet knows (`Cause gen`), else as the line wrote it; {} when the body has no block."""
    out: dict = {}
    lines = _lines(body)
    span = block_span(lines)
    if not span:
        return out
    for ln in lines[span[0]:span[1] + 1]:
        got = _split(ln)
        if got:
            out[_CANON.get(got[0].casefold()) or got[0]] = got[1]
    return out
