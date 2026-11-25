#!/usr/bin/env python3
"""Static checks for the fixed design's inline-SVG navigation and sole legacy-path icon.

SNES sprites are retired. Every registered section now has its own inline SVG symbol in
shell.js, drawn with currentColor at a consistent size by helm.css. Keyboard focus remains a
real outline, reduced motion remains covered, and /snes/icon-32.png is only the cached favicon.
No image library: PNG dimensions come from the IHDR chunk.
"""
from __future__ import annotations

import pathlib
import re
import struct
import sys

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"


def png_size(path: pathlib.Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise ValueError(f"{path.name} is not a PNG")
    return struct.unpack(">II", head[16:24])


def nav_sections() -> list[str]:
    keys: list[str] = []
    for f in sorted(WEB.glob("*.js")):
        for m in re.finditer(r"(?:H\.register|reg\.register|Helm\.register)\(\{\s*key:\s*\"([\w-]+)\"", f.read_text(encoding="utf-8")):
            if m.group(1) not in keys:
                keys.append(m.group(1))
    return keys


def icon_keys(shell: str) -> list[str]:
    # The rail draws each section's glyph from web/glyphs.js through the GLYPH table in shell.js (one line: key: "glyph", ...).
    m = re.search(r"const GLYPH = \{(.*?)\};", shell, re.S)
    if not m:
        return []
    return [quoted or plain for quoted, plain in re.findall(r'(?:"([\w-]+)"|\b([\w-]+)):\s*"[\w-]+"', m.group(1))]


def main() -> int:
    shell = (WEB / "shell.js").read_text(encoding="utf-8")
    polish = (WEB / "helm.css").read_text(encoding="utf-8")
    index = (WEB / "index.html").read_text(encoding="utf-8")
    sections = nav_sections()
    icons = icon_keys(shell)
    checks = [
        ("the registry still lists the navigation sections", len(sections) >= 5),
        ("every section has its own inline SVG icon", sections and all(k in icons for k in sections), {"sections": sections, "icons": icons}),
        ("icons use one 24 x 24 currentColor SVG shell", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"' in shell
         and 'const n = el("span", "nav-icon")' in shell and 'n.setAttribute("aria-hidden", "true")' in shell),
        ("the fixed stylesheet sizes the icon and its SVG consistently", ".nav-icon { width: 17px; height: 17px;" in polish
         and ".nav-icon svg { width: 17px; height: 17px; }" in polish),
        ("keyboard focus is a visible two-pixel outline", "outline: 2px solid var(--cyan)" in polish and ":focus-visible" in polish),
        ("reduced motion still has an explicit rule", "@media (prefers-reduced-motion: reduce)" in polish),
        ("no sprite sheet or SNES style is referenced by the fixed navigation", ".png" not in shell and "/snes/" not in polish
         and not re.search(r'<link[^>]+href="/snes/[^"]+\.css"', index)),
        ("/snes/icon-32.png remains a real 32 x 32 favicon", png_size(WEB / "snes" / "icon-32.png") == (32, 32)),
        ("the favicon is the only SNES path in index.html", re.findall(r'["\'](/snes/[^"\']+)["\']', index) == ["/snes/icon-32.png"]),
    ]
    passed = failed = 0
    for item in checks:
        label, ok = item[:2]
        detail = item[2] if len(item) > 2 else ""
        print(("ok   " if ok else "FAIL ") + label + ("" if ok or not detail else "  <- " + str(detail)))
        passed += ok
        failed += not ok
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
