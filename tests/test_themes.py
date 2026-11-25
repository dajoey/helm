#!/usr/bin/env python3
"""Static contract for Helm's one fixed visual design (the Chart Room design system).

There is no theme registry or selectable sound set. index.html identifies `helm`, imports the
base feature sheets and then /helm.css last, and loads no retired theme/SNES controls, styles
or scripts. helm.css owns the tokens, the self-hosted type and the shared components. The contract
is semantic, not literal: the sheet is last and once, every var() is defined, every @font-face
matches a real woff2 (wOF2 magic) with an OFL licence beside it, nothing is remote, focus-visible
and reduced-motion blocks exist, the documented component classes are really drawn, the object
glyph set covers every object type, and the retired controls stay absent.
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
WEB = HERE.parent / "web"
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:400] if detail else ""))


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def imported_sheets(index: str) -> list[str]:
    return re.findall(r'@import url\("(/[\w./-]+\.css)"\) layer\(base\)', index)


def main() -> int:
    index = (WEB / "index.html").read_text(encoding="utf-8")
    sheet_path = WEB / "helm.css"
    check("the design-system stylesheet exists and the old polish sheet is gone", sheet_path.is_file() and not (WEB / "polish.css").exists())
    if not sheet_path.is_file():
        return 1
    helm = sheet_path.read_text(encoding="utf-8")
    plain = strip_comments(helm)

    check('html carries exactly data-theme="helm"', re.search(r'<html\b[^>]*\bdata-theme="helm"', index) is not None
          and len(re.findall(r'<html\b[^>]*\bdata-theme=', index)) == 1)
    sheets = imported_sheets(index)
    check("helm.css is imported once and last in the base cascade", len(sheets) >= 5 and sheets[-1] == "/helm.css" and sheets.count("/helm.css") == 1, sheets)
    check("the base sheets include helm.css and every base sheet",
          all("/" + n in sheets for n in ["helm.css", "style.css", "conversation.css", "shell.css", "notebook/notebook.css", "home.css"]), sheets)

    loaded = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', index) + re.findall(r'<link\b[^>]*\bhref="([^"]+)"', index) + sheets
    retired = sorted(u for u in loaded if u.startswith("/themes/") or (u.startswith("/snes/") and u != "/snes/icon-32.png"))
    check("no retired theme/SNES style or script loads", not retired, retired)
    check("the only SNES asset still referenced is the fixed favicon", '/snes/icon-32.png' in loaded
          and sorted(u for u in loaded if u.startswith("/snes/")) == ["/snes/icon-32.png"])
    check("theme and sound controls are absent", 'id="btn-theme"' not in index and 'id="btn-sound"' not in index)
    check("no theme or SNES storage bootstrap remains", "helm.theme" not in index and "helm.snes" not in index and "helmTheme" not in index)
    check("the glyph script loads right after shell.js", re.search(r'<script src="/shell\.js"></script>\s*<script src="/glyphs\.js"></script>', index) is not None)

    # tokens: defined, and the legacy names every module sheet reads are re-pointed
    legacy = ["--bg", "--bg-dark", "--bg-deep", "--s1", "--s2", "--s3", "--hair", "--hair-light", "--c92", "--c80", "--c75", "--c60", "--c38",
              "--blue", "--cyan", "--gold", "--ok", "--warn", "--crit", "--purple", "--orange", "--approve", "--deny", "--amend", "--wash",
              "--fg", "--muted", "--line", "--green", "--yellow", "--red", "--accent", "--font-sans", "--font-mono", "--font-body", "--font-ui"]
    root = re.search(r":root\s*\{(.*?)\n\}", plain, re.S)
    root_defs = set(re.findall(r"(--[\w-]+)\s*:", root.group(1))) if root else set()
    check("helm.css re-points every legacy token the module sheets use", not [t for t in legacy if t not in root_defs], [t for t in legacy if t not in root_defs])
    types = ["ask", "note", "day", "chat", "task", "thread", "run", "job", "host"]
    check("every object type has a colour token and a .t-<type> class", all("--c-" + t in root_defs and re.search(r"\.t-%s\s*\{" % t, plain) for t in types))
    check("body type is the --font-body token and the sans/serif/mono stacks are declared",
          re.search(r"html, body\s*\{[^}]*font:[^;}]*var\(--font-body\)", plain, re.S) is not None
          and "Instrument Sans" in plain and "Newsreader" in plain and "--font-mono" in root_defs)
    check("no Tokyo literals remain in the base sheets", not any(t in "\n".join((WEB / n).read_text(encoding="utf-8").lower() for n in ["style.css", "shell.css", "conversation.css"])
                                                                for t in ["#7aa2f7", "#9ece6a", "#f7768e", "#1a1b26", "#e0af68", "#7dcfff", "#bb9af7"]))
    check("pane width tokens exist", "--pane-left:" in root.group(1) and "--pane-props:" in root.group(1))
    check("the <=1100 rail tokens are 244 px and 250 px",
          re.search(r"@media \(max-width: 1100px\)\s*\{\s*:root\s*\{\s*--pane-left:\s*244px;\s*--pane-props:\s*250px;", plain) is not None)

    # fonts: declarations <-> files <-> licences
    declarations = re.findall(r'@font-face\s*\{([^}]*)\}', plain, re.S)
    found = {}
    for body in declarations:
        fam = re.search(r'font-family:\s*"([^"]+)"', body)
        src = re.search(r'src:\s*url\("((?:/)?fonts/[^"]+\.woff2)"\)\s*format\("woff2"\)', body)
        if fam and src:
            found["/" + src.group(1).lstrip("/")] = fam.group(1)
    on_disk = sorted("/fonts/" + f.name for f in (WEB / "fonts").glob("*.woff2"))
    check("@font-face count matches the woff2 files present, one declaration each", len(declarations) == len(found) == len(on_disk) and sorted(found) == on_disk,
          {"declared": sorted(found), "on_disk": on_disk})
    check("only Newsreader and Instrument Sans are declared", sorted(set(found.values())) == ["Instrument Sans", "Newsreader"], sorted(set(found.values())))
    bad_headers = [u for u in on_disk if (WEB / u.lstrip("/")).read_bytes()[:4] != b"wOF2"]
    check("every local font really is woff2", not bad_headers, bad_headers)
    check("each font family ships its OFL licence beside the fonts", (WEB / "fonts" / "Newsreader-OFL.txt").is_file() and (WEB / "fonts" / "InstrumentSans-OFL.txt").is_file()
          and all("SIL OPEN FONT LICENSE" in (WEB / "fonts" / n).read_text(encoding="utf-8").upper() for n in ["Newsreader-OFL.txt", "InstrumentSans-OFL.txt"]))
    check("helm.css has no imports, remote URLs or retired asset references", "@import" not in plain
          and re.search(r"url\(\s*['\"]?(?:https?:)?//", plain) is None and "/themes/" not in plain and "/snes/" not in plain)

    # accessibility blocks and the documented component layer
    check("a :focus-visible ring and a prefers-reduced-motion block exist", ":focus-visible" in plain and "@media (prefers-reduced-motion: reduce)" in plain)
    documented = sorted(set(re.findall(r"\.(hm-[a-z0-9-]+)", helm[: helm.index("*/")])))
    drawn = set(re.findall(r"\.(hm-[a-z0-9-]+)", plain))
    check("the component reference block lists classes and every one is drawn", len(documented) >= 40 and not (set(documented) - drawn), sorted(set(documented) - drawn))
    glyphs = (WEB / "glyphs.js").read_text(encoding="utf-8") if (WEB / "glyphs.js").is_file() else ""
    check("Helm.glyph exists and draws every object type plus the private mark",
          "H.glyph = glyph" in glyphs and all(re.search(r"\b%s:\s*'<" % t, glyphs) for t in types + ["private"]))

    base = [WEB / s.lstrip("/") for s in sheets]
    base_css = "\n".join(strip_comments(p.read_text(encoding="utf-8")) for p in base if p.is_file())
    defined = set(re.findall(r"(--[\w-]+)\s*:", base_css))
    used = set(re.findall(r"var\(\s*(--[\w-]+)\s*\)", base_css))
    check("every var() used without a fallback is defined", not (used - defined), sorted(used - defined))

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
