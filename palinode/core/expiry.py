"""Expiry gate for the two state types that *act* — triggers and ``core: true``.

Most memory is merely recalled; two kinds of record act on their own:
``core: true`` memories are injected into every session start, and prospective
triggers fire on a future prompt. Both act under whatever authority existed when
they were written. This module is the single place that asks, at the moment of
acting, whether that grant is still current — the *authority monotonicity*
invariant: a record may influence an action only under a current, unrevoked
authority.

Two frontmatter / column fields carry it:

- ``expires_at`` — ISO-8601 timestamp after which the record no longer acts.
  The same clock as ADR-015 §2.3's TTL sweep (``palinode archive-expired``);
  this gate consults it at act time so a record that expired between sweeps
  is still refused. An expired record remains on disk, in git, and searchable
  — it just stops acting.
- ``authority`` — free text naming who or what licensed the record to act: a
  user grant (``"paul: standing"``), a session id, a policy name. Stored and
  displayed only; nothing enforces it yet. Recorded in plaintext so the signed
  envelope (ADR-016) has something to sign later.

An expired record is reported **once per process** per ``(kind, key,
expires_at)`` — not once per prompt — so a long-running API server does not
log the same stale trigger on every ``/check-triggers`` call, but a record
re-armed with a new ``expires_at`` is reported afresh when that one lapses.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("palinode.expiry")

#: ``(kind, key, expires_at)`` triples already reported this process.
_REPORTED: set[tuple[str, str, str]] = set()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def parse_expires_at(raw: Any) -> datetime | None:
    """Parse an ``expires_at`` value to an aware UTC datetime, or ``None``.

    Accepts an ISO-8601 string or a ``datetime`` (YAML auto-converts unquoted
    timestamps). A naive value is taken as UTC. Malformed → ``None``.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def is_past(raw: Any, now: datetime | None = None) -> bool:
    """True iff ``raw`` is a parseable ``expires_at`` at or before ``now``.

    A missing or unparseable value is *not* past — the record keeps acting —
    so a typo in the field can never silently disarm a record; it is surfaced
    by ``palinode lint`` and the TTL sweep's warning instead.
    """
    exp = parse_expires_at(raw)
    if exp is None:
        return False
    return exp <= (now or _utc_now())


def report_expired_once(kind: str, key: str, expires_at: Any) -> bool:
    """Log that ``kind``/``key`` has expired, the first time only.

    Returns ``True`` when this call did the logging, ``False`` when the same
    ``(kind, key, expires_at)`` was already reported this process.
    """
    triple = (kind, key, str(expires_at))
    if triple in _REPORTED:
        return False
    _REPORTED.add(triple)
    logger.warning(
        "%s %s expired at %s — no longer acting (still stored and searchable)",
        kind, key, expires_at,
    )
    return True


def core_has_expired(file: str, meta: dict[str, Any], now: datetime | None = None) -> bool:
    """Has this ``core: true`` memory lapsed past its ``expires_at``?

    Callers decide *whether* a memory is core (each surface keeps its own
    test); this decides whether it may still act, and reports a lapsed one
    once. It is the one choke point every core-injection surface goes
    through — ``GET /list?core_only=true`` behind the session-start hook and
    the harness plugins, ``/context/prime`` behind ``palinode_session_init``
    and ``palinode prime`` — so an expired core memory is withheld everywhere
    or nowhere.
    """
    if not is_past(meta.get("expires_at"), now):
        return False
    report_expired_once("core memory", file, meta.get("expires_at"))
    return True
