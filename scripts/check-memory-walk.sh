#!/bin/bash
# check-memory-walk.sh — enforce the core/memory_files.py walk choke point.
#
# core/memory_files.py is the only code that walks the store for memory files,
# so every surface agrees on which files are memory: the rule for skipped
# folders, hidden file names, symlinked folders and loops lives in one place.
# Until this script, that was prose only: a new `glob.glob(os.path.join(root, "**", "*.md"), recursive=True)`
# brought back a second, slightly different rule. A call is flagged when it is:
#
#   1. os.walk( or rglob( — always
#   2. glob( or iglob( (module function or pathlib method) whose text, joined
#      across its lines, contains `**`, `.md` or `recursive=True`, or holds no
#      string literal, so its pattern sits in a variable
#   3. os.fwalk(, walk(, os.listdir(, os.scandir( and iterdir( — always
#
# Each allowed call is a file plus the exact text of that call, whitespace
# normalised, with a reason. An entry never names a whole file or a part of a
# call: a second call in the same file would then pass, which is the failure
# this guard exists to catch.
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

# "file|exact call text", each with a reason. Add to this list only with a
# comment: an unexplained entry is a second memory rule in waiting.
WALK_ALLOWED=(
    # The walk itself.
    'palinode/core/memory_files.py|os.scandir(disk_dir)'
    # Prompt files under memory_dir/prompts/ — templates, not memory.
    'palinode/api/routers/session.py|glob.glob(os.path.join(prompts_dir, "*.md"))'
    # `-history.md` consolidation-audit siblings, listed for the compaction
    # view. They are audit trail, not memory, and the view judges them itself.
    'palinode/api/ui/router.py|glob.glob(os.path.join(base, "**/*-history.md"), recursive=True)'
    # Packaged prompt templates shipped inside the palinode package.
    'palinode/prompts/__init__.py|.glob("*.md")'
    'palinode/diagnostics/checks/prompts_current.py|packaged_dir.glob("*.md")'
    # Correction logs: JSONL, not Markdown.
    'palinode/corrections/readers.py|root.rglob("*.jsonl")'
    # A harness transcript tree outside the store, found by session id.
    'palinode/corrections/review.py|glob.glob(os.path.join(os.path.expanduser(str(root)), "**", f"{session_id}*"), recursive=True)'
    # The user's own --include pattern; every match is then filtered through
    # is_memory_path, so it cannot widen what counts as memory.
    'palinode/cli/obsidian_sync.py|glob.glob(os.path.join(memory_dir, include), recursive=True)'
    # The names in one folder, to find a save target that differs only in case.
    'palinode/core/save.py|os.listdir(folder)'
    # The system cron.d folder, searched for a consolidation entry.
    'palinode/diagnostics/checks/consolidation_schedule.py|CRON_D_DIR.iterdir()'
    # The process table under /proc.
    'palinode/diagnostics/checks/process_env.py|proc_root.iterdir()'
    # Database files hunted by the phantom-DB check, not memory.
    'palinode/diagnostics/checks/phantom_db.py|os.walk(root, followlinks=False)'
    # A source vault outside the store, read by the importer.
    'palinode/import_/vault.py|os.walk(source_vault)'
    # The raw inbox, whose files the pipeline ingests and moves out.
    'palinode/ingest/pipeline.py|os.listdir(raw_dir)'
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
        if [[ "$f" == "$file" || "$f" == */"$file" ]] && [[ "$text" == "$needle" ]]; then
            return 0
        fi
    done
    return 1
}

# Prints "<line>\t<call text>" for each flagged call in the file. A call's text
# runs from its dotted receiver (if any) to its closing parenthesis, with
# whitespace normalised. Every call is scanned on its own, so a second call on
# the same line is seen.
find_walks() {
    awk '
    function normalise(s) {
        gsub(/[ \t\n]+/, " ", s)
        gsub(/\( /, "(", s)
        gsub(/ \)/, ")", s)
        sub(/^ /, "", s)
        sub(/ $/, "", s)
        return s
    }
    function call_end(s, open,   i, c, depth, quote, n) {
        depth = 0
        quote = ""
        n = length(s)
        for (i = open; i <= n && i < open + 2000; i++) {
            c = substr(s, i, 1)
            if (quote != "") {
                if (c == "\\") i++
                else if (c == quote) quote = ""
            } else if (c == "\"" || c == "\047") quote = c
            else if (c == "(") depth++
            else if (c == ")") {
                depth--
                if (depth == 0) return i
            }
        }
        return i
    }
    function is_flagged(name, text) {
        if (name == "glob" || name == "iglob")
            return (text ~ /\*\*/ || text ~ /\.md/ || text ~ /recursive=True/ || text !~ /["\047]/)
        return 1
    }
    { buf = buf $0 "\n" }
    END {
        rest = buf
        line = 1
        while (match(rest, /(^|[^A-Za-z0-9_])(glob|iglob|rglob|listdir|scandir|iterdir|walk|fwalk)\(/)) {
            name_start = RSTART + (substr(rest, RSTART, 1) ~ /[A-Za-z]/ ? 0 : 1)
            open = RSTART + RLENGTH - 1
            before = substr(rest, 1, name_start - 1)
            name = substr(rest, name_start, open - name_start)
            newlines = gsub(/\n/, "\n", before)
            line += newlines
            lead = before
            sub(/^.*\n/, "", lead)
            end = call_end(rest, open)
            first = name_start
            while (first > 1 && substr(rest, first - 1, 1) ~ /[A-Za-z0-9_.]/) first--
            text = normalise(substr(rest, first, end - first + 1))
            if (lead !~ /#/ && lead !~ /def +$/ && is_flagged(name, text))
                printf "%d\t%s\n", line, text
            rest = substr(rest, open + 1)
        }
    }
    ' "$1"
}

violations=0

for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue

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
