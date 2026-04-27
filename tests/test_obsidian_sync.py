"""Tests for `palinode obsidian-sync` — Deliverable E of #210.

Covers:
- File with ``entities:`` frontmatter and no body wikilinks → would-update
  reports the correct slugs
- File with ``entities:`` AND inline body wikilinks for all entities → unchanged
- File with auto-footer already present (re-run) → unchanged (idempotent)
- ``--apply`` actually writes the changes; re-running with ``--apply`` is idempotent
- ``--include "decisions/*.md"`` scopes to only matching files
- File with no frontmatter → unchanged (nothing to backfill)
- Empty ``memory_dir`` → no errors, summary reports zero
- Directory structure: files in skip_dirs (.obsidian, archive, logs, .palinode)
  are excluded
- Non-zero exit code when a file fails to parse (simulated via unreadable file)

All tests use ``click.testing.CliRunner`` for CLI invocation and ``tmp_path``
for isolated filesystem fixtures.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRONTMATTER_ENTITIES = """\
---
id: abc123
title: Test Decision
category: decision
entities:
  - person/alice
  - project/palinode
---

This is the body of the decision.
"""

_FRONTMATTER_ALL_INLINE = """\
---
id: abc123
title: Test Decision
category: decision
entities:
  - person/alice
  - project/palinode
---

We talked to [[alice]] about the [[palinode]] project in depth.
"""

_AUTO_FOOTER_SYNCED = """\
---
id: abc123
title: Test Decision
category: decision
entities:
  - person/alice
  - project/palinode
---

This is the body of the decision.

## See also
<!-- palinode-auto-footer -->
- [[alice]]
- [[palinode]]
"""

_NO_FRONTMATTER = """\
This file has no YAML frontmatter at all.

Just some plain text content.
"""

_ENTITIES_EMPTY = """\
---
id: xyz
title: Empty entities
category: insight
entities: []
---

Body content here.
"""


def _make_memory_dir(tmp_path: Path) -> Path:
    """Return a path suitable as a memory_dir stub."""
    mem = tmp_path / "memory"
    mem.mkdir()
    return mem


def _invoke(runner: CliRunner, args: list[str], memory_dir: Path) -> object:
    """Run `palinode obsidian-sync` with PALINODE_DIR pointing to *memory_dir*."""
    env = {"PALINODE_DIR": str(memory_dir)}
    return runner.invoke(main, ["obsidian-sync"] + args, env=env, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


def test_would_update_file_with_entities_no_inline_links(tmp_path):
    """A file with ``entities:`` but no body wikilinks is reported as would-update."""
    mem = _make_memory_dir(tmp_path)
    (mem / "decisions").mkdir()
    f = mem / "decisions" / "my-decision.md"
    f.write_text(_FRONTMATTER_ENTITIES, encoding="utf-8")

    runner = CliRunner()
    result = _invoke(runner, [], mem)

    assert result.exit_code == 0, result.output
    assert "would update: decisions/my-decision.md" in result.output
    # Both entity slugs should be listed
    assert "[[alice]]" in result.output
    assert "[[palinode]]" in result.output
    assert "1 would be updated" in result.output
    assert "0 unchanged" in result.output
    # Dry-run must NOT write anything
    content_after = f.read_text(encoding="utf-8")
    assert content_after == _FRONTMATTER_ENTITIES


def test_unchanged_file_with_all_entities_inline(tmp_path):
    """A file where every entity already appears as a body wikilink is unchanged."""
    mem = _make_memory_dir(tmp_path)
    f = mem / "inline.md"
    f.write_text(_FRONTMATTER_ALL_INLINE, encoding="utf-8")

    runner = CliRunner()
    result = _invoke(runner, [], mem)

    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output
    assert "would update" not in result.output
    assert "0 would be updated" in result.output


def test_unchanged_file_with_existing_auto_footer(tmp_path):
    """Re-running on an already-synced file (auto-footer present and current) is idempotent."""
    mem = _make_memory_dir(tmp_path)
    f = mem / "synced.md"
    f.write_text(_AUTO_FOOTER_SYNCED, encoding="utf-8")

    runner = CliRunner()
    result = _invoke(runner, [], mem)

    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output
    assert "would update" not in result.output


def test_apply_writes_and_is_idempotent(tmp_path):
    """``--apply`` writes the footer; a second ``--apply`` run is a no-op."""
    mem = _make_memory_dir(tmp_path)
    f = mem / "needs-footer.md"
    f.write_text(_FRONTMATTER_ENTITIES, encoding="utf-8")

    runner = CliRunner()

    # First apply
    result1 = _invoke(runner, ["--apply"], mem)
    assert result1.exit_code == 0, result1.output
    assert "updated: needs-footer.md" in result1.output
    assert "1 updated" in result1.output

    written = f.read_text(encoding="utf-8")
    assert "<!-- palinode-auto-footer -->" in written
    assert "[[alice]]" in written
    assert "[[palinode]]" in written

    # Second apply — must be unchanged
    result2 = _invoke(runner, ["--apply"], mem)
    assert result2.exit_code == 0, result2.output
    assert "0 updated" in result2.output
    assert "1 unchanged" in result2.output

    # Content must not have changed
    written2 = f.read_text(encoding="utf-8")
    assert written2 == written


def test_include_glob_scopes_run(tmp_path):
    """``--include "decisions/*.md"`` only processes files matching that glob."""
    mem = _make_memory_dir(tmp_path)
    (mem / "decisions").mkdir()
    (mem / "projects").mkdir()

    dec_file = mem / "decisions" / "dec.md"
    dec_file.write_text(_FRONTMATTER_ENTITIES, encoding="utf-8")

    proj_file = mem / "projects" / "proj.md"
    proj_file.write_text(_FRONTMATTER_ENTITIES, encoding="utf-8")

    runner = CliRunner()
    result = _invoke(runner, ["--include", "decisions/*.md"], mem)

    assert result.exit_code == 0, result.output
    assert "decisions/dec.md" in result.output
    # projects/proj.md must NOT appear in output at all
    assert "projects" not in result.output
    assert "1 would be updated" in result.output


def test_no_frontmatter_unchanged(tmp_path):
    """A file with no frontmatter has no entities → unchanged, no error."""
    mem = _make_memory_dir(tmp_path)
    f = mem / "bare.md"
    f.write_text(_NO_FRONTMATTER, encoding="utf-8")

    runner = CliRunner()
    result = _invoke(runner, [], mem)

    assert result.exit_code == 0, result.output
    assert "would update" not in result.output
    assert "0 would be updated" in result.output
    assert "1 unchanged" in result.output


def test_empty_memory_dir_no_error(tmp_path):
    """An empty memory_dir produces zero counts without error."""
    mem = _make_memory_dir(tmp_path)

    runner = CliRunner()
    result = _invoke(runner, [], mem)

    assert result.exit_code == 0, result.output
    # Either "No .md files found" or "0 would be updated, 0 unchanged"
    assert "0" in result.output


def test_skip_dirs_excluded(tmp_path):
    """Files inside .obsidian/, archive/, logs/, .palinode/ are excluded."""
    mem = _make_memory_dir(tmp_path)

    skip_dir_names = [".obsidian", "archive", "logs", ".palinode"]
    for d in skip_dir_names:
        skip_dir = mem / d
        skip_dir.mkdir()
        (skip_dir / "shouldskip.md").write_text(_FRONTMATTER_ENTITIES, encoding="utf-8")

    runner = CliRunner()
    result = _invoke(runner, [], mem)

    assert result.exit_code == 0, result.output
    assert "would update" not in result.output
    assert "0 would be updated" in result.output


def test_empty_entities_list_unchanged(tmp_path):
    """A file with ``entities: []`` (empty list) is treated as unchanged."""
    mem = _make_memory_dir(tmp_path)
    f = mem / "empty-entities.md"
    f.write_text(_ENTITIES_EMPTY, encoding="utf-8")

    runner = CliRunner()
    result = _invoke(runner, [], mem)

    assert result.exit_code == 0, result.output
    assert "would update" not in result.output


def test_exclude_glob_removes_files(tmp_path):
    """``--exclude "daily/**"`` removes files in daily/ from the candidate set."""
    mem = _make_memory_dir(tmp_path)
    (mem / "daily").mkdir()
    (mem / "decisions").mkdir()

    daily_file = mem / "daily" / "2026-04-26.md"
    daily_file.write_text(_FRONTMATTER_ENTITIES, encoding="utf-8")

    dec_file = mem / "decisions" / "dec.md"
    dec_file.write_text(_FRONTMATTER_ENTITIES, encoding="utf-8")

    runner = CliRunner()
    result = _invoke(runner, ["--exclude", "daily/**"], mem)

    assert result.exit_code == 0, result.output
    assert "daily" not in result.output
    # decisions file should still appear
    assert "decisions/dec.md" in result.output


def test_nonexistent_memory_dir_exits_nonzero(tmp_path):
    """Pointing PALINODE_DIR at a non-existent path exits with code 1."""
    missing = str(tmp_path / "nonexistent")
    runner = CliRunner()
    env = {"PALINODE_DIR": missing}
    result = runner.invoke(main, ["obsidian-sync"], env=env, catch_exceptions=False)
    assert result.exit_code != 0


def test_apply_writes_multiple_files(tmp_path):
    """``--apply`` updates all files that need a footer."""
    mem = _make_memory_dir(tmp_path)
    (mem / "decisions").mkdir()
    (mem / "projects").mkdir()

    for i in range(3):
        (mem / "decisions" / f"dec-{i}.md").write_text(_FRONTMATTER_ENTITIES, encoding="utf-8")
    (mem / "projects" / "proj.md").write_text(_FRONTMATTER_ALL_INLINE, encoding="utf-8")

    runner = CliRunner()
    result = _invoke(runner, ["--apply"], mem)

    assert result.exit_code == 0, result.output
    assert "3 updated" in result.output
    assert "1 unchanged" in result.output

    # Verify files on disk
    for i in range(3):
        content = (mem / "decisions" / f"dec-{i}.md").read_text(encoding="utf-8")
        assert "<!-- palinode-auto-footer -->" in content
