"""Real SQLite regression coverage using measured bge-m3 cosine scores.

Unit vectors reproduce the issue's scores without contacting an embed host.
Runner-up values in competition tests are constructed scenarios: the issue
provides individual scores, not complete paired rankings for each prompt.
"""
from __future__ import annotations

import json
import logging
import math

import pytest

from palinode.core import store
from palinode.core.config import config
from palinode.diagnostics.runner import run_one
from palinode.diagnostics.types import DoctorContext

QUERY = [1.0] + [0.0] * 1023


def vector(score: float) -> list[float]:
    return [score, math.sqrt(1 - score * score)] + [0.0] * 1022


@pytest.fixture()
def trigger_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    store.init_db()
    return tmp_path


#: Shares three content words with every trigger ``add`` registers, so these
#: vector-rule cases clear the shared-term gate.
PROMPT = "deploy the release notes"


def add(name: str, score: float, **kwargs) -> None:
    store.add_trigger(name, f"{name}: deploy release notes", f"decisions/{name}.md",
                      vector(score), **kwargs)


@pytest.mark.parametrize("score,fires", [
    (0.456, False), (0.550, False), (0.590, False), (0.619, False),
    (0.620, True), (0.730, True), (0.842, True),
])
def test_default_threshold_is_the_gated_best_f1(trigger_db, score, fires):
    """A trigger registered without a threshold fires from 0.62, not the floor.

    0.62 is the best-F1 threshold once the shared-term gate applies, measured
    on the dogfood store's triggers (2026-10-31). Description-only on-target
    scores (0.42-0.55) and ordinary unrelated developer prompts (0.42-0.59 in
    the 2026-10-31 rig smoke) sit below it.
    """
    add("target", score)
    fired = store.check_triggers(QUERY, PROMPT)
    assert [row["id"] for row in fired] == (["target"] if fires else [])
    if fires:
        assert fired[0]["score"] == pytest.approx(score, abs=1e-6)


@pytest.mark.parametrize("score,expected", [(0.399, False), (0.400, True)])
def test_absolute_floor_even_with_lower_explicit_threshold(trigger_db, score, expected):
    add("target", score, threshold=0.0)
    assert bool(store.check_triggers(QUERY, PROMPT)) is expected


@pytest.mark.parametrize("best,runner,expected", [
    (0.550, 0.518, False), (0.518, 0.484, False),
    (0.550, 0.490, True), (0.550, 0.491, False), (0.550, 0.550, False),
    (0.400, 0.340, True), (0.400, 0.350, False),
])
def test_lead_margin_and_boundary_do_not_record_abstentions(trigger_db, best, runner, expected):
    add("best", best, threshold=0.0)
    add("runner", runner, threshold=0.0)
    fired = store.check_triggers(QUERY, PROMPT)
    assert [r["id"] for r in fired] == (["best"] if expected else [])
    counts = {r["id"]: r["fire_count"] for r in store.list_triggers()}
    assert counts == {"best": int(expected), "runner": 0}


def test_explicit_threshold_is_respected(trigger_db):
    add("target", 0.481, threshold=0.72)
    assert store.check_triggers(QUERY, PROMPT) == []
    row = store.list_triggers()[0]
    assert row["threshold"] == 0.72
    assert row["threshold_origin"] == "explicit"


def test_max_over_phrasings_competes_between_distinct_triggers(trigger_db):
    add("target", 0.456, phrasings=["write the reply", "draft a reply"],
        phrasing_embeddings=[vector(0.730), vector(0.729)])
    add("other", 0.550, threshold=0.40)
    fired = store.check_triggers(QUERY, PROMPT)
    assert [r["id"] for r in fired] == ["target"]
    assert fired[0]["score"] == pytest.approx(0.730, abs=1e-6)
    assert next(r for r in store.list_triggers() if r["id"] == "target")["phrasings"] == [
        "write the reply", "draft a reply"]
    add("target", 0.456)
    assert store.check_triggers(QUERY, PROMPT, cooldown_bypass=True)[0]["id"] == "other"
    store.delete_trigger("target")
    db = store.get_db()
    assert db.execute("SELECT count(*) FROM trigger_phrasings").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("reason", ["disabled", "expired", "cooldown", "hidden"])
def test_filtered_neighbours_do_not_compete_for_lead(trigger_db, reason):
    add("crowd", 0.99, expires_at="2000-01-01T00:00:00Z" if reason == "expired" else None)
    if reason == "disabled":
        db = store.get_db()
        db.execute("UPDATE triggers SET enabled = 0 WHERE id = 'crowd'")
        db.commit()
        db.close()
    if reason == "cooldown":
        store.update_trigger_fired("crowd")
    add("visible", 0.481, threshold=0.40)
    fired = store.check_triggers(QUERY, PROMPT, deliverable=lambda path: reason != "hidden" or "visible" in path)
    assert [r["id"] for r in fired] == ["visible"]


def test_crowding_beyond_vec0_hard_limit(trigger_db):
    db = store.get_db()
    crowd = [f"crowd-{i}" for i in range(store.VEC_KNN_MAX_K + 1)]
    db.executemany(
        "INSERT INTO triggers (id, description, memory_file, enabled) VALUES (?, 'crowd', 'hidden.md', 0)",
        [(name,) for name in crowd],
    )
    embedding = json.dumps(vector(0.99))
    db.executemany("INSERT INTO triggers_vec (id, embedding) VALUES (?, ?)",
                   [(name, embedding) for name in crowd])
    db.commit()
    db.close()
    add("visible", 0.481, threshold=0.40)
    assert [r["id"] for r in store.check_triggers(QUERY, PROMPT)] == ["visible"]


def test_migration_preserves_unknown_and_explicit_and_is_logged_once(trigger_db, caplog):
    for name, threshold in (("legacy-default-value", 0.75), ("legacy-custom", 0.72),
                            ("explicit", 0.75), ("known-default", None),
                            ("earlier-default", None)):
        add(name, 0.481, threshold=threshold)
    db = store.get_db()
    db.execute("UPDATE triggers SET threshold_origin = NULL WHERE id LIKE 'legacy-%'")
    db.execute("UPDATE triggers SET threshold = 0.75 WHERE id = 'known-default'")
    # Registered without a threshold while the default was the 0.40 floor.
    db.execute("UPDATE triggers SET threshold = 0.40 WHERE id = 'earlier-default'")
    db.commit()
    db.close()
    with caplog.at_level(logging.INFO, logger="palinode.store"):
        store.init_db()
        store.init_db()
    rows = {r["id"]: r for r in store.list_triggers()}
    assert rows["legacy-default-value"]["threshold"] == 0.75
    assert rows["legacy-custom"]["threshold"] == 0.72
    assert rows["explicit"]["threshold"] == 0.75
    assert rows["known-default"]["threshold"] == 0.62
    assert rows["earlier-default"]["threshold"] == 0.62
    db = store.get_db()
    audit = [tuple(r) for r in db.execute(
        "SELECT trigger_id, old_threshold, new_threshold, outcome FROM trigger_threshold_migrations ORDER BY trigger_id")]
    db.close()
    assert audit == [
        ("earlier-default", 0.40, 0.62, "migrated_default"),
        ("known-default", 0.75, 0.62, "migrated_default"),
        ("legacy-custom", 0.72, 0.72, "preserved_unknown_origin"),
        ("legacy-default-value", 0.75, 0.75, "preserved_unknown_origin"),
    ]
    assert sum("Trigger threshold migration" in r.message for r in caplog.records) == 4


def test_schema_upgrade_does_not_guess_origin_from_default_value(trigger_db):
    add("legacy", 0.481, threshold=0.75)
    db = store.get_db()
    db.execute("ALTER TABLE triggers DROP COLUMN threshold_origin")
    db.commit()
    db.close()
    store.init_db()
    assert store.list_triggers()[0]["threshold"] == 0.75
    assert store.list_triggers()[0]["threshold_origin"] is None


def test_doctor_reports_static_blocks_and_preserved_legacy_without_embedding(trigger_db, monkeypatch):
    from palinode.core import embedder
    monkeypatch.setattr(embedder, "embed", lambda _: pytest.fail("doctor must not embed"))
    add("reachable-high", 0.481, threshold=0.75)
    add("impossible", 0.481, threshold=1.1)
    add("expired", 0.481, expires_at="2000-01-01T00:00:00Z")
    add("disabled", 0.481)
    add("missing", 0.481)
    add("legacy", 0.481)
    db = store.get_db()
    db.execute("UPDATE triggers SET enabled = 0 WHERE id = 'disabled'")
    db.execute("UPDATE triggers SET threshold_origin = NULL WHERE id = 'legacy'")
    db.execute("DELETE FROM triggers_vec WHERE id = 'missing'")
    db.commit()
    db.close()
    result = run_one(DoctorContext(config=config), "trigger_health")
    assert not result.passed
    for text in ("4 triggers cannot fire", "impossible: unreachable threshold", "expired: expired",
                 "disabled: disabled", "missing: missing embedding", "1 legacy thresholds"):
        assert text in result.message
    assert "reachable-high" not in result.message


def test_doctor_healthy_and_missing_store(trigger_db):
    add("healthy", 0.481)
    assert run_one(DoctorContext(config=config), "trigger_health").passed
    from palinode.core.config import Config
    missing = Config(memory_dir=str(trigger_db), db_path=str(trigger_db / "absent.db"))
    assert run_one(DoctorContext(config=missing), "trigger_health").passed


def test_api_registration_embeds_phrasings_and_tracks_explicit_zero(trigger_db, monkeypatch):
    from palinode.api.routers.triggers import TriggerRequest, create_trigger_api
    from palinode.core import embedder
    embeddings = {"about replying": vector(0.456), "write the reply": vector(0.730)}
    monkeypatch.setattr(embedder, "embed", lambda text: embeddings[text])
    create_trigger_api(TriggerRequest(description="about replying", memory_file="reply.md",
                                     trigger_id="default", phrasings=["write the reply"]))
    create_trigger_api(TriggerRequest(description="about replying", memory_file="reply.md",
                                     trigger_id="explicit", threshold=0.0))
    rows = {r["id"]: r for r in store.list_triggers()}
    assert rows["default"]["threshold"] == 0.62
    assert rows["default"]["threshold_origin"] == "default"
    assert rows["default"]["phrasings"] == ["write the reply"]
    assert rows["explicit"]["threshold"] == 0.0
    assert rows["explicit"]["threshold_origin"] == "explicit"
    assert store.check_triggers(QUERY, "about replying, write the reply")[0]["id"] == "default"


def test_cli_forwards_phrasings_and_preserves_omitted_threshold(monkeypatch):
    from click.testing import CliRunner
    from palinode.cli.trigger import trigger
    from palinode.cli._api import api_client
    calls = []
    monkeypatch.setattr(api_client, "trigger_add", lambda *args, **kwargs: calls.append(kwargs) or {"id": "t"})
    result = CliRunner().invoke(trigger, ["add", "reply", "--file", "reply.md",
                                         "--phrasing", "write a reply", "--phrasing", "draft a reply"])
    assert result.exit_code == 0, result.output
    assert calls[0]["phrasings"] == ["write a reply", "draft a reply"]
    assert calls[0]["threshold"] is None


def test_mcp_forwards_phrasings(monkeypatch):
    import asyncio
    from palinode import mcp
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"id": "t"}

    async def post(path, **kwargs):
        calls.append((path, kwargs["json"]))
        return Response()

    monkeypatch.setattr(mcp, "_post", post)
    asyncio.run(mcp._tool_trigger({"action": "create", "description": "reply",
                                  "memory_file": "reply.md", "phrasings": ["write a reply"]}))
    assert calls == [("/triggers", {"description": "reply", "memory_file": "reply.md",
                                      "phrasings": ["write a reply"]})]
