"""palinode mcp-config --diagnose — surface all MCP config-file homes.

Walks every known canonical location where a running client might read
MCP server configuration, parses each one, and reports what it found for
the `palinode` server entry.

Read-only diagnostic: we never write to any user config file.
"""
from __future__ import annotations

import difflib
import json
import platform
import sys
from pathlib import Path
from typing import Any

import click

from palinode.cli._format import console


# ---------------------------------------------------------------------------
# Emit-mode constants
# ---------------------------------------------------------------------------

# Generic placeholder — NEVER bake a real internal host/IP into shipping output.
# The user substitutes their own palinode host (the machine running palinode-mcp).
DEFAULT_HTTP_HOST = "<palinode-host>"
DEFAULT_HTTP_PORT = 6341
DEFAULT_STDIO_COMMAND = "palinode-mcp"


# ---------------------------------------------------------------------------
# Config-block builders (emit mode)
# ---------------------------------------------------------------------------

def _http_url(url: str | None, host: str, port: int) -> str:
    """Resolve the streamable-HTTP MCP URL.

    An explicit ``--url`` wins; otherwise build ``http://{host}:{port}/mcp/``.
    The trailing slash on ``/mcp/`` is required by the streamable-HTTP transport.
    """
    if url:
        return url
    return f"http://{host}:{port}/mcp/"


def _build_http_entry(url: str, bearer: str | None = None) -> dict[str, Any]:
    """Build the ``palinode`` server entry for streamable-HTTP transport."""
    entry: dict[str, Any] = {"type": "http", "url": url}
    if bearer:
        # Forward-compat for MCP bearer auth. Isolated
        # endpoint is token-less today; pass --bearer once auth lands there.
        entry["headers"] = {"Authorization": f"Bearer {bearer}"}
    return entry


def _build_stdio_entry() -> dict[str, Any]:
    """Build the ``palinode`` server entry for local stdio transport."""
    return {"command": DEFAULT_STDIO_COMMAND, "env": {}}


def _wrap_block(entry: dict[str, Any]) -> dict[str, Any]:
    """Wrap a server entry in the canonical ``mcpServers`` block."""
    return {"mcpServers": {"palinode": entry}}


# ---------------------------------------------------------------------------
# Canonical config locations
# ---------------------------------------------------------------------------

def _candidate_paths() -> list[tuple[str, Path]]:
    """Return (label, path) pairs for all known MCP config locations.

    Ordered from most-likely-to-be-read to least, per platform.
    """
    home = Path.home()
    system = platform.system()

    paths: list[tuple[str, Path]] = []

    # Claude Code CLI — the one users edit most often but may be wrong
    paths.append((
        "Claude Code CLI (~/.claude.json)",
        home / ".claude.json",
    ))

    # Claude Desktop — macOS canonical location (THE one the app reads)
    if system == "Darwin":
        paths.append((
            "Claude Desktop (macOS) — ~/Library/Application Support/Claude/claude_desktop_config.json",
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        ))
        # Claude 3p variant — separate app bundle on macOS
        paths.append((
            "Claude Desktop 3p variant (macOS) — ~/Library/Application Support/Claude-3p/claude_desktop_config.json",
            home / "Library" / "Application Support" / "Claude-3p" / "claude_desktop_config.json",
        ))
    elif system == "Windows":
        import os
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        paths.append((
            "Claude Desktop (Windows) — %APPDATA%\\Claude\\claude_desktop_config.json",
            appdata / "Claude" / "claude_desktop_config.json",
        ))
    else:
        # Linux / other
        paths.append((
            "Claude Desktop (Linux) — ~/.config/Claude/claude_desktop_config.json",
            home / ".config" / "Claude" / "claude_desktop_config.json",
        ))

    # Shared fallback some integrations use
    paths.append((
        "Integration fallback — ~/.claude/claude_desktop_config.json",
        home / ".claude" / "claude_desktop_config.json",
    ))

    # Cline (VS Code extension, formerly Claude Dev) — globalStorage
    # JSON shape: { "mcpServers": { "palinode": { ... } } }  (same as Claude Desktop)
    if system == "Darwin":
        paths.append((
            "Cline (macOS) — ~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
            home / "Library" / "Application Support" / "Code" / "User"
            / "globalStorage" / "saoudrizwan.claude-dev" / "settings"
            / "cline_mcp_settings.json",
        ))
        # Roo Cline — fork of Cline, different extension ID and settings filename
        paths.append((
            "Roo Cline (macOS) — ~/Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json",
            home / "Library" / "Application Support" / "Code" / "User"
            / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings"
            / "mcp_settings.json",
        ))
    elif system == "Windows":
        import os
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        paths.append((
            "Cline (Windows) — %APPDATA%\\Code\\User\\globalStorage\\saoudrizwan.claude-dev\\settings\\cline_mcp_settings.json",
            appdata / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
            / "settings" / "cline_mcp_settings.json",
        ))
        paths.append((
            "Roo Cline (Windows) — %APPDATA%\\Code\\User\\globalStorage\\rooveterinaryinc.roo-cline\\settings\\mcp_settings.json",
            appdata / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
            / "settings" / "mcp_settings.json",
        ))
    else:
        # Linux
        paths.append((
            "Cline (Linux) — ~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
            home / ".config" / "Code" / "User" / "globalStorage"
            / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        ))
        paths.append((
            "Roo Cline (Linux) — ~/.config/Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json",
            home / ".config" / "Code" / "User" / "globalStorage"
            / "rooveterinaryinc.roo-cline" / "settings" / "mcp_settings.json",
        ))

    # Zed — context_servers block in settings.json
    # JSON shape: { "context_servers": { "palinode": { ... } } }
    # Primary: ~/.config/zed/settings.json  (all platforms)
    paths.append((
        "Zed — ~/.config/zed/settings.json",
        home / ".config" / "zed" / "settings.json",
    ))
    if system == "Darwin":
        # Older Zed builds on macOS also wrote to Application Support
        paths.append((
            "Zed (macOS fallback) — ~/Library/Application Support/Zed/settings.json",
            home / "Library" / "Application Support" / "Zed" / "settings.json",
        ))

    return paths


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _read_config(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read and parse a JSON config file.

    Returns (data, error_message).  One of the two will always be None.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read file: {exc}"

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"

    if not isinstance(data, dict):
        return None, "unexpected top-level type (expected object)"

    return data, None


def _extract_palinode_entry(data: dict[str, Any]) -> dict[str, Any] | None:
    """Pull out the palinode server block, if present.

    Checks both ``mcpServers`` (Claude Desktop / Cline / Cursor shape) and
    ``context_servers`` (Zed shape).  Returns the first match found.
    """
    for key in ("mcpServers", "context_servers"):
        servers = data.get(key)
        if isinstance(servers, dict):
            entry = servers.get("palinode")
            if entry is not None:
                return entry
    return None


def _render_entry(entry: dict[str, Any] | None) -> str:
    """Turn a palinode MCP entry into a concise single-line description."""
    if entry is None:
        return "(no palinode entry)"

    if "url" in entry:
        return f"HTTP — url={entry['url']}"

    if "command" in entry:
        cmd = entry["command"]
        args = entry.get("args", [])
        env = entry.get("env", {})
        parts = [f"stdio — command={cmd}"]
        if args:
            parts.append(f"args={args}")
        if env:
            parts.append(f"env={json.dumps(env)}")
        return ", ".join(parts)

    return json.dumps(entry, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

class ConfigResult:
    def __init__(
        self,
        label: str,
        path: Path,
        present: bool,
        entry: dict[str, Any] | None,
        entry_json: str | None,
        error: str | None,
    ) -> None:
        self.label = label
        self.path = path
        self.present = present          # file exists
        self.entry = entry              # parsed palinode block (may be None)
        self.entry_json = entry_json    # canonical JSON string for diff
        self.error = error              # parse / IO error message

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "label": self.label,
            "path": str(self.path),
            "present": self.present,
        }
        if self.error:
            d["error"] = self.error
        elif self.entry is not None:
            d["palinode_entry"] = self.entry
            d["summary"] = _render_entry(self.entry)
        else:
            d["palinode_entry"] = None
            d["summary"] = "(no palinode entry)"
        return d


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------

def _check_divergence(results: list[ConfigResult]) -> list[tuple[ConfigResult, ConfigResult, str]]:
    """Return pairs of results whose palinode entries differ.

    Returns list of (a, b, unified_diff_str).
    """
    with_entries = [r for r in results if r.present and r.entry is not None and r.error is None]
    if len(with_entries) < 2:
        return []

    pairs: list[tuple[ConfigResult, ConfigResult, str]] = []
    seen: set[frozenset[int]] = set()
    for i, a in enumerate(with_entries):
        for j, b in enumerate(with_entries):
            if i >= j:
                continue
            key = frozenset([i, j])
            if key in seen:
                continue
            seen.add(key)
            if a.entry_json != b.entry_json:
                diff = "\n".join(
                    difflib.unified_diff(
                        (a.entry_json or "").splitlines(),
                        (b.entry_json or "").splitlines(),
                        fromfile=str(a.path),
                        tofile=str(b.path),
                        lineterm="",
                    )
                )
                pairs.append((a, b, diff))
    return pairs


# ---------------------------------------------------------------------------
# Emit mode
# ---------------------------------------------------------------------------

def _emit_config(
    *,
    emit_http: bool,
    url: str | None,
    host: str,
    port: int,
    bearer: str | None,
    output_json: bool,
) -> None:
    """Print a ready-to-paste MCP config block for the chosen transport.

    TTY-aware: when piped (or --json), prints only the JSON block so the
    output can be redirected straight into a config file; when interactive,
    wraps it with guidance.
    """
    if emit_http:
        resolved_url = _http_url(url, host, port)
        entry = _build_http_entry(resolved_url, bearer=bearer)
    else:
        entry = _build_stdio_entry()
    block = _wrap_block(entry)
    block_json = json.dumps(block, indent=2)

    # Piped / --json: emit the raw block only (no decoration).
    if output_json or not sys.stdout.isatty():
        click.echo(block_json)
        return

    # Interactive: wrap with human guidance.
    console.print()
    if emit_http:
        console.print("[bold]Palinode MCP — streamable-HTTP config[/bold]")
    else:
        console.print("[bold]Palinode MCP — stdio config[/bold]")
    console.print()
    console.print(
        "Paste the [cyan]palinode[/cyan] entry into the config your client reads\n"
        "(run [cyan]palinode mcp-config[/cyan] with no flags to find which file that is):"
    )
    console.print()
    for line in block_json.splitlines():
        console.print(f"  {line}")
    console.print()

    if emit_http:
        console.print(
            "[bold]Claude Code one-liner:[/bold]\n"
            f"  [cyan]claude mcp add palinode --transport http --url {entry['url']}[/cyan]"
        )
        console.print()
        if host == DEFAULT_HTTP_HOST and not url:
            console.print(
                f"[yellow]Replace [cyan]{DEFAULT_HTTP_HOST}[/cyan] with your palinode host[/yellow] "
                "(the machine running palinode-mcp),\n"
                "or re-run with [cyan]--host <name>[/cyan] / [cyan]--url <full-url>[/cyan]."
            )
            console.print()
        console.print(
            "Streamable-HTTP reuses the warm BGE-M3 model behind the running service —\n"
            "no per-session Python cold-start and no persistent SSH tunnel.\n"
            "[yellow]Note:[/yellow] Claude Desktop only accepts stdio entries — use [cyan]--stdio[/cyan] there."
        )
    else:
        console.print(
            "stdio runs a local [cyan]palinode-mcp[/cyan] process per session.\n"
            "For a remote server, prefer [cyan]--http[/cyan] (warm model, no SSH/cold-start)."
        )
    console.print()
    console.print("See [cyan]docs/MCP-CONFIG-HOMES.md[/cyan] for the full reference.")
    console.print()


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

@click.command("mcp-config")
@click.option(
    "--diagnose",
    is_flag=True,
    default=True,
    is_eager=True,
    expose_value=False,
    help="(default) Scan all known MCP config locations and report palinode entries.",
)
@click.option(
    "--http",
    "emit_http",
    is_flag=True,
    default=False,
    help="Emit a ready-to-paste streamable-HTTP config block (remote, warm-model).",
)
@click.option(
    "--stdio",
    "emit_stdio",
    is_flag=True,
    default=False,
    help="Emit a ready-to-paste stdio config block (local install).",
)
@click.option(
    "--url",
    "url",
    default=None,
    help="Full MCP URL for --http (overrides --host/--port). E.g. http://host:6341/mcp/",
)
@click.option(
    "--host",
    "host",
    default=DEFAULT_HTTP_HOST,
    show_default=True,
    help="Palinode host for --http (the machine running palinode-mcp).",
)
@click.option(
    "--port",
    "port",
    type=int,
    default=DEFAULT_HTTP_PORT,
    show_default=True,
    help="Streamable-HTTP MCP port for --http.",
)
@click.option(
    "--bearer",
    "bearer",
    default=None,
    help="Optional bearer token for --http.",
)
@click.option(
    "--json", "output_json",
    is_flag=True,
    default=False,
    help="Emit results as JSON (useful for scripting or piped output).",
)
def mcp_config(
    emit_http: bool,
    emit_stdio: bool,
    url: str | None,
    host: str,
    port: int,
    bearer: str | None,
    output_json: bool,
) -> None:
    """Surface all MCP config-file homes, or emit a ready-to-paste config block.

    Default (no flags) walks every location a running MCP client might read,
    parses the JSON, and reports what it finds for the 'palinode' server entry —
    useful when you edited one file and changes didn't take effect.

    With --http or --stdio, instead emit a copy-pasteable config block for the
    chosen transport. --http is the streamable-HTTP form (remote server, reuses
    the warm BGE-M3 model behind the running service, no SSH/cold-start).

    Read-only: we never write to any user config file.

    See docs/MCP-CONFIG-HOMES.md for the full canonical-location reference.
    """
    # ---- Emit mode (--http / --stdio) -------------------------------------
    if emit_http or emit_stdio:
        if emit_http and emit_stdio:
            raise click.UsageError("Pass only one of --http / --stdio.")
        _emit_config(
            emit_http=emit_http,
            url=url,
            host=host,
            port=port,
            bearer=bearer,
            output_json=output_json,
        )
        return

    candidates = _candidate_paths()
    results: list[ConfigResult] = []

    for label, path in candidates:
        if not path.exists():
            results.append(ConfigResult(
                label=label,
                path=path,
                present=False,
                entry=None,
                entry_json=None,
                error=None,
            ))
            continue

        data, error = _read_config(path)
        if error:
            results.append(ConfigResult(
                label=label,
                path=path,
                present=True,
                entry=None,
                entry_json=None,
                error=error,
            ))
            continue

        entry = _extract_palinode_entry(data)
        entry_json = json.dumps(entry, sort_keys=True, indent=2) if entry is not None else None
        results.append(ConfigResult(
            label=label,
            path=path,
            present=True,
            entry=entry,
            entry_json=entry_json,
            error=None,
        ))

    # ---- JSON output -------------------------------------------------------
    if output_json:
        divergences = _check_divergence(results)
        payload: dict[str, Any] = {
            "configs": [r.to_dict() for r in results],
            "diverged": len(divergences) > 0,
            "divergences": [
                {
                    "file_a": str(a.path),
                    "file_b": str(b.path),
                    "diff": diff,
                }
                for a, b, diff in divergences
            ],
        }
        click.echo(json.dumps(payload, indent=2))
        if divergences:
            sys.exit(1)
        return

    # ---- Human-readable output --------------------------------------------
    console.print()
    console.print("[bold]Palinode MCP config locations[/bold]")
    console.print()

    found_any = False
    for r in results:
        if not r.present:
            console.print(f"  [dim]· {r.path}[/dim]")
            console.print(f"    [dim]not present[/dim]")
        elif r.error:
            console.print(f"  [red]✗[/red] {r.path}")
            console.print(f"    [red]ERROR parsing:[/red] {r.error}")
        elif r.entry is None:
            console.print(f"  [yellow]·[/yellow] {r.path}")
            console.print(f"    file exists — no 'palinode' entry in mcpServers / context_servers")
        else:
            found_any = True
            console.print(f"  [green]✓[/green] {r.path}")
            console.print(f"    [cyan]{_render_entry(r.entry)}[/cyan]")
        console.print()

    if not found_any:
        console.print(
            "[yellow]No MCP configs found with a palinode entry.[/yellow]\n"
            "Run [cyan]palinode init[/cyan] to scaffold one, or add a 'palinode'\n"
            "block to the config your client reads (see docs/MCP-CONFIG-HOMES.md)."
        )
        console.print()
        return

    # Divergence warning
    divergences = _check_divergence(results)
    if divergences:
        console.print("[bold red]WARNING: configs diverge[/bold red]")
        console.print(
            "Multiple files have a 'palinode' entry but they differ.\n"
            "Editing the wrong one is the silent-failure pattern documented in #189."
        )
        console.print()
        for a, b, diff in divergences:
            console.print(f"  [yellow]Differs:[/yellow] {a.path}")
            console.print(f"  [yellow]    vs.:[/yellow] {b.path}")
            if diff:
                console.print()
                for line in diff.splitlines():
                    if line.startswith("+"):
                        console.print(f"  [green]{line}[/green]")
                    elif line.startswith("-"):
                        console.print(f"  [red]{line}[/red]")
                    else:
                        console.print(f"  {line}")
            console.print()
    else:
        console.print("[green]All present palinode entries are consistent.[/green]")
        console.print()

    # Closing recommendation
    system = platform.system()
    if system == "Darwin":
        canonical_desktop = "~/Library/Application Support/Claude/claude_desktop_config.json"
        console.print(
            "[bold]Which file to edit?[/bold]\n"
            f"  Claude Desktop (the app) reads: [cyan]{canonical_desktop}[/cyan]\n"
            "  Claude Code CLI reads: [cyan]~/.claude.json[/cyan]  (mcpServers under your project entry)\n"
            "  Edit the file that matches the client you are configuring."
        )
        console.print()
        console.print(
            "[yellow]Claude Desktop warning:[/yellow] quit the app (cmd+Q) before editing its config.\n"
            "  Edits made while it is running are overwritten on quit.\n"
            "  Claude Desktop also only accepts stdio (command+args) entries — url-form entries are silently stripped."
        )
    elif system == "Windows":
        console.print(
            "[bold]Which file to edit?[/bold]\n"
            "  Claude Desktop (Windows) reads: [cyan]%APPDATA%\\Claude\\claude_desktop_config.json[/cyan]\n"
            "  Claude Code CLI reads: [cyan]~/.claude.json[/cyan]\n"
            "  Edit the file that matches the client you are configuring."
        )
        console.print()
        console.print(
            "[yellow]Claude Desktop warning:[/yellow] quit the app before editing its config.\n"
            "  Edits made while it is running are overwritten on quit.\n"
            "  Claude Desktop also only accepts stdio (command+args) entries — url-form entries are silently stripped."
        )
    else:
        console.print(
            "[bold]Which file to edit?[/bold]\n"
            "  Claude Desktop (Linux) reads: [cyan]~/.config/Claude/claude_desktop_config.json[/cyan]\n"
            "  Claude Code CLI reads: [cyan]~/.claude.json[/cyan]\n"
            "  Edit the file that matches the client you are configuring."
        )
        console.print()
        console.print(
            "[yellow]Claude Desktop warning:[/yellow] quit the app before editing its config.\n"
            "  Edits made while it is running are overwritten on quit.\n"
            "  Claude Desktop also only accepts stdio (command+args) entries — url-form entries are silently stripped."
        )
    console.print()
    console.print("See [cyan]docs/MCP-CONFIG-HOMES.md[/cyan] for the full reference.")
    console.print()
