"""Tests for derived-slug collision prevention (issue #129).

When ``slug`` is omitted, the save path derives one from the opening words
of the content. Two saves whose openings agree used to resolve to the same
file, silently overwriting the first. This test suite verifies that:

1. Two saves sharing an opening line but differing afterwards produce two
   distinct files, both retrievable.
2. Two saves identical for the first 100 characters but differing after
   character 100 still produce two distinct files (the prefix-hash trap).
3. An explicitly-passed slug still overwrites (documented escape hatch).
4. A re-save of identical content is idempotent (no spurious disambiguation).
"""
from __future__ import annotations

import os

import pytest
import yaml

from palinode.core.config import config
from palinode.core.save import save_memory


@pytest.fixture
def mock_palinode_dir(tmp_path, monkeypatch):
    """Point palinode_dir (= memory_dir) at a temp directory for the test."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.git, "auto_push", False)
    yield str(tmp_path)


def _read_body(file_path: str) -> str:
    """Read the markdown body (after frontmatter) from a memory file."""
    with open(file_path, "r") as f:
        text = f.read()
    parts = text.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return text.strip()


class TestDerivedSlugCollision:
    """Verify derived slugs disambiguate on content difference."""

    def test_two_saves_shared_opening_produce_distinct_files(self, mock_palinode_dir):
        """Two memories sharing an opening line but differing afterwards
        must produce two distinct files, both retrievable."""
        content_a = "User thanks that helps a lot\n\nALPHA — first memory with unique details"
        content_b = "User thanks that helps a lot\n\nBRAVO — second memory with different details"

        result_a = save_memory(content=content_a, type="Insight")
        result_b = save_memory(content=content_b, type="Insight")

        # They must produce different file paths
        assert result_a["file_path"] != result_b["file_path"]
        # Both files must exist
        assert os.path.exists(result_a["file_path"])
        assert os.path.exists(result_b["file_path"])
        # Content must be preserved
        assert "ALPHA" in _read_body(result_a["file_path"])
        assert "BRAVO" in _read_body(result_b["file_path"])

    def test_content_identical_first_100_chars_still_disambiguates(self, mock_palinode_dir):
        """Two memories whose content is identical for the first 100+
        characters but differs only after must still produce distinct files.
        This is the case a prefix-hash silently fails."""
        shared_prefix = "User thanks that helps a lot — this is an extended shared opening " + "x" * 60
        content_a = shared_prefix + "\n\nCHARLIE — unique tail A"
        content_b = shared_prefix + "\n\nDELTA — unique tail B"

        # Verify the first 100 chars are identical
        assert content_a[:100] == content_b[:100]

        result_a = save_memory(content=content_a, type="Insight")
        result_b = save_memory(content=content_b, type="Insight")

        assert result_a["file_path"] != result_b["file_path"]
        assert os.path.exists(result_a["file_path"])
        assert os.path.exists(result_b["file_path"])
        assert "CHARLIE" in _read_body(result_a["file_path"])
        assert "DELTA" in _read_body(result_b["file_path"])

    def test_explicit_slug_still_overwrites(self, mock_palinode_dir):
        """An explicitly-passed slug must keep overwriting (documented
        escape hatch for idempotent updates)."""
        content_a = "First version of this note"
        content_b = "Second version, completely different"

        result_a = save_memory(content=content_a, type="Insight", slug="my-note")
        result_b = save_memory(content=content_b, type="Insight", slug="my-note")

        # Same file path — overwrite is intentional
        assert result_a["file_path"] == result_b["file_path"]
        # Second content wins
        assert "Second version" in _read_body(result_b["file_path"])

    def test_identical_content_resave_is_idempotent(self, mock_palinode_dir):
        """Re-saving the exact same content (with a derived slug) must
        hit the same file — a re-save, not a collision."""
        content = "User thanks that helps a lot\n\nSame content saved twice"

        result_a = save_memory(content=content, type="Insight")
        result_b = save_memory(content=content, type="Insight")

        # Same file path — this is a re-save, not a collision
        assert result_a["file_path"] == result_b["file_path"]

    def test_three_collisions_produce_three_files(self, mock_palinode_dir):
        """Three saves with same opening line but different bodies produce
        three distinct files (the full reproduction from issue #129)."""
        contents = [
            "User thanks that helps a lot\n\nALPHA body",
            "User thanks that helps a lot\n\nBRAVO body",
            "User thanks that helps a lot\n\nCHARLIE body",
        ]
        results = [save_memory(content=c, type="Insight") for c in contents]

        paths = [r["file_path"] for r in results]
        assert len(set(paths)) == 3, f"Expected 3 distinct paths, got: {paths}"

        # All three files exist with correct content
        for path, marker in zip(paths, ["ALPHA", "BRAVO", "CHARLIE"]):
            assert os.path.exists(path)
            assert marker in _read_body(path)
