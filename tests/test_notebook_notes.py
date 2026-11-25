"""The notes importer against a throwaway folder: frontmatter, wikilinks,
checkbox lines, search, and incremental update on change and delete.

Ported from the reference suite's tests/test_notes.py (47 checks), setup and imports only:
the settings replace the vault path and the notes database and the module is
notebook.notes. Every one of the 47 ports.
"""
# temp-ok: the folder under test is a fixture this module creates and deletes
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VAULT = tempfile.mkdtemp(prefix="helm_test_notes_vault_")
INDEX = tempfile.mkdtemp(prefix="helm_test_notes_index_")
os.environ["HELM_NOTES_PROFILE"] = "fixed"
os.environ["HELM_NOTES_MODE"] = "folder"
os.environ["HELM_NOTES_ROOT"] = VAULT
os.environ["HELM_NOTES_DB"] = os.path.join(INDEX, "notes.db")
os.environ["HELM_DB"] = os.path.join(INDEX, "triage.db")
os.environ["HELM_DATA"] = INDEX

from notebook import notes, render  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def write(rel: str, text: str, mtime: float | None = None):
    p = os.path.join(VAULT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def seed():
    write("Contacts/Pine Hollow - PayTerm, ticket 00100001.md",
          "---\ntype: contact\nrelated_case: 00100001\ntags:\n  - payterm\n  - printing\n---\n"
          "# Pine Hollow: PayTerm driver flapping\n\nSee [[DR server checklist]] and "
          "[[DR server checklist#Firewall|the firewall bit]].\n\n"
          "![[attachments/pasted.png]]\n\nA [[Missing note]] too.\n\n"
          "```\n[[not a link, inside a fence]]\n- [ ] not a task either\n```\n\n"
          "- [ ] call Gary\n- [x] read the driver log\n* [X] uppercase done\n")
    write("Projects/Ongoing/DR server checklist.md",
          "# DR server checklist\n\n## Firewall\n\n- [ ] open 1433\n")
    write("Daily/2026/2026-09/2026-09-22.md",
          "- 9:23 w/ Gary about Pine Hollow payterm\n- [ ] follow up with Gary\n- [x] done thing\n")
    write("Daily/2026/2026-09/2026-09-23.md", "empty day\n")
    write("Home.md", "# Home\n\n[[Contacts]]\n")
    write("Reference/empty.md", "")
    write(".notes/workspace.md", "# skipped\n\n[[DR server checklist]]\n")
    write("Contacts/_scratch/scratch.md", "# skipped too\n")
    os.makedirs(os.path.join(VAULT, "attachments"), exist_ok=True)
    with open(os.path.join(VAULT, "attachments", "pasted.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)


def run() -> int:
    seed()
    con = notes.db()
    r = notes.rebuild(con, force=True)
    check("rebuild imports the notes and the file", r["notes"] == 6 and r["files"] == 1, r)
    check("skipped folders stay out",
          not notes.exists(con, ".notes/workspace.md") and not notes.exists(con, "Contacts/_scratch/scratch.md"))

    rel = "Contacts/Pine Hollow - PayTerm, ticket 00100001.md"
    n = notes.get(con, rel)
    check("frontmatter parsed", n["frontmatter"].get("type") == "contact" and n["frontmatter"].get("related_case") == "00100001", n["frontmatter"])
    check("list values on indented lines are joined", n["frontmatter"].get("tags") == "- payterm - printing", n["frontmatter"].get("tags"))
    check("title comes from the H1, not the file name", n["title"] == "Pine Hollow: PayTerm driver flapping", n["title"])
    check("body excludes the frontmatter", not n["body"].startswith("---"))
    check("folder and stem", n["folder"] == "Contacts" and n["stem"] == "Pine Hollow - PayTerm, ticket 00100001")
    check("a note with no H1 titles itself by stem", notes.get(con, "Daily/2026/2026-09/2026-09-22.md")["title"] == "2026-09-22")
    check("an empty note imports as empty", notes.get(con, "Reference/empty.md")["body"] == "")

    links = notes.outlinks(con, rel)
    targets = sorted((l["target"], l["rel"], bool(l["embed"])) for l in links)
    check("three wikilinks and one embed, fenced one ignored", len(links) == 4, targets)
    check("plain and heading links resolve to the same note",
          [t for t in targets if t[0] == "DR server checklist"] == [("DR server checklist", "Projects/Ongoing/DR server checklist.md", False)] * 2, targets)
    check("missing note stays unresolved", ("Missing note", None, False) in targets, targets)
    check("image embed resolves to the file", ("attachments/pasted.png", "attachments/pasted.png", True) in targets, targets)
    bl = notes.backlinks(con, "Projects/Ongoing/DR server checklist.md")
    check("backlinks find the member note", [b["rel"] for b in bl] == [rel], bl)

    # a hub linked from many notes: every backlink is listed, but only the lines the page draws are read from their bodies
    hub = "Projects/Ongoing/DR server checklist.md"
    real_exec, reads = con.execute, []
    class Spy:
        def __init__(self, c): self.c = c
        def execute(self, sql, *a):
            if "SELECT body FROM notes" in sql:
                reads.append(sql)
            return self.c.execute(sql, *a)
    notes.backlinks(Spy(con), hub, text_limit=0)
    check("backlinks(text_limit=0) reads no note body", not reads, reads)
    check("first_paragraph skips headings and fences, drops marks",
          notes.first_paragraph("# T\n\n```\ncode here that is long\n```\n\nShort.\n\nThe **real** first paragraph of [[Some Note|the note]].\n") == "The real first paragraph of the note.")
    check("first_paragraph of nothing is empty", notes.first_paragraph("# only a heading\n") == "")

    tasks = notes.tasks_for(con, rel)
    check("three checkbox lines, none from the fence", [(t["done"], t["text"]) for t in tasks] ==
          [(False, "call Gary"), (True, "read the driver log"), (True, "uppercase done")], tasks)
    check("task line numbers point into the body", tasks[0]["line"] > 0 and tasks[1]["line"] == tasks[0]["line"] + 1, tasks)

    hits = notes.search(con, "payterm")
    check("search finds by body word, member note first", hits and hits[0]["rel"] == rel, [h["rel"] for h in hits])
    check("snippet carries the match markers", any("\x02" in h["snippet"] for h in hits))
    check("search is case-insensitive and prefix-tolerant", any(h["rel"] == rel for h in notes.search(con, "FLAPP")))
    check("no hits for a word not in the vault", notes.search(con, "zzqxv") == [])
    check("switcher matches on stem", [s["rel"] for s in notes.switcher(con, "dr server")] == ["Projects/Ongoing/DR server checklist.md"])

    fo = {f["folder"]: f["count"] for f in notes.folders(con)}
    check("folder counts", fo.get("Contacts") == 1 and fo.get("Daily") == 2 and fo.get("Projects/Ongoing") == 1, fo)
    check("daily notes list newest first", [x["stem"] for x in notes.list_folder(con, "Daily")] == ["2026-09-23", "2026-09-22"])
    check("daily_rel and is_daily", notes.is_daily("Daily/2026/2026-09/2026-09-22.md") and not notes.is_daily("Daily/Meetings/2026-09-22 call.md"))
    st = notes.stats(con)
    check("stats count links and how many resolve (Missing note and the Contacts folder do not)",
          st["links"] == 5 and st["links_resolved"] == 3, st)

    # Rendering the note ties the resolver to the index.
    html = render.render(n["body"], render.Resolver(con))
    check("wikilink renders as an in-app link",
          'href="#/note/Projects%2FOngoing%2FDR%20server%20checklist.md"' in html, html[:400])
    check("alias is the link text", ">the firewall bit</a>" in html)
    check("missing note renders as plain marked text", 'class="wl missing"' in html)
    check("image embed renders as an img on the attachment route",
          'src="/att/attachments%2Fpasted.png"' in html)
    check("checkbox lines render with their body line, done ones marked",
          html.count('<input type="checkbox" class="task-box" data-line=') == 3 and html.count("checked") == 2, html)
    check("the first checkbox carries its own line number",
          'data-line="%d"' % tasks[0]["line"] in html, tasks[0]["line"])
    check("fenced wikilink is left alone", "[[not a link, inside a fence]]" in html)

    # Incremental: unchanged files are skipped, a changed one is re-read.
    r2 = notes.rebuild(con)
    check("second rebuild imports nothing", r2["imported"] == 0, r2)
    p = write("Projects/Ongoing/DR server checklist.md",
              "# DR server checklist\n\nNow mentions bluestem.\n\n- [x] open 1433\n", mtime=time.time() + 5)
    out = notes.import_path(con, p)
    check("changed file re-imports", out and not out.get("unchanged"), out)
    check("search sees the new word", [h["rel"] for h in notes.search(con, "bluestem")] == ["Projects/Ongoing/DR server checklist.md"])
    check("the task flipped to done", notes.tasks_for(con, "Projects/Ongoing/DR server checklist.md")[0]["done"] is True)
    check("import of an unchanged file is a no-op", notes.import_path(con, p).get("unchanged") is True)

    # A new note that an existing link pointed at gets resolved on arrival.
    p2 = write("Reference/Missing note.md", "# Missing note\n\nhere now\n")
    notes.import_path(con, p2)
    check("earlier dangling link now resolves",
          any(l["target"] == "Missing note" and l["rel"] == "Reference/Missing note.md" for l in notes.outlinks(con, rel)))

    # Delete: note, its links, tasks and search rows all go.
    os.remove(p2)
    check("remove_path reports the removal", notes.remove_path(con, p2) is True)
    check("deleted note is gone", not notes.exists(con, "Reference/Missing note.md"))
    check("search no longer finds it", notes.search(con, "here now") == [])
    check("link back to it is dangling again",
          any(l["target"] == "Missing note" and l["rel"] is None for l in notes.outlinks(con, rel)))
    check("removing an unknown path is False", notes.remove_path(con, os.path.join(VAULT, "nope.md")) is False)

    # Helpers.
    check("split_frontmatter without a block returns everything as body", notes.split_frontmatter("# x\n") == ({}, "# x\n"))
    check("split_frontmatter with an unterminated block leaves the text alone", notes.split_frontmatter("---\na: b\n# x\n")[0] == {})
    check("fts_query quotes each token and prefix-matches it", notes.fts_query('wire "exact phrase"') == '"wire"* "exact"* "phrase"*', notes.fts_query('wire "exact phrase"'))
    check("read_text tolerates a BOM and CRLF",
          notes.read_text(write("Reference/bom.md", "﻿line\r\nnext\r\n")) == "line\nnext\n")

    con.close()
    shutil.rmtree(VAULT, ignore_errors=True)
    shutil.rmtree(INDEX, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
