"""Store mode runs the cutover itself on its first start, so the home flip is one
ordinary deploy (the HELM_NOTES_MODE change in deploy/compose.home.yml) instead of a
database flip and a deploy that nothing ties together.

Each start is the real api (python -m uvicorn app.service:app, the entrypoint's
command) against a throwaway wiki folder and data directory, so the lifespan, the
exit code and the log are the ones the puller's smoke test and the error watch see:

  - folder and folder-readonly starts never flip, and a folder start records
    truth=folder in the meta table (the row the store-mode start looks for);
  - the first store start against a clean folder flips before serving: truth=store,
    one revision per note, one backup zip, the export marker, CRLF files rewritten LF,
    and one "notebook cutover: flipped" log line with the counts and the zip's path;
  - a second store start does nothing;
  - a folder with a round-trip difference is refused: the api exits non-zero with a
    "notebook cutover: refused" line, truth stays folder, no revision, no zip;
  - a folder-built DB from before the truth row existed (notes, no revisions, no row)
    is flipped too; a fresh store DB, and store mode with no folder, never are.
"""
# temp-ok: every folder under test is a fixture this module creates and deletes
from __future__ import annotations

import glob
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="helm_test_cutover_on_start_")

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + str(detail) if detail != "" else ""))


def write(vault, rel, text, newline="\n"):
    p = os.path.join(vault, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return p


def seed(vault):
    write(vault, "Projects/Alpha.md", "# Alpha\n\nSee [[Projects/Beta]].\n")
    write(vault, "Projects/Beta.md", "# Beta\n\n- [ ] a task\n")
    write(vault, "Journal/Day/2026-09-01.md", "# 2026-09-01\n")
    write(vault, "Ops/Windows.md", "# Windows\nline two\n", newline="\r\n")
    with open(os.path.join(vault, "Projects", "diagram.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\nnot really")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start(data, vault, mode, timeout=60):
    """One api start. Returns (served, returncode, log): served is True when /api/meta
    answered, and the process is then stopped; otherwise it exited on its own."""
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HELM_", "LEGACY_"))}
    env.update({
        "HELM_ROLE": "api", "HELM_APP_TOKEN": "test-token",
        "HELM_MODULES": "inbox,notebook", "HELM_DATA": data,
        "HELM_DB": os.path.join(data, "triage.db"),
        "HELM_NOTES_DB": os.path.join(data, "notes.db"),
        "HELM_NOTES_PROFILE": "wiki", "HELM_NOTES_MODE": mode,
        "HELM_NOTES_VERIFY_EVERY_S": "3600", "PYTHONUNBUFFERED": "1",
    })
    if vault:
        env["HELM_NOTES_ROOT"] = vault
    logf = tempfile.TemporaryFile(mode="w+", dir=TMP)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.service:app",
                             "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT)
    served = False
    deadline = time.time() + timeout
    while time.time() < deadline and proc.poll() is None:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/meta" % port, timeout=2).read()
            served = True
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    logf.seek(0)
    log = logf.read()
    logf.close()
    return served, proc.returncode, log


def db_facts(data):
    con = sqlite3.connect(os.path.join(data, "notes.db"))
    try:
        r = con.execute("SELECT value FROM meta WHERE key='truth'").fetchone()
        return {"truth": r[0] if r else None,
                "notes": con.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
                "revisions": con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0],
                "per_note": con.execute("SELECT COUNT(*) FROM notes n WHERE (SELECT COUNT(*)"
                                        " FROM revisions r WHERE r.rel=n.rel)=1").fetchone()[0]}
    finally:
        con.close()


def zips(data):
    return glob.glob(os.path.join(data, "vault_cutover_backup_*.zip"))


def fixture(name):
    vault = os.path.join(TMP, name, "wiki")
    data = os.path.join(TMP, name, "data")
    os.makedirs(vault)
    os.makedirs(data)
    seed(vault)
    return vault, data


def run() -> int:
    # --- a clean wiki: folder starts never flip, the first store start does ------------------
    print("\nclean folder")
    vault, data = fixture("clean")
    served, rc, log = start(data, vault, "folder")
    check("folder mode starts", served, log[-2000:])
    f = db_facts(data)
    check("a folder start records truth=folder in the meta table", f["truth"] == "folder", f)
    check("folder mode never flips (no revision, no zip)", f["revisions"] == 0 and not zips(data),
          (f, zips(data)))
    check("folder mode logs no cutover", "notebook cutover" not in log, log[-2000:])

    served, rc, log = start(data, vault, "folder-readonly")
    check("folder-readonly mode starts", served, log[-2000:])
    f = db_facts(data)
    check("folder-readonly mode never flips", f["truth"] == "folder" and f["revisions"] == 0
          and not zips(data), (f, zips(data)))

    served, rc, log = start(data, vault, "store")
    check("store mode starts after flipping", served, log[-3000:])
    f = db_facts(data)
    check("the first store start flips: truth=store", f["truth"] == "store", f)
    check("one revision per note", f["notes"] == 4 and f["revisions"] == 4 and f["per_note"] == 4, f)
    z = zips(data)
    check("one backup zip under HELM_DATA", len(z) == 1, z)
    flips = [ln for ln in log.splitlines() if "notebook cutover: flipped" in ln]
    check("exactly one flip log line", len(flips) == 1, log[-3000:])
    line = flips[0] if flips else ""
    check("the flip line carries the round trip's counts and the backup path",
          "notes 4" in line and "files 1" in line and (z[0] if z else "?") in line, line)
    check("the export marker is written",
          os.path.isfile(os.path.join(vault, "NOTEBOOK_EXPORT_README.txt")))
    with open(os.path.join(vault, "Ops", "Windows.md"), "rb") as fh:
        check("the export rebuild rewrote the CRLF note as LF", b"\r\n" not in fh.read())

    served, rc, log = start(data, vault, "store")
    check("a second store start serves", served, log[-2000:])
    f2 = db_facts(data)
    check("a second store start does nothing", f2 == f and len(zips(data)) == 1, (f, f2, zips(data)))
    check("a second store start logs no cutover", "notebook cutover" not in log, log[-2000:])

    # --- a round-trip difference: no flip, and the api does not start -----------------------
    print("\nfolder with a round-trip difference")
    vault, data = fixture("dirty")
    served, rc, log = start(data, vault, "folder")
    check("folder mode starts on the dirty folder", served, log[-2000:])
    # A note path that is not a regular file: the index never reads it (a FIFO would hang the
    # read), the export's comparison sees a note the store does not hold (a stray): not a
    # clean round trip. (Deterministic, and it holds when the suite runs as root.)
    os.mkfifo(os.path.join(vault, "Projects", "Twin.md"))
    served, rc, log = start(data, vault, "store")
    check("store mode refuses to start", not served and rc not in (None, 0), (served, rc, log[-3000:]))
    check("the refusal is one plain log line", "notebook cutover: refused" in log, log[-3000:])
    check("the refusal names the difference", "Projects/Twin.md" in log, log[-3000:])
    f = db_facts(data)
    check("no flip: truth stays folder, no revision", f["truth"] == "folder" and f["revisions"] == 0, f)
    check("no backup zip", not zips(data), zips(data))
    check("no export marker", not os.path.exists(os.path.join(vault, "NOTEBOOK_EXPORT_README.txt")))
    served, rc, log = start(data, vault, "folder")
    check("the previous image (folder mode) still starts on it", served, log[-2000:])

    # --- a folder-built DB from before the truth row -----------------------------------------
    print("\nfolder-built DB without a truth row")
    vault, data = fixture("legacy")
    start(data, vault, "folder")
    con = sqlite3.connect(os.path.join(data, "notes.db"))
    con.execute("DELETE FROM meta WHERE key='truth'")
    con.commit()
    con.close()
    served, rc, log = start(data, vault, "store")
    f = db_facts(data)
    check("it is flipped too", served and f["truth"] == "store" and f["revisions"] == 4
          and len(zips(data)) == 1, (served, f, log[-3000:]))

    # --- the flip happened and the export after it failed -------------------------------------
    print("\nexport fails after the flip")
    vault, data = fixture("afterflip")
    start(data, vault, "folder")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HELM_", "LEGACY_"))}
    env.update({"HELM_DATA": data, "HELM_NOTES_DB": os.path.join(data, "notes.db"),
                "HELM_NOTES_PROFILE": "wiki", "HELM_NOTES_MODE": "store", "HELM_NOTES_ROOT": vault})
    code = ("from notebook import cutover, export\n"
            "def boom(*a, **k): raise OSError('disk full')\n"
            "export.rebuild = boom\n"
            "print('RESULT', cutover.on_start())\n")
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    out = p.stdout + p.stderr
    f = db_facts(data)
    check("a failure after the flip does not stop the start", p.returncode == 0
          and "RESULT {'flipped': True" in out, out[-2000:])
    check("its log line says plainly the flip happened and how to reverse it",
          "notebook cutover: flipped (truth=store) but the export after the flip failed: OSError: disk full"
          in out and "reversal:" in out, out[-2000:])
    check("the store is truth", f["truth"] == "store" and f["revisions"] == 4, f)

    # --- store mode that is already the store ------------------------------------------------
    print("\nstore mode already the store")
    vault, data = fixture("fresh")
    served, rc, log = start(data, vault, "store")
    f = db_facts(data)
    # (Its export check adopts the folder's notes as revisions; that is store mode's own
    # business. The cutover's marks are the truth row, the zip and the log line.)
    check("a fresh store DB with a folder is not flipped", served
          and f["truth"] is None and not zips(data) and "notebook cutover" not in log,
          (served, f, log[-2000:]))
    data2 = os.path.join(TMP, "nofolder", "data")
    os.makedirs(data2)
    served, rc, log = start(data2, None, "store")
    check("store mode with no folder starts and never flips",
          served and not zips(data2) and "notebook cutover" not in log, (served, log[-2000:]))

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
