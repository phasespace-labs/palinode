"""Batch-embedding coverage for the real SQLite reconciliation seam."""
from __future__ import annotations

import pytest

from palinode.core import store
from palinode.core.config import config
from palinode.core.embedder import EmbeddingInputError, EmbeddingUnavailable
from palinode.indexer import reconcile

_VECTOR_A = [0.1] * 1024
_VECTOR_B = [0.2] * 1024


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    store.init_db()
    return tmp_path


def _two_section_plan(tmp_store, *, poison_second: bool = False):
    path = str(tmp_store / "projects" / "batch.md")
    (tmp_store / "projects").mkdir(exist_ok=True)
    second = "POISON " if poison_second else "beta "
    content = (
        "---\nid: batch-test\ncategory: projects\n---\n\n"
        f"## Alpha\n\n{'alpha ' * 500}\n\n"
        f"## Beta\n\n{second * 500}\n"
    )
    plan = reconcile.plan(reconcile.derive(path, content))
    assert len(plan.to_index) == 2
    return plan


def _row_counts() -> tuple[int, int]:
    db = store.get_db()
    try:
        chunks = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        vectors = db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0]
        return chunks, vectors
    finally:
        db.close()


def test_reconcile_embeds_all_sections_in_one_ordered_call(tmp_store):
    plan = _two_section_plan(tmp_store)

    class BatchEmbedder:
        def __init__(self):
            self.calls = []

        def embed_many(self, texts):
            self.calls.append(list(texts))
            return [_VECTOR_A, _VECTOR_B]

        def embed(self, text):  # pragma: no cover - batching must win
            raise AssertionError("scalar embed should not be called")

    embedder = BatchEmbedder()
    diff = reconcile.apply(plan, embedder)

    assert diff.committed and diff.written == 2
    assert embedder.calls == [[pw.section.content for pw in plan.to_index]]
    assert _row_counts() == (2, 2)


@pytest.mark.parametrize("batch", [
    [_VECTOR_A],
    [_VECTOR_A, []],
    [_VECTOR_A, _VECTOR_B, _VECTOR_A],
])
def test_reconcile_rejects_partial_or_malformed_batch_before_writes(
    tmp_store, batch
):
    plan = _two_section_plan(tmp_store)

    class InvalidBatchEmbedder:
        def embed_many(self, texts):
            return batch

    diff = reconcile.apply(plan, InvalidBatchEmbedder())

    assert not diff.committed
    assert diff.embed_failures == 1
    assert _row_counts() == (0, 0)


def test_reconcile_batch_outage_rolls_back_the_whole_file(tmp_store):
    plan = _two_section_plan(tmp_store)

    class OfflineEmbedder:
        def embed_many(self, texts):
            raise EmbeddingUnavailable(
                backend="local",
                model="bge-m3",
                text_len=sum(len(text) for text in texts),
                cause="offline",
            )

    diff = reconcile.apply(plan, OfflineEmbedder())

    assert not diff.committed
    assert diff.embed_failures == 1
    assert _row_counts() == (0, 0)


def test_batch_input_error_falls_back_to_isolate_poisoned_section(tmp_store):
    plan = _two_section_plan(tmp_store, poison_second=True)

    class SelectiveEmbedder:
        def __init__(self):
            self.batch_calls = 0
            self.scalar_calls = []

        def embed_many(self, texts):
            self.batch_calls += 1
            raise EmbeddingInputError(
                model="bge-m3",
                text_len=sum(len(text) for text in texts),
                ollama_message="unsupported value: NaN",
            )

        def embed(self, text):
            self.scalar_calls.append(text)
            if "POISON" in text:
                raise EmbeddingInputError(
                    model="bge-m3",
                    text_len=len(text),
                    ollama_message="unsupported value: NaN",
                )
            return _VECTOR_A

    embedder = SelectiveEmbedder()
    diff = reconcile.apply(plan, embedder)

    assert diff.committed and diff.written == 2
    assert not diff.vec_ok and diff.embed_failures == 1
    assert embedder.batch_calls == 1
    assert embedder.scalar_calls == [pw.section.content for pw in plan.to_index]
    assert _row_counts() == (2, 1)
