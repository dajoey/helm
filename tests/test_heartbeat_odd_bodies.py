"""POST /api/triage/dispatcher-heartbeat: bodies the dispatcher can really send never answer 500.

Run: python3 tests/test_heartbeat_odd_bodies.py

the poller names a run by its run folder. A folder name with a byte that is not UTF-8 reaches the
site as a lone surrogate (JSON "\\ud800"); SQLite refuses to bind it, and the heartbeat answered
500 on every tick while that folder stayed in the batch (a 500 pages the error tracker and the whole
heartbeat, runs and log tail, is lost). The run is kept under a name that shows the byte.
A body nested past the interpreter's limit is the caller's mistake: 400, not 500.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_SITE"] = "work"
os.environ.pop("HELM_MODULES", None)
TMP = tempfile.mkdtemp(prefix="helm-hb-odd-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ["HELM_DISPATCHER_HB_FILE"] = os.path.join(TMP, "hb.json")

import app.service as svc  # noqa: E402

client = TestClient(svc.app, raise_server_exceptions=False)
AUTH = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}
HB = "/api/triage/dispatcher-heartbeat"

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


def post(body):
    return client.post(HB, content=body if isinstance(body, (str, bytes)) else json.dumps(body), headers=AUTH)


def runs():
    """The runs the store kept (the home /api/status shape does not carry them;
    the work shape, which did, was the second deployment's)."""
    con = sqlite3.connect(os.environ["HELM_DB"])
    con.row_factory = sqlite3.Row
    try:
        from helm import store
        return [dict(r) for r in store.recent_runs(con, limit=8)]
    finally:
        con.close()


now = time.time()
LONE = chr(0xD800)  # json.dumps writes it as "\ud800", which json.loads reads back as a lone surrogate


def run(**kw):
    r = {"name": "t-1-1790000001", "ts": now, "thread_id": "t-1", "outcome": "result", "turns": 3}
    r.update(kw)
    return r


for field in ("name", "thread_id", "outcome"):
    r = post({"ts": now, "host": "client", "tick": 1, "runs": [run(**{field: "x" + LONE + "y"})]})
    check("a lone surrogate in a run's %s is accepted" % field, r.status_code == 202, (r.status_code, r.text[:160]))

names = [x["name"] for x in runs()]
check("the run is kept, its name shows the byte", any(n.startswith("x") and "d800" in n for n in names), names)

r = post({"ts": now, "host": "client", "tick": 2, "runs": [run(name="x" + LONE + "y")]})
check("a resend of the same run is accepted", r.status_code == 202, r.text[:160])
kept = runs()
check("and replaces it, never adds", len([x for x in kept if "d800" in x["name"]]) == 1, [x["name"] for x in kept])

# the same odd name beside ordinary runs: one bad name does not cost the batch
r = post({"ts": now, "host": "client", "tick": 3, "runs": [run(name="ok-1"), run(name="bad" + LONE)]})
check("a batch with one odd name is accepted whole", r.status_code == 202, r.text[:160])
kept = runs()
check("ordinary runs in that batch are stored", "ok-1" in [x["name"] for x in kept], [x["name"] for x in kept])

r = post('{"ts": 1, "x": ' + "[" * 100000 + "]" * 100000 + "}")
check("a body nested past the recursion limit is 400", r.status_code == 400, (r.status_code, r.text[:160]))

check("no 5xx recorded for this route", not any(k[2] >= 500 and "dispatcher-heartbeat" in str(k) for k in svc._request_counts),
      [k for k in svc._request_counts if k[2] >= 500])

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
