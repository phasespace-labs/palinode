#!/bin/bash
# check-shipping-leaks.sh — fast pre-merge leak scanner for public-shipping files.
#
# Unlike scripts/scrub-check.sh (which scans an entire public-tree clone),
# this scans only the files that would ship publicly, in the dev repo, on
# the current branch. Designed for speed: run as a pre-commit hook or in
# CI on every PR.
#
# Usage:
#   ./scripts/check-shipping-leaks.sh --diff origin/main       # scan only files changed vs origin/main (recommended)
#   ./scripts/check-shipping-leaks.sh path/to/file.py [...]    # scan specific files
#   ./scripts/check-shipping-leaks.sh                          # scan all shipping files on HEAD (audit mode)
#
# Exit code: 0 = clean, 1 = leaks found.
#
# IMPORTANT: this script is meant to catch new leaks introduced by a change.
# CI typically runs it against a diff so existing baseline noise does not block
# unrelated work. Full-audit mode is still useful when preparing a public sync.

set -euo pipefail

# Patterns that must NEVER appear in public-shipping files.
# Synced with scripts/scrub-check.sh and SYNC-PUBLIC.md.
PATTERNS=(
    # Private IPs and infrastructure
    '10\.2\.1\.(61|65|69)'
    '100\.83\.166'
    '100\.108\.11'
    '\.ts\.net'
    'tailscale'
    'clawdbot'

    # Personal paths and usernames
    '/home/clawd'
    '~/clawd/'
    'clawd@'
    'paul-kyle-pedro'

    # Private repo references
    'palinode-dev'

    # Email addresses
    'grue\.lurker'

    # Infrastructure specifics
    'deploy_5060'
    'engram-data'
    'engram-api'
    'engram-watcher'

    # Internal planning doc names
    'POST-STRATEGY'
    'PUBLIC-LAUNCH-PLAN'
    'AGENT-ROADMAP'
    'CODEX-REVIEW-PROMPT'
    'GOVERNOR-MEM0-DISABLE'

    # Reserved internal vocabulary
    '[Cc]olophon'
    'mm-kmd'
    'antigravity'
    'color-class'
    'GrueBrain'
    '[Ss]igned [Pp]rovenance'
    'palinode_cloud'
    'Ed25519'
    'RBAC'
    'IP-hygiene'
    'provenance bridge'

    # Old-org URLs that should now point at phasespace-labs
    'github\.com/Paul-Kyle/palinode([/-]|\.git|$)'
)

# Paths that DON'T ship publicly. Files matching these are skipped from
# the scan. Mirrors the rsync exclude list in
# .claude/plans/phase-a-codex-handoff.md and SYNC-PUBLIC.md.
DEV_ONLY_PREFIXES=(
    'specs/'
    'artifacts/'
    '.roo'
    'SYNC-PUBLIC.md'
    'docs/AGENT-ROADMAP.md'
    'docs/colophon-derivation/'
    '.claude/'
    '__pycache__'
    '.pytest_cache'
    '.venv'
    '.git/'
    'build/'
    'dist/'
    'HANDOFF.md'
    'PLAN.md'
    'TODO-PAUL.md'
    'FEATURES.md'
    'AGENTS.md'
    'CLAUDE.md'
    'deploy_5060.sh'
    'notes/'
    # Default-private architecture and planning docs
    'ADR-001-tools-over-pipeline.md'
    'ADR-002-watcher-fault-isolation.md'
    'ADR-003-memory-harness-boundary.md'
    'ADR-004-event-driven-consolidation.md'
    'ADR-005-debounced-reflection-executor.md'
    'ADR-006-on-read-reconsolidation.md'
    'ADR-007-access-metadata-and-decay.md'
    'ADR-008-ambient-context-search.md'
    'ADR-009-scoped-memory-context-prime.md'
    # Dev-only sync script
    'scripts/sync-ag-artifacts.sh'
    # Dev-only PR template
    '.github/PULL_REQUEST_TEMPLATE.md'
)

# Files that intentionally contain the blocked patterns as scanner inputs.
# Skip them so the scanner does not flag its own source data.
SCANNER_SOURCES=(
    'scripts/scrub-check.sh'
    'scripts/check-shipping-leaks.sh'
    # Tests may enumerate forbidden patterns as part of the guard itself.
    'tests/test_deploy_systemd.py'
)

# Allow specs/prompts/ even though specs/ is excluded above.
SHIPPING_ALLOWLIST=(
    'specs/prompts/'
)

# ── Build the file list ───────────────────────────────────────────────────────

FILES=()
MODE="all"
DIFF_REF=""

if [ "$#" -ge 2 ] && [ "$1" = "--diff" ]; then
    MODE="diff"
    DIFF_REF="$2"
    shift 2
fi

if [ "$MODE" = "diff" ]; then
    while IFS= read -r line; do
        [ -n "$line" ] && FILES+=("$line")
    done < <(git diff --name-only --diff-filter=AM "$DIFF_REF"...HEAD 2>/dev/null || true)
elif [ "$#" -gt 0 ]; then
    MODE="explicit"
    for f in "$@"; do
        FILES+=("$f")
    done
else
    while IFS= read -r line; do
        [ -n "$line" ] && FILES+=("$line")
    done < <(git ls-files)
fi

# Bash safety: empty array dereference is an error under `set -u`.
if [ "${#FILES[@]}" -eq 0 ]; then
    echo "check-shipping-leaks: no input files (mode=$MODE) — nothing to scan."
    exit 0
fi

# ── Filter to public-shipping files ───────────────────────────────────────────

is_dev_only() {
    local f="$1"
    for allow in "${SHIPPING_ALLOWLIST[@]}"; do
        case "$f" in
            "$allow"*) return 1 ;;
        esac
    done
    for p in "${DEV_ONLY_PREFIXES[@]}"; do
        case "$f" in
            "$p"*) return 0 ;;
        esac
    done
    return 1
}

is_scanner_source() {
    local f="$1"
    for s in "${SCANNER_SOURCES[@]}"; do
        [ "$f" = "$s" ] && return 0
    done
    return 1
}

SHIPPING=()
for f in "${FILES[@]}"; do
    [ -f "$f" ] || continue
    is_dev_only "$f" && continue
    is_scanner_source "$f" && continue
    SHIPPING+=("$f")
done

if [ "${#SHIPPING[@]}" -eq 0 ]; then
    echo "check-shipping-leaks: no public-shipping files in scope (mode=$MODE) — nothing to scan."
    exit 0
fi

# ── Scan ──────────────────────────────────────────────────────────────────────

FAILED=0
echo "check-shipping-leaks: scanning ${#SHIPPING[@]} files (mode=$MODE)"
echo ""

for pattern in "${PATTERNS[@]}"; do
    matches=$(/usr/bin/grep -nE "$pattern" "${SHIPPING[@]}" 2>/dev/null || true)
    if [ -n "$matches" ]; then
        echo "LEAK — pattern: $pattern"
        echo "$matches" | head -5
        echo ""
        FAILED=1
    fi
done

if [ $FAILED -eq 0 ]; then
    echo "OK — no leak patterns found in shipping files."
    exit 0
fi

echo "FAILED — fix the leaks above before merging. If a pattern is a"
echo "false-positive (e.g. a documentation example), either:"
echo "  - rename it to a neutral form (preferred), or"
echo "  - add the file to SCANNER_SOURCES in this script if it's"
echo "    legitimately listing the pattern by design."
exit 1
