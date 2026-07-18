"""Tests for the ADR-018 epistemic-marker axis (#72).

Covers:
  (a) epistemic round-trips through API / CLI-api-client and persists to
      frontmatter only when a marker is set; a missing field is `unmarked`
      (no claim — NOT fact) and the provenance panel renders it as such.
  (b) default (no param) save behaviour is UNCHANGED — no epistemic frontmatter
      is written, existing files are byte-for-byte unaffected.
  (c) an invalid value is rejected at the save surface (HTTP 400), whether it
      arrives via the first-class param or the free-form metadata dict.
  (d) `lint` flags a long-lived `open_question` as a staleness signal.

Real SQLite + tmp_path; the embedder is mocked so the test doesn't need Ollama
(mirrors test_update_policy_axis.py).
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
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
    body.setdefault("type", "Insight")
    scan, embed = _patch_io()
    with scan, embed:
        res = client.post("/save", json=body)
    return res


def _meta(file_path: str) -> dict:
    return frontmatter.load(file_path).metadata


# ── (a) epistemic round-trips and persists when set ──────────────────────────

@pytest.mark.parametrize("value", ["fact", "inference", "open_question", "unverified"])
def test_epistemic_persists_to_frontmatter(client, value):
    res = _save(client, content=f"A {value} claim.", slug=f"epi-{value}",
                epistemic=value)
    assert res.status_code == 200, res.text
    assert _meta(res.json()["file_path"])["epistemic"] == value


def test_epistemic_via_metadata_dict_persists(client):
    """An epistemic supplied through the free-form metadata dict still lands."""
    res = _save(client, content="Derived from logs.", slug="epi-meta",
                metadata={"epistemic": "inference"})
    assert res.status_code == 200, res.text
    assert _meta(res.json()["file_path"])["epistemic"] == "inference"


def test_epistemic_param_overrides_metadata(client):
    """The first-class param wins over a metadata-supplied value."""
    res = _save(client, content="Param wins.", slug="epi-prec",
                epistemic="open_question",
                metadata={"epistemic": "inference"})
    assert res.status_code == 200, res.text
    assert _meta(res.json()["file_path"])["epistemic"] == "open_question"


# ── (a2) sticky carry-forward — a marker survives a re-save that omits it ─────

def test_epistemic_sticky_carries_forward(client):
    """First save sets open_question; a later save of the same slug that omits
    the marker inherits it from the file's own frontmatter — never a silent
    downgrade to the `fact` default (ADR-018 §2)."""
    res1 = _save(client, content="Is .61 the right embed host?", slug="epi-sticky",
                 epistemic="open_question")
    assert res1.status_code == 200
    fp = res1.json()["file_path"]
    assert _meta(fp)["epistemic"] == "open_question"

    # Re-save same slug WITHOUT epistemic — sticky marker must survive.
    res2 = _save(client, content="Still investigating the embed host.",
                 slug="epi-sticky")
    assert res2.status_code == 200
    assert res2.json()["file_path"] == fp  # same file
    assert _meta(fp)["epistemic"] == "open_question"


def test_epistemic_explicit_value_overrides_sticky(client):
    """An explicit marker on a later save overrides the prior sticky value —
    this is how a writer resolves an open_question into a fact."""
    _save(client, content="Open.", slug="epi-resolve", epistemic="open_question")
    res = _save(client, content="Resolved: .61 is correct.", slug="epi-resolve",
                epistemic="fact")
    assert res.status_code == 200
    assert _meta(res.json()["file_path"])["epistemic"] == "fact"


# ── (b) default save behaviour UNCHANGED — regression guard ──────────────────

def test_default_save_writes_no_epistemic_field(client):
    """A save that never declares epistemic must NOT introduce the frontmatter
    field — existing callers' files are byte-for-byte unaffected. Stickiness
    only ever preserves a marker the writer already expressed; a never-marked
    memory stays clean."""
    res = _save(client, content="Plain memory, no marker.", slug="epi-plain")
    assert res.status_code == 200
    assert "epistemic" not in _meta(res.json()["file_path"])

    # And a re-save that still never declares it stays clean (no field minted).
    res2 = _save(client, content="Plain memory, still no marker.", slug="epi-plain")
    assert res2.status_code == 200
    assert "epistemic" not in _meta(res2.json()["file_path"])


# ── (c) invalid value rejected at the surface ────────────────────────────────

def test_invalid_epistemic_param_rejected(client):
    res = _save(client, content="bad.", slug="epi-bad", epistemic="inferrence")
    assert res.status_code == 400
    assert "epistemic" in res.text


def test_invalid_epistemic_via_metadata_rejected(client):
    """A typo'd value can't slip past validation through the metadata dict."""
    res = _save(client, content="bad meta.", slug="epi-bad-meta",
                metadata={"epistemic": "guess"})
    assert res.status_code == 400
    assert "epistemic" in res.text


# ── CLI api-client parity: the param threads into the POST body ──────────────

def test_cli_api_client_threads_epistemic():
    from palinode.cli import _api

    captured = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"file_path": "/x", "id": "insights-x"}

    class _FakeClient:
        def post(self, path, json=None, params=None):
            captured["json"] = json
            return _FakeResp()

    c = _api.PalinodeAPI.__new__(_api.PalinodeAPI)
    c.client = _FakeClient()
    c.save("body", "Insight", epistemic="open_question")
    assert captured["json"]["epistemic"] == "open_question"


def test_cli_api_client_omits_epistemic_when_absent():
    from palinode.cli import _api

    captured = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"file_path": "/x", "id": "insights-x"}

    class _FakeClient:
        def post(self, path, json=None, params=None):
            captured["json"] = json
            return _FakeResp()

    c = _api.PalinodeAPI.__new__(_api.PalinodeAPI)
    c.client = _FakeClient()
    c.save("body", "Insight")
    assert "epistemic" not in captured["json"]


# ── provenance panel: unmarked is rendered distinctly from fact ──────────────

def _claim_row(frontmatter: dict):
    from palinode.api.ui.provenance import build_provenance
    rows = build_provenance(file_path="insights/x.md", frontmatter=frontmatter,
                            history=[])
    return next(r for r in rows if r.kicker == "Claim type")


def test_provenance_unmarked_when_field_absent():
    """An absent epistemic field renders as 'unmarked' (trust-neutral), NOT as a
    fact — the load-bearing honesty of the unmarked default (ADR-018 §2)."""
    row = _claim_row({"id": "insights-x"})
    assert "unmarked" in row.value
    assert "fact" not in row.value
    assert row.state == ""  # neutral, not the 'ok' verified styling


def test_provenance_explicit_fact_is_asserted():
    row = _claim_row({"id": "insights-x", "epistemic": "fact"})
    assert row.value == "fact — observed/verified"
    assert row.state == "ok"


def test_provenance_open_question_warns():
    row = _claim_row({"id": "insights-x", "epistemic": "open_question"})
    assert "open question" in row.value
    assert row.state == "warn"


def test_provenance_unverified_is_labelled_lower_trust():
    """`unverified` renders like `inference` — an honest lower-trust assertion,
    distinct from both `fact` and the trust-neutral `unmarked` (#589)."""
    row = _claim_row({"id": "insights-x", "epistemic": "unverified"})
    assert row.value == "unverified — asserted, not checked"
    assert row.state == "ok"


# ── search-result labelling (recall surfacing, ADR-018) ─────────────────────

def _search_hit(epistemic: str | None) -> dict:
    meta = {"epistemic": epistemic} if epistemic else {}
    return {"file_path": "insights/x.md", "score": 0.9,
            "snippet": "a hit", "metadata": meta}


def test_search_results_label_unverified():
    from palinode.mcp import _format_results

    out = _format_results([_search_hit("unverified")])
    assert "[unverified]" in out


def test_search_results_leave_fact_unlabelled():
    from palinode.mcp import _format_results

    out = _format_results([_search_hit("fact")])
    assert "[unverified]" not in out and "[inference]" not in out


# ── unmarked is not a settable value ─────────────────────────────────────────

def test_explicit_unmarked_rejected(client):
    """`unmarked` is reachable only by omission, never by assertion."""
    res = _save(client, content="no.", slug="epi-unmarked", epistemic="unmarked")
    assert res.status_code == 400
    assert "epistemic" in res.text


# ── (d) lint flags a long-lived open question ────────────────────────────────

def test_lint_flags_stale_open_question(tmp_path, monkeypatch):
    # lint reads `getattr(config, "memory_dir", config.palinode_dir)`; setting
    # memory_dir is sufficient (palinode_dir is a read-only property).
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    insights = tmp_path / "insights"
    insights.mkdir()

    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()

    stale = insights / "stale-q.md"
    stale.write_text(
        "---\n"
        "id: insights-stale-q\ncategory: insights\ntype: Insight\n"
        "entities: [project/palinode]\n"
        "epistemic: open_question\n"
        f"created_at: {old}\nlast_updated: {old}\n"
        "---\n\nIs the embed host the right one?\n"
    )
    recent = insights / "fresh-q.md"
    recent.write_text(
        "---\n"
        "id: insights-fresh-q\ncategory: insights\ntype: Insight\n"
        "entities: [project/palinode]\n"
        "epistemic: open_question\n"
        f"created_at: {fresh}\nlast_updated: {fresh}\n"
        "---\n\nRecently raised question.\n"
    )

    from palinode.core import lint
    result = lint.run_lint_pass()

    flagged = {oq["file"] for oq in result["stale_open_questions"]}
    assert any("stale-q.md" in f for f in flagged)
    assert not any("fresh-q.md" in f for f in flagged)
