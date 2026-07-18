"""#508 (public #65 Q1) — claim-level source anchors: the unsigned claim_id layer.

Covers the ``claims:`` frontmatter written on save (content-addressed
``claim_id`` derivation, span ``quote_hash`` compute/verify, validation
rejections, param-or-metadata resolution), the blame read surface that
resolves each claim to the source span that justifies it, the lint advisory
finding, and the CLI ``--claim`` flag.

Real SQLite + tmp_path; no DB mocking (only the content security scanner and
the embedder are patched, matching the surrounding save-API test suites).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core.claims import (
    CLAIM_ID_HEX_LEN,
    ClaimError,
    derive_claim_id,
    format_claims_resolution,
    normalize_claims,
    parse_claims,
    resolve_memory_claims,
)
from palinode.core.config import config
from palinode.core.lint import run_lint_pass
from palinode.core.quote_verify import QuoteStatus, quote_hash

client = TestClient(app)

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
    with patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")), \
         patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
        return client.post("/save", json=json_body)


def _claim_entry(text="the chat breaker trips on model eviction",
                 source_id="research/contention.md",
                 quote="the two models share the card",
                 **extra):
    entry = {"text": text, "source_id": source_id, "span": {"quote": quote}}
    entry.update(extra)
    return entry


# ─────────────────────────────────────────────────────────────────────────────
# claim_id derivation (unit)
# ─────────────────────────────────────────────────────────────────────────────


def test_derive_claim_id_is_stable_and_truncated():
    a = derive_claim_id("insights/x.md", "some claim")
    assert a == derive_claim_id("insights/x.md", "some claim")
    assert len(a) == CLAIM_ID_HEX_LEN
    int(a, 16)  # hex


def test_derive_claim_id_normalizes_like_quote_anchors():
    """Smart punctuation + whitespace runs don't mint a new id."""
    plain = derive_claim_id("insights/x.md", 'a "quoted" claim - here')
    fancy = derive_claim_id("insights/x.md", "a  “quoted”\nclaim — here")
    assert plain == fancy


def test_derive_claim_id_is_salted_with_memory_ref():
    """Identical text in two memories gets two ids (no cross-memory aliasing)."""
    assert derive_claim_id("insights/a.md", "same claim") != \
        derive_claim_id("insights/b.md", "same claim")


def test_normalize_claims_dedups_identical_bindings():
    entries = [_claim_entry(), _claim_entry()]
    out = normalize_claims(entries, "insights/x.md")
    assert len(out) == 1


def test_normalize_claims_keeps_same_claim_with_different_span():
    entries = [_claim_entry(quote="span one"), _claim_entry(quote="span two")]
    out = normalize_claims(entries, "insights/x.md")
    assert len(out) == 2
    assert out[0]["claim_id"] == out[1]["claim_id"]


def test_normalize_claims_rejects_traversal_source_id():
    with pytest.raises(ClaimError):
        normalize_claims([_claim_entry(source_id="../outside.md")], "insights/x.md")


# ─────────────────────────────────────────────────────────────────────────────
# Save round-trip
# ─────────────────────────────────────────────────────────────────────────────


def test_save_with_claims_round_trips_frontmatter(mock_memory_dir):
    quote = "the exact justifying passage"
    text = "the deploy failed because the port was held"
    res = _save({
        "content": "claim body",
        "type": "Decision",
        "slug": "port-hold",
        "claims": [{"text": text, "source_id": "research/paper.md",
                    "span": {"quote": quote}}],
    })
    assert res.status_code == 200, res.text
    fm = _frontmatter(res.json()["file_path"])
    assert isinstance(fm["claims"], list)
    entry = fm["claims"][0]
    assert entry["text"] == text
    assert entry["source_id"] == "research/paper.md"
    assert entry["span"]["quote"] == quote
    assert entry["span"]["quote_hash"] == quote_hash(quote)
    assert entry["claim_id"] == derive_claim_id("decisions/port-hold.md", text)


def test_save_claims_anchor_id_carried_verbatim(mock_memory_dir):
    res = _save({
        "content": "x",
        "type": "Insight",
        "claims": [_claim_entry(anchor_id="page-4-para-2")],
    })
    assert res.status_code == 200, res.text
    fm = _frontmatter(res.json()["file_path"])
    assert fm["claims"][0]["anchor_id"] == "page-4-para-2"


def test_claims_absent_keeps_clean_frontmatter(mock_memory_dir):
    res = _save({"content": "x", "type": "Decision"})
    assert res.status_code == 200, res.text
    fm = _frontmatter(res.json()["file_path"])
    assert "claims" not in fm


def test_matching_supplied_claim_id_accepted(mock_memory_dir):
    text = "a pre-derived claim"
    cid = derive_claim_id("insights/pre.md", text)
    res = _save({
        "content": "x",
        "type": "Insight",
        "slug": "pre",
        "claims": [_claim_entry(text=text, claim_id=cid)],
    })
    assert res.status_code == 200, res.text
    fm = _frontmatter(res.json()["file_path"])
    assert fm["claims"][0]["claim_id"] == cid


def test_mismatching_supplied_claim_id_rejected(mock_memory_dir):
    res = _save({
        "content": "x",
        "type": "Insight",
        "slug": "pre",
        "claims": [_claim_entry(claim_id="deadbeefdeadbeef")],
    })
    assert res.status_code == 400, res.text
    assert "claim_id" in res.text


# ─────────────────────────────────────────────────────────────────────────────
# Validation rejections
# ─────────────────────────────────────────────────────────────────────────────


def test_claims_not_a_list_rejected(mock_memory_dir):
    res = _save({"content": "x", "type": "Insight", "claims": {"text": "t"}})
    assert res.status_code in (400, 422), res.text


def test_claims_entry_missing_text_rejected(mock_memory_dir):
    res = _save({
        "content": "x", "type": "Insight",
        "claims": [{"source_id": "r.md", "span": {"quote": "q"}}],
    })
    assert res.status_code == 400, res.text
    assert "text" in res.text


def test_claims_entry_missing_source_id_rejected(mock_memory_dir):
    res = _save({
        "content": "x", "type": "Insight",
        "claims": [{"text": "t", "span": {"quote": "q"}}],
    })
    assert res.status_code == 400, res.text
    assert "source_id" in res.text


def test_claims_entry_traversal_source_id_rejected(mock_memory_dir):
    res = _save({
        "content": "x", "type": "Insight",
        "claims": [_claim_entry(source_id="../../etc/passwd")],
    })
    assert res.status_code == 400, res.text


def test_claims_entry_missing_span_rejected(mock_memory_dir):
    res = _save({
        "content": "x", "type": "Insight",
        "claims": [{"text": "t", "source_id": "r.md"}],
    })
    assert res.status_code == 400, res.text
    assert "span" in res.text


def test_claims_entry_empty_quote_rejected(mock_memory_dir):
    res = _save({
        "content": "x", "type": "Insight",
        "claims": [{"text": "t", "source_id": "r.md", "span": {"quote": "  "}}],
    })
    assert res.status_code == 400, res.text
    assert "quote" in res.text


def test_claims_span_hash_mismatch_rejected(mock_memory_dir):
    res = _save({
        "content": "x", "type": "Insight",
        "claims": [{"text": "t", "source_id": "r.md",
                    "span": {"quote": "real quote", "quote_hash": "deadbeef"}}],
    })
    assert res.status_code == 400, res.text
    assert "quote_hash" in res.text


def test_claims_span_matching_hash_accepted(mock_memory_dir):
    quote = "consistent claim anchor"
    res = _save({
        "content": "x", "type": "Insight",
        "claims": [{"text": "t", "source_id": "r.md",
                    "span": {"quote": quote, "quote_hash": quote_hash(quote)}}],
    })
    assert res.status_code == 200, res.text


# ─────────────────────────────────────────────────────────────────────────────
# param-or-metadata resolution (param wins; metadata path still validated)
# ─────────────────────────────────────────────────────────────────────────────


def test_metadata_supplied_claims_are_validated_and_persisted(mock_memory_dir):
    res = _save({
        "content": "x", "type": "Insight", "slug": "via-meta",
        "metadata": {"claims": [_claim_entry()]},
    })
    assert res.status_code == 200, res.text
    fm = _frontmatter(res.json()["file_path"])
    assert fm["claims"][0]["claim_id"] == derive_claim_id(
        "insights/via-meta.md", _claim_entry()["text"]
    )


def test_metadata_supplied_malformed_claims_rejected(mock_memory_dir):
    res = _save({
        "content": "x", "type": "Insight",
        "metadata": {"claims": [{"source_id": "r.md"}]},
    })
    assert res.status_code == 400, res.text


def test_param_wins_over_metadata_claims(mock_memory_dir):
    res = _save({
        "content": "x", "type": "Insight", "slug": "pw",
        "claims": [_claim_entry(text="param claim")],
        "metadata": {"claims": [_claim_entry(text="metadata claim")]},
    })
    assert res.status_code == 200, res.text
    fm = _frontmatter(res.json()["file_path"])
    assert len(fm["claims"]) == 1
    assert fm["claims"][0]["text"] == "param claim"


# ─────────────────────────────────────────────────────────────────────────────
# Resolution (unit) + blame read surface
# ─────────────────────────────────────────────────────────────────────────────


def _seed_source(memory_dir, rel, text):
    p = memory_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _seed_memory(memory_dir, rel, claims, sources=None):
    p = memory_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {"id": "x", "category": rel.split("/")[0], "type": "Decision",
          "claims": claims}
    if sources is not None:
        fm["sources"] = sources
    p.write_text(
        f"---\n{yaml.safe_dump(fm, default_flow_style=False)}---\n\nbody\n",
        encoding="utf-8",
    )
    return p


def _seed_ok_claim(memory_dir, rel="decisions/claim.md",
                   source_rel="research/src.md",
                   quote="the cited passage lives here",
                   text="the claim under test", declare_source=True):
    _seed_source(memory_dir, source_rel, f"intro {quote} outro")
    claims = [{
        "claim_id": derive_claim_id(rel, text),
        "text": text,
        "source_id": source_rel,
        "span": {"quote": quote, "quote_hash": quote_hash(quote)},
    }]
    sources = [{"ref": source_rel, "quote": quote,
                "quote_hash": quote_hash(quote)}] if declare_source else None
    return _seed_memory(memory_dir, rel, claims, sources)


def test_resolve_ok_claim(mock_memory_dir):
    _seed_ok_claim(mock_memory_dir)
    resolved = resolve_memory_claims("decisions/claim.md", str(mock_memory_dir))
    assert len(resolved) == 1
    r = resolved[0]
    assert r["span_status"] == QuoteStatus.OK.value
    assert r["claim_id_status"] == "ok"
    assert r["source_declared"] is True


def test_resolve_drifted_span(mock_memory_dir):
    _seed_ok_claim(mock_memory_dir)
    (mock_memory_dir / "research/src.md").write_text("the source changed", encoding="utf-8")
    resolved = resolve_memory_claims("decisions/claim.md", str(mock_memory_dir))
    assert resolved[0]["span_status"] == QuoteStatus.SOURCE_DRIFTED.value


def test_resolve_missing_source(mock_memory_dir):
    _seed_ok_claim(mock_memory_dir)
    (mock_memory_dir / "research/src.md").unlink()
    resolved = resolve_memory_claims("decisions/claim.md", str(mock_memory_dir))
    assert resolved[0]["span_status"] == QuoteStatus.SOURCE_MISSING.value


def test_resolve_claim_id_mismatch(mock_memory_dir):
    """A stored id that no longer matches the derivation is flagged (e.g. the
    file was renamed out from under its content-addressed ids)."""
    quote = "still here"
    _seed_source(mock_memory_dir, "research/src.md", quote)
    _seed_memory(mock_memory_dir, "decisions/renamed.md", [{
        "claim_id": derive_claim_id("decisions/original.md", "the claim"),
        "text": "the claim",
        "source_id": "research/src.md",
        "span": {"quote": quote, "quote_hash": quote_hash(quote)},
    }])
    resolved = resolve_memory_claims("decisions/renamed.md", str(mock_memory_dir))
    assert resolved[0]["claim_id_status"] == "mismatch"


def test_resolve_undeclared_source_flagged(mock_memory_dir):
    _seed_ok_claim(mock_memory_dir, declare_source=False)
    resolved = resolve_memory_claims("decisions/claim.md", str(mock_memory_dir))
    assert resolved[0]["source_declared"] is False


def test_resolve_no_claims_is_noop(mock_memory_dir):
    p = mock_memory_dir / "decisions" / "plain.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nid: x\ntype: Decision\n---\nbody\n", encoding="utf-8")
    assert resolve_memory_claims("decisions/plain.md", str(mock_memory_dir)) == []


def test_parse_claims_soft_fail_drops_malformed():
    meta = {"claims": [
        "not a dict",
        {"text": "", "source_id": "r.md", "span": {"quote": "q"}},
        {"text": "ok", "source_id": "r.md", "span": {"quote": "q"}},
    ]}
    out = parse_claims(meta)
    assert len(out) == 1
    assert out[0]["text"] == "ok"


def test_blame_api_resolves_claims(mock_memory_dir):
    _seed_ok_claim(mock_memory_dir)
    res = client.get("/blame/decisions/claim.md", params={"claims": "true"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert "blame" in data
    assert len(data["claims"]) == 1
    assert data["claims"][0]["span_status"] == QuoteStatus.OK.value


def test_blame_api_without_claims_param_omits_key(mock_memory_dir):
    _seed_ok_claim(mock_memory_dir)
    res = client.get("/blame/decisions/claim.md")
    assert res.status_code == 200, res.text
    assert "claims" not in res.json()


def test_format_claims_resolution_renders_status_and_flags():
    text = format_claims_resolution("decisions/claim.md", [{
        "claim_id": "abc123", "text": "the claim", "source_id": "research/src.md",
        "span": {"quote": "q", "quote_hash": "h"},
        "span_status": "source_drifted", "span_detail": "cited quote not found in source",
        "claim_id_status": "ok", "source_declared": False,
    }])
    assert "source_drifted" in text
    assert "abc123" in text
    assert "source not in sources[]" in text


def test_format_claims_resolution_empty():
    assert "No claims" in format_claims_resolution("decisions/x.md", [])


# ─────────────────────────────────────────────────────────────────────────────
# Lint surface
# ─────────────────────────────────────────────────────────────────────────────


def test_lint_reports_drifted_claim(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(mock_memory_dir))
    _seed_ok_claim(mock_memory_dir)
    (mock_memory_dir / "research/src.md").write_text("changed", encoding="utf-8")
    report = run_lint_pass()
    issues = report["claim_anchor_issues"]
    assert len(issues) == 1
    assert issues[0]["file"] == "decisions/claim.md"
    assert QuoteStatus.SOURCE_DRIFTED.value in issues[0]["claims"][0]["issues"]


def test_lint_reports_undeclared_claim_source(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(mock_memory_dir))
    _seed_ok_claim(mock_memory_dir, declare_source=False)
    report = run_lint_pass()
    issues = report["claim_anchor_issues"]
    assert len(issues) == 1
    assert "source_undeclared" in issues[0]["claims"][0]["issues"]


def test_lint_ok_claim_not_reported(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(mock_memory_dir))
    _seed_ok_claim(mock_memory_dir)
    report = run_lint_pass()
    assert report["claim_anchor_issues"] == []


def test_lint_noop_when_no_claims(mock_memory_dir, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(mock_memory_dir))
    p = mock_memory_dir / "decisions" / "plain.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\nid: x\ncategory: decisions\ntype: Decision\n---\nbody\n",
                 encoding="utf-8")
    report = run_lint_pass()
    assert report["claim_anchor_issues"] == []


# ─────────────────────────────────────────────────────────────────────────────
# CLI --claim flag
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
    import importlib
    from click.testing import CliRunner
    from palinode.cli import _api

    save_mod = importlib.import_module("palinode.cli.save")
    fake = _api.PalinodeAPI.__new__(_api.PalinodeAPI)
    fake.client = _CapturingClient()
    with patch.object(save_mod, "api_client", fake):
        result = CliRunner().invoke(save_mod.save, args)
    return result, fake.client.captured


def test_cli_claim_flag_forwards_claims():
    result, body = _run_cli_save([
        "--type", "Decision",
        "--claim", "the port was held::research/paper.md::the exact passage",
        "claim body",
    ])
    assert result.exit_code == 0, result.output
    assert body["claims"] == [{
        "text": "the port was held",
        "source_id": "research/paper.md",
        "span": {"quote": "the exact passage"},
    }]


def test_cli_claim_quote_may_contain_double_colon():
    """Only the first two '::' split the triple, so quotes may contain '::'."""
    result, body = _run_cli_save([
        "--type", "Insight",
        "--claim", "claim text::r.md::see chapter 2:: the hashing part",
        "body",
    ])
    assert result.exit_code == 0, result.output
    assert body["claims"][0]["span"]["quote"] == "see chapter 2:: the hashing part"


def test_cli_claim_repeatable():
    result, body = _run_cli_save([
        "--type", "Insight",
        "--claim", "one::a.md::qa", "--claim", "two::b.md::qb",
        "body",
    ])
    assert result.exit_code == 0, result.output
    assert [c["source_id"] for c in body["claims"]] == ["a.md", "b.md"]


def test_cli_claim_malformed_rejected():
    result, body = _run_cli_save([
        "--type", "Insight", "--claim", "only-one::separator", "body",
    ])
    assert "TEXT::REF::QUOTE" in result.output
    assert result.exit_code != 0
    assert body is None


def test_cli_save_without_claim_sends_no_claims():
    result, body = _run_cli_save(["--type", "Decision", "plain body"])
    assert result.exit_code == 0, result.output
    assert "claims" not in body
