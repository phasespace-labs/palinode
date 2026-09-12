"""``expires_at`` + ``authority`` on the two state types that act — triggers and
``core: true`` (the acting-state expiry work; ``palinode.core.expiry``).

A record may influence an action only under a current, unrevoked authority.
Covers:

  - the expiry gate itself (parse / is_past / report-once);
  - triggers: an expired trigger does not fire and is logged once, not once
    per check; an unexpired trigger fires; a trigger with no ``expires_at``
    behaves exactly as before; the schema migration on a pre-existing DB
    that lacks the columns; the ADR-015 §2.3 sweep disabling expired triggers;
  - the ``POST /triggers`` API accepting and echoing the fields, rejecting a
    malformed ``expires_at``;
  - core memories: an expired ``core: true`` memory is withheld from
    ``GET /list?core_only=true`` (the session-start hook / plugin path) and
    from ``/context/prime`` (``palinode_session_init``), stays listed and
    readable, and is reported once across both surfaces;
  - ``palinode lint`` flagging core memories with no ``expires_at``;
  - the MCP ``palinode_trigger`` handler forwarding both fields.

Real SQLite + real files under ``tmp_path``; the DB is never mocked.
"""
from __future__ import annotations

import importlib
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from palinode.core import expiry, store
from palinode.core.config import config

EMBED_DIM = 1024
_VEC = [0.05] * EMBED_DIM

PAST = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
FUTURE = (datetime.now(UTC) + timedelta(days=30)).isoformat()


@pytest.fixture(autouse=True)
def _fresh_report_ledger():
    """Report-once state is per process; isolate it per test."""
    expiry._REPORTED.clear()
    yield
    expiry._REPORTED.clear()


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", os.path.join(str(tmp_path), ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return str(tmp_path)


def _write(memory_dir: str, relpath: str, frontmatter: dict, body: str = "Body.") -> str:
    import yaml

    path = os.path.join(memory_dir, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = yaml.safe_dump(frontmatter, default_flow_style=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{fm}---\n\n{body}\n")
    return path


# ───────────────────────────── the gate ─────────────────────────────────────


class TestExpiryGate:
    def test_parse_accepts_iso_z_naive_and_datetime(self):
        assert expiry.parse_expires_at("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
        assert expiry.parse_expires_at("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)
        assert expiry.parse_expires_at(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)
        assert expiry.parse_expires_at(None) is None
        assert expiry.parse_expires_at("") is None
        assert expiry.parse_expires_at("next tuesday") is None

    def test_is_past_never_true_for_missing_or_malformed(self):
        now = datetime(2026, 6, 1, tzinfo=UTC)
        assert expiry.is_past(None, now) is False
        assert expiry.is_past("garbage", now) is False
        assert expiry.is_past("2026-05-31T23:59:59Z", now) is True
        assert expiry.is_past("2026-06-01T00:00:00Z", now) is True  # at == past
        assert expiry.is_past("2026-06-01T00:00:01Z", now) is False

    def test_report_once_per_kind_key_and_expiry(self, caplog):
        with caplog.at_level(logging.WARNING, logger="palinode.expiry"):
            assert expiry.report_expired_once("trigger", "t1", PAST) is True
            assert expiry.report_expired_once("trigger", "t1", PAST) is False
            # Re-armed with a new expires_at → reported afresh when that lapses.
            assert expiry.report_expired_once("trigger", "t1", PAST + "x") is True
            assert expiry.report_expired_once("core memory", "t1", PAST) is True
        assert sum("expired at" in r.getMessage() for r in caplog.records) == 3


# ───────────────────────────── triggers ─────────────────────────────────────


class TestTriggerExpiry:
    def test_expired_trigger_does_not_fire_and_logs_once(self, isolated_store, caplog):
        store.add_trigger("t-expired", "deploying to prod", "projects/deploy.md", _VEC,
                          expires_at=PAST, authority="paul: one-off grant")
        with caplog.at_level(logging.WARNING, logger="palinode.expiry"):
            first = store.check_triggers(_VEC, cooldown_bypass=True)
            second = store.check_triggers(_VEC, cooldown_bypass=True)
            third = store.check_triggers(_VEC, cooldown_bypass=True)
        assert first == [] and second == [] and third == []
        hits = [r for r in caplog.records if "trigger t-expired expired" in r.getMessage()]
        assert len(hits) == 1, "expired trigger must be reported once per process, not per check"
        # It did not fire: no firing bookkeeping happened.
        row = next(t for t in store.list_triggers() if t["id"] == "t-expired")
        assert row["fire_count"] == 0 and row["last_fired"] is None
        assert row["expires_at"] == PAST
        assert row["authority"] == "paul: one-off grant"

    def test_unexpired_trigger_fires(self, isolated_store):
        store.add_trigger("t-live", "deploying to prod", "projects/deploy.md", _VEC,
                          expires_at=FUTURE, authority="paul: standing")
        fired = store.check_triggers(_VEC, cooldown_bypass=True)
        assert [f["id"] for f in fired] == ["t-live"]
        row = next(t for t in store.list_triggers() if t["id"] == "t-live")
        assert row["fire_count"] == 1
        assert row["enabled"] == 1

    def test_trigger_without_expiry_behaves_as_before(self, isolated_store, caplog):
        store.add_trigger("t-eternal", "deploying to prod", "projects/deploy.md", _VEC)
        with caplog.at_level(logging.WARNING, logger="palinode.expiry"):
            fired = store.check_triggers(_VEC, cooldown_bypass=True)
        assert [f["id"] for f in fired] == ["t-eternal"]
        assert not [r for r in caplog.records if "expired" in r.getMessage()]
        row = next(t for t in store.list_triggers() if t["id"] == "t-eternal")
        assert row["expires_at"] is None and row["authority"] is None
        # No sweep side-effect either.
        assert store.expire_triggers() == []
        assert row["enabled"] == 1

    def test_migration_adds_columns_to_legacy_database(self, tmp_path, monkeypatch):
        """A DB created before the columns existed must gain them on init_db and
        keep its rows: the legacy trigger reads back with NULL expiry/authority
        and keeps firing."""
        db_path = os.path.join(str(tmp_path), ".palinode.db")
        monkeypatch.setattr(config, "memory_dir", str(tmp_path))
        monkeypatch.setattr(config, "db_path", db_path)
        monkeypatch.setattr(config.git, "auto_commit", False)
        legacy = sqlite3.connect(db_path)
        legacy.execute("""
            CREATE TABLE triggers (
                id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                memory_file TEXT NOT NULL,
                threshold FLOAT DEFAULT 0.75,
                cooldown_hours INT DEFAULT 24,
                last_fired TEXT,
                fire_count INT DEFAULT 0,
                created_at TEXT,
                enabled INT DEFAULT 1
            )
        """)
        legacy.execute(
            "INSERT INTO triggers (id, description, memory_file, created_at) VALUES (?, ?, ?, ?)",
            ("t-legacy", "old trigger", "projects/old.md", "2025-01-01T00:00:00Z"),
        )
        legacy.commit()
        legacy.close()

        store.init_db()
        store.init_db()  # idempotent: second run must not raise on existing columns

        cols = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(triggers)")}
        assert {"expires_at", "authority"} <= cols
        rows = {t["id"]: t for t in store.list_triggers()}
        assert rows["t-legacy"]["expires_at"] is None
        assert rows["t-legacy"]["authority"] is None
        # Legacy row had no vec entry; a fresh add on the migrated table works end to end.
        store.add_trigger("t-new", "new trigger", "projects/new.md", _VEC, expires_at=FUTURE)
        assert [f["id"] for f in store.check_triggers(_VEC, cooldown_bypass=True)] == ["t-new"]

    def test_ttl_sweep_disables_expired_triggers_on_the_same_clock(self, isolated_store):
        from palinode.consolidation import ttl

        store.add_trigger("t-expired", "a", "projects/a.md", _VEC, expires_at=PAST)
        store.add_trigger("t-live", "b", "projects/b.md", _VEC, expires_at=FUTURE)
        store.add_trigger("t-eternal", "c", "projects/c.md", _VEC)

        dry = ttl.archive_expired(dry_run=True)
        assert dry["triggers_expired"] == ["t-expired"]
        assert {t["id"]: t["enabled"] for t in store.list_triggers()}["t-expired"] == 1

        live = ttl.archive_expired()
        assert live["triggers_expired"] == ["t-expired"]
        enabled = {t["id"]: t["enabled"] for t in store.list_triggers()}
        assert enabled == {"t-expired": 0, "t-live": 1, "t-eternal": 1}
        # Idempotent: already-disabled triggers are not re-reported.
        assert ttl.archive_expired()["triggers_expired"] == []


# ───────────────────────────── API + core injection ─────────────────────────


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.delenv("PALINODE_PROJECT", raising=False)
    for _k in ("PALINODE_API_TOKEN", "PALINODE_API_TOKEN_FILE"):
        monkeypatch.delenv(_k, raising=False)
    store.init_db()
    import palinode.api.server as srv
    srv = importlib.reload(srv)
    srv._rate_counters.clear()
    with (
        patch("palinode.core.store.scan_memory_content", return_value=(True, "OK")),
        patch("palinode.core.embedder.embed", return_value=list(_VEC)),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as c:
            yield c, str(tmp_path)
    srv._rate_counters.clear()


class TestTriggerApi:
    def test_create_stores_and_lists_expiry_and_authority(self, api_client):
        client, _ = api_client
        resp = client.post("/triggers", json={
            "description": "talking about deploys",
            "memory_file": "projects/deploy.md",
            "trigger_id": "t-api",
            "expires_at": FUTURE,
            "authority": "session:abc123",
        })
        assert resp.status_code == 200, resp.text
        rows = {t["id"]: t for t in client.get("/triggers").json()}
        assert rows["t-api"]["expires_at"] == FUTURE
        assert rows["t-api"]["authority"] == "session:abc123"

    def test_create_without_fields_is_unchanged(self, api_client):
        client, _ = api_client
        resp = client.post("/triggers", json={
            "description": "x", "memory_file": "projects/x.md", "trigger_id": "t-plain",
        })
        assert resp.status_code == 200, resp.text
        row = {t["id"]: t for t in client.get("/triggers").json()}["t-plain"]
        assert row["expires_at"] is None and row["authority"] is None

    def test_malformed_expires_at_is_400(self, api_client):
        client, _ = api_client
        resp = client.post("/triggers", json={
            "description": "x", "memory_file": "projects/x.md", "expires_at": "next tuesday",
        })
        assert resp.status_code == 400
        assert "expires_at" in resp.json()["detail"]
        assert client.get("/triggers").json() == []


def _seed_core_memories(memory_dir: str) -> None:
    _write(memory_dir, "insights/live-core.md",
           {"type": "Insight", "core": True, "expires_at": FUTURE,
            "authority": "paul: standing", "description": "still licensed"})
    _write(memory_dir, "insights/stale-core.md",
           {"type": "Insight", "core": True, "expires_at": PAST,
            "authority": "paul: q2 grant", "description": "grant lapsed"})
    _write(memory_dir, "insights/eternal-core.md",
           {"type": "Insight", "core": True, "description": "no expiry, as before"})


class TestCoreInjectionExpiry:
    def test_list_core_only_withholds_expired_core_memory(self, api_client):
        client, memory_dir = api_client
        _seed_core_memories(memory_dir)
        files = {r["file"] for r in client.get("/list", params={"core_only": "true"}).json()}
        assert files == {"insights/live-core.md", "insights/eternal-core.md"}

    def test_expired_core_memory_stays_listed_and_readable_but_not_core(self, api_client):
        client, memory_dir = api_client
        _seed_core_memories(memory_dir)
        rows = {r["file"]: r for r in client.get("/list").json()}
        stale = rows["insights/stale-core.md"]
        assert stale["core"] is False
        assert stale["expires_at"] == PAST
        assert stale["authority"] == "paul: q2 grant"
        assert rows["insights/live-core.md"]["core"] is True
        assert rows["insights/eternal-core.md"]["core"] is True
        assert rows["insights/eternal-core.md"]["expires_at"] is None
        read = client.get("/read", params={"file_path": "insights/stale-core.md"})
        assert read.status_code == 200

    def test_context_prime_withholds_expired_core_memory(self, api_client):
        client, memory_dir = api_client
        _seed_core_memories(memory_dir)
        resp = client.post("/context/prime", json={})
        assert resp.status_code == 200, resp.text
        files = {m["file"] for m in resp.json()["core_memories"]}
        assert files == {"insights/live-core.md", "insights/eternal-core.md"}

    def test_expired_core_memory_reported_once_across_surfaces(self, api_client, caplog):
        client, memory_dir = api_client
        _seed_core_memories(memory_dir)
        with caplog.at_level(logging.WARNING, logger="palinode.expiry"):
            client.get("/list", params={"core_only": "true"})
            client.get("/list", params={"core_only": "true"})
            client.post("/context/prime", json={})
        hits = [r for r in caplog.records
                if "core memory insights/stale-core.md expired" in r.getMessage()]
        assert len(hits) == 1

    def test_lint_flags_core_memories_without_expiry(self, api_client):
        from palinode.core.lint import run_lint_pass

        _, memory_dir = api_client
        _seed_core_memories(memory_dir)
        _write(memory_dir, "insights/not-core.md", {"type": "Insight", "description": "x"})
        report = run_lint_pass()
        assert report["missing_expiry"] == ["insights/eternal-core.md"]


# ───────────────────────────── MCP handler ──────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_trigger_create_forwards_expiry_and_authority(monkeypatch):
    import palinode.mcp as mcp

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"id": "t-mcp", "status": "created"}

    async def _fake_post(path, json=None, timeout=30.0):
        captured["path"] = path
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(mcp, "_post", _fake_post)
    result = await mcp._dispatch_tool("palinode_trigger", {
        "action": "create",
        "description": "deploys",
        "memory_file": "projects/deploy.md",
        "expires_at": FUTURE,
        "authority": "policy:release-window",
    })
    assert captured["path"] == "/triggers"
    assert captured["json"]["expires_at"] == FUTURE
    assert captured["json"]["authority"] == "policy:release-window"
    assert "Created trigger t-mcp" in result[0].text

    # Omitted → not sent, so the server-side defaults are untouched.
    await mcp._dispatch_tool("palinode_trigger", {
        "action": "create", "description": "d", "memory_file": "projects/d.md",
    })
    assert "expires_at" not in captured["json"] and "authority" not in captured["json"]
