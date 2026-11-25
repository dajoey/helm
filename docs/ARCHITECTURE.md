# Architecture

Two processes and one database.

```
producer (curl, cron, agent)            browser
        |  bearer token                    |  login header (from the proxy)
        v                                  v
   api role  :3038  <--- proxy /api/* ---  web role :3037
   app.service:app                         app.web:app
        |                                       |
        v                                       v
   helm/store.py  (SQLite)                web/ (static ES modules)
   notebook/      (markdown + index)
```

## The api role (`app/service.py`)

Everything that holds state. `helm/store.py` owns the schema and the rules: threads, asks, events,
claims, attachments, the four-test gate (`helm/gate_rules.py`) and the card-shape rules
(`helm/card_rules.py`). Auth is a bearer token (`HELM_APP_TOKEN` or a per-caller token minted with
`python3 -m helm.tokens`), so every write is recorded as a principal. The notebook
(`notebook/`, `app/notebook_api.py`) lives in the same process and the same data directory.

## The web role (`app/web.py`)

Serves `web/` and proxies an allowlist of `/api/*` and `/att/*` to the api role, adding the bearer
token server-side, so the browser never holds one. It takes the person's login from the header the
proxy sets, and believes it only from the peers in `HELM_TRUSTED_PROXIES`. With `HELM_DEV_LOGIN`
set it acts as that login with no proxy at all — refused unless the bind address is loopback, which
is what makes the quick start safe.

## The front end (`web/`)

Plain ES modules, no build step. `modules.js` is the section registry: a section appears in the rail
only when its module is on (`/api/meta`), and each section's file registers its own route, list and
page. `shell.js` owns routing, the three panes and the keyboard map; `app.js` is the data layer for
threads; `conversation.js` draws a thread; `notebook/` is the note page. Nothing polls faster than a
minute, and a page keeps what a person typed when a read lands on top of it.

## Where the rules live

A rule that a machine can check is code, not prose:

| Rule | Where |
|---|---|
| whether a card may reach a person | `helm/gate_rules.py` (the four tests) |
| the shape of an ask | `helm/store.py` (validation) + `helm/card_rules.py` |
| who may answer | `app/web.py` (the login header) + `HELM_OWNER` |
| what a deployment switches on | `helm/site.py`, `notebook/config.py` (both read the environment once) |

## Tests

`tests/run_all.py` discovers every suite and runs each in its own process. The suites fall into
three kinds: pure unit suites over the store and the notebook, HTTP suites that drive the app
in-process (`starlette.testclient`, told it is on loopback because the web role checks its peer),
and browser suites that bring a real deployment up on loopback (`tools/screens/fixture.py`) and
drive it with Playwright.
