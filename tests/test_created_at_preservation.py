"""Tests for created_at preservation on existing-slug overwrite (ADR-015 §2.4, #430).

Before this change, ``save_api`` re-stamped both ``created_at`` and
``last_updated`` to *now* on every write, including overwrites of an existing
slug — destroying first-seen for any re-saved fact. For a living document
(re-saving the same logical memory) that birth timestamp should be preserved.

Fix: when the target file already exists, carry its existing ``created_at``
forward; only ``last_updated`` advances to now. A genuinely new slug still
stamps ``created_at = now``. Fallback when an existing file lacks
``created_at``: leave today's behaviour (stamp now) — the git-log first-commit
lookup is deferred (ADR-015 §2.4).

This is NOT gated behind ``update_policy`` (that param is PR-B): it applies to
any existing-slug overwrite, the correct default.

Real SQLite + tmp_path; the embedder is mocked so the test doesn't need Ollama.
"""
from __future__ import annotations

import importlib
from unittest.mock import patch

import frontmatter
import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config

_FAKE_VECTOR = [0.01] * 1024


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient on a fresh tmp memory_dir + real SQLite DB; git off."""
    db_path = tmp_path / ".palinode.db"
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(config.git, "auto_commit", False)
    # Bearer auth (PALINODE_API_TOKEN) is baked into the app's middleware at
    # module-import time. test_api_bearer_auth.py reloads the server module with
    # a token set and does not restore it, so in the full suite the cached app
    # can carry a token. Reload here with the token cleared so this
    # unauthenticated TestClient isn't 401'd (test-isolation, not impl).
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with TestClient(srv.app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _patch_scan():
    return patch("palinode.core.store.scan_memory_content", return_value=(True, "OK"))


def _patch_embed_ok():
    return patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR)


def _save(client, content: str, slug: str = "lyapunov-probe"):
    with _patch_scan(), _patch_embed_ok():
        res = client.post(
            "/save",
            json={"content": content, "type": "ActionItem", "slug": slug},
        )
    assert res.status_code == 200, res.text
    return res.json()["file_path"]


def _meta(file_path: str) -> dict:
    return frontmatter.load(file_path).metadata


# ── (a) overwrite preserves created_at, bumps last_updated ───────────────────

def test_overwrite_preserves_created_at_bumps_last_updated(client):
    """Re-saving the same slug keeps the original created_at but refreshes
    last_updated to now."""
    fp = _save(client, "First write of the incident.", slug="incident-x")
    first = _meta(fp)
    created0 = str(first["created_at"])
    updated0 = str(first["last_updated"])
    assert created0 == updated0  # born identical on first write

    # Re-save the same slug with new content — same logical memory.
    fp2 = _save(client, "Updated state of the incident.", slug="incident-x")
    assert fp2 == fp  # same file, no sibling minted
    second = _meta(fp2)

    assert str(second["created_at"]) == created0, "created_at must be preserved"
    assert str(second["last_updated"]) >= updated0, "last_updated must advance (or equal)"
    # The file body actually changed (proves it was a real overwrite).
    assert "Updated state" in frontmatter.load(fp2).content


def test_overwrite_preserves_created_at_across_multiple_updates(client):
    """N successive overwrites all carry the same first-seen created_at."""
    fp = _save(client, "v1", slug="multi-update")
    created0 = str(_meta(fp)["created_at"])

    for i in range(2, 5):
        _save(client, f"v{i}", slug="multi-update")
        assert str(_meta(fp)["created_at"]) == created0


# ── (b) a brand-new slug stamps created_at = now ─────────────────────────────

def test_new_slug_stamps_created_at_now(client):
    """A genuinely new file gets created_at == last_updated (both = now)."""
    fp = _save(client, "Brand new memory.", slug="fresh-slug")
    meta = _meta(fp)
    assert "created_at" in meta
    assert str(meta["created_at"]) == str(meta["last_updated"])


def test_distinct_slugs_get_independent_created_at(client):
    """Two different slugs do not share created_at — preservation is keyed by
    the target path, not global state."""
    fp_a = _save(client, "Memory A.", slug="slug-a")
    fp_b = _save(client, "Memory B.", slug="slug-b")
    assert str(_meta(fp_a)["created_at"]) != "" and str(_meta(fp_b)["created_at"]) != ""
    # Re-saving A must not disturb B's created_at.
    created_b0 = str(_meta(fp_b)["created_at"])
    _save(client, "Memory A v2.", slug="slug-a")
    assert str(_meta(fp_b)["created_at"]) == created_b0


# ── fallback: existing file lacking created_at stamps now (deliberate) ───────

def test_existing_file_without_created_at_falls_back_to_now(client, tmp_path):
    """If an existing file lacks created_at in frontmatter, the overwrite falls
    back to today's behaviour (stamp now) rather than over-engineering a
    git-log lookup (ADR-015 §2.4 — deferred)."""
    # Hand-write a file with NO created_at in its frontmatter.
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    legacy = inbox / "legacy-no-created.md"
    legacy.write_text(
        "---\n"
        "id: inbox-legacy-no-created\n"
        "category: inbox\n"
        "type: ActionItem\n"
        "---\n\n"
        "Legacy file with no created_at.\n"
    )

    fp = _save(client, "Overwrite of the legacy file.", slug="legacy-no-created")
    meta = _meta(fp)
    # created_at is now present (stamped fresh), and equals last_updated since
    # there was no prior value to carry forward.
    assert "created_at" in meta
    assert str(meta["created_at"]) == str(meta["last_updated"])
