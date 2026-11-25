"""health(): closing a parked thread must not be subtracted twice.

Run: python3 tests/test_health_parked_done.py   (from the repo root)

close_thread sets state='done' but never clears `parked`, so health's
in_flight (total - done - parked - backlog) counted the closed thread as
both done and parked and went negative. Offline: throwaway DB.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helm import store  # noqa: E402
from test_append_note import parked_thread  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail else ""))


def run():
    con = store.db(os.path.join(tempfile.mkdtemp(prefix="helm-health-"), "t.db"))
    parked_thread(con, "pk-1")
    parked_thread(con, "pk-2")

    print("\ntwo parked threads")
    h = store.health(con)
    check("parked 2", h["parked"] == 2, h)
    check("in_flight 0", h["in_flight"] == 0, h)

    print("\nclose one parked thread")
    store.close_thread(con, "pk-1", "done with it")
    h = store.health(con)
    check("pk-1 is done", h["by_state"].get("done") == 1, h)
    check("in_flight not negative", h["in_flight"] >= 0, h)
    check("in_flight is 0 (one live thread, parked)", h["in_flight"] == 0, h)
    check("parked excludes the closed thread", h["parked"] == 1, h)

    print("\nun-parked live thread still counts as in flight")
    store.set_parked(con, "pk-2", False, source="test")
    h = store.health(con)
    check("in_flight 1", h["in_flight"] == 1, h)
    check("parked 0", h["parked"] == 0, h)

    print("\n%d passed, %d failed" % (PASS, FAIL))


if __name__ == "__main__":
    run()
    raise SystemExit(1 if FAIL else 0)
