"""The keyword arm's score is priced per query, so it means the same everywhere.

The measured defect: ``search_fts`` normalized BM25 as ``|bm25| / 25``, an
absolute constant against a quantity that grows with the corpus (IDF ≈ log
N/df) and with the number of query terms. So the same exact-identifier hit
scored 0.021 on a three-record store, 0.105 on a twenty-record one and 0.200
on a two-hundred-record one, while the marks read against it stayed put — a
small store read pessimistically on every query, and a single identifier token
could not reach the keyword arm's confident mark however exact it was.

``store.bm25_query_scale`` divides instead by what *this query* could score in
*this index*: the sum of its units' IDFs, which is the BM25 of a document
holding every query term once at average length. That makes 1.0 mean "carries
the whole query" on any store size, and — because every candidate of one query
is divided by the same positive number — leaves ordering, the ratio each
candidate carries against the best one, and therefore both relative floors
exactly where they were.

Real SQLite + FTS5 under ``tmp_path`` throughout; no DB mocking (per CLAUDE.md).
"""
from __future__ import annotations

import random

import pytest

from palinode.core import confidence, store
from palinode.core.config import config
from tests._store_helpers import upsert_chunks

_DIM = 1024
_TS = "2026-09-22T00:00:00+00:00"

#: The one record the identifier questions are about.
IDENTIFIER = "PLNC-4821"
IDENTIFIER_RECORD = (
    "Ledger rollout ticket PLNC-4821 closed the retention change for the audit trail."
)

#: Filler vocabulary: ordinary words, none of them the identifier. Sampled with
#: a fixed seed so a store of a given size is byte-identical run to run.
_VOCAB = """
standup timezone printer toner facilities portal procurement staging database nightly
contractor laptop wiped returned working days design review notes shared drive quarterly
planning fire drill attendance floor warden spring expense claims approver finance tool
meeting rooms booked fortnight ahead window throughput batch release milestone signed
transport advisory consolidation groups facts changed roadmap dump warehouse invoice
ledger rollout retention audit trail ticket closed change
""".split()


def _vec(axis: int = 0) -> list[float]:
    vector = [0.0] * _DIM
    vector[axis] = 1.0
    return vector


def _chunk(chunk_id: str, content: str, embedding: list[float] | None = None) -> dict:
    return {
        "id": chunk_id,
        "file_path": f"insights/{chunk_id}.md",
        "section_id": "root",
        "category": "insights",
        "content": content,
        "metadata": {},
        "created_at": _TS,
        "last_updated": _TS,
        "embedding": embedding if embedding is not None else _vec(1),
    }


def _build(n_records: int) -> None:
    """One identifier record plus ``n_records - 1`` filler records."""
    rng = random.Random(7)
    rows = [_chunk("ident", IDENTIFIER_RECORD)]
    for index in range(n_records - 1):
        body = " ".join(rng.sample(_VOCAB, 14)).capitalize() + "."
        rows.append(_chunk(f"filler-{index}", body))
    upsert_chunks(rows, skip_unchanged=False)


@pytest.fixture()
def store_at(tmp_path, monkeypatch):
    """Build a store of a requested size and hand back its query helpers."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.decay, "enabled", False)
    store.init_db()

    def build(n_records: int) -> None:
        _build(n_records)

    return build


def _raw_bm25(query: str) -> float:
    """FTS5's own ``bm25()`` for the best match, absolute and unnormalized.

    Read straight from the index rather than from a product function, so the
    "what the old constant was dividing" side of the comparison does not
    depend on the code under test.
    """
    db = store.get_db()
    try:
        row = db.execute(
            "SELECT rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 1",
            (store.fts_match_expression(query),),
        ).fetchone()
        return abs(float(row[0])) if row else 0.0
    finally:
        db.close()


class TestCorpusSizeInvariance:
    """The point of the rescale: the same hit reads the same on any store."""

    @pytest.mark.parametrize("n_records", [3, 20, 200])
    def test_an_exact_identifier_carries_the_whole_query(self, store_at, n_records):
        store_at(n_records)
        hits = store.search_fts(IDENTIFIER, top_k=5)
        assert [hit["id"] for hit in hits] == ["ident"]
        # 1.0 is the reference document — one holding every query term once at
        # average length. This record is shorter than average, so it reads a
        # little above it; the old scale read 0.021 / 0.105 / 0.200 here.
        assert hits[0]["score"] == pytest.approx(1.0, abs=0.1)
        assert hits[0]["score"] >= confidence.KEYWORD_CONFIDENT

    def test_the_score_is_flat_where_the_raw_bm25_grows_tenfold(self, store_at, tmp_path):
        """Both halves in one place: what moved, and what stopped moving."""
        scores: dict[int, float] = {}
        raws: dict[int, float] = {}
        for n_records in (3, 20, 200):
            # A fresh store per size, in its own directory under tmp_path.
            directory = tmp_path / f"store-{n_records}"
            directory.mkdir()
            config.memory_dir = str(directory)
            config.db_path = str(directory / ".palinode.db")
            store.init_db()
            _build(n_records)
            scores[n_records] = store.search_fts(IDENTIFIER, top_k=1)[0]["score"]
            raws[n_records] = _raw_bm25(IDENTIFIER)

        # What the constant 25 was dividing grows by more than 5x across the
        # same three stores — this is the drift the marks used to inherit.
        assert raws[200] / raws[3] > 5.0
        # What the arm now reports does not: within 5% end to end.
        spread = max(scores.values()) / min(scores.values())
        assert spread < 1.05, scores
        # And the verdict that reads it agrees on all three.
        for score in scores.values():
            verdict = confidence.assess(
                [{"raw_score": None, "keyword_score": score}], active_mode="lexical"
            )
            assert verdict["confidence"] == "confident"


class TestOrderingAndFloorsAreUntouched:
    """Dividing every candidate by one positive number changes no decision."""

    def test_a_multi_term_query_keeps_its_ordering_and_its_ratios(self, store_at):
        store_at(20)
        query = "ledger rollout ticket retention audit trail"
        hits = store.search_fts(query, top_k=10)
        assert len(hits) > 2, "the fixture must give the floors something to rank"
        scores = [hit["score"] for hit in hits]
        assert scores == sorted(scores, reverse=True)
        assert hits[0]["id"] == "ident"

        # The ratio each candidate carries against the best one — the only
        # thing `fts_threshold` and `lexical_fts_threshold` read — is the ratio
        # of the raw BM25 scores, unchanged by the division.
        db = store.get_db()
        try:
            raw = {
                row["id"]: abs(float(row["rank"]))
                for row in db.execute(
                    "SELECT c.id, rank FROM chunks_fts fts JOIN chunks c "
                    "ON c.rowid = fts.rowid WHERE chunks_fts MATCH ? ORDER BY rank",
                    (store.fts_match_expression(query),),
                ).fetchall()
            }
        finally:
            db.close()
        top_raw = max(raw.values())
        for hit in hits:
            assert hit["score"] / scores[0] == pytest.approx(
                raw[hit["id"]] / top_raw, rel=1e-9
            )

    def test_the_delivered_slate_is_what_the_old_constant_delivered(
        self, store_at, monkeypatch
    ):
        """End to end against the old scale, reproduced exactly.

        ``bm25_query_scale`` returning 25.0 *is* the old normalization, so a
        search through the real ranker — both relative floors, RRF fusion,
        per-file cap — can be run both ways and compared.
        """
        store_at(20)
        query = "ledger rollout ticket retention audit trail"
        new = store.search_hybrid(query, _vec(0), top_k=5, threshold=0.4,
                                  record_access=False)
        monkeypatch.setattr(store, "bm25_query_scale", lambda db, units: 25.0)
        old = store.search_hybrid(query, _vec(0), top_k=5, threshold=0.4,
                                  record_access=False)
        assert [hit["id"] for hit in new] == [hit["id"] for hit in old]
        assert [hit["score"] for hit in new] == [hit["score"] for hit in old]


class TestTheScaleItself:
    def test_the_match_expression_is_the_units_joined(self):
        query = "what changed in v0.16.0 for CVE-2026-31889"
        assert store.fts_match_expression(query) == " OR ".join(
            store.fts_match_units(query)
        )
        assert store.fts_match_units("???") == []

    def test_a_unit_the_store_has_never_seen_still_costs(self, store_at):
        """Priced at the rarest a term can be, so a query full of words the
        store does not know cannot be *easy* to cover — the shape of a
        question with no answer in the corpus."""
        store_at(20)
        db = store.get_db()
        try:
            known = store.bm25_query_scale(db, ['"ticket"'])
            with_unknown = store.bm25_query_scale(db, ['"ticket"', '"xylophonemissing"'])
        finally:
            db.close()
        assert with_unknown > known
        hits = store.search_fts("PLNC-4821 xylophonemissing flugelbinder", top_k=5)
        assert [hit["id"] for hit in hits] == ["ident"]
        assert hits[0]["score"] < 0.5, "a third of the query is all it carries"

    def test_nothing_to_price_is_zero_not_a_division(self, store_at):
        store_at(3)
        db = store.get_db()
        try:
            assert store.bm25_query_scale(db, []) == 0.0
        finally:
            db.close()

    def test_an_empty_index_prices_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "memory_dir", str(tmp_path))
        monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
        store.init_db()
        db = store.get_db()
        try:
            assert store.bm25_query_scale(db, ['"anything"']) == 0.0
        finally:
            db.close()
        assert store.search_fts("anything") == []

    def test_a_denser_chunk_may_pass_the_reference(self, store_at):
        """1.0 is a reference, not a ceiling: a chunk repeating the term in
        half the words of the reference carries more of it than the reference
        document does. Capping here would compress two candidates into one
        value and hand the relative floors a ratio that was never measured."""
        store_at(20)
        upsert_chunks([_chunk("dense", "PLNC-4821 PLNC-4821 PLNC-4821")],
                      skip_unchanged=False)
        hits = store.search_fts(IDENTIFIER, top_k=5)
        assert hits[0]["id"] == "dense"
        assert hits[0]["score"] > 1.0
