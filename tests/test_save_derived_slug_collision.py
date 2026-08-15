"""
Two saves whose opening lines agree derive the same slug.

Before this was fixed, the second write landed on the first one's path: no
error, no warning, and a receipt indistinguishable from a fresh create. The
earlier memory survived only in git history, which the queryable store never
consults.

An *explicit* slug that collides is a different case — that is an update of the
same logical memory, and it must keep overwriting.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core.config import config

client = TestClient(app)

# The shared opening line from the issue's reproduction: the derived slug comes
# from the first 30 characters, so these three differ only after that point.
SHARED_OPENING = "user: thanks that helps a lot"


@pytest.fixture
def mock_memory_dir(tmp_path):
    old = config.memory_dir
    config.memory_dir = str(tmp_path)
    yield str(tmp_path)
    config.memory_dir = old


def _save(content: str, **extra):
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")):
        res = client.post(
            "/save",
            json={"content": content, "type": "Insight", **extra},
        )
    assert res.status_code == 200, res.text
    return res.json()


def test_colliding_derived_slugs_do_not_overwrite(mock_memory_dir):
    """Three saves sharing an opening line keep three distinct memories."""
    markers = ["ALPHA", "BRAVO", "CHARLIE"]
    results = [_save(f"{SHARED_OPENING}\n\n{marker} body text") for marker in markers]

    rel_paths = [r["rel_path"] for r in results]
    assert len(set(rel_paths)) == 3, f"expected 3 distinct paths, got {rel_paths}"

    for marker, result in zip(markers, results, strict=True):
        with open(result["file_path"], "r") as fh:
            assert marker in fh.read(), f"{marker} was overwritten and is gone"


def test_identical_content_is_not_duplicated(mock_memory_dir):
    """The same memory arriving twice stays one file, not two."""
    content = f"{SHARED_OPENING}\n\nidentical body"
    first = _save(content)
    second = _save(content)

    assert first["rel_path"] == second["rel_path"]
    directory = os.path.dirname(first["file_path"])
    assert len(os.listdir(directory)) == 1


def test_explicit_slug_still_overwrites(mock_memory_dir):
    """An explicit slug that collides is an update, and keeps that behaviour."""
    first = _save("first body", slug="pinned-note")
    second = _save("second body", slug="pinned-note")

    assert first["rel_path"] == second["rel_path"]
    with open(second["file_path"], "r") as fh:
        body = fh.read()
    assert "second body" in body
    assert "first body" not in body
