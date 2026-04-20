"""Tests for #114 (content_hash in frontmatter) and #113 (confidence field)."""
import hashlib
import pytest
import yaml
from fastapi.testclient import TestClient
from palinode.api.server import app
from palinode.core.config import config
from unittest.mock import patch

client = TestClient(app)


@pytest.fixture
def mock_memory_dir(tmp_path):
    old_memory_dir = config.memory_dir
    config.memory_dir = str(tmp_path)
    yield str(tmp_path)
    config.memory_dir = old_memory_dir


def _read_frontmatter(file_path: str) -> dict:
    """Read a memory file and return its parsed frontmatter dict."""
    with open(file_path, "r") as f:
        raw = f.read()
    # Extract YAML between --- delimiters
    parts = raw.split("---", 2)
    assert len(parts) >= 3, f"Expected frontmatter delimiters in:\n{raw}"
    return yaml.safe_load(parts[1])


# --- #114: content_hash in frontmatter ---

class TestContentHash:
    def test_save_includes_full_sha256_content_hash(self, mock_memory_dir):
        """content_hash should be full SHA-256 hex digest of body content."""
        body = "This is the memory body content"
        expected_hash = hashlib.sha256(body.encode()).hexdigest()

        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post("/save", json={
                "content": body,
                "type": "Insight",
            })
        assert res.status_code == 200

        fm = _read_frontmatter(res.json()["file_path"])
        assert fm["content_hash"] == expected_hash
        assert len(fm["content_hash"]) == 64  # full SHA-256 hex = 64 chars

    def test_content_hash_changes_with_content(self, mock_memory_dir):
        """Different content should produce different hashes."""
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res1 = client.post("/save", json={
                "content": "first content",
                "type": "Insight",
                "slug": "hash-test-1",
            })
            res2 = client.post("/save", json={
                "content": "second content",
                "type": "Insight",
                "slug": "hash-test-2",
            })

        fm1 = _read_frontmatter(res1.json()["file_path"])
        fm2 = _read_frontmatter(res2.json()["file_path"])
        assert fm1["content_hash"] != fm2["content_hash"]

    def test_content_hash_is_deterministic(self, mock_memory_dir):
        """Same content should always produce same hash."""
        body = "deterministic test body"
        expected = hashlib.sha256(body.encode()).hexdigest()

        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post("/save", json={
                "content": body,
                "type": "Insight",
                "slug": "determ-test",
            })

        fm = _read_frontmatter(res.json()["file_path"])
        assert fm["content_hash"] == expected


# --- #113: confidence field ---

class TestConfidence:
    def test_save_with_confidence(self, mock_memory_dir):
        """When confidence is provided, it appears in frontmatter."""
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post("/save", json={
                "content": "High confidence fact",
                "type": "Insight",
                "confidence": 0.95,
            })
        assert res.status_code == 200

        fm = _read_frontmatter(res.json()["file_path"])
        assert fm["confidence"] == 0.95

    def test_save_without_confidence_omits_field(self, mock_memory_dir):
        """When confidence is not provided, it should NOT appear in frontmatter."""
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post("/save", json={
                "content": "No confidence specified",
                "type": "Insight",
            })
        assert res.status_code == 200

        fm = _read_frontmatter(res.json()["file_path"])
        assert "confidence" not in fm

    def test_save_confidence_zero(self, mock_memory_dir):
        """Confidence of 0.0 should still be written (not treated as falsy)."""
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post("/save", json={
                "content": "Zero confidence fact",
                "type": "Insight",
                "confidence": 0.0,
            })
        assert res.status_code == 200

        fm = _read_frontmatter(res.json()["file_path"])
        assert fm["confidence"] == 0.0

    def test_save_confidence_one(self, mock_memory_dir):
        """Confidence of 1.0 should be written."""
        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post("/save", json={
                "content": "Full confidence fact",
                "type": "Decision",
                "confidence": 1.0,
            })
        assert res.status_code == 200

        fm = _read_frontmatter(res.json()["file_path"])
        assert fm["confidence"] == 1.0

    def test_confidence_roundtrips_through_parser(self, mock_memory_dir):
        """Confidence in frontmatter should be readable via the parser."""
        from palinode.core.parser import parse_markdown

        with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
            res = client.post("/save", json={
                "content": "Roundtrip test",
                "type": "Insight",
                "confidence": 0.75,
            })

        with open(res.json()["file_path"], "r") as f:
            raw = f.read()

        metadata, _ = parse_markdown(raw)
        assert metadata["confidence"] == 0.75
