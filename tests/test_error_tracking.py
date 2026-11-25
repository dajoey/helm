"""Error tracking hook (2026-09-21, wiki Docker/error-tracker): _record_unhandled_error must hand the
exception to the error tracker when the SDK is configured, stay silent when it is not, and never let a
failing SDK break the gateway's own error recording."""
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

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


class FakeSdk:
    def __init__(self, boom=False):
        self.captured, self.boom = [], boom

    def capture_exception(self, exc):
        if self.boom:
            raise RuntimeError("sdk down")
        self.captured.append(exc)


check("no DSN in env -> SDK off", svc.sentry_sdk is None, repr(svc.sentry_sdk))

exc = ValueError("gateway boom")
fake = FakeSdk()
svc.sentry_sdk = fake
svc._record_unhandled_error("tr-test-1", "GET", "/api/x", exc)
check("configured SDK receives the exception", fake.captured == [exc], repr(fake.captured))

svc.sentry_sdk = FakeSdk(boom=True)
try:
    svc._record_unhandled_error("tr-test-2", "GET", "/api/x", exc)
    check("a failing SDK never raises into the gateway", True)
except Exception as e:  # pragma: no cover
    check("a failing SDK never raises into the gateway", False, repr(e))

svc.sentry_sdk = None
try:
    svc._record_unhandled_error("tr-test-3", "GET", "/api/x", exc)
    check("SDK off still records locally without error", True)
except Exception as e:  # pragma: no cover
    check("SDK off still records locally without error", False, repr(e))

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
