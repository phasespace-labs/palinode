"""Tests for the Layer-2 wiki-contract auto-footer in palinode_save (#210).

Covers:
- entities provided with no body wikilinks → footer appended
- entities provided but all already linked inline → no footer
- entities partially linked → footer for the unlinked subset only
- no entities → no footer
- empty / None content → no footer
- idempotency: saving twice produces identical output (auto-footer is replaced)
- user-authored ## See also (no marker) is left alone; auto-footer appended after
- stale auto-footer removed when all entities become inline-linked
- marker comment is present and correct

Unit tests target ``_apply_wiki_footer`` directly; integration tests drive
``/save`` via ``TestClient`` with the lifespan context-manager pattern (same
as ``tests/test_reindex_concurrency.py``) and inspect the written file.
"""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import _WIKI_FOOTER_MARKER, _apply_wiki_footer, app
from palinode.core.config import config


# ---------------------------------------------------------------------------
# Unit tests for _apply_wiki_footer
# ---------------------------------------------------------------------------


class TestApplyWikiFooter:
    """Pure unit tests — no I/O, no DB."""

    # --- Basic cases ---------------------------------------------------------

    def test_entities_no_links_appends_footer(self):
        content = "Decision to use BGE-M3 for embeddings."
        result = _apply_wiki_footer(content, ["project/palinode", "person/alice"])
        assert "## See also" in result
        assert _WIKI_FOOTER_MARKER in result
        assert "- [[palinode]]" in result
        assert "- [[alice]]" in result

    def test_all_linked_inline_no_footer(self):
        content = "Using [[palinode]] and [[alice]] extensively."
        result = _apply_wiki_footer(content, ["project/palinode", "person/alice"])
        assert "## See also" not in result
        assert result == content

    def test_partial_inline_links_footer_for_missing(self):
        content = "Decided with [[alice]] to build this."
        result = _apply_wiki_footer(content, ["project/palinode", "person/alice"])
        assert "- [[palinode]]" in result
        assert "- [[alice]]" not in result.split("## See also")[-1]

    def test_no_entities_no_footer(self):
        content = "Just a note with no entities."
        result = _apply_wiki_footer(content, [])
        assert result == content

    def test_empty_content_unchanged(self):
        assert _apply_wiki_footer("", ["project/palinode"]) == ""

    def test_none_content_unchanged(self):
        assert _apply_wiki_footer(None, ["project/palinode"]) is None  # type: ignore[arg-type]

    # --- Marker presence -----------------------------------------------------

    def test_marker_comment_present(self):
        result = _apply_wiki_footer("Some note.", ["project/palinode"])
        assert _WIKI_FOOTER_MARKER in result

    def test_marker_is_html_comment(self):
        """The marker must be an HTML comment so Obsidian renders it invisibly."""
        assert _WIKI_FOOTER_MARKER.startswith("<!--")
        assert _WIKI_FOOTER_MARKER.endswith("-->")

    # --- Idempotency ---------------------------------------------------------

    def test_idempotent_double_apply(self):
        content = "A decision about [[alice]]."
        first = _apply_wiki_footer(content, ["project/palinode", "person/alice"])
        second = _apply_wiki_footer(first, ["project/palinode", "person/alice"])
        assert first == second

    def test_idempotent_no_duplicate_headers(self):
        content = "Note."
        first = _apply_wiki_footer(content, ["project/palinode"])
        second = _apply_wiki_footer(first, ["project/palinode"])
        assert second.count("## See also") == 1

    def test_footer_updated_on_entity_addition(self):
        """When entities list grows between saves the footer is replaced."""
        content = "Note."
        first = _apply_wiki_footer(content, ["project/palinode"])
        # Now add a second entity.
        second = _apply_wiki_footer(first, ["project/palinode", "person/alice"])
        assert "- [[alice]]" in second
        assert second.count("## See also") == 1

    # --- User-authored ## See also -------------------------------------------

    def test_user_authored_see_also_left_alone(self):
        content = "Note.\n\n## See also\n- [[manual-link]]\n"
        result = _apply_wiki_footer(content, ["project/palinode"])
        # User block untouched.
        assert "- [[manual-link]]" in result
        # Auto-footer appended separately.
        assert _WIKI_FOOTER_MARKER in result
        assert "- [[palinode]]" in result

    def test_user_authored_see_also_not_duplicated(self):
        content = "Note.\n\n## See also\n- [[manual-link]]\n"
        first = _apply_wiki_footer(content, ["project/palinode"])
        second = _apply_wiki_footer(first, ["project/palinode"])
        # The auto-footer should appear exactly once.
        assert second.count(_WIKI_FOOTER_MARKER) == 1

    # --- Stale footer removal ------------------------------------------------

    def test_stale_footer_removed_when_all_inline(self):
        """If user later adds inline links, the auto-footer should be dropped."""
        content_without_links = "Note."
        with_footer = _apply_wiki_footer(content_without_links, ["project/palinode"])
        assert "## See also" in with_footer

        # Now the body has inline links — footer should vanish.
        content_now_linked = with_footer.replace(
            "Note.", "Note. See [[palinode]] for details."
        )
        cleaned = _apply_wiki_footer(content_now_linked, ["project/palinode"])
        assert "## See also" not in cleaned
        assert _WIKI_FOOTER_MARKER not in cleaned

    # --- Canonicalization ----------------------------------------------------

    def test_entity_slug_after_slash(self):
        """Only the part after '/' appears in the wikilink."""
        result = _apply_wiki_footer("Note.", ["person/alice-smith"])
        assert "[[alice-smith]]" in result
        assert "[[person/alice-smith]]" not in result

    def test_entity_without_slash_used_as_is(self):
        """Bare entity refs (no '/') are used directly as the wikilink target."""
        result = _apply_wiki_footer("Note.", ["palinode"])
        assert "[[palinode]]" in result


# ---------------------------------------------------------------------------
# Integration tests — /save endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient with memory_dir + db_path pointing at tmp_path.

    Uses TestClient as a context manager so lifespan startup runs and
    store.init_db() creates the schema.
    """
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _read_body(file_path: str) -> str:
    """Return the body (everything after the closing frontmatter ---) of a file."""
    with open(file_path) as f:
        text = f.read()
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"No frontmatter in {file_path}"
    return parts[2].lstrip("\n")


def _patch_scan():
    return patch("palinode.core.store.scan_memory_content", return_value=(True, "OK"))


class TestSaveWikiFooterIntegration:

    def test_entities_no_body_links_writes_footer(self, client, tmp_path):
        with _patch_scan():
            res = client.post(
                "/save",
                json={
                    "content": "Decided to adopt BGE-M3 for all embeddings.",
                    "type": "Decision",
                    "entities": ["project/palinode", "person/alice"],
                },
            )
        assert res.status_code == 200, res.text
        body = _read_body(res.json()["file_path"])
        assert "## See also" in body
        assert _WIKI_FOOTER_MARKER in body
        assert "[[palinode]]" in body
        assert "[[alice]]" in body

    def test_entities_all_inline_no_footer(self, client, tmp_path):
        with _patch_scan():
            res = client.post(
                "/save",
                json={
                    "content": "[[palinode]] and [[alice]] discussed the decision.",
                    "type": "Decision",
                    "entities": ["project/palinode", "person/alice"],
                },
            )
        assert res.status_code == 200, res.text
        body = _read_body(res.json()["file_path"])
        assert "## See also" not in body
        assert _WIKI_FOOTER_MARKER not in body

    def test_no_entities_no_footer(self, client, tmp_path):
        with _patch_scan():
            res = client.post(
                "/save",
                json={
                    "content": "A plain note with no entity refs.",
                    "type": "Insight",
                },
            )
        assert res.status_code == 200, res.text
        body = _read_body(res.json()["file_path"])
        assert "## See also" not in body

    def test_partial_inline_footer_only_for_missing(self, client, tmp_path):
        with _patch_scan():
            res = client.post(
                "/save",
                json={
                    "content": "Agreed with [[alice]] on this.",
                    "type": "Decision",
                    "entities": ["project/palinode", "person/alice"],
                },
            )
        assert res.status_code == 200, res.text
        body = _read_body(res.json()["file_path"])
        assert "[[palinode]]" in body
        # alice is already inline; must NOT appear in footer
        footer_section = body.split("## See also")[-1] if "## See also" in body else ""
        assert "[[alice]]" not in footer_section

    def test_idempotent_save_twice(self, client, tmp_path):
        """Two identical saves produce files with the same footer structure."""
        payload = {
            "content": "Persistent note about the architecture.",
            "type": "Decision",
            "entities": ["project/palinode"],
            "slug": "idempotent-wiki-test",
        }
        with _patch_scan():
            res1 = client.post("/save", json=payload)
            res2 = client.post("/save", json=payload)
        assert res1.status_code == 200
        assert res2.status_code == 200
        body1 = _read_body(res1.json()["file_path"])
        body2 = _read_body(res2.json()["file_path"])
        # Both should have the same footer; no duplicates.
        assert body1.count("## See also") == 1
        assert body2.count("## See also") == 1
        assert body1.count(_WIKI_FOOTER_MARKER) == 1
        assert body2.count(_WIKI_FOOTER_MARKER) == 1
