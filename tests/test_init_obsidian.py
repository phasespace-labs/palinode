"""Tests for `palinode init --obsidian` — Obsidian vault scaffold (Deliverable A, #210).

Coverage:
- All expected files are created by ``--obsidian``
- JSON files are valid and contain expected keys
- Markdown files exist and link category dirs
- ``palinode init`` without ``--obsidian`` must NOT create ``.obsidian/`` (no regression)
- Idempotency: second run skips existing files (preserves user edits)
- ``--force-obsidian`` overwrites scaffold files but preserves ``workspace.json``
- ``--force`` (global) overwrites everything including ``workspace.json``
- ``--force-obsidian`` implies ``--obsidian`` even without the flag
- ``--dry-run`` with ``--obsidian`` prints expected paths, writes nothing
"""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from palinode.cli import main
from palinode.cli.init import (
    OBSIDIAN_APP_JSON,
    OBSIDIAN_GRAPH_JSON,
    OBSIDIAN_WORKSPACE_JSON,
    OBSIDIAN_INDEX_MD,
    OBSIDIAN_README_MD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_init(tmp_path: Path, *extra_args: str) -> object:
    """Invoke ``palinode init --dir <tmp_path> <extra_args>`` and return the result."""
    runner = CliRunner()
    return runner.invoke(main, ["init", "--dir", str(tmp_path), *extra_args])


def obsidian_files(target: Path) -> list[Path]:
    """Return all paths that --obsidian is expected to create."""
    return [
        target / ".obsidian" / "app.json",
        target / ".obsidian" / "graph.json",
        target / ".obsidian" / "workspace.json",
        target / "_index.md",
        target / "_README.md",
    ]


# ---------------------------------------------------------------------------
# Canonical template guards
# ---------------------------------------------------------------------------

def test_app_json_has_required_keys():
    """app.json template must specify file-recovery and wikilinks settings."""
    assert OBSIDIAN_APP_JSON.get("alwaysUpdateLinks") is True
    assert OBSIDIAN_APP_JSON.get("newFileFolderPath") == "daily"
    assert OBSIDIAN_APP_JSON.get("useMarkdownLinks") is False
    assert OBSIDIAN_APP_JSON.get("newFileLocation") == "folder"


def test_graph_json_has_color_groups():
    """graph.json template must define colour groups for the four core categories."""
    groups = OBSIDIAN_GRAPH_JSON.get("colorGroups", [])
    queries = {g["query"] for g in groups}
    assert "path:people/" in queries
    assert "path:projects/" in queries
    assert "path:decisions/" in queries
    assert "path:insights/" in queries


def test_graph_json_collapses_archive_and_logs():
    """graph.json template must collapse archive/, logs/, and .palinode/."""
    collapsed = OBSIDIAN_GRAPH_JSON.get("collapsedNodeGroups", [])
    assert "path:archive/" in collapsed
    assert "path:logs/" in collapsed
    assert "path:.palinode/" in collapsed


def test_workspace_json_is_valid_structure():
    """workspace.json template must have 'main', 'left', 'right', 'active' keys."""
    for key in ("main", "left", "right", "active"):
        assert key in OBSIDIAN_WORKSPACE_JSON


def test_index_md_has_category_wikilinks():
    """_index.md must contain wikilinks to all seven category dirs."""
    categories = ["people", "projects", "decisions", "insights", "research", "daily", "archive"]
    for cat in categories:
        assert cat in OBSIDIAN_INDEX_MD, f"_index.md missing wikilink to {cat}"


def test_readme_md_mentions_palinode_and_help():
    """_README.md must orient a cold reader: mention palinode, --help, directory table."""
    assert "palinode" in OBSIDIAN_README_MD.lower()
    assert "--help" in OBSIDIAN_README_MD
    assert "daily/" in OBSIDIAN_README_MD


# ---------------------------------------------------------------------------
# Scaffold creation
# ---------------------------------------------------------------------------

def test_obsidian_flag_creates_all_files(tmp_path: Path):
    """``--obsidian`` must create all five scaffold files."""
    result = run_init(tmp_path, "--obsidian")
    assert result.exit_code == 0, result.output

    for path in obsidian_files(tmp_path):
        assert path.exists(), f"Expected {path.relative_to(tmp_path)} to exist"


def test_obsidian_app_json_is_valid_json(tmp_path: Path):
    run_init(tmp_path, "--obsidian")
    content = (tmp_path / ".obsidian" / "app.json").read_text()
    parsed = json.loads(content)
    assert parsed.get("newFileFolderPath") == "daily"
    assert parsed.get("useMarkdownLinks") is False


def test_obsidian_graph_json_is_valid_json(tmp_path: Path):
    run_init(tmp_path, "--obsidian")
    content = (tmp_path / ".obsidian" / "graph.json").read_text()
    parsed = json.loads(content)
    assert "colorGroups" in parsed
    assert "collapsedNodeGroups" in parsed


def test_obsidian_workspace_json_is_valid_json(tmp_path: Path):
    run_init(tmp_path, "--obsidian")
    content = (tmp_path / ".obsidian" / "workspace.json").read_text()
    parsed = json.loads(content)
    assert "main" in parsed
    assert "active" in parsed


def test_obsidian_index_md_contains_wikilinks(tmp_path: Path):
    run_init(tmp_path, "--obsidian")
    content = (tmp_path / "_index.md").read_text()
    assert "[[" in content
    assert "people" in content
    assert "decisions" in content


def test_obsidian_readme_md_is_markdown(tmp_path: Path):
    run_init(tmp_path, "--obsidian")
    content = (tmp_path / "_README.md").read_text()
    assert "# " in content  # at least one heading
    assert "palinode" in content.lower()


# ---------------------------------------------------------------------------
# No-regression: init without --obsidian must NOT create .obsidian/
# ---------------------------------------------------------------------------

def test_no_obsidian_flag_does_not_create_obsidian_dir(tmp_path: Path):
    """Plain ``palinode init`` (no --obsidian) must leave .obsidian/ untouched."""
    result = run_init(tmp_path)
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".obsidian").exists()


def test_no_obsidian_flag_does_not_create_vault_markdown(tmp_path: Path):
    """Plain ``palinode init`` must not write _index.md or _README.md."""
    run_init(tmp_path)
    assert not (tmp_path / "_index.md").exists()
    assert not (tmp_path / "_README.md").exists()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_obsidian_idempotent_skip(tmp_path: Path):
    """Second run with --obsidian must skip all existing scaffold files."""
    run_init(tmp_path, "--obsidian")

    # User edits a file
    index = tmp_path / "_index.md"
    original = index.read_text()
    index.write_text(original + "\n## User-added section\n")
    user_content = index.read_text()

    # Second run
    result = run_init(tmp_path, "--obsidian")
    assert result.exit_code == 0, result.output

    # User edit preserved
    assert index.read_text() == user_content
    # Output indicates files were skipped
    assert "skipped" in result.output


def test_obsidian_idempotent_json_unchanged(tmp_path: Path):
    """Second run must not change JSON files that already exist."""
    run_init(tmp_path, "--obsidian")
    app_json_before = (tmp_path / ".obsidian" / "app.json").read_text()

    run_init(tmp_path, "--obsidian")
    assert (tmp_path / ".obsidian" / "app.json").read_text() == app_json_before


# ---------------------------------------------------------------------------
# --force-obsidian
# ---------------------------------------------------------------------------

def test_force_obsidian_overwrites_scaffold_files(tmp_path: Path):
    """--force-obsidian must overwrite app.json, graph.json, _index.md, _README.md."""
    run_init(tmp_path, "--obsidian")

    # Corrupt the files to confirm they are overwritten
    (tmp_path / ".obsidian" / "app.json").write_text('{"corrupted": true}')
    (tmp_path / "_index.md").write_text("# Corrupted\n")

    result = run_init(tmp_path, "--force-obsidian")
    assert result.exit_code == 0, result.output

    app = json.loads((tmp_path / ".obsidian" / "app.json").read_text())
    assert "corrupted" not in app
    assert app.get("newFileFolderPath") == "daily"

    index_content = (tmp_path / "_index.md").read_text()
    assert "Corrupted" not in index_content
    assert "[[" in index_content


def test_force_obsidian_preserves_workspace_json(tmp_path: Path):
    """--force-obsidian must NOT overwrite workspace.json (Obsidian owns it post-launch)."""
    run_init(tmp_path, "--obsidian")
    workspace = tmp_path / ".obsidian" / "workspace.json"
    workspace.write_text('{"user_edited": true}')

    run_init(tmp_path, "--force-obsidian")

    ws = json.loads(workspace.read_text())
    assert ws.get("user_edited") is True


def test_global_force_overwrites_workspace_json(tmp_path: Path):
    """Global --force (not --force-obsidian) must overwrite workspace.json too."""
    run_init(tmp_path, "--obsidian")
    workspace = tmp_path / ".obsidian" / "workspace.json"
    workspace.write_text('{"user_edited": true}')

    run_init(tmp_path, "--obsidian", "--force")

    ws = json.loads(workspace.read_text())
    assert "user_edited" not in ws
    assert "main" in ws


def test_force_obsidian_implies_obsidian(tmp_path: Path):
    """--force-obsidian without --obsidian must still scaffold the vault."""
    # First create a partial install then use force-obsidian without the --obsidian flag
    run_init(tmp_path, "--obsidian")
    (tmp_path / ".obsidian" / "app.json").write_text('{"corrupted": true}')

    # No explicit --obsidian, but --force-obsidian implies it
    result = run_init(tmp_path, "--force-obsidian")
    assert result.exit_code == 0, result.output

    app = json.loads((tmp_path / ".obsidian" / "app.json").read_text())
    assert app.get("newFileFolderPath") == "daily"


# ---------------------------------------------------------------------------
# --dry-run with --obsidian
# ---------------------------------------------------------------------------

def test_obsidian_dry_run_writes_nothing(tmp_path: Path):
    """--dry-run --obsidian must print expected paths but write nothing."""
    result = run_init(tmp_path, "--obsidian", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert ".obsidian" in result.output

    assert not (tmp_path / ".obsidian").exists()
    assert not (tmp_path / "_index.md").exists()
    assert not (tmp_path / "_README.md").exists()


# ---------------------------------------------------------------------------
# Output messages
# ---------------------------------------------------------------------------

def test_obsidian_output_shows_created(tmp_path: Path):
    """First run output must show '✓' for each scaffold file."""
    result = run_init(tmp_path, "--obsidian")
    assert result.exit_code == 0, result.output
    assert "✓" in result.output
    assert ".obsidian/app.json" in result.output
    assert ".obsidian/graph.json" in result.output
    assert "_index.md" in result.output
    assert "_README.md" in result.output


def test_obsidian_next_steps_mentions_obsidian(tmp_path: Path):
    """When --obsidian is set, Next steps should mention opening in Obsidian."""
    result = run_init(tmp_path, "--obsidian")
    assert "Obsidian" in result.output or "obsidian" in result.output.lower()
