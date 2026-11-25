"""The shared run bridge behind every suite's Srv class.

A loaded host (a busy CI runner) can starve the fake server's event loop past the first
.result(timeout) — and that timeout does NOT cancel the coroutine, which simply keeps running on
the loop. So a first wait timing out is not a failure: wait again, scaled for load, on the same
run. A coroutine that still has not answered after the scaled wait is a hung loop, not a busy
one: cancel it and raise SrvTimeout, a named error a job log can tell apart from an ordinary flake.

Flake signature this absorbs: a dashboard suite's N4 run_coroutine_threadsafe timeout — three red
ci/tests evaluations on 2026-11-21 under runner load, each an unhandled TimeoutError at
Srv.run that aborted the whole suite ("no checks reported by this suite").
"""
from __future__ import annotations

import asyncio
import concurrent.futures


class SrvTimeout(TimeoutError):
    """The coroutine never answered, even after the load-scaled second wait."""


def run_on_loop(loop, coro, timeout: float = 15) -> object:
    """Run `coro` on `loop` from a foreign thread and wait for its result, absorbing host load.

    The first `.result(timeout)` timing out leaves the run alive on the loop; the second wait is
    the load-scaled retry (4x, at least timeout + 45 s). Everything else a coroutine raises
    surfaces unchanged.
    """
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout)
    except concurrent.futures.TimeoutError:
        pass  # not cancelled — the run is still going on the loop; fall through to the scaled wait
    scaled = max(timeout * 4, timeout + 45)
    print("  [srv.run] the first %.0fs wait timed out (loaded host); the coroutine is still running - waiting %ds more" % (timeout, scaled))
    try:
        return fut.result(scaled)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        raise SrvTimeout("the coroutine did not answer within %.0fs then %.0fs; the loop looks hung, not merely loaded" % (timeout, scaled))
