"""Guard: private issue refs (`#NNN`) must not appear in shipping source (#597).

Palinode is developed in a private repo whose issue numbers are meaningful, then
synced to a public repo where the same `#NNN` resolves to a different (or
nonexistent) issue. Two surfaces are guarded here:

1. **User-visible text** — CLI ``--help`` output and MCP tool/param schema
   descriptions. A public user reads these directly, so a leaked `#NNN` is
   actively misleading.
2. **Code comments** — cleaned in the #597 comment-scrub. `#NNN` in a comment
   is only recurring public-sync noise (the sync operator eyeballs every ref),
   so the baseline is held at zero to stop it creeping back.

Both are enumerated live and fail the build if a `#NNN` reappears.

Scope note: docstrings and shipped docs (docs/*.md) are NOT yet scanned — that
tail stays deferred on #597. `ADR-` string refs are always fine (mirrors the
scrub policy: ADR refs in comments ship, only ADR `.md` files don't).
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import click
import pytest

from palinode.cli import main as cli_root

# A private issue reference: `#` followed by 2–4 digits (a bare `#<NNN>` tag).
# Two digits min avoids matching enumerations like "fix" / "ranks"; four
# max avoids long digit runs that aren't issue numbers. (This comment is kept
# ref-free on purpose so the source-comment guard below doesn't flag itself.)
_ISSUE_REF = re.compile(r"#\d{2,4}\b")

# Shipping Python roots whose COMMENT tokens must stay issue-ref-free.
_SOURCE_ROOTS = ("palinode", "tests")


def _iter_commands(
    node: click.Command, path: str = ""
) -> list[tuple[str, click.Command]]:
    """Flatten the Click command tree into ``(dotted_path, command)`` pairs."""
    here = f"{path} {node.name}".strip()
    found = [(here, node)]
    if isinstance(node, click.Group):
        for sub in node.commands.values():
            found.extend(_iter_commands(sub, here))
    return found


def _cli_help_strings() -> list[tuple[str, str]]:
    """Every user-visible help string in the CLI: command help + option help.

    Returns ``(location_label, text)`` pairs so a failure names exactly where
    the ref lives.
    """
    strings: list[tuple[str, str]] = []
    for cmd_path, cmd in _iter_commands(cli_root):
        if cmd.help:
            strings.append((f"cmd {cmd_path} (help)", cmd.help))
        if getattr(cmd, "short_help", None):
            strings.append((f"cmd {cmd_path} (short_help)", cmd.short_help))
        for param in cmd.params:
            help_text = getattr(param, "help", None)
            if help_text:
                strings.append((f"cmd {cmd_path} --{param.name} (help)", help_text))
    return strings


def test_no_issue_refs_in_cli_help() -> None:
    """No CLI ``--help`` text may contain a private issue reference."""
    offenders = [
        (loc, _ISSUE_REF.findall(text))
        for loc, text in _cli_help_strings()
        if _ISSUE_REF.search(text)
    ]
    assert not offenders, (
        "Private issue refs found in user-visible CLI help "
        "(strip them — see #597):\n"
        + "\n".join(f"  {loc}: {refs}" for loc, refs in offenders)
    )


@pytest.mark.asyncio
async def test_no_issue_refs_in_mcp_tool_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No MCP tool description or param description may contain an issue ref.

    Runs against the full surface so no tool is skipped.
    """
    monkeypatch.setenv("PALINODE_MCP_SURFACE", "full")
    from palinode.mcp import list_tools

    offenders: list[tuple[str, list[str]]] = []
    for tool in await list_tools():
        if tool.description and _ISSUE_REF.search(tool.description):
            offenders.append(
                (f"tool {tool.name} (description)", _ISSUE_REF.findall(tool.description))
            )
        props = (tool.inputSchema or {}).get("properties", {}) or {}
        for pname, prop in props.items():
            desc = prop.get("description") if isinstance(prop, dict) else None
            if desc and _ISSUE_REF.search(desc):
                offenders.append(
                    (f"tool {tool.name}.{pname} (description)", _ISSUE_REF.findall(desc))
                )

    assert not offenders, (
        "Private issue refs found in user-visible MCP schema text "
        "(strip them — see #597):\n"
        + "\n".join(f"  {loc}: {refs}" for loc, refs in offenders)
    )


def _comment_refs_in(path: Path) -> list[tuple[int, str]]:
    """Return ``(line, comment)`` for every COMMENT token carrying an issue ref.

    Uses ``tokenize`` so only comment text is inspected — never code or
    string/docstring literals (those are out of the #597 comment-scrub scope).
    """
    src = path.read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT and _ISSUE_REF.search(tok.string):
                found.append((tok.start[0], tok.string.strip()))
    except tokenize.TokenError:
        pass
    return found


def test_no_issue_refs_in_source_comments() -> None:
    """No Python COMMENT in the shipping source may carry a private issue ref.

    Locks in the #597 comment-scrub baseline: `#NNN` in comments is recurring
    public-sync noise, so it stays at zero. Docstrings/strings are excluded.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for root in _SOURCE_ROOTS:
        for py in sorted((repo_root / root).rglob("*.py")):
            for line, text in _comment_refs_in(py):
                rel = py.relative_to(repo_root)
                offenders.append(f"  {rel}:{line}: {_ISSUE_REF.findall(text)}  →  {text[:80]}")

    assert not offenders, (
        "Private issue refs found in source comments (strip them — see #597; "
        "the scrub keeps comments issue-ref-free):\n" + "\n".join(offenders)
    )
