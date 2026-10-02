"""``resolve`` on the search surfaces — additive, opt-in, rendered per surface.

The REST endpoint is exercised through ``TestClient`` against a real SQLite
store under ``tmp_path``; the MCP handler through the captured-POST seam; the
renderers directly. No DB mocking.
"""
from __future__ import annotations

import hashlib
import math
import re
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

import palinode.mcp as mcp
from palinode.api.server import app
from palinode.cli.search import _evidence_lines, search as cli_search
from palinode.core import store
from palinode.core.config import config
from palinode.indexer import reconcile
from palinode.mcp import _format_results

_DIM = 1024


def _bow_embed(text: str, backend: str = "local") -> list[float]:
    vec = [0.0] * _DIM
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(tok.encode(), usedforsecurity=False).hexdigest(), 16)
        vec[h % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=_bow_embed):
        yield tmp_path


def _write(mem, rel: str, body: str, **meta) -> str:
    import yaml

    path = mem / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    content = f"---\n{fm}---\n\n{body}\n"
    path.write_text(content, encoding="utf-8")
    assert reconcile.reconcile(str(path), content).committed
    return str(path)


@pytest.fixture()
def client(mem):
    with TestClient(app) as c:
        yield c


def _corpus(mem):
    _write(mem, "decisions/db.md", "# DB\n\nWe use Postgres as the primary database.",
           status="active", superseded_by="decisions/db-v2", entities=["project/shop"])
    _write(mem, "decisions/db-v2.md", "# DB v2\n\nThe primary database is SQLite now.",
           status="active", date="2026-09-01", entities=["project/shop"])
    _write(mem, "insights/hidden.md", "# Hidden Quokka\n\nquokka contradicts the database choice",
           status="active", visibility="private", contradicts=["decisions/db"])


# ── REST ─────────────────────────────────────────────────────────────────────


def test_rest_default_is_byte_identical_and_resolve_is_additive(client, mem):
    _corpus(mem)
    body = {"query": "primary database Postgres", "limit": 1, "threshold": 0.0}

    # Recall write-back (recall_count / last_recalled / importance) moves
    # between two identical requests by design; everything else must not.
    def _stable(rows):
        return [{k: v for k, v in r.items()
                 if k not in ("recall_count", "last_recalled", "importance")} for r in rows]

    plain = _stable(client.post("/search", json=body).json())
    again = _stable(client.post("/search", json=body | {"resolve": "none"}).json())
    assert plain and "evidence" not in plain[0]
    assert plain == again

    resolved = _stable(client.post("/search", json=body | {"resolve": "linked"}).json())
    assert [r["file_path"] for r in resolved] == [r["file_path"] for r in plain]
    hit = resolved[0]
    assert hit["rel_path"] == "decisions/db.md"
    assert hit["currency"] == "retired"  # the seed is not presented as current
    block = hit["evidence"]
    # `history_withheld`: retired records left out of the evidence by default
    # (none here — the only linked record is the current successor).
    assert set(block) == {"replacements", "conflicts", "support", "discovered",
                          "seed_freshness", "coverage", "history_withheld"}
    assert block["history_withheld"] == 0
    assert [(r["ref"], r["currency"]) for r in block["replacements"]] == [("decisions/db-v2", "current")]
    assert block["coverage"]["status"] == "partial"
    assert "target_hidden" in block["coverage"]["reasons"]
    assert "fallback_disabled" in block["coverage"]["reasons"]
    # The hidden record leaks neither its title, body nor ref — only the reason.
    leak = str(resolved)
    assert "uokka" not in leak and "insights/hidden" not in leak and "Hidden" not in leak
    # Every key present without resolve is still there, unchanged.
    for k, v in plain[0].items():
        assert hit[k] == v


def test_rest_full_runs_discovery(client, mem):
    _corpus(mem)
    body = {"query": "primary database Postgres", "limit": 1, "threshold": 0.0, "resolve": "full"}
    hit = client.post("/search", json=body).json()[0]
    assert "fallback_disabled" not in hit["evidence"]["coverage"]["reasons"]


def test_rest_rejects_unknown_mode(client, mem):
    _corpus(mem)
    resp = client.post("/search", json={"query": "x", "resolve": "everything"})
    assert resp.status_code == 422


def test_rest_recency_branch_attaches_too(client, mem):
    _corpus(mem)
    hits = client.post("/search", json={"query": "", "limit": 5, "resolve": "linked"}).json()
    assert hits and all("evidence" in h for h in hits)


# ── MCP ──────────────────────────────────────────────────────────────────────


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_mcp_forwards_resolve_and_omits_none(monkeypatch):
    captured: list[dict] = []

    async def _fake_post(path, json=None, timeout=30.0):
        captured.append(json)
        return _Resp([])

    monkeypatch.setattr(mcp, "_post", _fake_post)
    monkeypatch.setattr(mcp, "_resolve_context", lambda: None)
    await mcp._tool_search({"query": "q", "resolve": "full"})
    await mcp._tool_search({"query": "q", "resolve": "none"})
    await mcp._tool_search({"query": "q"})
    assert captured[0]["resolve"] == "full"
    assert "resolve" not in captured[1] and "resolve" not in captured[2]


def _hit(**evidence) -> dict:
    return {
        "file_path": "/store/decisions/db.md",
        "score": 0.9,
        "snippet": "We use Postgres.",
        "metadata": {},
        "currency": "retired",
        "currency_reason": "superseded_by: decisions/db-v2",
        "evidence": {
            "replacements": [], "conflicts": [], "support": [], "discovered": [],
            "seed_freshness": "valid",
            "coverage": {"status": "complete", "reasons": []},
        } | evidence,
    }


def test_mcp_renders_evidence_and_coverage():
    rec = {"ref": "decisions/db-v2", "rel_path": "decisions/db-v2.md",
           "relation": "superseded_by", "direction": "forward", "depth": 1,
           "via": "decisions/db", "title": "DB v2", "currency": "current",
           "currency_reason": "status:active", "freshness": "valid",
           "effective_at": "2026-09-01T00:00:00+00:00", "epistemic": None,
           "excerpt": "The primary database is SQLite now."}
    disc = rec | {"ref": "decisions/other", "relation": "entity", "direction": "discovered",
                  "currency": "contested", "freshness": "stale"}
    out = _format_results([_hit(
        replacements=[rec], discovered=[disc],
        coverage={"status": "partial", "reasons": ["target_hidden", "budget_exhausted:edges"]},
    )])
    assert "↳ replaced by: decisions/db-v2 [current, 2026-09-01] — The primary database is SQLite now." in out
    assert "↳ discovered via entity: decisions/other [⚠ contested, ⚠ index stale, 2026-09-01]" in out
    assert "↳ coverage: partial (target_hidden, budget_exhausted:edges)" in out
    assert out.rstrip().endswith("Evidence coverage: partial (budget_exhausted:edges, target_hidden)")


def test_mcp_render_is_unchanged_without_evidence():
    row = {"file_path": "/store/decisions/db.md", "score": 0.9, "snippet": "x", "metadata": {}}
    out = _format_results([row])
    assert "↳" not in out and "Evidence coverage" not in out


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_evidence_lines_and_flag(monkeypatch):
    lines = _evidence_lines({
        "conflicts": [{"ref": "insights/b", "relation": "contradicts", "direction": "reverse",
                       "currency": "current", "freshness": "valid", "effective_at": None,
                       "excerpt": "beta"}],
        "coverage": {"status": "partial", "reasons": ["index_lag"]},
    })
    assert any("contradicted by: insights/b" in line for line in lines)
    assert any("coverage: partial (index_lag)" in line for line in lines)
    assert _evidence_lines(None) == []

    sent: dict = {}

    def _fake_search(query, **kw):
        sent.update(kw)
        # The CLI asks for the delivery receipt, so the client hands back
        # ``(results, receipt)``.
        return ([], None) if kw.get("receipt") else []

    from palinode.cli.search import api_client

    monkeypatch.setattr(api_client, "search", _fake_search)
    result = CliRunner().invoke(cli_search, ["q", "--resolve", "full", "--no-context"])
    assert result.exit_code == 0, result.output
    assert sent["resolve"] == "full"
    bad = CliRunner().invoke(cli_search, ["q", "--resolve", "everything"])
    assert bad.exit_code != 0
