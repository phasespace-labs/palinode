#!/bin/bash
# tests/test_check_memory_walk.sh — bash tests for the store-walk guard.
#
# palinode/core/memory_files.py is the only code that walks the store for
# memory files (issue 251, step two). scripts/check-memory-walk.sh fails on a
# glob(, iglob(, rglob( or os.walk( under palinode/ outside that module and its
# commented allowlist of readers of things that are not memory.
#
# This script does NOT depend on pytest. Invoke directly:
#   bash tests/test_check_memory_walk.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK="$REPO_ROOT/scripts/check-memory-walk.sh"

if [ ! -f "$CHECK" ]; then
    echo "FAIL: scripts/check-memory-walk.sh missing"
    exit 1
fi

pass=0
fail=0

ok() { echo "  ok: $1"; pass=$((pass + 1)); }
ng() { echo "  FAIL: $1"; fail=$((fail + 1)); }

PROBE_REL="palinode/_memory_walk_probe.py"
PROBE="$REPO_ROOT/$PROBE_REL"
cleanup() { rm -f "$PROBE"; }
trap cleanup EXIT

cd "$REPO_ROOT"

echo "== check-memory-walk =="

# ── 1. the tree after the build passes ──────────────────────────────────────
if bash "$CHECK" >/dev/null 2>&1; then
    ok "clean tree passes"
else
    ng "clean tree should pass but did not"
fi

# ── 2. each walk shape outside the walker is caught ─────────────────────────
expect_caught() {
    local label="$1"
    if bash "$CHECK" "$PROBE_REL" >/dev/null 2>&1; then
        ng "$label was NOT caught"
    else
        ok "$label outside memory_files.py is caught"
    fi
    cleanup
}

cp "$REPO_ROOT/palinode/core/skip_dirs.py" "$PROBE"
cat >> "$PROBE" <<'PY'

import glob
import os


def bad_walk(root):
    return glob.glob(os.path.join(root, "**", "*.md"), recursive=True)
PY
expect_caught "a recursive glob( in a copy of a palinode/ file"

cat > "$PROBE" <<'PY'
import glob


def bad_walk(root):
    return list(glob.iglob(root + "/**/*.md", recursive=True))
PY
expect_caught "iglob("

cat > "$PROBE" <<'PY'
from pathlib import Path


def bad_walk(root):
    return list(Path(root).rglob("*.md"))
PY
expect_caught "rglob("

cat > "$PROBE" <<'PY'
import os


def bad_walk(root):
    return [d for d, _, _ in os.walk(root)]
PY
expect_caught "os.walk("

cat > "$PROBE" <<'PY'
from pathlib import Path

MEMORY_PATTERN = "**/*.md"


def bad_walk(root):
    return list(Path(root).glob(MEMORY_PATTERN))
PY
expect_caught "a Path.glob( whose pattern sits in a variable"

cat > "$PROBE" <<'PY'
from pathlib import Path


def bad_walk(root):
    return [d / f for d, _, fs in Path(root).walk() for f in fs if f.endswith(".md")]
PY
expect_caught "Path.walk("

cat > "$PROBE" <<'PY'
import os


def bad_walk(root):
    return [os.path.join(d, f) for d, _, fs, _ in os.fwalk(root) for f in fs]
PY
expect_caught "os.fwalk("

cat > "$PROBE" <<'PY'
import os


def bad_walk(folder):
    return [f for f in os.listdir(folder) if f.endswith(".md")]
PY
expect_caught "os.listdir("

cat > "$PROBE" <<'PY'
import os


def bad_walk(folder):
    for entry in os.scandir(folder):
        if entry.is_dir():
            yield from bad_walk(entry.path)
        elif entry.name.endswith(".md"):
            yield entry.path
PY
expect_caught "os.scandir("

cat > "$PROBE" <<'PY'
from pathlib import Path


def bad_walk(folder):
    return [p for p in Path(folder).iterdir() if p.suffix == ".md"]
PY
expect_caught ".iterdir("

cat > "$PROBE" <<'PY'
import glob
import os


def bad_walk(d):
    a = glob.glob(os.path.join(d, "*.txt")); b = glob.glob(os.path.join(d, "**", "*.md"), recursive=True)
    return a + b
PY
expect_caught "a memory glob after a plain glob on one line"

# ── 3. a file that walks nothing passes ─────────────────────────────────────
cat > "$PROBE" <<'PY'
def read_only(path):
    with open(path) as f:
        return f.read()
PY
if bash "$CHECK" "$PROBE_REL" >/dev/null 2>&1; then
    ok "a file that walks nothing is not flagged"
else
    ng "a file that walks nothing was wrongly flagged"
fi
cleanup

# ── 4. the walker and the allowlisted non-memory readers are exempt ─────────
for allowed in \
    palinode/core/memory_files.py \
    palinode/api/routers/session.py \
    palinode/api/routers/health.py \
    palinode/consolidation/write_time.py \
    palinode/corrections/readers.py \
    palinode/import_/vault.py \
    palinode/diagnostics/checks/phantom_db.py \
    palinode/api/ui/router.py \
    palinode/core/save.py \
    palinode/diagnostics/checks/consolidation_schedule.py \
    palinode/diagnostics/checks/process_env.py \
    palinode/ingest/pipeline.py
do
    if bash "$CHECK" "$allowed" >/dev/null 2>&1; then
        ok "$allowed passes"
    else
        ng "$allowed should pass but was flagged"
    fi
done

# ── 4b. an allowlist entry exempts one call, not its whole file ─────────────
ALLOWED_COPY_DIR="$(mktemp -d)"
ALLOWED_COPY="$ALLOWED_COPY_DIR/palinode/api/ui/router.py"
mkdir -p "$(dirname "$ALLOWED_COPY")"
cat > "$ALLOWED_COPY" <<'PY'
import glob
import os


def history_files(base):
    return glob.glob(os.path.join(base, "**/*-history.md"), recursive=True)
PY
if bash "$CHECK" "$ALLOWED_COPY" >/dev/null 2>&1; then
    ok "the allowed -history.md call passes in a file named like api/ui/router.py"
else
    ng "the allowed -history.md call was flagged"
fi
cat >> "$ALLOWED_COPY" <<'PY'


def memory_files(root):
    return glob.glob(os.path.join(root, "**", "*.md"), recursive=True)
PY
if bash "$CHECK" "$ALLOWED_COPY" >/dev/null 2>&1; then
    ng "a memory glob beside an allowed call in api/ui/router.py was NOT caught"
else
    ok "a memory glob beside an allowed call is still caught"
fi
rm -rf "$ALLOWED_COPY_DIR"

# ── 4c. a call beside an allowed call in a real allowlisted file is caught ───
expect_caught_beside() {
    local rel="$1" label="$2" extra="$3" dir copy
    dir="$(mktemp -d)"
    copy="$dir/$rel"
    mkdir -p "$(dirname "$copy")"
    cp "$REPO_ROOT/$rel" "$copy"
    if ! bash "$CHECK" "$copy" >/dev/null 2>&1; then
        ng "the untouched copy of $rel was flagged, so its next cell proves nothing"
    fi
    printf '\n\n%s\n' "$extra" >> "$copy"
    if bash "$CHECK" "$copy" >/dev/null 2>&1; then
        ng "$label beside the allowed call in $rel was NOT caught"
    else
        ok "$label beside the allowed call in $rel is caught"
    fi
    rm -rf "$dir"
}

expect_caught_beside palinode/prompts/__init__.py 'rglob("*.md")' \
'def list_store(store):
    return sorted(Path(store).rglob("*.md"))'
expect_caught_beside palinode/api/routers/session.py "a ** glob from prompts_dir" \
'def list_store(prompts_dir):
    return glob.glob(os.path.join(os.path.dirname(prompts_dir), "**", "*.md"), recursive=True)'
expect_caught_beside palinode/diagnostics/checks/phantom_db.py "os.walk(root_memory_dir)" \
'def list_store(root_memory_dir):
    return [f for _, _, fs in os.walk(root_memory_dir) for f in fs if f.endswith(".md")]'

# ── 5. a bulk reader of memory is not exempt by name ────────────────────────
for reader in palinode/core/lint.py palinode/api/routers/maintenance.py; do
    if grep -q "$reader" "$CHECK"; then
        ng "$reader is named in the guard; a memory reader must use the walker"
    else
        ok "$reader is not allowlisted"
    fi
done

# ── 6. the allowlist expansion survives bash 3.2 under set -u ──────────────
if grep -q 'ALLOWED\[@\]+' "$CHECK"; then
    ok "empty-array expansion is guarded for bash 3.2"
else
    ng "unguarded allowlist expansion — will abort on bash 3.2 under set -u"
fi

# ── 7. CI runs the guard and its test ───────────────────────────────────────
if grep -q 'bash scripts/check-memory-walk.sh' "$REPO_ROOT/.github/workflows/ci.yml"; then
    ok "CI runs scripts/check-memory-walk.sh"
else
    ng "CI does not run scripts/check-memory-walk.sh"
fi
if grep -q 'bash tests/test_check_memory_walk.sh' "$REPO_ROOT/.github/workflows/ci.yml"; then
    ok "CI runs tests/test_check_memory_walk.sh"
else
    ng "CI does not run tests/test_check_memory_walk.sh"
fi

echo
echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ] || exit 1
