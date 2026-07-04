"""G1 (#459) — source-citation CAPTURE path tests.

Covers the capture half of the source-citation feature: the ``sources:``
quote-anchor frontmatter written on save, the auto-computed/validated
``quote_hash``, validation rejections, and the lint surface that runs the
existing verifier (``palinode.core.quote_verify``) against captured anchors.

Real SQLite + tmp_path; no DB mocking (only the content security scanner is
patched, matching the surrounding save-API test suite).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core.config import config
from palinode.core.lint import run_lint_pass
from palinode.core.quote_verify import quote_hash, verify_memory_sources, QuoteStatus

client = TestClient(app)

# A fixed unit-norm-ish vector matching the configured embedding dim so the real
# indexer (SQLite-vec + FTS5) runs without reaching a live Ollama (path).
_FAKE_VECTOR = [0.1] * 1024


@pytest.fixture
def mock_memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    yield tmp_path


def _frontmatter(file_path: str) -> dict:
    with open(file_path, "r") as f:
        text = f.read()
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"no frontmatter in {file_path}: {text[:120]}"
    return yaml.safe_load(parts[1])


def _save(json_body: dict):
    # Patch the embedder (not the DB) so the inline indexer runs against real
    # SQLite without reaching a live Ollama — the canonical save-test pattern.
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")), \
         patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
        return client.post("/save", json=json_body)


# ─────────────────────────────────────────────────────────────────────────────
# Save round-trip + quote_hash semantics
# ─────────────────────────────────────────────────────────────────────────────


def test_save_with_sources_round_trips_frontmatter(mock_memory_dir):
    quote = "the exact cited passage"
    res = _save({
        "content": "claim body",
        "type": "Decision",
        "sources": [{"ref": "research/paper.md", "quote": quote}],
    })
    assert res.status_code == 200, res.text
    fm = _frontmatter(res.json()["file_path"])
    assert isinstance(fm["sources"], list)
    assert fm["sources"][0]["ref"] == "research/paper.md"
    assert fm["sources"][0]["quote"] == quote
    assert fm["sources"][0]["quote_hash"] == quote_hash(quote)


def test_quote_hash_auto_computed_when_omitted(mock_memory_dir):
    quote = "smart “quotes”  and   spacing"
    res = _save({
        "content": "x",
        "type": "Insight",
        "sources": [{"ref": "research/p.md", "quote": quote}],
    })
    assert res.status_code == 200, res.text
    fm = _frontmatter(res.json()["file_path"])
    # Auto-computed hash equals the normalized-quote hash.
    assert fm["sources"][0]["quote_hash"] == quote_hash(quote)


def test_quote_hash_matching_supplied_is_accepted(mock_memory_dir):
    quote = "consistent anchor"
    res = _save({
        "content": "x",
        "type": "Insight",
        "sources": [{"ref": "r.md", "quote": quote, "quote_hash": quote_hash(quote)}],
    })
    assert res.status_code == 200, res.text


def test_quote_hash_mismatch_rejected(mock_memory_dir):
    res = _save({
        "content": "x",
        "type": "Insight",
        "sources": [{"ref": "r.md", "quote": "real quote", "quote_hash": "deadbeef"}],
    })
    assert res.status_code == 400, res.text
    assert "quote_hash" in res.text


def test_sources_absent_keeps_clean_frontmatter(mock_memory_dir):
    res = _save({"content": "x", "type": "Decision"})
    assert res.status_code == 200, res.text
    fm = _frontmatter(res.json()["file_path"])
    assert "sources" not in fm


# ─────────────────────────────────────────────────────────────────────────────
# Validation rejections
# ─────────────────────────────────────────────────────────────────────────────


def test_sources_not_a_list_rejected(mock_memory_dir):
    res = _save({"content": "x", "type": "Insight", "sources": {"ref": "r.md"}})
    # Pydantic rejects a non-list (422) before our normalizer; either way it's
    # a client error and no file is written.
    assert res.status_code in (400, 422), res.text


def test_sources_entry_missing_ref_rejected(mock_memory_dir):
    res = _save({
        "content": "x",
        "type": "Insight",
        "sources": [{"quote": "a quote"}],
    })
    assert res.status_code == 400, res.text
    assert "ref" in res.text


def test_sources_entry_missing_quote_rejected(mock_memory_dir):
    res = _save({
        "content": "x",
        "type": "Insight",
        "sources": [{"ref": "r.md"}],
    })
    assert res.status_code == 400, res.text
    assert "quote" in res.text


def test_sources_entry_empty_ref_rejected(mock_memory_dir):
    res = _save({
        "content": "x",
        "type": "Insight",
        "sources": [{"ref": "   ", "quote": "q"}],
    })
    assert res.status_code == 400, res.text


# ─────────────────────────────────────────────────────────────────────────────
# Verifier — OK / DRIFTED / SOURCE_MISSING against a seeded source file
# ─────────────────────────────────────────────────────────────────────────────


def _seed_source(memory_dir, rel, text):
    p = memory_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _seed_claim(memory_dir, rel, sources):
    p = memory_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump({"id": "x", "category": "decisions", "type": "Decision",
                         "sources": sources}, default_flow_style=False)
    p.write_text(f"---\n{fm}---\n\nbody\n", encoding="utf-8")
    return p


def test_verify_ok(mock_memory_dir):
    quote = "the cited passage lives here"
    _seed_source(mock_memory_dir, "research/src.md", f"intro {quote} outro")
    _seed_claim(mock_memory_dir, "decisions/claim.md",
                [{"ref": "research/src.md", "quote": quote, "quote_hash": quote_hash(quote)}])
    results = verify_memory_sources("decisions/claim.md", str(mock_memory_dir))
    assert len(results) == 1
    assert results[0].status is QuoteStatus.OK


def test_verify_drifted(mock_memory_dir):
    quote = "this passage was removed"
    _seed_source(mock_memory_dir, "research/src.md", "the source no longer says it")
    _seed_claim(mock_memory_dir, "decisions/claim.md",
                [{"ref": "research/src.md", "quote": quote, "quote_hash": quote_hash(quote)}])
    results = verify_memory_sources("decisions/claim.md", str(mock_memory_dir))
    assert results[0].status is QuoteStatus.SOURCE_DRIFTED


def test_verify_source_missing(mock_memory_dir):
    quote = "anything"
    _seed_claim(mock_memory_dir, "decisions/claim.md",
                [{"ref": "research/gone.md", "quote": quote, "quote_hash": quote_hash(quote)}])
    results = verify_memory_sources("decisions/claim.md", str(mock_memory_dir))
    assert results[0].status is QuoteStatus.SOURCE_MISSING


def test_verify_no_anchors_is_noop(mock_memory_dir):
    p = mock_memory_dir / "decisions" / "plain.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nid: x\ntype: Decision\n---\nbody\n", encoding="utf-8")
    assert verify_memory_sources("decisions/plain.md", str(mock_memory_dir)) == []


# ─────────────────────────────────────────────────────────────────────────────
# Lint surface
# ─────────────────────────────────────────────────────────────────────────────


def test_lint_reports_drifted_anchor(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(mock_memory_dir))
    quote = "missing from source now"
    _seed_source(mock_memory_dir, "research/src.md", "unrelated text")
    _seed_claim(mock_memory_dir, "decisions/claim.md",
                [{"ref": "research/src.md", "quote": quote, "quote_hash": quote_hash(quote)}])
    report = run_lint_pass()
    issues = report["source_anchor_issues"]
    assert len(issues) == 1
    assert issues[0]["file"] == "decisions/claim.md"
    assert issues[0]["anchors"][0]["status"] == QuoteStatus.SOURCE_DRIFTED.value


def test_lint_noop_when_no_anchors(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(mock_memory_dir))
    p = mock_memory_dir / "decisions" / "plain.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nid: x\ncategory: decisions\ntype: Decision\nentities:\n  - project/p\n---\nbody\n",
                 encoding="utf-8")
    report = run_lint_pass()
    assert report["source_anchor_issues"] == []


def test_lint_ok_anchor_not_reported(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(mock_memory_dir))
    quote = "still present in the source"
    _seed_source(mock_memory_dir, "research/src.md", f"prefix {quote} suffix")
    _seed_claim(mock_memory_dir, "decisions/claim.md",
                [{"ref": "research/src.md", "quote": quote, "quote_hash": quote_hash(quote)}])
    report = run_lint_pass()
    assert report["source_anchor_issues"] == []


# ─────────────────────────────────────────────────────────────────────────────
# CLI --cite flag
# ─────────────────────────────────────────────────────────────────────────────


class _CapturingClient:
    """Fake httpx client that records the POST body sent by the CLI."""

    def __init__(self) -> None:
        self.captured: dict | None = None

    def post(self, path, json=None, params=None):
        self.captured = json

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"file_path": "/x.md", "id": "decisions-x"}

        return _Resp()


def _run_cli_save(args: list[str]) -> tuple[object, dict | None]:
    """Invoke the CLI save command against a body-capturing fake API client."""
    import importlib
    from click.testing import CliRunner
    from palinode.cli import _api

    save_mod = importlib.import_module("palinode.cli.save")
    fake = _api.PalinodeAPI.__new__(_api.PalinodeAPI)
    fake.client = _CapturingClient()
    with patch.object(save_mod, "api_client", fake):
        result = CliRunner().invoke(save_mod.save, args)
    return result, fake.client.captured


def test_cli_cite_flag_forwards_sources():
    """`--cite REF::QUOTE` threads a normalized sources anchor into the body."""
    result, body = _run_cli_save(
        ["--type", "Decision", "--cite", "research/paper.md::the exact cited passage", "claim body"]
    )
    assert result.exit_code == 0, result.output
    assert body["sources"] == [
        {"ref": "research/paper.md", "quote": "the exact cited passage"}
    ]


def test_cli_cite_quote_may_contain_double_colon():
    """Only the FIRST '::' splits ref from quote, so quotes may contain '::'."""
    result, body = _run_cli_save(
        ["--type", "Insight", "--cite", "r.md::see chapter 2:: the part about hashing", "body"]
    )
    assert result.exit_code == 0, result.output
    assert body["sources"] == [
        {"ref": "r.md", "quote": "see chapter 2:: the part about hashing"}
    ]


def test_cli_cite_repeatable():
    """`--cite` is repeatable — each anchor lands in the sources list in order."""
    result, body = _run_cli_save(
        ["--type", "Insight", "--cite", "a.md::quote one", "--cite", "b.md::quote two", "body"]
    )
    assert result.exit_code == 0, result.output
    assert [s["ref"] for s in body["sources"]] == ["a.md", "b.md"]


def test_cli_cite_malformed_rejected():
    """A `--cite` value without '::' is rejected (non-zero exit, no API call)."""
    result, body = _run_cli_save(
        ["--type", "Insight", "--cite", "no-double-colon-here", "body"]
    )
    assert "REF::QUOTE" in result.output
    assert result.exit_code != 0  # raise click.Abort() → non-zero exit
    assert body is None  # never reached the API


def test_cli_save_without_cite_sends_no_sources():
    """A save with no `--cite` omits the sources field entirely (clean body)."""
    result, body = _run_cli_save(["--type", "Decision", "plain body"])
    assert result.exit_code == 0, result.output
    assert "sources" not in body
