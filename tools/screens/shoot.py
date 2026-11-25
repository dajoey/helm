"""Screenshots of every screen at 999x1392 (a desktop window) and 412x915 (phone), from
synthetic data, into docs/screens/<module>-<WxH>.png.

    python3 tools/screens/shoot.py [--only needs-you,threads] [--out DIR]

fixture.py brings the deployment up on loopback; each shot gets a fresh browser context with
X-Forwarded-User set (the web role refuses without it), waits for network idle, and
saves the PNG. Every shot uses the one fixed Helm design. Notebook shots are taken only when
the notebook routes answer; otherwise they are listed as skipped.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture  # noqa: E402

ROOT = fixture.ROOT
OUT = os.path.join(ROOT, "docs", "screens")
SIZES = [(999, 1392), (412, 915)]
HEADERS = fixture.HEADERS

# (module, hash route, options). `notebook` shots need the notebook routes. `type` fills a
# box before the shot (a note being written on a done thread, §4); `click` taps; `key` presses.
SHOTS = [
    # the three-pane main screen: NEEDS YOU opens and its first ask is in the middle pane (L1)
    ("needs-you", "#/needs-you", {"wait": ".conv-live-ask .conv-opt", "wait_phone": "#list-needs-you .lrow"}),
    # a running task with a live run block and populated properties; the thread linked to a note
    ("conversation-running", "#/threads/t-monitor-dedupe", {"wait": ".conv-timeline .conv-run, .conv-timeline .conv-msg"}),
    ("conversation-note-link", "#/threads/t-portal-name", {"wait": ".conv-prop-link-a", "wait_phone": ".conv-timeline .conv-storybox"}),
    # THREADS: every thread, the done ones showing; a done thread's conversation with a note being written
    ("threads-done", "#/threads?state=done", {"wait": "#list-threads .lrow.done"}),
    ("conversation-done", "#/threads/t-digest-cron?state=done", {"wait": ".conv-timeline .conv-msg", "type": [".conv-note", "One more thing: the cron leg needs a Monitor monitor so the next miss is seen the same night."]}),
    ("file-card", "#/needs-you", {"click": "#add", "wait": "#sheet:not(.hidden)"}),
    ("help", "#/needs-you", {"key": "?", "wait": ".shell-help-overlay"}),
    ("search", "#/search?q=portal", {"wait": ".sres h3"}),
    ("activity", "#/activity", {"wait": ".act-tile", "wait_phone": "#list-activity .lrow"}),
    ("activity-summary", "#/activity?summary=1", {"wait": ".act-tile"}),
    ("notebook-folder", "#/notebook", {"notebook": True, "wait": "#main .nbx-shelves"}),
    ("notebook-day", "#/day", {"notebook": True, "wait": "#main .dayhead"}),
    ("notebook-note", "#/note/Contacts%2FPine%20Hollow%20portal.md", {"notebook": True, "wait": "#main .md"}),
    ("notebook-topic", "#/topic/Contacts%2FPine%20Hollow%20portal.md", {"notebook": True, "wait": "#main .ctx"}),
    ("notebook-editor", "#/edit/Contacts%2FPine%20Hollow%20portal.md", {"notebook": True, "work": True, "wait": "#editor-host .cm-editor"}),
    # L7: an action done in place (the toast is the proof), repeatable at both sizes
    ("activity-action", "#/activity", {"work": True, "ready": "#list-activity .lrow .act-btn[data-act=snooze]",
                                       "clicks": ["#list-activity .lrow .act-btn[data-act=snooze]", ".act-pop .act-pop-item >> nth=0"], "wait": ".toast"}),
]


def launch_args() -> dict:
    """Playwright's own Chromium when its revision is installed; else the newest Chromium
    under PLAYWRIGHT_BROWSERS_PATH (the build host pins one and never runs `playwright
    install`), or HELM_SCREENS_CHROME when set."""
    exe = os.environ.get("HELM_SCREENS_CHROME")
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if not exe and base and os.path.isdir(base):
        cands = []
        for d in os.listdir(base):
            for rel in ("chrome-linux/headless_shell", "chrome-linux/chrome"):
                p = os.path.join(base, d, rel)
                if d.startswith("chromium") and os.path.isfile(p):
                    cands.append((d, p))
        cands.sort()
        if cands:
            exe = cands[-1][1]
    return {"executable_path": exe} if exe else {}


def notebook_up(site) -> bool:
    try:
        req = urllib.request.Request(site.web_url + "/api/notes/folders", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def shoot(st, sites, only, out_dir):
    from playwright.sync_api import sync_playwright
    taken, skipped = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args())
        for name in sites:
            site = st.sites[name]
            nb = notebook_up(site)
            os.makedirs(os.path.join(out_dir, name), exist_ok=True)
            for mod, route, opt in SHOTS:
                if only and mod not in only:
                    continue
                if opt.get("home") and name != "home":
                    continue
                if opt.get("work") and name != "work":
                    continue
                if opt.get("notebook") and not nb:
                    skipped.append((name, mod, "the notebook routes are not up at this site yet"))
                    continue
                stub = bool(opt.get("notebook") and site.stub_notebook)
                for w, h in SIZES:
                    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, extra_http_headers=HEADERS,
                                              locale="en-US", timezone_id="America/New_York")
                    page = ctx.new_page()
                    errors = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(site.web_url + "/" + route, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass  # an SSE relay keeps a connection open; the element wait below is the real signal
                    try:
                        page.wait_for_selector(".lrow, .lempty, .empty .big", timeout=8000)
                    except Exception:
                        pass
                    if opt.get("ready"):
                        try:
                            page.wait_for_selector(opt["ready"], timeout=10000)
                        except Exception as e:
                            errors.append("ready %s: %s" % (opt["ready"], str(e).splitlines()[0]))
                    if opt.get("click"):
                        page.click(opt["click"])
                    if opt.get("hover"):
                        page.hover(opt["hover"])
                    for sel in opt.get("clicks") or []:
                        try:
                            page.click(sel, timeout=8000)
                            page.wait_for_timeout(250)
                        except Exception as e:
                            errors.append("click %s: %s" % (sel, str(e).splitlines()[0]))
                    if opt.get("key"):
                        page.keyboard.press(opt["key"])
                    if opt.get("type"):
                        try:
                            page.wait_for_selector(opt["type"][0], timeout=8000)
                            page.fill(opt["type"][0], opt["type"][1])
                        except Exception as e:
                            errors.append("type into %s: %s" % (opt["type"][0], str(e).splitlines()[0]))
                    wait = opt.get("wait_phone") if (w <= 760 and opt.get("wait_phone")) else opt.get("wait")
                    if wait:
                        try:
                            page.wait_for_selector(wait, timeout=8000)
                        except Exception as e:
                            errors.append("wait for %s: %s" % (wait, str(e).splitlines()[0]))
                    page.mouse.move(w - 4, h - 4)   # park the pointer on empty space: no hover tip over the shot
                    page.wait_for_timeout(400)   # local fonts and view transitions settle
                    path = os.path.join(out_dir, name, "%s-%dx%d.png" % (mod, w, h))
                    page.screenshot(path=path, full_page=False)
                    taken.append((name, mod, w, h, path, errors, stub))
                    ctx.close()
        browser.close()
    return taken, skipped


USAGE = "usage: shoot.py [--site home] [--only mod1,mod2] [--out DIR]"


def parse_args(argv):
    """The three flags, each with a value. Anything else (--help, a typo, a stray word) prints the
    usage and exits: an ignored flag used to run a full default shoot over docs/screens/."""
    a = {"sites": ["home"], "only": None, "out": OUT}
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag in ("--help", "-h"):
            print(USAGE)
            raise SystemExit(0)
        if flag not in ("--site", "--only", "--out") or i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            print("shoot.py: %s %s" % ("unknown argument" if flag not in ("--site", "--only", "--out") else "needs a value:", flag), file=sys.stderr)
            print(USAGE, file=sys.stderr)
            raise SystemExit(2)
        val = argv[i + 1]
        if flag == "--site":
            a["sites"] = [val]
        elif flag == "--only":
            a["only"] = set(val.split(","))
        else:
            a["out"] = val
        i += 2
    return a


def main(argv):
    a = parse_args(argv)
    sites, only, out_dir = a["sites"], a["only"], a["out"]
    with fixture.Stack(tuple(sites)) as st:
        taken, skipped = shoot(st, sites, only, out_dir)
    bad = 0
    for name, mod, w, h, path, errors, stub in taken:
        flag = (" !! " + "; ".join(errors) if errors else "") + ("  [notebook API stubbed]" if stub else "")
        bad += 1 if errors else 0
        print("%-5s %-18s %4dx%-4d %s%s" % (name, mod, w, h, os.path.relpath(path, ROOT), flag))
    for name, mod, why in skipped:
        print("%-5s %-18s skipped: %s" % (name, mod, why))
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump({"taken": [{"site": t[0], "module": t[1], "size": "%dx%d" % (t[2], t[3]), "file": os.path.relpath(t[4], out_dir), "errors": t[5],
                              "notebook_stubbed": t[6]} for t in taken],
                   "skipped": [{"site": s[0], "module": s[1], "why": s[2]} for s in skipped]}, f, indent=1)
    write_readme(out_dir)
    print("%d shots, %d with page errors, %d skipped" % (len(taken), bad, len(skipped)))
    return 1 if bad else 0


MODULE_NOTE = {
    "needs-you": "The three-pane main screen in the fixed Helm design: NEEDS YOU open in the left pane, its first ask in the middle with the options in the composer, the properties on the right.",
    "conversation-running": "A running task: the full timeline from the thread's events, populated properties.",
    "conversation-note-link": "A thread linked to a note (note: link in the properties; the phone shows the conversation itself, the properties pane is a drawer there).",
    "threads-done": "THREADS filtered to done: every thread it has held, newest first, paged.",
    "conversation-done": "A done thread's full conversation with a note being written on it.",
    "file-card": "The file-a-card sheet: the intent chips, the category and the link.",
    "help": "The ? overlay: both apps' keys, clashes resolved.",
    "search": "Search: notes and threads grouped; a thread hit is a row into its conversation.",
    "activity": "ACTIVITY: today's events as rows on the left, heartbeat and counts in the middle, the status on the right.",
    "notebook-folder": "NOTEBOOK: the rail in the left pane (today, folders, shelves) and the notebook's home in the middle (in play, shelves, recently edited).",
    "notebook-day": "The Day page: the daily note, the append box (store mode), the threads timeline, carry-over.",
    "notebook-note": "A note in the middle with its topic hub in the right pane (threads about it, backlinks, daily mentions).",
    "notebook-topic": "The topic hub as a page (#/topic/<rel>).",
    "notebook-editor": "The editor (CodeMirror, store mode only) with the frontmatter folded.",
}


def write_readme(out_dir):
    """docs/screens/README.md from manifest.json: every PNG, what it shows, and which were
    rendered against the notebook stub."""
    try:
        with open(os.path.join(out_dir, "manifest.json")) as f:
            m = json.load(f)
    except OSError:
        return
    lines = ["# Screenshots", "",
             "The three-pane app at 999x1392 (a desktop window) and 412x915 (phone), rendered from synthetic data by",
             "`tools/screens/shoot.py` over `tools/screens/fixture.py` (fictional threads and notes only). Re-run:", "",
             "    python3 tools/screens/shoot.py", ""]
    stubbed = [t for t in m["taken"] if t.get("notebook_stubbed")]
    if stubbed:
        lines += ["The notebook shots (`notebook-*`) were rendered against `tools/screens/fake_notebook.py`, a stand-in for the",
                  "notebook routes that were being built in parallel; the fixture drops the stub the moment the api role answers",
                  "`/api/notes/folders`, and the same command then renders them from the real module.", ""]
    for site in ("home",):
        rows = [t for t in m["taken"] if t["site"] == site]
        if not rows:
            continue
        lines += ["", "| Screen | 999x1392 | 412x915 | What it shows |", "|---|---|---|---|"]
        mods = []
        for t in rows:
            if t["module"] not in mods:
                mods.append(t["module"])
        for mod in mods:
            big = next((t for t in rows if t["module"] == mod and t["size"] == "999x1392"), None)
            small = next((t for t in rows if t["module"] == mod and t["size"] == "412x915"), None)
            note = MODULE_NOTE.get(mod, "")
            if big and big.get("notebook_stubbed"):
                note += " (notebook API stubbed)"
            lines.append("| %s | %s | %s | %s |" % (mod, "[png](%s)" % big["file"] if big else "", "[png](%s)" % small["file"] if small else "", note))
        lines.append("")
    if m.get("skipped"):
        lines += ["## Not rendered", ""]
        for s in m["skipped"]:
            lines.append("- %s / %s: %s" % (s["site"], s["module"], s["why"]))
        lines.append("")
    with open(os.path.join(out_dir, "README.md"), "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
