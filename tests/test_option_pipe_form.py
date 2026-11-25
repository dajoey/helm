"""An option sent as one string in the CLI's "Label | value | effect" form is split, not stored whole.

A run that talked to /result directly (the CLI does the split client-side, the raw API did not) sent
["Keep waiting | wait | keep the 72h wake checks", ...]. normalize_options made the WHOLE string the
label, a slug of it the value, and left the effect empty, so the card showed three buttons that read
like a log line and promised nothing. Same convention as the fleet's filing tools.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HELM_SITE"] = "home"

from helm import store  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def run():
    print("\nthe real defect: three piped strings")
    opts, errs = store.normalize_options([
        "Keep waiting | wait | keep the 72h wake checks",
        "Promote it now | promote | production gets 0.1.5.0",
        "Cancel the ask | cancel | card closes, channels unchanged"])
    check("no errors", errs == [], errs)
    check("label is the first part", [o["label"] for o in opts] == ["Keep waiting", "Promote it now", "Cancel the ask"], opts)
    check("value is the second part", [o["value"] for o in opts] == ["wait", "promote", "cancel"], opts)
    check("effect is the third part", opts[0]["effect"] == "keep the 72h wake checks", opts)

    print("\nshapes around it")
    o, e = store.normalize_options(["Hold it | hold"])
    check("two parts: label and value, no effect", e == [] and o[0]["label"] == "Hold it" and o[0]["value"] == "hold" and o[0]["effect"] == "", o)
    o, e = store.normalize_options(["Send it | send | it goes out | and the log is kept"])
    check("extra parts stay in the effect, none are dropped", o[0]["effect"] == "it goes out | and the log is kept", o)
    o, e = store.normalize_options(["Just a plain label"])
    check("a plain string is unchanged", o[0]["label"] == "Just a plain label" and o[0]["effect"] == "", o)
    o, e = store.normalize_options([{"label": "Keep waiting | wait | keep checking"}])
    check("an object with only a piped label is split too",
          o[0]["label"] == "Keep waiting" and o[0]["value"] == "wait" and o[0]["effect"] == "keep checking", o)
    o, e = store.normalize_options([{"label": "Keep it", "value": "keep", "effect": "a | b"}])
    check("an object that already has its fields is left alone", o[0]["label"] == "Keep it" and o[0]["effect"] == "a | b", o)
    o, e = store.normalize_options([{"label": "Keep waiting | wait | keep checking", "terminal": True, "resolved": True}])
    check("explicit terminal and resolved flags survive the split", o[0]["terminal"] is True and o[0]["resolved"] is True, o)
    o, e = store.normalize_options(["| orphan | x"])
    check("an empty label is still refused", o == [] and e, (o, e))

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
