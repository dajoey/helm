"""A page, a diff or a screenshot an agent built opens from the card it reported on (offline).

Run: python3 tests/test_run_preview.py   (from the repo root)

The ask (the run-preview directive, 2026-10-14): what a run builds had to be
read as a C:\\ path. Thread attachments already existed (the attach route, the list, the bytes),
but an HTML file always came back as a DOWNLOAD (the notebook's rule for active content), and a
diff had no viewer. This suite is the server half of the fix:

  * GET /api/triage/threads/<id>/attachments/<att>/view serves an agent's HTML for a frame:
    text/html, with a Content-Security-Policy header that sandboxes it (no same-origin, no
    popups, no top navigation, no forms) and allows no network at all, whatever is opened
    where. Only HTML is served that way (415 for anything else); the stored type wins over the
    file name, so a .html name over text/plain stays text.
  * The web role passes that route and sets the SAME policy itself (it overwrites any
    /attachments answer's policy with `sandbox`, which would have un-sandboxed nothing but
    also lost the network lock), and it never relays a write to it.
  * An agent's upload (uploader other than "owner") has a type allowlist, a per-file cap, a
    per-thread count and byte cap, and a credential tripwire on its text. the owner's own files
    and the existing producers (card visuals: png, svg, pdf, txt) are untouched.

The browser half (the hostile page failing every attempt) is tests/test_screens_run_preview.py.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "run-preview-test-token-5b2d7a"
os.environ["HELM_APP_TOKEN"] = TOKEN
os.environ["HELM_SITE"] = "work"
os.environ.pop("LEGACY_DASHBOARD_URL", None)
os.environ.pop("LEGACY_CONSOLE_TOKEN", None)
os.environ.pop("HELM_WEB_REQUIRE_AUTH_HEADER", None)
TMP = tempfile.mkdtemp(prefix="helm-run-preview-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")
os.environ["HELM_API_URL"] = "http://api.test"

import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.service as svc  # noqa: E402
import app.web as web  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail else ""))


api = TestClient(svc.app)
AUTH = {"Authorization": "Bearer " + TOKEN}
web.UPSTREAM_TRANSPORT = httpx.ASGITransport(app=svc.app)
webc = TestClient(web.app, client=("127.0.0.1", 40000))
WEB = {"X-Forwarded-User": "owner"}

# Synthetic fixtures only: no run file from any real site is used or described here.
HTML = (b"<!doctype html><title>Synthetic report</title><style>b{color:#080}</style>"
        b"<p>Pine Hollow portal, ticket 00100001 (fictional).</p><script>document.title='ran'</script>")
DIFF = (b"diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n keep\n-old line\n+new line\n")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def put_thread(tid):
    r = api.put("/api/triage/threads", json={"threads": [{
        "thread_id": tid, "title": "Synthetic run report", "summary": "A fictional run built a page " * 2,
        "category": "Test", "urgency": "whenever", "state": "needs-you",
        "ask": {"kind": "ack", "prompt": "Read the page and close the card", "requires": ["decision"],
                "options": [{"label": "Got it", "value": "ack", "effect": "Closes the card"}]},
        "sources": [{"source": "test", "id": tid}]}]}, headers=AUTH)
    assert r.status_code == 200, r.text
    return tid


def upload(tid, name, mime, data, uploader="agent", client=None):
    return (client or api).post("/api/triage/threads/%s/attach" % tid, json={
        "filename": name, "mime": mime, "uploader": uploader,
        "content_base64": base64.b64encode(data).decode()}, headers=AUTH)


# ---------------------------------------------------------------- what is previewable
print("\nwhat counts as previewable (one rule, server and screens)")
try:
    from helm import preview
    ok_import = True
except Exception as e:  # noqa: BLE001
    preview = None
    ok_import = False
    check("helm.preview imports", False, repr(e))
if preview:
    K = preview.preview_kind
    rows = [("text/html", "r.html", "html"), ("text/html; charset=utf-8", "r", "html"),
            ("text/plain", "r.html", "text"),                      # the stored type wins: this is text
            ("application/octet-stream", "r.html", "html"), ("", "R.HTM", "html"),
            ("text/x-diff", "a.diff", "diff"), ("text/x-patch", "a", "diff"), ("text/plain", "fix.patch", "diff"),
            ("application/octet-stream", "fix.diff", "diff"),
            ("image/png", "s.png", "image"), ("image/jpeg", "s.jpg", "image"), ("image/svg+xml", "s.svg", "image"),
            ("text/plain", "notes.txt", "text"), ("text/markdown", "n.md", "text"), ("application/json", "d.json", "text"),
            ("application/pdf", "x.pdf", "none"), ("application/zip", "x.zip", "none"),
            ("application/xhtml+xml", "x.xhtml", "none"), ("image/png", "x.html", "image")]
    for mime, name, want in rows:
        check("%r %r -> %s" % (mime, name, want), K(mime, name) == want, K(mime, name))

# ---------------------------------------------------------------- the view route
print("\nGET .../attachments/<att>/view serves an agent's HTML for a frame")
tid = put_thread("t-rp-1")
rec = upload(tid, "report.html", "text/html", HTML)
check("an agent's HTML uploads (201)", rec.status_code == 201, rec.text)
att = rec.json().get("att_id", "x")
r = api.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, att), headers=AUTH)
check("view answers 200 with the bytes as text/html", r.status_code == 200 and r.content == HTML
      and r.headers.get("content-type", "").startswith("text/html"), (r.status_code, r.headers.get("content-type")))
csp = r.headers.get("content-security-policy", "")
check("view sends a sandbox that allows scripts and nothing else", csp.startswith("sandbox allow-scripts;"), csp)
for bad in ("allow-same-origin", "allow-top-navigation", "allow-popups", "allow-forms", "allow-modals", "allow-downloads"):
    check("view never grants " + bad, bad not in csp, csp)
for want in ("default-src 'none'", "connect-src 'none'", "form-action 'none'", "frame-src 'none'", "object-src 'none'",
             "base-uri 'none'", "frame-ancestors 'self'", "img-src data: blob:"):
    check("view policy carries " + want, want in csp, csp)
check("view policy allows no remote script, style, image or font (no http/https/* source anywhere)",
      "http:" not in csp and "https:" not in csp and " * " not in csp + " " and "unsafe-eval" not in csp, csp)
check("view is never sniffed and never cached", r.headers.get("x-content-type-options") == "nosniff"
      and "no-store" in r.headers.get("cache-control", "") and r.headers.get("referrer-policy") == "no-referrer", r.headers)
r = api.get("/api/triage/threads/%s/attachments/%s" % (tid, att), headers=AUTH)
check("the plain attachment route is unchanged: HTML still downloads", r.headers.get("content-type") == "application/octet-stream"
      and r.headers.get("content-disposition", "").startswith("attachment"), r.headers)
check("view needs the bearer", api.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, att)).status_code == 401)
r = api.get("/api/triage/threads/%s/attachments/nope/view?raw=1" % tid, headers=AUTH)
check("an unknown attachment is 404", r.status_code == 404, r.status_code)
r = api.get("/api/triage/threads/..%%2F..%%2Fx/attachments/%s/view?raw=1" % att, headers=AUTH)
check("a path trick is 404", r.status_code == 404, r.status_code)

print("\nwithout ?raw=1 a tab gets the host page, never the page itself")
V = "/api/triage/threads/%s/attachments/%s/view" % (tid, att)
r = api.get(V, headers=AUTH)
check("the bare address answers the host page: a sandboxed frame onto ?raw=1 and nothing else",
      r.status_code == 200 and b'sandbox="allow-scripts"' in r.content and b'src="view?raw=1"' in r.content
      and b"<script" not in r.content and HTML not in r.content, r.content[:200])
check("the host page says, outside the frame, not to type anything into it",
      b"Do not type anything into it" in r.content and r.content.index(b"Do not type") < r.content.index(b"<iframe"), r.content[:400])
check("the host page carries its own policy (frames only itself, no script, no network)",
      r.headers.get("content-security-policy") == preview.WRAPPER_CSP if preview else False, r.headers.get("content-security-policy"))
check("and that policy lets the host frame the page but run nothing itself", "frame-src 'self'" in r.headers.get("content-security-policy", "")
      and "script-src" not in r.headers.get("content-security-policy", "") and "default-src 'none'" in r.headers.get("content-security-policy", ""))
hostn = upload(tid, 'x"><img src=x onerror=alert(1)>.html', "text/html", b"<p>name test</p>").json()["att_id"]
r = api.get("/api/triage/threads/%s/attachments/%s/view" % (tid, hostn), headers=AUTH)
check("a file name cannot break out of the host page's markup", b"<img src=x" not in r.content and b"&lt;img src=x" in r.content, r.content[:300])
r = api.get(V + "?raw=1", headers={**AUTH, "Sec-Fetch-Dest": "document"})
check("?raw=1 opened as a top-level navigation still gets the host page", b'src="view?raw=1"' in r.content and HTML not in r.content, r.content[:120])
r = api.get(V + "?raw=1", headers={**AUTH, "Sec-Fetch-Dest": "iframe"})
check("?raw=1 in a frame gets the page", r.content == HTML and r.headers.get("content-security-policy") == preview.VIEW_CSP if preview else False)
r = api.get(V + "?raw=0", headers=AUTH)
check("anything but raw=1 is the host page", b'src="view?raw=1"' in r.content)

d_att = upload(tid, "change.patch", "text/x-diff", DIFF).json()["att_id"]
r = api.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, d_att), headers=AUTH)
check("a diff is not served as a page: 415", r.status_code == 415, r.status_code)
p_att = upload(tid, "shot.png", "image/png", PNG).json()["att_id"]
r = api.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, p_att), headers=AUTH)
check("an image is not served as a page: 415", r.status_code == 415, r.status_code)
t_att = upload(tid, "sneaky.html", "text/plain", b"<script>alert(1)</script>").json()["att_id"]
r = api.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, t_att), headers=AUTH)
check("a .html name over text/plain stays text: 415", r.status_code == 415, r.status_code)

# ---------------------------------------------------------------- the web role
print("\nthe web role passes the route and holds the same policy itself")
r = webc.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, att), headers=WEB)
check("the screens can open it (200, the page's bytes)", r.status_code == 200 and r.content == HTML, (r.status_code, r.content[:60]))
check("the web role sends the identical policy", r.headers.get("content-security-policy") == csp, r.headers.get("content-security-policy"))
check("and nosniff, no referrer, no cache", r.headers.get("x-content-type-options") == "nosniff"
      and r.headers.get("referrer-policy") == "no-referrer" and "no-store" in r.headers.get("cache-control", ""), r.headers)
check("it is not the plain /att sandbox (that would let the page do nothing but also hide the policy)",
      r.headers.get("content-security-policy") != "sandbox")
r = webc.get("/api/triage/threads/%s/attachments/%s/view" % (tid, att), headers=WEB)
check("the web role's bare address is the host page under the host's own policy", b'src="view?raw=1"' in r.content and HTML not in r.content
      and r.headers.get("content-security-policy") == preview.WRAPPER_CSP, (r.headers.get("content-security-policy"), r.content[:80]))
r = webc.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, att), headers={**WEB, "Sec-Fetch-Dest": "document"})
check("a top-level navigation to ?raw=1 through the web role is the host page, with the host's policy",
      HTML not in r.content and r.headers.get("content-security-policy") == preview.WRAPPER_CSP, r.headers.get("content-security-policy"))
r = webc.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, att), headers={**WEB, "Sec-Fetch-Dest": "iframe"})
check("a frame's ?raw=1 through the web role is the page under the page policy", r.content == HTML and r.headers.get("content-security-policy") == csp)
r = webc.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, att))
check("no login header -> 401 and still no script rights", r.status_code == 401, r.status_code)
r = webc.get("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, p_att), headers=WEB)
check("a refusal on the route carries the host's policy too (nothing to run, nothing to load)", r.status_code == 415 and r.headers.get("content-security-policy") == preview.WRAPPER_CSP,
      (r.status_code, r.headers.get("content-security-policy")))
r = webc.post("/api/triage/threads/%s/attachments/%s/view?raw=1" % (tid, att), headers=WEB, json={})
check("a write to the route is 404, never relayed", r.status_code == 404, r.status_code)
r = webc.get("/api/triage/threads/%s/attachments/%s" % (tid, att), headers=WEB)
check("the plain attachment route still leaves sandboxed", r.headers.get("content-security-policy") == "sandbox", r.headers)

# ---------------------------------------------------------------- the upload policy
print("\nan agent's upload has a type allowlist, caps and a credential tripwire; the owner's do not")
tid2 = put_thread("t-rp-2")
for mime, name in [("text/html", "a.html"), ("text/plain", "a.txt"), ("text/markdown", "a.md"), ("text/x-diff", "a.diff"),
                   ("text/x-patch", "a.patch"), ("text/csv", "a.csv"), ("application/json", "a.json"),
                   ("application/pdf", "a.pdf"), ("image/png", "a.png"), ("image/jpeg", "a.jpg"), ("image/gif", "a.gif"),
                   ("image/webp", "a.webp"), ("image/svg+xml", "a.svg")]:
    r = upload(tid2, name, mime, ("body of " + name).encode())
    check("allowed for an agent: %s" % mime, r.status_code == 201, (r.status_code, r.text[:160]))
for mime, name in [("application/zip", "a.zip"), ("application/x-msdownload", "a.exe"), ("application/octet-stream", "a.bin"),
                   ("text/javascript", "a.js"), ("application/xml", "a.xml"), ("video/mp4", "a.mp4")]:
    r = upload(tid2, name, mime, ("body of " + name).encode())
    check("refused for an agent: %s (400, says which types)" % mime, r.status_code == 400 and "allowed" in r.text, (r.status_code, r.text[:200]))
r = upload(tid2, "inv.zip", "application/zip", b"PK-not-really", uploader="owner")
check("the owner's own file of any type still uploads (the share sheet, a pasted invoice)", r.status_code == 201, (r.status_code, r.text[:160]))
r = api.post("/api/triage/threads/%s/attach" % tid2, json={"filename": "n.zip", "mime": "application/zip",
                                                         "content_base64": base64.b64encode(b"PK-2").decode()}, headers=AUTH)
check("a file that names no uploader is an agent's and is held to the allowlist", r.status_code == 400, (r.status_code, r.text[:160]))
r = api.post("/api/triage/threads/%s/attach" % tid2, json={"files": [
    {"filename": "ok.png", "mime": "image/png", "content_base64": base64.b64encode(PNG + b"1").decode()},
    {"filename": "bad.zip", "mime": "application/zip", "content_base64": base64.b64encode(b"PK-3").decode()}],
    "uploader": "agent"}, headers=AUTH)
check("the share-sheet shape (files[]) is held to it too when the uploader is an agent", r.status_code == 400, (r.status_code, r.text[:160]))

print("\ncredential tripwire (text types only; the value is never echoed)")
tid3 = put_thread("t-rp-3")
leaky = b"<p>ok</p>\n<pre>\npassword = hunter2-hunter2-hunter2\n</pre>\n"
r = upload(tid3, "leak.html", "text/html", leaky)
check("an agent's HTML with a credential-shaped line is refused (400)", r.status_code == 400 and "credential" in r.text, (r.status_code, r.text[:200]))
check("the refusal names the line and the shape, never the value", "line 3" in r.text and "hunter2" not in r.text, r.text[:300])
check("nothing was stored for it", api.get("/api/triage/threads/%s/attachments" % tid3, headers=AUTH).json()["attachments"] == [])
r = upload(tid3, "leak.diff", "text/x-diff", b"+token = abcdefgh12345678\n")
check("a diff that adds a token line is refused", r.status_code == 400, (r.status_code, r.text[:160]))
r = upload(tid3, "placeholder.html", "text/html", b"<p>ok</p>\npassword = <MEDIA_TOKEN>\n")
check("a documented placeholder is not a credential", r.status_code == 201, (r.status_code, r.text[:200]))
FP = [
    ("a login-form page (the word password, no value)", b"<label>Password: required, 8+ chars</label>\n<th>Secret: rotated</th>\n<p>Token: invalid_grant</p>\n"),
    ("a diff that reads a token from code", b"+ token = request.headers.get('Authorization')\n+const apiKey = process.env.API_KEY\n+  password = os.environ['DB_PASSWORD']\n"),
    ("function and identifier names", b"+ password = generate_password_hash(raw)\n+ token = generateRandomToken();\n+ token = get_access_token(request)\ntoken = refresh_token_value_result\ntoken: color-primary-500\n"),
    ("a CSS class that starts sk-", b".sk-circle-wrapper-animation-long { color: red }\n"),
    ("a placeholder and an env var name", b"password = $DB_PASSWORD\ntoken = <YOUR_TOKEN>\nsecret: ${SECRET}\napi_key = REDACTED_VALUE\n"),
    ("a sentence about tokens", b"The session token expires after one hour; the secret is rotated nightly.\n"),
    ("message strings", b'"password": "Password is required"\npassword: "Please enter your password"\nmsg.password = "Wrong password, try again"\ntoken: "expires in 1 hour"\nsecret = "see the vault"\ntoken = "<redacted>"\nsecret: "i18n.errors.required"\n'),
    ("calls with digits and dates", b"token = getToken(123456)\ntoken = fetchToken(user123)\nconst secret = crypto.randomBytes(32).toString('hex')\nsecret: 2024-10-14\ntoken: 20241014\n")]
TP = [
    ("a private key header", b"-----BEGIN RSA " + b"PRIVATE KEY-----\nabc\n"),
    ("an AWS-shaped key id", b"id: AKIA" + b"ABCDEFGHIJKLMNOP" + b"\n"),
    ("an sk- API key", b"key sk-" + b"a1b2c3d4e5f6g7h8i9j0" + b"\n"),
    ("a URL with a password", b"see https://user:s3cretpw@example.com/x\n"),
    ("a bearer token", b"Authorization: Bearer " + b"abcdef0123456789abcdef0123" + b"\n"),
    ("a bearer token of letters only", b"Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n"),
    ("a JWT after token =", b"token = eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhYmNkZWZnaCJ9.abcdefghijklmnopqrstu\n"),
    ("a short password with a digit", b"password = hunter2\n"),
    ("a password with a symbol and a digit, then a semicolon", b"Password=P@ssw0rd!;\n"),
    ("a connection string", b"Server=db;User=sa;Password=abc123;\n"),
    ("a short token with a digit", b"token=abcd1234\n"),
    ("an access_key", b"access_key: AbCdEf0123456789xyzQWE\n"),
    ("a quoted password with brackets", b'password = "p@ss(word)123"\n'),
    ("a passphrase", b"passphrase: tr0ub4dor99\n"),
    ("a quoted dotted password", b'password = "Summer.2024"\nTOKEN="ya29.a0AfH6SMBx1234567890abcdef"\n'),
    ("an all-digit password", b"password=123456\n"),
    ("a prefixed name", b"DB_PASSWORD=Sup3rS3cret\naws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCY\n"),
    ("an OAuth-shaped token behind a dotted prefix", b"TOKEN=ya29.a0AfH6SMBx1234567890abcdef\n"),
    ("a password with a digit, 10+ characters", b"password: tr0ub4dor99\n")]
for i, (label, body) in enumerate(FP):
    r = upload(tid3, "fp-%d.html" % i, "text/html", body)
    check("not a credential: " + label, r.status_code == 201, (r.status_code, r.text[:200]))
for i, (label, body) in enumerate(TP):
    r = upload(tid3, "tp-%d.html" % i, "text/html", body)
    check("a credential: " + label, r.status_code == 400 and "credential" in r.text, (r.status_code, r.text[:200]))
    check("  ...and as a diff line", upload(tid3, "tp-%d.diff" % i, "text/x-diff", b"+" + body).status_code == 400)
import time as _time
for body in (b"eyJa-" * 100000, b"eyJaaaaaaaa-" * 40000, b"sk-" * 100000, b"Bearer " * 50000, b"pwd=" * 100000, b"password=" * 50000, b"token=" * 80000):
    t0 = _time.time()
    preview.secret_line(body)
    check("no backtracking blow-up on %d bytes of %r (%.2f s)" % (len(body), body[:9], _time.time() - t0), _time.time() - t0 < 3.0, _time.time() - t0)
# the retry of held bytes is not scanned again
held = upload(tid3, "held.txt", "text/plain", b"password: tr0ub4dor99\n", uploader="owner")
r = upload(tid3, "held-again.txt", "text/plain", b"password: tr0ub4dor99\n")
check("an agent re-sending bytes the thread already holds (the owner's) is a duplicate, not a refusal", held.status_code == 201 and r.status_code == 201 and r.json().get("duplicate") is True, (r.status_code, r.text[:160]))
r = upload(tid3, "leak.png", "image/png", PNG + b"\npassword = hunter2-hunter2\n")
check("binary types are not scanned as text", r.status_code == 201, (r.status_code, r.text[:200]))
r = upload(tid3, "owner-leak.txt", "text/plain", b"password = hunter2-hunter2\n", uploader="owner")
check("the owner's own text file is never scanned", r.status_code == 201, (r.status_code, r.text[:200]))

r = upload("t-does-not-exist", "a.zip", "application/zip", b"PK")
check("a missing thread is a 404 even when the file would also be refused", r.status_code == 404, (r.status_code, r.text[:160]))
r = api.post("/api/triage/threads/t-does-not-exist/attach", json={"files": [{"filename": "a.png", "mime": "image/png", "content_base64": base64.b64encode(PNG).decode()}], "uploader": "agent"}, headers=AUTH)
check("so is a files[] batch to a missing thread (never a 500)", r.status_code == 404, (r.status_code, r.text[:160]))

print("\ncaps (lowered for the test; the shipped values are in helm/preview.py)")
saved = (preview.AGENT_MAX_FILE, preview.AGENT_MAX_FILES, preview.AGENT_MAX_BYTES) if preview else None
if preview:
    preview.AGENT_MAX_FILE, preview.AGENT_MAX_FILES, preview.AGENT_MAX_BYTES = 2000, 3, 4500
    try:
        tid4 = put_thread("t-rp-4")
        r = upload(tid4, "big.html", "text/html", b"<p>" + b"x" * 2100)
        check("a file over the per-file cap is refused (400, says the size and the cap)", r.status_code == 400 and "limit" in r.text, (r.status_code, r.text[:200]))
        a = upload(tid4, "one.html", "text/html", b"<p>" + b"1" * 1800)
        b = upload(tid4, "two.html", "text/html", b"<p>" + b"2" * 1800)
        check("two files within the caps are accepted", a.status_code == 201 and b.status_code == 201, (a.status_code, b.status_code))
        r = upload(tid4, "three.html", "text/html", b"<p>" + b"3" * 1800)
        check("the one that takes the thread over its byte cap is refused (400)", r.status_code == 400 and "thread" in r.text, (r.status_code, r.text[:200]))
        r = upload(tid4, "one-again.html", "text/html", b"<p>" + b"1" * 1800)
        check("the same bytes again are a retry, not a second file: accepted, not counted", r.status_code == 201 and r.json().get("duplicate") is True, (r.status_code, r.text[:200]))
        c1 = upload(tid4, "c.txt", "text/plain", b"c")
        c2 = upload(tid4, "d.txt", "text/plain", b"d")
        check("small files fill the count (3 files in all)", c1.status_code == 201 and c2.status_code == 400, (c1.status_code, c2.status_code, c2.text[:160]))
        check("and the 4th is refused for the count", "files" in c2.text, c2.text[:200])
        r = upload(tid4, "owner-big.txt", "text/plain", b"j" * 2500, uploader="owner")
        check("the owner's own file is exempt from every one of these caps", r.status_code == 201, (r.status_code, r.text[:200]))
        r = upload(tid4, "shot.png", "image/png", PNG, uploader="owner")
        check("and does not use up the agent's room", r.status_code == 201)
    finally:
        preview.AGENT_MAX_FILE, preview.AGENT_MAX_FILES, preview.AGENT_MAX_BYTES = saved
    check("shipped defaults: 8 MB a file, 100 files and 128 MB a thread",
          (preview.AGENT_MAX_FILE, preview.AGENT_MAX_FILES, preview.AGENT_MAX_BYTES) == (8 * 1024 * 1024, 100, 128 * 1024 * 1024),
          (preview.AGENT_MAX_FILE, preview.AGENT_MAX_FILES, preview.AGENT_MAX_BYTES))
    check("one policy string is shared by the api role and the web role", svc.VIEW_CSP is preview.VIEW_CSP and web.VIEW_CSP is preview.VIEW_CSP
          if hasattr(svc, "VIEW_CSP") and hasattr(web, "VIEW_CSP") else False)

print("\nthe card's timeline can point at the file (conversation event carries att_id)")
conv = api.get("/api/triage/threads/%s" % tid, headers=AUTH).json()
from helm import conversation as _conv  # noqa: E402
evs = [e for e in _conv.helm_events(conv.get("thread") or conv) if e.get("kind") == "attachment"]
check("an attachment event carries the att_id of its file", evs and all(e.get("att_id") for e in evs), evs[:1])
check("and it is the uploaded file's id", evs and evs[0]["att_id"] in {att, d_att, p_att, t_att}, evs[:1])

print("%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
