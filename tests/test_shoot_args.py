#!/usr/bin/env python3
"""tools/screens/shoot.py accepts only the fixed-design shooter's three flags.

An unknown flag, a flag missing its value, or --help prints the usage and exits before any
site comes up. The retired --theme option is deliberately refused rather than ignored.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools", "screens"))
import shoot  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name + ("  <- " + str(detail)[:300] if detail else ""))


def refused(argv):
    try:
        shoot.parse_args(argv)
    except SystemExit as e:
        return e.code not in (0, None) or argv == ["--help"]
    return False


def main():
    check("--help prints the usage and stops", refused(["--help"]))
    check("an unknown flag is refused", refused(["--themes", "tokyo"]))
    check("the retired --theme flag is refused", refused(["--theme", "helm"]))
    check("a flag with no value is refused", refused(["--site"]))
    check("a stray word is refused", refused(["helm"]))
    a = shoot.parse_args(["--site", "home", "--only", "board,help", "--out", "/tmp/x"])
    check("the known flags still read", a == {"sites": ["home"], "only": {"board", "help"}, "out": "/tmp/x"}, a)
    a = shoot.parse_args([])
    check("no flags: the one site, every module, docs/screens, the fixed design",
          a == {"sites": ["home"], "only": None, "out": shoot.OUT}, a)
    check("usage does not offer theme selection", "--theme" not in shoot.USAGE, shoot.USAGE)
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
