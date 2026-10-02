"""
Check: projection_current

Reports how many indexed chunks were derived under the current text
projection (:mod:`palinode.core.projection`) and how many are still on an
older version or none at all.

The indexer projects the consolidation executor's retirement tombstones
(``~~old~~ [superseded …]`` / ``[RETRACTED …]``) and the generated
``## See also`` footer out of the text it hands to FTS and the embedder, and
stamps every derived row with the ``PROJECTION_VERSION`` it used.  A row
without that stamp — a store indexed before the projection existed, or one
indexed under earlier rules — still carries the retired wording, or the
footer's wikilink slugs, in its keyword and vector index: an old assertion can
rank beside its successor, and a footer-only chunk can rank for a question its
record does not answer.  Reconcile re-derives such rows as it visits their
files (the watcher, a save, or ``palinode reindex``), so the count here is
migration progress: it falls to zero as the store converges.

Severity: warn
  Read-only, local, no network.  Passes when every chunk is on the current
  version (or the store is empty); warns with the counts otherwise.  A
  missing column means the schema itself predates the projection — every
  chunk is behind and the API has not been started since the upgrade.

Recovery hint: ``palinode reindex`` finishes the migration in one pass.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from palinode.core.projection import PROJECTION_VERSION
from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


def _counts(con: sqlite3.Connection) -> tuple[int, int] | None:
    """``(total, current)`` chunk counts, or ``None`` when ``chunks`` is absent.

    A ``chunks`` table without the ``projection_version`` column reports every
    row as behind: the column is added by ``store.init_db`` on the first API
    start after the upgrade, and until then nothing has been projected.
    """
    try:
        total = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    try:
        current = con.execute(
            "SELECT count(*) FROM chunks WHERE projection_version = ?",
            (PROJECTION_VERSION,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        current = 0
    return total, current


@register(tags=("fast",))
def projection_current(ctx: DoctorContext) -> CheckResult:
    """Count indexed chunks behind the current text projection version."""
    db_path = Path(ctx.config.db_path).expanduser().resolve()

    if not db_path.exists():
        return CheckResult(
            name="projection_current",
            severity="warn",
            passed=True,
            message="DB file does not exist — skipping projection version check.",
            remediation=None,
        )

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return CheckResult(
            name="projection_current",
            severity="warn",
            passed=False,
            message=f"Cannot open DB to check projection version: {exc}",
            remediation=(
                "Verify db_path in config and that palinode-api has been started "
                "at least once to create the DB."
            ),
        )

    try:
        counts = _counts(con)
    finally:
        con.close()

    if counts is None:
        return CheckResult(
            name="projection_current",
            severity="warn",
            passed=True,
            message=(
                "``chunks`` table not found — DB schema not yet initialised.  "
                "Start palinode-api once to create the schema."
            ),
            remediation=None,
        )

    total, current = counts
    behind = total - current

    if total == 0:
        return CheckResult(
            name="projection_current",
            severity="warn",
            passed=True,
            message="DB is empty — projection version check skipped (no chunks indexed yet).",
            remediation=None,
        )

    if behind == 0:
        return CheckResult(
            name="projection_current",
            severity="warn",
            passed=True,
            message=(
                f"All {total} indexed chunks are on text projection "
                f"v{PROJECTION_VERSION}."
            ),
            remediation=None,
        )

    return CheckResult(
        name="projection_current",
        severity="warn",
        passed=False,
        message=(
            f"{behind} of {total} indexed chunks are on an older text projection "
            f"than v{PROJECTION_VERSION} (or none).  Retired facts and generated "
            "'## See also' footers in those chunks can still rank in keyword and "
            "vector search.  Reconcile converges them as their files are visited; "
            "the count is migration progress."
        ),
        remediation=(
            "Run 'palinode reindex' to re-derive the remaining chunks in one pass.\n"
            f"  chunks total   : {total}\n"
            f"  on v{PROJECTION_VERSION}          : {current}\n"
            f"  behind         : {behind}\n"
            f"  DB path        : {db_path}"
        ),
    )
