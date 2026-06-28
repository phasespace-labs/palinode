#!/usr/bin/env bash
# deploy/systemd/install.sh — Install palinode systemd units (user or system scope).
#
# Idempotent: re-running upgrades existing files.
# Linux only — macOS users should use launchd (see deploy/launchd/ when available).
#
# Two scopes:
#   --user   (default) writes ~/.config/systemd/user/*.service, no root needed,
#            [Install] WantedBy=default.target, managed with `systemctl --user`.
#   --system writes /etc/systemd/system/*.service (requires root),
#            [Install] WantedBy=multi-user.target, managed with `systemctl`.
#            Use this to reconcile a production host whose live units are
#            system-scope (e.g. the dedicated palinode host: /opt + /var/lib,
#            units run by root under multi-user.target — #252).
#
# Usage:
#   PALINODE_HOME=/opt/palinode bash deploy/systemd/install.sh
#   PALINODE_HOME=/opt/palinode bash deploy/systemd/install.sh --enable
#   sudo -E PALINODE_HOME=/opt/palinode bash deploy/systemd/install.sh --system --enable
#
# Variables (all have defaults shown; override via env or export):
#   PALINODE_HOME       — palinode code + venv root  (default: $HOME/palinode)
#   PALINODE_DATA_DIR   — memory markdown directory   (default: $HOME/palinode-data)
#   OLLAMA_URL          — Ollama API base URL          (default: http://localhost:11434)
#   EMBEDDING_MODEL     — model name for embeddings   (default: bge-m3)
#   API_PORT            — port for palinode-api        (default: 6340)
#   MCP_PORT            — port for palinode-mcp-sse    (default: 6341)
#   WATCHER_UNIT_NAME   — installed name of the watcher/indexer unit
#                         (default: palinode-watcher). Set this when an existing
#                         deployment named the watcher unit differently (e.g.
#                         palinode-indexer) so re-running stays idempotent against
#                         the live unit instead of creating a duplicate.
#   PALINODE_API_BIND_INTENT — bind-intent for the API's 0.0.0.0 bind (default: empty).
#                         Leave empty for a token-less, network-isolated host: the
#                         API starts and only logs the 0.0.0.0 warning. Set to
#                         "public" to suppress the warning — but then the app
#                         REQUIRES PALINODE_API_TOKEN and refuses to start without
#                         one, so only set it alongside a token (#252).
#
# Flags:
#   --system    Install system-scope units in /etc/systemd/system (requires root).
#   --enable    Also enable and start all three services after installing unit files.
#   --help      Show this message and exit.
#
# After install (without --enable), user scope:
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
SCOPE=user
for arg in "$@"; do
    case "$arg" in
        --enable) ENABLE=1 ;;
        --system) SCOPE=system ;;
        --user) SCOPE=user ;;
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
# Empty by default: a value-based check in the app treats "" as "not public", so
# the API binds 0.0.0.0 and starts WITHOUT requiring a token (only logs a warning).
# Set to "public" (with a token) to suppress the warning. Exported for envsubst.
export PALINODE_API_BIND_INTENT="${PALINODE_API_BIND_INTENT:-}"
WATCHER_UNIT_NAME="${WATCHER_UNIT_NAME:-palinode-watcher}"

# Map a template basename to the unit name it installs as. Only the watcher
# template is renameable (existing deploys may call it palinode-indexer).
unit_name_for() {
    case "$1" in
        palinode-watcher) echo "$WATCHER_UNIT_NAME" ;;
        *) echo "$1" ;;
    esac
}

# ── scope: user vs system ────────────────────────────────────────────────────
# Both the install location and the [Install] WantedBy target depend on scope.
# SYSTEMD_WANTED_BY is exported so envsubst renders it into the templates.

if [[ "$SCOPE" == "system" ]]; then
    UNIT_DIR="/etc/systemd/system"
    SYSTEMCTL=( systemctl )
    export SYSTEMD_WANTED_BY="multi-user.target"
    [[ "${EUID:-$(id -u)}" -eq 0 ]] || \
        die "--system writes to $UNIT_DIR and needs root. Re-run with: sudo -E bash $0 --system ..."
else
    UNIT_DIR="$HOME/.config/systemd/user"
    SYSTEMCTL=( systemctl --user )
    export SYSTEMD_WANTED_BY="default.target"
fi

# ── resolve script location so it works from any cwd ─────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info "Installing palinode systemd $SCOPE units"
info "  PALINODE_HOME     = $PALINODE_HOME"
info "  PALINODE_DATA_DIR = $PALINODE_DATA_DIR"
info "  OLLAMA_URL        = $OLLAMA_URL"
info "  EMBEDDING_MODEL   = $EMBEDDING_MODEL"
info "  API_PORT          = $API_PORT"
info "  MCP_PORT          = $MCP_PORT"
info "  API_BIND_INTENT   = ${PALINODE_API_BIND_INTENT:-(empty — token-less, warning only)}"
info "  WATCHER_UNIT_NAME = $WATCHER_UNIT_NAME"
info "  scope             = $SCOPE (WantedBy=$SYSTEMD_WANTED_BY)"
info "  target unit dir   = $UNIT_DIR"

# ── validate source dir ───────────────────────────────────────────────────────

TEMPLATES=( palinode-api palinode-mcp palinode-watcher )
for svc in "${TEMPLATES[@]}"; do
    tmpl="$SCRIPT_DIR/${svc}.service.template"
    [[ -f "$tmpl" ]] || die "Template not found: $tmpl"
done

# ── install ───────────────────────────────────────────────────────────────────

mkdir -p "$UNIT_DIR"

UNITS=()
for svc in "${TEMPLATES[@]}"; do
    tmpl="$SCRIPT_DIR/${svc}.service.template"
    unit="$(unit_name_for "$svc")"
    dest="$UNIT_DIR/${unit}.service"
    envsubst < "$tmpl" > "$dest"
    info "wrote $dest"
    UNITS+=( "$unit" )
done

# ── daemon-reload ─────────────────────────────────────────────────────────────

"${SYSTEMCTL[@]}" daemon-reload
info "daemon-reload complete"

# ── optional enable + start ───────────────────────────────────────────────────

if [[ "$ENABLE" -eq 1 ]]; then
    info "enabling and starting services..."
    "${SYSTEMCTL[@]}" enable --now "${UNITS[@]}"
    info "done — check status with: ${SYSTEMCTL[*]} status ${UNITS[*]}"
else
    info "unit files installed. To enable and start:"
    info "  ${SYSTEMCTL[*]} enable --now ${UNITS[*]}"
fi
