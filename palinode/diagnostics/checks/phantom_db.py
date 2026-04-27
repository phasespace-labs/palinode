"""
Check: phantom_db_files

The marquee stale-database check. Walks a list of plausible
roots looking for any .palinode.db files that are *not* the configured
db_path.  Filters by SQLite magic bytes.  Deduplicates by inode.  Opens each
candidate read-only to count chunks.

Severity: critical when >1 distinct DB file is found outside the configured
path (data-integrity risk — a stale process may still be writing to one of
them); info when exactly 1 file exists and it is the configured one.

Plausible roots (always searched):
  - config.memory_dir
  - ~ (home dir)
  - ~/palinode
  - ~/palinode-data
  - /var/lib/palinode
  - /home/example-user/palinode-data
  - /home/example-user/palinode
  - /home/example-user/old-data

Plus any paths from config.doctor.search_roots (YAML-configurable, ~ expanded).

 """
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

# Magic bytes that identify a valid SQLite 3 database file.
_SQLITE_MAGIC = b"SQLite format 3\x00"

# Built-in plausible roots.  Paths that do not exist on the current system
# are silently skipped during the walk.
_BUILTIN_ROOTS: list[str] = [
    "{memory_dir}",
    "{home}",
    "{home}/palinode",
    "{home}/palinode-data",
    "/var/lib/palinode",
    "/home/example-user/palinode-data",
    "/home/example-user/palinode",
    "/home/example-user/old-data",
]


def _is_sqlite(path: str) -> bool:
    """Return True if the first 16 bytes match the SQLite format 3 magic."""
    try:
        with open(path, "rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _safe_chunk_count(path: str) -> int:
    """Return the number of rows in the `chunks` table, or -1 on any error."""
    try:
        uri = f"file:{path}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            return con.execute("SELECT count(*) FROM chunks").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return -1


def _iso_mtime(mtime: float) -> str:
    return (
        datetime.fromtimestamp(mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


@register(tags=("deep",))
def phantom_db_files(ctx: DoctorContext) -> CheckResult:
    """Walk plausible roots for .palinode.db files outside the configured path."""
    home = os.path.expanduser("~")
    memory_dir = str(Path(ctx.config.memory_dir).expanduser().resolve())
    configured = Path(ctx.config.db_path).expanduser().resolve()

    # Build root list.
    # When search_roots is empty (default), use the built-in plausible roots.
    # When search_roots is non-empty (operator-specified or set in tests),
    # use ONLY those paths — built-ins are bypassed so operators and tests can
    # pin the exact set without discovering unrelated databases on the machine.
    if ctx.config.doctor.search_roots:
        roots: list[str] = [
            os.path.expanduser(r) for r in ctx.config.doctor.search_roots
        ]
    else:
        roots = [
            template.format(memory_dir=memory_dir, home=home)
            for template in _BUILTIN_ROOTS
        ]

    # Walk each root, collect all .palinode.db files, dedup by inode.
    seen_inodes: set[int] = set()
    all_found: list[dict] = []

    for root in roots:
        if not os.path.isdir(root):
            continue
        # Use os.walk instead of glob for finer control over errors.
        for dirpath, _dirs, filenames in os.walk(root, followlinks=False):
            for fname in filenames:
                if fname != ".palinode.db":
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_ino in seen_inodes:
                    continue
                seen_inodes.add(st.st_ino)
                if not _is_sqlite(full):
                    continue
                all_found.append(
                    {
                        "path": full,
                        "size_bytes": st.st_size,
                        "chunks": _safe_chunk_count(full),
                        "mtime": _iso_mtime(st.st_mtime),
                    }
                )

    # Separate extras (not the configured path) from the expected entry.
    extras = [
        f for f in all_found if Path(f["path"]).resolve() != configured
    ]

    if not extras:
        count_msg = (
            "1 DB file (configured path)"
            if all_found
            else "no .palinode.db files found"
        )
        return CheckResult(
            name="phantom_db_files",
            severity="info",
            passed=True,
            message=f"No phantom DB files found — {count_msg}",
            remediation=None,
        )

    # Build a human-readable list.
    lines = [
        f"  {e['path']} ({e['size_bytes']} bytes, {e['chunks']} chunks, mtime {e['mtime']})"
        for e in extras
    ]
    detail_block = "\n".join(lines)

    return CheckResult(
        name="phantom_db_files",
        severity="critical",
        passed=False,
        message=(
            f"{len(extras)} phantom .palinode.db file(s) found outside configured path "
            f"({configured}):\n{detail_block}"
        ),
        remediation=(
            "Stale DB files were found.  A stale palinode-watcher or palinode-api "
            "process may still be writing to one of them with old env vars.\n"
            "Steps:\n"
            "  1. Confirm the configured DB contains the data you expect:\n"
            f"       sqlite3 {configured} 'SELECT count(*) FROM chunks'\n"
            "  2. Restart all palinode services to ensure nothing is writing "
            "to the phantom DB.\n"
            "  3. Move (do NOT delete) each phantom to a .bak sibling:\n"
            + "\n".join(
                f"       mv {e['path']} {e['path']}.bak" for e in extras
            )
            + "\n"
            "See the related diagnostics above."
        ),
    )
