---
created: 2026-04-26T00:00:00Z
category: documentation
---

# Palinode — Developer Setup

This document covers the local development workflow, including running the test
suite and the specific setup required when working inside a `git worktree`.

## Basic setup

```bash
git clone https://github.com/phasespace-labs/palinode
cd palinode
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the unit tests:

```bash
pytest
```

`pyproject.toml` configures `pythonpath = ["."]` under `[tool.pytest.ini_options]`,
so pytest always prefers the local source tree over whatever is installed in
`site-packages`.  This is the canonical fix for the editable-install gotcha
described below.

## Working in worktrees

Palinode's agent-based development workflow uses `git worktree` to give each
parallel agent its own branch and checkout:

```bash
git worktree add .claude/worktrees/my-feature my-feature
cd .claude/worktrees/my-feature
```

### The editable-install gotcha

When you (or an agent) run `pytest` inside a worktree **without** additional
setup, Python loads the `palinode` package through the editable-install finder
registered in the **main** repo's `.venv`:

```
.venv/lib/.../site-packages/__editable___palinode_0_7_2_finder.py
  → /Users/you/Code/palinode/palinode   ← main repo, not the worktree!
```

Result: code changes in the worktree are silently invisible to the test suite.
Tests can pass on broken code (and fail on correct code) without any error.
This was independently flagged by two M1-Wave-1 agents implementing #201 and
#200, and is tracked as #207.

The `pythonpath = ["."]` setting in `pyproject.toml` mitigates this for most
pytest invocations — pytest prepends the worktree root to `sys.path`, so the
local source directory shadows the editable finder.  However, the fully
correct fix for an isolated worktree is a per-worktree venv.

### Canonical fix: per-worktree venv via `scripts/setup-worktree.sh`

After creating a new worktree, run:

```bash
cd .claude/worktrees/<branch>
bash scripts/setup-worktree.sh
source .venv-worktree/bin/activate
pytest
```

The script:

1. Detects whether the current directory is a worktree (`.git` is a file) or
   the main working tree (`.git` is a directory) and reports which.
2. Creates a per-worktree venv at `.venv-worktree/` — separate from the main
   repo's `.venv` so the editable install resolves to *this* worktree.
3. Runs `pip install -e .[dev]` from the worktree root.
4. Verifies that `palinode.__file__` resolves under the worktree and warns if
   it doesn't.
5. Prints the exact `source` and `pytest` commands to run next.

The script is idempotent: re-running it on an existing `.venv-worktree/`
upgrades the editable install without recreating the venv.

### Quick one-liner fallback (no venv required)

If you just need a quick sanity check without setting up a full per-worktree
venv, the `PYTHONPATH` override forces the local source tree onto the path:

```bash
PYTHONPATH=. pytest tests/
```

This is equivalent to what `pythonpath = ["."]` does automatically when pytest
reads `pyproject.toml`.  Use the per-worktree venv for sustained development
inside a worktree; the one-liner for quick CI debugging.

### Summary of worktree test commands

| Situation | Command |
|---|---|
| First time in a new worktree | `bash scripts/setup-worktree.sh && source .venv-worktree/bin/activate && pytest` |
| Returning to an existing worktree | `source .venv-worktree/bin/activate && pytest` |
| Quick one-off (no venv) | `PYTHONPATH=. pytest tests/` |
| Main repo (no worktree) | `pytest` (works with the default `.venv`) |

## Running specific test subsets

```bash
# All non-live tests (default)
pytest

# A single file
pytest tests/test_store.py -v

# Tests matching a keyword
pytest -k "session_end" -v

# Integration tests (require a running palinode-api)
pytest tests/integration/ -v
```
