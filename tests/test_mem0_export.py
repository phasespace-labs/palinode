from __future__ import annotations

from palinode.migration import mem0_export


def test_collection_names_default_is_generic(monkeypatch) -> None:
    monkeypatch.delenv("PALINODE_MEM0_COLLECTIONS", raising=False)

    assert mem0_export._collection_names() == ["mem0_memories"]


def test_collection_names_parse_env_list(monkeypatch) -> None:
    monkeypatch.setenv(
        "PALINODE_MEM0_COLLECTIONS",
        "mem0_primary, mem0_archive, ,mem0_legacy",
    )

    assert mem0_export._collection_names() == [
        "mem0_primary",
        "mem0_archive",
        "mem0_legacy",
    ]
