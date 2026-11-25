# Setup

## The two roles

One image, two roles (`HELM_ROLE`):

- **api** — the store and the triage routes. Bearer auth (`HELM_APP_TOKEN` or a per-caller token).
  Producers talk to it. It does not need to be reachable by a browser.
- **web** — the screens, and a same-origin proxy that injects the token for the allowlisted
  `/api/*` and `/att/*` routes. Nothing else should be able to reach it: it trusts a login header.

Put an authenticating proxy in front of **web** (Traefik, Caddy, nginx + oauth2-proxy, Authelia,
Authentik, …). The proxy authenticates the person and sets the login header; Helm believes that
header only when the request's peer address is inside `HELM_TRUSTED_PROXIES`.

If nobody can answer a card unless they are the owner: the web role signs the person in as the
header's value, and only a value equal to `HELM_OWNER` may answer a card (everyone else can read).

## Tokens

```sh
# the deployment's own token: what a cron job or a script uses
openssl rand -hex 32          # -> HELM_APP_TOKEN in .env (0600, never in git)

# a token per producer, so the record says who did it. Run it inside the container, against the
# container's own database; on the host it would open a different, empty store.
docker compose exec api python3 -m helm.tokens add backup-job
docker compose exec api python3 -m helm.tokens list
docker compose exec api python3 -m helm.tokens revoke backup-job
```

`HELM_BEARER_STRICT_PRINCIPALS` (comma-separated token names) forces those callers to be recorded
as themselves even when they claim another actor.

## The notebook

`HELM_NOTES_ROOT` is a folder of markdown notes; the notebook indexes it into `HELM_NOTES_DB`.

- `HELM_NOTES_MODE=folder-readonly` (default) — index the folder and write nothing.
- `HELM_NOTES_MODE=store` — Helm keeps its own copy; edits, moves and the editor write to the store
  and the folder is an export.
- `HELM_NOTES_MODE=folder` — the folder is the truth and the editor writes files directly.
- `HELM_NOTES_PROFILE=wiki` (default) — folders are whatever top-level folders the space holds, the
  daily note is `Journal/Day/YYYY-MM-DD.md`.
- `HELM_NOTES_PROFILE=fixed` — a declared vault shape: a fixed folder list, a template per folder,
  the daily note under `Daily/YYYY/YYYY-MM/`, and contact-name / ticket-number matching in the topic
  hub. Both profiles are generic descriptions of a folder of notes; neither is required.
- `HELM_NOTES_VAULT_ALIASES` — folders a link may still name a note by (an old path); `;`-separated.

## Backups

Three things hold state, and all three matter:

1. `HELM_DATA` (the volume in the compose files) — `triage.db`, `notes.db` and `attachments/`.
2. The notes folder, in `store` mode too: it is what a person edits.
3. `.env` — the tokens. Keep it outside the repo, mode 0600.

A consistent copy is a file copy while the api role is stopped. The volume below is the one
`deploy/compose.yml` declares (`helm_helm-data`, named after the compose project `helm`); the quick
start's volume is `helm-quickstart_helm-quickstart-data`. Run this from `deploy/`:

```sh
docker compose stop api
docker run --rm -v helm_helm-data:/data -v "$PWD:/backup" alpine tar czf /backup/helm-data.tgz -C /data .
docker compose start api
```

The SQLite files are in WAL mode: copy `-wal` and `-shm` too if the store was running (the tar
above takes the whole directory, which is why the role is stopped).

## The quality gate

`HELM_CARD_RULES` decides which card-shape rules are enforced: `rule=reject`, `rule=log` or
`rule=off`, comma-separated (see `helm/card_rules.py`; the current state is at
`GET /api/triage/card-rules`). Two are worth rejecting from the start:
`no-subject-recap=reject,bookkeeping-jargon=reject`.

The four-test gate itself (`helm/gate_rules.py`) is on by default: only-the-owner-can,
still-true-now, they-do-not-already-know, no-agent-can-fix. `GET /api/triage/gate?days=14` shows
what it held. The cold read — a model that judges whether an ask names a step only a person can do —
is optional; without `HELM_COLD_READ_JUDGE_URL`/`_MODEL`/`_KEY` a card passes unchecked and the api
role logs that it did.

## Every environment variable

| Variable | Default | What it does |
|---|---|---|
| `HELM_ROLE` | `api` | `api` or `web` |
| `HELM_HOST` / `HELM_PORT` | `0.0.0.0` / `3038` | where uvicorn binds |
| `HELM_API_URL` | `http://127.0.0.1:3038` | web role: where to proxy `/api/*` |
| `HELM_APP_TOKEN` | — | the deployment's bearer token |
| `HELM_OWNER` | `owner` | the principal that may answer a card |
| `HELM_AUTH_HEADER` | `X-Forwarded-User` | web role: the login header the proxy sets |
| `HELM_TRUSTED_PROXIES` | `127.0.0.1/32` | web role: the CIDRs the header is believed from |
| `HELM_DEV_LOGIN` | — | web role: act as this login with no proxy; refused unless the bind is loopback |
| `HELM_WEB_PRESS_KEY` | — | the web role proves to the api that a press came from a signed-in page |
| `HELM_WEB_REQUIRE_AUTH_HEADER` | `1` | web role: refuse anything without the login header |
| `HELM_MODULES` | `inbox,conversations,notebook,activity` | which sections exist |
| `HELM_DATA` | `data` | the data directory |
| `HELM_DB` | `$HELM_DATA/triage.db` | the thread store |
| `HELM_ATTACH_DIR` | `$HELM_DATA/attachments` | attachment files |
| `HELM_DISPATCHER_HB_FILE` | `$HELM_DATA/dispatcher-heartbeat.json` | the runner's heartbeat |
| `HELM_NOTES_*` | see above | the notebook |
| `HELM_CARD_RULES` | all in log mode | which card-shape rules are enforced |
| `HELM_BEARER_STRICT_PRINCIPALS` | — | tokens always recorded as themselves |
| `HELM_COLD_READ_JUDGE_URL` / `_MODEL` / `_KEY` | — | the optional cold-read judge; without them a card passes unchecked |
| `HELM_SENTRY_DSN`, `HELM_SENTRY_ENV` | — | error reporting; the press key and tokens are scrubbed |
| `HELM_BUILD_SHA` | `unknown` | stamped into the image and reported by `/api/meta` |

## Running it without Docker

```sh
pip install -r requirements.txt
export HELM_APP_TOKEN=$(openssl rand -hex 32) HELM_OWNER=owner
python3 -m uvicorn app.service:app --host 127.0.0.1 --port 3038
HELM_ROLE=web HELM_DEV_LOGIN=owner HELM_HOST=127.0.0.1 HELM_PORT=3037 \
  HELM_API_URL=http://127.0.0.1:3038 python3 -m uvicorn app.web:app --host 127.0.0.1 --port 3037
```
