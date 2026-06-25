"""
Check: recall_write_health

Verifies that the recall-feedback loop (ADR-006/007, #371) is actually
writing access metadata back to the ``chunks`` table on retrieval.

The failure mode this catches is the one the 2026-05-29 production audit
surfaced: ``.audit/retrievals.jsonl`` logged thousands of retrievals, but on
the ``chunks`` table ``MAX(recall_count)=0`` and ``last_recalled`` was
uniformly NULL — the retrieval hook fired but never wrote back. Every
recency/importance/decay policy downstream then ran on null data.

Signal: read ``MAX(recall_count)`` and the count of rows with a non-NULL
``last_recalled`` directly from ``chunks``.

  - No chunks indexed yet           → pass (nothing to recall).
  - Chunks exist, MAX(recall_count)=0
    and 0 rows ever recalled         → fail (loop is severed).
  - Otherwise                        → pass (metadata is being written).

Severity: warn
  Search still functions without recall metadata — only the decay ranker and
  importance signals are starved. ``palinode search`` is the recovery action
  (a single search stamps its hits), so this stops at "warn".

Tag: fast (one COUNT/MAX query, no network).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

_LINKED_ISSUE = "#371"


@register(tags=("fast",))
def recall_write_health(ctx: DoctorContext) -> CheckResult:
    """Detect a severed recall-feedback loop on the ``chunks`` table."""
    db_path = Path(ctx.config.db_path).expanduser().resolve()

    if not db_path.exists():
        return CheckResult(
            name="recall_write_health",
            severity="warn",
            passed=True,
            message="DB file does not exist — skipping recall-write health check.",
            remediation=None,
            linked_issue=_LINKED_ISSUE,
        )

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return CheckResult(
            name="recall_write_health",
            severity="warn",
            passed=False,
            message=f"Cannot open DB to check recall-write health: {exc}",
            remediation=(
                "Verify db_path in config and that palinode-api has been started "
                "at least once to create the DB."
            ),
            linked_issue=_LINKED_ISSUE,
        )

    try:
        row = con.execute(
            "SELECT count(*), "
            "COALESCE(MAX(recall_count), 0), "
            "count(last_recalled), "
            # ADR-007 §6.6: importance spread — are hot memories separating from
            # base (0.5), or is everything stuck at the neutral prior? A healthy
            # demand-decay system shows MAX(importance) > base for at least a few
            # chunks once explicit, cross-session demand has accumulated.
            "COALESCE(MAX(importance), 0), "
            "COALESCE(SUM(CASE WHEN importance > 0.5 THEN 1 ELSE 0 END), 0) "
            "FROM chunks"
        ).fetchone()
    except sqlite3.OperationalError:
        return CheckResult(
            name="recall_write_health",
            severity="warn",
            passed=True,
            message=(
                "``chunks`` table (or recall columns) not found — DB schema not "
                "yet initialised. Start palinode-api once to create the schema."
            ),
            remediation=None,
            linked_issue=_LINKED_ISSUE,
        )
    finally:
        con.close()

    total_chunks, max_recall, recalled_rows = row[0], row[1], row[2]
    max_importance, hot_rows = row[3], row[4]

    if total_chunks == 0:
        return CheckResult(
            name="recall_write_health",
            severity="warn",
            passed=True,
            message="DB has no chunks yet — nothing to recall.",
            remediation=None,
            linked_issue=_LINKED_ISSUE,
        )

    if max_recall == 0 and recalled_rows == 0:
        return CheckResult(
            name="recall_write_health",
            severity="warn",
            passed=False,
            message=(
                f"Recall-feedback loop appears severed: {total_chunks} chunks "
                "indexed but MAX(recall_count)=0 and 0 rows have ever been "
                "recalled. Access metadata is not being written on retrieval — "
                "decay/importance policies are running on null data."
            ),
            remediation=(
                "Run a search to confirm write-back, then re-check:\n"
                "  palinode search 'any topic you have memories about'\n"
                "  palinode doctor\n"
                "If recall_count is still 0 after a search that returned hits, "
                "the store write-back is broken (see #371)."
            ),
            linked_issue=_LINKED_ISSUE,
        )

    # ADR-007 §6.6 importance-spread signal. Recall metadata is being written;
    # additionally report whether importance is separating from base. Everything
    # stuck at base (max_importance <= base, hot_rows == 0) while recalls exist
    # means the demand-decay nudge isn't accumulating — expected briefly on a
    # fresh corpus, but a standing flat distribution after sustained explicit,
    # cross-session demand hints the explicit/session gate is mislabeled.
    if max_importance > 0.5 or hot_rows > 0:
        spread = (
            f" Importance spread: {hot_rows} chunk(s) above base, "
            f"MAX(importance)={max_importance:.3f} (hot memories separating from base)."
        )
    else:
        spread = (
            " Importance spread: all chunks at base (0.5) — no demand-decay "
            "separation yet. Normal on a fresh corpus; if it persists after "
            "sustained explicit cross-session recall, check that ambient/"
            "session-start recalls are labeled mode!=explicit (ADR-007 §3.2)."
        )

    return CheckResult(
        name="recall_write_health",
        severity="warn",
        passed=True,
        message=(
            f"Recall metadata is being written: {recalled_rows}/{total_chunks} "
            f"chunks recalled, MAX(recall_count)={max_recall}." + spread
        ),
        remediation=None,
        linked_issue=_LINKED_ISSUE,
    )
