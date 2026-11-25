#!/usr/bin/env python3
"""The helm.css design system owns the shared shell, navigation and component finish.

This replaces the retired SNES selector-coverage suite. Feature sheets still own behavior and
layout; /helm.css is imported once, after all of them, and must name the shared selectors whose
cascade it now controls. There is no per-theme coverage matrix and no silent exemption list.
"""
from __future__ import annotations

import pathlib
import re
import sys

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
REQUIRED_SELECTORS = {
    "workspace shell": ["shell-top", "shell-brand", "shell-search", "shell-tools"],
    "three panes": ["panes", "pane-left", "pane-main", "pane-props", "props-head"],
    "fixed navigation": ["nav-sec", "nav-icon", "nav-label", "nav-count", "nav-list", "lrow", "ltitle", "lmeta", "lage", "lhead", "lempty"],
    "agent status and rows": ["hx-status", "hx-status-dot", "hx-status-profile", "hx-status-model", "hx-row", "hx-row-title", "hx-row-meta", "hx-row-age"],
    "shared pages and cards": ["view", "hx-view", "hx-h", "hx-sub", "hx-note", "hx-card", "hx-card-title", "hx-card-sub", "hx-hub", "hx-tile", "hx-cols", "hx-col"],
    "shared controls": ["btn", "hx-btn", "chip", "hx-chip", "hx-form", "hx-composer"],
    "conversation and chat": ["hx-msg", "hx-msg-user", "hx-msg-body", "hx-tool", "hx-log", "hx-reason"],
    "overlays and drawers": ["overlay", "sheet", "help", "toast", "hx-drawer", "hx-drawer-title"],
    "coordinated navigation": ["main-view", "view-leaving", "view-entering"],
    "design-system components": ["hm-ochip", "hm-priv", "hm-pill", "hm-row", "hm-card", "hm-tabs", "hm-seg", "hm-input", "hm-pane-head", "hm-empty",
                                 "hm-toast", "hm-dialog", "hm-sheet", "hm-peek", "hm-pal", "hm-pal-item", "hm-conn", "hm-log", "hm-gl"],
}


def selector_classes(css: str) -> set[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: set[str] = set()
    for m in re.finditer(r"([^{}]*)\{", css):
        prelude = m.group(1)
        if prelude.strip().startswith("@"):
            continue
        out.update(re.findall(r"\.([a-zA-Z][\w-]*)", prelude))
    return out


def imported_sheets(index: str) -> list[str]:
    return re.findall(r'@import url\("(/[\w./-]+\.css)"\) layer\(base\)', index)


def coverage_gaps(web: pathlib.Path = WEB) -> dict[str, list[str]]:
    named = selector_classes((web / "helm.css").read_text(encoding="utf-8"))
    return {group: [c for c in classes if c not in named] for group, classes in REQUIRED_SELECTORS.items()
            if any(c not in named for c in classes)}


def main() -> int:
    passed = failed = 0
    index = (WEB / "index.html").read_text(encoding="utf-8")
    sheets = imported_sheets(index)
    imported = [pathlib.Path(s).name for s in sheets]
    feats = ["style", "conversation", "shell", "notebook", "home"]
    for label, ok in [
        ("index.html imports the base sheets", len(sheets) >= 5),
        ("the base sheets include helm.css and every base sheet", all(n in imported for n in ["helm.css"] + [f + ".css" for f in feats])),
        ("helm.css is the one final shared finish", sheets and sheets[-1] == "/helm.css" and sheets.count("/helm.css") == 1),
        ("index.html links no SNES theme sheets", not re.findall(r'<link rel="stylesheet" href="/snes/', index)),
        ("index.html imports no selectable theme sheets", not any(s.startswith("/themes/") for s in sheets)),
    ]:
        print(("ok   " if ok else "FAIL ") + label)
        passed += ok
        failed += not ok
    gaps = coverage_gaps()
    for group in REQUIRED_SELECTORS:
        missing = gaps.get(group, [])
        ok = not missing
        print(("ok   " if ok else "FAIL ") + f"helm.css draws {group}" + ("" if ok else f" (missing: {', '.join(missing)})"))
        passed += ok
        failed += not ok
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
