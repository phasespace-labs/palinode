"""
Check: fts5_sync

Detects drift between the ``chunks`` source table and the ``chunks_fts``
external-content FTS5 virtual table.

``chunks_fts`` is an *external content* table — SQLite does not
automatically keep it in sync with ``chunks``.  Every write path in
``store.upsert_chunks`` manually updates both tables, but a mid-write
exception, a schema migration, or a crashed bulk-index run can leave FTS5
with fewer rows than ``chunks``, causing keyword search to silently miss
content that exists in the vector index.

Approach: row-count comparison.
  - Open the DB read-only (no sqlite-vec extension needed for a COUNT query).
  - Count rows in ``chunks`` and in ``chunks_fts``.
  - Any divergence fails the check.

Severity: warn
  Drift is recoverable by running ``palinode reindex``, so we stop at
  "warn" rather than "error".  The check cannot tell *which* chunks are
  missing — ``palinode reindex`` is the authoritative fix regardless.

Recovery hint: ``palinode reindex``
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


def _count_rows(con: sqlite3.Connection, table: str) -> int | None:
    """Return row count for *table*, or None if the table does not exist."""
    try:
        row = con.execute(f"SELECT count(*) FROM {table}").fetchone()  # nosec B608
        return row[0] if row else 0
    except sqlite3.OperationalError:
        # Table does not exist (schema not yet initialised, or virtual table missing).
        return None


def _fts5_indexed_count(con: sqlite3.Connection) -> int | None:
    """Return the number of documents actually present in the FTS5 index.

    ``COUNT(*) FROM chunks_fts`` for an external-content table proxies back
    through the source table (``chunks``) — it does not reflect the actual
    FTS shadow state.  The ``chunks_fts_docsize`` shadow table has one row
    per indexed document and shrinks on deletion, making it the correct
    signal for sync checking.

    Returns None when the FTS5 virtual table (and thus its shadow tables)
    does not exist.
    """
    return _count_rows(con, "chunks_fts_docsize")


@register(tags=("fast",))
def fts5_sync(ctx: DoctorContext) -> CheckResult:
    """Detect row-count drift between ``chunks`` and ``chunks_fts``.

    Compares the number of rows in the ``chunks`` source table against
    the ``chunks_fts`` FTS5 virtual table.  Any divergence means keyword
    search (BM25 half of hybrid search) is returning incomplete results.
    """
    db_path = Path(ctx.config.db_path).expanduser().resolve()

    if not db_path.exists():
        return CheckResult(
            name="fts5_sync",
            severity="warn",
            passed=True,
            message="DB file does not exist — skipping FTS5 sync check.",
            remediation=None,
        )

    try:
        uri = f"file:{db_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return CheckResult(
            name="fts5_sync",
            severity="warn",
            passed=False,
            message=f"Cannot open DB to check FTS5 sync: {exc}",
            remediation=(
                "Verify db_path in config and that palinode-api has been started "
                "at least once to create the DB."
            ),
        )

    try:
        chunks_count = _count_rows(con, "chunks")
        fts_count = _fts5_indexed_count(con)
    finally:
        con.close()

    # chunks table missing — DB not yet initialised.
    if chunks_count is None:
        return CheckResult(
            name="fts5_sync",
            severity="warn",
            passed=True,
            message=(
                "``chunks`` table not found — DB schema not yet initialised.  "
                "Start palinode-api once to create the schema."
            ),
            remediation=None,
        )

    # FTS5 shadow tables missing — virtual table was never created or schema
    # migration failed.
    if fts_count is None:
        return CheckResult(
            name="fts5_sync",
            severity="warn",
            passed=False,
            message=(
                "``chunks_fts`` FTS5 index does not exist.  "
                f"DB has {chunks_count} chunks but FTS5 index is absent — "
                "keyword search is fully broken."
            ),
            remediation=(
                "Run 'palinode reindex' to rebuild the FTS5 index from scratch.\n"
                f"  chunks rows : {chunks_count}\n"
                f"  chunks_fts  : (table missing)\n"
                f"  DB path     : {db_path}"
            ),
        )

    # Both empty — fresh install.  Not an error.
    if chunks_count == 0 and fts_count == 0:
        return CheckResult(
            name="fts5_sync",
            severity="warn",
            passed=True,
            message="DB is empty — FTS5 sync check skipped (no chunks indexed yet).",
            remediation=None,
        )

    if chunks_count == fts_count:
        return CheckResult(
            name="fts5_sync",
            severity="warn",
            passed=True,
            message=(
                f"FTS5 index is in sync: {chunks_count} rows in ``chunks``, "
                f"{fts_count} indexed in FTS5."
            ),
            remediation=None,
        )

    drift = chunks_count - fts_count
    return CheckResult(
        name="fts5_sync",
        severity="warn",
        passed=False,
        message=(
            f"FTS5 index is out of sync: ``chunks`` has {chunks_count} rows "
            f"but ``chunks_fts`` has {fts_count} rows "
            f"(drift of {drift:+d}).  "
            "Keyword search is silently missing content."
        ),
        remediation=(
            "Run 'palinode reindex' to rebuild the FTS5 index from scratch.\n"
            f"  chunks rows : {chunks_count}\n"
            f"  chunks_fts  : {fts_count}\n"
            f"  drift       : {drift:+d}\n"
            f"  DB path     : {db_path}"
        ),
    )
