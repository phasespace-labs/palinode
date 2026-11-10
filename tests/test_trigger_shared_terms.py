"""A trigger fires only when the prompt shares enough of its own words.

Whole-prompt cosine could not tell a contributor-reply request from ordinary
maintainer chatter: on the dogfood host "reply in the thread" scored 0.709
against the reply-template trigger, above every held-out on-target prompt but
one. The vector winner now also has to share ``TRIGGER_MIN_SHARED_TERMS``
content words with its description and phrasings (porter-stemmed).

Real SQLite under ``tmp_path``; the embedder is stubbed so the vector side
always picks the trigger, which isolates the lexical gate.
"""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core import embedder, store
from palinode.core.config import config
from palinode.core.defaults import TRIGGER_MIN_SHARED_TERMS

VEC = [1.0] + [0.0] * 1023
REPLY_DESCRIPTION = (
    "User is drafting or asking how to respond to a contributor comment on a "
    "public issue - someone asking to take, be assigned to, or work on an issue"
)
REPLY_PHRASINGS = [
    "kevin asked to be assigned to 210, write the reply",
    "someone wants to take issue 250, what do we say",
    "a contributor asked to work on this issue, how do we answer",
    "answer the person asking to be assigned",
]


def vector(score: float) -> list[float]:
    return [score, (1 - score * score) ** 0.5] + [0.0] * 1022


@pytest.fixture()
def trigger_db(tmp_path, monkeypatch):
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
    (tmp_path / "insights").mkdir()
    (tmp_path / "insights" / "reply.md").write_text(
        f"---\n{yaml.safe_dump({'category': 'insights', 'type': 'Insight'})}---\nreply template\n",
        encoding="utf-8",
    )
    return tmp_path


def add_reply(**kwargs) -> None:
    store.add_trigger(
        "reply", kwargs.pop("description", REPLY_DESCRIPTION), "insights/reply.md",
        kwargs.pop("embedding", VEC), threshold=0.0,
        phrasings=kwargs.get("phrasings", REPLY_PHRASINGS),
        phrasing_embeddings=[VEC] * len(kwargs.get("phrasings", REPLY_PHRASINGS)),
    )


def fire_count(trigger_id: str = "reply") -> int:
    return next(t for t in store.list_triggers() if t["id"] == trigger_id)["fire_count"]


def test_minimum_is_three_shared_terms():
    assert TRIGGER_MIN_SHARED_TERMS == 3


@pytest.mark.parametrize("prompt", [
    "reply in the thread",              # measured 0.709 on the dogfood host
    "file an issue for this",           # 0.683
    "assign the issue to me",
])
def test_everyday_prompt_does_not_fire_through_the_api(trigger_db, monkeypatch, prompt):
    """The API path passes the prompt text through; a high cosine is not enough."""
    add_reply()
    monkeypatch.setattr(embedder, "embed", lambda _: list(VEC))
    with TestClient(app) as client:
        fired = client.post("/check-triggers", json={"query": prompt}).json()
    assert fired == []
    assert fire_count() == 0


@pytest.mark.parametrize("prompt", [
    "answer the contributor who asked about issue 251",
    "what should we tell the person asking to work on the windows bug",
    "nolanefe asked to take issue 72, write the reply",
])
def test_on_target_prompt_fires_through_the_api(trigger_db, monkeypatch, prompt):
    add_reply()
    monkeypatch.setattr(embedder, "embed", lambda _: list(VEC))
    with TestClient(app) as client:
        fired = client.post("/check-triggers", json={"query": prompt}).json()
    assert [t["id"] for t in fired] == ["reply"]
    assert fire_count() == 1


def test_shared_terms_are_stemmed_deduplicated_and_include_phrasings(trigger_db):
    add_reply(description="port a merged contribution", phrasings=["open the dev backflow"])
    db = store.get_db()
    try:
        count = lambda q: store._trigger_shared_terms(db, "reply", q)  # noqa: E731
        assert count("porting merging contributions") == 3   # stems match
        assert count("backflow BACKFLOW backflow") == 1      # one unit, counted once
        assert count("open dev backflow") == 3               # phrasing words count
        assert count("the backflow") == 1                    # "the" is dropped as a stopword
        assert store._trigger_shared_terms(db, "reply", "open dev backflow", enough=2) == 2
    finally:
        db.close()


def test_winner_failing_the_gate_does_not_hand_the_prompt_to_the_runner_up(trigger_db):
    """At most one trigger fires per prompt, and only the vector winner is tried."""
    store.add_trigger("winner", "unrelated deploy words", "insights/reply.md", vector(0.90),
                      threshold=0.0)
    store.add_trigger("runner", REPLY_DESCRIPTION, "insights/reply.md", vector(0.70),
                      threshold=0.0)
    prompt = "draft a reply to the contributor asking to take this issue"
    assert store.check_triggers(VEC, prompt) == []
    assert fire_count("winner") == fire_count("runner") == 0
    store.delete_trigger("winner")
    assert [t["id"] for t in store.check_triggers(VEC, prompt)] == ["runner"]


def test_reregistering_replaces_the_terms_and_delete_removes_them(trigger_db):
    add_reply()
    prompt = "a contributor asked to take this issue"
    assert [t["id"] for t in store.check_triggers(VEC, prompt, cooldown_bypass=True)] == ["reply"]
    add_reply(description="deploy the release to production", phrasings=[])
    assert store.check_triggers(VEC, prompt, cooldown_bypass=True) == []
    store.delete_trigger("reply")
    db = store.get_db()
    try:
        assert db.execute("SELECT count(*) FROM triggers_fts").fetchone()[0] == 0
    finally:
        db.close()


def test_existing_triggers_get_their_terms_at_startup_once(trigger_db):
    """A store from before the gate has triggers and phrasings but no term rows."""
    add_reply()
    db = store.get_db()
    db.execute("DROP TABLE triggers_fts")
    db.commit()
    db.close()

    store.init_db()
    db = store.get_db()
    rows = db.execute("SELECT trigger_id, body FROM triggers_fts ORDER BY rowid").fetchall()
    db.close()
    assert [tuple(r) for r in rows] == [("reply", REPLY_DESCRIPTION)] + [
        ("reply", text) for text in REPLY_PHRASINGS]

    store.init_db()
    db = store.get_db()
    again = db.execute("SELECT trigger_id, body FROM triggers_fts ORDER BY rowid").fetchall()
    db.close()
    assert [tuple(r) for r in again] == [tuple(r) for r in rows]
    prompt = "someone asked to be assigned this issue"
    assert [t["id"] for t in store.check_triggers(VEC, prompt)] == ["reply"]
