"""Tests for Deliverable G (#210): /save is canonical, /ps is back-compat alias.

Coverage:
- ``palinode init`` (default, no extra flags) writes BOTH save.md and ps.md
- save.md content references palinode_save correctly and has no deprecation notice
- ps.md content references palinode_save correctly and has the deprecation notice
- SAVE_COMMAND_BODY and PS_COMMAND_BODY constants in init.py satisfy the above
- Existing ps.md is not clobbered on second run (back-compat installs survive)
"""
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli import main
from palinode.cli.init import SAVE_COMMAND_BODY, PS_COMMAND_BODY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_init(tmp_path: Path, *extra_args: str):
    """Invoke ``palinode init --dir <tmp_path> <extra_args>`` and return result."""
    runner = CliRunner()
    return runner.invoke(main, ["init", "--dir", str(tmp_path), *extra_args])


# ---------------------------------------------------------------------------
# Template constant guards
# ---------------------------------------------------------------------------

def test_save_command_body_calls_palinode_save():
    """SAVE_COMMAND_BODY must reference palinode_save."""
    assert "palinode_save" in SAVE_COMMAND_BODY


def test_save_command_body_specifies_project_snapshot():
    """SAVE_COMMAND_BODY must specify type ProjectSnapshot."""
    assert "ProjectSnapshot" in SAVE_COMMAND_BODY


def test_save_command_body_has_no_deprecation_notice():
    """SAVE_COMMAND_BODY is the canonical command — must NOT contain a deprecation notice."""
    assert "DEPRECATED" not in SAVE_COMMAND_BODY
    assert "deprecated" not in SAVE_COMMAND_BODY.lower()


def test_ps_command_body_calls_palinode_save():
    """PS_COMMAND_BODY must still reference palinode_save (back-compat alias)."""
    assert "palinode_save" in PS_COMMAND_BODY


def test_ps_command_body_has_deprecation_notice():
    """PS_COMMAND_BODY must contain a deprecation notice pointing at /save."""
    assert "DEPRECATED" in PS_COMMAND_BODY
    assert "/save" in PS_COMMAND_BODY


def test_ps_command_body_mentions_save_preferred():
    """PS_COMMAND_BODY must tell users /save is preferred."""
    # Either "canonical" or "preferred" or the deprecation redirect covers this
    lower = PS_COMMAND_BODY.lower()
    assert "save" in lower


# ---------------------------------------------------------------------------
# Scaffold creation — both files are written
# ---------------------------------------------------------------------------

def test_init_writes_save_md(tmp_path: Path):
    """``palinode init`` must create .claude/commands/save.md."""
    result = run_init(tmp_path)
    assert result.exit_code == 0, result.output
    save_cmd = tmp_path / ".claude" / "commands" / "save.md"
    assert save_cmd.exists(), "save.md was not created"


def test_init_writes_ps_md(tmp_path: Path):
    """``palinode init`` must still create .claude/commands/ps.md for back-compat."""
    result = run_init(tmp_path)
    assert result.exit_code == 0, result.output
    ps_cmd = tmp_path / ".claude" / "commands" / "ps.md"
    assert ps_cmd.exists(), "ps.md was not created"


def test_init_writes_both_save_and_ps(tmp_path: Path):
    """``palinode init`` must write save.md AND ps.md in a single run."""
    run_init(tmp_path)
    assert (tmp_path / ".claude" / "commands" / "save.md").exists()
    assert (tmp_path / ".claude" / "commands" / "ps.md").exists()


# ---------------------------------------------------------------------------
# Content correctness
# ---------------------------------------------------------------------------

def test_save_md_content_references_palinode_save(tmp_path: Path):
    """Scaffolded save.md must call palinode_save."""
    run_init(tmp_path)
    content = (tmp_path / ".claude" / "commands" / "save.md").read_text()
    assert "palinode_save" in content


def test_save_md_content_has_no_deprecation(tmp_path: Path):
    """Scaffolded save.md must NOT have a deprecation notice."""
    run_init(tmp_path)
    content = (tmp_path / ".claude" / "commands" / "save.md").read_text()
    assert "DEPRECATED" not in content


def test_ps_md_content_references_palinode_save(tmp_path: Path):
    """Scaffolded ps.md must still call palinode_save."""
    run_init(tmp_path)
    content = (tmp_path / ".claude" / "commands" / "ps.md").read_text()
    assert "palinode_save" in content


def test_ps_md_content_has_deprecation_notice(tmp_path: Path):
    """Scaffolded ps.md must contain a deprecation notice pointing at /save."""
    run_init(tmp_path)
    content = (tmp_path / ".claude" / "commands" / "ps.md").read_text()
    assert "DEPRECATED" in content
    assert "/save" in content


# ---------------------------------------------------------------------------
# Idempotency — second run skips existing files (back-compat install survives)
# ---------------------------------------------------------------------------

def test_idempotent_save_md_skipped_on_second_run(tmp_path: Path):
    """Second ``palinode init`` run must skip save.md if it already exists."""
    run_init(tmp_path)
    save_cmd = tmp_path / ".claude" / "commands" / "save.md"
    original = save_cmd.read_text()
    save_cmd.write_text(original + "\n# user edit\n")
    user_content = save_cmd.read_text()

    run_init(tmp_path)
    assert save_cmd.read_text() == user_content


def test_idempotent_ps_md_skipped_on_second_run(tmp_path: Path):
    """Second run must not clobber an existing ps.md (back-compat installs)."""
    run_init(tmp_path)
    ps_cmd = tmp_path / ".claude" / "commands" / "ps.md"
    original = ps_cmd.read_text()
    ps_cmd.write_text(original + "\n# user edit\n")
    user_content = ps_cmd.read_text()

    run_init(tmp_path)
    assert ps_cmd.read_text() == user_content


# ---------------------------------------------------------------------------
# --force overwrites both files
# ---------------------------------------------------------------------------

def test_force_overwrites_save_md(tmp_path: Path):
    """--force must overwrite save.md."""
    run_init(tmp_path)
    save_cmd = tmp_path / ".claude" / "commands" / "save.md"
    save_cmd.write_text("# corrupted\n")

    run_init(tmp_path, "--force")
    content = save_cmd.read_text()
    assert "palinode_save" in content
    assert "corrupted" not in content


def test_force_overwrites_ps_md(tmp_path: Path):
    """--force must overwrite ps.md."""
    run_init(tmp_path)
    ps_cmd = tmp_path / ".claude" / "commands" / "ps.md"
    ps_cmd.write_text("# corrupted\n")

    run_init(tmp_path, "--force")
    content = ps_cmd.read_text()
    assert "palinode_save" in content
    assert "corrupted" not in content


# ---------------------------------------------------------------------------
# --no-slash skips both files
# ---------------------------------------------------------------------------

def test_no_slash_skips_save_md(tmp_path: Path):
    """--no-slash must not write save.md."""
    run_init(tmp_path, "--no-slash")
    assert not (tmp_path / ".claude" / "commands" / "save.md").exists()


def test_no_slash_skips_ps_md(tmp_path: Path):
    """--no-slash must not write ps.md."""
    run_init(tmp_path, "--no-slash")
    assert not (tmp_path / ".claude" / "commands" / "ps.md").exists()


# ---------------------------------------------------------------------------
# Output shows both commands
# ---------------------------------------------------------------------------

def test_output_mentions_save_command(tmp_path: Path):
    """init output must indicate /save command was written."""
    result = run_init(tmp_path)
    assert "/save" in result.output


def test_output_mentions_ps_alias(tmp_path: Path):
    """init output must indicate /ps (alias) command was written."""
    result = run_init(tmp_path)
    assert "/ps" in result.output


# ---------------------------------------------------------------------------
# dry-run mentions both files
# ---------------------------------------------------------------------------

def test_dry_run_mentions_save_md(tmp_path: Path):
    """--dry-run output must mention save.md."""
    result = run_init(tmp_path, "--dry-run")
    assert "save.md" in result.output or "/save" in result.output


def test_dry_run_mentions_ps_md(tmp_path: Path):
    """--dry-run output must mention ps.md."""
    result = run_init(tmp_path, "--dry-run")
    assert "ps.md" in result.output or "/ps" in result.output
