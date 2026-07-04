# Issue (parent) — Phase 3: per-harness smoke checklist + CLI helper.
"""palinode mcp-smoke — harness smoke checklist runbook and recorder.

Lists supported MCP harnesses with tier info, prints the smoke runbook
for a given harness, and optionally records completed smoke runs to a
JSONL log for launch-gate validation (Phase 4, #346).
"""
from __future__ import annotations

import json
import os
import platform
import subprocess  # nosec B404 - argv-form process probe, no shell
import sys
from datetime import date
from pathlib import Path
from typing import Any

import click

from palinode.core.config import config


# ---------------------------------------------------------------------------
# Harness registry — single source of truth for names + tiers
# ---------------------------------------------------------------------------

# (harness_id, display_name, tier)
_HARNESSES: list[tuple[str, str, int]] = [
    ("claude-code",     "Claude Code",     1),
    ("codex",           "Codex CLI",       1),
    ("antigravity",     "Antigravity",     1),
    ("cursor",          "Cursor",          2),
    ("claude-desktop",  "Claude Desktop",  2),
    ("cline",           "Cline",           2),
    ("zed",             "Zed",             2),
    ("windsurf",        "Windsurf",        2),
    ("continue",        "Continue",        2),
]

_HARNESS_MAP: dict[str, tuple[str, int]] = {
    h[0]: (h[1], h[2]) for h in _HARNESSES
}

# Tier 3 harnesses — documented but refused by the CLI
_TIER3_NAMES: set[str] = {"openclaw", "hermes-ai", "pi"}

# The canonical 5-call smoke sequence (same for every harness)
_SMOKE_CALLS: list[dict[str, str]] = [
    {
        "tool": "palinode_status",
        "args": "(none)",
        "expected": "Contains 'Palinode Status' or file/chunk counts",
    },
    {
        "tool": "palinode_search",
        "args": 'query: "hello"',
        "expected": "Text response (results or 'No results found.')",
    },
    {
        "tool": "palinode_save",
        "args": 'content: "Smoke test <harness> <date>", type: "Insight", slug: "smoke-<harness>"',
        "expected": "Saved confirmation; no 'Error' or 'Save failed' prefix",
    },
    {
        "tool": "palinode_list",
        "args": "(none)",
        "expected": "Listing includes 'smoke-<harness>'",
    },
    {
        "tool": "palinode_read",
        "args": 'file_path: "insights/smoke-<harness>.md"',
        "expected": "Body contains 'Smoke test'",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_tty() -> bool:
    return sys.stdout.isatty()


def claude_desktop_running() -> bool | None:
    """Best-effort detection of a live Claude Desktop process (#373).

    Claude Desktop holds ``claude_desktop_config.json`` in memory and rewrites
    it on quit, silently clobbering any edit made while it runs (and stripping
    ``url``-form MCP entries). So a config edit must be done with the app
    *quit* — this lets callers warn before they walk someone into the
    edit-gets-overwritten trap.

    Matches the platform-specific *application* (the macOS ``Claude.app``
    bundle, the Windows ``Claude.exe`` image) rather than a bare ``claude``
    token, so it never false-positives on the Claude **Code** CLI (this very
    process) or a ``claude`` shell alias.

    Returns:
        True if a Claude Desktop process is detected, False if confidently not,
        and ``None`` when detection isn't possible (unknown platform, the probe
        tool is missing, or it errored) — callers should treat ``None`` as
        "couldn't tell," not "safe."
    """
    system = platform.system()
    try:
        if system in ("Darwin", "Linux"):
            # macOS app: /Applications/Claude.app/Contents/MacOS/Claude
            # Linux app: an Electron binary under a .../claude-desktop/ path.
            # Match the bundle/app path via -f so the bare `claude` CLI (no
            # ".app"/"claude-desktop" path segment) can't match.
            pattern = "Claude.app" if system == "Darwin" else "claude-desktop"
            proc = subprocess.run(  # nosec B603,B607 - argv form, no shell
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # pgrep: 0 = match, 1 = no match, >1 = error.
            if proc.returncode == 0:
                return True
            if proc.returncode == 1:
                return False
            return None
        if system == "Windows":
            proc = subprocess.run(  # nosec B603,B607 - argv form, no shell
                ["tasklist", "/FI", "IMAGENAME eq Claude.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode != 0:
                return None
            return "Claude.exe" in proc.stdout
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    return None


def _smoke_log_path() -> Path:
    """Resolve the JSONL log path inside the memory dir."""
    memory_dir = os.environ.get("PALINODE_DIR", config.memory_dir)
    return Path(os.path.expanduser(memory_dir)) / ".palinode" / "harness-smoke-runs.jsonl"


def _runbook_text(harness_id: str) -> str:
    """Build a plain-text runbook for a harness."""
    display, tier = _HARNESS_MAP[harness_id]
    lines: list[str] = []
    lines.append(f"# Smoke checklist: {display} (Tier {tier})")
    lines.append("")
    lines.append("Run these 5 calls in order inside the harness:")
    lines.append("")
    for i, call in enumerate(_SMOKE_CALLS, 1):
        args = call["args"].replace("<harness>", harness_id)
        expected = call["expected"].replace("<harness>", harness_id)
        lines.append(f"  {i}. {call['tool']}")
        lines.append(f"     Args: {args}")
        lines.append(f"     Expected: {expected}")
        lines.append("")
    lines.append("After completing all 5 calls, record the result:")
    lines.append(f"  palinode mcp-smoke {harness_id} --record")
    lines.append("")
    return "\n".join(lines)


def _json_payload(harness_id: str) -> dict[str, Any]:
    """Build the parseable JSON record for a harness."""
    display, tier = _HARNESS_MAP[harness_id]
    calls = []
    expected = []
    for call in _SMOKE_CALLS:
        calls.append({
            "tool": call["tool"],
            "args": call["args"].replace("<harness>", harness_id),
        })
        expected.append(call["expected"].replace("<harness>", harness_id))
    return {
        "harness": harness_id,
        "display_name": display,
        "tier": tier,
        "calls": calls,
        "expected": expected,
    }


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

@click.command("mcp-smoke")
@click.argument("harness", required=False)
@click.option(
    "--list", "list_harnesses",
    is_flag=True,
    default=False,
    help="List all supported harnesses with their tier.",
)
@click.option(
    "--json", "output_json",
    is_flag=True,
    default=False,
    help="Emit a parseable JSON record for the harness.",
)
@click.option(
    "--record",
    is_flag=True,
    default=False,
    help="Append a completed smoke-run record to the JSONL log.",
)
@click.option(
    "--date", "run_date",
    default=None,
    help="Override the run date (YYYY-MM-DD). Defaults to today.",
)
@click.option(
    "--operator",
    default=None,
    help="Name of the person or agent who ran the smoke test.",
)
def mcp_smoke(
    harness: str | None,
    list_harnesses: bool,
    output_json: bool,
    record: bool,
    run_date: str | None,
    operator: str | None,
) -> None:
    """Harness smoke checklist runbook and recorder.

    List supported MCP harnesses, print a copy-paste smoke checklist for
    a given harness, or record a completed smoke run for the launch gate.

    \b
    Examples:
      palinode mcp-smoke --list
      palinode mcp-smoke claude-code
      palinode mcp-smoke cursor --json
      palinode mcp-smoke codex --record --operator paul
    """
    # --list mode
    if list_harnesses:
        if _is_tty() and not output_json:
            try:
                from rich.console import Console
                from rich.table import Table
                console = Console()
                table = Table(title="Supported MCP Harnesses")
                table.add_column("Harness ID", style="cyan")
                table.add_column("Display Name")
                table.add_column("Tier", justify="center")
                for hid, display, tier in _HARNESSES:
                    table.add_row(hid, display, str(tier))
                console.print(table)
            except ImportError:
                # Fallback if rich not available
                for hid, display, tier in _HARNESSES:
                    click.echo(f"{hid}\t{display}\tTier {tier}")
        else:
            # Machine-friendly: JSON when piped or --json
            payload = [
                {"harness": hid, "display_name": display, "tier": tier}
                for hid, display, tier in _HARNESSES
            ]
            click.echo(json.dumps(payload, indent=2))
        return

    # Require harness argument for all other modes
    if not harness:
        raise click.UsageError(
            "Provide a harness name or use --list to see all supported harnesses."
        )

    # Tier 3 hard refusal
    if harness in _TIER3_NAMES:
        click.echo(
            f"Error: '{harness}' is a Tier 3 harness (future / best effort).\n"
            f"Tier 3 harnesses are not yet supported for smoke testing.\n"
            f"See docs/HARNESS-SMOKE.md for details. Contributions welcome.",
            err=True,
        )
        sys.exit(1)

    # Unknown harness
    if harness not in _HARNESS_MAP:
        known = ", ".join(sorted(_HARNESS_MAP.keys()))
        tier3 = ", ".join(sorted(_TIER3_NAMES))
        click.echo(
            f"Error: unknown harness '{harness}'.\n"
            f"Supported (Tier 1+2): {known}\n"
            f"Tier 3 (future): {tier3}",
            err=True,
        )
        sys.exit(1)

    # --record mode
    if record:
        today = run_date or date.today().isoformat()
        display, tier = _HARNESS_MAP[harness]
        entry: dict[str, Any] = {
            "harness": harness,
            "tier": tier,
            "date": today,
            "passed": True,
        }
        if operator:
            entry["operator"] = operator

        log_path = _smoke_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

        if _is_tty():
            click.echo(f"Recorded smoke run for {display} (Tier {tier}) on {today}")
            click.echo(f"Log: {log_path}")
        else:
            click.echo(json.dumps(entry, indent=2))
        return

    # --json mode
    if output_json:
        click.echo(json.dumps(_json_payload(harness), indent=2))
        return

    # Claude Desktop rewrites its config on quit, clobbering live edits.
    # Warn loudly (to stderr, so it doesn't pollute a piped runbook) when the
    # app is detected running before the user follows a runbook that has them
    # touch the config. Detection is best-effort: a None ("couldn't tell")
    # stays silent rather than crying wolf.
    if harness == "claude-desktop" and claude_desktop_running() is True:
        click.echo(
            "WARNING: Claude Desktop appears to be running.\n"
            "  Quit it (cmd+Q / fully exit) BEFORE editing "
            "claude_desktop_config.json — the app rewrites that file on quit "
            "and will silently overwrite your edit (and strip url-form MCP "
            "entries). Recovery order: quit → edit → relaunch.\n"
            "  See docs/MCP-CONFIG-HOMES.md.",
            err=True,
        )

    # Default: print the runbook
    click.echo(_runbook_text(harness))
