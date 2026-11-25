"""Ctrl+click on a [[wikilink]] in the editor opens the note the link names.

The editor used to ask the autocomplete (substring match, 12 items, newest first) and look for
an exact name among them, so with a dozen newer notes called "Plan N" a link to [[Plan]] said
"no note called Plan yet" while the read view linked it fine. It asks /api/notes/resolve now,
the resolver the rendered links use:

  1. the exact-stem note is found even when more than 12 other notes contain the text
  2. two notes sharing a stem resolve the way the rendered link does (shortest path first)
  3. a path form, an alias/heading, a non-note file, a moved note's old name, a missing name
  4. a hidden or unindexed path is never offered
  5. the web role relays the route
"""
# temp-ok: the folder and DBs under test are fixtures this module creates and deletes
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="helm_test_resolve_")
WIKI = os.path.join(TMP, "wiki")
TOKEN = "resolve-test-token-7b2e"
os.environ["HELM_NOTES_PROFILE"] = "wiki"
os.environ["HELM_NOTES_MODE"] = "folder-readonly"
os.environ["HELM_NOTES_ROOT"] = WIKI
os.environ["HELM_NOTES_DB"] = os.path.join(TMP, "notes.db")
os.environ["HELM_DB"] = os.path.join(TMP, "triage.db")
os.environ["HELM_DATA"] = TMP
os.environ["HELM_APP_TOKEN"] = TOKEN
os.environ["HELM_API_URL"] = "http://api.test"
for k in ("LEGACY_DASHBOARD_URL", "LEGACY_CONSOLE_TOKEN", "HELM_WEB_REQUIRE_AUTH_HEADER"):
    os.environ.pop(k, None)

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from notebook import notes  # noqa: E402
import app.notebook_api as notebook_api  # noqa: E402
import app.web as web  # noqa: E402

PASS = FAIL = 0
AUTH = {"X-Forwarded-User": "owner"}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:400] if detail != "" else ""))


def write(rel, data, mtime=None):
    p = os.path.join(WIKI, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def seed():
    old = time.time() - 86400 * 400
    write("Projects/Plan.md", "# Plan\n\nthe exact note, and the oldest.\n", old)
    for i in range(14):
        write("Reference/Plan %d.md" % i, "# Plan %d\n" % i, time.time() - i)
    # two notes with one stem: the shorter path is what a rendered [[Twin]] opens
    write("Reference/Twin.md", "# Twin\n")
    write("Projects/Deep/Nested/Twin.md", "# Twin deeper\n")
    write("Reference/Contact.md", "# Contact\n")
    write("attachments/report.pdf", b"%PDF-1.4 fake")
    write("Personal/.hidden-note.md", "# hidden\n")
    write("Daily/2026-10-04.md", "[[Plan]] and [[Twin]]\n")


def run() -> int:
    os.makedirs(WIKI, exist_ok=True)
    seed()
    api = TestClient(Starlette(routes=notebook_api.routes))
    web.UPSTREAM_TRANSPORT = httpx.ASGITransport(app=Starlette(routes=notebook_api.routes))
    wc = TestClient(web.app, client=("127.0.0.1", 40000))
    notes.full_index()
    con = notes.db()

    def resolve(target):
        r = api.get("/api/notes/resolve", params={"target": target})
        return r.status_code, r.json()

    print("\n1. the exact stem wins over any number of other matches")
    items = api.get("/api/notes/complete", params={"q": "Plan", "kind": "all"}).json()["items"]
    check("setup: the autocomplete's 12 items do not hold Projects/Plan.md",
          len(items) == 12 and not any(i["rel"] == "Projects/Plan.md" for i in items), [i["rel"] for i in items])
    code, body = resolve("Plan")
    check("[[Plan]] resolves to Projects/Plan.md", code == 200 and body.get("kind") == "note" and body.get("rel") == "Projects/Plan.md", body)
    code, body = resolve("plan")
    check("the match ignores case", body.get("rel") == "Projects/Plan.md", body)
    code, body = resolve("Plan 3")
    check("a longer name resolves to its own note", body.get("rel") == "Reference/Plan 3.md", body)

    print("\n2. the same note a rendered link opens")
    html = api.get("/api/notes/note", params={"rel": "Daily/2026-10-04.md"}).json()["html"]
    for t in ("Plan", "Twin"):
        want = resolve(t)[1].get("rel")
        check("[[%s]]: Ctrl+click target is the read view's link (%s)" % (t, want),
              want and 'data-rel="%s"' % want in html, html)
    check("two notes sharing a stem: the shorter path", resolve("Twin")[1].get("rel") == "Reference/Twin.md", resolve("Twin"))

    print("\n3. other link forms")
    check("a path form", resolve("Projects/Plan")[1].get("rel") == "Projects/Plan.md", resolve("Projects/Plan"))
    check("a path suffix with .md", resolve("Nested/Twin.md")[1].get("rel") == "Projects/Deep/Nested/Twin.md", resolve("Nested/Twin.md"))
    check("a heading and alias are ignored", resolve("Plan#Steps|the plan")[1].get("rel") == "Projects/Plan.md", resolve("Plan#Steps|the plan"))
    code, body = resolve("report.pdf")
    check("a non-note file is a file", code == 200 and body.get("kind") == "file" and body.get("rel") == "attachments/report.pdf", body)
    code, body = resolve("No Such Page")
    check("a missing name is 200 with no rel", code == 200 and body.get("rel") is None and body.get("kind") is None, (code, body))
    code, body = resolve("")
    check("an empty target is no note", code == 200 and body.get("rel") is None, (code, body))

    print("\n4. hidden or old-index rows are not offered")
    notes.upsert_note(con, "Personal/.hidden-note.md", "# hidden\n", time.time(), 40)
    con.commit()
    code, body = resolve("Personal/.hidden-note")
    check("a hidden path is not resolved", code == 200 and body.get("rel") is None, (code, body))
    code, body = resolve(".hidden-note")
    check("a hidden stem is not resolved", code == 200 and body.get("rel") is None, (code, body))

    print("\n5. through the web role")
    r = wc.get("/api/notes/resolve", params={"target": "Plan"})
    check("no login header: refused", r.status_code in (401, 403, 404), r.status_code)
    r = wc.get("/api/notes/resolve", params={"target": "Plan"}, headers=AUTH)
    check("with the login header it relays", r.status_code == 200 and r.json().get("rel") == "Projects/Plan.md", (r.status_code, r.text[:200]))

    con.close()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
