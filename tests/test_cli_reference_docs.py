"""``docs/CLI.md`` must name every registered CLI command, and nothing else.

Before this page existed, 18 of 46 CLI commands appeared in no shipping doc,
and nothing would have noticed a 19th. The failure mode is silent in both
directions: a new command lands with no entry, or a command is removed and its
entry outlives it. This test is the same forcing function the surface-parity
tests apply to MCP/API/CLI parameter names, aimed at the reference page.

- Every visible click command reachable from ``palinode.cli.main`` — including
  subcommands of groups and the ``dream`` alias — must have a heading of the
  form ``### `palinode <name>``` (``####`` for subcommands).
- Every such heading must correspond to a registered command.

Hidden commands (``hidden=True``) are excluded from the check rather than
documented: hidden is click's own statement that the command is not part of
the user-facing surface. There are none today; the exclusion is so that adding
one does not force an entry.
"""
from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

from palinode.cli import main as cli_root

CLI_DOC = Path(__file__).resolve().parent.parent / "docs" / "CLI.md"

# A command heading: two-to-four hashes, then the full invocation in backticks.
# Group headings (`palinode trigger`) and subcommand headings
# (`palinode trigger add`) both match; the group itself is a registered click
# object, so it needs an entry too.
_HEADING = re.compile(r"^#{2,4} `palinode ((?:[a-z][a-z0-9-]*)(?: [a-z][a-z0-9-]*)*)`\s*$", re.M)


def _registered_commands() -> set[str]:
    """Every visible command path under the root group, space-joined."""
    found: set[str] = set()

    def walk(group: click.Group, prefix: str) -> None:
        for name, cmd in group.commands.items():
            if cmd.hidden:
                continue
            path = f"{prefix}{name}"
            found.add(path)
            if isinstance(cmd, click.Group):
                walk(cmd, f"{path} ")

    walk(cli_root, "")
    return found


def _documented_commands() -> list[str]:
    return _HEADING.findall(CLI_DOC.read_text(encoding="utf-8"))


def test_cli_reference_exists() -> None:
    assert CLI_DOC.is_file(), "docs/CLI.md is the CLI command reference and must ship"


def test_every_registered_command_is_documented() -> None:
    missing = sorted(_registered_commands() - set(_documented_commands()))
    assert not missing, (
        "Registered CLI commands with no heading in docs/CLI.md: "
        f"{missing}. Add a `### \\`palinode <name>\\`` entry (synopsis, purpose, "
        "options with defaults, an example, output pattern)."
    )


def test_every_documented_command_is_registered() -> None:
    stale = sorted(set(_documented_commands()) - _registered_commands())
    assert not stale, (
        "docs/CLI.md documents commands that are not registered in "
        f"palinode.cli.main: {stale}. Remove the entry or register the command."
    )


def test_no_duplicate_headings() -> None:
    headings = _documented_commands()
    dupes = sorted({h for h in headings if headings.count(h) > 1})
    assert not dupes, f"docs/CLI.md has more than one entry for: {dupes}"


@pytest.mark.parametrize("name", sorted(_registered_commands()))
def test_documented_synopsis_matches_click_usage(name: str) -> None:
    """The fenced synopsis under each heading must start with the command's
    own invocation, so a renamed option or argument shows up as a diff here
    rather than as a reader's surprise."""
    text = CLI_DOC.read_text(encoding="utf-8")
    heading = re.search(rf"^#{{2,4}} `palinode {re.escape(name)}`\s*$", text, re.M)
    assert heading is not None
    tail = text[heading.end():]
    fence = re.search(r"```\n(.*?)\n```", tail, re.S)
    assert fence is not None, f"no fenced synopsis under `palinode {name}`"
    first_line = fence.group(1).splitlines()[0]
    assert first_line.startswith(f"palinode {name}"), (
        f"synopsis under `palinode {name}` starts with {first_line!r}"
    )
