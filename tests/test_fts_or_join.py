"""The implicit-AND finding: a question must reach the BM25 arm.

FTS5 joins bare terms with implicit AND, so ``search_fts("why does
consolidation skip groups")`` required all five words in one chunk and
returned nothing — for the dominant caller shape (an agent asking a question)
hybrid search was vector-only, and the arm's contribution to recall was
invisible. Measured on LongMemEval-V2 web: the empty arm cost 5.8 points on
exact-label questions against an OR-joined one.

``store.fts_match_expression`` now builds the MATCH expression: one quoted
unit per raw token (an identifier that sanitizes to several words becomes a
phrase), stopword units dropped, units OR-joined. BM25 scores a chunk higher
for each additional matched term, so short queries rank as before.

Real SQLite + tmp_path, no DB mocking (per CLAUDE.md).
"""
from __future__ import annotations

import pytest

from palinode.core import store
from palinode.core.config import config
from palinode.core.store import fts_match_expression
from tests._store_helpers import upsert_chunks


_FAKE_EMBEDDING = [0.01] * 1024


def _chunk(chunk_id: str, content: str) -> dict:
    return {
        "id": chunk_id, "file_path": f"insights/{chunk_id}.md", "section_id": "root",
        "category": "insights", "content": content, "metadata": {},
        "created_at": "2026-09-07T00:00:00+00:00", "last_updated": "2026-09-07T00:00:00+00:00",
        "embedding": _FAKE_EMBEDDING,
    }


class TestMatchExpression:
    @pytest.mark.parametrize("raw, expected", [
        # Stopwords dropped, content words OR-joined and quoted.
        ("Why does consolidation skip groups?", '"consolidation" OR "skip" OR "groups"'),
        ("the user's dog breed", '"user s" OR "dog" OR "breed"'),
        # An identifier that sanitizes to several words is one phrase, in order.
        ("CVE-2026-31889", '"CVE 2026 31889"'),
        ("what changed in v0.16.0", '"changed" OR "v0 16 0"'),
        ("palinode/core/store.py", '"palinode core store py"'),
        # A single word is a one-word phrase; case is left to the tokenizer.
        ("PLNC", '"PLNC"'),
        # Boolean operators are words, not operators, and are stopwords anyway.
        ("cats AND dogs OR birds", '"cats" OR "dogs" OR "birds"'),
    ])
    def test_shapes(self, raw: str, expected: str):
        assert fts_match_expression(raw) == expected

    def test_all_stopwords_keeps_every_unit(self):
        # Something rather than nothing: the caller asked, and BM25 ranking
        # still prefers the chunk carrying more of these words.
        assert fts_match_expression("what is it") == '"what" OR "is" OR "it"'

    @pytest.mark.parametrize("raw", ["", "   ", "???", "( ) *^"])
    def test_no_word_characters_is_the_empty_phrase(self, raw: str):
        assert fts_match_expression(raw) == '""'

    def test_stopword_check_is_case_insensitive(self):
        assert fts_match_expression("The Executor") == '"Executor"'


@pytest.fixture()
def store_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    upsert_chunks([
        _chunk("consol", "Consolidation skips a group when none of its facts changed since the last pass."),
        _chunk("cve", "Patched CVE-2026-31889 in the HTTP transport; the advisory landed on Tuesday."),
        _chunk("year", "The 2026 roadmap moves the signed-ledger milestone to a later release."),
        _chunk("dog", "The user's golden retriever is a friendly dog breed."),
    ], skip_unchanged=False)
    return tmp_path


class TestQuestionsReachTheArm:
    def test_question_matches_declarative_chunk(self, store_db):
        # Under implicit AND this needed "why", "does", "skip", "groups" all in
        # the chunk (it says "skips a group") and returned nothing.
        results = store.search_fts("Why does consolidation skip groups?")
        assert results and results[0]["id"] == "consol"

    def test_identifier_stays_an_exact_phrase(self, store_db):
        # "CVE 2026 31889" as a phrase: the chunk that merely mentions 2026
        # must not match on the shared token.
        results = store.search_fts("CVE-2026-31889")
        assert [r["id"] for r in results] == ["cve"]

    def test_more_matched_terms_rank_higher(self, store_db):
        # OR-join, but BM25 still prefers the chunk carrying more of the query.
        results = store.search_fts("user dog breed 2026")
        assert results[0]["id"] == "dog"
        assert "year" in {r["id"] for r in results}

    def test_previous_behaviour_preserved_for_plain_keywords(self, store_db):
        assert [r["id"] for r in store.search_fts("golden retriever")] == ["dog"]
        assert store.search_fts("???") == []

    def test_hybrid_fts_arm_contributes_for_a_question(self, store_db):
        # The fused result must carry the chunk the question is about even
        # though the fake embedding gives the vector arm nothing to prefer.
        merged = store.search_hybrid("why does consolidation skip groups?", _FAKE_EMBEDDING,
                                     top_k=2, threshold=0.0)
        assert any(r["id"] == "consol" for r in merged)
