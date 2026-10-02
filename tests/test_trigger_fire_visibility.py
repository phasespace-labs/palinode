"""A trigger only records a fire for a delivery that actually happened.

``check_triggers`` used to write ``last_fired``/``fire_count`` inside its match
loop, and ``/check-triggers`` dropped invisible targets afterwards. A trigger
pointing at a missing or hidden file was therefore recorded as fired and sat in
its 24 h cooldown having delivered nothing, while ``fire_count`` — the only
signal for whether the trigger channel is alive — counted the non-delivery.

Visibility is now decided before the fire is recorded. Real SQLite and real
files under ``tmp_path``; only the embedder is stubbed (no Ollama in CI).
"""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core import embedder, store
from palinode.core.config import config

EMBED_DIM = 1024
_VEC = [1.0] + [0.0] * (EMBED_DIM - 1)


@pytest.fixture()
def trigger_store(tmp_path, monkeypatch):
    """Two targets: one visible, one private and out of scope."""
    monkeypatch.setenv("PALINODE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.context, "project_map", {})
    monkeypatch.setattr(config.context, "auto_detect", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    for level in ("agent", "member", "harness", "org"):
        monkeypatch.setattr(config.scope, level, None)
    store.init_db()
    (tmp_path / "decisions").mkdir()
    for name, extra in (("visible", {}), ("hidden", {"visibility": "private",
                                                     "scope": "project/known"})):
        meta = {"category": "decisions", "type": "Decision", **extra}
        (tmp_path / "decisions" / f"{name}.md").write_text(
            f"---\n{yaml.safe_dump(meta)}---\n{name}-trigger-body\n", encoding="utf-8"
        )
    monkeypatch.setattr(embedder, "embed", lambda _: list(_VEC))
    return tmp_path


@pytest.fixture()
def client(trigger_store):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _row(trigger_id: str) -> dict:
    return next(t for t in store.list_triggers() if t["id"] == trigger_id)


@pytest.mark.parametrize("target", ["decisions/hidden.md", "decisions/absent.md"])
def test_undeliverable_target_returns_nothing_and_records_no_fire(client, target):
    store.add_trigger("t-undeliverable", "deploying to prod", target, _VEC)
    response = client.post("/check-triggers", json={"query": "deploying to prod"})
    assert response.status_code == 200
    assert response.json() == []
    row = _row("t-undeliverable")
    assert row["fire_count"] == 0
    assert row["last_fired"] is None


def test_target_that_becomes_visible_fires_immediately(client, trigger_store):
    """The cooldown was not burned by the undelivered match."""
    store.add_trigger("t-later", "deploying to prod", "decisions/hidden.md", _VEC)
    assert client.post("/check-triggers", json={"query": "deploying to prod"}).json() == []

    (trigger_store / "decisions" / "hidden.md").write_text(
        "---\ncategory: decisions\n---\nnow visible\n", encoding="utf-8"
    )
    second = client.post("/check-triggers", json={"query": "deploying to prod"})
    assert second.status_code == 200
    assert [row["id"] for row in second.json()] == ["t-later"]
    assert _row("t-later")["fire_count"] == 1


def test_visible_target_fires_and_records_as_before(client):
    store.add_trigger("t-visible", "deploying to prod", "decisions/visible.md", _VEC)
    first = client.post("/check-triggers", json={"query": "deploying to prod"})
    assert first.status_code == 200
    assert [row["memory_file"] for row in first.json()] == ["decisions/visible.md"]
    row = _row("t-visible")
    assert row["fire_count"] == 1
    assert row["last_fired"] is not None

    # Unchanged: a delivered fire holds the trigger in cooldown.
    second = client.post("/check-triggers", json={"query": "deploying to prod"})
    assert second.json() == []
    assert _row("t-visible")["fire_count"] == 1
    assert _row("t-visible")["last_fired"] == row["last_fired"]


def test_store_without_predicate_still_fires_every_match(trigger_store):
    """``deliverable=None`` keeps the pre-existing store-level contract."""
    store.add_trigger("t-bare", "deploying to prod", "decisions/absent.md", _VEC)
    assert [row["id"] for row in store.check_triggers(_VEC)] == ["t-bare"]
    assert _row("t-bare")["fire_count"] == 1


def test_store_predicate_sees_the_memory_file(trigger_store):
    seen: list[str] = []
    store.add_trigger("t-pred", "deploying to prod", "decisions/visible.md", _VEC)
    fired = store.check_triggers(_VEC, deliverable=lambda path: seen.append(path) or False)
    assert fired == []
    assert seen == ["decisions/visible.md"]
    assert _row("t-pred")["fire_count"] == 0
