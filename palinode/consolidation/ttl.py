"""TTL / auto-archive for ephemeral monitoring memories (ADR-015 §2.3, #482).

Deterministic monitor writes (and any caller) can mark a memory as ephemeral by
giving it an expiry:

- ``metadata.ttl`` — a duration (seconds, or ``<N>{s,m,h,d,w}``) resolved to an
  absolute ``expires_at`` at save time (see :func:`normalize_expiry`).
- ``expires_at`` — an explicit ISO-8601 timestamp in frontmatter.

The :func:`archive_expired` sweep flips every memory whose ``expires_at`` has
passed to ``status: archived``. Because #485 made ``status: archived`` content
recall-suppressed-but-retained (``config.search.exclude_status``), an expired
ephemeral memory ages out of default recall while staying on disk + in git for
audit — exactly ADR-015 §2.3's "down-weighted, then archived" end state.

The sweep is deterministic (no LLM), idempotent, and intended to be run from
cron / the monitor harness via ``palinode archive-expired`` (CLI), ``POST
/archive-expired`` (API), or the ``palinode_archive_expired`` MCP tool.

Setting the status in the file alone is not enough: ``index_file``'s fast path
keys on the *body* content-hash, so a frontmatter-only change would leave the
indexed chunk metadata stale and recall would not be suppressed. The sweep
therefore also calls :func:`palinode.core.store.set_status_for_path` to push the
new status into the chunk index directly — no body re-embed required.
"""
from __future__ import annotations

import glob
import logging
import os
import re
from datetime import UTC, datetime, timedelta

import frontmatter

from palinode.core import parser, store, git_tools
from palinode.core.config import config

logger = logging.getLogger("palinode.ttl")

# Top-level directories exempt from TTL sweeps: ``daily/`` notes are episodic
# session logs, and ``*-history.md`` files are already-archived snapshots
# (skipped anyway by the status check below).
_SKIP_TOP_DIRS = {"daily"}

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$")
_DURATION_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def parse_ttl(value: object) -> int | None:
    """Parse a TTL into a positive number of seconds, or ``None`` if invalid.

    Accepts an int/float (seconds) or a short duration string of the form
    ``<N><unit>`` where unit ∈ {s, m, h, d, w}. Zero/negative → ``None``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    s = str(value).strip().lower()
    if s.isdigit():
        n = int(s)
        return n if n > 0 else None
    m = _DURATION_RE.match(s)
    if not m:
        return None
    n = int(m.group(1))
    return n * _DURATION_MULT[m.group(2)] if n > 0 else None


def compute_expires_at(ttl: object, base: datetime | None = None) -> str | None:
    """Resolve a TTL to an absolute ISO-8601 ``expires_at``, or ``None``."""
    secs = parse_ttl(ttl)
    if secs is None:
        return None
    base = base or _utc_now()
    return (base + timedelta(seconds=secs)).isoformat()


def normalize_expiry(fm: dict, now_iso: str | None = None) -> str | None:
    """In-place resolve a frontmatter dict's expiry (ADR-015 §2.3).

    - A ``ttl`` (duration) is consumed and resolved to an absolute
      ``expires_at`` (``ttl`` is removed so ``expires_at`` is the single
      unambiguous source of truth). An explicit ``expires_at`` wins over ``ttl``.
    - An ``expires_at`` (supplied or computed) is validated as ISO-8601.

    Returns an error message if ``ttl``/``expires_at`` is malformed (the caller
    should reject the save with HTTP 400), else ``None``.
    """
    ttl = fm.pop("ttl", None)
    existing = fm.get("expires_at")
    if existing is None and ttl is not None:
        base = None
        if now_iso:
            try:
                base = datetime.fromisoformat(now_iso)
            except (ValueError, TypeError):
                base = None
        computed = compute_expires_at(ttl, base=base)
        if computed is None:
            return f"Invalid ttl {ttl!r}; expected seconds or <N>{{s,m,h,d,w}}"
        fm["expires_at"] = computed
        return None
    if existing is not None:
        try:
            datetime.fromisoformat(str(existing))
        except (ValueError, TypeError):
            return f"Invalid expires_at {existing!r}; expected an ISO-8601 timestamp"
    return None


def _coerce_aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def is_expired(meta: dict, now: datetime) -> bool:
    """True iff the memory carries an ``expires_at`` that is at or before ``now``."""
    raw = meta.get("expires_at")
    if not raw:
        return False
    try:
        exp = _coerce_aware(datetime.fromisoformat(str(raw)))
    except (ValueError, TypeError):
        logger.warning("Unparseable expires_at %r in memory frontmatter — skipping", raw)
        return False
    return exp <= now


def _iter_memory_files(root: str):
    for path in glob.glob(os.path.join(root, "**", "*.md"), recursive=True):
        rel = os.path.relpath(path, root)
        if rel.split(os.sep)[0] in _SKIP_TOP_DIRS:
            continue
        yield path


def _archive_file(path: str) -> None:
    """Set ``status: archived`` in a file's frontmatter, preserving the body."""
    with open(path, encoding="utf-8") as f:
        post = frontmatter.load(f)
    post["status"] = "archived"
    git_tools.write_memory_file(path, frontmatter.dumps(post) + "\n")


def archive_expired(now: datetime | None = None, dry_run: bool = False) -> dict:
    """Archive every memory whose ``expires_at`` has passed (ADR-015 §2.3, #482).

    Returns ``{"archived": [relpaths], "count": int, "dry_run": bool}``.
    Idempotent: a memory already at ``status: archived`` is skipped.
    """
    now = now or _utc_now()
    root = config.memory_dir
    archived: list[str] = []
    abs_paths: list[str] = []

    for path in _iter_memory_files(root):
        try:
            with open(path, encoding="utf-8") as f:
                meta, _ = parser.parse_markdown(f.read())
        except (OSError, ValueError):
            continue
        if meta.get("status") == "archived":
            continue
        if not is_expired(meta, now):
            continue
        archived.append(os.path.relpath(path, root))
        abs_paths.append(path)

    if not dry_run:
        for path in abs_paths:
            try:
                _archive_file(path)
                # Propagate to the chunk index directly: a frontmatter-only
                # change does not move the body content-hash, so index_file's
                # fast path would leave the stored status stale and recall
                # un-suppressed. set_status_for_path updates it without re-embed.
                store.set_status_for_path(path, "archived")
                # One archive = one per-file commit (#565): each expired memory
                # is its own mutation, committed via the git_tools choke point —
                # never a repo-wide sweep batching all expired files together.
                rel = os.path.relpath(path, root)
                git_tools.commit_memory_file(
                    path, f"{config.git.commit_prefix} ttl: auto-archive {rel}"
                )
            except OSError:
                logger.warning("Failed to auto-archive %s", path, exc_info=True)

    return {"archived": archived, "count": len(archived), "dry_run": dry_run}
