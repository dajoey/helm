"""Files on an answer, and how any thread attachment is served (ledger GW-067, GW-086, GW-108, GW-075).

Run: python3 tests/test_reply_files.py

A file the owner sends with a tap (POST .../reply with inline attachments, the conversation's
composer on a lit card) is stored: its bytes are written beside the record, so the agent
can open it and the screens can show it, and it is the owner's (a owner-attachment event).
Every thread attachment is served sandboxed and never sniffed; an active type (HTML, SVG,
XML, script) downloads instead of rendering, at the api and through the web role.
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

os.environ["HELM_APP_TOKEN"] = "test-token"
os.environ.pop("HELM_MODULES", None)
TMP = tempfile.mkdtemp(prefix="helm-files-")
os.environ["HELM_DB"] = os.path.join(TMP, "t.db")
os.environ["HELM_ATTACH_DIR"] = os.path.join(TMP, "att")

import app.service as svc  # noqa: E402
import app.web as web  # noqa: E402
from helm import store  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {name} {detail}")


client = TestClient(svc.app)
AUTH = {"Authorization": "Bearer test-token"}
con = store.db(os.environ["HELM_DB"])
store.put_threads(con, {"threads": [{"thread_id": "t-f", "title": "Which disk is it?", "summary": "Send a screenshot", "category": "Fleet",
                                     "urgency": "this-week", "state": "needs-you", "sources": [{"source": "test", "id": "f"}],
                                     "ask": {"kind": "choice", "prompt": "Which disk is full?", "requires": ["decision"],
                                             "options": [{"label": "The media disk", "value": "media", "effect": "The agent prunes media"},
                                                         {"label": "The backup disk", "value": "backup", "effect": "The agent prunes backups"}]}}]})
con.close()
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64

print("a file on an answer is stored")
r = client.post("/api/triage/threads/t-f/reply", headers={**AUTH, "X-Helm-Actor": "owner"},
                json={"value": "media", "attachments": [{"filename": "df.png", "mime": "image/png", "content_base64": base64.b64encode(PNG).decode()}]})
check("the reply is accepted", r.status_code == 202, r.text[:200])
atts = client.get("/api/triage/threads/t-f/attachments", headers=AUTH).json()["attachments"]
check("the attachment is recorded", len(atts) == 1 and atts[0]["filename"] == "df.png", atts)
if atts:
    g = client.get("/api/triage/threads/t-f/attachments/%s" % atts[0]["att_id"], headers=AUTH)
    check("its bytes were written and are served back", g.status_code == 200 and g.content == PNG, (g.status_code, g.content[:20]))
    check("it is the owner's file", atts[0].get("uploader") == "owner", atts[0].get("uploader"))
t = client.get("/api/triage/threads/t-f", headers=AUTH).json()
check("the thread says the owner attached it", any(e["kind"] == "owner-attachment" for e in t["events"]), [e["kind"] for e in t["events"]])

print("\nattachments are served sandboxed")
HTML = b"<script>fetch('/api/triage/resort',{method:'POST'})</script>"
r = client.post("/api/triage/threads/t-f/attach", headers=AUTH,
                json={"filename": "page.html", "mime": "text/html", "uploader": "agent:worker", "content_base64": base64.b64encode(HTML).decode()})
check("an agent may attach an HTML file", r.status_code in (200, 201), r.text[:200])
atts = {a["filename"]: a for a in client.get("/api/triage/threads/t-f/attachments", headers=AUTH).json()["attachments"]}
g = client.get("/api/triage/threads/t-f/attachments/%s" % atts["page.html"]["att_id"], headers=AUTH)
check("an active type downloads, never renders", g.headers.get("content-type", "").startswith("application/octet-stream")
      and "attachment" in g.headers.get("content-disposition", ""), dict(g.headers))
check("the api sandboxes it and forbids sniffing", g.headers.get("content-security-policy") == "sandbox" and g.headers.get("x-content-type-options") == "nosniff", dict(g.headers))
g = client.get("/api/triage/threads/t-f/attachments/%s" % atts["df.png"]["att_id"], headers=AUTH)
check("an image still shows inline, sandboxed", g.headers.get("content-type") == "image/png" and "attachment" not in g.headers.get("content-disposition", "")
      and g.headers.get("content-security-policy") == "sandbox", dict(g.headers))

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
client.post("/api/triage/threads/t-f/attach", headers=AUTH, json={"filename": "chart.svg", "mime": "image/svg+xml", "uploader": "helm-card-visual",
                                                                   "content_base64": base64.b64encode(SVG).decode()})
atts = {a["filename"]: a for a in client.get("/api/triage/threads/t-f/attachments", headers=AUTH).json()["attachments"]}
g = client.get("/api/triage/threads/t-f/attachments/%s" % atts["chart.svg"]["att_id"], headers=AUTH)
check("an SVG card visual stays an image (the screens draw it), sandboxed", g.headers.get("content-type", "").startswith("image/svg+xml")
      and "attachment" not in g.headers.get("content-disposition", "") and g.headers.get("content-security-policy") == "sandbox", dict(g.headers))

client.post("/api/triage/threads/t-f/attach", headers=AUTH, json={"filename": "fake.svg", "mime": "text/html", "uploader": "agent:worker",
                                                                   "content_base64": base64.b64encode(HTML + b"<!-- another file -->").decode()})
atts = {a["filename"]: a for a in client.get("/api/triage/threads/t-f/attachments", headers=AUTH).json()["attachments"]}
g = client.get("/api/triage/threads/t-f/attachments/%s" % atts["fake.svg"]["att_id"], headers=AUTH)
check("a name ending .svg over an HTML type is HTML: it downloads", "attachment" in g.headers.get("content-disposition", "")
      and g.headers.get("content-type", "").startswith("application/octet-stream"), dict(g.headers))

print("\nthrough the web role")
web.UPSTREAM_TRANSPORT = httpx.ASGITransport(app=svc.app)
web.TOKEN = "test-token"
wc = TestClient(web.app, client=("127.0.0.1", 40000))
g = wc.get("/api/triage/threads/t-f/attachments/%s" % atts["page.html"]["att_id"], headers={"X-Forwarded-User": "owner"})
check("the web role serves a thread attachment sandboxed and never sniffed", g.status_code == 200 and g.headers.get("content-security-policy") == "sandbox"
      and g.headers.get("x-content-type-options") == "nosniff", (g.status_code, dict(g.headers)))
check("...and an active type still downloads", "attachment" in g.headers.get("content-disposition", ""), dict(g.headers))

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
