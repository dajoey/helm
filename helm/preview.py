"""What an agent built, shown from the card it reported on: the rules both roles share.

Thread attachments (the attach route, the list, the bytes) already carried a run's files.
Two things were missing, and this module is the shared half of each:

  * a way to open an agent's HTML without handing it the page it is opened from
    (`VIEW_CSP`: the policy `/attachments/<att>/view` is served under, by the api role and
    again by the web role), and one rule for what the screens may preview (`preview_kind`);
  * limits on what an agent may leave on a card (`check_agent_upload`): a type allowlist, a
    per-file cap, a per-thread count and byte cap, and a credential tripwire on its text.
    the owner's own files are never held to these (the share sheet sends anything), and an
    existing producer (card visuals: png, svg, pdf, txt) is inside them.

The numbers are module constants, not settings: changing one is one line and a deploy.
"""
from __future__ import annotations

import os
import re

# The policy an agent's HTML is served under, as a header (a meta tag can be removed by the page
# itself; a header cannot). `sandbox allow-scripts` gives the document an opaque origin: no cookies,
# no storage, no reaching the page it sits in, no popups, no top navigation, no forms. The rest allows
# no request of any kind (fetch, XHR, sockets, beacons, images, fonts, scripts, styles, frames from
# anywhere) and nothing but what the file carries inline.
# What no header can stop: the page navigating ITS OWN frame (`location = ...`, a meta refresh, a link
# click), because the browsers do not implement `navigate-to`. Such a page can send what it holds, or
# what someone types into it, to another site in the address, and can draw a fake sign-in inside its
# frame. So the viewer says so where it is read ("don't type anything into it") and the host page says
# it outside the frame. It cannot read the Helm page or the session.
VIEW_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data: blob:; font-src data:; media-src data:; connect-src 'none'; form-action 'none'; "
            "frame-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
VIEW_HEADERS = {"Content-Security-Policy": VIEW_CSP, "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-store"}

# A page opened as a tab of its own is a top-level document, and a top-level document can send its
# own tab anywhere (the sandbox only stops a FRAME from navigating the window above it). So the
# address that opens in a tab never serves the page itself: it serves this host, a few lines of
# markup that frame the page the way the card does. The page proper is `?raw=1`, and even that is
# answered with the host when a browser says it is a top-level navigation (Sec-Fetch-Dest: document).
WRAPPER_CSP = ("default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; base-uri 'none'; "
               "form-action 'none'; frame-ancestors 'self'")
WRAPPER_HEADERS = {"Content-Security-Policy": WRAPPER_CSP, "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-store",
                   "Vary": "Sec-Fetch-Dest"}
VIEW_HEADERS["Vary"] = "Sec-Fetch-Dest"


def serves_raw(raw_param, fetch_dest) -> bool:
    """True when this request gets the page itself rather than the host. Only `?raw=1`, and not as a
    top-level navigation. A request with no Sec-Fetch-Dest (a script, an old browser) is judged on
    the parameter alone."""
    return str(raw_param or "") == "1" and str(fetch_dest or "").strip().lower() != "document"


UNTRUSTED_NOTE = "A page a run built, in a sandbox: it cannot see Helm or your sign-in. Do not type anything into it."


def wrapper_page(filename) -> bytes:
    """The host page: a line saying what this is, then one sandboxed frame onto the file's raw page.
    The line sits outside the frame, so the page cannot change it."""
    import html
    name = html.escape(str(filename or "file"), quote=True)
    return ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content=\"width=device-width, initial-scale=1\">"
            "<title>%s</title><style>html,body{margin:0;height:100%%;background:#fff}"
            "body{display:flex;flex-direction:column;font:13px/1.3 system-ui,sans-serif}"
            "p{margin:0;padding:6px 10px;background:#fff3cd;color:#4a3b00;border-bottom:1px solid #e0c870;flex:none}"
            "iframe{display:block;border:0;width:100%%;flex:1 1 auto;min-height:0}</style>"
            "<p>%s</p>"
            "<iframe sandbox=\"allow-scripts\" referrerpolicy=no-referrer title=\"%s\" src=\"view?raw=1\"></iframe></html>"
            % (name, html.escape(UNTRUSTED_NOTE), name)).encode("utf-8")


# ---------------------------------------------------------------- what the screens may preview
_DIFF_TYPES = frozenset({"text/x-diff", "text/x-patch", "text/diff", "application/x-patch", "application/x-diff"})
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "text/x-log", "application/json"})
_EXT_KIND = {".html": "html", ".htm": "html", ".diff": "diff", ".patch": "diff",
             ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
             ".txt": "text", ".md": "text", ".log": "text", ".csv": "text", ".json": "text"}


def preview_kind(mime, filename) -> str:
    """'html', 'diff', 'image', 'text' or 'none'. The stored type decides when it names a kind;
    the file name decides only when the type says nothing (empty or octet-stream), so a `.html`
    name over text/plain is text and is never framed. web/preview.js applies the same rule."""
    base = str(mime or "").split(";")[0].strip().lower()
    ext = os.path.splitext(str(filename or ""))[1].lower()
    if base == "text/html":
        return "html"
    if base in _DIFF_TYPES:
        return "diff"
    if base.startswith("image/"):
        return "image"
    if base in _TEXT_TYPES:
        return "diff" if (base == "text/plain" and ext in (".diff", ".patch")) else "text"
    if base in ("", "application/octet-stream"):
        return _EXT_KIND.get(ext, "none")
    return "none"


# ---------------------------------------------------------------- what an agent may leave on a card
AGENT_MAX_FILE = 8 * 1024 * 1024      # a card visual may be up to 8 MB; a run's largest file measured 356 KB
AGENT_MAX_FILES = 100                 # per thread, for the life of the thread (nothing frees room: see DECISIONS G21)
AGENT_MAX_BYTES = 128 * 1024 * 1024   # per thread
AGENT_TYPES = frozenset({
    "text/html", "text/plain", "text/markdown", "text/csv", "application/json",
    "text/x-diff", "text/x-patch", "application/pdf",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"})
# Types whose bytes are read as text for the credential tripwire. Images and PDFs are not.
_SCANNED = frozenset({"text/html", "text/plain", "text/markdown", "text/csv", "application/json", "text/x-diff", "text/x-patch"})


def is_agent(uploader) -> bool:
    return str(uploader or "agent").strip().lower() != "owner"


_KEYWORDS = re.compile(rb"(?i)password|passwd|pwd|passphrase|secret|token|api[_-]?key|access[_-]?key|akia|sk-|xox[bap]-|ghp-|gho-|glpat-|bearer|private key|eyj|://")
_HIGH = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API key", re.compile(r"(?<![\w.-])(?:sk|xox[bap]|ghp|gho|glpat)-(?=[A-Za-z0-9_-]{0,200}\d)[A-Za-z0-9_-]{16,200}")),
    ("signed token (JWT)", re.compile(r"\beyJ[A-Za-z0-9_-]{8,512}\.[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{8,512}")),
    ("bearer token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,512}")),
    ("connection string", re.compile(r"(?i)\b(?:pwd|password)\s{0,20}=\s{0,20}[^;\s]{1,200}\s{0,20};")),   # bounded: an unbounded run backtracks quadratically
    ("URL with a password", re.compile(r"://[^/\s:@\"'<>]+:[^/\s@\"'<>]+@")),
)
# An assignment of a value to a secret-looking name. A quoted literal is a hit at 6 characters or more
# when it carries a digit or a symbol (16 or more of anything). A bare value is a hit when it carries a
# digit and is long enough (6 for a password, 8 for the rest), and is not code (a call `f(x)`, a dotted
# reference `process.env.KEY`, a snake_case or kebab-case identifier) or a placeholder (`<TOKEN>`,
# `${TOKEN}`, `$TOKEN`, REDACTED, xxx, ***). Prose (`Password: required`, `Token: invalid_grant`) and
# code (`token = get_access_token(request)`) pass; a password with no digit or symbol is a miss, as
# is any tripwire.
_NAMES = r"(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|client[_-]?secret|access[_-]?key)"
_ASSIGN = re.compile(r"(?i)(?<![A-Za-z0-9])(?P<name>" + _NAMES + r")(?![A-Za-z0-9])[\"']?\s*[:=]\s*(?:(?P<q>[\"'])(?P<lit>[^\"'\n]{6,200})(?P=q)|(?P<val>[^\s\"'<>{}\[\]]{6,}))")
_CODE_REF = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_IDENT = re.compile(r"[a-z]+(?:[_-][a-z]+)*[_-](?:[a-z]+|\d+)")   # snake_case / kebab-case words, at most a numeric last part (color-primary-500)
_DOTTED_KEY = re.compile(r"[A-Za-z_][\w-]*(?:\.[\w-]+)+")                         # i18n.errors.required
_DATEISH = re.compile(r"\d{4}-\d\d-\d\d|\d+")                                    # 2024-10-14, 20241014
_PLACEHOLDER = re.compile(r"\$[A-Za-z_{(]\w*\}?|[A-Z_][A-Z0-9_]*|(?i:redacted\w*|x{3,})|\*{3,}")


def _secret_shape(line: str):
    for name, rx in _HIGH:
        m = rx.search(line)
        if m:
            return "%s (%s)" % (name, re.sub(r"\S", "x", m.group(0))[:40])
    for m in _ASSIGN.finditer(line):
        pw = re.match(r"(?i)(password|passwd|pwd|passphrase)$", m.group("name")) is not None
        lit, val = m.group("lit"), m.group("val")
        if lit is not None:
            if (_PLACEHOLDER.fullmatch(lit) or (_DOTTED_KEY.fullmatch(lit) and not re.search(r"\d{3}", lit)) or re.search(r"\s|^<.*>$", lit)
                    or (len(lit) < 16 and not re.search(r"[\d\W]", lit))):
                continue   # prose and message strings have spaces; a literal secret has none
            return "password or token (%s)" % re.sub(r"\S", "x", m.group(0))[:40]
        val = val.rstrip(".,;:)")
        if ("(" in val or line[m.end():m.end() + 1] == "(" or (_DATEISH.fullmatch(val) and not (pw and val.isdigit())) or _IDENT.fullmatch(val)
                or _PLACEHOLDER.fullmatch(val) or (_CODE_REF.fullmatch(val) and not re.search(r"\d{3}", val))):
            continue
        if re.search(r"\d", val) and len(val) >= (6 if pw else 8):
            return "password or token (%s)" % re.sub(r"\S", "x", m.group(0))[:40]
    return None


def secret_line(data: bytes):
    """(line number, shape) of the first credential-shaped line in these bytes, or None. The shape
    is the pattern's name with the characters masked; the value is never returned. Judged on the
    values, not the words: a page about a login form, or a diff that reads a token from the
    environment, is not a credential. Pure and slow on a big file: callers run it off the event loop."""
    if not _KEYWORDS.search(data):
        return None
    text = data.decode("utf-8", "replace")
    for n, line in enumerate(text.split("\n"), 1):
        if len(line) > 20000:   # a minified file: cut it where a token could still sit
            for i in range(0, len(line), 20000):
                shape = _secret_shape(line[i:i + 20400])
                if shape:
                    return n, shape
            continue
        shape = _secret_shape(line)
        if shape:
            return n, shape
    return None


def check_agent_upload(clean: dict, data: bytes, thread_count: int, thread_bytes: int) -> list:
    """Why this agent upload is refused, as plain sentences (empty list: it is fine).

    `thread_count` / `thread_bytes` are what agents already left on the thread, not counting this
    file. The caller skips this for the owner's own files and for a re-send of bytes the thread holds."""
    errs = []
    name = clean.get("filename") or "file"
    base = str(clean.get("mime") or "").split(";")[0].strip().lower()
    if base not in AGENT_TYPES:
        errs.append("%r is %s, which is not a type an agent is allowed to attach. Allowed: %s"
                    % (name, base or "untyped", ", ".join(sorted(AGENT_TYPES))))
    size = len(data)
    if size > AGENT_MAX_FILE:
        errs.append("%r is %d KB, over the %d KB limit for a file an agent attaches"
                    % (name, size // 1024, AGENT_MAX_FILE // 1024))
    if thread_count + 1 > AGENT_MAX_FILES:
        errs.append("this thread already holds %d files from agents, the limit is %d: attach fewer, or fold files together"
                    % (thread_count, AGENT_MAX_FILES))
    if thread_bytes + size > AGENT_MAX_BYTES:
        errs.append("this thread's agent files would total %.1f MB, over the %d MB thread limit"
                    % ((thread_bytes + size) / (1024 * 1024), AGENT_MAX_BYTES // (1024 * 1024)))
    return errs


def scan_agent_upload(clean: dict, data: bytes) -> list:
    """The credential tripwire, for the types read as text. Separate from check_agent_upload because it
    costs about a second on an 8 MB file: the route runs it in a worker thread."""
    base = str(clean.get("mime") or "").split(";")[0].strip().lower()
    if base not in _SCANNED:
        return []
    hit = secret_line(data)
    if hit:
        return ["%r has a credential-shaped line (line %d, %s): files on a card never carry credentials. "
                "Redact it and attach again" % (clean.get("filename") or "file", hit[0], hit[1])]
    return []

