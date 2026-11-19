#!/bin/bash
# check-memory-walk.sh — enforce the core/memory_files.py walk choke point.
#
# core/memory_files.py is the only code that turns a path into a store path and
# the only code that walks the store for memory files, so every surface agrees
# on which files are memory: the rule for skipped folders, hidden file names,
# symlinked folders and loops lives in one place. Until this script, that was
# prose only: a new `glob.glob(os.path.join(root, "**", "*.md"), recursive=True)`
# brought back a second, slightly different rule. A call is flagged when it is:
#
#   1. os.walk( or rglob( — always
#   2. glob( or iglob( (module function or pathlib method) whose text, joined
#      across its lines, contains `**`, `.md` or `recursive=True`
#
# Each allowed call is a file plus a fixed substring of that call's text, with a
# reason. An entry never names a whole file: a memory glob left in the same file
# would then pass, which is the failure this guard exists to catch.
#
# Usage:
#   ./scripts/check-memory-walk.sh                   # scan whole tree
#   ./scripts/check-memory-walk.sh path/to/file [...] # specific files
#
# Exit code: 0 = clean, 1 = a walk outside the choke point.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# The choke point itself.
CHOKE_POINT="palinode/core/memory_files.py"

# "file|substring of the call's text", each with a reason. Add to this list only
# with a comment: an unexplained entry is a second memory rule in waiting.
WALK_ALLOWED=(
    # Prompt files under memory_dir/prompts/ — templates, not memory.
    "palinode/api/routers/session.py|prompts_dir"
    # `-history.md` consolidation-audit siblings, listed for the compaction
    # view. They are audit trail, not memory, and the view judges them itself.
    "palinode/api/ui/router.py|*-history.md"
    # Packaged prompt templates shipped inside the palinode package.
    "palinode/prompts/__init__.py|glob(\"*.md\")"
    "palinode/diagnostics/checks/prompts_current.py|glob(\"*.md\")"
    # Correction logs: JSONL, not Markdown.
    "palinode/corrections/readers.py|*.jsonl"
    # A harness transcript tree outside the store, found by session id.
    "palinode/corrections/review.py|{session_id}*"
    # The user's own --include pattern; every match is then filtered through
    # is_memory_path, so it cannot widen what counts as memory.
    "palinode/cli/obsidian_sync.py|os.path.join(memory_dir, include)"
    # A source vault outside the store, read by the importer.
    "palinode/import_/vault.py|os.walk(source_vault"
    # Database files hunted by the phantom-DB check, not memory.
    "palinode/diagnostics/checks/phantom_db.py|os.walk(root"
)

files=()
if [[ $# -gt 0 ]]; then
    files=("$@")
else
    while IFS= read -r f; do
        [[ -n "$f" ]] && files+=("$f")
    done < <(find palinode -name '*.py' 2>/dev/null | sort)
fi

if [[ ${#files[@]} -eq 0 ]]; then
    echo "check-memory-walk: no files to scan"
    exit 0
fi

# See check-write-choke-point.sh for why the guard exists: under `set -u`, bash
# 3.2 (still the system bash on macOS) treats expansion of an EMPTY array as
# unbound and aborts. `${arr[@]+"${arr[@]}"}` is safe on bash 3.2 and bash 5.
is_walk_allowed() {
    local f="$1" text="$2" entry file needle
    for entry in ${WALK_ALLOWED[@]+"${WALK_ALLOWED[@]}"}; do
        file="${entry%%|*}"
        needle="${entry#*|}"
        if [[ "$f" == "$file" || "$f" == */"$file" ]] && [[ "$text" == *"$needle"* ]]; then
            return 0
        fi
    done
    return 1
}

# Prints "<line>\t<call text>" for each flagged call in the file. A call's text
# runs from its name to the closing parenthesis, over at most 12 lines.
find_walks() {
    awk '
    function count(s, ch,   n, i) {
        n = 0
        for (i = 1; i <= length(s); i++) if (substr(s, i, 1) == ch) n++
        return n
    }
    function emit(   flagged) {
        if (text ~ /^(os\.walk|rglob)\(/) flagged = 1
        else flagged = (text ~ /\*\*/ || text ~ /\.md/ || text ~ /recursive=True/)
        if (flagged) printf "%d\t%s\n", start, text
        capturing = 0
    }
    {
        if (!capturing) {
            if (match($0, /(^|[^A-Za-z0-9_])(glob|iglob|rglob)\(|os\.walk\(/)) {
                from = RSTART
                if (substr($0, from, 1) !~ /[A-Za-z]/) from++
                text = substr($0, from)
                start = NR
                depth = count(text, "(") - count(text, ")")
                lines = 1
                capturing = 1
                if (depth <= 0) emit()
            }
        } else {
            text = text " " $0
            depth += count($0, "(") - count($0, ")")
            lines++
            if (depth <= 0 || lines >= 12) emit()
        }
    }
    END { if (capturing) emit() }
    ' "$1"
}

violations=0

for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    [[ "$f" == "$CHOKE_POINT" || "$f" == */"$CHOKE_POINT" ]] && continue

    while IFS=$'\t' read -r lineno text; do
        [[ -n "$lineno" ]] || continue
        if ! is_walk_allowed "$f" "$text"; then
            echo "fail: $f:$lineno — walks the store outside core/memory_files.py: $text"
            violations=$((violations + 1))
        fi
    done < <(find_walks "$f")
done

if [[ $violations -gt 0 ]]; then
    echo
    echo "memory walk violation: $violations site(s) bypass core/memory_files.py."
    echo "List the files with memory_files.list_memory_files(...) and judge a single"
    echo "path with memory_files.is_memory_path(...), or — for a reader of something"
    echo "that is not memory — add an explicit, commented entry to WALK_ALLOWED in"
    echo "this script, naming the file and a fixed substring of the call."
    exit 1
fi

exit 0
