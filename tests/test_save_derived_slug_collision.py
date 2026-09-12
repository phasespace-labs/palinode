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
    original_slug = os.path.splitext(os.path.basename(rel_paths[0]))[0]
    assert [r["save_outcome"] for r in results] == [
        "created",
        "disambiguated",
        "disambiguated",
    ]
    assert [r["disambiguated_from"] for r in results] == [
        None,
        original_slug,
        original_slug,
    ]

    for marker, result in zip(markers, results, strict=True):
        with open(result["file_path"], "r", encoding="utf-8") as fh:
            assert marker in fh.read(), f"{marker} was overwritten and is gone"


def test_identical_content_is_not_duplicated(mock_memory_dir):
    """The same memory arriving twice stays one file, not two."""
    content = f"{SHARED_OPENING}\n\nidentical body"
    first = _save(content)
    second = _save(content)

    assert first["rel_path"] == second["rel_path"]
    assert first["save_outcome"] == "created"
    assert second["save_outcome"] == "resaved"
    assert first["disambiguated_from"] is None
    assert second["disambiguated_from"] is None
    directory = os.path.dirname(first["file_path"])
    assert len(os.listdir(directory)) == 1


def test_resaving_a_suffixed_memory_reuses_its_own_file(mock_memory_dir):
    """A memory pushed to a suffix stays there on re-save instead of climbing.

    The base path belongs to someone else, so the hash check against it fails
    every time. Without checking the suffixed candidates too, each re-save
    would skip the occupied ``-2`` and claim ``-3``, ``-4``, and so on.
    """
    first = _save(f"{SHARED_OPENING}\n\nALPHA body text")
    second = _save(f"{SHARED_OPENING}\n\nBRAVO body text")
    again = _save(f"{SHARED_OPENING}\n\nBRAVO body text")

    assert second["rel_path"] != first["rel_path"]
    assert again["rel_path"] == second["rel_path"]
    assert first["save_outcome"] == "created"
    assert second["save_outcome"] == "disambiguated"
    assert second["disambiguated_from"] == os.path.splitext(
        os.path.basename(first["rel_path"])
    )[0]
    assert again["save_outcome"] == "resaved"
    assert again["disambiguated_from"] is None

    directory = os.path.dirname(first["file_path"])
    assert sorted(os.listdir(directory)) == sorted(
        [
            os.path.basename(first["file_path"]),
            os.path.basename(second["file_path"]),
        ]
    )


def test_explicit_slug_still_overwrites(mock_memory_dir):
    """An explicit slug that collides is an update, and keeps that behaviour."""
    first = _save("first body", slug="pinned-note")
    second = _save("second body", slug="pinned-note")

    assert first["rel_path"] == second["rel_path"]
    assert first["save_outcome"] == "created"
    assert second["save_outcome"] == "replaced"
    assert first["disambiguated_from"] is None
    assert second["disambiguated_from"] is None
    with open(second["file_path"], "r", encoding="utf-8") as fh:
        body = fh.read()
    assert "second body" in body
    assert "first body" not in body
