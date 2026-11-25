"""``/resolve`` seeds rank against what the caller may see, as ``/search`` does.

``/resolve`` takes its query seeds from ``store.search_hybrid`` and gates them
afterwards. Each arm's relative floor is measured against the best candidate,
so unless the ranker is handed the caller's visibility check, a record the
caller may not see can be that best candidate: it lifts the floor past a
visible match and is then withheld itself, leaving the bundle with nothing.
``tests/test_relative_floor_over_visible_hits.py`` pins this for ``/search``;
this file pins it for the seed search behind ``/resolve``.

Real SQLite and real memory files under ``tmp_path``, with vectors at exact
cosines to the query, so every cutoff is arithmetic.
"""
from __future__ import annotations

import pytest

from palinode.core.config import config
from tests import test_relative_floor_over_visible_hits as search_floor
from tests.test_relative_floor_over_visible_hits import QUERY, _WITHHELD, _record

# The same store and client the /search floor tests use, so both routes are
# checked against identical records.
memory = search_floor.memory
client = search_floor.client


def _selected(client, **body) -> list[str]:
    resp = client.post("/resolve", json={"query": QUERY, **body})
    assert resp.status_code == 200, resp.text
    return [s["ref"] for s in resp.json()["selected"]]


@pytest.mark.parametrize("withheld", sorted(_WITHHELD))
def test_a_withheld_better_match_does_not_hide_a_visible_seed(memory, client, withheld):
    # Measured against 0.80 the vector floor is 0.68 and drops the visible 0.60.
    _record(memory, "withheld", 0.80, 1, **_WITHHELD[withheld])
    _record(memory, "visible", 0.60, 2)
    assert _selected(client) == ["insights/visible"]


def test_another_projects_better_match_does_not_hide_this_projects_seed(memory, client):
    _record(memory, "beta", 0.80, 1, entities="[project/beta]")
    _record(memory, "alpha", 0.60, 2, entities="[project/alpha]")
    assert _selected(client, project="alpha") == ["insights/alpha"]


def test_the_keyword_arm_floor_ignores_a_withheld_better_seed(memory, client, monkeypatch):
    monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    _record(memory, "withheld", 0.80, 1, visibility="private",
            content="orionledger retention window. " * 6)
    _record(memory, "visible", 0.60, 2,
            content="orionledger is mentioned once. The atrium lighting refit "
                    "is scheduled around the quarterly close. " * 4)
    assert _selected(client) == ["insights/visible"]


def test_a_visible_better_match_still_sets_the_seed_floor(memory, client):
    _record(memory, "best", 0.80, 1)
    _record(memory, "weak", 0.60, 2)
    assert _selected(client) == ["insights/best"]
