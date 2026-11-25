"""The notebook writes against a throwaway folder: save with its conflict and
credential checks, notes from templates, today's note, appends, checkbox
toggles, rename with link rewriting, attachments, autocomplete.

Ported from the reference suite's tests/test_edit.py (69 checks), setup and imports only.
Every one of the 69 ports; the suite runs in the reference's phase-1 `folder` mode,
which is the mode the original ran in (the editor writes files, the store
re-indexes them). Two extra checks at the end prove the read-only refusal.
"""
# temp-ok: the folder under test is a fixture this module creates and deletes
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VAULT = tempfile.mkdtemp(prefix="helm_test_edit_vault_")
INDEX = tempfile.mkdtemp(prefix="helm_test_edit_index_")
os.environ["HELM_NOTES_PROFILE"] = "fixed"
os.environ["HELM_NOTES_MODE"] = "folder"
os.environ["HELM_NOTES_ROOT"] = VAULT
os.environ["HELM_NOTES_DB"] = os.path.join(INDEX, "notes.db")
os.environ["HELM_DB"] = os.path.join(INDEX, "triage.db")
os.environ["HELM_DATA"] = INDEX

from notebook import edit, notes  # noqa: E402

edit.TEMPLATE_DIR = os.path.join(VAULT, ".notes", "templates")

TODAY = dt.date.today()
PAST = TODAY - dt.timedelta(days=3)
PAST2 = TODAY - dt.timedelta(days=5)
OLD = "Contacts/Old Name.md"
REF = "Projects/Ongoing/Referrer.md"

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def write(rel: str, text: str):
    p = os.path.join(VAULT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return p


def read(rel: str) -> str:
    with open(os.path.join(VAULT, rel.replace("/", os.sep)), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def err(fn):
    """The EditError a call raises, or None when it does not raise."""
    try:
        fn()
    except edit.EditError as e:
        return e
    return None


def seed():
    write(".notes/templates/Contact.md", "---\ntype: contact\ncreated: {{date}}\n---\n# {{title}}\n\n## Contacts\n\n## Notes\n\n")
    write(".notes/templates/Build.md", "# {{title}}\n\n## Status\n\nstarted {{date:YYYY-MM-DD}} at {{time}}\n")
    write(".notes/templates/Daily.md", "# {{date:YYYY-MM-DD}}\n")
    write(OLD, "# Old Name\n\n- [ ] first\n- [x] second\n")
    write("Contacts/With props.md", "---\ntype: contact\n---\n# With props\n\n- [ ] after props\n")
    write(REF, "# Referrer\n\nSee [[Old Name]], [[Old Name#Heading|alias]], [[Contacts/Old Name]] and ![[Old Name]] plus [[old name]] lowercase.\n")
    write(notes.daily_rel(PAST), "- talked about [[Old Name]]\n- [ ] past task\n")
    write("Reference/no newline.md", "# No newline\n\nlast line")
    os.makedirs(os.path.join(VAULT, "attachments"), exist_ok=True)


def run() -> int:
    seed()
    con = notes.db()
    notes.rebuild(con, force=True)
    past_rel = notes.daily_rel(PAST)
    today_rel = notes.daily_rel(TODAY)

    # --- read, editable
    r = edit.read(con, OLD)
    check("read returns the text and mtime of an editable note", r["text"].startswith("# Old Name") and r["mtime"] > 0 and r["editable"] is True, r)
    rp = edit.read(con, past_rel)
    check("a past day's note is not editable, with the reason", rp["editable"] is False and "history" in rp["reason"], rp)
    check("today's note would be editable", edit.editable(today_rel) == (True, ""))
    e = err(lambda: edit.read(con, "Contacts/nope.md"))
    check("read of a missing note is 404", e is not None and e.status == 404)
    e = err(lambda: edit.read(con, "../outside.md"))
    check("a path that climbs out is 400", e is not None and e.status == 400)

    # --- save
    out = edit.save(con, OLD, r["text"], base_mtime=r["mtime"])
    check("saving unchanged text is a no-op", out["saved"] is False, out)
    out = edit.save(con, OLD, "# Old Name\r\n\r\n- [ ] first\r\n- [x] second\r\nzebra line", base_mtime=r["mtime"])
    check("changed text saves and reports the new mtime", out["saved"] is True and out["mtime"] >= r["mtime"], out)
    check("CRLF becomes LF and a final newline is added", read(OLD) == "# Old Name\n\n- [ ] first\n- [x] second\nzebra line\n", repr(read(OLD)))
    check("the index sees the new word", [h["rel"] for h in notes.search(con, "zebra")] == [OLD])
    e = err(lambda: edit.save(con, OLD, "# stale\n", base_mtime=r["mtime"] - 100))
    check("a stale base_mtime is 409 and carries the disk text", e is not None and e.status == 409 and
          e.extra.get("text", "").startswith("# Old Name") and "mtime" in e.extra, e and e.extra.keys())
    check("the refused save wrote nothing", read(OLD).startswith("# Old Name\n\n- [ ] first"))
    e = err(lambda: edit.save(con, past_rel, "rewritten\n"))
    check("saving a past day's note is 403", e is not None and e.status == 403)
    e = err(lambda: edit.save(con, "Contacts/nope.md", "x\n"))
    check("saving a missing note is 404", e is not None and e.status == 404)

    # --- credentials
    cur = edit.read(con, OLD)
    e = err(lambda: edit.save(con, OLD, cur["text"] + "svc password: hunter2hunter2\n", base_mtime=cur["mtime"]))
    check("a new line that looks like a password is refused with its shape", e is not None and e.status == 400 and
          e.extra.get("secret") is True and e.extra.get("shape", "").startswith("password"), e and e.extra)
    check("the refused credential never reached the file", "hunter2" not in read(OLD))
    out = edit.save(con, OLD, cur["text"] + "the password reset flow is in the runbook\n", base_mtime=cur["mtime"])
    check("prose that mentions a password is fine", out["saved"] is True)
    cur = edit.read(con, OLD)
    out = edit.save(con, OLD, cur["text"] + "svc password: hunter2hunter2\n", base_mtime=cur["mtime"], allow_secret=True)
    check("allow_secret saves it anyway", out["saved"] is True and "hunter2" in read(OLD))
    cur = edit.read(con, OLD)
    out = edit.save(con, OLD, cur["text"] + "token = abcdefghijklmnop\n", base_mtime=cur["mtime"])
    check("a note that already carries one is exempt", out["saved"] is True)
    for s, name in (("Bearer abcdefghijklmnopqrstuvwxyz0123", "bearer"), ("Server=x;Pwd=secret1;Database=y;", "password"),
                    ("-----BEGIN RSA " + "PRIVATE KEY-----", "private"), ("AKIA" + "ABCDEFGHIJKLMNOP", "AWS"),
                    ("https://user:pw@host/x", "URL"), ("ghp-abcdefghijklmnopqrstuvwxyz", "API key")):
        check("secret shape: " + name, (edit.secret_shape(s) or "").lower().startswith(name.lower()), edit.secret_shape(s))
    check("a plain line has no secret shape", edit.secret_shape("- 9:23 w/ Gary about the driver") is None)
    check("the shape never carries the value", "hunter2" not in (edit.secret_shape("password: hunter2hunter2") or ""))
    # A documented placeholder names the vault secret to fetch; it is not a credential.
    for s in ("curl -s 'http://media.example:8096/library/sections?X-Media-Token=<MEDIA_TOKEN>'",
              "token: ${HELM_APP_TOKEN}", "api_key={env:SERVICE_API_KEY}", "token=$HELM_APP_TOKEN",
              "password: <SVC_PASSWORD>", "client_secret: REDACTED", "api_key=********", "token=xxxxxxxxxxxx",
              "- `X-Media-Token=<MEDIA_TOKEN>`, then `api_key={env:SERVICE_API_KEY}`",
              "?X-Media-Token=<MEDIA_TOKEN>&X-Media-Client=helm"):
        check("a placeholder is not a credential: " + s, edit.secret_shape(s) is None, edit.secret_shape(s))
    for s, name in (("X-Media-Token=Zr8kq3LmP0aa", "secret"), ("token=<abc123def456ghi789", "secret"),
                    ("password: hunter22222", "password"), ("token=<MEDIA_TOKEN>Zr8kq3LmP0aa", "secret"),
                    ("token=${X}Zr8kq3LmP0aa", "secret"),
                    ("token=<MEDIA_TOKEN> secret=Zr8kq3LmP0aa", "secret"),
                    ("?X-Media-Token=<MEDIA_TOKEN>&api_key=Zr8kq3LmP0aa", "secret")):
        check("still a credential: " + s, (edit.secret_shape(s) or "").lower().startswith(name), edit.secret_shape(s))
    e = err(lambda: edit.check_secrets("# Media\n", "# Plex\ncurl '...?X-Media-Token=<MEDIA_TOKEN>'\n"))
    check("check_secrets lets a placeholder line through", e is None, e and e.detail)
    e = err(lambda: edit.check_secrets("# Media\n", "# Plex\ncurl '...?X-Media-Token=Zr8kq3LmP0aa'\n"))
    check("check_secrets still refuses the real value", e is not None and e.status == 400, e)

    # --- new notes
    when = dt.datetime(2026, 9, 23, 14, 5, 9)
    out = edit.new_note(con, "Contacts", "New Member", when=when)
    check("new note lands in the folder from its template", out["created"] and out["rel"] == "Contacts/New Member.md" and
          read(out["rel"]).startswith("---\ntype: contact\ncreated: 2026-09-23\n---\n# New Member\n"), read(out["rel"]))
    check("the new note is indexed", notes.exists(con, "Contacts/New Member.md"))
    out = edit.new_note(con, "Reference", "Plain one", when=when)
    check("a folder with no template gets a heading", read(out["rel"]) == "# Plain one\n", repr(read(out["rel"])))
    out = edit.new_note(con, "Reference", "From build", template="Build", when=when)
    check("an explicit template applies, with date and time formats",
          read(out["rel"]) == "# From build\n\n## Status\n\nstarted 2026-09-23 at 14:05\n", read(out["rel"]))
    for folder, title, status, why in (("Daily", "x", 400, "the daily folder"), ("Nope", "x", 400, "an unknown folder"),
                                      ("Contacts", "Untitled", 400, "Untitled"), ("Contacts", "  ", 400, "a blank title"),
                                      ("Contacts", "a/b", 400, "a slash"), ("Contacts", "New Member", 409, "an existing name"),
                                      ("Contacts", "x" * 181, 400, "a title over 180 chars")):
        e = err(lambda: edit.new_note(con, folder, title))
        check("new_note refuses %s with %d" % (why, status), e is not None and e.status == status, e and (e.status, e.detail))
    check("templates are listed by name", edit.templates() == ["Build", "Contact", "Daily"], edit.templates())
    check("template_for maps folders", edit.template_for("Contacts") == "Contact.md" and edit.template_for("Reference") is None)

    # --- today's note
    e = err(lambda: edit.create_daily(con, PAST))
    check("a past day is not created", e is not None and e.status == 403)
    out = edit.create_daily(con)
    check("today's note comes from the Daily template", out["rel"] == today_rel and read(today_rel) == "# %s\n" % TODAY.isoformat(), repr(read(today_rel)))
    e = err(lambda: edit.create_daily(con))
    check("creating it twice is 409", e is not None and e.status == 409)

    # --- appends
    out = edit.append_daily(con, TODAY, "- [sync] appended line\n")
    check("append goes on the end with one newline", read(today_rel) == "# %s\n- [sync] appended line\n" % TODAY.isoformat() and
          out["appended"] == "- [sync] appended line", repr(read(today_rel)))
    gap = TODAY - dt.timedelta(days=10)
    edit.append_daily(con, gap, "late entry")
    check("appending to a day with no note creates it with its heading",
          read(notes.daily_rel(gap)) == "# %s\n\nlate entry\n" % gap.isoformat(), repr(read(notes.daily_rel(gap))))
    check("the created day is indexed", notes.exists(con, notes.daily_rel(gap)))
    write(notes.daily_rel(PAST2), "- no newline at the end")
    edit.append_daily(con, PAST2, "next")
    check("a last line without its newline is not glued onto", read(notes.daily_rel(PAST2)) == "- no newline at the end\nnext\n",
          repr(read(notes.daily_rel(PAST2))))
    e = err(lambda: edit.append_daily(con, TODAY, "   "))
    check("a blank append is 400", e is not None and e.status == 400)
    # the owner 2026-09-28: "one line at a time" was never his rule (the reference's port carried it). What is
    # typed lands as typed, several lines included, Windows line ends made plain.
    before = read(today_rel)
    e = err(lambda: edit.append_daily(con, TODAY, "two\r\nlines\n\n- and a third\n"))
    check("a multi-line append lands as typed", e is None and read(today_rel) == before + "two\nlines\n\n- and a third\n",
          (e and e.status, repr(read(today_rel)[len(before):])))
    # The day door shares the generic append's text rules: credential guard, lone CR made plain.
    before = read(today_rel)
    e = err(lambda: edit.append_daily(con, TODAY, "ok line\nsvc password: hunter2hunter2\n"))
    check("a daily append with a credential-shaped line is refused 400 with its shape",
          e is not None and e.status == 400 and e.extra.get("secret") is True, e and (e.status, e.extra))
    check("the refused daily append left the note unchanged", read(today_rel) == before and "hunter2" not in read(today_rel))
    gone = TODAY - dt.timedelta(days=20)
    e = err(lambda: edit.append_daily(con, gone, "password: hunter2hunter2"))
    check("a refused daily append does not create the day's note",
          e is not None and e.status == 400 and not os.path.exists(os.path.join(VAULT, notes.daily_rel(gone))))
    e = err(lambda: edit.append_daily(con, TODAY, "password: hunter2hunter2", allow_secret=True))
    check("allow_secret lets a daily append through", e is None and "hunter2hunter2" in read(today_rel))
    before = read(today_rel)
    out = edit.append_daily(con, TODAY, "a\rb")
    check("a lone CR in a daily append becomes a newline", read(today_rel) == before + "a\nb\n" and out["appended"] == "a\nb",
          (repr(read(today_rel)[len(before):]), out["appended"]))

    # --- checkbox toggles
    trel = "Contacts/Toggle.md"
    write(trel, "# Toggle\n\n- [ ] first\n- [x] second\nplain\n")
    notes.import_path(con, notes.abspath(trel), force=True)
    out = edit.toggle_task(con, trel, 3)
    check("toggling line 3 ticks it", out["done"] is True and read(trel).split("\n")[2] == "- [x] first", read(trel))
    check("the tasks table follows", notes.tasks_for(con, trel)[0]["done"] is True)
    out = edit.toggle_task(con, trel, 3)
    check("toggling again unticks", out["done"] is False and read(trel).split("\n")[2] == "- [ ] first")
    out = edit.toggle_task(con, trel, 4, done=True)
    check("an explicit done state is honoured", out["done"] is True and read(trel).split("\n")[3] == "- [x] second")
    e = err(lambda: edit.toggle_task(con, trel, 5))
    check("a plain line is 400", e is not None and e.status == 400)
    e = err(lambda: edit.toggle_task(con, trel, 99))
    check("a line past the end is 400", e is not None and e.status == 400)
    e = err(lambda: edit.toggle_task(con, trel, 3, base_mtime=1.0))
    check("a stale mtime is 409", e is not None and e.status == 409)
    prel = "Contacts/With props.md"
    ptasks = notes.tasks_for(con, prel)
    out = edit.toggle_task(con, prel, ptasks[0]["line"])
    check("line numbers count from after the frontmatter, which stays intact",
          out["done"] is True and read(prel) == "---\ntype: contact\n---\n# With props\n\n- [x] after props\n", read(prel))
    e = err(lambda: edit.toggle_task(con, past_rel, 2))
    check("a past day's checkbox is 403", e is not None and e.status == 403)

    # Every checkbox the renderer draws (bullet, ordered, quoted) must toggle, and flip only its line.
    from notebook import render as _render  # noqa: E402
    import re as _re  # noqa: E402
    srel = "Contacts/Shapes.md"
    shapes = ["# Shapes", "", "1. [ ] ordered", "2) [x] paren", "> - [ ] quoted", "> 1. [ ] quoted ordered",
              ">> * [ ] double quoted", "- [ ] plain", "  - [ ] nested", "10. [ ] ten"]
    write(srel, "\n".join(shapes) + "\n")
    notes.import_path(con, notes.abspath(srel), force=True)
    drawn = [int(n) for n in _re.findall(r'class="task-box" data-line="(\d+)"',
                                         _render.render("\n".join(shapes) + "\n", _render.Resolver(con)))]
    check("the renderer draws a box for every shape", drawn == [3, 4, 5, 6, 7, 8, 9, 10], drawn)
    check("the tasks table counts every box the renderer draws",
          [t["line"] for t in notes.tasks_for(con, srel)] == drawn, notes.tasks_for(con, srel))
    for ln in drawn:
        before = read(srel).split("\n")
        e = err(lambda: edit.toggle_task(con, srel, ln))
        after = read(srel).split("\n")
        changed = [i + 1 for i, (a, b) in enumerate(zip(before, after)) if a != b]
        check("toggling line %d (%s) works and flips only that line" % (ln, shapes[ln - 1]),
              e is None and changed == [ln] and len(before) == len(after),
              (e and (e.status, e.detail), changed, after[ln - 1]))
    check("the toggled lines keep their markers and quotes",
          read(srel).split("\n")[2:10] == ["1. [x] ordered", "2) [ ] paren", "> - [x] quoted", "> 1. [x] quoted ordered",
                                           ">> * [x] double quoted", "- [x] plain", "  - [x] nested", "10. [x] ten"],
          read(srel))

    # --- rename
    e = err(lambda: edit.rename_plan(con, past_rel, "x"))
    check("a daily note is not renamed", e is not None and e.status == 400)
    e = err(lambda: edit.rename_plan(con, OLD, "Old Name"))
    check("renaming to the same name is 400", e is not None and e.status == 400)
    e = err(lambda: edit.rename_plan(con, OLD, "With props"))
    check("renaming onto an existing note is 409", e is not None and e.status == 409)
    plan = edit.rename(con, OLD, "New Name", dry_run=True)
    ref = next((c for c in plan["changes"] if c["rel"] == REF), None)
    dly = next((c for c in plan["changes"] if c["daily"]), None)
    check("dry run names the target and touches nothing", plan["to"] == "Contacts/New Name.md" and plan["done"] is False and
          os.path.exists(notes.abspath(OLD)) and not os.path.exists(notes.abspath("Contacts/New Name.md")), plan)
    check("the plan counts every link form in the referrer", ref is not None and ref["count"] == 5, ref)
    check("the plan flags the daily note that links to it", dly is not None and dly["rel"] == past_rel and dly["count"] == 1, dly)
    check("total is the sum", plan["total"] == 6, plan["total"])
    done = edit.rename(con, OLD, "New Name", dry_run=False)
    check("the file moved", done["done"] is True and os.path.exists(notes.abspath("Contacts/New Name.md")) and not os.path.exists(notes.abspath(OLD)))
    reftext = read(REF)
    check("plain, heading-alias, path, embed and lowercase links all point at the new name",
          reftext == "# Referrer\n\nSee [[New Name]], [[New Name#Heading|alias]], [[New Name]] and ![[New Name]] plus [[New Name]] lowercase.\n", reftext)
    check("the daily note's link was rewritten too", "[[New Name]]" in read(past_rel) and "- [ ] past task" in read(past_rel))
    check("the index moved with it", notes.exists(con, "Contacts/New Name.md") and not notes.exists(con, OLD))
    check("backlinks resolve to the new note",
          sorted(b["rel"] for b in notes.backlinks(con, "Contacts/New Name.md")) == sorted([REF, past_rel]),
          notes.backlinks(con, "Contacts/New Name.md"))
    check("nothing dangles in the referrer", all(l["rel"] for l in notes.outlinks(con, REF)), notes.outlinks(con, REF))

    # --- rename: a bare link that resolves to a different note of the same name is left alone
    d1, d2, dref = "Contacts/Dup.md", "Projects/Ongoing/Dup.md", "Projects/Ongoing/DupRef.md"
    for r, t in ((d1, "# Dup\n"), (d2, "# Dup\n"),
                 (dref, "# DupRef\n\n[[Contacts/Dup]] and [[Dup]] and [[Projects/Ongoing/Dup]]\n")):
        write(r, t)
        notes.import_path(con, notes.abspath(r), force=True)
    plan = edit.rename(con, d2, "Dup Two", dry_run=True)
    dr = next((c for c in plan["changes"] if c["rel"] == dref), None)
    check("the plan counts only the link that names the renamed note", dr is not None and dr["count"] == 1, plan)
    edit.rename(con, d2, "Dup Two", dry_run=False)
    check("renaming one of two same-name notes leaves the bare link to the other alone",
          read(dref) == "# DupRef\n\n[[Contacts/Dup]] and [[Dup]] and [[Dup Two]]\n", read(dref))
    check("the bare link still resolves to the note it named",
          sorted((l["target"], l["rel"]) for l in notes.outlinks(con, dref))
          == [("Contacts/Dup", d1), ("Dup", d1), ("Dup Two", "Projects/Ongoing/Dup Two.md")], notes.outlinks(con, dref))
    edit.rename(con, d1, "Dup One", dry_run=False)
    check("renaming the note a bare link resolves to rewrites it",
          read(dref) == "# DupRef\n\n[[Dup One]] and [[Dup One]] and [[Dup Two]]\n", read(dref))

    # --- attachments
    when = dt.datetime(2026, 9, 23, 9, 8, 7)
    a = edit.attach(con, "image.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 16, when=when)
    check("a pasted image gets Obsidian's name and an embed link", a["name"] == "Pasted image 20260923090807.png" and
          a["wikilink"] == "![[Pasted image 20260923090807.png]]" and a["image"] is True, a)
    check("the file is on disk and in the index", os.path.exists(notes.abspath(a["rel"])) and
          con.execute("SELECT 1 FROM files WHERE rel=?", (a["rel"],)).fetchone() is not None)
    b = edit.attach(con, "image.png", b"\x89PNG\r\n\x1a\n" + b"\1" * 16, when=when)
    check("a second paste in the same second gets a suffix", b["name"] == "Pasted image 20260923090807 1.png", b)
    c = edit.attach(con, "report.pdf", b"%PDF-1.4 x", when=when)
    check("a named file keeps its name and links without embedding", c["name"] == "report.pdf" and c["wikilink"] == "[[report.pdf]]" and c["image"] is False, c)
    d = edit.attach(con, "../evil<>.txt", b"x", when=when)
    check("path parts and bad characters are stripped from a name", d["name"] == "evil.txt", d)
    e = err(lambda: edit.attach(con, "empty.png", b""))
    check("an empty upload is 400", e is not None and e.status == 400)

    # --- names a link cannot carry, and names that hold @ or $ (legal, and linkable)
    for rel, body in (("Contacts/owner@example.com.md", "# owner@example.com\n"),
                      ("Contacts/Budget $5.md", "# Budget $5\n"),
                      ("Contacts/Pos Target.md", "# Pos Target\n")):
        write(rel, body)
        notes.import_path(con, notes.abspath(rel), force=True)
    write("Contacts/Linker.md", "# Linker\n\n[[Contacts/owner@example.com]] [[owner@example.com]] [[Budget $5]] "
          "[[Pos Target@120]] [[Pos Target$anchor]] [[Nope@1]]\n")
    notes.import_path(con, notes.abspath("Contacts/Linker.md"), force=True)
    got = {l["target"]: l["rel"] for l in notes.outlinks(con, "Contacts/Linker.md")}
    check("a note named with @ is linkable by path and by bare name",
          got.get("Contacts/owner@example.com") == "Contacts/owner@example.com.md"
          and got.get("owner@example.com") == "Contacts/owner@example.com.md", got)
    check("a note named with $ is linkable", got.get("Budget $5") == "Contacts/Budget $5.md", got)
    check("the wiki engine's @pos and $anchor suffixes still drop when no note has the whole name",
          got.get("Pos Target@120") == "Contacts/Pos Target.md"
          and got.get("Pos Target$anchor") == "Contacts/Pos Target.md" and got.get("Nope@1") is None, got)
    check("resolve_note agrees with the full-pass resolver",
          notes.resolve_note(con, "owner@example.com") == "Contacts/owner@example.com.md"
          and notes.resolve_note(con, "Budget $5") == "Contacts/Budget $5.md")
    for bad in ("C# Notes", "Array [0]", "a]b"):
        e = err(lambda: edit.valid_title(bad))
        check("a title with # [ or ] is refused (a link cannot name it): %s" % bad,
              e is not None and e.status == 400, e and (e.status, e.detail))
    h = edit.attach(con, "Screenshot #1.png", b"\x89PNG\r\n\x1a\n" + b"\3" * 16, when=when)
    check("an attachment name has no # or brackets, so its embed names the file", "#" not in h["name"]
          and h["wikilink"] == "![[%s]]" % h["name"] and h["name"] == "Screenshot 1.png", h)
    write("Contacts/Embedder.md", "# Embedder\n\n%s\n" % h["wikilink"])
    notes.import_path(con, notes.abspath("Contacts/Embedder.md"), force=True)
    emb = notes.outlinks(con, "Contacts/Embedder.md")
    check("that embed resolves to the file",
          bool(emb) and notes.resolve_file(con, emb[0]["target"]) == h["rel"], emb)

    from notebook import render  # noqa: E402
    html = render.render("[[owner@example.com]] and [[Budget $5]]", render.Resolver(con))
    check("the rendered page links a note whose name ends like an extension",
          "missing" not in html and 'data-rel="Contacts/owner@example.com.md"' in html, html)

    # --- autocomplete
    items = edit.complete(con, "new")
    check("notes complete by stem, prefix matches first", bool(items) and items[0]["label"] in ("New Name", "New Member") and
          all(i["kind"] == "note" for i in items), items)
    items = edit.complete(con, "report", kind="file")
    check("files complete by name", [i["label"] for i in items] == ["report.pdf"], items)
    items = edit.complete(con, "report", kind="all")
    check("all mixes notes and files, files last", bool(items) and items[-1]["kind"] == "file", items)
    check("nothing matches nonsense", edit.complete(con, "zzqxv") == [])

    # --- the read-only site (home): every write is a 405 with the reason, reads still work
    os.environ["HELM_NOTES_MODE"] = "folder-readonly"
    e = err(lambda: edit.save(con, "Contacts/New Name.md", "x\n"))
    check("a save at a read-only site is 405 with the reason",
          e is not None and e.status == 405 and "read-only" in e.detail, e and (e.status, e.detail))
    check("editable says read-only and read still works",
          edit.editable("Contacts/New Name.md")[0] is False and "read-only" in edit.editable("Contacts/New Name.md")[1]
          and edit.read(con, "Contacts/New Name.md")["editable"] is False)
    os.environ["HELM_NOTES_MODE"] = "folder"

    con.close()
    shutil.rmtree(VAULT, ignore_errors=True)
    shutil.rmtree(INDEX, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
