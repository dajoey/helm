"""Run one synthetic Helm preview with graceful, whole-stack cleanup."""
from __future__ import annotations
import argparse
import os
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "screens"))
import fixture  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    stopping = threading.Event()
    def stop(*_):
        stopping.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    stack = fixture.Stack(("home",), quiet=False)
    try:
        for site in stack.sites.values():
            site.build()
            site.web_port = args.port
            site.start(quiet=False)
        print(f"PREVIEW_PID={os.getpid()}", flush=True)
        print(f"HOME_WEB_URL={stack.home.web_url}", flush=True)
        while not stopping.wait(1):
            pass
    finally:
        stack.__exit__()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
