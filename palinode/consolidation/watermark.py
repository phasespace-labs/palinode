"""Per-project high-water marks for the nightly consolidation pass.

The nightly used to select notes with a UTC calendar-date cutoff — ``date >=
today − lookback_days``, compared as strings. That window is wrong at both
ends. Too narrow and a working day that straddles UTC midnight falls outside
it and is never consolidated, because nothing revisits a window once it has
moved on. Too wide and every note in it is re-sent on every run, which is the
one thing the nightly cannot afford: its whole design rests on a small context
("smaller LLM context = better JSON output"), and a widened window is what
produced a proposal cut off at the token cap that applied nothing.

A watermark removes the window rather than resizing it. Each project carries
the timestamp of the last pass that **resolved** it; the next pass selects the
notes written since. Three properties follow:

* **Timezone-immune.** A timestamp comparison has no calendar-day boundary to
  misalign with a working day.
* **Self-healing.** A failed, truncated, deferred or lock-refused run does not
  advance the mark, so the next run covers the gap by construction. This is
  what a widened ``--days`` was hand-approximating.
* **Per project.** A pass where one project fails and three succeed advances
  three marks and leaves one. A single global mark gets that wrong in one of
  two directions: it either re-sends the three or abandons the one.

Where it lives
--------------
Beside the activity gate's clock and outcome history, in
``<memory_dir>/.palinode/consolidation-state.json`` under ``watermarks``,
written through the gate's own atomic descriptor-level writer. It is derived
operational state, never memory content, and it is rebuildable: losing the
file costs a bounded catch-up, not notes. Writers are serialised by the
consolidation run lock, the same protection ``record_run`` and
``record_outcome`` already rely on.

The floor
---------
A mark that has not advanced in months must not hand the model months of
notes in one request. ``catchup_days`` bounds how far back any mark reaches —
cold start included — and the clamp is reported, never silent. The shipped
default matches ``consolidation.auto_gate.max_hours_elapsed`` (168 h): the
gate may legitimately let a week pass before firing a pass at its ceiling, so
a bound shorter than that would drop notes the gate itself chose to wait on.

Resumable progress
------------------
A pass cannot always show the model everything it selected: the prompt has a
fixed budget for note text. A mark that moved to the pass's start regardless
would acknowledge notes the model never saw, so a pass that could
not present its whole selection records a **resume position** instead of a
timestamp: the first note, in ``(modified_at, path)`` order, that it did not
finish, and how many characters of that note it did. The next pass starts
there, so a backlog larger than one prompt is consumed a prompt at a time and
nothing is acknowledged unseen. A pass that presented everything records the
plain timestamp, exactly as before.

Nightly only. The weekly pass is a deep clean with ARCHIVE and MERGE over a
deliberately wider view and keeps its window; the ``mode`` key exists so the
file's shape matches ``modes`` and ``runs`` next to it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from palinode.consolidation import activity_gate

logger = logging.getLogger("palinode.consolidation.watermark")

#: The only pass with watermarks today.
MODE = "nightly"

#: Top-level key in the consolidation state file. Its own key because
#: ``record_run`` replaces ``modes[mode]`` wholesale and ``record_outcome``
#: appends under ``runs``.
STATE_KEY = "watermarks"


def stamp(moment: datetime) -> str:
    """The state file's timestamp form — UTC, ``Z``-suffixed."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def floor_at(now: datetime, catchup_days: int) -> datetime:
    """The earliest moment any mark may reach back to on this pass."""
    return now - timedelta(days=max(int(catchup_days), 1))


def _entry(state: dict[str, Any], mode: str) -> dict[str, Any]:
    marks = state.get(STATE_KEY)
    if not isinstance(marks, dict):
        return {}
    entry = marks.get(mode)
    return entry if isinstance(entry, dict) else {}


def load(
    memory_dir: str | os.PathLike[str] | None = None, mode: str = MODE
) -> dict[str, datetime]:
    """Every recorded mark for ``mode``, keyed by project id.

    A resume position contributes its ``modified_at``; :func:`load_resume`
    returns the rest of it.

    An unreadable value is dropped with a warning rather than failing the
    pass: the project then starts from the cold-start moment, which costs a
    bounded re-read and never loses notes.
    """
    state = activity_gate._read_state(activity_gate.state_path(memory_dir))
    marks: dict[str, datetime] = {}
    for project_id, raw in _entry(state, mode).items():
        resume = Resume.from_state(raw)
        parsed = (
            resume.modified_at
            if resume is not None
            else activity_gate._parse_timestamp(raw)
        )
        if parsed is None:
            logger.warning(
                "Ignoring unreadable consolidation watermark project=%r value=%r",
                project_id,
                raw,
            )
            continue
        marks[str(project_id)] = parsed
    return marks


def cold_start(
    floor: datetime,
    memory_dir: str | os.PathLike[str] | None = None,
    mode: str = MODE,
) -> tuple[datetime, str]:
    """Where a project with no recorded mark starts, and why — for the log.

    The last *successful* pass the store recorded is the honest seed: whatever
    it consolidated is already in the project documents. Both places that
    record one are read — the outcome history (which counts the idle statuses
    as success, since an empty window left nothing behind) and the gate's own
    clock, which is all a state file written before outcomes existed carries —
    and the newer wins. With neither, the pass starts at the catch-up floor.
    Every answer is clamped to the floor, so a store that has not consolidated
    since spring does not hand the model since spring.
    """
    state = activity_gate._read_state(activity_gate.state_path(memory_dir))
    candidates: list[datetime] = []
    for record in activity_gate.run_history(mode, memory_dir):
        if str(record.get("status")) in activity_gate.SUCCESS_STATUSES:
            recorded = activity_gate._parse_timestamp(record.get("started_at"))
            if recorded is not None:
                candidates.append(recorded)
    gate_stamp = activity_gate._parse_timestamp(
        activity_gate._mode_state(state, mode).get("last_run_at")
    )
    if gate_stamp is not None:
        candidates.append(gate_stamp)

    if not candidates:
        return floor, f"no successful {mode} pass recorded; starting at the catch-up floor"
    newest = max(candidates)
    if newest <= floor:
        return floor, (
            f"last successful {mode} pass {stamp(newest)} is older than the "
            f"catch-up bound; starting at the floor"
        )
    return newest, f"last successful {mode} pass {stamp(newest)}"


@dataclass(frozen=True)
class Since:
    """Where one project's selection starts on this pass."""

    project_id: str
    since: datetime
    #: ``mark`` (its own recorded watermark), ``cold-start`` (none recorded),
    #: or ``floor`` (recorded but clamped by the catch-up bound).
    source: str
    #: The mark the floor overrode, when it did. ``None`` otherwise.
    clamped_from: datetime | None


@dataclass(frozen=True)
class Resume:
    """Where the next pass picks up inside a selection it could not finish.

    Notes are ordered by ``(modified_at, path)``. Everything before
    ``(modified_at, path)`` in that order was presented in full; the note at
    that position was presented up to ``offset`` characters of its body. A
    note edited since has a later ``modified_at``, so it no longer matches and
    is re-read from the start — the offset only ever applies to the revision
    it was measured on.
    """

    modified_at: datetime
    #: Relative to the memory dir — the tie-break between equal timestamps.
    path: str
    offset: int = 0

    def to_state(self) -> dict[str, Any]:
        return {"at": stamp(self.modified_at), "path": self.path, "offset": self.offset}

    @classmethod
    def from_state(cls, raw: Any) -> Resume | None:
        if not isinstance(raw, dict):
            return None
        at = activity_gate._parse_timestamp(raw.get("at"))
        path = raw.get("path")
        offset = raw.get("offset", 0)
        if at is None or not isinstance(path, str) or not path:
            return None
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return None
        return cls(at, path, offset)


def _position(raw: Any) -> tuple[datetime, int, str, int] | None:
    """A stored entry as a point in ``(modified_at, path, offset)`` order.

    A plain timestamp covers *everything* written at or before it, so it sorts
    after every resume position at the same moment.
    """
    resume = Resume.from_state(raw)
    if resume is not None:
        return (resume.modified_at, 0, resume.path, resume.offset)
    at = activity_gate._parse_timestamp(raw)
    return (at, 1, "", 0) if at is not None else None


def load_resume(
    memory_dir: str | os.PathLike[str] | None = None, mode: str = MODE
) -> dict[str, Resume]:
    """The projects whose last resolved pass stopped part-way, and where."""
    state = activity_gate._read_state(activity_gate.state_path(memory_dir))
    resumes: dict[str, Resume] = {}
    for project_id, raw in _entry(state, mode).items():
        resume = Resume.from_state(raw)
        if resume is not None:
            resumes[str(project_id)] = resume
    return resumes


def since_for(
    project_id: str,
    *,
    marks: dict[str, datetime],
    cold_start_at: datetime,
    floor: datetime,
) -> Since:
    """Resolve one project's starting point against its mark and the floor."""
    mark = marks.get(project_id)
    if mark is None:
        return Since(project_id, max(cold_start_at, floor), "cold-start", None)
    if mark < floor:
        return Since(project_id, floor, "floor", mark)
    return Since(project_id, mark, "mark", None)


def advance(
    project_ids: Iterable[str],
    at: datetime,
    memory_dir: str | os.PathLike[str] | None = None,
    mode: str = MODE,
    resume: Mapping[str, Resume] | None = None,
) -> list[str]:
    """Move the named projects' marks to ``at``; returns the ids actually written.

    A project named in ``resume`` moves to that position instead: its pass
    resolved only a prefix of what it selected (see *Resumable progress*).

    ``at`` is the pass's *start*, not its finish: a note written while the pass
    was running was not in its selection, and stamping the finish would step
    over it. A mark never moves backwards — an out-of-order call (a hand-run
    pass racing a cron tick that took an hour) must not re-expose notes an
    earlier mark already covered.

    Like the gate's clock, a state file that cannot be written degrades the
    pass, it does not fail it: the work happened, and the cost of losing the
    stamp is a bounded re-read on the next run.
    """
    ids = sorted({str(project_id) for project_id in project_ids})
    if not ids:
        return []

    path = activity_gate.state_path(memory_dir)
    state = activity_gate._read_state(path)
    marks = state.get(STATE_KEY)
    if not isinstance(marks, dict):
        marks = {}
    entry = dict(_entry(state, mode))
    written: list[str] = []
    resume = resume or {}
    for project_id in ids:
        target = resume.get(project_id)
        value: str | dict[str, Any] = target.to_state() if target else stamp(at)
        current = _position(entry.get(project_id))
        wanted = _position(value)
        if current is not None and wanted is not None and current >= wanted:
            continue
        entry[project_id] = value
        written.append(project_id)
    if not written:
        return []

    marks[mode] = entry
    state[STATE_KEY] = marks
    try:
        activity_gate._write_state(path, state)
    except OSError as error:
        logger.warning(
            "Could not record consolidation watermarks path=%s error=%r", path, error
        )
        return []
    return written
