"""The harness itself: CI trusts run_all.py's exit code, so prove it.

Fake suites in a temp dir, run through run_all's own run_one/main:
a pass is 0; a reported failure, a crash, a suite that lies with exit 1,
a suite that reports nothing, a suite that ran no checks, and an empty
suite list are all non-zero.
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_all  # noqa: E402

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print("  FAIL %s %s" % (name, str(detail)[:300]))


FAKES = {
    "test_pass.py": 'print("3 passed, 0 failed")\n',
    "test_fail.py": 'import sys\nprint("2 passed, 1 failed")\nsys.exit(1)\n',
    "test_crash.py": 'raise SystemExit(3)\n',
    "test_liar.py": 'import sys\nprint("5 passed, 0 failed")\nsys.exit(1)\n',
    "test_silent.py": 'print("all good, trust me")\n',
    "test_zero.py": 'print("0 passed, 0 failed")\n',
}

tmp = tempfile.mkdtemp(prefix="helm-runall-")
for name, body in FAKES.items():
    with open(os.path.join(tmp, name), "w") as fh:
        fh.write(body)

run_all.HERE = tmp
res = {n: run_all.run_one(n) for n in FAKES}
check("a clean suite passes", res["test_pass.py"]["failed"] == 0
      and res["test_pass.py"]["rc"] == 0, res["test_pass.py"])
check("a reported failure counts", res["test_fail.py"]["failed"] == 1, res["test_fail.py"])
check("a crash with no counts is a failure", res["test_crash.py"]["failed"] >= 1,
      res["test_crash.py"])
check("exit 1 with '0 failed' is still a failure", res["test_liar.py"]["failed"] >= 1,
      res["test_liar.py"])
check("exit 0 with no count line is a failure", res["test_silent.py"]["failed"] >= 1,
      res["test_silent.py"])
check("a suite that ran no checks is a failure", res["test_zero.py"]["failed"] >= 1,
      res["test_zero.py"])


ENV_FAKE = ('import os, sys\n'
            'ok = "HELM_DEV_LOGIN" not in os.environ and "HELM_MODULES" not in os.environ and "HELM_SENTRY_DSN" not in os.environ'
            ' and "LEGACY_API_TOKEN" not in os.environ\n'
            'print("1 passed, 0 failed" if ok else "0 passed, 1 failed"); sys.exit(0 if ok else 1)\n')
with open(os.path.join(tmp, "test_env.py"), "w") as fh:
    fh.write(ENV_FAKE)
_saved = {k: os.environ.get(k) for k in ("HELM_DEV_LOGIN", "HELM_MODULES", "HELM_SENTRY_DSN", "LEGACY_API_TOKEN")}
os.environ.update(HELM_DEV_LOGIN="owner", HELM_MODULES="inbox", HELM_SENTRY_DSN="https://x@y/1", LEGACY_API_TOKEN="host-exported-token-x")
try:
    site_res = run_all.run_one("test_env.py")
finally:
    for k, v in _saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
check("run_all strips the host's HELM_* / LEGACY_* environment from every suite",
      site_res["failed"] == 0 and site_res["rc"] == 0, site_res)
os.remove(os.path.join(tmp, "test_env.py"))


def main_rc(keep):
    for name in os.listdir(tmp):
        if name not in keep:
            os.remove(os.path.join(tmp, name))
    with contextlib.redirect_stdout(io.StringIO()):
        return run_all.main()


# A run that is cut off must never read as a pass (2026-11-15: the CI runner ended the silent test step at ten
# minutes and reported success). So main() says each suite's result the moment it finishes, and it leaves a
# marker file with the totals only when it ran to the end; the workflow's next step requires that marker.
import threading  # noqa: E402

MARK = os.path.join(tmp, ".run_all.done")
run_all.DONE_FILE = MARK
SLOW = {"test_a_quick.py": 'print("1 passed, 0 failed")\n',
        "test_b_slow.py": 'import time\ntime.sleep(1.5)\nprint("1 passed, 0 failed")\n'}
for name in os.listdir(tmp):
    os.remove(os.path.join(tmp, name))
for name, body in SLOW.items():
    with open(os.path.join(tmp, name), "w") as fh:
        fh.write(body)
with open(MARK, "w") as fh:
    fh.write("stale marker from an earlier run\n")


class Tee(io.StringIO):
    """Records when each line arrived, and what the marker looked like at that moment."""
    def __init__(self):
        super().__init__()
        self.seen = []

    def write(self, s):
        if "test_a_quick.py" in s and "passed" in s:
            self.seen.append(("quick-line", os.path.exists(MARK)))
        return super().write(s)


tee = Tee()
with contextlib.redirect_stdout(tee):
    rc_slow = run_all.main()
out_slow = tee.getvalue()
check("main: a suite's result line is printed when that suite finishes, before the next one ends",
      tee.seen and out_slow.index("test_a_quick.py") < out_slow.index("=" * 72), tee.seen)
check("main: a stale marker is removed at the start, so a cut-off run leaves none",
      tee.seen and tee.seen[0][1] is False, tee.seen)
check("main: the marker exists only after the whole run, with the totals",
      rc_slow == 0 and os.path.exists(MARK) and "2 suites, 2 passed, 0 failed" in open(MARK).read(),
      open(MARK).read() if os.path.exists(MARK) else "no marker")
os.remove(MARK)
for name in SLOW:
    os.remove(os.path.join(tmp, name))
for name, body in FAKES.items():
    with open(os.path.join(tmp, name), "w") as fh:
        fh.write(body)
with contextlib.redirect_stdout(io.StringIO()):
    run_all.main()
check("main: a failing run still writes the marker, and it says how many failed",
      os.path.exists(MARK) and " 0 failed" not in open(MARK).read().splitlines()[-1], open(MARK).read() if os.path.exists(MARK) else "no marker")

check("main: any failing suite makes the exit code non-zero",
      main_rc(set(FAKES)) == 1)
check("main: only clean suites exit 0", main_rc({"test_pass.py"}) == 0)
check("main: no suites at all is non-zero", main_rc(set()) == 1)

# ---- --jobs: suites in parallel, same contract (progress per suite, marker only at the end, same exit code) ----
check("parse_jobs: default is 1", run_all.parse_jobs([], {}) == 1)
check("parse_jobs: HELM_TEST_JOBS sets it", run_all.parse_jobs([], {"HELM_TEST_JOBS": "3"}) == 3)
check("parse_jobs: --jobs beats the environment", run_all.parse_jobs(["--jobs", "2"], {"HELM_TEST_JOBS": "3"}) == 2)
check("parse_jobs: --jobs=N works", run_all.parse_jobs(["--jobs=4"], {}) == 4)
for bad in (["--jobs", "0"], ["--jobs", "many"], ["--jobs=-1"]):
    try:
        run_all.parse_jobs(bad, {})
        bad_ok = False
    except SystemExit:
        bad_ok = True
    check("parse_jobs: %s is refused" % " ".join(bad), bad_ok)

for name in os.listdir(tmp):
    os.remove(os.path.join(tmp, name))
LOG = os.path.join(tmp, "overlap.log")
# each fake appends "start/end <name> <time>" so overlap can be read back
STAMP = """import os, sys, time
def mark(w):
    with open(%r, "a") as f:
        f.write("%%s %%s %%.3f\\n" %% (w, os.path.basename(__file__), time.time()))
mark("start")
time.sleep(%s)
mark("end")
print("1 passed, 0 failed")
"""
PAR = {"test_p1.py": 1.5, "test_p2.py": 1.5, "test_p3.py": 1.5, "test_a_quick.py": 0.0}
for name, secs in PAR.items():
    with open(os.path.join(tmp, name), "w") as fh:
        fh.write(STAMP % (LOG, secs))
run_all.DONE_FILE = MARK
buf = io.StringIO()
t0 = time.time()
with contextlib.redirect_stdout(buf):
    rc_par = run_all.main(["--jobs", "3"])
wall = time.time() - t0
out_par = buf.getvalue()
check("jobs=3: four suites (3 x 1.5 s) finish in under the serial time", wall < 4.0, wall)
check("jobs=3: exit 0, marker has the totals", rc_par == 0 and "4 suites, 4 passed, 0 failed" in open(MARK).read(), open(MARK).read())
check("jobs=3: a progress line per suite, counted [n/4]", all(("[%d/4]" % i) in out_par for i in range(1, 5)), out_par)
check("jobs=3: the quick suite's line comes before the slow ones'", out_par.index("test_a_quick.py") < out_par.index("test_p1.py"), out_par)
summary = out_par.split("=" * 72)[1].splitlines()
check("jobs=3: the summary lists suites in name order whatever finished first",
      [l.split()[1] for l in summary if l.strip()] == sorted(PAR), summary)
os.remove(MARK)

# a failing suite beside others still fails the run
with open(os.path.join(tmp, "test_zfail.py"), "w") as fh:
    fh.write('import sys\\nprint("0 passed, 1 failed")\\nsys.exit(1)\\n')
with contextlib.redirect_stdout(io.StringIO()):
    rc_pf = run_all.main(["--jobs", "3"])
check("jobs=3: one failing suite makes the exit non-zero and the marker says exit 1",
      rc_pf == 1 and open(MARK).read().startswith("exit 1"), open(MARK).read())
os.remove(os.path.join(tmp, "test_zfail.py"))
os.remove(MARK)

# SERIAL suites run alone, after the parallel ones
open(LOG, "w").close()
with open(os.path.join(tmp, "test_solo.py"), "w") as fh:
    fh.write(STAMP % (LOG, 0.6))
run_all.SERIAL["test_solo.py"] = "fake: needs the machine to itself"
try:
    with contextlib.redirect_stdout(io.StringIO()):
        rc_solo = run_all.main(["--jobs", "3"])
finally:
    run_all.SERIAL.pop("test_solo.py")
ev = [l.split() for l in open(LOG).read().splitlines()]
solo = {w: float(t) for w, n, t in ev if n == "test_solo.py"}
others_end = max(float(t) for w, n, t in ev if n != "test_solo.py" and w == "end")
check("SERIAL: a listed suite starts only after every other suite has ended", solo["start"] >= others_end, (solo, others_end))
check("SERIAL: the run is still complete and green", rc_solo == 0 and "5 suites, 5 passed, 0 failed" in open(MARK).read(), open(MARK).read())

# ---- a hung suite: named, failed, bounded, and its whole process group killed ----
for name in os.listdir(tmp):
    os.remove(os.path.join(tmp, name))
CHILD_PID = os.path.join(tmp, "child.pid")
HANG = ('import os, subprocess, sys, time\n'
        'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])\n'
        'open(%r, "w").write(str(c.pid))\n'
        'print("partial output before the hang", flush=True)\n'
        'time.sleep(300)\n' % CHILD_PID)
with open(os.path.join(tmp, "test_hang.py"), "w") as fh:
    fh.write(HANG)
with open(os.path.join(tmp, "test_ok.py"), "w") as fh:
    fh.write('print("2 passed, 0 failed")\n')
run_all.SUITE_TIMEOUT = 2
t0 = time.time()
hung = run_all.run_one("test_hang.py")
check("hang: run_one returns near the limit, not after the hang", time.time() - t0 < 10, time.time() - t0)
check("hang: counted as failed with a non-zero rc", hung["failed"] >= 1 and hung["rc"] != 0, hung)
check("hang: the output names the suite and the limit",
      "suite test_hang.py timed out after 2 s" in hung["output"], hung["output"])
check("hang: partial output is kept", "partial output before the hang" in hung["output"], hung["output"])
time.sleep(0.5)
child = int(open(CHILD_PID).read())


def _running(pid):
    """True while the process still runs code. A killed process that nothing has reaped yet (a zombie: the normal
    state of a killed orphan in a container whose first process does not reap) still answers signal 0, so its
    state is read too: it is dead."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


_z = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
_z.kill()
time.sleep(0.3)   # not waited for: it stays a zombie of this process until the wait below
check("a killed child nobody has reaped yet counts as not running", not _running(_z.pid), _z.pid)
_z.wait()
alive = _running(child)
check("hang: the suite's child process is killed too", not alive, child)
if alive:
    os.kill(child, 9)
for jobs in ("1", "3"):
    if os.path.exists(MARK):
        os.remove(MARK)
    buf = io.StringIO()
    t0 = time.time()
    with contextlib.redirect_stdout(buf):
        rc_h = run_all.main(["--jobs", jobs])
    o = buf.getvalue()
    check("hang jobs=%s: the run ends in bounded time" % jobs, time.time() - t0 < 12, time.time() - t0)
    check("hang jobs=%s: exit 1, marker says exit 1 with the totals" % jobs,
          rc_h == 1 and open(MARK).read().startswith("exit 1") and "2 suites, 2 passed, 1 failed" in open(MARK).read(),
          open(MARK).read() if os.path.exists(MARK) else "no marker")
    check("hang jobs=%s: FAIL line names test_hang.py and the output says timed out" % jobs,
          "FAIL test_hang.py" in o and "timed out after 2 s" in o, o)
check("suite_timeout: default is 600 s, HELM_TEST_SUITE_TIMEOUT overrides",
      run_all.suite_timeout({}) == 600 and run_all.suite_timeout({"HELM_TEST_SUITE_TIMEOUT": "90"}) == 90)
for bad in ("0", "soon", "-5"):
    try:
        run_all.suite_timeout({"HELM_TEST_SUITE_TIMEOUT": bad})
        ok_bad = False
    except SystemExit:
        ok_bad = True
    check("suite_timeout: %r is refused" % bad, ok_bad)


# The workflow runs the suites three at a time and still requires the completion marker.
ROOT = os.path.dirname(HERE)
for wf in (".github/workflows/ci.yml",):
    text = open(os.path.join(ROOT, wf), encoding="utf-8").read()
    check("%s: the tests step sets HELM_TEST_JOBS to 3" % wf,
          'HELM_TEST_JOBS: "3"' in text.split("- name: tests")[1].split("- name:")[0], wf)
    check("%s: the marker step is still there" % wf, "tests/.run_all.done" in text and "grep -qx 'exit 0'" in text, wf)

print("%d passed, %d failed" % (PASSED, FAILED))
sys.exit(1 if FAILED else 0)
