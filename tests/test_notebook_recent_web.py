"""The notebook home's recent-pages read passes through the web role.

Run: python3 tests/test_notebook_recent_web.py

An in-process upstream proves the canonical limit=12 query is preserved, the
server-side bearer is injected, and the write-log route beside it stays closed.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "notebook-recent-web-test-token"
os.environ["HELM_APP_TOKEN"] = TOKEN
os.environ["HELM_API_URL"] = "http://api.test"
os.environ["HELM_SITE"] = "work"
os.environ.pop("HELM_WEB_REQUIRE_AUTH_HEADER", None)

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.web as web  # noqa: E402

PASS = FAIL = 0
SEEN = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail else ""))


async def recent(request):
    SEEN.append({
        "path": request.url.path,
        "query": request.url.query,
        "authorization": request.headers.get("authorization"),
        "actor": request.headers.get("x-helm-actor"),
    })
    return JSONResponse({"notes": [{"rel": "Reference/Alpha.md", "folder": "Reference",
                                    "stem": "Alpha", "title": "Alpha", "mtime": 123.0,
                                    "size": 20, "daily": False}]})


web.UPSTREAM_TRANSPORT = httpx.ASGITransport(app=Starlette(routes=[Route("/api/notes/recent", recent)]))


def run():
    print("\nnotebook recent through the browser's web role")
    check("the exact recent route is allowlisted", bool(web.ALLOW_GET.match("/api/notes/recent")))
    check("a longer lookalike route is not allowlisted", not web.ALLOW_GET.match("/api/notes/recently"))
    client = TestClient(web.app, client=("127.0.0.1", 40000))
    auth = {"X-Forwarded-User": "owner"}
    response = client.get("/api/notes/recent?limit=12", headers=auth)
    check("GET /api/notes/recent?limit=12 proxies", response.status_code == 200,
          (response.status_code, response.text[:200]))
    check("the canonical limit and credentials reach upstream",
          SEEN == [{"path": "/api/notes/recent", "query": "limit=12",
                    "authorization": "Bearer " + TOKEN, "actor": "owner"}], SEEN)
    check("the recent payload reaches the browser", response.json()["notes"][0]["title"] == "Alpha")
    response = client.get("/api/notes/recent?limit=12")
    check("the web login gate still applies", response.status_code == 401 and len(SEEN) == 1,
          (response.status_code, SEEN))
    response = client.get("/api/notes/writes?limit=12", headers=auth)
    check("the write log beside recent remains closed", response.status_code == 404 and len(SEEN) == 1,
          (response.status_code, SEEN))
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(run())
