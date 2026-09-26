from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


_VECTOR_TABLES = ("chunks_vec", "triggers_vec")

_VECTOR_DIMENSIONS_RE = re.compile(
    r"\bembedding\s+FLOAT\s*\[\s*(\d+)\s*\]",
    re.IGNORECASE,
)


def _declared_vector_dimensions(
    con: sqlite3.Connection,
    table_name: str,
) -> int | None:
    """Return the embedding width declared by an existing sqlite-vec table."""
    row = con.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()

    if row is None:
        return None

    sql = row[0]
    if not sql:
        raise ValueError(
            f"Could not determine embedding dimensions for {table_name}"
        )

    match = _VECTOR_DIMENSIONS_RE.search(sql)
    if match is None:
        raise ValueError(
            f"Could not determine embedding dimensions for {table_name}"
        )

    return int(match.group(1))


@register(tags=("fast",))
def embedding_space_consistency(ctx: DoctorContext) -> CheckResult:
    """Compare recorded embedding provenance with the active configuration."""

    db_path = Path(ctx.config.db_path).expanduser().resolve()

    if not db_path.exists():
        return CheckResult(
            name="embedding_space_consistency",
            severity="info",
            passed=True,
            message="Database does not exist yet; no embedding space is recorded.",
            remediation=None,
        )

    try:
        uri = f"file:{db_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)

        try:
            table_exists = con.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'embedding_space'
                """
            ).fetchone()


            row = None

            if table_exists is not None:
                row = con.execute(
                    """
                    SELECT model, dimensions
                    FROM embedding_space
                    WHERE id = 1
                    """
                ).fetchone()

            if row is None:
                configured_dimensions = int(
                    ctx.config.embeddings.primary.dimensions
                )

                verified_vector_tables = []

                for vector_table in _VECTOR_TABLES:
                    declared_dimensions = _declared_vector_dimensions(
                        con,
                        vector_table,
                    )

                    if declared_dimensions is None:
                        continue

                    verified_vector_tables.append(vector_table)

                    if declared_dimensions != configured_dimensions:
                        return CheckResult(
                            name="embedding_space_consistency",
                            severity="error",
                            passed=False,
                            message=(
                                "Embedding space mismatch: "
                                f"existing {vector_table} declares "
                                f"dimensions={declared_dimensions}; "
                                f"configuration uses "
                                f"dimensions={configured_dimensions}."
                            ),
                            remediation=(
                                "Delete .palinode.db, then run `palinode reindex`. "
                                "Warning: deleting the database also removes DB-only state, "
                                "including registered triggers and recall reinforcement state "
                                "(importance, last_recalled, recall_count)."
                            ),
                        )

                if verified_vector_tables:
                    message = (
                        "Database has no recorded embedding space. Existing vector "
                        "dimensions match the active configuration, but the model used "
                        "to create existing vectors cannot be independently verified."
                    )
                    remediation = (
                        "Start Palinode once with the intended embedding configuration "
                        "to adopt the active model and verified dimensions for this legacy store."
                    )
                else:
                    message = (
                        "Database has no recorded embedding space; "
                        "there are no existing vector tables whose dimensions can be verified."
                    )
                    remediation = (
                        "Start Palinode once with the intended embedding configuration "
                        "to initialize embedding-space metadata."
                    )

                return CheckResult(
                    name="embedding_space_consistency",
                    severity="warn",
                    passed=False,
                    message=message,
                    remediation=remediation,
                )

        finally:
            con.close()

    except (sqlite3.Error, ValueError) as exc:
        return CheckResult(
            name="embedding_space_consistency",
            severity="error",
            passed=False,
            message=f"Could not inspect embedding space metadata: {exc}",
            remediation="Verify that db_path points to a valid Palinode database.",
        )

    recorded_model = row[0]
    recorded_dimensions = int(row[1])

    configured_model = ctx.config.embeddings.primary.model
    configured_dimensions = int(ctx.config.embeddings.primary.dimensions)

    if (
        recorded_model == configured_model
        and recorded_dimensions == configured_dimensions
    ):
        return CheckResult(
            name="embedding_space_consistency",
            severity="error",
            passed=True,
            message=(
                "Recorded embedding space matches configuration: "
                f"model={configured_model!r}, "
                f"dimensions={configured_dimensions}."
            ),
            remediation=None,
        )

    return CheckResult(
        name="embedding_space_consistency",
        severity="error",
        passed=False,
        message=(
            "Embedding space mismatch: "
            f"database uses model={recorded_model!r}, "
            f"dimensions={recorded_dimensions}; "
            f"configuration uses model={configured_model!r}, "
            f"dimensions={configured_dimensions}."
        ),
        remediation=(
            "Delete .palinode.db, then run `palinode reindex`. "
            "Warning: deleting the database also removes DB-only state, "
            "including registered triggers and recall reinforcement state "
            "(importance, last_recalled, recall_count)."
        ),
    )
