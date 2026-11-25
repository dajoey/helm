"""ClientDisconnect is expected network noise, not a gateway bug (the client-disconnect noise).

A client that disconnects mid-request (e.g. a dispatcher heartbeat sender timing
out on POST /api/triage/dispatcher-heartbeat) raises starlette ClientDisconnect
out of call_next. telemetry_gate must answer quietly (499, no body nobody will
read): no propagate, no the error tracker/local unhandled-error record, no 5xx metric.

History: the first attempt let it propagate (re-raise). That still reaches
Starlette's ServerErrorMiddleware, which answers 500 and re-raises, and Sentry
captures the propagated exception -- the error tracker kept filing (10
events 2026-09-22 14:28Z-22:26Z, trace shows http.response.status_code 500).
So propagation is the bug; only a quiet in-gateway answer closes it.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp()
os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ.pop("HELM_SENTRY_DSN", None)

import app.service as svc  # noqa: E402
from starlette.requests import ClientDisconnect  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


calls = []
svc._record_unhandled_error = lambda *a, **k: calls.append((a, k))  # noqa: E731


async def boom(request):
    raise ClientDisconnect()


svc.app.routes.append(Route("/__test_disconnect__", boom, methods=["POST"]))
client = TestClient(svc.app, raise_server_exceptions=False)
AUTH = {"Authorization": "Bearer test-token"}

error_keys_before = {k for k in svc._request_counts if k[2] >= 500}

raised = None
resp = None
try:
    resp = client.post("/__test_disconnect__", headers=AUTH)
except Exception as e:  # pragma: no cover
    raised = e

check("ClientDisconnect is answered in-gateway, not propagated", raised is None, repr(raised))
check("quiet answer is not a 500", resp is not None and resp.status_code != 500,
      repr(getattr(resp, "status_code", None)))
check("quiet answer carries the 499 disconnect status", resp is not None and resp.status_code == 499,
      repr(getattr(resp, "status_code", None)))
check("no unhandled-error record reaches the error tracker/local store", calls == [], repr(calls)[:300])
error_keys_after = {k for k in svc._request_counts if k[2] >= 500}
check("no 5xx metric recorded for the disconnect", error_keys_after == error_keys_before,
      repr(error_keys_after - error_keys_before)[:200])

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
