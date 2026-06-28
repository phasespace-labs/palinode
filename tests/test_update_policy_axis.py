"""Tests for the ADR-015 §2.1/§2.2 write-semantics axis (PR-B).

Covers:
  (a) update_policy round-trips through API / MCP-body / CLI and persists to
      frontmatter as a sticky field; the sticky field carries forward when a
      later save omits the param.
  (b) default (append / no param) save behaviour is UNCHANGED vs current main:
      a same-slug save overwrites in place, no update_policy frontmatter is
      written, no sibling is minted. This is the critical regression guard —
      update_policy is a *declaration*, not a clobber-guard, in this PR.
  (d) status validation accepts both existing lifecycle values and the new
      incident values (open/monitoring/resolved) without regressing
      status-based search exclusion.

Real SQLite + tmp_path; the embedder is mocked so the test doesn't need Ollama
(mirrors test_created_at_preservation.py from PR-A).
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
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with TestClient(srv.app, raise_server_exceptions=True) as c:
        yield c
    srv._rate_counters.clear()


def _patch_io():
    return (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR),
    )


def _save(client, **body):
    body.setdefault("type", "ActionItem")
    scan, embed = _patch_io()
    with scan, embed:
        res = client.post("/save", json=body)
    return res


def _meta(file_path: str) -> dict:
    return frontmatter.load(file_path).metadata


# ── (a) update_policy round-trips and persists as sticky frontmatter ─────────

def test_update_policy_replace_persists_to_frontmatter(client):
    res = _save(client, content="Living infra inventory.", slug="infra-inv",
                update_policy="replace")
    assert res.status_code == 200, res.text
    meta = _meta(res.json()["file_path"])
    assert meta["update_policy"] == "replace"


def test_update_policy_append_persists_when_explicit(client):
    """An explicit append is still a declaration and is persisted."""
    res = _save(client, content="Episodic note.", slug="ep-note",
                update_policy="append")
    assert res.status_code == 200, res.text
    meta = _meta(res.json()["file_path"])
    assert meta["update_policy"] == "append"


def test_update_policy_sticky_carries_forward(client):
    """First save sets replace; a later save that omits the param inherits it
    from the file's own frontmatter (ADR-015 §6 Q2 — both param + sticky)."""
    res1 = _save(client, content="v1 current state.", slug="sticky-doc",
                 update_policy="replace")
    assert res1.status_code == 200
    fp = res1.json()["file_path"]
    assert _meta(fp)["update_policy"] == "replace"

    # Re-save same slug WITHOUT update_policy — sticky field must survive.
    res2 = _save(client, content="v2 current state.", slug="sticky-doc")
    assert res2.status_code == 200
    assert res2.json()["file_path"] == fp  # same file
    assert _meta(fp)["update_policy"] == "replace"


def test_update_policy_param_overrides_sticky(client):
    """An explicit param on a later save overrides the prior sticky value."""
    _save(client, content="v1.", slug="flip-doc", update_policy="replace")
    res = _save(client, content="v2.", slug="flip-doc", update_policy="append")
    assert res.status_code == 200
    assert _meta(res.json()["file_path"])["update_policy"] == "append"


def test_invalid_update_policy_rejected(client):
    res = _save(client, content="bad.", slug="bad-policy", update_policy="repalce")
    assert res.status_code == 400
    assert "update_policy" in res.text


def test_cli_api_client_threads_update_policy():
    """CLI api_client.save forwards update_policy into the POST body (parity)."""
    from palinode.cli import _api

    captured = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"file_path": "/x", "id": "inbox-x"}

    class _FakeClient:
        def post(self, path, json=None, params=None):
            captured["json"] = json
            return _FakeResp()

    c = _api.PalinodeAPI.__new__(_api.PalinodeAPI)
    c.client = _FakeClient()
    c.save("body", "ActionItem", update_policy="replace")
    assert captured["json"]["update_policy"] == "replace"


# ── (b) default (append) save behaviour UNCHANGED — critical regression guard ─

def test_default_save_writes_no_update_policy_field(client):
    """A save that never declares update_policy must NOT introduce the
    frontmatter field — existing callers' files are byte-for-byte unaffected by
    this PR's addition."""
    res = _save(client, content="Plain memory, no policy.", slug="plain")
    assert res.status_code == 200
    meta = _meta(res.json()["file_path"])
    assert "update_policy" not in meta


def test_default_same_slug_save_overwrites_in_place(client):
    """The §2.6 clobber-guard is OUT OF SCOPE here: a same-slug append save
    still overwrites in place (no sibling, content replaced) exactly as on
    current main. update_policy does not change this."""
    res1 = _save(client, content="original content here.", slug="dup")
    fp1 = res1.json()["file_path"]
    res2 = _save(client, content="replacement content here.", slug="dup")
    fp2 = res2.json()["file_path"]

    assert fp1 == fp2  # same file, no sibling minted
    post = frontmatter.load(fp2)
    assert "replacement content here." in post.content
    assert "original content here." not in post.content
    assert "update_policy" not in post.metadata


# ── (d) status validation: lifecycle + incident values, no exclusion regression ─

@pytest.mark.parametrize("status", ["active", "archived", "deprecated",
                                    "open", "monitoring", "resolved"])
def test_valid_status_accepted(client, status):
    res = _save(client, content=f"status {status}.", slug=f"st-{status}",
                metadata={"status": status})
    assert res.status_code == 200, res.text
    assert _meta(res.json()["file_path"])["status"] == status


def test_invalid_status_rejected(client):
    res = _save(client, content="bad status.", slug="bad-st",
                metadata={"status": "totally-bogus"})
    assert res.status_code == 400
    assert "status" in res.text


def test_incident_status_not_excluded_from_search(client):
    """An incident with status open/monitoring/resolved must remain searchable —
    the new incident vocabulary is disjoint from config.search.exclude_status
    (['archived']), so status-based exclusion does not regress. Exercised via
    the BM25 FTS path (search_fts), which applies the same exclude_status
    filter without needing a query embedding."""
    from palinode.core import store

    for st in ("open", "monitoring", "resolved"):
        _save(
            client,
            content=f"Monitor probe incident {st} state on hostalpha.",
            slug=f"incident-{st}",
            metadata={"status": st},
        )

    results = store.search_fts("monitor probe", top_k=20)
    slugs = {r.get("file_path", "") for r in results}
    # All three incident states surface (none excluded).
    for st in ("open", "monitoring", "resolved"):
        assert any(f"incident-{st}" in s for s in slugs), (
            f"incident-{st} was excluded from search; got {slugs}"
        )


def test_archived_status_still_excluded(client):
    """Regression: an `archived` status is still excluded by default recall —
    adding incident values must not loosen the existing exclusion. BM25 path."""
    from palinode.core import store

    _save(client, content="An archived widgetconfig record alpha.",
          slug="arch-doc", metadata={"status": "archived"})
    _save(client, content="An active widgetconfig record alpha.",
          slug="active-doc", metadata={"status": "active"})

    results = store.search_fts("widgetconfig", top_k=20)
    slugs = {r.get("file_path", "") for r in results}
    assert any("active-doc" in s for s in slugs), (
        f"active doc missing from search; got {slugs}"
    )
    assert not any("arch-doc" in s for s in slugs), (
        f"archived doc leaked into default recall; got {slugs}"
    )


# ── (H4) update_policy via the metadata dict is validated, not bypassed ───────

def test_update_policy_via_metadata_typo_is_rejected(client):
    """H4: a typo'd update_policy supplied through the ``metadata`` dict (not the
    first-class param) must be rejected at the surface. Previously only the
    param was validated, so a metadata value bypassed validation, landed in
    frontmatter, and could silently arm the executor replace-guard."""
    res = _save(client, content="x", slug="meta-typo",
                metadata={"update_policy": "repalce"})
    assert res.status_code == 400, res.text
    assert "update_policy" in res.text


def test_update_policy_via_metadata_valid_persists_once(client):
    """A *valid* metadata-supplied update_policy still works and is written as
    the single resolved value (the raw metadata key is excluded from the merge,
    so it can't land a second, unvalidated copy)."""
    res = _save(client, content="living state", slug="meta-valid",
                metadata={"update_policy": "replace"})
    assert res.status_code == 200, res.text
    meta = _meta(res.json()["file_path"])
    assert meta["update_policy"] == "replace"


def test_update_policy_param_wins_over_metadata(client):
    """The explicit first-class param wins when both are supplied; the metadata
    value does not override the validated param."""
    res = _save(client, content="state", slug="both-supplied",
                update_policy="replace", metadata={"update_policy": "append"})
    assert res.status_code == 200, res.text
    assert _meta(res.json()["file_path"])["update_policy"] == "replace"
