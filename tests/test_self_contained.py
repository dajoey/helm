"""Self-contained gate: an option must survive being screenshotted alone.

Run: python3 tests/test_self_contained.py   (from the repo root)

Contract: docs/contracts/P1.md §3. LESSON 17 (options must survive being screenshotted
alone), supporting 15 (multiple choice or nothing; the option text is the decision).

The failure: options written by whatever authored them while it was looking at the email,
the ticket, the previous card or its own last message -- "as discussed", "the first one",
"option 2" -- and then rendered on a phone where none of that is present. The person is
asked to decide between buttons that only make sense somewhere he is not.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import store  # noqa: E402

PASS = FAIL = 0
NOTE = "The run did the work and this is the long-enough report about it. " + "z" * 40


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail else ""))


_DIG = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
        "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}


def put(con, tid, ask):
    # P3b §1.5: markers ride on choice/text asks (state=needs-you only); the
    # self-contained rejections under test fire earlier, unchanged. The title
    # spells the thread's digits (T3 dedupe fingerprints titles and strips
    # digits, so a shared default made every thread a duplicate of the first).
    if str((ask or {}).get("kind")) in ("choice", "text"):
        ask = dict(ask, requires=ask.get("requires", ["decision"]))
    return store.put_threads(con, {"threads": [{
        "thread_id": tid, "title": "concern " + "".join(_DIG.get(c, c) for c in tid),
        "summary": "a summary",
        "category": "Fleet", "urgency": "this-week", "state": "needs-you", "ask": ask}]})


def errors_of(res):
    return " | ".join(" ".join(r["errors"]) for r in res.get("rejected", []))


def refuses(con, label_or_opt, prompt="Which way do you want this to go?"):
    """PUT a choice ask carrying this one option; True if the producer's door said no."""
    opt = label_or_opt if isinstance(label_or_opt, dict) else {
        "label": label_or_opt, "value": "v", "effect": "something happens"}
    res = put(con, "sc-probe", {"kind": "choice", "prompt": prompt,
                                "options": [opt, {"label": "Leave it alone for now",
                                                  "value": "leave",
                                                  "effect": "nothing happens"}]})
    return res["rejected_count"] == 1, errors_of(res)


def run():
    tmp = os.path.join(tempfile.mkdtemp(prefix="helm-selfc-"), "t.db")
    con = store.db(tmp)

    print("\nan option that points outside the card is refused")
    for label in ["Do it as discussed",
                  "Send the version we agreed above",
                  "Go with the first one",
                  "Take option 2",
                  "See the email and decide from that",
                  "Same as last time",
                  "Do what the note says",
                  "Use his previous suggestion"]:
        ok, errs = refuses(con, label)
        check("refused: " + label, ok, errs)

    print("\nthe rejection quotes the label, so the author can see which one")
    ok, errs = refuses(con, "Do it as discussed")
    check("the offending label is in the message", "Do it as discussed" in errs, errs)
    check("and it says what to do instead",
          "screenshot" in errs.lower() or "self-contained" in errs.lower(), errs)

    print("\nan effect that points outside the card is refused too")
    ok, errs = refuses(con, {"label": "Send it now", "value": "send",
                             "effect": "Sends the draft we discussed above"})
    check("the effect text is gated the same as the label", ok, errs)

    print("\na label that is only an index cannot be read alone")
    for label in ["1", "#2", "A", "Option 3", "choice b", "First", "The second"]:
        ok, errs = refuses(con, label)
        check("refused: " + repr(label), ok, errs)

    print("\na label that is only a pointer word cannot be read alone")
    for label in ["it", "That", "this one", "the other", "Same"]:
        ok, errs = refuses(con, label)
        check("refused: " + repr(label), ok, errs)

    print("\na label made of letters from a list that is not on the card")
    # The exact shape the brief names: a run reads a numbered list somewhere
    # else, and the phone gets buttons saying "C+D" with no C and no D on it.
    for label in ["C+D", "A and B", "Option D", "options C & D", "1+3", "B/C"]:
        ok, errs = refuses(con, label)
        check("refused: " + repr(label), ok, errs)
    ok, errs = refuses(con, "C+D")
    check("the rejection names the letters that are undefined",
          "'C'" in errs and "'D'" in errs, errs)
    check("and tells the producer the two ways to fix it",
          "ask.context" in errs and "does" in errs.lower(), errs)

    print("\nthe same letters are fine when the card carries the list itself")
    res = put(con, "sc-ctx", {
        "kind": "choice", "prompt": "Which of the quotes do we take?",
        "context": ("A) Ingenico, 3 terminals, $4,100 fitted\n"
                    "B) Verifone, 3 terminals, $3,780 fitted\n"
                    "C) Ingenico, 2 terminals now and 1 in March\n"
                    "D) keep the old terminals and re-evaluate in June"),
        "options": [{"label": "C+D", "value": "cd", "effect": "I book the split order"},
                    {"label": "B", "value": "b", "effect": "I order the Verifone three"},
                    {"label": "None of these, ask me again with prices",
                     "value": "none", "effect": "nothing is ordered"}]})
    check("accepted, because A-D are defined on the card", res["rejected_count"] == 0, res)
    got = next(t for t in store.list_threads(con)["threads"] if t["thread_id"] == "sc-ctx")
    check("and the context is stored and rendered with the ask",
          "Verifone" in got["ask"]["context"], got["ask"])

    print("\na context that defines only SOME of the letters does not license the rest")
    res = put(con, "sc-ctx-partial", {
        "kind": "choice", "prompt": "Which quote?",
        "context": "A) Ingenico, 3 terminals\nB) Verifone, 3 terminals",
        "options": [{"label": "C+D", "value": "cd", "effect": "I book the split order"},
                    {"label": "Neither, keep the old ones", "value": "no",
                     "effect": "nothing is ordered"}]})
    check("refused", res["rejected_count"] == 1, res)
    check("and it names C and D specifically",
          "'C'" in errors_of(res) and "'D'" in errors_of(res), errors_of(res))

    print("\na context that merely MENTIONS a letter defines nothing")
    res = put(con, "sc-ctx-mention", {
        "kind": "choice", "prompt": "Which quote?",
        "context": "The vendor sent four quotes and C looked best to me.",
        "options": [{"label": "C", "value": "c", "effect": "I take that quote"},
                    {"label": "Ask the vendor for a fourth quote", "value": "more",
                     "effect": "nothing is ordered yet"}]})
    check("refused", res["rejected_count"] == 1, errors_of(res))

    print("\na context that itself points offstage has only moved the riddle")
    res = put(con, "sc-ctx-external", {
        "kind": "choice", "prompt": "Which quote?",
        "context": "A) the one we discussed above\nB) the cheaper Verifone quote",
        "options": [{"label": "A", "value": "a", "effect": "I take it"},
                    {"label": "B", "value": "b", "effect": "I take the Verifone quote"}]})
    check("refused", res["rejected_count"] == 1, errors_of(res))
    check("and says the context is the thing that has to be on the card",
          "ask.context" in errors_of(res), errors_of(res))

    print("\ncontext licenses a letter named inside a sentence too")
    res = put(con, "sc-ctx-sentence", {
        "kind": "choice", "prompt": "Which quote?",
        "context": "A) Ingenico, 3 terminals\nB) Verifone, 3 terminals",
        "options": [{"label": "Take option B and tell Ingenico no", "value": "b",
                     "effect": "I order the Verifone three"},
                    {"label": "Hold both quotes until March", "value": "hold",
                     "effect": "nothing is ordered"}]})
    check("accepted", res["rejected_count"] == 0, errors_of(res))
    ok, errs = refuses(con, "Take option B and tell Ingenico no")
    check("and the same label is refused with no context on the card", ok, errs)

    print("\ncontext is bounded: it has to be readable on a phone")
    res = put(con, "sc-ctx-huge", {
        "kind": "choice", "prompt": "Which quote?",
        "context": "A) " + "x" * (store.MAX_ASK_CONTEXT + 1),
        "options": [{"label": "A", "value": "a", "effect": "I take it"},
                    {"label": "Do nothing for now", "value": "n", "effect": "nothing"}]})
    check("refused", res["rejected_count"] == 1, errors_of(res))

    print("\nthe bot's door honours the same escape hatch")
    put(con, "sc-ctx-bot", {"kind": "choice", "prompt": "Chase it?", "options": [
        {"label": "Chase the vendor", "value": "chase", "effect": "I chase them"},
        {"label": "Skip it", "value": "skip", "effect": "Do not chase"}]})
    store.reply(con, "sc-ctx-bot", "chase")
    try:
        store.post_result(con, "sc-ctx-bot", {
            "outcome": "asked", "state": "needs-you", "note": NOTE,
            "ask": {"kind": "choice", "prompt": "Which quote?",
                    "context": "A) Ingenico 3 terminals\nB) Verifone 3 terminals",
                    "options": [{"label": "A", "value": "a", "effect": "I order Ingenico"},
                                {"label": "B", "value": "b", "effect": "I order Verifone"}]}})
        posted = 202
    except store.StoreError as e:
        posted = e.detail
    check("a lettered ask from a run lands when it carries its list", posted == 202, posted)
    got = next(t for t in store.list_threads(con)["threads"]
               if t["thread_id"] == "sc-ctx-bot")
    check("and the run's context reaches the card", "Verifone" in got["ask"]["context"], got)
    store.reply(con, "sc-ctx-bot", "a")
    pend = store.pending_dispatch(con)["pending"]
    check("and it reaches the executor with the answer",
          any(p["thread_id"] == "sc-ctx-bot" and "Verifone" in p["ask_context"]
              for p in pend), pend)
    store.close_thread(con, "sc-ctx-bot")
    got = next(t for t in store.list_threads(con, include_done=True)["threads"]
               if t["thread_id"] == "sc-ctx-bot")
    check("a cleared ask takes its context with it: no list under a dead question",
          got["ask"]["context"] == "", got["ask"])

    print("\ntwo buttons that read identically cannot be told apart")
    res = put(con, "sc-dup", {"kind": "choice", "prompt": "Which way?", "options": [
        {"label": "Send it", "value": "a", "effect": "sends the draft as written"},
        {"label": "send it", "value": "b", "effect": "sends the draft after a rewrite"}]})
    check("duplicate labels are refused", res["rejected_count"] == 1, res)
    check("and the duplicate is named", "send it" in errors_of(res).lower(), errors_of(res))

    print("\na choice with no prompt is a row of buttons with no question")
    res = put(con, "sc-noprompt", {"kind": "choice", "prompt": "", "options": [
        {"label": "Send the draft", "value": "send", "effect": "it goes out"},
        {"label": "Hold the draft", "value": "hold", "effect": "nothing goes out"}]})
    check("refused", res["rejected_count"] == 1, res)
    check("and says the prompt is what carries the question",
          "prompt" in errors_of(res), errors_of(res))

    print("\nplain, complete options are accepted, including Yes and No")
    res = put(con, "sc-good", {
        "kind": "choice", "prompt": "The relay cert expires on Friday. Renew it now?",
        "options": [{"label": "Yes", "value": "yes", "effect": "I renew it today"},
                    {"label": "No", "value": "no", "effect": "I leave it and it expires"},
                    {"label": "Renew it and rotate the key too", "value": "rotate",
                     "effect": "I renew the cert and rotate the signing key"}]})
    check("accepted", res["rejected_count"] == 0, res)
    check("Yes/No survive, because the prompt is on the same card", res["created"] == 1, res)

    print("\nwords that merely CONTAIN a pointer are not rejected")
    for label in ["Ship it to production", "Rewrite the first paragraph",
                  "Keep this one open and chase the vendor",
                  "Archive the duplicate card", "Email the site contact"]:
        opt = {"label": label, "value": "v", "effect": "something specific happens"}
        res = put(con, "sc-ok", {"kind": "choice", "prompt": "Which way?",
                                 "options": [opt, {"label": "Leave it alone for now",
                                                   "value": "leave",
                                                   "effect": "nothing happens"}]})
        check("accepted: " + label, res["rejected_count"] == 0, errors_of(res))

    print("\nthe bot's door is gated exactly like the producer's")
    put(con, "sc-bot", {"kind": "choice", "prompt": "Chase it?", "options": [
        {"label": "Chase the vendor", "value": "chase", "effect": "I chase them"},
        {"label": "Leave it alone", "value": "leave", "effect": "Do not chase"}]})
    store.reply(con, "sc-bot", "chase")

    def result(options, prompt="Which way now?"):
        try:
            store.post_result(con, "sc-bot", {
                "outcome": "asked", "state": "needs-you", "note": NOTE,
                "ask": {"kind": "choice", "prompt": prompt, "options": options}})
            return 202
        except store.StoreError as e:
            return e.status

    check("an external reference from a run is refused", result(
        [{"label": "Go with the first one", "value": "a", "effect": "x happens"},
         {"label": "Leave it alone for now", "value": "b", "effect": "nothing happens"}])
          == 400)
    check("an index-only label from a run is refused", result(
        [{"label": "A", "value": "a", "effect": "x happens"},
         {"label": "B", "value": "b", "effect": "y happens"}]) == 400)
    check("a prompt-less choice from a run is refused", result(
        [{"label": "Send the draft", "value": "a", "effect": "it goes out"},
         {"label": "Hold the draft", "value": "b", "effect": "nothing goes out"}],
        prompt="") == 400)
    check("a well-formed ask from a run is accepted", result(
        [{"label": "Send the draft as written", "value": "a", "effect": "it goes out now"},
         {"label": "Hold it until Monday", "value": "b", "effect": "nothing goes out yet"}])
          == 202)

    print("\nonly 'choice' is gated: an ack or a text ask has no buttons to read alone")
    res = put(con, "sc-ack", {"kind": "ack", "prompt": "Read this and close it."})
    check("an ack ask is untouched", res["rejected_count"] == 0, res)
    res = put(con, "sc-text", {"kind": "text", "prompt": "What should happen here?"})
    check("a text ask is untouched", res["rejected_count"] == 0, res)

    print("\nthe check is callable on its own, so a producer can pre-flight its payload")
    errs = store.self_contained_errors(
        [{"label": "As discussed", "value": "a", "effect": ""}], "Which way?")
    check("it names the problem", errs and "As discussed" in errs[0], errs)
    print("\nthe store's OWN authored options pass the gate it enforces")
    expiry = store._expiry_ask("Ingenico support, case 4821", 31)
    check("the 30-day park-expiry ask is self-contained",
          store.self_contained_errors(expiry["options"], expiry["prompt"]) == [],
          store.self_contained_errors(expiry["options"], expiry["prompt"]))
    hand = store.create_thread(con, {"title": "Chase the cert bundle"})
    got = next(t for t in store.list_threads(con)["threads"]
               if t["thread_id"] == hand["thread_id"])
    check("so is the one option a hand-written card is born with",
          store.self_contained_errors(got["ask"]["options"], got["ask"]["prompt"]) == [],
          got["ask"])

    check("clean options produce no errors", store.self_contained_errors(
        [{"label": "Renew the cert today", "value": "a", "effect": "I renew it"},
         {"label": "Let it expire", "value": "b", "effect": "nothing is renewed"}],
        "Renew the relay cert?") == [])

    con.close()
    context_floor()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


CHOICE = [{"label": "Renew the cert today", "value": "renew", "effect": "I renew it now"},
          {"label": "Let it expire", "value": "expire", "effect": "nothing is renewed"}]
GOOD_CONTEXT = ("The relay cert expires Friday. Renewing costs nothing but restarts the "
                "relay for a minute; letting it lapse stops mail to the site.")


def _floor_checks():
    """Ledger GW-062: a needs-you question with no background is refused, at both doors."""
    con = store.db(os.path.join(tempfile.mkdtemp(prefix="helm-ctxmin-"), "t.db"))

    for ctx in ["", "   ", "See above."]:
        res = put(con, "ctx-thin", {"kind": "choice", "prompt": "Renew the relay cert?",
                                    "options": CHOICE, "context": ctx})
        err = errors_of(res)
        check("a choice ask with context %r is refused" % ctx,
              res["rejected_count"] == 1, res)
        check("and the detail names ask.context and what to write",
              "ask.context" in err and "what happened" in err and "how_to" in err, err)
    res = put(con, "ctx-thin-text", {"kind": "text", "prompt": "Which cert did you mean?",
                                     "context": "the cert"})
    check("a text ask with one line of context is refused",
          res["rejected_count"] == 1 and "ask.context" in errors_of(res), res)
    res = put(con, "ctx-ack", {"kind": "ack", "prompt": "Read this and close it."})
    check("an ack with no context is accepted", res["rejected_count"] == 0, res)
    res = put(con, "ctx-good", {"kind": "choice", "prompt": "Renew the relay cert?",
                                "options": CHOICE, "context": GOOD_CONTEXT})
    check("a choice ask that carries its background is accepted",
          res["rejected_count"] == 0, errors_of(res))
    res = store.put_threads(con, {"threads": [{
        "thread_id": "ctx-wait", "title": "waiting on the vendor", "summary": "s",
        "state": "waiting-external", "blocked_on": "the vendor's support desk",
        "recheck_at": time.time() + 86400, "gate_cmd": "true",
        "ask": {"kind": "choice", "prompt": "Renew?", "options": CHOICE}}]})
    check("a thread that is not lit is not held to it",
          res["rejected_count"] == 0, errors_of(res))

    def result(ask):
        try:
            store.post_result(con, "ctx-good", {"outcome": "asked", "state": "needs-you",
                                                "note": NOTE, "ask": ask})
            return 202, ""
        except store.StoreError as e:
            return e.status, e.detail
    store.reply(con, "ctx-good", "renew")
    st, detail = result({"kind": "choice", "prompt": "Renew it now?", "options": CHOICE})
    check("the bot's door refuses a choice result with no context",
          st == 400 and "ask.context" in detail, (st, detail))
    st, _ = result({"kind": "ack", "prompt": "Renewed the cert; nothing else is owed."})
    check("and accepts an ack result with none", st == 202, st)
    con.close()


def context_floor():
    import contextlib
    import io
    from helm import site as _site
    print("\nthe context floor (GW-062): enforce mode")
    saved = {k: os.environ.get(k) for k in ("HELM_ASK_CONTEXT_MIN",)}
    try:
        os.environ["HELM_ASK_CONTEXT_MIN"] = "enforce"
        _site.reset()
        _floor_checks()

        print("\nlog mode (the default until the producers are fixed): accepted, and logged")
        os.environ.pop("HELM_ASK_CONTEXT_MIN", None)
        _site.reset()
        check("the default mode is log", store.ask_context_min_mode() == "log",
              store.ask_context_min_mode())
        con = store.db(os.path.join(tempfile.mkdtemp(prefix="helm-ctxlog-"), "t.db"))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            res = put(con, "ctx-log", {"kind": "choice", "prompt": "Renew the relay cert?",
                                       "options": CHOICE})
        check("a thin ask is still stored", res["rejected_count"] == 0, res)
        check("and one would-refuse line names the thread and the door",
              "ask-context-min would-refuse door=put thread=ctx-log" in buf.getvalue(),
              buf.getvalue())
        con.close()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _site.reset()


if __name__ == "__main__":
    raise SystemExit(run())
