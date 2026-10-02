"""palinode mcp-config --diagnose — surface all MCP config-file homes.

Walks every known canonical location where a running client might read
MCP server configuration, parses each one, and reports what it found for
the `palinode` server entry.

Read-only diagnostic: we never write to any user config file.
"""
from __future__ import annotations

import difflib
import json
import os
import platform
import re
import sys
import sysconfig
from importlib import metadata
from pathlib import Path
from typing import Any

import click

from palinode.cli._format import console
from palinode.core.context_prime import PROJECT_HEADER


# ---------------------------------------------------------------------------
# Emit-mode constants
# ---------------------------------------------------------------------------

# Generic placeholder — NEVER bake a real internal host/IP into shipping output.
# The user substitutes their own palinode host (the machine running palinode-mcp).
DEFAULT_HTTP_HOST = "<palinode-host>"
DEFAULT_HTTP_PORT = 6341
DEFAULT_STDIO_COMMAND = "palinode-mcp"
_PROJECT_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


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


def _build_http_entry(url: str, bearer: str | None = None,
                      project: str | None = None) -> dict[str, Any]:
    """Build the ``palinode`` server entry for streamable-HTTP transport.

    With *project*, the client carries it in the ``X-Palinode-Project`` header:
    the HTTP server runs on another machine, so neither the client's directory
    nor its environment reaches it, and the header is how this client's project
    does.
    """
    entry: dict[str, Any] = {"type": "http", "url": url}
    headers: dict[str, str] = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if project is not None:
        headers[PROJECT_HEADER] = project
    if headers:
        entry["headers"] = headers
    return entry


def _resolve_executable(executable: str | None = None) -> str:
    """Use this installation's recorded script, never an unrelated PATH hit."""
    name = DEFAULT_STDIO_COMMAND + (".exe" if os.name == "nt" else "")
    if executable is not None:
        path = Path(executable).expanduser()
        if not path.is_absolute() or ".." in path.parts:
            raise click.ClickException("--executable must be an absolute path without '..'.")
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise click.ClickException("--executable must name the real executable, not a symlink.")
        candidates = [path]
    else:
        try:
            dist = metadata.distribution("palinode")
            candidates = [Path(dist.locate_file(f)).resolve()
                          for f in (dist.files or []) if f.name == name]
        except metadata.PackageNotFoundError:
            candidates = []
        if not candidates:
            # Do not resolve sys.executable: venv Python often links to base Python.
            candidates = [Path(sys.executable).absolute().parent / name,
                          Path(sysconfig.get_path("scripts")) / name]
    usable = sorted({p.absolute() for p in candidates if p.is_file() and os.access(p, os.X_OK)})
    if len(usable) != 1:
        reason = "Ambiguous" if len(usable) > 1 else "Missing or non-executable"
        raise click.ClickException(
            f"{reason} palinode-mcp for this installation. "
            "Run the intended installation's palinode command, reinstall Palinode there, "
            "or pass --executable /absolute/path/to/palinode-mcp."
        )
    return str(usable[0])


def _validate_project_slug(_ctx: click.Context, _param: click.Parameter,
                           value: str | None) -> str | None:
    """Accept only a plain, safe project slug for a generated process env."""
    if value is None:
        return None
    if not _PROJECT_SLUG_RE.fullmatch(value):
        raise click.BadParameter(
            "must be a slug containing letters, numbers, '.', '_' or '-' "
            "(not a project/ ref or path)"
        )
    return value


def _stdio_env(project: str | None = None) -> dict[str, str]:
    """Carry connection settings across a GUI launch without copying the shell."""
    from palinode.core.config import config

    env = {
        "PALINODE_DIR": str(Path(config.memory_dir).expanduser().absolute()),
        "PALINODE_API_HOST": config.services.api.host,
        "PALINODE_API_PORT": str(config.services.api.port),
    }
    for key in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE", "PALINODE_PROJECT",
                "PALINODE_MCP_SURFACE", "PALINODE_ORG", "PALINODE_MEMBER",
                "PALINODE_HARNESS", "PALINODE_AGENT"):
        if key in os.environ:
            value = os.environ[key]
            if key.endswith("_FILE") and value:
                value = str(Path(value).expanduser().absolute())
            env[key] = value
    # A generated entry names one spawned stdio process. Its explicit scope
    # must win over a shell's ambient scope without changing any other client.
    if project is not None:
        env["PALINODE_PROJECT"] = project
    return env


def _build_stdio_entry(executable: str | None = None,
                       project: str | None = None) -> dict[str, Any]:
    """Build a local entry bound to the intended installed executable."""
    return {"command": _resolve_executable(executable), "env": _stdio_env(project)}


EDITORS = ("generic", "claude-code", "codex", "continue", "claude-desktop")
MERGE_HELP = {
    "generic": "Merge palinode into your client's mcpServers object; consult its config documentation.",
    "claude-code": "Merge palinode into mcpServers in .mcp.json at your project root. "
                   "Approve the project server when Claude Code prompts; use /mcp to verify.",
    "codex": "Merge [mcp_servers.palinode] into ~/.codex/config.toml "
             "($CODEX_HOME/config.toml if set), or .codex/config.toml in a trusted project. "
             "Update an existing table instead of appending a duplicate. Use /mcp to verify.",
    "continue": "Merge the palinode item into the mcpServers list in ~/.continue/config.yaml. "
                "Keep the existing name, version, schema and models; use Agent mode to verify.",
    "claude-desktop": "Merge palinode into mcpServers in Claude Desktop's claude_desktop_config.json "
                      "(macOS: ~/Library/Application Support/Claude/; Windows: %APPDATA%/Claude/). "
                      "Quit Desktop before editing, then relaunch.",
}


def _client_block(entry: dict[str, Any], editor: str) -> dict[str, Any]:
    entry = dict(entry)
    if editor == "codex":
        entry.pop("type", None)
        if "headers" in entry:
            entry["http_headers"] = entry.pop("headers")
        return {"mcp_servers": {"palinode": entry}}
    if editor == "continue":
        entry["type"] = "streamable-http" if "url" in entry else "stdio"
        if "headers" in entry:
            entry["requestOptions"] = {"headers": entry.pop("headers")}
        return {"mcpServers": [{"name": "palinode", **entry}]}
    if editor == "claude-code" and "command" in entry:
        entry["type"] = "stdio"
    return _wrap_block(entry)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _serialize_block(block: dict[str, Any], editor: str) -> str:
    if editor == "continue":
        import yaml
        return yaml.safe_dump(block, sort_keys=False, allow_unicode=True).rstrip()
    if editor == "codex":
        # Our TOML subset is strings and string maps. JSON quoting also escapes
        # Windows backslashes, quotes and control characters correctly for TOML.
        entry = block["mcp_servers"]["palinode"]
        lines = ["[mcp_servers.palinode]"]
        for key, value in entry.items():
            if not isinstance(value, dict):
                lines.append(f"{key} = {_toml_string(value)}")
        for key, value in entry.items():
            if isinstance(value, dict):
                lines.extend(["", f"[mcp_servers.palinode.{key}]"])
                lines.extend(f"{_toml_string(k)} = {_toml_string(v)}"
                             for k, v in value.items())
        return "\n".join(lines)
    return json.dumps(block, indent=2)


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

    paths: list[tuple[str, Path]] = [
        ("Claude Code project (.mcp.json)", Path.cwd() / ".mcp.json"),
        ("Codex user config", Path(os.environ.get("CODEX_HOME") or home / ".codex") / "config.toml"),
        ("Codex project config (trusted projects)", Path.cwd() / ".codex" / "config.toml"),
        ("Continue user config", home / ".continue" / "config.yaml"),
    ]

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
    """Read and parse a JSON, TOML or YAML config file.

    Returns (data, error_message).  One of the two will always be None.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read file: {exc}"

    if path.suffix == ".toml":
        import tomllib
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None, "TOML parse error (file contents omitted)"
    elif path.suffix in (".yaml", ".yml"):
        import yaml
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return None, "YAML parse error (file contents omitted)"
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None, "JSON parse error (file contents omitted)"

    if not isinstance(data, dict):
        return None, "unexpected top-level type (expected object)"

    return data, None


def _extract_palinode_entry(data: dict[str, Any]) -> dict[str, Any] | None:
    """Pull out the palinode server block, if present.

    Checks both ``mcpServers`` (Claude Desktop / Cline / Cursor shape) and
    ``context_servers`` (Zed shape).  Returns the first match found.
    """
    projects = data.get("projects")
    project = projects.get(str(Path.cwd())) if isinstance(projects, dict) else None
    local_servers = project.get("mcpServers") if isinstance(project, dict) else None
    local_entry = local_servers.get("palinode") if isinstance(local_servers, dict) else None
    if isinstance(local_entry, dict):
        return local_entry
    for key in ("mcpServers", "context_servers", "mcp_servers"):
        servers = data.get(key)
        if isinstance(servers, dict):
            entry = servers.get("palinode")
            if isinstance(entry, dict):
                return entry
        if isinstance(servers, list):
            for entry in servers:
                if isinstance(entry, dict) and entry.get("name") == "palinode":
                    return entry
    return None


def _redact(value: Any) -> Any:
    """Redact credential environment values, headers, arguments and URL credentials."""
    from urllib.parse import urlsplit, urlunsplit

    if isinstance(value, list):
        return [_redact(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ("env", "headers", "http_headers") and isinstance(item, dict):
            result[key] = {
                name: ("<redacted>" if (key != "env" and name != PROJECT_HEADER) or any(
                    part in name.upper() for part in ("TOKEN", "SECRET", "KEY", "PASSWORD", "AUTH")
                ) else setting)
                for name, setting in item.items()
            }
        elif key == "args":
            result[key] = ["<redacted>"] if item else []
        elif key in ("url", "serverUrl") and isinstance(item, str):
            try:
                parts = urlsplit(item)
                result[key] = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1],
                                         parts.path, "<redacted>" if parts.query else "", ""))
            except ValueError:
                result[key] = "<redacted>"
        else:
            result[key] = _redact(item)
    return result


def _render_entry(entry: dict[str, Any] | None) -> str:
    """Turn a palinode MCP entry into a concise single-line description."""
    if entry is None:
        return "(no palinode entry)"

    entry = _redact(entry)
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
            d["palinode_entry"] = _redact(self.entry)
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
                        json.dumps(_redact(a.entry), sort_keys=True, indent=2).splitlines(),
                        json.dumps(_redact(b.entry), sort_keys=True, indent=2).splitlines(),
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
    editor: str = "generic",
    executable: str | None = None,
    project: str | None = None,
) -> None:
    """Print a client-native fragment only; never open a destination for writing."""
    if output_json and editor in ("codex", "continue"):
        raise click.UsageError(f"{editor} requires {'TOML' if editor == 'codex' else 'YAML'}; omit --json.")
    if emit_http and editor == "claude-desktop":
        raise click.UsageError("Claude Desktop's local JSON config requires --stdio; use its Connectors UI for HTTP.")
    if emit_http:
        entry = _build_http_entry(_http_url(url, host, port), bearer=bearer, project=project)
    else:
        entry = _build_stdio_entry(executable, project)
    block = _client_block(entry, editor)
    if output_json or not sys.stdout.isatty():
        click.echo(_serialize_block(block, editor))
        return
    click.echo(f"Palinode MCP — {editor} {'HTTP' if emit_http else 'stdio'} preview")
    click.echo(MERGE_HELP[editor])
    click.echo("Generation never writes settings. Merge the fragment; do not overwrite the file.")
    # The pasteable fragment carries configured credentials only in raw output.
    safe_block = _redact(block)
    if safe_block != block:
        click.echo("Credentials hidden in this preview. Pipe output to a private preview file for the usable fragment.")
    click.echo(_serialize_block(safe_block, editor))
    click.echo("See docs/MCP-CONFIG-HOMES.md for destinations and verification.")


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
    help="Optional bearer token for --http; included only in raw config output.",
)
@click.option("--editor", "--client", type=click.Choice(EDITORS), default="generic",
              show_default=True, help="Client-native format and merge guidance (requires --stdio or --http).")
@click.option("--executable", default=None, help="Explicit absolute MCP executable for --stdio.")
@click.option(
    "--project",
    callback=_validate_project_slug,
    help=(
        "Project slug for this generated client: PALINODE_PROJECT for --stdio, "
        "the X-Palinode-Project header for --http."
    ),
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
    editor: str,
    executable: str | None,
    project: str | None,
) -> None:
    """Surface all MCP config-file homes, or emit a ready-to-paste config block.

    Default (no flags) walks every location a running MCP client might read,
    parses the client config, and reports what it finds for the 'palinode' server entry —
    useful when you edited one file and changes didn't take effect.

    With --http or --stdio, instead emit a copy-pasteable config block for the
    chosen transport. --http is the streamable-HTTP form (remote server, reuses
    the warm BGE-M3 model behind the running service, no SSH/cold-start).

    Read-only: we never write to any user config file.

    See docs/MCP-CONFIG-HOMES.md for the full canonical-location reference.
    """
    if executable and not emit_stdio:
        raise click.UsageError("--executable requires --stdio.")
    if project is not None and not (emit_stdio or emit_http):
        raise click.UsageError("--project requires --stdio or --http.")
    if editor != "generic" and not (emit_http or emit_stdio):
        raise click.UsageError("--editor requires --stdio or --http.")
    if emit_stdio and (url or bearer or host != DEFAULT_HTTP_HOST or port != DEFAULT_HTTP_PORT):
        raise click.UsageError("--url, --host, --port and --bearer are HTTP options; use PALINODE_API_* for stdio.")
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
            editor=editor,
            executable=executable,
            project=project,
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
            console.print("    [dim]not present[/dim]")
        elif r.error:
            console.print(f"  [red]✗[/red] {r.path}")
            console.print(f"    [red]ERROR parsing:[/red] {r.error}")
        elif r.entry is None:
            console.print(f"  [yellow]·[/yellow] {r.path}")
            console.print("    file exists — no 'palinode' entry in mcpServers / context_servers")
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
            "Editing the wrong one can silently leave the intended configuration unchanged."
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

    # Live check: the warning above always prints, but if Claude Desktop is
    # actually running right now, say so loudly — an edit made while it is live
    # is silently discarded on the app's next quit. Best-effort: a None verdict
    # ("couldn't tell") stays quiet rather than crying wolf. Lazy import keeps
    # the process probe off the module-load path.
    from palinode.cli.mcp_smoke import claude_desktop_running

    if claude_desktop_running() is True:
        console.print()
        console.print(
            "[bold red]⚠ Claude Desktop appears to be RUNNING right now.[/bold red]\n"
            "  Quit it (cmd+Q / fully exit) BEFORE editing claude_desktop_config.json —\n"
            "  an edit made while it is live is discarded on the next quit.\n"
            "  Recovery order: quit → edit → relaunch."
        )

    console.print()
    console.print("See [cyan]docs/MCP-CONFIG-HOMES.md[/cyan] for the full reference.")
    console.print()
