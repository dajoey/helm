"""A forced notebook rescan sees the folder as it is now, even while another pass runs.

Run: python3 tests/test_notebook_rescan.py

notes.rescan_if_due(force=True) returned None, doing nothing, whenever another pass held the
scan lock: a background or read-triggered pass that began before a file was written. The
caller asked for the folder's current state and got the older pass's. That is the one-in-seven
flake of test_notebook_folder.py's "one unreadable file does not abort the pass" (review-6.md,
follow-up 5). A forced rescan now waits for the running pass and then runs its own; an
unforced one still skips a busy lock (it is called from every read and must stay cheap).
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp(prefix="helm_test_rescan_")
WIKI = os.path.join(TMP, "wiki")
os.makedirs(os.path.join(WIKI, "Docker"))
os.environ.update(HELM_NOTES_PROFILE="wiki", HELM_NOTES_MODE="folder-readonly", HELM_NOTES_ROOT=WIKI,
                  HELM_NOTES_DB=os.path.join(TMP, "notes.db"), HELM_DB=os.path.join(TMP, "triage.db"), HELM_DATA=TMP,
                  HELM_APP_TOKEN="test-token")

from notebook import notes  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def write(rel, text):
    with open(os.path.join(WIKI, rel), "w") as f:
        f.write(text)


write("Docker/first.md", "# first\n")
con = notes.db()
notes.rebuild(con)
check("the first pass indexes the folder", notes.exists(con, "Docker/first.md"))

# Another pass holds the lock (a background or read-triggered pass that began before the write).
held = threading.Event()


def other_pass():
    with notes._scan_lock:
        held.set()
        time.sleep(0.5)


t = threading.Thread(target=other_pass)
t.start()
held.wait(5)
write("Docker/written-during.md", "# written while another pass ran\n")
out = notes.rescan_if_due(force=True)
t.join()
con.close()
con = notes.db()
check("a forced rescan does not give up on a busy lock", out is not None, out)
check("...it waits and indexes the file written before it was called", notes.exists(con, "Docker/written-during.md"))

# An unforced call from a read still skips a busy lock (it must stay cheap).
held.clear()
t = threading.Thread(target=other_pass)
t.start()
held.wait(5)
t0 = time.time()
out = notes.rescan_if_due(con, every_s=0)
check("an unforced rescan skips a busy lock at once", out is None and time.time() - t0 < 0.2, (out, time.time() - t0))
t.join()
con.close()

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
