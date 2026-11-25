"""A request a route matched is counted under that route, including a `{name:path}` route.

The metric matchers compiled every path parameter as one segment, so `/att/{rel:path}`
became `^/att/[^/]+$`: a nested attachment (206) or a directory (403) that the router
matched and answered was counted as `/<unmatched>`, and the 403 raised a false "new error
kind" in the error watch.

Run: python3 tests/run_all.py   (or this file alone, from the repo root)
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_metric_paths_")
WIKI = os.path.join(TMP, "wiki")
TOKEN = "metric-paths-test-token-3e9a"
os.environ["HELM_SITE"] = "home"
os.environ["HELM_NOTES_PROFILE"] = "wiki"
os.environ["HELM_NOTES_MODE"] = "folder-readonly"
os.environ["HELM_NOTES_ROOT"] = WIKI
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ["HELM_APP_TOKEN"] = TOKEN
os.environ.pop("HELM_SENTRY_DSN", None)

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
for rel in ("attachments/logo.png", "cowork/generated-images/edit-1.png", "Home.md"):
    p = os.path.join(WIKI, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(PNG if rel.endswith(".png") else b"# Home\n")

from starlette.routing import Route  # noqa: E402
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


print("the compiled matchers")
TEMPLATES = ["/att/{rel:path}", "/api/triage/threads/{thread_id}", "/n/{a:int}/x"]
keys = svc._compile_route_metric_keys(TEMPLATES)


def label(path):
    for matcher, lab in keys:
        if matcher.match(path):
            return lab
    return svc.UNMATCHED_PATH


check(":path matches one segment", label("/att/logo.png") == "/att/:id", label("/att/logo.png"))
check(":path matches nested segments", label("/att/a/b/c.png") == "/att/:id", label("/att/a/b/c.png"))
check(":path matches a trailing slash", label("/att/attachments/") == "/att/:id", label("/att/attachments/"))
check("a plain parameter stays one segment",
      label("/api/triage/threads/t-1/extra") == svc.UNMATCHED_PATH, label("/api/triage/threads/t-1/extra"))
check("a plain parameter still matches", label("/api/triage/threads/t-1") == "/api/triage/threads/:id")
check("an :int parameter stays one segment and digits only",
      label("/n/5/x") == "/n/:id/x" and label("/n/5/6/x") == svc.UNMATCHED_PATH
      and label("/n/abc/x") == svc.UNMATCHED_PATH, [label("/n/5/x"), label("/n/5/6/x"), label("/n/abc/x")])

# The metric label must agree with the router: counted under a route exactly when the
# router would dispatch the path to that route, and /<unmatched> otherwise.
routes = [Route(t, lambda r: None) for t in TEMPLATES]
disagree = []
for path in ("/att/", "/att/x", "/att/a/b/", "/att", "/api/triage/threads/", "/api/triage/threads/t-1",
             "/api/triage/threads/t-1/", "/n/5/x", "/n/5/6/x", "/n/x/x", "/nope"):
    routed = next((r.path for r in routes if r.path_regex.match(path)), None)
    want = svc._PATH_PARAM_RE.sub(":id", routed) if routed else svc.UNMATCHED_PATH
    if label(path) != want:
        disagree.append((path, label(path), want))
check("every label agrees with the router's own match", not disagree, disagree)

print("\nthrough the API: nested and directory /att/ requests are counted under /att/:id")
with TestClient(svc.app) as client:
    r = client.get("/att/cowork/generated-images/edit-1.png", headers=AUTH)
    check("nested attachment is served", r.status_code in (200, 206), r.status_code)
    r = client.get("/att/attachments/", headers=AUTH)
    check("a directory is refused", r.status_code == 403, r.status_code)
    text = client.get("/metrics", headers=AUTH).text
    series = "\n".join(ln for ln in text.splitlines() if "/att/" in ln or "<unmatched>" in ln)
    ok_status = 200 if 'path="/att/:id",status="200"' in text else 206
    check("the nested 2xx is counted under /att/:id",
          f'helm_http_requests_total{{method="GET",path="/att/:id",status="{ok_status}"}} 1' in text, series)
    check("the directory 403 is counted under /att/:id",
          'helm_http_requests_total{method="GET",path="/att/:id",status="403"} 1' in text, series)
    check("the 403 client error carries the route label",
          'helm_client_errors_total{method="GET",path="/att/:id",status="403"' in text, series)
    check("neither lands in /<unmatched>", 'path="/<unmatched>"' not in text, series)

print("\nthrough the API: the /api/threads aliases are counted under the canonical route")
with TestClient(svc.app) as client:
    client.get("/api/threads", headers=AUTH)
    client.get("/api/threads/t-nope", headers=AUTH)
    client.get("/api/threadz", headers=AUTH)
    text = client.get("/metrics", headers=AUTH).text
    series = "\n".join(ln for ln in text.splitlines() if "threads" in ln or "<unmatched>" in ln)
    check("the list alias lands on /api/triage/threads",
          'helm_http_requests_total{method="GET",path="/api/triage/threads",status="200"} 1' in text, series)
    check("the by-id alias lands on /api/triage/threads/:id",
          'helm_http_requests_total{method="GET",path="/api/triage/threads/:id",status="404"} 1' in text, series)
    check("no alias series of its own", 'path="/api/threads' not in text, series)
    check("an unrouted guess is still /<unmatched>",
          'helm_http_requests_total{method="GET",path="/<unmatched>",status="404"} 1' in text, series)

print("\nthrough the API: GET /api/notes/read is the read-only alias of /api/notes/raw")
with TestClient(svc.app) as client:
    a = client.get("/api/notes/read", params={"rel": "Home.md"}, headers=AUTH)
    c = client.get("/api/notes/raw", params={"rel": "Home.md"}, headers=AUTH)
    check("the alias answers 200 with the canonical body",
          a.status_code == 200 and c.status_code == 200 and a.json() == c.json(), (a.status_code, a.text[:200]))
    check("the alias needs the bearer token too", client.get("/api/notes/read", params={"rel": "Home.md"}).status_code == 401)
    for method in ("POST", "PUT", "DELETE"):
        r = client.request(method, "/api/notes/read", json={"rel": "Home.md", "text": "x"}, headers=AUTH)
        check("%s /api/notes/read is not a write door" % method, r.status_code in (404, 405), r.status_code)
    g = client.get("/api/notes/fetch", params={"rel": "Home.md"}, headers=AUTH)
    check("another notes guess is a 404 whose hint names GET /api/notes/raw",
          g.status_code == 404 and "/api/notes/raw" in g.json().get("hint", ""), (g.status_code, g.text[:200]))
    t = client.get("/api/thread", headers=AUTH)
    check("the thread hint is unchanged and does not name notes",
          t.status_code == 404 and "/api/triage/threads" in t.json().get("hint", "")
          and "/api/notes" not in t.json().get("hint", ""), t.text[:200])
    text = client.get("/metrics", headers=AUTH).text
    series = "\n".join(ln for ln in text.splitlines() if "notes" in ln or "<unmatched>" in ln)
    check("the alias is counted under /api/notes/raw",
          'helm_http_requests_total{method="GET",path="/api/notes/raw",status="200"} 2' in text, series)
    check("no alias series of its own", 'path="/api/notes/read' not in text, series)

print("\nthrough the API: the the wiki engine read vocabulary is read-only aliases of the canonical reads")
SB_ALIASES = [
    # (guessed path, params, canonical path)
    ("/api/notes/get", {"rel": "Home.md"}, "/api/notes/raw"),
    ("/api/notes", {"folder": "Playbooks"}, "/api/notes/list"),
    ("/api/notes/search", {"q": "x"}, "/api/search"),
    ("/api/fs/list", {"folder": "Playbooks"}, "/api/notes/list"),
    ("/api/space/list", {"folder": "Playbooks"}, "/api/notes/list"),
]
UNMATCHED_404 = 'helm_http_requests_total{method="GET",path="/<unmatched>",status="404"} '


def unmatched_404(metrics_text):
    for ln in metrics_text.splitlines():
        if ln.startswith(UNMATCHED_404):
            return int(float(ln[len(UNMATCHED_404):]))
    return 0


with TestClient(svc.app) as client:
    before = unmatched_404(client.get("/metrics", headers=AUTH).text)
    for guess, params, canon in SB_ALIASES:
        a = client.get(guess, params=params, headers=AUTH)
        c = client.get(canon, params=params, headers=AUTH)
        check("GET %s answers 200 with the body of GET %s" % (guess, canon),
              a.status_code == 200 and c.status_code == 200 and a.json() == c.json(), (a.status_code, a.text[:200]))
        check("%s needs the bearer token too" % guess, client.get(guess, params=params).status_code == 401)
        for method in ("POST", "PUT", "DELETE"):
            r = client.request(method, guess, json={"rel": "Home.md", "text": "x"}, headers=AUTH)
            check("%s %s is not a write door" % (method, guess), r.status_code in (404, 405), r.status_code)
    for prefix in ("/api/fs", "/api/space"):
        g = client.get(prefix + "/whatever", headers=AUTH)
        check("unrouted GET %s/whatever is a 404 whose hint names GET /api/notes/list" % prefix,
              g.status_code == 404 and "/api/notes/list" in g.json().get("hint", ""), (g.status_code, g.text[:200]))
    text = client.get("/metrics", headers=AUTH).text
    series = "\n".join(ln for ln in text.splitlines() if "notes" in ln or "search" in ln or "<unmatched>" in ln)
    for alias in ("/api/notes/get", "/api/notes/search", "/api/fs", "/api/space"):
        check("no alias series of its own for %s" % alias, 'path="%s' % alias not in text, series)
    check("no GET under /api/notes (the bare alias) has a series of its own",
          'path="/api/notes"' not in text, series)
    check("only the two unrouted fs/space probes are /<unmatched>",
          unmatched_404(text) - before == 2, (before, unmatched_404(text)))

print("\nthrough the API: GET /api/notes/revisions is the read-only alias of /api/notes/history")
with TestClient(svc.app) as client:
    before = unmatched_404(client.get("/metrics", headers=AUTH).text)
    a = client.get("/api/notes/revisions", params={"rel": "Home.md"}, headers=AUTH)
    c = client.get("/api/notes/history", params={"rel": "Home.md"}, headers=AUTH)
    check("the alias answers 200 with the canonical body",
          a.status_code == 200 and c.status_code == 200 and a.json() == c.json(), (a.status_code, a.text[:200]))
    check("the body carries rel, truth and revisions",
          a.status_code == 200 and {"rel", "truth", "revisions"} <= set(a.json()), a.text[:200])
    check("the alias needs the bearer token too",
          client.get("/api/notes/revisions", params={"rel": "Home.md"}).status_code == 401)
    for method in ("POST", "PUT", "DELETE"):
        r = client.request(method, "/api/notes/revisions", json={"rel": "Home.md", "text": "x"}, headers=AUTH)
        check("%s /api/notes/revisions is not a write door" % method, r.status_code in (404, 405), r.status_code)
    g = client.get("/api/notes/fetch", params={"rel": "Home.md"}, headers=AUTH)
    hint = g.json().get("hint", "") if g.status_code == 404 else ""
    check("the /api/notes guess hint names GET /api/notes/history", "/api/notes/history" in hint, hint)
    check("the /api/notes guess hint still names GET /api/notes/raw", "/api/notes/raw" in hint, hint)
    text = client.get("/metrics", headers=AUTH).text
    series = "\n".join(ln for ln in text.splitlines() if "notes" in ln or "<unmatched>" in ln)
    check("the alias is counted under /api/notes/history",
          'helm_http_requests_total{method="GET",path="/api/notes/history",status="200"} 2' in text, series)
    check("no alias series of its own", 'path="/api/notes/revisions' not in text, series)
    check("only the unrouted /api/notes/fetch probe is /<unmatched>",
          unmatched_404(text) - before == 1, (before, unmatched_404(text)))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
