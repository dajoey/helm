"""Every offline suite in this repo, one command, one exit code.

Run: python3 tests/run_all.py   (from anywhere; paths resolve from this file)

Suites are discovered, not listed, so a gate that lands with a new suite cannot be
forgotten here and silently stop running. Each suite is a separate process: a suite that
crashes outright (import error, SystemExit, segfault) is reported as a failure with its
output, instead of taking the harness down with it and leaving the run looking unfinished.

Run: python3 tests/run_all.py [--jobs N]   (N suites at a time; HELM_TEST_SUITE_TIMEOUT seconds per suite, default 600, then it is killed and reported as failed; default 1, or HELM_TEST_JOBS from the
environment). Parallel runs keep the same contract: one progress line per suite as it finishes, the same
summary and exit code, and the completion marker only when the whole run reached its end. A suite named in
SERIAL runs alone, after the parallel ones have finished.

The adversarial probe (probe_400s.py) runs last and counts the same way: it is the only
evidence that the gates are wired rather than merely written, so it is part of "green",
not an extra step someone might skip.
"""
from __future__ import annotations

import concurrent.futures
import os
import re
import signal
import subprocess
import sys
import time

# Offline test runs must never report errors to the error tracker / Sentry
os.environ.pop("HELM_SENTRY_DSN", None)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COUNT_RE = re.compile(r"^(\d+) passed, (\d+) failed", re.M)
DONE_FILE = os.path.join(HERE, ".run_all.done")   # written only by a run that reached its end (see main)


# Suites that must not share the machine with another suite, each with the reason. Empty until a
# repeated parallel run shows one that passes alone and fails beside others.
SERIAL = {}

# One suite may run this long (seconds) before it is killed and reported as failed by name. The slowest
# browser suite takes about a minute; the job's own timeout is 45 minutes and names nothing.
DEFAULT_SUITE_TIMEOUT = 600
SUITE_TIMEOUT = None   # set by main() from HELM_TEST_SUITE_TIMEOUT; run_one falls back to the default


def suite_timeout(environ):
    raw = environ.get("HELM_TEST_SUITE_TIMEOUT")
    if raw is None or raw == "":
        return DEFAULT_SUITE_TIMEOUT
    try:
        n = int(raw)
    except ValueError:
        raise SystemExit("run_all: HELM_TEST_SUITE_TIMEOUT needs a whole number of seconds, got %r" % raw)
    if n < 1:
        raise SystemExit("run_all: HELM_TEST_SUITE_TIMEOUT must be at least 1, got %d" % n)
    return n


def parse_jobs(argv, environ):
    """--jobs N wins; else HELM_TEST_JOBS; else 1. Anything that is not a whole number >= 1 is an error."""
    raw = None
    args = list(argv)
    for i, a in enumerate(args):
        if a == "--jobs" and i + 1 < len(args):
            raw = args[i + 1]
        elif a.startswith("--jobs="):
            raw = a.split("=", 1)[1]
    if raw is None:
        raw = environ.get("HELM_TEST_JOBS") or "1"
    try:
        n = int(raw)
    except ValueError:
        raise SystemExit("run_all: --jobs needs a whole number, got %r" % raw)
    if n < 1:
        raise SystemExit("run_all: --jobs must be at least 1, got %d" % n)
    return n


def suites():
    names = sorted(f for f in os.listdir(HERE)
                   if f.startswith("test_") and f.endswith(".py"))
    if os.path.exists(os.path.join(HERE, "probe_400s.py")):
        names.append("probe_400s.py")  # last: the adversarial pass over the same gates
    return names


def run_one(name):
    t0 = time.time()
    # The host's HELM_* / LEGACY_* settings (a deploy's env file sourced into the shell, an
    # exported HELM_MODULES=inbox) never reach a suite: every suite sets what it needs. The
    # Sentry DSN is dropped for the same reason.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HELM_", "LEGACY_"))}
    limit = SUITE_TIMEOUT or DEFAULT_SUITE_TIMEOUT
    # Own process group, so a timeout can kill the suite's children too (a hung browser must not outlive it).
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, name)], cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    timed_out = False
    try:
        so, se = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        so, se = proc.communicate()
        so = so or exc.stdout or ""
        se = se or exc.stderr or ""
    out = (so or "") + (se or "")
    if timed_out:
        out += "\n[run_all] suite %s timed out after %d s and was killed\n" % (name, limit)
        return dict(name=name, passed=0, failed=1, rc=124, secs=time.time() - t0, output=out)
    last = None
    for last in COUNT_RE.finditer(out):
        pass  # the LAST count line is the suite's own total
    passed = int(last.group(1)) if last else 0
    failed = int(last.group(2)) if last else 0
    if proc.returncode != 0 and failed == 0:
        # A suite that died without reporting counts must not read as 0 failures.
        failed = 1
    if last is None or passed + failed == 0:
        # A suite that exits 0 without its "N passed, M failed" line, or that ran
        # no checks at all, proved nothing: that is a failure, never a quiet pass.
        failed = max(failed, 1)
        out += "\n[run_all] no checks reported by this suite\n"
    return dict(name=name, passed=passed, failed=failed, rc=proc.returncode,
                secs=time.time() - t0, output=out)


def main(argv=None):
    # A run that is cut off must never read as a pass. The CI runner once ended this step after ten silent
    # minutes and called it green (2026-11-15), so: every suite's result is said the moment it finishes (the
    # step is never silent for long), and DONE_FILE is written only when the run reached its end. The
    # workflow's next step requires that file.
    global SUITE_TIMEOUT
    jobs = parse_jobs(sys.argv[1:] if argv is None else argv, os.environ)
    if os.environ.get("HELM_TEST_SUITE_TIMEOUT"):
        SUITE_TIMEOUT = suite_timeout(os.environ)
    try:
        os.remove(DONE_FILE)
    except OSError:
        pass
    names = suites()
    results = {}
    done = [0]

    def report(r):
        results[r["name"]] = r
        done[0] += 1
        print("[%d/%d] %s %-24s %4d passed %4d failed  %.2fs"
              % (done[0], len(names), "ok  " if (r["failed"] == 0 and r["rc"] == 0) else "FAIL", r["name"], r["passed"], r["failed"], r["secs"]),
              flush=True)

    parallel = [n for n in names if n not in SERIAL]
    alone = [n for n in names if n in SERIAL]
    if jobs <= 1:
        for n in names:
            report(run_one(n))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            for fut in concurrent.futures.as_completed([pool.submit(run_one, n) for n in parallel]):
                report(fut.result())
        for n in alone:
            report(run_one(n))
    results = [results[n] for n in names if n in results]   # the summary lists them in the same order whatever the finishing order was
    if not results:
        print("no suites found under " + HERE)
        return 1
    total_p = sum(r["passed"] for r in results)
    total_f = sum(r["failed"] for r in results)
    print("=" * 72)
    for r in results:
        status = "ok  " if (r["failed"] == 0 and r["rc"] == 0) else "FAIL"
        print("%s %-24s %4d passed %4d failed  rc=%d  %.2fs"
              % (status, r["name"], r["passed"], r["failed"], r["rc"], r["secs"]))
    for r in results:
        if r["failed"] or r["rc"]:
            print("\n--- output of " + r["name"] + " " + "-" * 40)
            print(r["output"].rstrip())
    print("=" * 72)
    summary = "%d suites, %d passed, %d failed" % (len(results), total_p, total_f)
    print(summary, flush=True)
    rc = 1 if total_f or any(r["rc"] for r in results) else 0
    with open(DONE_FILE, "w", encoding="utf-8") as fh:
        fh.write("exit %d\n%s\n" % (rc, summary))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
