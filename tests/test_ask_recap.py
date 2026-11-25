"""A card carries its own cold-read: a short subject and a recap that belong to the QUESTION.

Producers may send ask.subject (the thing, <= 40 chars) and ask.recap (what this is and why it
needs the owner, <= 280 chars). Helm stores them against the question that was asked, so a question
that changes drops them (the recap of the old question would be wrong) and a resync that does not
resend them keeps them. The read model surfaces them as catchup.subject and catchup.recap.

Soft rules (recap mentions a time word, carries a SHA/path/id, or is missing on a needs-you
choice) log "ask-recap would-refuse" and refuse only when HELM_ASK_RECAP=enforce, the same
log-then-enforce rollout as the context floor, so no producer's real ask is dropped on day one.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("HELM_ASK_RECAP", None)

from helm import store  # noqa: E402

PASS = FAIL = 0
RECAP = ("A fix to keep the fish sitter seated through a whole multi-cast hole visit has been on the "
         "testing channel since September 26. Only play in the game can show it holds.")
NOTE = "I checked the build and the channel and here is the long enough report about it. " + "z" * 40


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def ask(prompt="Promote it, or keep it on testing?", **extra):
    a = {"kind": "choice", "prompt": prompt, "requires": ["decision"],
         "context": "The testing build has run for a week and nobody has played it yet.",
         "options": [{"label": "Promote it", "value": "promote", "effect": "production gets the build"},
                     {"label": "Keep it on testing", "value": "keep", "effect": "nothing changes"}]}
    a.update(extra)
    return a


def thread(tid="rc-1", title="Promote the fish sitter build", **over):
    t = {"thread_id": tid, "title": title, "summary": "a build is ready", "category": "Fleet",
         "urgency": "this-week", "state": "needs-you", "ask": ask(subject="LazyFishSitter 0.1.5.0", recap=RECAP)}
    t.update(over)
    return t


def put(con, t):
    return store.put_threads(con, {"threads": [t]})


def cu(con, tid="rc-1"):
    return store.get_thread(con, tid)["catchup"]


def run():
    con = store.db(os.path.join(tempfile.mkdtemp(prefix="helm-recap-"), "t.db"))

    print("\nsubject and recap ride with the ask and read back")
    r = put(con, thread())
    check("accepted", not r.get("rejected"), r)
    c = cu(con)
    check("subject", c.get("subject") == "LazyFishSitter 0.1.5.0", c)
    check("recap", c.get("recap") == RECAP, c)

    print("\nthey belong to the question")
    put(con, thread(ask=ask()))
    c = cu(con)
    check("a resync that does not resend them keeps them", c.get("subject") == "LazyFishSitter 0.1.5.0" and c.get("recap") == RECAP, c)
    put(con, thread(ask=ask(prompt="Is the build good enough to promote now?")))
    c = cu(con)
    check("a different question without them drops the old ones", c.get("subject") == "" and c.get("recap") == "", c)
    put(con, thread(ask=ask(prompt="Is the build good enough to promote now?", subject="Fish sitter", recap="Short new recap.")))
    c = cu(con)
    check("sending them again on the same question replaces them", c.get("subject") == "Fish sitter" and c.get("recap") == "Short new recap.", c)

    print("\nlimits are hard")
    r = put(con, thread(tid="rc-2", title="Another card", ask=ask(subject="s" * 41, recap="ok")))
    check("a 41 char subject is refused", r.get("rejected") and "subject" in str(r["rejected"]), r)
    r = put(con, thread(tid="rc-3", title="A third card", ask=ask(subject="ok", recap="r" * 281)))
    check("a 281 char recap is refused", r.get("rejected") and "recap" in str(r["rejected"]), r)
    r = put(con, thread(tid="rc-4", title="A fourth card", ask=ask(subject="s" * 40, recap="r" * 280)))
    check("exactly at the limits is fine", not r.get("rejected"), r)

    print("\nthe result door takes them too")
    put(con, thread(tid="rc-5", title="A fifth card", ask=ask(subject="Old subject", recap="Old recap.")))
    body = {"outcome": "checked", "state": "needs-you", "note": NOTE,
            "ask": ask(prompt="Now a different question?", subject="Result subject", recap="Result recap.")}
    store.post_result(con, "rc-5", body)
    c = cu(con, "rc-5")
    check("subject and recap from /result", c.get("subject") == "Result subject" and c.get("recap") == "Result recap.", c)

    print("\nsoft rules log and let it through, until enforced")
    r = put(con, thread(tid="rc-6", title="A sixth card", ask=ask(subject="Fish sitter", recap="This was checked last night and a1b2c3d4e5 is the commit.")))
    check("time word and a SHA do not refuse in log mode", not r.get("rejected"), r)
    r = put(con, thread(tid="rc-7", title="A seventh card", ask=ask()))
    check("a missing recap does not refuse in log mode", not r.get("rejected"), r)
    os.environ["HELM_ASK_RECAP"] = "enforce"
    try:
        r = put(con, thread(tid="rc-8", title="An eighth card", ask=ask(subject="Fish sitter", recap="This was checked last night.")))
        check("enforced: a time word is refused", r.get("rejected") and "last night" in str(r["rejected"]).lower(), r)
        r = put(con, thread(tid="rc-9", title="A ninth card", ask=ask(subject="Fish sitter", recap="See commit a1b2c3d4e5f6 for the change.")))
        check("enforced: a commit id is refused", r.get("rejected"), r)
        r = put(con, thread(tid="rc-10", title="A tenth card", ask=ask()))
        check("enforced: a missing recap is refused", r.get("rejected") and "recap" in str(r["rejected"]), r)
        r = put(con, thread(tid="rc-11", title="An eleventh card", ask={"kind": "ack", "prompt": "Read this and close it.", "context": "x" * 50}))
        check("enforced: an ack is exempt (reading it is the whole job)", not r.get("rejected"), r)
        r = put(con, thread(tid="rc-12", title="A twelfth card", ask=ask(subject="Fish sitter", recap=RECAP)))
        check("enforced: a clean recap passes", not r.get("rejected"), r)
    finally:
        os.environ.pop("HELM_ASK_RECAP", None)

    con.close()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
