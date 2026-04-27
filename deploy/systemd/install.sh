#!/usr/bin/env bash
# deploy/systemd/install.sh — Install palinode systemd user units.
#
# Idempotent: re-running upgrades existing files. Requires no root.
# Linux only — macOS users should use launchd (see deploy/launchd/ when available).
#
# Usage:
#   PALINODE_HOME=/opt/palinode bash deploy/systemd/install.sh
#   PALINODE_HOME=/opt/palinode bash deploy/systemd/install.sh --enable
#
# Variables (all have defaults shown; override via env or export):
#   PALINODE_HOME       — palinode code + venv root  (default: $HOME/palinode)
#   PALINODE_DATA_DIR   — memory markdown directory   (default: $HOME/palinode-data)
#   OLLAMA_URL          — Ollama API base URL          (default: http://localhost:11434)
#   EMBEDDING_MODEL     — model name for embeddings   (default: bge-m3)
#   API_PORT            — port for palinode-api        (default: 6340)
#   MCP_PORT            — port for palinode-mcp-sse    (default: 6341)
#
# Flags:
#   --enable    Also enable and start all three services after installing unit files.
#   --help      Show this message and exit.
#
# After install (without --enable) run:
#   systemctl --user daemon-reload
#   systemctl --user enable --now palinode-api palinode-mcp palinode-watcher

set -euo pipefail

# ── helpers ──────────────────────────────────────────────────────────────────

usage() {
    # Print the leading block comment (lines 2..EOF up to first blank/non-comment line)
    sed -n '/^#!/d; /^#/!q; s/^# \{0,1\}//p' "$0"
    exit 0
}

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "  [install.sh] $*"; }

# ── parse flags ───────────────────────────────────────────────────────────────

ENABLE=0
for arg in "$@"; do
    case "$arg" in
        --enable) ENABLE=1 ;;
        --help|-h) usage ;;
        *) die "Unknown argument: $arg. Use --help for usage." ;;
    esac
done

# ── OS check ─────────────────────────────────────────────────────────────────

if [[ "$(uname -s)" != "Linux" ]]; then
    die "This installer requires Linux (systemd). For macOS, use launchd or another platform-native service manager."
fi

if ! command -v systemctl &>/dev/null; then
    die "systemctl not found. Is this a systemd system?"
fi

if ! command -v envsubst &>/dev/null; then
    die "envsubst not found. Install gettext (e.g. 'apt install gettext' or 'brew install gettext')."
fi

# ── defaults ─────────────────────────────────────────────────────────────────

export PALINODE_HOME="${PALINODE_HOME:-$HOME/palinode}"
export PALINODE_DATA_DIR="${PALINODE_DATA_DIR:-$HOME/palinode-data}"
export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-bge-m3}"
export API_PORT="${API_PORT:-6340}"
export MCP_PORT="${MCP_PORT:-6341}"

# ── resolve script location so it works from any cwd ─────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

info "Installing palinode systemd user units"
info "  PALINODE_HOME     = $PALINODE_HOME"
info "  PALINODE_DATA_DIR = $PALINODE_DATA_DIR"
info "  OLLAMA_URL        = $OLLAMA_URL"
info "  EMBEDDING_MODEL   = $EMBEDDING_MODEL"
info "  API_PORT          = $API_PORT"
info "  MCP_PORT          = $MCP_PORT"
info "  target unit dir   = $UNIT_DIR"

# ── validate source dir ───────────────────────────────────────────────────────

TEMPLATES=( palinode-api palinode-mcp palinode-watcher )
for svc in "${TEMPLATES[@]}"; do
    tmpl="$SCRIPT_DIR/${svc}.service.template"
    [[ -f "$tmpl" ]] || die "Template not found: $tmpl"
done

# ── install ───────────────────────────────────────────────────────────────────

mkdir -p "$UNIT_DIR"

for svc in "${TEMPLATES[@]}"; do
    tmpl="$SCRIPT_DIR/${svc}.service.template"
    dest="$UNIT_DIR/${svc}.service"
    envsubst < "$tmpl" > "$dest"
    info "wrote $dest"
done

# ── daemon-reload ─────────────────────────────────────────────────────────────

systemctl --user daemon-reload
info "daemon-reload complete"

# ── optional enable + start ───────────────────────────────────────────────────

if [[ "$ENABLE" -eq 1 ]]; then
    info "enabling and starting services..."
    systemctl --user enable --now palinode-api palinode-mcp palinode-watcher
    info "done — check status with: systemctl --user status palinode-api palinode-mcp palinode-watcher"
else
    info "unit files installed. To enable and start:"
    info "  systemctl --user enable --now palinode-api palinode-mcp palinode-watcher"
fi
