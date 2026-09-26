import pytest
import sqlite3
import logging

from palinode.core import store
from palinode.core.config import config


def test_rejects_same_dimensions_with_different_model(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / ".palinode.db"

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)

    monkeypatch.setattr(config.embeddings.primary, "model", "model-a")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 1024)

    # First startup creates the database with model-a / 1024.
    store.init_db()

    # Same dimensions, but a different embedding model.
    monkeypatch.setattr(config.embeddings.primary, "model", "model-b")

    with pytest.raises(RuntimeError, match="Embedding space"):
        store.init_db()


def test_rejects_different_embedding_dimensions(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / ".palinode.db"

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)

    monkeypatch.setattr(config.embeddings.primary, "model", "model-a")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 1024)

    store.init_db()

    monkeypatch.setattr(config.embeddings.primary, "dimensions", 768)

    with pytest.raises(RuntimeError, match="Embedding space"):
        store.init_db()


def test_records_embedding_space_on_first_init(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / ".palinode.db"

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)

    monkeypatch.setattr(config.embeddings.primary, "model", "model-a")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 1024)

    store.init_db()

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            """
            SELECT model, dimensions
            FROM embedding_space
            WHERE id = 1
            """
        ).fetchone()

    assert row == ("model-a", 1024)


def test_legacy_database_adopts_embedding_space_with_warning(
    tmp_path,
    monkeypatch,
    caplog,
):
    db_path = tmp_path / ".palinode.db"

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)

    monkeypatch.setattr(config.embeddings.primary, "model", "model-a")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 1024)

    # Create a normal database first so chunks_vec/triggers_vec exist.
    store.init_db()

    # Simulate a legacy database: vector indexes exist, but provenance does not.
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE embedding_space")

    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="palinode.store"):
        store.init_db()

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            """
            SELECT model, dimensions
            FROM embedding_space
            WHERE id = 1
            """
        ).fetchone()

    assert row == ("model-a", 1024)

    log_text = caplog.text.lower()
    assert "adopt" in log_text
    assert "cannot be independently verified" in log_text
    assert "verified dimensions" in log_text
    assert "model" in log_text

def test_embedding_space_mismatch_error_explains_recovery(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / ".palinode.db"

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)

    monkeypatch.setattr(config.embeddings.primary, "model", "model-a")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 1024)

    store.init_db()

    monkeypatch.setattr(config.embeddings.primary, "model", "model-b")

    with pytest.raises(RuntimeError) as exc_info:
        store.init_db()

    message = str(exc_info.value)

    assert "model-a" in message
    assert "model-b" in message
    assert "delete" in message.lower()
    assert ".palinode.db" in message
    assert "palinode reindex" in message
    assert "triggers" in message
    assert "importance" in message
    assert "last_recalled" in message
    assert "recall_count" in message

@pytest.mark.parametrize(
    ("vector_table", "other_vector_table"),
    [
        ("chunks_vec", "triggers_vec"),
        ("triggers_vec", "chunks_vec"),
    ],
)
def test_legacy_database_rejects_declared_dimension_mismatch(
    tmp_path,
    monkeypatch,
    vector_table,
    other_vector_table,
):
    db_path = tmp_path / ".palinode.db"

    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(db_path))
    monkeypatch.setattr(store, "_db_checked", False)

    # Build the original database with 4-dimensional vector tables.
    monkeypatch.setattr(config.embeddings.primary, "model", "model-a")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 4)

    store.init_db()

    # Simulate a legacy database with no provenance metadata.
    # Keep only the table under test so this case proves that table's
    # declared vector width is actually inspected.
    db = store.get_db()
    try:
        db.execute("DROP TABLE embedding_space")
        db.execute(f"DROP TABLE {other_vector_table}")
        db.commit()
    finally:
        db.close()

    # Configuration changed before upgrading Palinode.
    monkeypatch.setattr(config.embeddings.primary, "model", "other-model")
    monkeypatch.setattr(config.embeddings.primary, "dimensions", 8)

    with pytest.raises(RuntimeError, match="Embedding space mismatch") as exc_info:
        store.init_db()

    message = str(exc_info.value)

    assert vector_table in message
    assert "4" in message
    assert "8" in message


    with sqlite3.connect(db_path) as db:
        metadata_table = db.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'embedding_space'
            """
        ).fetchone()

    assert metadata_table is None
