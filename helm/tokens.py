"""Per-caller tokens: a `tokens` table plus a CLI.

Today one HELM_APP_TOKEN per site is shared by every client (the producers, the
mobile app, the sweeps). That token stays valid, so nothing changes at cutover. This
adds a way for each caller to get its own token, so a leaked one can be revoked alone
and so writes can say who made them: the token's principal is recorded on every event
the request writes (in `source_id` when the event carries none; the `source` column
keeps its meaning of owner / bot / helm that every store rule reads).

    python3 -m helm.tokens add <principal>      mints a token, prints it ONCE
    python3 -m helm.tokens list                 principals, created, revoked, last used
    python3 -m helm.tokens revoke <principal>   the token stops working on the next request

Only the SHA-256 of a token is stored. HELM_DB names the database (the site's data dir).
The auth gate caches the live hashes for 15 s to avoid opening the database on every
request, and confirms each hit with one indexed SELECT, so a revoke from another process
is honoured on the very next request.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import time

DDL = """
CREATE TABLE IF NOT EXISTS tokens (
  principal  TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  created    REAL NOT NULL,
  revoked    REAL,
  last_used  REAL
);
"""

# The principal behind the current request, read by helm.store.add_event.
PRINCIPAL: contextvars.ContextVar = contextvars.ContextVar("helm_principal", default="")

PRINCIPAL_RE_HINT = "letters, digits, '-', '_' and '.', 2-64 chars"
_CACHE_TTL_S = 15.0
_cache = {"at": 0.0, "hashes": {}}


def init(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_principal(p: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}", p or ""))


def add(con, principal: str) -> str:
    """Mint a token for a principal. One live token per principal: re-adding rotates it."""
    if not _valid_principal(principal):
        raise ValueError("principal must be %s" % PRINCIPAL_RE_HINT)
    init(con)
    tok = secrets.token_urlsafe(32)
    now = time.time()
    con.execute(
        "INSERT INTO tokens (principal, token_hash, created, revoked, last_used) VALUES (?,?,?,NULL,NULL)"
        " ON CONFLICT(principal) DO UPDATE SET token_hash=excluded.token_hash, created=excluded.created,"
        " revoked=NULL, last_used=NULL", (principal, _hash(tok), now))
    con.commit()
    _cache["at"] = 0.0
    return tok


def revoke(con, principal: str) -> bool:
    init(con)
    cur = con.execute("UPDATE tokens SET revoked=? WHERE principal=? AND revoked IS NULL",
                      (time.time(), principal))
    con.commit()
    _cache["at"] = 0.0
    return cur.rowcount == 1


def list_tokens(con) -> list[dict]:
    init(con)
    return [dict(r) for r in con.execute(
        "SELECT principal, created, revoked, last_used FROM tokens ORDER BY principal")]


def _live_hashes(open_db, force: bool = False) -> dict:
    """token_hash -> principal for every live token, cached briefly so the auth gate does
    not open the database on every request. `force` re-reads regardless of the cache age."""
    now = time.time()
    if not force and now - _cache["at"] < _CACHE_TTL_S:
        return _cache["hashes"]
    hashes = {}
    try:
        con = open_db()
        try:
            init(con)
            for r in con.execute("SELECT principal, token_hash FROM tokens WHERE revoked IS NULL"):
                hashes[r["token_hash"]] = r["principal"]
        finally:
            con.close()
    except Exception:
        # A broken table must not lock the legacy token out; it only disables per-caller ones.
        hashes = {}
    _cache["at"], _cache["hashes"] = now, hashes
    return hashes


def principal_for(presented: str, legacy_token: str, open_db) -> str | None:
    """Who presented this bearer: "" for the site's HELM_APP_TOKEN, a principal for a
    per-caller token, None when it matches nothing. Constant-time on the legacy token."""
    try:
        if legacy_token and hmac.compare_digest(presented.encode("utf-8"), legacy_token.encode("utf-8")):
            return ""
    except (AttributeError, UnicodeError):
        return None
    if not presented:
        return None
    h = _hash(presented)
    for retry in (False, True):
        live = _live_hashes(open_db, force=retry)
        for known, principal in live.items():
            if hmac.compare_digest(h, known):
                # A hit is re-checked against the table so a revoke from the CLI (another
                # process) takes effect on the next request, not after the cache window.
                if _still_live(open_db, known):
                    return principal
                _cache["at"] = 0.0
                return None
        # A miss re-reads the table once: a token minted or rotated by the CLI in another
        # process is good on its first use, not after the cache window. A wrong bearer
        # costs one table read, which is the same price as the hit path pays.
    return None


def _still_live(open_db, token_hash: str) -> bool:
    try:
        con = open_db()
        try:
            r = con.execute("SELECT 1 FROM tokens WHERE token_hash=? AND revoked IS NULL",
                            (token_hash,)).fetchone()
            return r is not None
        finally:
            con.close()
    except Exception:
        return False


def touch(open_db, principal: str) -> None:
    """Record last use, best effort, at most once per cache window."""
    key = "touched:" + principal
    if time.time() - _cache.get(key, 0.0) < 60.0:
        return
    _cache[key] = time.time()
    try:
        con = open_db()
        try:
            con.execute("UPDATE tokens SET last_used=? WHERE principal=?", (time.time(), principal))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _open():
    from helm import store
    return store.db(os.environ.get("HELM_DB") or store.DB_PATH)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("add", "list", "revoke"):
        print("usage: python3 -m helm.tokens add|list|revoke <principal>", file=sys.stderr)
        return 2
    con = _open()
    try:
        if argv[0] == "list":
            rows = list_tokens(con)
            if not rows:
                print("no per-caller tokens; the site's HELM_APP_TOKEN is the only bearer")
            for r in rows:
                state = "revoked %s" % time.strftime("%Y-%m-%d", time.localtime(r["revoked"])) if r["revoked"] else "live"
                used = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_used"])) if r["last_used"] else "never used"
                print("%-24s %-22s created %s  %s" % (r["principal"], state,
                      time.strftime("%Y-%m-%d", time.localtime(r["created"])), used))
            return 0
        if len(argv) < 2:
            print("a principal is required", file=sys.stderr)
            return 2
        if argv[0] == "add":
            tok = add(con, argv[1])
            print("token for %s (shown once, never stored in clear):" % argv[1], file=sys.stderr)
            print(tok)
            return 0
        ok = revoke(con, argv[1])
        print("revoked %s" % argv[1] if ok else "no live token for %s" % argv[1])
        return 0 if ok else 1
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
