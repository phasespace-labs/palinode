import sqlite3
from pathlib import Path
import pytest

from palinode.core import store
from palinode.core.config import Config, config
from palinode.diagnostics.checks.embedding_space import embedding_space_consistency
from palinode.diagnostics.types import DoctorContext


def _ctx(
    tmp_path: Path,
    *,
    model: str = "model-a",
    dimensions: int = 1024,
) -> DoctorContext:
    db_path = tmp_path / ".palinode.db"

    cfg = Config(
        memory_dir=str(tmp_path),
        db_path=str(db_path),
    )
    cfg.embeddings.primary.model = model
    cfg.embeddings.primary.dimensions = dimensions

    return DoctorContext(config=cfg)


def _write_embedding_space(
    db_path: Path,
    *,
    model: str,
    dimensions: int,
) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE embedding_space (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO embedding_space (id, model, dimensions)
            VALUES (1, ?, ?)
            """,
            (model, dimensions),
        )


def test_doctor_passes_when_embedding_space_matches(tmp_path):
    ctx = _ctx(tmp_path, model="model-a", dimensions=1024)

    _write_embedding_space(
        Path(ctx.config.db_path),
        model="model-a",
        dimensions=1024,
    )

    result = embedding_space_consistency(ctx)

    assert result.passed is True
    assert "matches" in result.message.lower()
    assert result.remediation is None


def test_doctor_fails_when_embedding_space_mismatches(tmp_path):
    ctx = _ctx(tmp_path, model="model-b", dimensions=1024)

    _write_embedding_space(
        Path(ctx.config.db_path),
        model="model-a",
        dimensions=1024,
    )

    result = embedding_space_consistency(ctx)

    assert result.passed is False
    assert result.severity == "error"
    assert "mismatch" in result.message.lower()
    assert "model-a" in result.message
    assert "model-b" in result.message
    assert ".palinode.db" in result.remediation
    assert "palinode reindex" in result.remediation


def test_doctor_warns_for_legacy_database_without_provenance(tmp_path):
    ctx = _ctx(tmp_path)

    # Valid existing SQLite DB, but no embedding_space table.
    with sqlite3.connect(ctx.config.db_path) as db:
        db.execute("CREATE TABLE chunks (id TEXT PRIMARY KEY)")

    result = embedding_space_consistency(ctx)

    assert result.passed is False
    assert result.severity == "warn"
    assert "no recorded embedding space" in result.message.lower()
    assert "no existing vector tables" in result.message.lower()
    assert "dimensions can be verified" in result.message.lower()
    assert "initialize embedding-space metadata" in result.remediation.lower()


@pytest.mark.parametrize(
    ("vector_table", "other_vector_table"),
    [
        ("chunks_vec", "triggers_vec"),
        ("triggers_vec", "chunks_vec"),
    ],
)
def test_doctor_rejects_legacy_declared_dimension_mismatch(
    tmp_path,
    monkeypatch,
    vector_table,
    other_vector_table,
):
    db_path = tmp_path / ".palinode.db"

    # Build a real database whose vector tables are FLOAT[4].
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)
    monkeypatch.setattr(config.embeddings.primary, "model", "model-a")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 4)

    store.init_db()

    # Simulate a legacy database and isolate the vector table under test.
    db = store.get_db()
    try:
        db.execute("DROP TABLE embedding_space")
        db.execute(f"DROP TABLE {other_vector_table}")
        db.commit()
    finally:
        db.close()

    # Doctor is now run with a different configured width.
    ctx = _ctx(
        tmp_path,
        model="other-model",
        dimensions=8,
    )

    result = embedding_space_consistency(ctx)

    assert result.passed is False
    assert result.severity == "error"
    assert "mismatch" in result.message.lower()
    assert vector_table in result.message
    assert "4" in result.message
    assert "8" in result.message
    assert "palinode reindex" in result.remediation
    assert "start palinode once" not in result.remediation.lower()


@pytest.mark.parametrize(
    ("vector_table", "other_vector_table"),
    [
        ("chunks_vec", "triggers_vec"),
        ("triggers_vec", "chunks_vec"),
    ],
)
def test_doctor_allows_legacy_adoption_when_dimensions_match(
    tmp_path,
    monkeypatch,
    vector_table,
    other_vector_table,
):
    db_path = tmp_path / ".palinode.db"

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)
    monkeypatch.setattr(config.embeddings.primary, "model", "model-a")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 4)

    store.init_db()

    # Simulate a legacy database and isolate the table under test.
    db = store.get_db()
    try:
        db.execute("DROP TABLE embedding_space")
        db.execute(f"DROP TABLE {other_vector_table}")
        db.commit()
    finally:
        db.close()

    # The model may be different because a legacy DB cannot prove which
    # model created its vectors, but the declared width can be verified.
    ctx = _ctx(
        tmp_path,
        model="other-model",
        dimensions=4,
    )

    result = embedding_space_consistency(ctx)

    assert result.passed is False
    assert result.severity == "warn"
    assert "dimensions match" in result.message.lower()
    assert "model" in result.message.lower()
    assert "cannot be independently verified" in result.message.lower()
    assert "start palinode once" in result.remediation.lower()


def test_doctor_checks_dimensions_when_metadata_table_is_empty(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / ".palinode.db"

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)
    monkeypatch.setattr(config.embeddings.primary, "model", "model-a")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 4)

    store.init_db()

    # Keep the metadata table, but remove its provenance row.
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM embedding_space")

    ctx = _ctx(
        tmp_path,
        model="other-model",
        dimensions=8,
    )

    result = embedding_space_consistency(ctx)

    assert result.passed is False
    assert result.severity == "error"
    assert "mismatch" in result.message.lower()
    assert "4" in result.message
    assert "8" in result.message
    assert "palinode reindex" in result.remediation
