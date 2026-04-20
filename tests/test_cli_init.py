"""Tests for `palinode init` — the zero-friction scaffolding command.

These are regression guards for two things:

1. The deterministic slash commands (`/ps` and `/wrap`). If someone refactors
   `init.py` and accidentally reintroduces smart-dispatch, these tests fail.
2. The idempotent install flow — re-running init must not corrupt existing
   files, and merging into existing JSON must not stomp unrelated keys.
"""
import json
from pathlib import Path

from click.testing import CliRunner

from palinode.cli import main
from palinode.cli.init import (
    PS_COMMAND_BODY,
    WRAP_COMMAND_BODY,
    _slugify,
)


# ---- Slug ---------------------------------------------------------------


def test_slugify_basic():
    assert _slugify("my-project") == "my-project"
    assert _slugify("My Project") == "my-project"
    assert _slugify("My Project!") == "my-project"
    assert _slugify("palinode") == "palinode"
    assert _slugify("foo_bar.baz") == "foo_bar-baz"


def test_slugify_falls_back_to_project():
    assert _slugify("") == "project"
    assert _slugify("!!!") == "project"


# ---- Deterministic prompt guards ----------------------------------------


def test_ps_command_is_deterministic():
    """/ps must always call palinode_save with type=ProjectSnapshot, never session_end."""
    body = PS_COMMAND_BODY
    assert "palinode_save" in body
    assert '"ProjectSnapshot"' in body
    assert "This command is deterministic" in body
    assert "Do not call any other tool" in body
    # Must NOT contain smart dispatch instructions
    assert "palinode_session_end" not in body or "use `/wrap`" in body
    assert "Pick the right tool" not in body


def test_wrap_command_is_deterministic():
    """/wrap must always call palinode_session_end, never palinode_save."""
    body = WRAP_COMMAND_BODY
    assert "palinode_session_end" in body
    assert "summary" in body
    assert "decisions" in body
    assert "blockers" in body
    assert "This command is deterministic" in body
    assert "Do not call any other tool" in body
    # Must tell the agent what to say after saving
    assert "safe to /clear now" in body
    # Must NOT dispatch to palinode_save
    assert "palinode_save" not in body or "use `/ps`" in body


def test_ps_and_wrap_are_different():
    """The two commands must be distinct operations, not aliases."""
    assert PS_COMMAND_BODY != WRAP_COMMAND_BODY


# ---- Scaffolding flow ---------------------------------------------------


def test_init_creates_all_files(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    assert (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "hooks" / "palinode-session-end.sh").exists()
    assert (tmp_path / ".claude" / "commands" / "ps.md").exists()
    assert (tmp_path / ".claude" / "commands" / "wrap.md").exists()
    assert (tmp_path / ".mcp.json").exists()


def test_init_uses_directory_name_as_slug(tmp_path: Path):
    proj = tmp_path / "my-awesome-project"
    proj.mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(proj)])
    assert result.exit_code == 0

    content = (proj / ".claude" / "CLAUDE.md").read_text()
    assert "my-awesome-project" in content


def test_init_explicit_project_slug_wins(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, [
        "init", "--dir", str(tmp_path), "--project", "custom-slug",
    ])
    assert result.exit_code == 0

    content = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert "custom-slug" in content


def test_init_dry_run_writes_nothing(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output

    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".mcp.json").exists()


def test_init_is_idempotent(tmp_path: Path):
    runner = CliRunner()
    first = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert first.exit_code == 0

    ps_content = (tmp_path / ".claude" / "commands" / "ps.md").read_text()
    settings_content = (tmp_path / ".claude" / "settings.json").read_text()

    second = runner.invoke(main, ["init", "--dir", str(tmp_path)])
    assert second.exit_code == 0
    assert "skipped" in second.output

    # Files unchanged
    assert (tmp_path / ".claude" / "commands" / "ps.md").read_text() == ps_content
    assert (tmp_path / ".claude" / "settings.json").read_text() == settings_content


def test_init_appends_to_existing_claude_md(tmp_path: Path):
    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("# Pre-existing header\n\nSome project rules here.\n")

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--no-hook", "--no-mcp", "--no-slash"])
    assert result.exit_code == 0

    content = claude_md.read_text()
    assert "# Pre-existing header" in content
    assert "Some project rules here." in content
    assert "## Memory (Palinode)" in content


def test_init_merges_into_existing_settings_json(tmp_path: Path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "other.sh"}]}]},
        "unrelated_key": "should_survive",
    }, indent=2))

    runner = CliRunner()
    result = runner.invoke(main, ["init", "--dir", str(tmp_path), "--no-claudemd", "--no-mcp", "--no-slash"])
    assert result.exit_code == 0

    merged = json.loads(settings.read_text())
    assert merged["unrelated_key"] == "should_survive"
    assert "PreToolUse" in merged["hooks"]
    assert "SessionEnd" in merged["hooks"]
    assert len(merged["hooks"]["SessionEnd"]) == 1


def test_init_scope_flags(tmp_path: Path):
    """--no-claudemd --no-hook --no-mcp should only write slash commands."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "init", "--dir", str(tmp_path),
        "--no-claudemd", "--no-hook", "--no-mcp",
    ])
    assert result.exit_code == 0

    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".claude" / "hooks").exists()
    assert not (tmp_path / ".mcp.json").exists()
    assert (tmp_path / ".claude" / "commands" / "ps.md").exists()
    assert (tmp_path / ".claude" / "commands" / "wrap.md").exists()
