# git-hooks

An optional pre-commit gate for this repository. It lints what a commit stages:
`ruff check --select F,E9` plus a `py_compile` on staged `.py` files, and
`bash -n` plus `shellcheck -S error` on staged `.sh` files. It is lint only; run
`python3 tests/run_all.py` (or the container) for the full suite.

Install it in your own clone:

```sh
cp git-hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

Skip a single run with `SKIP_GATE=1 git commit`.
