"""Lifecycle eligibility — one answer to "may this record be presented as current?"

Recall (the session-start digest) and consolidation input (the compaction
prompt's ``ACTIVE_DECISIONS`` and ``EXISTING_FACTS``) each used to decide on
their own whether a record was still in force, and they disagreed: the digest
read ``status`` only for action items, so an archived-in-place decision under
``decisions/`` surfaced as a recent decision; the runner withheld only
``status: superseded``, so the same archived decision reached the model as a
governing constraint. This module is the single classifier both consult.

It is pure: frontmatter (plus the record's path and an injected clock) in, a
small typed :class:`Eligibility` out. No filesystem, database, or network —
the one lookup it makes is ``config.memory_dir``, to read a path as
memory-relative, which is string arithmetic and not a disk touch. The same
discipline as :mod:`palinode.consolidation.retirement`, which decides
*what a document is* (may age retire it?), where this decides *whether it is
still current*. The two are orthogonal and neither reads the other.

Vocabulary is the existing one, nothing new: ``status`` / ``lifecycle`` from
:data:`palinode.core.parser.VALID_LIFECYCLES`, ``status: superseded`` as the
legacy value the runner already withheld, ``superseded_by`` as the archive
op's replacement pointer, and ``expires_at`` on the same clock as the TTL
sweep and the acting-state gate in :mod:`palinode.core.expiry`.

Three states:

``current``
    A lifecycle state was declared and it is a live one (``status: active``
    or an incident status). Usable, governing.
``retired``
    Archived, deprecated, superseded, retracted, past ``expires_at``, or
    retired *by location*: a note under ``archive/`` keeps whatever
    frontmatter it had when the weekly pass moved it there, so its path is
    the only record that it was retired at all. Never presented as current —
    but never deleted either: the file, its version history and its
    ``-history.md`` sibling stay on disk and searchable on demand, which is
    why this module reports and does not act.
``unmarked``
    No lifecycle state declared. Every memory written before ``status``
    existed is here. It stays usable **and stays unmarked** — the absence of
    a declaration is not silently promoted to "active", exactly as an absent
    ``epistemic`` is not promoted to ``fact``.

The qualifiers a usable record carries — ``contradicts``, ``stale_backing``,
``epistemic``, ``expires_at`` — ride along so a surface that selects through
this module cannot select the record and lose its qualification on the way
to the reader. Each surface keeps its own formatting (ADR-010: shared
semantics, not a shared renderer).

Ordering fallback (for "most recent" sections): a file's mtime is **not** a
new effective decision. A record's effective moment is its declared ``date``,
else ``last_updated``, else ``created_at`` (the stamps the save path writes).
A record with none of these is *undated* and ranks after every dated record;
among undated records the caller may break ties by mtime, which orders them
deterministically without asserting a date for any of them.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from palinode.core.expiry import is_past, parse_expires_at

State = Literal["current", "retired", "unmarked"]

#: ``status`` / ``lifecycle`` values that retire a record. ``archived`` and
#: ``deprecated`` are the two terminal members of ``VALID_LIFECYCLES``;
#: ``superseded`` is the legacy value the consolidation runner has always
#: withheld from ACTIVE_DECISIONS (the archive op writes ``archived`` +
#: ``superseded_by`` instead, but a hand-written one must still count);
#: ``retracted`` is the KU lifecycle's known-incorrect state.
RETIRED_STATUSES: frozenset[str] = frozenset(
    {"archived", "deprecated", "superseded", "retracted"}
)

#: The directory segment that retires a record *by location*. The weekly
#: pass (``runner._archive_daily_notes``) and ``archive_memory`` move a note
#: under ``archive/`` and leave its bytes alone, so nothing in its
#: frontmatter says retired; the move itself is the statement. Search still
#: indexes ``archive/`` on purpose — history stays findable — which is how an
#: archived daily note reached a ``resolve`` answer as a current assertion.
ARCHIVE_SEGMENT = "archive"

#: Frontmatter fields consulted, in order, for a record's effective moment.
#: ``date`` is the only *declared* date in the current vocabulary (there is no
#: dedicated effective-date field yet); the other two are the save path's
#: stamps. See the module docstring for why mtime is not on this list.
EFFECTIVE_DATE_FIELDS: tuple[str, ...] = ("date", "last_updated", "created_at")

#: A fact bullet the executor (or the mention-level retract) has retired in
#: place: the whole text struck through, followed by Palinode's own retirement
#: marker. Anchored at the start and requiring the marker, so user-authored
#: strikethrough inside a sentence, or a mid-bullet mention strike, is not a
#: retired fact. Mirrors the renderings in ``executor._supersede_fact`` /
#: ``_retract_fact`` and ``retract.py``; ``tests/test_lifecycle_eligibility.py``
#: pins the executor's actual output against this pattern so the writer and
#: this reader cannot drift.
RETIRED_FACT_RE = re.compile(
    r"^~~.*~~\s*\[(?:superseded|RETRACTED)\s+\d{4}-\d{2}-\d{2}\b", re.DOTALL
)

#: One struck *mention* inside otherwise-current text — the span the
#: mention-level retract (``consolidation.retract``) writes for a sentence or
#: list item that carries a forgotten preference: ``~~text~~ [RETRACTED
#: YYYY-MM-DD r:<8-hex>].``, terminator-final and opaque (the marker never
#: repeats the retracted words). Unlike :data:`RETIRED_FACT_RE` this is not
#: anchored: the span can sit mid-paragraph with current sentences either
#: side, and it is only ever the span — never the line — that is retired.
#: Mirrors ``retract._MARKER_RE`` / ``retract._unstrike_re``; the tests pin
#: the retract module's actual output against it.
RETIRED_MENTION_RE = re.compile(
    r"~~(.+?)~~ \[RETRACTED \d{4}-\d{2}-\d{2} r:[0-9a-f]{8}\]\."
)


@dataclass(frozen=True)
class Eligibility:
    """What one record's live metadata says about presenting it as current."""

    state: State
    #: Which signal decided ``state`` — ``status:archived``, ``path:archive``,
    #: ``superseded_by``, ``expired``, ``status:active``, ``unmarked`` — for
    #: logs that must say why.
    reason: str
    #: Refs this record is in open conflict with (``contradicts`` frontmatter).
    contradicts: tuple[str, ...] = ()
    #: Refs of retired sources this record rested on (``stale_backing``).
    stale_backing: tuple[str, ...] = ()
    #: Declared epistemic marker, or ``None`` — absence stays unmarked.
    epistemic: str | None = None
    #: The record's ``expires_at`` as given, or ``None``.
    expires_at: str | None = None
    #: The replacement the record was retired in favour of, if any.
    superseded_by: str | None = None
    #: The record's effective moment for ordering, or ``None`` when undated.
    effective_at: datetime | None = None
    #: Whether the record sits under ``archive/``. Reported beside
    #: :attr:`reason` rather than only through it, because a declared
    #: retirement outranks the path rule and therefore *hides* it: a file that
    #: is both ``status: archived`` and under ``archive/`` says
    #: ``status:archived``. A caller asking "was this retired by anything other
    #: than X?" needs the signal that precedence swallowed.
    by_path: bool = False

    @property
    def usable(self) -> bool:
        """May a surface present this record at all? ``current`` or ``unmarked``."""
        return self.state != "retired"

    @property
    def retired(self) -> bool:
        return self.state == "retired"

    @property
    def qualified(self) -> bool:
        """Does the record carry a qualification a reader must see?"""
        return bool(self.contradicts or self.stale_backing or self.epistemic)


def _archived_by_path(path: str | None) -> bool:
    """True when a *directory* segment of ``path`` is :data:`ARCHIVE_SEGMENT`.

    Whole directory segments only — the same shape as
    :func:`palinode.core.skip_dirs.is_skipped_path` — so ``archives/x.md``,
    ``x/my-archive.md`` and a memory file named ``archive.md`` are all
    untouched.

    The path is read as memory-relative first
    (:func:`palinode.core.path_guard.to_rel_path`), so a store whose own
    absolute path happens to contain a directory called ``archive`` does not
    retire every record in it. A path that cannot be made relative — a test
    fixture's, or one outside the store — is read as given, the same
    fallback ``retirement._memory_relpath`` takes for the same reason.
    """
    if not path:
        return False
    try:
        from palinode.core.path_guard import to_rel_path

        rel = to_rel_path(str(path))
    except Exception:
        rel = str(path)
    return ARCHIVE_SEGMENT in rel.replace(os.sep, "/").split("/")[:-1]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _refs(meta: dict[str, Any], field: str) -> tuple[str, ...]:
    """Typed-link refs, soft-fail: a string is one ref, non-strings are dropped."""
    raw = meta.get(field)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(r.strip() for r in raw if isinstance(r, str) and r.strip())


def _stale_refs(meta: dict[str, Any]) -> tuple[str, ...]:
    """``stale_backing`` entry refs, same soft-fail shape as ``propagate.parse_stale_backing``."""
    raw = meta.get("stale_backing")
    if not isinstance(raw, list):
        return ()
    return tuple(
        str(e["ref"]).strip()
        for e in raw
        if isinstance(e, dict) and str(e.get("ref") or "").strip()
    )


def parse_moment(raw: Any) -> datetime | None:
    """An ISO-8601 timestamp or date to an aware UTC datetime, or ``None``.

    Public because the resolution policy (:mod:`palinode.core.resolution`)
    reads the *declared* ``date`` on its own, separately from the
    :data:`EFFECTIVE_DATE_FIELDS` fallback chain: a save stamp must not be
    able to make a record future-effective. Both must parse a moment the same
    way, so there is one parser.
    """
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw
    if raw is None or isinstance(raw, (int, float, bool)):
        return None
    if hasattr(raw, "isoformat") and not isinstance(raw, str):
        # A YAML bare date (``date: 2026-09-10``) arrives as ``datetime.date``.
        raw = raw.isoformat()
    return parse_expires_at(str(raw))


def effective_at(meta: dict[str, Any]) -> datetime | None:
    """The record's effective moment per :data:`EFFECTIVE_DATE_FIELDS`, or ``None``.

    First parseable field wins; a malformed value falls through to the next
    field rather than dating the record at parse failure.
    """
    for field in EFFECTIVE_DATE_FIELDS:
        moment = parse_moment(meta.get(field))
        if moment is not None:
            return moment
    return None


def eligibility(
    meta: dict[str, Any] | None,
    *,
    path: str | None = None,
    now: datetime | None = None,
) -> Eligibility:
    """Classify a record from its live frontmatter.

    ``path`` is the record's memory-relative path (an absolute one is read
    against the store root), the same ``(path, frontmatter)`` pair
    ``retirement.classify`` takes. One path rule retires: a record under
    ``archive/`` is retired *by location*, because the weekly pass and
    ``archive_memory`` move a note there without touching its frontmatter.
    Every call site holding a path must pass it; given none, the record is
    judged by its frontmatter alone. ``now`` is the clock for ``expires_at``;
    ``None`` reads the wall clock.

    Precedence, first match wins:

    1. ``expires_at`` at or before ``now`` → retired (``expired``). The same
       clock as the TTL sweep; a record that lapsed between sweeps is already
       retired here, as it already is for acting core memories.
    2. ``status`` in :data:`RETIRED_STATUSES` → retired.
    3. ``lifecycle`` in :data:`RETIRED_STATUSES` → retired (KU mirror field).
    4. an ``archive`` directory segment in ``path`` → retired
       (``path:archive``). Below the declared retirements, so a file that is
       both under ``archive/`` and marked ``status: archived`` is reported by
       its declaration; above everything else, so a file under ``archive/``
       whose frontmatter still claims ``status: active`` is retired anyway,
       with the reason naming the path as what decided it. ``unmarked`` never
       applies to a path under ``archive/``.
    5. non-empty ``superseded_by`` → retired. An explicit replacement is
       stronger evidence than any status the record still claims.
    6. ``status`` or ``lifecycle`` declared as anything else → current.
    7. nothing declared → unmarked.
    """
    fm: dict[str, Any] = meta if isinstance(meta, dict) else {}
    clock = now or _utc_now()

    contradicts = _refs(fm, "contradicts")
    stale = _stale_refs(fm)
    epistemic = _text(fm.get("epistemic"))
    expires_raw = _text(fm.get("expires_at"))
    superseded_by = _text(fm.get("superseded_by"))
    moment = effective_at(fm)
    by_path = _archived_by_path(path)

    def _result(state: State, reason: str) -> Eligibility:
        return Eligibility(
            state=state,
            reason=reason,
            contradicts=contradicts,
            stale_backing=stale,
            epistemic=epistemic,
            expires_at=expires_raw,
            superseded_by=superseded_by,
            effective_at=moment,
            by_path=by_path,
        )

    if expires_raw is not None and parse_expires_at(expires_raw) is not None:
        if is_past(expires_raw, clock):
            return _result("retired", "expired")

    status = _text(fm.get("status"))
    lifecycle = _text(fm.get("lifecycle"))
    if status is not None and status.lower() in RETIRED_STATUSES:
        return _result("retired", f"status:{status.lower()}")
    if lifecycle is not None and lifecycle.lower() in RETIRED_STATUSES:
        return _result("retired", f"lifecycle:{lifecycle.lower()}")
    if by_path:
        return _result("retired", f"path:{ARCHIVE_SEGMENT}")
    if superseded_by is not None:
        return _result("retired", "superseded_by")
    if status is not None:
        return _result("current", f"status:{status.lower()}")
    if lifecycle is not None:
        return _result("current", f"lifecycle:{lifecycle.lower()}")
    return _result("unmarked", "unmarked")


def retired_reason(elig: Eligibility) -> str:
    """Why a retired record is retired, naming the replacement when there is one.

    :attr:`Eligibility.reason` says which *signal* decided the state; a reader
    being told a record no longer stands wants the successor when one exists,
    so a ``superseded_by`` pointer outranks the status that carried it (an
    ``archive_memory(..., superseded_by=…)`` writes both). One renderer, so the
    search surface's ``currency_reason`` and the listing's
    ``core_retired_reason`` cannot drift into two spellings of one fact.
    """
    if elig.superseded_by:
        return f"superseded_by: {elig.superseded_by}"
    return elig.reason


def order_key(elig: Eligibility, mtime: float = 0.0) -> tuple[int, float, float]:
    """Sort key for "most recent first" — use with ``reverse=True``.

    Dated records (any :data:`EFFECTIVE_DATE_FIELDS` value) sort above every
    undated one, newest first. Undated records fall back to ``mtime`` among
    themselves only: it orders them without asserting a date, and a touched
    file can never climb above a dated record.
    """
    if elig.effective_at is None:
        return (0, 0.0, mtime)
    return (1, elig.effective_at.timestamp(), mtime)


def is_retired_fact_text(text: str) -> bool:
    """Is this harvested fact text a retired tombstone (see :data:`RETIRED_FACT_RE`)?"""
    return RETIRED_FACT_RE.match(text.strip()) is not None


#: A markdown list marker at the head of a line: the executor writes every
#: fact as a bullet, so a chunk's lines are tested with the marker removed.
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def contains_retired_fact_text(text: str) -> bool:
    """Does any line of this indexed chunk carry a retired fact tombstone?

    The same :data:`RETIRED_FACT_RE` as :func:`is_retired_fact_text`, applied
    line by line with the list marker stripped — the shape a section has after
    the executor supersedes or retracts a fact in place. The indexer projects
    such lines out of the text it derives (:mod:`palinode.core.projection`),
    so a chunk written on the current projection never carries one; a chunk
    indexed before the projection existed, or raw text handed in by a caller,
    can still hold the struck fact and its replacement side by side, and a
    reader must then be told the text carries retired wording even though
    the index agrees with the file.
    """
    return any(
        is_retired_fact_text(_LIST_MARKER_RE.sub("", line, count=1))
        for line in text.splitlines()
    )
