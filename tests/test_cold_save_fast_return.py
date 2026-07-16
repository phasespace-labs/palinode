"""Cold first-save fast return (#619, follow-on to #611).

Before: the very first save on a fully-cold host (Ollama up, ``bge-m3``
unpulled/cold, or no Ollama at all) blocked until the full embed timeout —
per section — before the circuit breaker opened. After: until an embed has
succeeded in-process, one bounded ``probe_embed`` (negative verdict cached)
decides the whole pass; deferred sections are written as FTS-only rows —
keyword-searchable immediately, which makes the CLI's "keyword-searchable
now" note true — and converge to fully-embedded rows via the existing
missing-vec re-embed branch once the embedder warms.

Real SQLite store on tmp_path (no DB mocks); network driven through
httpx.MockTransport where a client is needed.
"""
from __future__ import annotations

import random
from unittest.mock import patch

import httpx
import pytest

from palinode.core import store
from palinode.core.config import config
from palinode.core.ollama_client import OllamaClient, RetryPolicy
from palinode.indexer import index_file as index_file_mod
from palinode.indexer.index_file import _embeds_deferred, index_file

_FAKE_VECTOR = [0.01] * 1024


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    index_file_mod._probe_cache["ts"] = 0.0
    index_file_mod._probe_cache["ok"] = None
    yield
    index_file_mod._probe_cache["ts"] = 0.0
    index_file_mod._probe_cache["ok"] = None


def _write_md(tmp_path) -> str:
    p = tmp_path / "insights" / "cold-save.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\nid: cold-save\ncategory: insights\n---\n\n"
        "Zanzibar swordfish memorandum — distinctive FTS bait text.\n"
    )
    return str(p)


def _chunk_ids_for(filepath: str) -> list[str]:
    db = store.get_db()
    rows = db.execute("SELECT id FROM chunks WHERE file_path = ?", (filepath,)).fetchall()
    db.close()
    return [r["id"] for r in rows]


def _vec_present(chunk_id: str) -> bool:
    db = store.get_db()
    row = db.execute("SELECT 1 FROM chunks_vec WHERE id = ?", (chunk_id,)).fetchone()
    db.close()
    return bool(row)


def _fts_matches(term: str) -> int:
    db = store.get_db()
    rows = db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (term,)
    ).fetchall()
    db.close()
    return len(rows)


class _FakeClient:
    def __init__(self, *, warm: bool = False, probe_ok: bool = False):
        self.has_embedded_ok = warm
        self.probe_calls = 0
        self._probe_ok = probe_ok

    def probe_embed(self, **_kw) -> bool:
        self.probe_calls += 1
        return self._probe_ok


# ── the gate: _embeds_deferred ──────────────────────────────────────────────

def test_gate_skips_probe_once_embed_path_is_proven_warm():
    client = _FakeClient(warm=True)
    assert _embeds_deferred(client) is False
    assert client.probe_calls == 0


def test_gate_defers_on_cold_probe_failure():
    client = _FakeClient(warm=False, probe_ok=False)
    assert _embeds_deferred(client) is True
    assert client.probe_calls == 1


def test_gate_caches_negative_probe_within_ttl():
    client = _FakeClient(warm=False, probe_ok=False)
    assert _embeds_deferred(client) is True
    assert _embeds_deferred(client) is True
    assert client.probe_calls == 1, "second call within TTL must not re-probe"


def test_gate_proceeds_when_probe_succeeds():
    client = _FakeClient(warm=False, probe_ok=True)
    assert _embeds_deferred(client) is False
    assert client.probe_calls == 1


# ── deferred pass: FTS-only rows, no embed attempts ─────────────────────────

def test_deferred_pass_writes_fts_only_rows_and_never_calls_embedder(tmp_store, monkeypatch):
    monkeypatch.setattr(index_file_mod, "_embeds_deferred", lambda client: True)
    filepath = _write_md(tmp_store)

    with patch("palinode.core.embedder.embed") as embed_spy:
        result = index_file(filepath)

    embed_spy.assert_not_called()
    assert result["embedded"] is False
    assert result["indexed_vec"] is False
    assert result["indexed_fts"] is True
    assert "deferred" in (result["error"] or "")
    assert result["chunks_written"] >= 1

    ids = _chunk_ids_for(filepath)
    assert ids, "chunks row must exist (keyword-searchable now)"
    assert not any(_vec_present(cid) for cid in ids), "no vector rows in deferred mode"
    assert _fts_matches("swordfish") >= 1, "deferred row must be BM25-findable"


def test_deferred_rows_converge_to_embedded_on_warm_rerun(tmp_store, monkeypatch):
    filepath = _write_md(tmp_store)

    monkeypatch.setattr(index_file_mod, "_embeds_deferred", lambda client: True)
    index_file(filepath)

    # Embedder comes back: the missing-vec re-embed branch (fix B)
    # picks the row up even though content_hash is unchanged.
    monkeypatch.setattr(index_file_mod, "_embeds_deferred", lambda client: False)
    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
        result = index_file(filepath)

    assert result["embedded"] is True
    assert result["chunks_reembedded"] >= 1
    ids = _chunk_ids_for(filepath)
    assert all(_vec_present(cid) for cid in ids), "vectors must exist after warm rerun"


def test_unchanged_and_indexed_rows_stay_untouched_in_deferred_mode(tmp_store, monkeypatch):
    filepath = _write_md(tmp_store)
    monkeypatch.setattr(index_file_mod, "_embeds_deferred", lambda client: False)
    with patch("palinode.core.embedder.embed", return_value=_FAKE_VECTOR):
        index_file(filepath)

    # A later cold pass (e.g. process restart, Ollama down) over an already
    # fully-indexed file must hit the unchanged fast path — embedded stays
    # True, no FTS-only downgrade of existing vectors.
    monkeypatch.setattr(index_file_mod, "_embeds_deferred", lambda client: True)
    result = index_file(filepath)

    assert result["embedded"] is True
    assert result["chunks_unchanged"] >= 1
    ids = _chunk_ids_for(filepath)
    assert all(_vec_present(cid) for cid in ids), "existing vectors must survive"


# ── store: vector-less upsert ───────────────────────────────────────────────

def test_upsert_chunks_empty_embedding_writes_fts_only_without_error(tmp_store, caplog):
    chunk = {
        "id": "deferred-chunk-1",
        "file_path": str(tmp_store / "insights" / "x.md"),
        "section_id": "root",
        "category": "insights",
        "content": "Quixotic zeppelin ledger for deferred upsert.",
        "metadata": {},
        "created_at": "",
        "last_updated": "",
        "embedding": [],
    }
    with caplog.at_level("ERROR", logger="palinode.store"):
        result = store.upsert_chunks([chunk], skip_unchanged=False)

    assert result["written"] == 1
    assert result["vec_ok"] is True, "deliberate vec skip is not a write failure"
    assert result["fts_ok"] is True
    assert not caplog.records, "vector-less rows must not log store errors"
    assert not _vec_present("deferred-chunk-1")
    assert _fts_matches("zeppelin") >= 1


# ── client: has_embedded_ok ─────────────────────────────────────────────────

def _make_client(handler, *, retries=0):
    transport = httpx.MockTransport(handler)
    return OllamaClient(
        retry_policy=RetryPolicy(retries=retries),
        http_client=httpx.Client(transport=transport),
        sleep=lambda s: None,
        rng=random.Random(0),
    )


def test_has_embedded_ok_flips_on_first_successful_embed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})

    client = _make_client(handler)
    assert client.has_embedded_ok is False
    assert client.embed("ok")
    assert client.has_embedded_ok is True


def test_has_embedded_ok_stays_false_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no ollama", request=request)

    client = _make_client(handler)
    assert client.probe_embed(timeout=0.5) is False
    assert client.has_embedded_ok is False


def test_probe_success_also_proves_the_path():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})

    client = _make_client(handler)
    assert client.probe_embed() is True
    assert client.has_embedded_ok is True
