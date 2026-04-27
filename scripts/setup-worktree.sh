#!/usr/bin/env bash
# setup-worktree.sh — first-class agent worktree bootstrap
#
# After `git worktree add .claude/worktrees/<branch> <branch>`, run this
# script from inside the new worktree directory to create a per-worktree
# virtual environment whose editable install points at *this* worktree's
# source tree — not the main repo's `.venv`.
#
# Without this step, `pytest` silently loads palinode from the main repo
# via the shared editable-install finder, so code changes in the worktree
# are invisible to the test suite (#207).
#
# Usage:
#   cd .claude/worktrees/<branch>
#   bash scripts/setup-worktree.sh            # create/update .venv-worktree/
#   bash scripts/setup-worktree.sh --help     # show this message and exit
#   source .venv-worktree/bin/activate        # activate before running pytest
#
# Idempotent: re-running on an existing .venv-worktree/ upgrades the
# editable install but does not re-create the venv from scratch.
#
# Detection: this script identifies whether the current directory IS a
# worktree by checking whether .git is a file (worktree) or a directory
# (main working tree). It always proceeds either way — the per-worktree
# venv pattern is safe to use in the main working tree too.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$WORKTREE_ROOT/.venv-worktree"

# ── Help ──────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    # Print the leading comment block (lines starting with '#' up to the first
    # non-comment, non-blank line), stripping the leading '# ' prefix.
    while IFS= read -r line; do
        case "$line" in
            "#"*) printf '%s\n' "${line#\# }" ;;
            "")   printf '\n' ;;
            *)    break ;;
        esac
    done < "${BASH_SOURCE[0]}"
    exit 0
fi

# ── Detect worktree vs main working tree ─────────────────────────────────────

GIT_DIR_PATH="$WORKTREE_ROOT/.git"
if [[ -f "$GIT_DIR_PATH" ]]; then
    IS_WORKTREE=1
    # .git is a file like "gitdir: ../../.git/worktrees/<name>"
    # Read the gitdir target from the file for informational display.
    GIT_COMMON_HINT=$(grep -oE '[^ ]+$' "$GIT_DIR_PATH" 2>/dev/null || echo "unknown")
    echo "Detected: git worktree (gitdir pointer: $GIT_COMMON_HINT)"
elif [[ -d "$GIT_DIR_PATH" ]]; then
    IS_WORKTREE=0
    echo "Detected: main working tree (not a worktree)"
else
    echo "ERROR: $GIT_DIR_PATH does not exist — is this a git repository?" >&2
    exit 1
fi

# ── Create or reuse per-worktree venv ────────────────────────────────────────

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" &>/dev/null; then
    echo "ERROR: Python interpreter not found (tried: $PYTHON_BIN). Set PYTHON= to override." >&2
    exit 1
fi

if [[ -d "$VENV_DIR" ]]; then
    echo "Per-worktree venv already exists at $VENV_DIR — upgrading editable install."
else
    echo "Creating per-worktree venv at $VENV_DIR ..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    echo "Venv created."
fi

# ── Install the package (editable) from this worktree's source ───────────────

PIP="$VENV_DIR/bin/pip"
echo "Installing palinode[dev] in editable mode from $WORKTREE_ROOT ..."
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -e "${WORKTREE_ROOT}[dev]"
echo "Install complete."

# ── Verify the install resolves to this worktree ─────────────────────────────

INSTALLED_PATH=$("$VENV_DIR/bin/python" -c "import palinode; print(palinode.__file__)")
if [[ "$INSTALLED_PATH" == "$WORKTREE_ROOT"/* ]]; then
    echo "OK: palinode resolves to $INSTALLED_PATH"
else
    echo "WARNING: palinode resolves to $INSTALLED_PATH" >&2
    echo "         expected a path under $WORKTREE_ROOT" >&2
    echo "         The editable install may still be pointing at the wrong tree." >&2
fi

# ── Print next-steps ─────────────────────────────────────────────────────────

echo ""
echo "Setup complete. Run tests against this worktree's source with:"
echo ""
echo "    source $VENV_DIR/bin/activate && pytest"
echo ""
echo "Or as a one-liner without permanently activating:"
echo ""
if [[ "$IS_WORKTREE" -eq 1 ]]; then
    echo "    VIRTUAL_ENV=$VENV_DIR $VENV_DIR/bin/pytest tests/"
else
    echo "    $VENV_DIR/bin/pytest tests/"
fi
echo ""
