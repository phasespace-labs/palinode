"""Tests for `palinode init --skills` (skill-format scaffolding, #474).

`--skills {none|project|personal|both}` installs /save /ps /wrap as Claude Code
SKILL.md files from the SAME `*_COMMAND_BODY` constants the slash commands use
(single source — no drift). 'personal' targets `~/.claude/skills/` so /wrap is
typeable in every project, not just the inited one.

All file-only; no network. Personal scope is sandboxed via a tmp HOME.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli.init import init

# Isolate: only exercise the skills path (no mcp/hook/claudemd/slash noise).
_BASE = ["--no-mcp", "--no-hook", "--no-claudemd", "--no-slash"]


def _run(args: list[str]) -> "object":
    return CliRunner().invoke(init, args)


def test_default_writes_no_skills(tmp_path):
    res = _run(["--dir", str(tmp_path), *_BASE])
    assert res.exit_code == 0, res.output
    assert not (tmp_path / ".claude" / "skills").exists()


def test_project_scope_writes_three_skills(tmp_path):
    res = _run(["--dir", str(tmp_path), *_BASE, "--skills", "project"])
    assert res.exit_code == 0, res.output
    for name in ("save", "ps", "wrap"):
        sk = tmp_path / ".claude" / "skills" / name / "SKILL.md"
        assert sk.exists(), f"{name} skill missing"
        assert sk.read_text().startswith(f"---\nname: {name}\n"), "skill needs name frontmatter"


def test_single_source_body_matches_command(tmp_path):
    """The skill body is the command body + injected name — not a separate copy."""
    from palinode.cli.init import WRAP_COMMAND_BODY

    _run(["--dir", str(tmp_path), *_BASE, "--skills", "project"])
    wrap_skill = (tmp_path / ".claude" / "skills" / "wrap" / "SKILL.md").read_text()
    # Push-twice ordering and the description carry through from the body.
    assert wrap_skill.count("palinode_push") == WRAP_COMMAND_BODY.count("palinode_push")
    assert "name: wrap" in wrap_skill


def test_personal_scope_targets_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home  # sanity: HOME drives ~ resolution
    proj = tmp_path / "proj"
    proj.mkdir()
    res = _run(["--dir", str(proj), *_BASE, "--skills", "personal"])
    assert res.exit_code == 0, res.output
    assert (home / ".claude" / "skills" / "wrap" / "SKILL.md").exists()
    # personal scope must NOT also write into the project
    assert not (tmp_path / "proj" / ".claude" / "skills").exists()


def test_both_scope_writes_project_and_personal(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir()
    res = _run(["--dir", str(proj), *_BASE, "--skills", "both"])
    assert res.exit_code == 0, res.output
    assert (home / ".claude" / "skills" / "wrap" / "SKILL.md").exists()
    assert (tmp_path / "proj" / ".claude" / "skills" / "wrap" / "SKILL.md").exists()


def test_wrap_policy_heavy_uses_heavy_body(tmp_path):
    from palinode.cli.init import WRAP_HEAVY_COMMAND_BODY

    _run(["--dir", str(tmp_path), *_BASE, "--skills", "project", "--wrap-policy", "heavy"])
    wrap_skill = (tmp_path / ".claude" / "skills" / "wrap" / "SKILL.md").read_text()
    # Heavy body's distinguishing content should be present.
    assert "heavy" in wrap_skill.lower()
    assert WRAP_HEAVY_COMMAND_BODY.split("\n", 3)[-1][:40] in wrap_skill


def test_idempotent_skip_without_force(tmp_path):
    args = ["--dir", str(tmp_path), *_BASE, "--skills", "project"]
    _run(args)
    res2 = _run(args)
    assert res2.exit_code == 0
    assert "skipped (exists)" in res2.output


def test_dry_run_writes_nothing(tmp_path):
    res = _run(["--dir", str(tmp_path), *_BASE, "--skills", "personal", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "SKILL.md" in res.output  # plan mentions it
    assert not (tmp_path / ".claude" / "skills").exists()
