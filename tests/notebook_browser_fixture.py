"""Static loopback fixture for the notebook-only Playwright suites.

The full app fixture is POSIX-only (the notebook's no-link file walk uses
O_DIRECTORY/O_NOFOLLOW). These tests need the shipped browser assets, not that
file walk: serve web/ directly and answer the shell's incidental API reads with
small synthetic payloads. Notebook-specific routes are layered on by each suite.
The real app.web proxy is tested separately in test_notebook_recent_web.py.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass


@contextmanager
def static_site():
    handler = lambda *args, **kwargs: QuietHandler(*args, directory=WEB, **kwargs)  # noqa: E731
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(web_url="http://127.0.0.1:%d" % server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fulfill(route, payload, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))


def install_shell_api(ctx):
    """Answer only the shell/inbox's background reads; notebook routes override this."""
    def api(route):
        path = urllib.parse.urlsplit(route.request.url).path
        if path == "/api/meta":
            return fulfill(route, {"ok": True, "site": "work", "modules": ["inbox", "notebook", "activity", "search"],
                                   "notes_mode": "store", "web": {}})
        if path in ("/api/triage/threads", "/api/triage/threads/index"):
            return fulfill(route, {"threads": [], "total": 0, "page": 1, "pages": 1})
        if path == "/api/triage/events":
            return fulfill(route, {"events": []})
        if path == "/api/triage/gate":
            return fulfill(route, {"gate": {}})
        if path == "/api/status":
            return fulfill(route, {"ok": True})
        if path == "/api/triage/dispatcher-heartbeat":
            return fulfill(route, {"detail": "no synthetic heartbeat"}, 404)
        return fulfill(route, {"detail": "not supplied by the static notebook fixture"}, 404)

    # Playwright uses the most recently registered matching route first. Suites
    # call this before installing their notebook routes.
    ctx.route("**/api/**", api)
