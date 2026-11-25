"""The service worker's VERSION is stamped by the web role, never hand-bumped.

Run: python3 tests/test_sw_version.py   (from the repo root)

web/sw.js carries a placeholder; app/web.py serves it with the placeholder replaced by
`helm-app-<12 hex>`, a sha256 over every file under web/ (names and contents). So a change
to any shell file gives installed clients a new cache and drops the old shell, an unchanged
tree keeps the same VERSION, and two open web PRs no longer edit the same line of sw.js.
Proves: the served VERSION is derived (not the literal in the file); it is stable across
reads; it changes when a shell file changes (and when a file is added) and comes back when
the change is undone; sw.js keeps no-cache and Service-Worker-Allowed; the file in the repo
holds the placeholder and no hand-written VERSION.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["HELM_APP_TOKEN"] = "sw-version-test-token"
os.environ["HELM_API_URL"] = "http://api.test"
os.environ.pop("LEGACY_DASHBOARD_URL", None)
os.environ.pop("HELM_WEB_REQUIRE_AUTH_HEADER", None)

from starlette.testclient import TestClient  # noqa: E402

import app.web as web  # noqa: E402

PASS = FAIL = 0
AUTH = {"X-Forwarded-User": "tester"}
REPO_WEB = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "web"
VERSION_RE = re.compile(r'const VERSION = "([^"]*)"')


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail else ""))


def served_version(c):
    r = c.get("/sw.js", headers=AUTH)
    m = VERSION_RE.search(r.text)
    return r, (m.group(1) if m else None)


def run():
    print("the file in the repo: a placeholder, no hand-bumped VERSION")
    src = (REPO_WEB / "sw.js").read_text(encoding="utf-8")
    m = VERSION_RE.search(src)
    check("web/sw.js declares VERSION from the placeholder", m and m.group(1) == "__HELM_SW_VERSION__", m and m.group(1))
    check("web/sw.js holds no literal helm-app-vN VERSION", not re.search(r"helm-app-v\d+", src))

    print("\nserved through the web role: stamped with a digest of web/")
    orig_root = web.WEB_ROOT
    tmp = Path(tempfile.mkdtemp(prefix="helm-sw-"))
    try:
        root = tmp / "web"
        shutil.copytree(REPO_WEB, root, symlinks=True)
        web.WEB_ROOT = root.resolve()
        c = TestClient(web.app, client=("127.0.0.1", 40000))

        r, v1 = served_version(c)
        check("sw.js 200, no-cache, allowed at /", r.status_code == 200 and r.headers["cache-control"] == "no-cache"
              and r.headers.get("service-worker-allowed") == "/" and "javascript" in r.headers["content-type"], r.status_code)
        check("the served VERSION is derived: helm-app-<12 hex>", v1 and re.fullmatch(r"helm-app-[0-9a-f]{12}", v1), v1)
        check("the placeholder never reaches the browser", "__HELM_SW_VERSION__" not in r.text)
        h = c.head("/sw.js", headers=AUTH)
        check("HEAD's Content-Length is the stamped body's", h.status_code == 200 and h.headers.get("content-length") == str(len(r.content)),
              (h.headers.get("content-length"), len(r.content)))
        _, again = served_version(c)
        check("an unchanged tree keeps the same VERSION", again == v1, (v1, again))

        app_js = root / "app.js"
        before = app_js.read_bytes()
        app_js.write_bytes(before + b"\n// a change to the shell\n")
        _, v2 = served_version(c)
        check("a change to a shell file (app.js) gives a new VERSION", v2 and v2 != v1, (v1, v2))
        app_js.write_bytes(before)
        _, v3 = served_version(c)
        check("undoing the change gives the old VERSION back (a digest, not a counter)", v3 == v1, (v1, v3))

        theme = root / "fonts" / "instrument-sans-400.woff2"
        tb = theme.read_bytes()
        theme.write_bytes(tb + b"\x00")
        _, v4 = served_version(c)
        check("a change in a nested shell file (a local font) gives a new VERSION", v4 and v4 != v1, (v1, v4))
        theme.write_bytes(tb)

        (root / "new-screen.js").write_text("export const x = 1;\n")
        _, v5 = served_version(c)
        check("a new file under web/ gives a new VERSION", v5 and v5 != v1, (v1, v5))
        (root / "new-screen.js").unlink()

        outside = tmp / "outside.txt"
        outside.write_text("not the shell")
        os.symlink(outside, root / "zz-escape-link")
        _, v6 = served_version(c)
        check("a symlink leading out of web/ is not read into the digest", v6 == v1, (v1, v6))
        (root / "zz-escape-link").unlink()

        body = r.text.replace(v1, "__HELM_SW_VERSION__")
        check("apart from VERSION the served worker is the file as written", body == (root / "sw.js").read_text(encoding="utf-8"))
        check("other static files are served untouched",
              c.get("/app.js", headers=AUTH).content == app_js.read_bytes())
    finally:
        web.WEB_ROOT = orig_root
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    run()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
