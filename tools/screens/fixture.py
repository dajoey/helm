"""Synthetic data for the screens, and the two roles running on loopback.

    python3 tools/screens/fixture.py            # up; prints the URLs; Ctrl-C stops
    python3 tools/screens/fixture.py --verbose  # the roles' own output on the console

Builds a throwaway threads DB (needs-you choice / ack / text at the three urgencies with
context and attachments, working, parked with a recheck, snoozed, done, gate-held), a
dispatcher heartbeat and a synthetic notes folder, then starts the api role and the web role
on free ports. shoot.py drives it for screenshots and the suites import it for their own
instances. Nothing here touches a real database.

Every value in this file is fictional: the hosts are example.com, the addresses are 192.0.2.x
and the notes are about a made-up client called Pine Hollow.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
TOKEN = "fixture-token-not-a-secret"
PRESS_KEY = "fixture-press-key-not-a-secret"   # HELM_WEB_PRESS_KEY: the web role proves a signed-in press to the api
USER = "owner"
HEADERS = {"X-Forwarded-User": USER}   # a browser at the fixture's web role, as a proxy would send it


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def png(w=640, h=300):
    """A PNG that reads as a chart (bars on a dark panel), built without Pillow: the
    store's image attachment on the lit card."""
    bg, bar, axis = (22, 22, 30), (122, 162, 247), (59, 66, 97)
    bars = [0.35, 0.42, 0.5, 0.58, 0.66, 0.74, 0.8, 0.86, 0.94]
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            c = bg
            if y == h - 20 or x == 30:
                c = axis
            elif x > 40 and y < h - 20:
                i = (x - 40) // ((w - 60) // len(bars))
                if i < len(bars) and (x - 40) % ((w - 60) // len(bars)) > 8 and y > (h - 20) - bars[i] * (h - 40):
                    c = bar if i < len(bars) - 1 else (247, 118, 142)
            row += bytes(c)
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


# ---------------------------------------------------------------- the threads DB
def build_db(db_path: str, attach_dir: str, site: str = "home") -> None:
    """Runs in its own process."""
    sys.path.insert(0, ROOT)
    from helm import store
    con = store.db(db_path)
    now = time.time()
    hours = lambda h: now - h * 3600  # noqa: E731

    def put(t):
        out = store.put_threads(con, {"threads": [t]})
        if out.get("rejected"):
            raise SystemExit("fixture thread rejected: %s" % out["rejected"])

    def opt(label, value, effect, **kw):
        return dict({"label": label, "value": value, "effect": effect}, **kw)

    # NEEDS YOU · now · choice with context, attachments and a prior report
    put({"thread_id": "t-media-box-disk", "title": "Media box disk at 94%: prune the recordings or grow the volume?",
         "summary": "Needs you because: the nightly recorder will stop in about two days. The media box's data volume is 94% full; "
                    "the recordings folder holds 310 GB from March to now. Growing the volume means a short outage tonight.",
         "category": "Fleet", "urgency": "now", "state": "needs-you", "responder": "agent:worker",
         "ask": {"kind": "choice", "prompt": "Prune recordings older than 90 days, or grow the volume by 500 GB tonight?", "requires": ["decision"],
                 "context": "Pruning frees 180 GB now and keeps everything since June. Growing the volume keeps every recording and costs a 10 minute outage around 02:00.",
                 "options": [opt("Prune older than 90 days", "prune", "Deletes 180 GB of recordings from March to June tonight"),
                             opt("Grow the volume by 500 GB", "grow", "A 10 minute outage at 02:00, nothing deleted"),
                             opt("Leave it and ask me again Friday", "wait", "Nothing changes; the card comes back in three days")]},
         "sources": [{"source": "monitor", "id": "media-box-disk"}],
         "links": [{"label": "Pine Hollow runbook", "url": "note:Contacts/Pine Hollow portal"}],
         "events": [{"ts": hours(3), "source": "agent:worker", "kind": "bot-result",
                     "body": "Checked the volume: 310 GB of recordings, growth about 4 GB a day. Two workable options below; I did not delete anything."}]})
    for i, a in enumerate([("disk-usage.png", "image/png", png()), ("recorder.log", "text/plain", b"2026-09-25 01:00 recorder: 94% used\n" * 20)]):
        raw = a[2]
        import hashlib
        clean, errs = store.normalize_attachment({"filename": a[0], "mime": a[1], "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                                                  "uploader": "agent:worker", "caption": "Disk usage by month" if i == 0 else ""})
        if errs:
            raise SystemExit(errs)
        rec = store.add_attachment(con, "t-media-box-disk", clean)
        os.makedirs(os.path.join(attach_dir, "t-media-box-disk"), exist_ok=True)
        with open(os.path.join(attach_dir, "t-media-box-disk", rec["att_id"] + ".bin"), "wb") as f:
            f.write(raw)

    # NEEDS YOU · this week · ack after a report
    put({"thread_id": "t-backup-restore-check", "title": "Nightly backup restore test passed on the spare box",
         "summary": "The restore drill ran on the spare box: 41 GB restored in 12 minutes, checksums match. Nothing to do; read and close.",
         "category": "Backups", "urgency": "this-week", "state": "needs-you", "responder": "agent:worker",
         "ask": {"kind": "ack", "prompt": "Read the restore report and close it", "requires": ["decision"],
                 "options": [opt("Got it", "ack", "Closes the card")]},
         "sources": [{"source": "cron", "id": "backup-verify"}],
         "events": [{"ts": hours(20), "source": "agent:worker", "kind": "bot-result",
                     "body": "Restore drill finished: 41 GB, 12 minutes, 0 checksum mismatches. Log attached to the run."}]})

    # NEEDS YOU · whenever · text
    put({"thread_id": "t-portal-name", "title": "What should the portal's login page be called?",
         "summary": "The Pine Hollow portal brief needs a name for the login page before the mock-up goes out.",
         "category": "Projects", "urgency": "whenever", "state": "needs-you",
         "ask": {"kind": "text", "prompt": "What should the login page be called?", "requires": ["decision"]},
         "sources": [{"source": "notebook", "id": "Contacts/Pine Hollow portal"}],
         "links": [{"label": "the brief", "url": "note:Contacts/Pine Hollow portal"}]})

    # IN FLIGHT · working
    put({"thread_id": "t-monitor-dedupe", "title": "Dedupe the Monitor sweep's repeat alerts",
         "summary": "61 of the last 200 alerts were one flapping monitor. A 3 minute window keeps one alert per flap.",
         "category": "Fleet", "urgency": "this-week", "state": "working", "responder": "agent:worker",
         "ask": {"kind": "none"}, "sources": [{"source": "monitor", "id": "sweep-dedupe"}, {"source": "assistant", "id": "tasks-20260925-monitor-sweep-dedupe"}],
         "events": [{"ts": hours(1.5), "source": "owner", "kind": "owner-reply", "body": "three-minutes"},
                    {"ts": hours(1.4), "source": "agent:worker", "kind": "bot-result", "body": "Claimed. Editing the sweep's dedupe window and adding a test for the flap case."}]})

    # ON HOLD · waiting-external with a recheck
    put({"thread_id": "t-registrar-cert", "title": "Vendor certificate renewal: waiting on the registrar",
         "summary": "The registrar's ticket 00100001 is open; they said two business days.",
         "category": "Fleet", "urgency": "whenever", "state": "waiting-external",
         "blocked_on": "the registrar's support desk, ticket 00100001", "recheck_at": now + 86400 * 2,
         "ask": {"kind": "none"}, "sources": [{"source": "email", "id": "registrar-00100001"}]})

    # snoozed needs-you (hidden until it wakes)
    put({"thread_id": "t-wiki-tidy", "title": "Tidy the wiki's broken links before the month ends",
         "summary": "About 30 wikilinks point at pages that were renamed.", "category": "Notebook", "urgency": "whenever", "state": "needs-you",
         "ask": {"kind": "choice", "prompt": "Rewrite the links now, or leave them for the rename pass?", "requires": ["decision"],
                 "options": [opt("Rewrite them now", "now", "An agent rewrites the 30 links tonight"), opt("Leave them for the rename pass", "later", "Nothing changes")]},
         "sources": [{"source": "notebook", "id": "wiki-links"}]})
    store.snooze(con, "t-wiki-tidy", hours=24)

    # done
    for tid, title in (("t-digest-cron", "Fix the email digest cron leg"), ("t-parks-retry", "Retry storm on the parks pass")):
        put({"thread_id": tid, "title": title, "summary": "Fixed and verified.", "category": "Fleet", "urgency": "this-week", "state": "needs-you",
             "ask": {"kind": "ack", "prompt": "Read the fix report and close it", "requires": ["decision"], "options": [opt("Got it", "ack", "Closes the card")]},
             "sources": [{"source": "cron", "id": tid}],
             "events": [{"ts": hours(30), "source": "agent:worker", "kind": "bot-result", "body": "Fixed: the cron leg had lost its env file after the host move. Re-added and ran once by hand; the digest landed."}]})
        store.close_thread(con, tid, reason="read it", actor="owner")

    # A deliberate park: a card the shop waits on someone else for, with a recheck.
    put({"thread_id": "t-ticket-ph-14", "title": "T-14: portal mock-up review with the client",
         "summary": "The client reviews the mock-up at their next board meeting.", "category": "Projects", "urgency": "whenever",
         "state": "waiting-external", "blocked_on": "the client's October board meeting", "ask": {"kind": "none"},
         "sources": [{"source": "tracker", "id": "T-14"}]})
    store.set_parked(con, "t-ticket-ph-14", True,
                     "Nothing pushes this until the client's board meets in October; the mock-up is done and sent, so the wait is theirs, not ours.")

    # HELD BY GATE: the needs-owner gate holds a "work completed" ack inside a test cycle (tests/test_gate.py's flow)
    put({"thread_id": "t-lantern-grid-routing", "title": "Lantern grid routing fix",
         "summary": "A routing fix for the lantern grid add-on, shipped to the testing channel.", "category": "Hobby", "urgency": "this-week", "state": "needs-you",
         "ask": {"kind": "choice", "prompt": "Ship the routing fix to the testing channel, or hold it?", "requires": ["decision"],
                 "options": [opt("Ship it to testing", "ship", "Builds 0.1.46.0 and publishes it to the testing channel"), opt("Hold it", "hold", "Nothing ships")]},
         "sources": [{"source": "assistant", "id": "lantern-grid"}]})
    store.reply(con, "t-lantern-grid-routing", "ship")
    store.post_result(con, "t-lantern-grid-routing", {"outcome": "shipped", "state": "waiting-external",
                                                      "note": "Shipped lantern grid 0.1.46.0 to the testing channel for in-game verification; the publish job finished cleanly and the build is listed. The channel page shows 0.1.46.0 as current.",
                                                      "blocked_on": "the testing channel publish job"})
    store.post_result(con, "t-lantern-grid-routing", {"outcome": "reported", "state": "needs-you",
                                                      "note": "Work completed: routing fix done in code and pushed (0.1.46.0). The build is on the testing channel; nothing else is owed until it is verified in game. Tap to close.",
                                                      "ask": {"kind": "ack", "prompt": "Work completed: routing fix done in code and pushed (0.1.46.0). Tap to close."}})
    con.commit()
    con.close()


# ---------------------------------------------------------------- notes
NOTES = {
    "Contacts/Pine Hollow portal.md": "---\ntitle: Pine Hollow portal\nstatus: drafting\n---\n# Pine Hollow portal\n\nThe portal for Pine Hollow (a fictional company).\n\n- [ ] name the login page\n- [x] collect the mock-ups\n- [ ] send the brief to the board (ticket 00100001)\n\nSee [[Reference/Ticket numbers]] and the runbook at https://kb.example.com/portal.\n",
    "Reference/Ticket numbers.md": "# Ticket numbers\n\nFictional numbers used by the fixtures.\n\n| system | number |\n|---|---|\n| registrar | 00100001 |\n| case | 00100002 |\n\nLinks back to [[Contacts/Pine Hollow portal]].\n",
    "Reference/Route table.md": "# Route table\n\nFictional routes; the table shape that squished (short tokens beside long prose).\n\n| Hostname | Backend | Service |\n|---|---|---|\n| vault.example.com | 192.0.2.10:8280 | Password manager for the household, reachable from the LAN and over the tunnel; the admin page stays behind the second login and the nightly export runs from the same host |\n| notes.example.com | 192.0.2.10:3037 | The notebook web role, behind the single sign-on proxy; every request carries the forwarded user header or the gate refuses it with a 401 |\n| media.example.com | 192.0.2.20:8096 | Media server front end for the living room; transcoding is off on this route and the library scan runs at night |\n\nA wide one:\n\n| Container | Image digest | Started | Health endpoint | Volume path |\n|---|---|---|---|---|\n| helmapi | sha256:3f1c9a7e5b2d4c8a9e0f1b2c3d4e5f60 | 20261019T044300Z | http://192.0.2.10:3038/api/meta | /srv/docker/helm/data/notebook |\n| helmweb | sha256:9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c40 | 20261019T044305Z | http://192.0.2.10:3037/healthz | /srv/docker/helm/data/attachments |\n\nA small one:\n\n| key | value |\n|---|---|\n| owner | nobody |\n| state | fictional |\n",
    "Daily/2026/2026-09/2026-09-25.md":"# 2026-09-25\n\n- 09:10 registrar ticket 00100001 opened\n- [ ] call the client about T-14\n- read [[Contacts/Pine Hollow portal]]\n",
    "Daily/2026/2026-09/2026-09-24.md": "# 2026-09-24\n\n- [ ] send the brief\n- [x] restore drill on the spare box\n",
}


def build_notes(root: str) -> None:
    for rel, text in NOTES.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    os.makedirs(os.path.join(root, "attachments"), exist_ok=True)


# ---------------------------------------------------------------- processes
def wait_http(url: str, headers=None, timeout=20.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status < 500:
                    return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError("no answer from " + url)


class Site:
    def __init__(self, site: str = "home", base: str = "", notes_mode: str = ""):
        self.site = site
        # The notebook is the folder given, read-only, unless a drive asks for the store to
        # exercise the write screens.
        self.notes_mode = notes_mode or "folder-readonly"
        self.dir = os.path.join(base, site)
        os.makedirs(self.dir, exist_ok=True)
        self.db = os.path.join(self.dir, "triage.db")
        self.attach = os.path.join(self.dir, "attachments")
        self.notes_root = os.path.join(self.dir, "notes")
        self.notes_db = os.path.join(self.dir, "notes.db")
        self.hb = os.path.join(self.dir, "heartbeat.json")
        self.api_port = free_port()
        self.web_port = free_port()
        self.stub_port = free_port()
        self.stub_notebook = False    # set when the api role has no notebook routes yet
        self.procs = []
        # Role name -> (process, the environment handed to that spawn). A test process whose tree is
        # sealed (a worker's sandbox sets PR_SET_DUMPABLE=0 on the process, and every child inherits it) cannot
        # read /proc/<pid>/environ of any process in its own tree, and a test still has to say which
        # environment each role got. See `spawn`.
        self.roles = {}

    @property
    def web_url(self):
        return "http://127.0.0.1:%d" % self.web_port

    @property
    def api_url(self):
        return "http://127.0.0.1:%d" % self.api_port

    def build(self):
        os.makedirs(self.attach, exist_ok=True)
        subprocess.run([PY, os.path.abspath(__file__), "--build-db", self.db, self.attach, self.site], check=True, cwd=ROOT)
        build_notes(self.notes_root)
        with open(self.hb, "w") as f:
            json.dump({"ts": time.time() - 95, "host": "host-a", "runs_today": 4}, f)

    def spawn(self, role, args, **kw):
        """Start one role and remember the environment it was handed.

        The process is exactly the one `start` used to start (same Popen call, no wrapper, no extra
        child); the record is kept because a sealed test process cannot ask the kernel what a child
        got, and a test still needs a source for that. `role` is a name the test can ask for: "api",
        "notebook", "web".
        """
        p = subprocess.Popen(args, **kw)
        self.procs.append(p)
        self.roles[role] = (p, dict(kw.get("env") or {}))
        return p

    def start(self, quiet=True):
        out = None if not quiet else subprocess.DEVNULL
        env = dict(os.environ, HELM_APP_TOKEN=TOKEN, HELM_WEB_PRESS_KEY=PRESS_KEY, HELM_DB=self.db, HELM_ATTACH_DIR=self.attach,
                   HELM_DATA=self.dir, HELM_DISPATCHER_HB_FILE=self.hb, HELM_NOTES_ROOT=self.notes_root, HELM_NOTES_DB=self.notes_db,
                   HELM_NOTES_MODE=self.notes_mode, HELM_OWNER=USER, HELM_AUTH_HEADER="X-Forwarded-User",
                   HELM_TRUSTED_PROXIES="127.0.0.1/32", PYTHONPATH=ROOT)
        env.pop("HELM_SENTRY_DSN", None)
        env.pop("HELM_MODULES", None)
        self.spawn("api", [PY, "-m", "uvicorn", "app.service:app", "--host", "127.0.0.1", "--port", str(self.api_port), "--log-level", "warning"],
                   cwd=ROOT, env=env, stdout=out, stderr=out)
        wait_http(self.api_url + "/api/meta")
        # The notebook module is built in parallel. Until its routes answer, a stub that speaks
        # the notebook's shapes over the synthetic folder sits in front of the api (forwarding the
        # rest), so the notebook screens can be judged. shoot.py labels those shots.
        api_for_web = self.api_url
        if os.environ.get("HELM_SCREENS_STUB_NOTEBOOK", "auto") != "0":
            try:
                req = urllib.request.Request(self.api_url + "/api/notes/folders", headers={"Authorization": "Bearer " + TOKEN})
                with urllib.request.urlopen(req, timeout=3) as r:
                    has_notebook = r.status == 200
            except urllib.error.HTTPError as e:
                has_notebook = e.code != 404
            except Exception:
                has_notebook = False
            if not has_notebook or os.environ.get("HELM_SCREENS_STUB_NOTEBOOK") == "1":
                mode = self.notes_mode
                self.spawn("notebook", [PY, os.path.join(ROOT, "tools", "screens", "fake_notebook.py"), str(self.stub_port), self.api_url, self.notes_root, mode],
                           cwd=ROOT, env=dict(os.environ, PYTHONPATH=ROOT), stdout=out, stderr=out)
                api_for_web = "http://127.0.0.1:%d" % self.stub_port
                self.stub_notebook = True
                wait_http(api_for_web + "/api/notes/meta")
        wenv = dict(env, HELM_ROLE="web", HELM_PORT=str(self.web_port), HELM_API_URL=api_for_web,
                    HELM_WEB_REQUIRE_AUTH_HEADER="1", HELM_TRUSTED_PROXIES="127.0.0.1/32")
        self.spawn("web", [PY, "-m", "uvicorn", "app.web:app", "--host", "127.0.0.1", "--port", str(self.web_port), "--log-level", "warning"],
                   cwd=ROOT, env=wenv, stdout=out, stderr=out)
        wait_http(self.web_url + "/api/meta", {"X-Forwarded-User": USER})

    def stop(self):
        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        self.procs = []


class Stack:
    """One deployment, built and running. `with Stack() as st: st.home.web_url ...`"""

    def __init__(self, sites=("home",), quiet=True, notes_modes=None, **_ignored):
        self.base = tempfile.mkdtemp(prefix="helm-screens-")
        self.sites = {s: Site(s, self.base, (notes_modes or {}).get(s, "")) for s in sites}
        self.quiet = quiet

    def __enter__(self):
        try:
            for s in self.sites.values():
                s.build()
                s.start(self.quiet)
        except BaseException:
            # __exit__ never runs when __enter__ raises: stop what did start, drop the dir.
            self.__exit__()
            raise
        return self

    def __exit__(self, *a):
        try:
            for s in self.sites.values():
                s.stop()
        finally:
            # Only once the processes are down: they hold the SQLite files open until then.
            shutil.rmtree(self.base, ignore_errors=True)

    @property
    def home(self):
        return self.sites.get("home")


def main(argv):
    if len(argv) >= 4 and argv[0] == "--build-db":
        build_db(argv[1], argv[2], argv[3])
        return 0
    with Stack(("home",), quiet="--verbose" not in argv) as st:
        s = st.home
        print("web %s  api %s  (X-Forwarded-User: %s; token %s)%s" % (s.web_url, s.api_url, USER, TOKEN,
              "  [notebook routes stubbed by fake_notebook.py]" if s.stub_notebook else ""))
        print("Ctrl-C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
