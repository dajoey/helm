"""An unauthenticated request for a path no route serves is a scanner's probe, not an API error.

The work api (api.example.com) is bearer-only and reachable from the internet, so scanners
ask it for `/.aws/credentials`, `/terraform.tfstate`, `/actuator/env`, `/.git/config`,
`/wp`, `/vendor/phpunit/...` and the like. The auth gate answers each 401, correctly, but
telemetry counted it as `GET /<unmatched> 401` in helm_http_requests_total and
helm_client_errors_total and wrote a `client_error` line, so the error watch raised a "new error
kind" on traffic no client of the API sent.

Now: the 401 answer is unchanged, but the request is counted in
helm_unauthenticated_probes_total{method} and leaves no client_error line and no
`/<unmatched>` series. An AUTHENTICATED request for an unrouted path (a real client guessing a
route) is still a counted 404, and an unauthenticated request for a ROUTED path is still a 401.

Run: python3 tests/run_all.py   (or this file alone, from the repo root)
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_unauth_probe_")
TOKEN = "unauth-probe-test-token-8c1d"
os.environ["HELM_NOTES_PROFILE"] = "fixed"
os.environ["HELM_NOTES_MODE"] = "store"
os.environ["HELM_NOTES_ROOT"] = os.path.join(TMP, "notes")
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ["HELM_APP_TOKEN"] = TOKEN
os.environ.pop("HELM_SENTRY_DSN", None)

from starlette.testclient import TestClient  # noqa: E402
import app.service as svc  # noqa: E402

PASS = FAIL = 0
AUTH = {"Authorization": "Bearer " + TOKEN}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:600] if detail != "" else ""))


def call(client, method, path, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        r = client.request(method, path, **kw)
    return r, [ln for ln in buf.getvalue().splitlines() if ln.startswith("helm client_error:")]


PROBES = ["/.aws/credentials", "/terraform.tfstate", "/actuator/env", "/storage/logs/laravel.log",
          "/.config/gcloud/application_default_credentials.json"]

with TestClient(svc.app) as client:
    print("scanner probes: unauthenticated, no route")
    for p in PROBES:
        r, lines = call(client, "GET", p)
        check(f"{p}: still answered 401", r.status_code == 401 and r.json() == {"ok": False, "detail": "unauthorized"},
              (r.status_code, r.text))
        check(f"{p}: no client_error line", lines == [], lines)
    r, lines = call(client, "GET", "/.env", headers={"Authorization": "Bearer wrong-token"})
    check("a wrong bearer on an unrouted path is the same probe", r.status_code == 401 and lines == [],
          (r.status_code, lines))
    r, lines = call(client, "POST", "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php", content=b"<?php")
    check("a POST probe is answered 401 and leaves no line", r.status_code == 401 and lines == [],
          (r.status_code, lines))

    text = client.get("/metrics").text
    series = "\n".join(ln for ln in text.splitlines() if "unmatched" in ln or "probe" in ln)
    check("no /<unmatched> request series", 'path="/<unmatched>"' not in text, series)
    check("GET probes are counted on their own series",
          'helm_unauthenticated_probes_total{method="GET"} 6' in text, series)
    check("POST probes are counted on their own series",
          'helm_unauthenticated_probes_total{method="POST"} 1' in text, series)
    check("no scanner path becomes a label", "aws" not in text and "terraform" not in text and "actuator" not in text)

    print("\nreal client mistakes keep their signal")
    r, lines = call(client, "GET", "/api/triage/threadz", headers=AUTH)
    check("authenticated unrouted path is still a 404 client_error", r.status_code == 404 and len(lines) == 1, (r.status_code, lines))
    r, lines = call(client, "GET", "/api/triage/threads")
    check("unauthenticated ROUTED path is still a 401 client_error", r.status_code == 401 and len(lines) == 1, (r.status_code, lines))
    text = client.get("/metrics").text
    check("authenticated unrouted 404 is still in /<unmatched>",
          'helm_http_requests_total{method="GET",path="/<unmatched>",status="404"} 1' in text)
    check("routed 401 is still counted",
          'helm_http_requests_total{method="GET",path="/api/triage/threads",status="401"} 1' in text)
    check("the probe counter did not move for them", 'helm_unauthenticated_probes_total{method="GET"} 6' in text)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
