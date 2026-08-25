"""
Tests for the ``project`` shorthand on save (ADR-010).

``project="palinode"`` on the API/CLI/MCP save surface is sugar for
appending ``"project/palinode"`` to ``entities``.  A pre-prefixed value
is left as-is.  Both ``project`` and ``entities`` may be supplied
together — the project ref is appended if not already present.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core.config import config

client = TestClient(app)


@pytest.fixture
def mock_memory_dir(tmp_path):
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    yield str(tmp_path)
    config.memory_dir = old


def _frontmatter(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    # Frontmatter is between the first two `---` lines.
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"no frontmatter in {file_path}: {text[:120]}"
    return yaml.safe_load(parts[1])


def test_project_shorthand_adds_entity(mock_memory_dir):
    """Bare ``project`` slug becomes a ``project/<slug>`` entity."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post(
            "/save",
            json={"content": "x", "type": "Decision", "project": "palinode"},
        )
        assert res.status_code == 200, res.text
        fm = _frontmatter(res.json()["file_path"])
        assert "project/palinode" in fm["entities"]


def test_project_already_prefixed_left_alone(mock_memory_dir):
    """Caller passing ``project='project/palinode'`` is honored as-is."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post(
            "/save",
            json={"content": "x", "type": "Decision", "project": "project/palinode"},
        )
        assert res.status_code == 200, res.text
        fm = _frontmatter(res.json()["file_path"])
        assert "project/palinode" in fm["entities"]
        # No double-prefixed entry like ``project/project/palinode``.
        assert all(not e.startswith("project/project/") for e in fm["entities"])


def test_project_merges_with_explicit_entities(mock_memory_dir):
    """``project`` and ``entities`` together compose without duplication."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post(
            "/save",
            json={
                "content": "x",
                "type": "Decision",
                "project": "palinode",
                "entities": ["person/alice"],
            },
        )
        assert res.status_code == 200, res.text
        fm = _frontmatter(res.json()["file_path"])
        assert "project/palinode" in fm["entities"]
        assert "person/alice" in fm["entities"]
        # Project ref appears exactly once.
        assert fm["entities"].count("project/palinode") == 1


def test_project_dedup_when_entity_already_includes_it(mock_memory_dir):
    """If ``entities`` already has the project ref, ``project`` is a no-op."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post(
            "/save",
            json={
                "content": "x",
                "type": "Decision",
                "project": "palinode",
                "entities": ["project/palinode"],
            },
        )
        assert res.status_code == 200, res.text
        fm = _frontmatter(res.json()["file_path"])
        assert fm["entities"].count("project/palinode") == 1


def test_project_optional(mock_memory_dir):
    """Saves without ``project`` work exactly as before."""
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post(
            "/save",
            json={"content": "x", "type": "Decision", "entities": ["person/alice"]},
        )
        assert res.status_code == 200, res.text
        fm = _frontmatter(res.json()["file_path"])
        assert fm["entities"] == ["person/alice"]


def test_cli_save_passes_project_flag(mock_memory_dir, monkeypatch):
    """The CLI ``--project / -p`` flag forwards the value to the API.

    The API client is stubbed deliberately. Left unstubbed, this test reaches a
    real HTTP endpoint that is not running under pytest, and every run took the
    save-failed branch — which used to exit 0 because its ``click.Abort()`` was
    constructed and never raised, so ``exit_code == 0`` passed on a save that
    never happened and the ``project`` forwarding this test is named for went
    unchecked. Assert on the forwarded value, not on the exit code alone.
    """
    from click.testing import CliRunner

    from palinode.cli.save import api_client, save

    captured = {}

    def fake_save(*args, **kwargs):
        captured.update(kwargs)
        return {"file_path": "/tmp/memory.md", "id": "decisions-memory"}

    monkeypatch.setattr(api_client, "save", fake_save)
    runner = CliRunner()
    result = runner.invoke(
        save,
        [
            "--type",
            "Decision",
            "-p",
            "palinode",
            "test memory body",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["project"] == "palinode"
