"""``/search-associative`` applies the ``types`` / ``type_deny`` filters ``/search`` has.

Associative rows carry the file's live frontmatter as ``metadata``, and the
filters run inside the paging window, so the rows a filter removes cannot
crowd a match out of the delivered slate. Real SQLite in ``tmp_path``.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from palinode.api.server import app
from palinode.core import store
from palinode.core.config import config
from tests._store_helpers import upsert_entities

SEED = "project/ledger"
# Every file shares a neighbour of the seed, so spreading activation reaches
# all of them through it.
ENTITIES = [SEED, "topic/migration"]
INSIGHTS = 12


@pytest.fixture
def client(tmp_path, monkeypatch):
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config, "db_path", os.path.join(memory_dir, ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(store, "_db_checked", False)
    os.makedirs(os.path.join(memory_dir, "insights"))
    store.init_db()

    def write(name: str, memory_type: str) -> str:
        path = os.path.join(memory_dir, "insights", name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                f"---\nname: {name}\ntype: {memory_type}\n"
                f"entities: [{', '.join(ENTITIES)}]\n---\nThe ledger migration.\n"
            )
        upsert_entities(path, {"entities": ENTITIES, "category": "insights"})
        return path

    for i in range(INSIGHTS):
        write(f"a-{i:02d}.md", "Insight")
    # Sorts after every Insight, so it sits behind them in the store's window.
    write("z-decision.md", "Decision")

    with TestClient(app) as c:
        yield c


def _post(client, **body):
    res = client.post(
        "/search-associative",
        json={"query": "ledger migration", "seed_entities": [SEED], **body},
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_rows_carry_live_frontmatter(client):
    rows = _post(client, limit=INSIGHTS + 1)
    assert len(rows) == INSIGHTS + 1
    assert {r["metadata"]["type"] for r in rows} == {"Insight", "Decision"}


def test_types_reaches_a_match_behind_the_first_window(client):
    # Precondition: unfiltered, the first window holds no Decision, so a
    # filter applied after a single fetch would deliver nothing.
    first = store.search_associative("ledger migration", [SEED], top_k=1)
    assert [r["metadata"]["type"] for r in first] == ["Insight"]

    rows = _post(client, limit=1, types=["Decision"])
    assert [r["rel_path"] for r in rows] == ["insights/z-decision.md"]


def test_type_deny_excludes_and_wins_over_types(client):
    rows = _post(client, limit=1, type_deny=["Insight"])
    assert [r["metadata"]["type"] for r in rows] == ["Decision"]

    rows = _post(client, limit=5, types=["Decision"], type_deny=["Decision"])
    assert rows == []


def test_unknown_type_is_rejected(client):
    res = client.post(
        "/search-associative",
        json={"query": "ledger", "seed_entities": [SEED], "types": ["RCA"]},
    )
    assert res.status_code == 422
