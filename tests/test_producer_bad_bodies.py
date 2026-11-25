"""Producer, dispatcher, lesson, claim, settings and label routes: a bad body is a 400, never a 500.

Invalid JSON, invalid UTF-8, a body that is not an object, bad base64 and a multipart body
without a file answer 400 (create_thread, the action routes and snooze already did for the
first two). A 500 here pages the error tracker for what is the caller's mistake and loses the payload.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

os.environ["HELM_APP_TOKEN"] = "test-token"  # force: a persisted real token must never leak into tests
TMP = tempfile.mkdtemp(prefix="helm-prodbad-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")

import app.service as svc

client = TestClient(svc.app, raise_server_exceptions=False)
AUTH = {"Authorization": "Bearer test-token"}
JSON_H = dict(AUTH, **{"Content-Type": "application/json"})

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")

T = "pbb-1"
C = "c-none"
# (method, path, body required?) -- required: an empty body is a 400 too; optional: an empty body means {}
ROUTES = [
    ("PUT", "/api/triage/threads", True),
    ("POST", "/api/triage/threads/create", True),
    ("POST", "/api/triage/threads/new", True),
    ("POST", "/api/triage/threads/%s/reply" % T, True),
    ("POST", "/api/triage/threads/%s/close" % T, False),
    ("POST", "/api/triage/threads/%s/reopen" % T, False),
    ("POST", "/api/triage/threads/%s/mute" % T, False),
    ("POST", "/api/triage/threads/%s/park" % T, False),
    ("POST", "/api/triage/threads/%s/unpark" % T, False),
    ("POST", "/api/triage/threads/%s/backlog" % T, False),
    ("POST", "/api/triage/threads/%s/unbacklog" % T, False),
    ("POST", "/api/triage/threads/%s/wake" % T, False),
    ("POST", "/api/triage/threads/%s/snooze" % T, False),
    ("POST", "/api/triage/threads/%s/retry" % T, False),
    ("POST", "/api/triage/threads/%s/recheck" % T, False),
    ("POST", "/api/triage/threads/%s/result" % T, True),
    ("POST", "/api/triage/threads/%s/note" % T, True),
    ("POST", "/api/triage/threads/%s/attach" % T, True),
    ("POST", "/api/triage/threads/%s/dispatched" % T, True),
    ("POST", "/api/triage/threads/%s/claims" % T, True),
    ("POST", "/api/triage/threads/%s/claims/%s/result" % (T, C), True),
    ("POST", "/api/triage/threads/%s/claims/%s/release" % (T, C), False),
    ("POST", "/api/triage/threads/%s/label" % T, True),
    ("POST", "/api/triage/dispatcher-heartbeat", True),
    ("POST", "/api/triage/needs-owner-check", True),
    ("POST", "/api/triage/gave-up/restore", False),
    ("POST", "/api/triage/lessons", True),
    ("POST", "/api/triage/lessons/l1/retire", True),
    ("POST", "/api/triage/lessons/l1/supersede", True),
    ("POST", "/api/triage/lessons/l1/gate-implemented", True),
    ("POST", "/api/triage/lessons/from-thread/%s" % T, False),
    ("POST", "/api/triage/resort", False),
    ("POST", "/api/triage/settings", True),
    ("POST", "/api/triage/labels", True),
]

BAD = [
    ("invalid json", b"{bad"),
    ("truncated json", b"{"),
    ("invalid utf-8", b'{"a": "\xff\xfe"}'),
    ("array", b"[]"),
    ("string", b'"x"'),
    ("number", b"7"),
    ("null", b"null"),
]

for method, path, required in ROUTES:
    for label, raw in BAD:
        r = client.request(method, path, content=raw, headers=JSON_H)
        check("%s %s %s is 400" % (method, path, label), r.status_code == 400,
              "got %s %s" % (r.status_code, r.text[:120]))
        if r.status_code == 400:
            try:
                check("%s %s %s names a detail" % (method, path, label),
                      bool(r.json().get("detail")), r.text[:120])
            except ValueError:
                check("%s %s %s answers JSON" % (method, path, label), False, r.text[:120])
    if required:
        r = client.request(method, path, content=b"", headers=JSON_H)
        check("%s %s empty body is 400" % (method, path), r.status_code == 400,
              "got %s %s" % (r.status_code, r.text[:120]))

# attach: invalid base64, a non-string payload, a files entry that is not base64, multipart without a file
ATT = "/api/triage/threads/%s/attach" % T
for label, body in [
    ("bad base64", {"filename": "a.txt", "mime": "text/plain", "content_base64": "@@@not-base64@@@"}),
    ("non-string base64", {"filename": "a.txt", "mime": "text/plain", "content_base64": 12345}),
    ("files entry bad base64", {"files": [{"filename": "a.txt", "mime": "text/plain", "content_base64": "@@@"}]}),
    ("files not objects", {"files": ["x"]}),
]:
    r = client.post(ATT, json=body, headers=AUTH)
    check("attach %s is 400" % label, r.status_code == 400, "got %s %s" % (r.status_code, r.text[:120]))

r = client.post(ATT, data={"uploader": "x"}, files={"other": ("a.txt", b"hi", "text/plain")}, headers=AUTH)
check("attach multipart without file is 400", r.status_code == 400, "got %s %s" % (r.status_code, r.text[:120]))
r = client.post(ATT, data={"file": "just text"}, headers=AUTH, files={"other": ("a.txt", b"hi", "text/plain")})
check("attach multipart with file as text is 400", r.status_code == 400, "got %s %s" % (r.status_code, r.text[:120]))
r = client.post(ATT, content=b"--x\r\nbroken", headers=dict(AUTH, **{"Content-Type": "multipart/form-data; boundary=x"}))
check("attach malformed multipart is 400", r.status_code == 400, "got %s %s" % (r.status_code, r.text[:120]))

# good bodies keep their answers
seed = {"threads": [{
    "thread_id": T, "title": "Feed the marmot colony schedule", "summary": "Rota for the marmot enclosure keepers",
    "category": "Test", "urgency": "this-week", "state": "needs-you",
    "ask": {"kind": "choice", "prompt": "Which marmot rota do we run", "requires": ["decision"],
            "context": "The marmot enclosure keepers need a rota before the first of the month and nobody has picked one.",
            "options": [
                {"label": "Renew the cert and rotate the key", "value": "do", "effect": "It gets done"},
                {"label": "Leave it alone for now", "value": "no", "effect": "Nothing happens"},
            ]},
    "sources": [{"source": "test", "id": T, "note": ""}],
}]}
r = client.put("/api/triage/threads", json=seed, headers=AUTH)
check("good PUT threads is 200", r.status_code == 200, "%s %s" % (r.status_code, r.text[:160]))
r = client.post("/api/triage/threads/%s/note" % T, json={"note": "hello"}, headers=AUTH)
check("good note is 202", r.status_code == 202, "%s %s" % (r.status_code, r.text[:160]))
r = client.post("/api/triage/threads/%s/attach" % T, json={"filename": "a.txt", "mime": "text/plain",
                "content_base64": "aGk="}, headers=AUTH)
check("good attach is 201", r.status_code == 201, "%s %s" % (r.status_code, r.text[:160]))
r = client.post("/api/triage/threads/%s/attach" % T, files={"file": ("b.txt", b"hi", "text/plain")}, headers=AUTH)
check("good multipart attach is 201", r.status_code == 201, "%s %s" % (r.status_code, r.text[:160]))
r = client.post("/api/triage/gave-up/restore", content=b"", headers=JSON_H)
check("restore with an empty body is 202", r.status_code == 202, "%s %s" % (r.status_code, r.text[:160]))
r = client.post("/api/triage/resort", content=b"", headers=JSON_H)
check("resort with an empty body is 202", r.status_code == 202, "%s %s" % (r.status_code, r.text[:160]))
r = client.post("/api/triage/threads/%s/claims" % T, json={"executor_id": "exec-1", "runtime": "agent"}, headers=AUTH)
check("good claim is 201", r.status_code == 201, "%s %s" % (r.status_code, r.text[:160]))

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
