"""Markdown to HTML for the notebook, on the server. the reference's app/render.py.

Wikilinks resolve to in-app links where a note exists and to plain text where
none does; image embeds resolve into the attachments route; checkbox lines
render as disabled checkboxes. Everything else is markdown-it-py's CommonMark
plus tables and strikethrough, with single newlines kept as line breaks the
way Obsidian and the wiki engine show them.

Raw HTML in a note: in the wiki profile (the wiki engine's folder, written by many
agents and read in an origin whose web role proxies the API with the bearer
injected) it is escaped and shown as text in every mode, read-only or writable,
so a page can never run script in the Helm origin. In the fixed profile it renders as the
reference app rendered it (docs/DECISIONS.md H1, W1). The notebook's own markup (wikilinks,
embeds, checkboxes) is the same either way.
"""
from __future__ import annotations

import html
import os
import re
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.token import Token

from . import config, notes

TASK_PREFIX_RE = re.compile(r"^\[( |x|X)\]\s+")
SIZE_RE = re.compile(r"^(\d+)(?:x(\d+))?$")


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def att_url(rel: str) -> str:
    return "/att/" + quote(rel, safe="")


def note_url(rel: str) -> str:
    return "#/note/" + quote(rel, safe="")


class Resolver:
    """Wikilink targets against the notes index, memoised per render."""

    def __init__(self, con):
        self.con = con
        self._notes = {}
        self._files = {}

    def note(self, target: str):
        if target not in self._notes:
            self._notes[target] = notes.resolve_note(self.con, target)
        return self._notes[target]

    def file(self, name: str):
        if name not in self._files:
            self._files[name] = notes.resolve_file(self.con, name)
        return self._files[name]


def split_inner(inner: str):
    head, _, alias = inner.partition("|")
    target, _, heading = head.partition("#")
    return target.strip(), heading.strip(), alias.strip()


def link_target(r: Resolver, inner: str):
    """What the text inside [[ ]] opens, by the rule a rendered link follows: ("file", rel) for a
    target that names a non-note file, ("note", rel) for a note, else (None, None). The editor's
    Ctrl+click asks this, so it lands where a click in the read view does."""
    target = split_inner(inner)[0]
    if not target:
        return None, None
    ext = os.path.splitext(target)[1].lower()
    if ext and ext != ".md":
        rel = r.file(target)
        if rel:
            return "file", rel
    rel = r.note(target)
    return ("note", rel) if rel else (None, None)


def _wikilink_rule(state, silent: bool) -> bool:
    src = state.src
    pos = state.pos
    if src.startswith("![[", pos):
        embed, start = True, pos + 3
    elif src.startswith("[[", pos):
        embed, start = False, pos + 2
    else:
        return False
    end = src.find("]]", start)
    if end < 0:
        return False
    inner = src[start:end]
    if not inner.strip() or "\n" in inner or "[[" in inner:
        return False
    if not silent:
        tok = state.push("wikilink", "", 0)
        tok.content = inner
        tok.meta = {"embed": embed}
    state.pos = end + 2
    return True


def _render_wikilink(self, tokens, idx, options, env) -> str:
    tok = tokens[idx]
    inner = tok.content
    embed = bool(tok.meta.get("embed"))
    target, heading, alias = split_inner(inner)
    r = env.get("resolver")
    ext = os.path.splitext(target)[1].lower()
    label = alias or (target + ("#" + heading if heading else ""))

    if embed and ext in notes.IMAGE_EXT:
        rel = r.file(target) if r else None
        if rel:
            size = ""
            m = SIZE_RE.match(alias)
            if m:
                size = ' width="%s"' % m.group(1)
                if m.group(2):
                    size += ' height="%s"' % m.group(2)
            return '<img class="embed" src="%s" alt="%s" loading="lazy"%s>' % (
                att_url(rel), esc(target), size)
        return '<span class="wl missing" title="not found in the notebook">%s</span>' % esc(
            "![[" + inner + "]]")

    if ext and ext != ".md":
        rel = r.file(target) if r else None
        if rel:
            return '<a class="wl file" href="%s" target="_blank" rel="noopener">%s</a>' % (
                att_url(rel), esc(label))
        # No such file: a note's own name may end like an extension (owner@example.com).
        if not (r and r.note(target)):
            return '<span class="wl missing" title="not found in the notebook">%s</span>' % esc(label)

    rel = r.note(target) if r else None
    if rel:
        return '<a class="wl" href="%s" data-rel="%s">%s</a>' % (
            note_url(rel), esc(rel), esc(label))
    return '<span class="wl missing" title="no note with this name">%s</span>' % esc(label)


def _tasklist_core(state) -> None:
    tokens = state.tokens
    for i, tok in enumerate(tokens):
        if tok.type != "inline" or not tok.children or i < 2:
            continue
        if tokens[i - 1].type != "paragraph_open" or tokens[i - 2].type != "list_item_open":
            continue
        first = tok.children[0]
        if first.type != "text":
            continue
        m = TASK_PREFIX_RE.match(first.content)
        if not m:
            continue
        done = m.group(1) != " "
        first.content = first.content[m.end():]
        line = (tokens[i - 2].map or [0])[0] + 1
        cb = Token("html_inline", "", 0)
        cb.content = '<input type="checkbox" class="task-box" data-line="%d"%s> ' % (
            line, " checked" if done else "")
        tok.children.insert(0, cb)
        tokens[i - 2].attrJoin("class", "task done" if done else "task")


def _render_image(self, tokens, idx, options, env) -> str:
    tok = tokens[idx]
    src = str(tok.attrGet("src") or "")
    if src and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", src):
        r = env.get("resolver")
        rel = r.file(src) if r else None
        if rel:
            tok.attrSet("src", att_url(rel))
    tok.attrSet("loading", "lazy")
    return self.image(tokens, idx, options, env)


def _render_link_open(self, tokens, idx, options, env) -> str:
    tok = tokens[idx]
    href = str(tok.attrGet("href") or "")
    if re.match(r"^https?://", href, re.I):
        tok.attrSet("target", "_blank")
        tok.attrSet("rel", "noopener")
    return self.renderToken(tokens, idx, options, env)


def make_md(raw_html: bool = True) -> MarkdownIt:
    """The renderer. `raw_html` is markdown-it's `html` option: True passes raw HTML
    through (the reference's rendering); False escapes every raw tag and shows it as text.
    The checkbox this module inserts is its own token and renders either way."""
    md = MarkdownIt("commonmark", {"html": raw_html, "breaks": True, "linkify": False,
                                   "typographer": False})
    md.enable(["table", "strikethrough"])
    md.inline.ruler.before("link", "wikilink", _wikilink_rule)
    md.core.ruler.after("inline", "tasklist", _tasklist_core)
    md.add_render_rule("wikilink", _render_wikilink)
    md.add_render_rule("image", _render_image)
    md.add_render_rule("link_open", _render_link_open)
    return md


_MD = make_md(raw_html=True)         # the fixed profile: the reference's rendering
_MD_TEXT = make_md(raw_html=False)   # the wiki profile (in any mode): raw HTML is text


def render(body: str, resolver: Resolver | None = None) -> str:
    """A note body as HTML. Raw HTML is escaped where the folder is untrusted (the wiki
    profile, whatever the mode) and rendered in the fixed profile (see the module docstring)."""
    md = _MD_TEXT if config.untrusted_folder() else _MD
    return md.render(body or "", {"resolver": resolver})
