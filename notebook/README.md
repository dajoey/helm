# notebook/

the reference's notebook, ported into Helm as one module that serves both sites .
The HTTP surface is `app/notebook_api.py`; the routes are listed in `docs/API.md`.

## Layout

| File | What it is | the reference app source |
|---|---|---|
| `config.py` | The **profile** (`work` / `home`) and every path and mode from the environment | the constants in `notes.py:35-37`, `edit.py TEMPLATE_FOR`, `topic.py:14-16`, `day.py:13` |
| `notes.py` | The note store and index: text, revisions, frontmatter, wikilinks, checkbox lines, FTS5; the full index and the mtime rescan | `app/notes.py` |
| `edit.py` | Every write to a note and the rule on each; the 405 in read-only mode | `app/edit.py` |
| `export.py` | The one-way export in store mode: write, verify, rebuild, adopt, quarantine, the periodic check | `app/export.py` |
| `cutover.py` | The flip from folder-as-truth to store-as-truth (preflight, round trip, backup zip, flip) | `app/cutover.py` minus the git and Obsidian halves |
| `render.py` | Markdown to HTML: wikilinks, embeds, checkboxes | `app/render.py` |
| `day.py` | The Day view: daily note + thread timeline + pages changed that day (+ calls on a second site) | `app/day.py` |
| `topic.py` | The Topic hub: threads about a note | `app/topic.py` |
| `helm_adapter.py` | the reference's `helm_client.py` names, in-process over `helm.store` / `helm.actions` | `app/helm_client.py` |
| `templates/` | Daily, Member, Build, Runbook (synthetic; work profile only) | the reference's `templates/` |

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `HELM_NOTES_ROOT` | the folder: the reference's vault/export on a second site, the the wiki folder at home (mounted `:ro`) | unset = no folder (store mode only) |
| `HELM_NOTES_DB` | `notes.db`: the store or the index cache (home) | `$HELM_DATA/notes.db` |
| `HELM_DATA` | the data directory (quarantine, backups) | `data/` |
| `HELM_DB` | the thread store the adapter opens | `$HELM_DATA/triage.db` |
| `HELM_NOTES_MODE` | `store` \| `folder-readonly` \| `folder` | `helm.site.notes_mode()` (work `store`, home `folder-readonly`) |
| `HELM_NOTES_PROFILE` | `work` \| `home` | `helm.site.site()` |
| `HELM_NOTES_DAILY_ROOT` | where daily notes live | work `Daily`, home `Journal/Day` |
| `HELM_NOTES_VERIFY_EVERY_S` | the export check's period (store mode) | `300` |

## Modes

- **`store`**. `notes.db` is the truth: every write is a revision (`revisions` table:
  who, when, the full text). the reference's edit rules hold (past days append-only; mtime 409;
  credential-shaped lines refused; no "Untitled"; no new top-level folder). The folder at
  `HELM_NOTES_ROOT` is the one-way export: rewritten after every write, checked at start and every
  5 min (`export.start_verify`): a missing or CRLF file is rewritten; a file newer than the store's
  revision is adopted as a new revision; an older differing file is quarantined under
  `$HELM_DATA/quarantine/<stamp>/` and rewritten; a stray note file is adopted from a notebook folder
  and quarantined elsewhere; and anything adopted-over or set aside files one Helm card (category
  `Notebook`). With no `HELM_NOTES_ROOT` there is no export and no attachments folder (attach is 503).
  A fresh DB in this mode is the truth from the start; a the reference app `notes.db` carries its own `truth`
  row across `import-work`. A DB the folder modes built (they record `truth=folder`) is flipped
  by the api itself when it first starts in this mode with a folder (`cutover.on_start()`, from
  `notebook_api.startup()` in the lifespan), before it serves: the `--go` sequence, one
  `notebook cutover: flipped` log line. A preflight problem or an unclean round trip changes
  nothing and the api does not start (`notebook cutover: refused`).
- **`folder-readonly`** (home). The wiki folder is the truth and nothing here writes it. The folder
  is indexed in full on the first request (`notebook_api.startup()` → `notes.full_index()`), then a
  daemon thread rescans it by mtime every 60 s (`notes.start_rescan()`), and every read also calls
  `notes.rescan_if_due()`, which is a clock comparison until a pass is due. Every write route answers
  `405 {ok:false, detail, mode}`; `edit.editable()` says `read-only` so the screens draw the
  read-only state. Revisions and history are empty (the wiki keeps its own).
- **`folder`** (home: the wiki bridge; also the reference's phase 1 and the cutover's
  reversal path, `notebook/cutover.py`). The folder is truth and the editor writes files, re-indexed
  at once. Other writers share the folder (other writers), so an append is one
  `O_APPEND` write, a rewrite is `409` when the file moved since it was read, a written file keeps its
  mode and owner, every file operation walks from the root without following a link and works on
  held folder fds (W10: no link in the folder is followed, inside or out), and every write is logged
  (`writes` table). `docs/DECISIONS.md` W1-W10.

## Profiles

| | `work` | `home` |
|---|---|---|
| folders | the reference's fixed list: Daily, Daily/Meetings, Daily/Ticket Cleanup, Contacts, Projects/Ongoing, Projects/Parked, Systems, Runbooks, Reference, Team | the top-level folders present in the wiki root (plus `""` for pages at the root) |
| templates | Daily, Member, Build, Runbook; `Contacts`→Contact, `Projects/*`→Build, `Runbooks`→Runbook | none |
| daily note | `Daily/YYYY/YYYY-MM/YYYY-MM-DD.md` | `Journal/Day/YYYY-MM-DD.md` |
| topic hub | `note:` links (linked) + contact-name / ticket-number / title heuristics (suggested) for Contacts, Projects/*, Systems | `note:` links only, for every note |
| Day view | note + timeline + calls (category "Calls", recording link) + carry-over + pages changed | note + timeline + pages changed that day + carry-over |
| skipped folders | `.notes`, `copilot`, `.trash`, `_scratch`, agent folders, any `.`-folder | `_plug`, `.wiki-engine`, any `.`-folder |

## How the index works

`notes.rebuild(con)` walks the folder in one transaction: each `.md` is parsed (frontmatter,
title from the first `# heading`, wikilinks outside fences, checkbox lines) and upserted; each other
file goes in `files`; rows for files no longer on disk are removed; unchanged files (same mtime and
size) are skipped unless `force`. Then every link is resolved in memory (`_BulkResolver`: three
dicts instead of one query per link), which is what keeps the 900-page / 13 MB / 1,600-file wiki at
about one second (limit 60 s, `tests/test_notebook_folder.py` times it). FTS5 is external-content
over `notes`, kept by triggers.

Wikilinks resolve as the wiki engine and Obsidian write them: `[[Path/Page]]` and `[[Path/Page|alias]]`
against the page path (exact, else by suffix); `[[Page]]` by stem, shortest path first;
`[[Page#Heading]]`, `[[Page@pos]]` and `[[Page$anchor]]` with the suffix dropped; `![[file.png]]` and
`![](path/file.png)` against the files table (exact path, else by name, `attachments/` first).

Attachments (`/att/<rel>`) are served by their indexed path only, through `notes.confined_path`: no
`..`, nothing absolute, nothing hidden, and the real path (symlinks resolved) must stay inside the
folder's real path and not be hidden. Every `/att` answer carries `Content-Security-Policy: sandbox`
and `X-Content-Type-Options: nosniff`; at a folder-readonly site html, svg, xml and js download as
`application/octet-stream` instead of rendering (`docs/DECISIONS.md` H5).

The folder is untrusted at home (the wiki is written by many agents), so one rule covers every read
of a file under it, `notes.confined_path`: the index (`import_path`, regular files only), the note,
raw, topic and search routes, `/att`, the folder-mode editor and the export. A path is refused when
any part is dot-prefixed (`.chrome-data/`, `.wiki-engine.auth.json`) or a skipped folder, or when its
real path leaves the folder or lands on such a path, or when it is a regular file with more than one
link (a hard link, which `realpath` cannot see); a file the pass cannot read is unseen. A row an older
index holds for any of these is dropped on the next pass, and search, the switcher, list and completion
skip it meanwhile. At a folder-readonly site raw HTML in a note renders as escaped text; the work store
keeps the reference's rendering (`docs/DECISIONS.md` H1-H6). These key on the home profile
(`config.untrusted_folder()`), not on the read-only mode, so they hold while home is writable (W1).

## Threads and notes

A thread link whose url is `note:<rel>` (or a path inside the folder) opens the note; the note's
Topic hub lists the threads that cite it (`helm_adapter.threads_citing`, one `LIKE` over the links
column), and `GET /api/notes/note` carries its backlinks. The Day timeline is built from the events'
`kind`, `source` and `reply_kind` (`day.timeline_entry`); the reference's English-body parsing is kept only
for a person's `state-change` rows, which the store writes without a sub-kind (a snooze, a mute, a
reopen and a settle are all `state-change` from `owner`).

## Tests (`python3 tests/run_all.py`)

| Suite | Covers | From the reference app |
|---|---|---|
| `test_notebook_notes.py` | the importer: frontmatter, wikilinks, tasks, search, incremental update and delete, rendering | `test_notes.py`, 47/47 |
| `test_notebook_edit.py` | every write rule (C2): save/409/credentials, templates, today's note, appends, toggles, rename with link rewriting, attachments, autocomplete; plus the read-only refusal | `test_edit.py`, 69/69 call sites |
| `test_notebook_delete_route.py` | `POST /api/notes/delete` over HTTP at a store-mode work notebook: the removal with its reason in the history, 400 blank reason, 403 daily and dated-stream notes (under any path spelling), 404 with a JSON detail, the write-route and web-role checks | the `NOTE-DELETE-ROUTE.md` ask |
| `test_notebook_store.py` | store mode: the cutover flip and round trip, export verify/rebuild policies, revisions behind every write, move; plus store mode with no folder | `test_store.py`, 70/101 (the 31 that drove the reference's `vault.py` / `write_block.py` are not ported) |
| `test_notebook_day.py` | the Day timeline from a scratch thread store, the store readers, carry-over, assemble; plus the structured-field rows (C4) | `test_day.py`, 54/55 (the read-only-URI check has no meaning in-process) |
| `test_notebook_adapter.py` | the adapter: list, every action's key filtering and the actor stamp, rejections as HelmError, `threads_citing` | `test_helm_client.py`, 24/33 (the HTTP-only checks: bearer, timeouts, dead Helm) |
| `test_notebook_cutover_on_start.py` | the cutover on a store-mode start, against the real api (uvicorn): folder and folder-readonly never flip; the first store start flips before serving, the second does nothing; an unclean round trip stops the start with nothing changed; a failure after the flip is logged plainly; fresh store DBs and no-folder store mode never flip | new |
| `test_notebook_folder.py` | folder-readonly (C3): index, wikilinks both forms, backlinks, FTS, 405 on every write route, attachment confinement (`..`, absolute, symlink escape), the rescan (on demand and threaded), the timed 900-page index, the home Day view and the `note:` join | new |
| `test_notebook_redirects.py` | moves that keep old paths working (POST /api/notes/move, the `redirects` table): link, read and write of an old path, chains, a real note winning, rebuild, the write log under both paths; POST /api/notes/folder | new (the notebook organizer, 2026-09-26) |
| `test_notebook_file_move.py` | non-note files (images, state files, .md.bak-* copies) moving through POST /api/notes/move: on disk and in the index, path embeds and /att/ of the old path via the redirect, links rewritten, pruning, refusals (a file never becomes a note) | new (the notebook organizer, 2026-09-27) |
| `test_notebook_wiki_bridge.py` | the wiki bridge (W1-W7): escaping and /att by profile in folder mode, mode/owner kept, append racing direct appenders, recent, delete to Trash, the 409 on a moved file, symlinked folders refused, the write log | new |
| `test_notebook_hardening.py` | the folder as untrusted input (review of PR #3): raw HTML escaped in the note and Day views at home and kept on a second site; symlinked notes, folders and `/proc/self/environ` never indexed or served by any read path (and dropped from an older index); dot-prefixed files and folders refused by name, through a link and from an older index; `/att` headers per type at the api and through the web role at both sites; the work export never adopting through a link | new |
