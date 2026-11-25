"""SQL against the derived index's tables lives in the store.

The recall path, the evidence layer and the inspector ask the index named
questions (``store.reader()``); none of them writes its own SQL against
``chunks``, ``chunks_vec`` or ``chunks_fts``, so a schema change is made in
one module. Exempt by design: the indexer's write path, which is already one
deep module inside ``store.transaction()``, and the health and diagnostics
checks, which inspect the schema itself.
"""
from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "palinode"

#: An SQL statement naming one of the index tables.
_SQL = re.compile(r"\b(FROM|JOIN|INTO|UPDATE)\s+chunks(_vec|_fts)?\b")

_EXEMPT = (
    "core/store.py",
    "indexer/reconcile.py",
    "api/routers/health.py",
    "diagnostics/",
)


def _offenders() -> list[str]:
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith(_EXEMPT):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _SQL.search(line):
                found.append(f"palinode/{rel}:{number}: {line.strip()}")
    return found


def test_no_module_outside_the_store_queries_the_index_tables():
    assert _offenders() == [], "add a named read to store.ChunkReader instead"


def test_the_guard_sees_a_query_when_there_is_one(tmp_path, monkeypatch):
    planted = tmp_path / "palinode" / "core"
    planted.mkdir(parents=True)
    (planted / "leak.py").write_text('db.execute("SELECT id FROM chunks_vec")\n')
    monkeypatch.setitem(globals(), "PACKAGE", tmp_path / "palinode")
    assert _offenders() == ['palinode/core/leak.py:1: db.execute("SELECT id FROM chunks_vec")']
