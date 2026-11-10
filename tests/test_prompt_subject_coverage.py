"""Automatic recall asks what a prompt is about.

The measured defect: on a large store of one project's work notes, everyday
commands ("bump the version and tag it", "commit this and push") reached
``confident`` and the per-prompt hook injected unrelated memories. Their words
travel together in release and session notes, so a keyword row held them all,
rarer than chance under independence; and any prompt found a chunk near the
vector arm's 0.60 mark. Two rules, prompt mode only:

* the keyword arm vouches only through a row that matched one of the prompt's
  subject terms (``prompt_query.parse_prompt``);
* the vector arm's mark sits ``VECTOR_NULL_MARGIN`` above what a contentless
  prompt scores in the store (``vector_baseline``).

Real SQLite + FTS5 under ``tmp_path`` throughout; no DB mocking. The embedder
is stubbed where a test needs vectors, as elsewhere in the suite.
"""
from __future__ import annotations

import math
import tomllib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from palinode.core import confidence, embedder, store, vector_baseline
from palinode.core.bundle import BundleBudget, BundleRequest, build_bundle
from palinode.core.config import config
from palinode.core.prompt_query import (
    WORKING_STATE,
    command_without_subject,
    prompt_shape,
    shape_prompt,
)
from palinode.core.scoring import describe_diagnostics
from tests._store_helpers import upsert_chunks

ROOT = Path(__file__).resolve().parent.parent
_DIM = 1024


# --- the parse ---------------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    "bump the version and tag it",
    "commit this and push",
    "lets make a plan to work on these:",
    "ok now do the same for the other two files",
    "delete the old branches and prune the remote",
    "close the stale issues and update the labels",
    "can you take this issue and work on it",
    "merge 9502 and 9503",
    # Held out when the rule was measured: written before it was scored.
    "push it to the remote",
    "tag the release and publish the notes",
    "close this PR and delete the branch",
    "update the changelog and bump the patch version",
])
def test_commands_that_name_nothing_have_no_subject(prompt):
    shape = prompt_shape(prompt)
    assert shape.command
    assert shape.subject_terms == ()
    assert shape.subject_units == frozenset()
    assert command_without_subject(shape_prompt(prompt))


@pytest.mark.parametrize("prompt,subject", [
    ("fix the watcher debounce", ("watcher", "debounce")),
    ("update the envelope guard to handle unmatched closing tags",
     ("envelope", "guard", "unmatched", "closing")),
    ("change the sync history retention to 30 days", ("sync", "history", "retention", "days")),
    ("add a test for the daily note retention setting", ("daily", "note", "retention", "setting")),
    # A clause opened by a pronoun is a statement: its words are subject.
    ("revert the last commit, it broke the dashboard", ("broke", "dashboard")),
])
def test_commands_with_a_subject_keep_it(prompt, subject):
    shape = prompt_shape(prompt)
    assert shape.command
    assert shape.subject_terms == subject
    assert not command_without_subject(shape.query)


def test_questions_keep_every_content_word_and_drop_working_state():
    shape = prompt_shape("what threshold do new triggers default to")
    assert shape.question and not shape.command
    assert shape.subject_terms == ("threshold", "triggers", "default")
    assert shape.working_state_terms == ("new",)
    nightly = prompt_shape("how does the dogfood nightly catch-up bound work")
    assert nightly.subject_terms == ("dogfood", "nightly", "catch-up", "bound")
    assert '"catch up"' in nightly.subject_units


def test_an_identifier_is_always_subject():
    assert prompt_shape("sync.batch_window_ms").subject_terms == ("sync.batch_window_ms",)
    assert prompt_shape("merge it after ADR-012").subject_terms == ("ADR-012",)


def test_shape_prompt_still_returns_the_same_string():
    raw = "<system-reminder>noise</system-reminder>Thanks! bump the version and tag it"
    assert prompt_shape(raw).query == shape_prompt(raw) == "bump the version and tag it"


def test_the_working_state_vocabulary_is_shipped_data():
    text = (ROOT / "palinode" / "core" / "working_state.txt").read_text(encoding="utf-8")
    words = {w for line in text.splitlines() for w in line.split("#", 1)[0].split()}
    assert words == set(WORKING_STATE)
    assert {"branch", "commit", "version", "tag", "it", "same"} <= WORKING_STATE
    # Generic lowercase English only: the file ships publicly.
    assert all(w.isalpha() and w == w.lower() for w in WORKING_STATE)
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "working_state.txt" in pyproject["tool"]["setuptools"]["package-data"]["palinode.core"]


# --- the store: which units a keyword row holds -----------------------------

def _chunk(chunk_id: str, content: str, cosine: float = 0.0) -> dict:
    return {
        "id": chunk_id,
        "file_path": f"projects/{chunk_id}.md",
        "section_id": "root",
        "category": "projects",
        "content": content,
        "metadata": {},
        "created_at": "2026-10-31T00:00:00+00:00",
        "last_updated": "2026-10-31T00:00:00+00:00",
        "embedding": [cosine, math.sqrt(1 - cosine ** 2)] + [0.0] * (_DIM - 2),
    }


#: Forty notes. Three are release prep holding "bump", "version" and "tag"
#: together, which is rarer than chance would put three words in forty notes
#: and is still not what a command to bump the version is asking about.
_NOTES = (
    [_chunk(f"chore-{i}", f"Session {i}: reviewed the planner and updated the dashboard.")
     for i in range(36)]
    + [_chunk(f"release-{i}", f"Release prep {i}: bump the version, then tag the build.")
       for i in range(3)]
    + [_chunk("watcher", "The watcher debounce coalesces two filesystem events into one reindex.")]
)


@pytest.fixture()
def notes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.decay, "enabled", False)
    store.init_db()
    rows = [dict(row) for row in _NOTES]
    upsert_chunks(rows, skip_unchanged=False)
    for row in rows:
        path = tmp_path / row["file_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"status": "active", "type": "ProjectSnapshot", "date": "2026-10-31"}
        path.write_text(f"---\n{yaml.safe_dump(meta)}---\n\n{row['content']}\n")
    return tmp_path


def test_keyword_rows_carry_the_units_they_matched(notes):
    hits = {h["id"]: h for h in store.search_fts("bump the version and fix the watcher", top_k=50)}
    assert hits["release-0"]["keyword_units"] == ['"bump"', '"version"']
    assert hits["watcher"]["keyword_units"] == ['"watcher"']
    one = store.search_fts("debounce", top_k=5)
    assert one and all(h["keyword_units"] == ['"debounce"'] for h in one)


def test_the_units_ride_through_the_ranker(notes):
    rows = store.search_hybrid("fix the watcher debounce", None, top_k=5, record_access=False)
    watcher = [r for r in rows if r["id"] == "watcher"]
    assert watcher and set(watcher[0]["keyword_units"]) == {'"watcher"', '"debounce"'}


def test_unit_hits_and_chance_agree(notes):
    """The chance figure now reads the hits instead of querying again."""
    db = store.get_db()
    try:
        units = store.fts_match_units("bump version tag")
        rowids = [r[0] for r in db.execute("SELECT rowid FROM chunks_fts")]
        hits = store.keyword_unit_hits(db, units, rowids)
        assert store.keyword_chance(db, units, rowids, hits=hits) == store.keyword_chance(
            db, units, rowids)
    finally:
        db.close()


# --- the verdict -------------------------------------------------------------

_RELEASE_ROW = {"raw_score": None, "keyword_score": 1.2, "keyword_chance": 0.2,
                "keyword_units": ['"bump"', '"version"', '"tag"']}


class TestSubjectCoverage:
    def test_a_prompt_with_no_subject_never_vouches(self):
        value = confidence.assess([_RELEASE_ROW], active_mode="lexical", subject_units=frozenset())
        assert value["confidence"] == "weak"
        assert value["subject_coverage"] == "no_subject"
        assert value["arms"]["keyword"]["verdict"] == "confident"

    def test_a_match_on_working_state_alone_does_not_vouch(self):
        row = {**_RELEASE_ROW, "keyword_units": ['"commit"', '"last"']}
        value = confidence.assess([row], active_mode="lexical",
                                  subject_units=frozenset({'"dashboard"', '"broke"'}))
        assert value["confidence"] == "weak"
        assert value["subject_coverage"] == "working_state_only"

    def test_one_covered_row_is_enough(self):
        rows = [_RELEASE_ROW, {**_RELEASE_ROW, "keyword_units": ['"watcher"']}]
        value = confidence.assess(rows, active_mode="lexical",
                                  subject_units=frozenset({'"watcher"'}))
        assert value["confidence"] == "confident"
        assert "subject_coverage" not in value

    def test_without_a_subject_set_nothing_changes(self):
        """Explicit search passes none: the verdict is the one it always gave."""
        assert confidence.assess([_RELEASE_ROW], active_mode="lexical")["confidence"] == "confident"

    def test_a_row_that_does_not_say_its_units_is_not_judged(self):
        row = {k: v for k, v in _RELEASE_ROW.items() if k != "keyword_units"}
        value = confidence.assess([row], active_mode="lexical", subject_units=frozenset({'"x"'}))
        assert value["confidence"] == "confident"

    def test_the_diagnostics_line_says_why(self):
        value = confidence.assess([_RELEASE_ROW], active_mode="lexical", subject_units=frozenset())
        line = describe_diagnostics(
            {"active_mode": "lexical", "index_state": "ready", "outcome": "matched", **value})
        assert "match confidence: weak" in line
        assert "names nothing a memory could be about" in line


class TestVectorMark:
    def test_the_mark_rises_over_the_background(self):
        rows = [{"raw_score": 0.66, "keyword_score": 0.4, "keyword_chance": 0.5}]
        assert confidence.assess(rows, active_mode="hybrid")["confidence"] == "confident"
        value = confidence.assess(rows, active_mode="hybrid", vector_confident_at=0.73)
        assert value["confidence"] == "weak"
        assert value["arms"]["vector"]["confident_at"] == pytest.approx(0.73)

    def test_the_mark_never_falls_below_the_calibration(self):
        rows = [{"raw_score": 0.58, "keyword_score": 0.4, "keyword_chance": 0.5}]
        value = confidence.assess(rows, active_mode="hybrid", vector_confident_at=0.52)
        assert value["arms"]["vector"]["confident_at"] == confidence.VECTOR_CONFIDENT
        assert value["confidence"] == "weak"


# --- end to end through the real store, ranker and bundle ---------------------

@pytest.fixture()
def lexical_notes(notes, monkeypatch):
    monkeypatch.setattr(config.search, "retrieval_mode", "lexical")
    return notes


def _verdict(prompt: str, *, as_prompt: bool) -> dict:
    bundle = build_bundle(BundleRequest(query=prompt, prompt=as_prompt,
                                        budget=BundleBudget(max_items=5, max_chars=3000)))
    return bundle.retrieval


def test_an_everyday_command_is_not_confident_recall(lexical_notes):
    """Fails before subject coverage: the release-prep rows made this confident."""
    retrieval = _verdict("bump the version and tag it", as_prompt=True)
    assert retrieval["arms"]["keyword"]["verdict"] == "confident"
    assert retrieval["confidence"] == "weak"
    assert retrieval["subject_coverage"] == "no_subject"


def test_the_same_words_as_an_explicit_query_are_unchanged(lexical_notes):
    assert _verdict("bump the version and tag it", as_prompt=False)["confidence"] == "confident"


def test_a_command_with_a_subject_stays_confident(lexical_notes):
    retrieval = _verdict("fix the watcher debounce", as_prompt=True)
    assert retrieval["confidence"] == "confident"
    assert "subject_coverage" not in retrieval


def test_a_mixed_prompt_whose_match_missed_its_subject_is_weak(lexical_notes):
    prompt = "bump the version and tag it, the planner broke"
    retrieval = _verdict(prompt, as_prompt=True)
    # The release-prep row clears the keyword mark; the planner notes, which
    # are what the prompt names, do not. Before subject coverage this was confident.
    assert retrieval["arms"]["keyword"]["verdict"] == "confident"
    assert retrieval["confidence"] == "weak"
    assert retrieval["subject_coverage"] == "working_state_only"


# --- the background baseline -------------------------------------------------

@pytest.fixture()
def hybrid_notes(notes, monkeypatch):
    calls: list[str] = []

    def embed(text):
        calls.append(text)
        return [1.0] + [0.0] * (_DIM - 1)

    monkeypatch.setattr(embedder, "embed", embed)
    monkeypatch.setattr(config.search, "retrieval_mode", "hybrid")
    monkeypatch.setattr(config.search, "hybrid_enabled", True)
    vector_baseline._cache.clear()
    upsert_chunks([_chunk("background", "Unrelated background words.", cosine=0.7)],
                  skip_unchanged=False)
    yield calls
    vector_baseline._cache.clear()


def test_a_save_does_not_remeasure_the_baseline(hybrid_notes):
    """One new chunk leaves the store's background where it was: no re-embed."""
    calls = hybrid_notes
    assert vector_baseline.null_cosine() == pytest.approx(0.7, abs=1e-4)
    upsert_chunks([_chunk("closer", "Other unrelated words.", cosine=0.8)], skip_unchanged=False)
    assert vector_baseline.null_cosine() == pytest.approx(0.7, abs=1e-4)
    assert calls.count(vector_baseline.NULL_PROMPT) == 1


def test_the_baseline_is_remeasured_once_the_store_has_moved(hybrid_notes, monkeypatch):
    calls = hybrid_notes
    assert vector_baseline.null_cosine() == pytest.approx(0.7, abs=1e-4)
    monkeypatch.setattr(vector_baseline, "REFRESH_MIN_CHUNKS", 1)
    upsert_chunks([_chunk("closer", "Other unrelated words.", cosine=0.8)], skip_unchanged=False)
    assert vector_baseline.null_cosine() == pytest.approx(0.8, abs=1e-4)
    assert calls.count(vector_baseline.NULL_PROMPT) == 2
    assert vector_baseline.vector_confident_mark() == pytest.approx(
        0.8 + vector_baseline.VECTOR_NULL_MARGIN, abs=1e-4)


def test_the_baseline_is_remeasured_when_old(hybrid_notes, monkeypatch):
    calls = hybrid_notes
    clock = [1000.0]
    monkeypatch.setattr(vector_baseline.time, "monotonic", lambda: clock[0])
    vector_baseline.null_cosine()
    clock[0] += vector_baseline.MAX_AGE_S - 1
    vector_baseline.null_cosine()
    assert calls.count(vector_baseline.NULL_PROMPT) == 1
    clock[0] += 2
    vector_baseline.null_cosine()
    assert calls.count(vector_baseline.NULL_PROMPT) == 2


def test_another_model_measures_afresh(hybrid_notes, monkeypatch):
    calls = hybrid_notes
    vector_baseline.null_cosine()
    monkeypatch.setattr(config.embeddings.primary, "model", "another-model")
    vector_baseline.null_cosine()
    assert calls.count(vector_baseline.NULL_PROMPT) == 2


def test_no_embedding_endpoint_means_the_plain_mark(hybrid_notes, monkeypatch):
    monkeypatch.setattr(embedder, "embed", lambda _: (_ for _ in ()).throw(RuntimeError("down")))
    assert vector_baseline.vector_confident_mark() is None


def test_prompt_recall_judges_the_vector_arm_over_the_background(hybrid_notes):
    prompt = _verdict("how does the watcher debounce work", as_prompt=True)
    assert prompt["arms"]["vector"]["confident_at"] == pytest.approx(
        0.7 + vector_baseline.VECTOR_NULL_MARGIN, abs=1e-4)
    explicit = _verdict("how does the watcher debounce work", as_prompt=False)
    assert explicit["arms"]["vector"]["confident_at"] == confidence.VECTOR_CONFIDENT


# --- public responses --------------------------------------------------------

def test_search_responses_do_not_carry_the_internal_units(lexical_notes, monkeypatch):
    from palinode.api import server

    monkeypatch.setenv("PALINODE_API_ALLOW_UNAUTH", "1")
    server._rate_counters.clear()
    assert store.search_fts("watcher debounce", top_k=5)[0]["keyword_units"]
    with TestClient(server.app) as client:
        for body in ({"query": "watcher debounce"},
                     {"query": "watcher debounce", "receipt": True}):
            response = client.post("/search", json=body)
            assert response.status_code == 200, response.text
            data = response.json()
            results = data["results"] if isinstance(data, dict) else data
            assert results, data
            assert all("keyword_units" not in row for row in results)
            assert "keyword_units" not in response.text
