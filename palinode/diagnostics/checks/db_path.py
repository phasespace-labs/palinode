"""
Checks: db_path_resolvable, db_path_under_memory_dir

db_path_resolvable — verifies that the configured db_path can actually be
opened by SQLite in read-only mode.  A non-openable path is a hard error;
the system will refuse to serve any search until it is fixed.

db_path_under_memory_dir — warns when config.db_path resolves to a location
outside config.memory_dir. This is the common drift pattern where the data
directory was renamed and PALINODE_DIR was updated, but db_path in
palinode.config.yaml was not, so the DB silently diverged from the store.

Severity:
  db_path_resolvable      error
  db_path_under_memory_dir  warn

 """
from __future__ import annotations

import sqlite3
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext


@register(tags=("fast",))
def db_path_resolvable(ctx: DoctorContext) -> CheckResult:
    """Verify that config.db_path is openable by SQLite (read-only)."""
    db_path = Path(ctx.config.db_path).expanduser().resolve()

    # The parent directory must exist before SQLite can do anything useful.
    if not db_path.parent.exists():
        return CheckResult(
            name="db_path_resolvable",
            severity="error",
            passed=False,
            message=(
                f"db_path parent directory does not exist: {db_path.parent} "
                f"(db_path={db_path})"
            ),
            remediation=(
                "Open palinode.config.yaml and set db_path to a path whose "
                "parent directory exists, then restart palinode-api.\n"
                f"  Configured db_path: {db_path}"
            ),
        )

    # Try a read-only SQLite connection.
    try:
        uri = f"file:{db_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            # Force SQLite to read the DB header. `SELECT 1` is a literal that
            # never touches the file; `PRAGMA schema_version` reads sqlite_master
            # and validates the magic bytes, which catches a non-SQLite file
            # masquerading as the DB. Full PRAGMA integrity_check is left for
            # a deeper check because it can be slow on large stores.
            con.execute("PRAGMA schema_version").fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return CheckResult(
            name="db_path_resolvable",
            severity="error",
            passed=False,
            message=f"Cannot open db_path read-only: {db_path} — {exc}",
            remediation=(
                "Open palinode.config.yaml and set db_path: <correct path>, "
                "then restart palinode-api.\n"
                f"  Configured db_path: {db_path}\n"
                "If the DB file does not exist yet, run 'palinode-api' once to "
                "create it, or copy an existing DB to this location."
            ),
        )

    return CheckResult(
        name="db_path_resolvable",
        severity="error",
        passed=True,
        message=f"db_path is openable: {db_path}",
        remediation=None,
    )


@register(tags=("fast",))
def db_path_under_memory_dir(ctx: DoctorContext) -> CheckResult:
    """Warn if config.db_path is outside config.memory_dir.

    This catches the divergence pattern where memory_dir was renamed/moved but
    db_path in palinode.config.yaml was not updated to match.
    """
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()
    db_path = Path(ctx.config.db_path).expanduser().resolve()

    try:
        db_path.relative_to(memory_dir)
        inside = True
    except ValueError:
        inside = False

    if inside:
        return CheckResult(
            name="db_path_under_memory_dir",
            severity="warn",
            passed=True,
            message=f"db_path is inside memory_dir: {db_path}",
            remediation=None,
        )

    return CheckResult(
        name="db_path_under_memory_dir",
        severity="warn",
        passed=False,
        message=(
            f"db_path is outside memory_dir — they may have diverged. "
            f"memory_dir={memory_dir}  db_path={db_path}"
        ),
        remediation=(
            "db_path and memory_dir have diverged, likely after a directory "
            "rename. "
            "Open palinode.config.yaml and set db_path to a path inside "
            f"memory_dir, then restart palinode-api.\n"
            f"  memory_dir : {memory_dir}\n"
            f"  db_path    : {db_path}\n"
            f"  Suggested  : {memory_dir / '.palinode.db'}"
        ),
    )
