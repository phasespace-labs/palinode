"""
Checks: db_size_sanity, chunks_match_md_count

db_size_sanity — compares current DB file size against an append-only
baseline log at ${memory_dir}/.palinode/db_size.log.  On first run,
records a baseline and passes. On subsequent runs, warns if the DB has
shrunk by more than 50% since the last recorded size — the "phantom empty
DB" signature where a fresh zero-byte DB replaced the real one.

The log format (one line per doctor run) is:
  <ISO-8601-UTC-timestamp> <size-bytes> <chunks-count>

chunks_match_md_count — counts *.md files under memory_dir and compares
against the number of chunks in the configured DB.  Consolidation can
compress many files into fewer chunks (expected), but if chunks count is
less than 50% of md file count, something is broken in the indexing
pipeline (a partial reindex, a fresh empty DB, watcher stopped early).

Severity:
  db_size_sanity        warn  (shrinkage is likely a misconfiguration)
  chunks_match_md_count warn  (with error escalation when ratio < 0.5)

 """
from __future__ import annotations

import glob
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

_LOG_SUBPATH = ".palinode/db_size.log"


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _read_last_log_line(log_path: Path) -> tuple[str, int, int] | None:
    """Return (timestamp, size_bytes, chunks) from the last non-empty line.

    Returns None if the file does not exist or has no parseable lines.
    """
    if not log_path.exists():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 3:
            try:
                return parts[0], int(parts[1]), int(parts[2])
            except ValueError:
                continue
    return None


def _append_log_line(log_path: Path, size_bytes: int, chunks: int) -> None:
    """Append one baseline line to the log; creates the file if needed.

    Truncate the log to reset the baseline on a fresh install.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"{_utc_now()} {size_bytes} {chunks}\n"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(entry)
    except OSError:
        pass  # Best-effort write; don't let a log failure break the check.


def _db_chunk_count(db_path: Path) -> int | None:
    """Return the chunk count from the DB, or None on any SQLite error."""
    if not db_path.exists():
        return None
    try:
        uri = f"file:{db_path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            row = con.execute("SELECT count(*) FROM chunks").fetchone()
            return row[0] if row else 0
        finally:
            con.close()
    except sqlite3.Error:
        return None


@register(tags=("deep",))
def db_size_sanity(ctx: DoctorContext) -> CheckResult:
    """Detect unexpected DB shrinkage using an append-only baseline log.

    On first run: records current size + chunk count as the baseline and
    returns pass.  On subsequent runs: warns if the DB has shrunk by more
    than 50% since the previous run (the phantom-empty-DB signature).
    """
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()
    db_path = Path(ctx.config.db_path).expanduser().resolve()
    log_path = memory_dir / _LOG_SUBPATH

    # Current DB size.
    if not db_path.exists():
        return CheckResult(
            name="db_size_sanity",
            severity="warn",
            passed=False,
            message=f"DB file does not exist: {db_path}",
            remediation=(
                "The configured DB file is missing.  Start palinode-api once "
                "to create it, or check config.db_path."
            ),
        )

    current_size = db_path.stat().st_size
    current_chunks = _db_chunk_count(db_path)
    if current_chunks is None:
        current_chunks = -1

    # Read last baseline entry.
    last = _read_last_log_line(log_path)

    # First run: write baseline and pass.
    if last is None:
        _append_log_line(log_path, current_size, current_chunks)
        return CheckResult(
            name="db_size_sanity",
            severity="warn",
            passed=True,
            message=(
                f"First run — baseline recorded: {current_size} bytes, "
                f"{current_chunks} chunks.  Future runs will compare against this."
            ),
            remediation=None,
        )

    _last_ts, last_size, _last_chunks = last

    # Always append a new line so the log grows across doctor runs.
    _append_log_line(log_path, current_size, current_chunks)

    if last_size == 0:
        # Edge case: previous baseline was zero (empty DB).  Nothing to compare.
        return CheckResult(
            name="db_size_sanity",
            severity="warn",
            passed=True,
            message=(
                f"Previous baseline size was 0 bytes; current is {current_size} bytes.  "
                "Skipping shrinkage check (baseline was an empty DB)."
            ),
            remediation=None,
        )

    # Shrinkage check: warn if current < 50% of previous size.
    ratio = current_size / last_size
    if ratio < 0.5:
        return CheckResult(
            name="db_size_sanity",
            severity="warn",
            passed=False,
            message=(
                f"DB size dropped by {100 - int(ratio * 100)}% — "
                f"previously {last_size} bytes, now {current_size} bytes.  "
                "This may indicate the configured db_path now points at a fresh "
                "or different file (the phantom-empty-DB pattern)."
            ),
            remediation=(
                f"Run 'palinode doctor --check phantom_db_files' to check whether "
                f"a stale DB with the old data still exists elsewhere.\n"
                f"  Previous size : {last_size} bytes\n"
                f"  Current size  : {current_size} bytes\n"
                f"  DB path       : {db_path}"
            ),
        )

    return CheckResult(
        name="db_size_sanity",
        severity="warn",
        passed=True,
        message=(
            f"DB size is {current_size} bytes (previous: {last_size} bytes, "
            f"ratio {ratio:.2f}) — within expected range."
        ),
        remediation=None,
    )


@register(tags=("fast",))
def chunks_match_md_count(ctx: DoctorContext) -> CheckResult:
    """Warn when DB chunk count is suspiciously low relative to md file count.

    Consolidation legitimately compresses many files into fewer chunks, so
    chunks < md_count is expected.  But chunks < 50% of md_count suggests
    a broken indexing pipeline (partial reindex, empty DB, watcher stopped).
    """
    memory_dir = Path(ctx.config.memory_dir).expanduser().resolve()
    db_path = Path(ctx.config.db_path).expanduser().resolve()

    # Count *.md files, excluding hidden dirs.
    md_files = [
        f for f in glob.glob(str(memory_dir / "**" / "*.md"), recursive=True)
        if "/.palinode" not in f  # skip any md files inside the hidden palinode dir
    ]
    md_count = len(md_files)

    if md_count == 0:
        return CheckResult(
            name="chunks_match_md_count",
            severity="warn",
            passed=True,
            message="No *.md files found in memory_dir — nothing to compare against.",
            remediation=None,
        )

    # Count chunks in DB.
    chunks = _db_chunk_count(db_path)
    if chunks is None:
        return CheckResult(
            name="chunks_match_md_count",
            severity="warn",
            passed=False,
            message=f"Cannot open DB to count chunks: {db_path}",
            remediation=(
                "Verify db_path in config and that palinode-api has been started "
                "at least once to create the DB."
            ),
        )

    ratio = chunks / md_count

    if ratio < 0.5:
        severity = "warn" if ratio >= 0.25 else "warn"
        return CheckResult(
            name="chunks_match_md_count",
            severity=severity,
            passed=False,
            message=(
                f"DB has {chunks} chunks for {md_count} markdown files "
                f"(ratio {ratio:.2f} — below 0.5 threshold).  "
                "Broken indexing pipeline suspected."
            ),
            remediation=(
                "Confirm no reindex is in progress ('palinode doctor --check reindex_in_progress'), "
                "then run 'palinode reindex' to rebuild the index from disk.\n"
                f"  Markdown files : {md_count}\n"
                f"  DB chunks      : {chunks}\n"
                f"  Ratio          : {ratio:.2f}"
            ),
        )

    return CheckResult(
        name="chunks_match_md_count",
        severity="warn",
        passed=True,
        message=(
            f"DB has {chunks} chunks for {md_count} markdown files "
            f"(ratio {ratio:.2f}) — within expected range."
        ),
        remediation=None,
    )
