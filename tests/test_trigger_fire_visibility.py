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

#: Shares three content words with the trigger descriptions below, so these
#: cases clear the shared-term gate and test what they are about.
PROMPT = "deploying the release to prod"


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
def test_undeliverable_target_returns_nothing_and_records_no_fire(client, trigger_store, target):
    store.add_trigger("t-undeliverable", "deploying the release to prod", target, _VEC)
    response = client.post("/check-triggers", json={"query": "deploying the release to prod"})
    assert response.status_code == 200
    assert response.json() == []
    row = _row("t-undeliverable")
    assert row["fire_count"] == 0
    assert row["last_fired"] is None
    assert not (trigger_store / ".audit/retrievals.jsonl").exists()


def test_target_that_becomes_visible_fires_immediately(client, trigger_store):
    """The cooldown was not burned by the undelivered match."""
    store.add_trigger("t-later", "deploying the release to prod", "decisions/hidden.md", _VEC)
    assert client.post("/check-triggers", json={"query": "deploying the release to prod"}).json() == []

    (trigger_store / "decisions" / "hidden.md").write_text(
        "---\ncategory: decisions\n---\nnow visible\n", encoding="utf-8"
    )
    second = client.post("/check-triggers", json={"query": "deploying the release to prod"})
    assert second.status_code == 200
    assert [row["id"] for row in second.json()] == ["t-later"]
    assert _row("t-later")["fire_count"] == 1


def test_visible_target_fires_and_records_as_before(client):
    store.add_trigger("t-visible", "deploying the release to prod", "decisions/visible.md", _VEC)
    first = client.post("/check-triggers", json={"query": "deploying the release to prod"})
    assert first.status_code == 200
    assert [row["memory_file"] for row in first.json()] == ["decisions/visible.md"]
    row = _row("t-visible")
    assert row["fire_count"] == 1
    assert row["last_fired"] is not None

    # Unchanged: a delivered fire holds the trigger in cooldown.
    second = client.post("/check-triggers", json={"query": "deploying the release to prod"})
    assert second.json() == []
    assert _row("t-visible")["fire_count"] == 1
    assert _row("t-visible")["last_fired"] == row["last_fired"]


def test_store_without_predicate_still_fires_every_match(trigger_store):
    """``deliverable=None`` keeps the pre-existing store-level contract."""
    store.add_trigger("t-bare", "deploying the release to prod", "decisions/absent.md", _VEC)
    assert [row["id"] for row in store.check_triggers(_VEC, PROMPT)] == ["t-bare"]
    assert _row("t-bare")["fire_count"] == 1


def test_store_predicate_sees_the_memory_file(trigger_store):
    seen: list[str] = []
    store.add_trigger("t-pred", "deploying the release to prod", "decisions/visible.md", _VEC)
    fired = store.check_triggers(_VEC, PROMPT, deliverable=lambda path: seen.append(path) or False)
    assert fired == []
    assert seen == ["decisions/visible.md"]
    assert _row("t-pred")["fire_count"] == 0


@pytest.mark.parametrize("session_id", [None, "trigger-owner"])
def test_trigger_receipt_only_records_delivered_refs(client, trigger_store, session_id):
    import json
    from palinode.cli.retrieval_stats import _stats

    store.add_trigger("delivered", "secret trigger description", "decisions/visible.md", _VEC)
    store.add_trigger("hidden", "secret trigger description", "decisions/hidden.md", _VEC)
    target = trigger_store / "decisions/visible.md"
    original = target.read_bytes()
    request = {"query": "secret trigger description query", "session_id": session_id}
    delivered = client.post("/check-triggers", json=request).json()
    path = trigger_store / ".audit/retrievals.jsonl"
    raw = path.read_text()
    row = json.loads(raw)
    assert row["session_id"] == session_id
    assert row["fires"] == [{"trigger_id": f["id"], "ref": f["memory_file"].removesuffix(".md")}
                            for f in delivered]
    assert row["fires"] == [{"trigger_id": "delivered", "ref": "decisions/visible"}]
    assert "file_path" not in row
    for prose in ("secret trigger description", "secret trigger description query", "visible-trigger-body"):
        assert prose not in raw
    assert _stats([row])["total_events"] == 0
    with TestClient(app) as second:
        assert second.post("/check-triggers", json=request).json() == []
    assert path.read_text() == raw
    assert target.read_bytes() == original
    assert _row("delivered")["fire_count"] == 1
    assert _row("hidden")["fire_count"] == 0


@pytest.mark.parametrize("control", ["recall", "excluded", "path", "invalid", "env", "config", "capture", "bound", "io"])
def test_trigger_receipt_safeguards(client, trigger_store, monkeypatch, control):
    from palinode.core.capture_policy import update_capture_policy
    from palinode.core import retrieval_log

    store.add_trigger("t-control", "deploy release production", "decisions/visible.md", _VEC)
    if control == "recall":
        update_capture_policy(recall_paused=True)
    elif control == "capture":
        update_capture_policy(capture_paused=True)
    elif control == "excluded":
        update_capture_policy(excluded_projects=["demo"])
    elif control == "path":
        update_capture_policy(excluded_paths=[str(trigger_store)])
    elif control == "invalid":
        (trigger_store / ".capture-policy.yaml").write_text("invalid")
    elif control == "env":
        monkeypatch.setenv("PALINODE_INSTRUMENTATION_DISABLED", "1")
    elif control == "config":
        monkeypatch.setattr(config.instrumentation, "capture_retrievals", False)
    elif control == "bound":
        monkeypatch.setattr(retrieval_log, "MAX_RECEIPT_BYTES", 0)
    elif control == "io":
        (trigger_store / ".audit/retrievals.jsonl").mkdir(parents=True)
    response = client.post("/check-triggers", json={
        "query": "deploy release production", "project": "demo", "cwd": str(trigger_store),
    })
    blocked = control in {"recall", "invalid"}
    assert response.status_code == (403 if blocked else 200)
    if not blocked:
        assert [f["id"] for f in response.json()] == ["t-control"]
        assert _row("t-control")["fire_count"] == 1
    assert (trigger_store / ".audit/retrievals.jsonl").is_file() == (control == "capture")
