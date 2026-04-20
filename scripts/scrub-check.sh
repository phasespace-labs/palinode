#!/bin/bash
# Scan the public repo (or any directory) for leaked secrets/PII.
# Run against the public-push branch or the public repo clone.
#
# Usage:
#   ./scripts/scrub-check.sh                  # scan current directory
#   ./scripts/scrub-check.sh /path/to/public  # scan specific path
#
# Exit code: 0 = clean, 1 = leaks found

set -euo pipefail

TARGET="${1:-.}"
FAILED=0

# Patterns that must NEVER appear in public code
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
    'grue\.lurker@gmail'

    # Infrastructure specifics
    'deploy_5060'
    'engram-data'
    'engram-api'
    'engram-watcher'

    # Internal planning docs that shouldn't be in file content
    'POST-STRATEGY'
    'PUBLIC-LAUNCH-PLAN'
    'AGENT-ROADMAP'
    'CODEX-REVIEW-PROMPT'
    'GOVERNOR-MEM0-DISABLE'
)

# Files/dirs to skip (binary, git internals, this script itself)
SKIP="--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=__pycache__ --exclude=scrub-check.sh --exclude=*.db --exclude=*.pyc"

echo "=== Palinode Public Repo Scrub Check ==="
echo "Target: $TARGET"
echo ""

for pattern in "${PATTERNS[@]}"; do
    matches=$(/usr/bin/grep -riE $SKIP "$pattern" "$TARGET" 2>/dev/null || true)
    if [ -n "$matches" ]; then
        echo "LEAK FOUND — pattern: $pattern"
        echo "$matches" | head -5
        echo ""
        FAILED=1
    fi
done

# Check for files that should never exist in public
BAD_FILES=(
    "CLAUDE.md"
    "AGENTS.md"
    ".roorules"
    ".roomodes"
    ".roo/mcp.json"
    "deploy_5060.sh"
    "HANDOFF.md"
    "PLAN.md"
    "PRD.md"
    "FEATURES.md"
    "specs/5060-usage-plane.md"
    "specs/task-prompts"
    "docs/POST-STRATEGY.md"
    "docs/PUBLIC-LAUNCH-PLAN.md"
    "docs/AGENT-ROADMAP.md"
    "docs/CODEX-REVIEW-PROMPT.md"
    "docs/GOVERNOR-MEM0-DISABLE.md"
    "scripts/sync-ag-artifacts.sh"
    "uv.lock"
)

for f in "${BAD_FILES[@]}"; do
    if [ -e "$TARGET/$f" ]; then
        echo "FORBIDDEN FILE: $f exists in public tree"
        FAILED=1
    fi
done

echo ""
if [ $FAILED -eq 0 ]; then
    echo "ALL CLEAN — no secrets or forbidden files found."
else
    echo "LEAKS DETECTED — fix before pushing to public."
    exit 1
fi
