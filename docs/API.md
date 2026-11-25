# API

Two roles. **Producers** talk to the API role (`:3038` in the quick start) with
`Authorization: Bearer <HELM_APP_TOKEN>`. **The browser** is served by the web role (`:3037`),
which proxies an allowlist of `/api/*` and `/att/*` to the API role with the token injected
server-side, and signs the person in — a request through the web role carries no token.

Everything is JSON. An unknown path is a 404, a bad body a 400, a missing token a 401, and a
refused card comes back with the reason in `rejected[].errors`, so a producer can fix its own
payload and re-`PUT` just those threads.

## Opening and reading

| Route | What it does |
|---|---|
| `GET /api/meta` | the service, its version, the modules, the owner principal |
| `GET /api/health` | liveness for a monitor |
| `GET /api/status` | the store's own summary: counts, the dispatcher heartbeat, the last runs |
| `GET /metrics` | Prometheus metrics |
| `GET /api/triage/threads` | the list, filtered (`state`, `category`, `urgency`, `q`, `from`, `limit`) |
| `GET /api/triage/threads/{id}` | one thread: its ask, its events, its links and attachments |
| `GET /api/triage/threads/{id}/conversation` | the same thread as the timeline the screens draw |
| `POST /api/triage/needs-owner-check` | ask the gate about a card **before** filing it: body `{"thread": { … }}` |

## Filing

`PUT /api/triage/threads` takes `{"threads": [ ... ]}` and answers
`{"ok": true, "accepted": n, "created": n, "updated": n, "rejected": [...]}`. A `PUT` of a known
`thread_id` updates it, so a producer can be idempotent. A card is described by:

| Field | Notes |
|---|---|
| `thread_id` | stable id; `[a-z0-9][a-z0-9._-]*` |
| `title`, `summary` | the summary should say why it is the reader's |
| `category`, `urgency` | `urgency` is `now`, `this-week` or `whenever` |
| `state` | `needs-you` to land in the queue, `waiting-external` with `blocked_on` + `recheck_at` to park |
| `intent` | `mine` (the default: the owner's, lit with one option, Done), `agent` (hand it straight to a run — then `instruction` is required and is the run's brief), `waiting` (on someone else; not lit) |
| `ask` | see below |
| `sources`, `links` | where it came from, what it points at (`{"url": "note:Folder/Page"}` links a note) |
| `events` | the history that came with it |

An `ask` is one of:

```json
{"kind": "choice", "subject": "…", "recap": "…", "prompt": "…?", "context": "…",
 "requires": ["decision"], "options": [{"label": "…", "value": "…", "effect": "…"}]}
{"kind": "text",   "subject": "…", "recap": "…", "prompt": "What should it be called?"}
{"kind": "ack",    "subject": "…", "recap": "…", "prompt": "Read the report and close it",
 "options": [{"label": "Got it", "value": "ack", "effect": "Closes the card"}]}
{"kind": "none"}
```

`requires` names the only-the-owner-can capability the next step needs — `decision`, `funds`,
`credential`, `presence` or `contact`. The gate refuses a `needs-you` card whose ask names none of
them (ask the gate first with `POST /api/triage/needs-owner-check`). An option's `effect` is what
the reader gets by tapping it, so it is written for them, not for the machine.

## Working and answering

| Route | What it does |
|---|---|
| `POST /api/triage/threads/{id}/claims` | an executor takes the thread (`executor_id`, `runtime`) |
| `POST /api/triage/threads/{id}/claims/{claim}/result` | the run's result: `outcome`, `note`, an optional new `state` and `ask` |
| `POST /api/triage/threads/{id}/reply` | the answer: `{"value": "<option value>"}`, or `{"note": "…"}` |
| `POST /api/triage/threads/{id}/note` | a note on the thread without answering it |
| `POST /api/triage/threads/{id}/close` | close it (`reason`) |
| `POST /api/triage/threads/{id}/park` | wait on someone else (`blocked_on`, `recheck_at`), and `…/recheck` to come back |
| `POST /api/triage/threads/{id}/backlog` | keep it, out of the queue, with a reason |
| `POST /api/triage/threads/{id}/attach` | attach a file (`filename`, `mime`, `content_base64`) |
| `POST /api/triage/dispatcher-heartbeat` | what a runner is doing: `{"ts": …, "runs": […]}` |
| `GET /api/triage/gate?days=14` | what the gate held or dropped recently |

## Notebook

`GET /api/notes/{folders,list,note,raw,history,tail,changes,graph,tasks,atlas}` and
`GET /api/notes/search?q=` read. `POST /api/notes/new` creates, `PUT /api/notes/note` replaces,
`POST /api/notes/append` appends, and `POST /api/notes/{rename,move}` and
`DELETE /api/notes/note` change and retire. The web role serves the same routes to the browser; a
producer with the token can read and write notes with them.

## Tokens

`HELM_APP_TOKEN` is the deployment's own token. A per-caller token is minted inside the container,
against the container's own database:

```sh
docker compose exec api python3 -m helm.tokens add <principal>     # prints the token once
docker compose exec api python3 -m helm.tokens list
docker compose exec api python3 -m helm.tokens revoke <principal>
```

A reply or a result sent with a per-caller token is recorded under that name, which is what puts
"who says so" on the record.
