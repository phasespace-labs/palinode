"""TTL / auto-archive for ephemeral monitoring memories (ADR-015 §2.3, #482).

Covers:
  - duration/expiry parsing (`parse_ttl`, `compute_expires_at`, `normalize_expiry`);
  - `store.set_status_for_path` propagating status to the chunk index;
  - the `archive_expired` sweep end-to-end on real SQLite + tmp_path: an expired
    memory becomes status: archived and drops out of default recall (ties #485's
    exclude_status) while remaining retrievable on demand; future/already-archived
    memories and daily/ notes are left alone; dry_run writes nothing;
  - the `/save` API resolving `metadata.ttl` → `expires_at` and rejecting
    malformed values;
  - the `/archive-expired` API sweep.
"""
from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.core.config import config
from palinode.consolidation import ttl

EMBED_DIM = 1024
_FAKE_VECTOR = [0.05] * EMBED_DIM


def _fake_embed(text: str, backend: str = "local") -> list[float]:
    return list(_FAKE_VECTOR)


# ───────────────────────────── unit: parsing ────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (3600, 3600),
        ("3600", 3600),
        ("60s", 60),
        ("5m", 300),
        ("2h", 7200),
        ("7d", 604800),
        ("1w", 604800),
        ("  2H ", 7200),
        (0, None),
        (-5, None),
        ("banana", None),
        ("", None),
        (None, None),
        (True, None),   # bool must not be read as 1 second
        (False, None),
    ],
)
def test_parse_ttl(value, expected):
    assert ttl.parse_ttl(value) == expected


def test_compute_expires_at_offsets_from_base():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    out = ttl.compute_expires_at("1h", base=base)
    assert datetime.fromisoformat(out) == base + timedelta(hours=1)


def test_compute_expires_at_invalid_returns_none():
    assert ttl.compute_expires_at("nope") is None


def test_normalize_expiry_resolves_ttl_and_drops_it():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    fm = {"ttl": "24h"}
    err = ttl.normalize_expiry(fm, now_iso=base.isoformat())
    assert err is None
    assert "ttl" not in fm  # consumed; expires_at is the single source of truth
    assert datetime.fromisoformat(fm["expires_at"]) == base + timedelta(hours=24)


def test_normalize_expiry_explicit_expires_at_wins_over_ttl():
    fm = {"ttl": "1h", "expires_at": "2030-01-01T00:00:00+00:00"}
    err = ttl.normalize_expiry(fm)
    assert err is None
    assert fm["expires_at"] == "2030-01-01T00:00:00+00:00"
    assert "ttl" not in fm


def test_normalize_expiry_rejects_bad_ttl():
    assert ttl.normalize_expiry({"ttl": "banana"}) is not None


def test_normalize_expiry_rejects_bad_expires_at():
    assert ttl.normalize_expiry({"expires_at": "not-a-date"}) is not None


def test_normalize_expiry_noop_when_absent():
    fm = {"id": "x"}
    assert ttl.normalize_expiry(fm) is None
    assert "expires_at" not in fm


def test_is_expired():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert ttl.is_expired({"expires_at": "2026-05-01T00:00:00+00:00"}, now) is True
    assert ttl.is_expired({"expires_at": "2026-07-01T00:00:00+00:00"}, now) is False
    assert ttl.is_expired({}, now) is False
    # Naive timestamps are coerced to UTC, not crashed on.
    assert ttl.is_expired({"expires_at": "2026-05-01T00:00:00"}, now) is True


# ───────────────── store/sweep: real SQLite + fake embedder ──────────────────


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    from palinode.core import store

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", os.path.join(str(tmp_path), ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for d in ("insights", "projects", "inbox", "daily"):
        os.makedirs(os.path.join(str(tmp_path), d), exist_ok=True)
    store.init_db()
    with patch("palinode.core.embedder.embed", side_effect=_fake_embed):
        yield str(tmp_path)


def _write_and_index(memory_dir: str, relpath: str, body: str, frontmatter: dict) -> str:
    import yaml
    from palinode.indexer.index_file import index_file

    path = os.path.join(memory_dir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = yaml.safe_dump(frontmatter, default_flow_style=False)
    with open(path, "w") as f:
        f.write(f"---\n{fm}---\n\n{body}\n")
    index_file(path)
    return path


def _past() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()


def _future() -> str:
    return (datetime.now(UTC) + timedelta(days=30)).isoformat()


def test_set_status_for_path_updates_chunk_metadata(isolated_store):
    from palinode.core import store

    path = _write_and_index(
        isolated_store, "insights/probe.md", "deterministic probe body",
        {"id": "insights-probe", "category": "insights", "kind": "telemetry"},
    )
    n = store.set_status_for_path(path, "archived")
    assert n >= 1
    # Idempotent — already archived, no further updates.
    assert store.set_status_for_path(path, "archived") == 0


def test_archive_expired_suppresses_from_recall_but_retains(isolated_store):
    from palinode.core import store

    # Non-telemetry on purpose: telemetry is hard-excluded from recall by
    # regardless of status, so this isolates the status-archive axis adds.
    marker = "ephemeral probe incident XYZ"
    _write_and_index(
        isolated_store, "inbox/probe-incident.md", marker,
        {"id": "inbox-probe-incident", "category": "inbox",
         "expires_at": _past()},
    )

    result = ttl.archive_expired()
    assert result["count"] == 1
    assert "inbox/probe-incident.md" in result["archived"]

    # File frontmatter is now archived.
    from palinode.core import parser
    with open(os.path.join(isolated_store, "inbox/probe-incident.md")) as f:
        meta, _ = parser.parse_markdown(f.read())
    assert meta["status"] == "archived"

    # Recall: excluded by default, retrievable with the explicit include path.
    q = [0.05] * EMBED_DIM
    default_hits = store.search(q, threshold=0.0, top_k=50, record_access=False)
    assert not any(marker in r["content"] for r in default_hits)
    all_hits = store.search(q, threshold=0.0, top_k=50, status_exclude_list=[], record_access=False)
    assert any(marker in r["content"] for r in all_hits)


def test_archive_expired_leaves_future_and_archived_alone(isolated_store):
    _write_and_index(
        isolated_store, "insights/live.md", "still valid",
        {"id": "insights-live", "category": "insights", "expires_at": _future()},
    )
    _write_and_index(
        isolated_store, "insights/already.md", "already gone",
        {"id": "insights-already", "category": "insights",
         "status": "archived", "expires_at": _past()},
    )
    _write_and_index(
        isolated_store, "insights/permanent.md", "no ttl",
        {"id": "insights-permanent", "category": "insights"},
    )
    result = ttl.archive_expired()
    assert result["count"] == 0


def test_archive_expired_skips_daily(isolated_store):
    _write_and_index(
        isolated_store, "daily/2026-01-01.md", "expired daily note",
        {"id": "daily-note", "category": "daily", "expires_at": _past()},
    )
    result = ttl.archive_expired()
    assert result["count"] == 0


def test_archive_expired_dry_run_writes_nothing(isolated_store):
    from palinode.core import parser

    path = _write_and_index(
        isolated_store, "inbox/dry.md", "expired but dry",
        {"id": "inbox-dry", "category": "inbox", "expires_at": _past()},
    )
    result = ttl.archive_expired(dry_run=True)
    assert result["count"] == 1 and result["dry_run"] is True
    with open(path) as f:
        meta, _ = parser.parse_markdown(f.read())
    assert meta.get("status") != "archived"  # untouched


# ─────────────────────── API: /save ttl + /archive-expired ───────────────────


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=list(_FAKE_VECTOR)),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as c:
            yield c, str(tmp_path)
    srv._rate_counters.clear()


def test_save_resolves_ttl_to_expires_at(api_client):
    from palinode.core import parser

    client, memory_dir = api_client
    resp = client.post("/save", json={
        "content": "probe down transition",
        "type": "ActionItem",
        "slug": "ttl-probe",
        "metadata": {"kind": "telemetry", "ttl": "1h"},
    })
    assert resp.status_code == 200, resp.text
    with open(os.path.join(memory_dir, "inbox", "ttl-probe.md")) as f:
        meta, _ = parser.parse_markdown(f.read())
    assert "expires_at" in meta and "ttl" not in meta
    # roughly now + 1h
    exp = datetime.fromisoformat(str(meta["expires_at"]))
    assert timedelta(minutes=55) < (exp - datetime.now(UTC)) < timedelta(minutes=65)


def test_save_rejects_bad_ttl(api_client):
    client, _ = api_client
    resp = client.post("/save", json={
        "content": "bad ttl", "type": "Insight", "slug": "bad-ttl",
        "metadata": {"ttl": "banana"},
    })
    assert resp.status_code == 400


def test_save_rejects_bad_expires_at(api_client):
    client, _ = api_client
    resp = client.post("/save", json={
        "content": "bad expiry", "type": "Insight", "slug": "bad-exp",
        "metadata": {"expires_at": "not-a-date"},
    })
    assert resp.status_code == 400


def test_archive_expired_endpoint(api_client):
    client, memory_dir = api_client
    # Save an already-expired ephemeral memory.
    resp = client.post("/save", json={
        "content": "expired via endpoint",
        "type": "ActionItem",
        "slug": "endpoint-expired",
        "metadata": {"kind": "telemetry", "expires_at": _past()},
    })
    assert resp.status_code == 200, resp.text

    dry = client.post("/archive-expired", json={"dry_run": True}).json()
    assert dry["count"] == 1 and dry["dry_run"] is True

    live = client.post("/archive-expired", json={"dry_run": False}).json()
    assert live["count"] == 1
    from palinode.core import parser
    with open(os.path.join(memory_dir, "inbox", "endpoint-expired.md")) as f:
        meta, _ = parser.parse_markdown(f.read())
    assert meta["status"] == "archived"
