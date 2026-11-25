"""The web role streams a vault attachment (`/att/*`) and answers HEAD with headers only.

Run: python3 tests/test_web_att_stream.py   (from the repo root)

Before: `_proxy` read the whole upstream body (`r.content`) before answering, so a plain GET of a
big file sat whole in the web role's memory, and a HEAD went upstream as a full GET. The fake api
role here is an httpx transport whose body is a generator that will not hand over its second chunk
until the browser has had the first byte (it gives up after a short wait and records that the
proxy buffered). A HEAD must reach the api as HEAD, read no body, and send none.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["HELM_APP_TOKEN"] = "web-att-stream-test-token"
os.environ["HELM_API_URL"] = "http://api.test"
os.environ.pop("LEGACY_DASHBOARD_URL", None)
os.environ.pop("HELM_WEB_REQUIRE_AUTH_HEADER", None)

import httpx  # noqa: E402

import app.web as web  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  ", name)
    else:
        FAIL += 1
        print("  FAIL", name, detail)


CHUNK = 64 * 1024
NCHUNKS = 16                       # 1 MB: sixteen chunks, the proxy may hold one


class Upstream(httpx.AsyncBaseTransport):
    """The api role's /att answer: a 200 with Content-Length whose body is produced lazily."""

    def __init__(self):
        self.methods = []
        self.headers = []
        self.yielded = 0            # chunks handed to the proxy so far
        self.buffered = False       # a chunk past the first was pulled before the browser saw a byte
        self.closed = False
        self.first_byte = asyncio.Event()

    async def handle_async_request(self, request):
        self.methods.append(request.method)
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        up = self
        headers = {"content-type": "video/mp4", "content-length": str(CHUNK * NCHUNKS),
                   "accept-ranges": "bytes", "etag": '"1-2-3"', "cache-control": "private, max-age=300"}

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                for i in range(NCHUNKS):
                    if i == 2 and not up.first_byte.is_set():
                        try:
                            await asyncio.wait_for(up.first_byte.wait(), 1.0)
                        except asyncio.TimeoutError:
                            up.buffered = True
                    up.yielded += 1
                    yield bytes([65 + i % 26]) * CHUNK

            async def aclose(self):
                up.closed = True

        return httpx.Response(200, headers=headers, stream=Body())   # a HEAD that reads this body is the bug


def call(method, path, headers=(), disconnect_after_start=False, spec="2.3"):
    """Drive the web app as uvicorn would; note when the first body byte leaves."""
    up = web.UPSTREAM_TRANSPORT
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 1), "server": ("testserver", 80),
             "headers": [(b"x-forwarded-user", b"owner"), (b"host", b"testserver")] + list(headers)}
    out = {"status": None, "headers": {}, "body": b"", "yielded_at_first_byte": None}
    gone = asyncio.Event()

    async def receive():
        if not out.get("asked"):
            out["asked"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
            if disconnect_after_start:
                gone.set()
        elif msg["type"] == "http.response.body":
            if msg.get("body") and out["yielded_at_first_byte"] is None and isinstance(up, Upstream):
                out["yielded_at_first_byte"] = up.yielded
                up.first_byte.set()
            out["body"] += msg.get("body", b"")
            if disconnect_after_start and out["body"]:
                raise OSError("client went away")

    async def main():
        try:
            await asyncio.wait_for(web.app(scope, receive, send), 10)
        except Exception:
            if not disconnect_after_start:   # 2.4 re-raises the gone client as ClientDisconnect
                raise
        await asyncio.sleep(0)
    asyncio.run(main())
    return out


def run():
    print("GET /att of a big file: streamed, never held whole")
    web.UPSTREAM_TRANSPORT = up = Upstream()
    r = call("GET", "/att/Media/clip.mp4")
    check("200 with every byte, in order", r["status"] == 200 and len(r["body"]) == CHUNK * NCHUNKS
          and r["body"][:1] == b"A" and r["body"][-1:] == bytes([65 + (NCHUNKS - 1) % 26]), (r["status"], len(r["body"])))
    check("the browser had its first byte before the proxy pulled a third chunk (no whole-file read)",
          not up.buffered and r["yielded_at_first_byte"] is not None and r["yielded_at_first_byte"] <= 2,
          (up.buffered, r["yielded_at_first_byte"]))
    check("the upstream response is closed when the body is done", up.closed)
    h = r["headers"]
    check("the type, Content-Length, ETag and Accept-Ranges come through",
          h.get("content-type") == "video/mp4" and h.get("content-length") == str(CHUNK * NCHUNKS)
          and h.get("etag") == '"1-2-3"' and h.get("accept-ranges") == "bytes", h)
    check("still sandboxed and never sniffed", h.get("content-security-policy") == "sandbox"
          and h.get("x-content-type-options") == "nosniff", h)
    check("the bearer went upstream, the api was asked for no encoding (raw bytes relayed as is)",
          up.headers[-1].get("authorization") == "Bearer " + web.TOKEN
          and up.headers[-1].get("accept-encoding") == "identity", up.headers[-1])
    check("no token in any header the browser got", not any(web.TOKEN in v for v in h.values()))

    print("\nHEAD /att: headers only, asked upstream as HEAD")
    web.UPSTREAM_TRANSPORT = up = Upstream()
    r = call("HEAD", "/att/Media/clip.mp4")
    check("HEAD reaches the api as HEAD, not GET", up.methods == ["HEAD"], up.methods)
    check("no body was read from the api", up.yielded == 0, up.yielded)
    check("200, no body, the file's length and type", r["status"] == 200 and r["body"] == b""
          and r["headers"].get("content-length") == str(CHUNK * NCHUNKS)
          and r["headers"].get("content-type") == "video/mp4", (r["status"], len(r["body"]), r["headers"]))
    check("HEAD is sandboxed too", r["headers"].get("content-security-policy") == "sandbox")

    print("\nthe browser leaves after the first byte: the upstream is closed, nothing more is pulled")
    for spec in ("2.3", "2.4"):    # 2.3 cancels on http.disconnect; 2.4 raises on the send and skips a background task
        web.UPSTREAM_TRANSPORT = up = Upstream()
        call("GET", "/att/Media/clip.mp4", disconnect_after_start=True, spec=spec)
        check("ASGI %s: the upstream response is closed" % spec, up.closed)
        check("ASGI %s: the proxy stopped pulling (%d of %d chunks)" % (spec, up.yielded, NCHUNKS), up.yielded < NCHUNKS, up.yielded)

    print("\nRange and If-None-Match still forwarded for /att")
    web.UPSTREAM_TRANSPORT = up = Upstream()
    call("GET", "/att/Media/clip.mp4", headers=[(b"range", b"bytes=0-99"), (b"if-none-match", b'"1-2-3"')])
    check("Range and If-None-Match reach the api", up.headers[-1].get("range") == "bytes=0-99"
          and up.headers[-1].get("if-none-match") == '"1-2-3"', up.headers[-1])

    print("\napi down: still a 502 for /att")
    web.UPSTREAM_TRANSPORT = httpx.MockTransport(lambda req: (_ for _ in ()).throw(RuntimeError("connect failed")))
    r = call("GET", "/att/Media/clip.mp4")
    check("502 gateway unreachable, sandboxed", r["status"] == 502 and b"gateway unreachable" in r["body"]
          and r["headers"].get("content-security-policy") == "sandbox", (r["status"], r["body"][:120]))


if __name__ == "__main__":
    run()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
