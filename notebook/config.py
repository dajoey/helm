"""The notebook's profile and environment: every shape a deployment can take, in one place.

A notebook is a folder of markdown notes plus an index cache (notes.db). The **profile** says how
that folder is read, chosen by HELM_NOTES_PROFILE (default `wiki`):

    wiki    a wiki space: the folders are whatever top-level folders the space holds, no
            templates, the daily note at Journal/Day/YYYY-MM-DD.md, and the Day view is the
            thread timeline plus the pages whose mtime falls on that day.
    fixed   a vault with a declared shape: ten folders (four of them nested), four templates, the
            daily note at Daily/YYYY/YYYY-MM/YYYY-MM-DD.md, name and ticket-number matching in the
            Topic hub, and the "Calls" category in the Day view.

Paths and modes, all from the environment:

    HELM_NOTES_ROOT     the folder of markdown notes
    HELM_NOTES_DB       the note store / index cache (default $HELM_DATA/notes.db)
    HELM_DATA           the data directory (default data/)
    HELM_DB             the thread store the adapter opens in-process
    HELM_NOTES_MODE     store | folder-readonly | folder   (default helm.site.notes_mode())
    HELM_NOTES_DAILY_ROOT   where the daily notes live (the profile's own by default)
    HELM_NOTES_VAULT_ALIASES  the folders a link may name a note by besides the notes folder,
                        `;`-separated: a link to a path under one is a link to the note.

`folder` is the phase-1 mode (the folder is truth and the editor writes files). It is not the
default but is kept because the suites and the cutover's reversal path run in it; see
notebook/README.md.
"""
from __future__ import annotations

import os
import re

from helm import site as _site

HERE = os.path.dirname(os.path.abspath(__file__))
DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MODES = ("store", "folder-readonly", "folder")
PROFILES = ("wiki", "fixed")

# Folders whose contents are never notes: editors' and agents' own state, build junk.
# Any folder whose name starts with "." is skipped as well (see notes.in_skipped_dir).
COMMON_SKIP = frozenset({"__pycache__", "node_modules", "_scratch", "copilot"})
FIXED_SKIP = COMMON_SKIP | {".notes", ".trash", ".git"}
WIKI_SKIP = COMMON_SKIP | {"_plug", ".wiki-engine", ".git"}   # a wiki engine's own folder
# Top-level folders only (a folder of that name deeper down is an ordinary one).
WIKI_SKIP_TOP = frozenset()


class Profile:
    """One notebook shape. Attributes are plain data so a test can override one."""

    def __init__(self, name: str = "wiki"):
        if name not in PROFILES:
            raise RuntimeError("HELM_NOTES_PROFILE must be one of %s, got %r" % (PROFILES, name))
        self.name = name
        self.fixed = name == "fixed"
        if self.fixed:
            # The declared shape: the folder list is the answer to "where may a note go".
            self.folders = ("Daily", "Daily/Meetings", "Daily/Ticket Cleanup", "Contacts",
                            "Projects/Ongoing", "Projects/Parked", "Systems", "Runbooks",
                            "Reference", "Team")
            self.nested_folders = ("Daily/Meetings", "Daily/Ticket Cleanup", "Projects/Ongoing",
                                   "Projects/Parked")
            self.topic_folders = ("Contacts", "Projects/Ongoing", "Projects/Parked", "Systems")
            self.template_dir = os.path.join(HERE, "templates")
            self.template_for = {"Contacts": "Contact.md", "Projects/Ongoing": "Build.md",
                                 "Projects/Parked": "Build.md", "Runbooks": "Runbook.md",
                                 "Daily": "Daily.md"}
            self.daily_root = os.environ.get("HELM_NOTES_DAILY_ROOT") or "Daily"
            self.daily_nested = True          # Daily/YYYY/YYYY-MM/YYYY-MM-DD.md
            self.calls_category = "Calls"
            self.recording_re = re.compile(r"https?://[a-z0-9.-]+/recording/(\d+)", re.I)
            self.heuristics = True            # name and ticket-number matching in the Topic hub
            self.case_re = re.compile(r"\b(00\d{6})\b")
            self.name_suffixes = ("inc", "inc.", "llc", "ltd", "corp", "company", "holdings", "group")
            self.skip_dirs = FIXED_SKIP
            self.skip_top = WIKI_SKIP_TOP
        else:
            # A wiki: no fixed folders (see folders_present), no templates, no heuristics.
            self.folders = ()
            self.nested_folders = ()
            self.topic_folders = ()
            self.template_dir = None
            self.template_for = {}
            self.daily_root = os.environ.get("HELM_NOTES_DAILY_ROOT") or "Journal/Day"
            self.daily_nested = False         # Journal/Day/YYYY-MM-DD.md
            self.calls_category = None
            self.recording_re = None
            self.heuristics = False
            self.case_re = None
            self.name_suffixes = ()
            self.skip_dirs = WIKI_SKIP
            self.skip_top = WIKI_SKIP_TOP

    # --- paths -------------------------------------------------------------------------
    def folder_of(self, rel: str) -> str:
        """The folder a note is listed under: one of the nested folders when the path is
        inside one, else the top-level folder, else '' for a note at the root."""
        for f in self.nested_folders:
            if rel.startswith(f + "/"):
                return f
        head = rel.split("/", 1)
        return head[0] if len(head) > 1 else ""

    def daily_rel(self, d) -> str:
        if self.daily_nested:
            return "%s/%04d/%04d-%02d/%s.md" % (self.daily_root, d.year, d.year, d.month,
                                                d.isoformat())
        return "%s/%s.md" % (self.daily_root, d.isoformat())

    def is_daily(self, rel: str) -> bool:
        stem = os.path.splitext(rel.rsplit("/", 1)[-1])[0]
        if not DAILY_RE.match(stem) or not rel.startswith(self.daily_root + "/"):
            return False
        # The declared shape takes any date-named note under the daily root, even one misfiled
        # at the wrong depth, so the audit can find and move it. A wiki takes only the folder.
        if self.daily_nested:
            return self.folder_of(rel) == self.daily_root
        return rel.rsplit("/", 1)[0] == self.daily_root

    def folders_present(self, root: str) -> list:
        """The declared shape: its fixed list. A wiki: the top-level folders actually in the
        root, sorted. Either way the list is what a new note may be filed under."""
        if self.fixed:
            return list(self.folders)
        if not root or not os.path.isdir(root):
            return []
        try:
            names = sorted(os.listdir(root), key=str.lower)
        except OSError:
            return []
        out = []
        for name in names:
            if name.startswith(".") or name in self.skip_dirs or name in self.skip_top:
                continue
            path = os.path.join(root, name)
            # A linked folder is not one of the wiki's: the index never walks into it
            # (os.walk does not follow links), and it may point anywhere.
            if os.path.isdir(path) and not os.path.islink(path):
                out.append(name)
        return out

    def __repr__(self):
        return "Profile(%s)" % self.name


_profile: Profile | None = None


def profile() -> Profile:
    global _profile
    if _profile is None:
        _profile = Profile((os.environ.get("HELM_NOTES_PROFILE") or "wiki").strip().lower())
    return _profile


def reset() -> None:
    """Forget the cached profile."""
    global _profile
    _profile = None


# --- environment ---------------------------------------------------------------------------
def data_dir() -> str:
    return os.environ.get("HELM_DATA") or "data"


def root() -> str:
    """The notes folder. Empty means "none": store mode without an export folder."""
    return (os.environ.get("HELM_NOTES_ROOT") or "").strip()


def vault_aliases() -> list:
    """HELM_NOTES_VAULT_ALIASES, split on `;`: the folders a link may name a note by besides
    the notes folder itself. Read on each call, so a test can set it after import."""
    raw = os.environ.get("HELM_NOTES_VAULT_ALIASES") or ""
    return [p.strip() for p in raw.split(";") if p.strip()]


def db_path() -> str:
    return os.environ.get("HELM_NOTES_DB") or os.path.join(data_dir(), "notes.db")


def helm_db() -> str:
    return os.environ.get("HELM_DB") or os.path.join(data_dir(), "triage.db")


def mode() -> str:
    m = (os.environ.get("HELM_NOTES_MODE") or _site.notes_mode()).strip()
    if m not in MODES:
        raise RuntimeError("HELM_NOTES_MODE must be one of %s, got %r" % (MODES, m))
    return m


def read_only() -> bool:
    return mode() == "folder-readonly"


def untrusted_folder() -> bool:
    """A wiki's folder is written by many hands and tools, so what it holds is untrusted
    input in EVERY mode: raw HTML is escaped and active attachment types download. This keys
    on the profile, not on the read-only mode, so making the wiki writable must not turn the
    escaping off."""
    return profile().name == "wiki"
