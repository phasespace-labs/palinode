"""Activity gate for automatic consolidation.

Wall-clock cron answers "is it 4am on a Sunday?", which is not the question
consolidation cares about. Bursty solo use makes both errors at once: idle
weeks burn an LLM pass over nothing, and heavy days wait days for the next
tick. The gate replaces the calendar with two conditions that must **both**
hold before an automatic pass runs — enough time elapsed *and* enough sessions
recorded since the last run — so an hourly cron can fire for free and the pass
lands when there is something to consolidate.

Three deliberate choices:

* **The session counter is derived, not incremented.** It is the number of
  ``## Session End —`` headings in ``daily/*.md`` newer than the last recorded
  run. ``POST /session-end`` is the single writer of that heading and every
  surface (MCP, CLI, hook, plugin) routes through it, so the count is exact
  without a second write path to keep coherent across the API, watcher and cron
  processes. Deleting the state file loses the clock, never the sessions.
* **A ceiling defeats the gate.** ``max_hours_elapsed`` runs the pass regardless
  of session count. A deployment that ingests through the watcher and never
  records a session would otherwise never consolidate at all — the dual gate
  alone converts "no sessions" into "never", which is a worse failure than the
  wasted pass it exists to prevent.
* **Last-run state is per mode.** Nightly and weekly are separate passes on
  separate cadences; one shared clock lets whichever ran last starve the other.

On-demand runs (``palinode consolidate`` / ``dream``, ``POST /consolidate``,
the MCP tool) bypass the gate — an operator asking for a pass has already made
the decision the gate exists to make. ``--respect-gate`` opts a manual
invocation into the same policy, which is what a hand-rolled scheduler wants.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from palinode.core.config import config

logger = logging.getLogger("palinode.consolidation.activity_gate")

Mode = Literal["weekly", "nightly"]

#: Sits beside the run lock in the store's git-ignored operational directory,
#: so gate bookkeeping never becomes memory content.
STATE_RELATIVE_PATH = Path(".palinode") / "consolidation-state.json"

#: The one heading ``POST /session-end`` writes into the day's daily note.
#: The dash is an em dash today; the alternatives are accepted so a cosmetic
#: change to the writer cannot silently zero the counter.
_SESSION_HEADING = re.compile(
    r"^##[ \t]+Session End[ \t]+[—–-][ \t]+(\S+)[ \t]*$", re.MULTILINE
)


@dataclass(frozen=True)
class GateDecision:
    """Whether an automatic pass may run, and the numbers behind the answer."""

    should_run: bool
    mode: str
    reason: str
    sessions_since_last_run: int
    min_sessions: int
    #: ``None`` when no run has ever been recorded — unbounded elapsed time.
    hours_since_last_run: float | None
    min_hours_elapsed: float
    max_hours_elapsed: float
    last_run_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "should_run": self.should_run,
            "mode": self.mode,
            "reason": self.reason,
            "sessions_since_last_run": self.sessions_since_last_run,
            "min_sessions": self.min_sessions,
            "hours_since_last_run": self.hours_since_last_run,
            "min_hours_elapsed": self.min_hours_elapsed,
            "max_hours_elapsed": self.max_hours_elapsed,
            "last_run_at": self.last_run_at,
        }


def state_path(memory_dir: str | os.PathLike[str] | None = None) -> Path:
    """Return the gate's state file for one configured memory store."""
    base = memory_dir or getattr(config, "memory_dir", None) or config.palinode_dir
    return Path(base).expanduser() / STATE_RELATIVE_PATH


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        logger.warning("Could not read consolidation gate state path=%s error=%r", path, error)
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Ignoring unparseable consolidation gate state path=%s", path)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    """Write the state file atomically — a torn read reverts the gate to
    "never run", which fires a pass rather than silently suppressing one.

    Descriptor-level, like ``run_lock``'s writer next door and for the same
    reason: this is operational state under ``.palinode/``, never a memory
    file, so it must not go through the ``git_tools`` mutation choke point
    (which writes *and commits*) — and it must not read as if it bypassed it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    encoded = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written == 0:
                raise OSError("short write while recording consolidation gate state")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)


def _mode_state(state: dict[str, Any], mode: str) -> dict[str, Any]:
    modes = state.get("modes")
    if not isinstance(modes, dict):
        return {}
    entry = modes.get(mode)
    return entry if isinstance(entry, dict) else {}


def _daily_dir(memory_dir: str | os.PathLike[str] | None = None) -> Path:
    base = memory_dir or getattr(config, "memory_dir", None) or config.palinode_dir
    return Path(base).expanduser() / "daily"


def count_sessions_since(
    since: datetime | None,
    memory_dir: str | os.PathLike[str] | None = None,
) -> int:
    """Count session-end entries in ``daily/`` recorded after ``since``.

    A heading whose timestamp will not parse is counted. Over-counting defers
    nothing — it lets a pass run, which is the status quo — while under-counting
    stalls consolidation indefinitely, so the ambiguous case resolves toward
    running.
    """
    daily = _daily_dir(memory_dir)
    if not daily.is_dir():
        return 0

    total = 0
    for note in sorted(daily.glob("*.md")):
        try:
            body = note.read_text(encoding="utf-8", errors="ignore")
        except OSError as error:
            logger.warning("Could not read daily note path=%s error=%r", note, error)
            continue
        for raw_timestamp in _SESSION_HEADING.findall(body):
            if since is None:
                total += 1
                continue
            recorded = _parse_timestamp(raw_timestamp)
            if recorded is None:
                logger.debug(
                    "Counting session-end heading with unparseable timestamp "
                    "path=%s value=%r",
                    note,
                    raw_timestamp,
                )
                total += 1
            elif recorded > since:
                total += 1
    return total


def evaluate(
    mode: Mode = "weekly",
    memory_dir: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> GateDecision:
    """Decide whether an automatic ``mode`` pass may run right now."""
    gate = config.consolidation.auto_gate
    now = now or _utc_now()

    entry = _mode_state(_read_state(state_path(memory_dir)), mode)
    last_run_raw = entry.get("last_run_at")
    last_run = _parse_timestamp(last_run_raw)
    last_run_at = last_run_raw if isinstance(last_run_raw, str) and last_run else None

    hours_elapsed = (now - last_run).total_seconds() / 3600 if last_run else None
    sessions = count_sessions_since(last_run, memory_dir)

    def decide(should_run: bool, reason: str) -> GateDecision:
        return GateDecision(
            should_run=should_run,
            mode=mode,
            reason=reason,
            sessions_since_last_run=sessions,
            min_sessions=gate.min_sessions,
            hours_since_last_run=hours_elapsed,
            min_hours_elapsed=gate.min_hours_elapsed,
            max_hours_elapsed=gate.max_hours_elapsed,
            last_run_at=last_run_at,
        )

    if not gate.enabled:
        return decide(True, "gate disabled")

    if hours_elapsed is None:
        return decide(True, "no previous run recorded")

    if hours_elapsed >= gate.max_hours_elapsed:
        return decide(
            True,
            f"ceiling reached: {hours_elapsed:.0f} h / {gate.max_hours_elapsed:.0f} h max",
        )

    if hours_elapsed >= gate.min_hours_elapsed and sessions >= gate.min_sessions:
        return decide(
            True,
            f"{sessions} sessions / {gate.min_sessions}, "
            f"{hours_elapsed:.0f} h / {gate.min_hours_elapsed:.0f} h",
        )

    return decide(
        False,
        f"deferred: {sessions} sessions / {gate.min_sessions}, "
        f"{hours_elapsed:.0f} h / {gate.min_hours_elapsed:.0f} h",
    )


def record_run(
    mode: Mode = "weekly",
    memory_dir: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> None:
    """Record that a ``mode`` pass completed, resetting the gate's counters.

    Called only for a real (non-dry-run) pass that returned without raising.
    A pass that raised leaves the previous timestamp in place, so its work is
    retried on the next tick rather than waiting out a fresh interval.
    ``sessions_at_run`` is stored for observability only — the live counter is
    derived from ``last_run_at``, so a hand-edited or missing state file cannot
    put the two out of step.
    """
    now = now or _utc_now()
    path = state_path(memory_dir)
    state = _read_state(path)
    modes = state.get("modes")
    if not isinstance(modes, dict):
        modes = {}
    modes[mode] = {
        "last_run_at": now.isoformat().replace("+00:00", "Z"),
        "sessions_at_run": count_sessions_since(
            _parse_timestamp(_mode_state(state, mode).get("last_run_at")), memory_dir
        ),
    }
    state["modes"] = modes
    try:
        _write_state(path, state)
    except OSError as error:
        # A store whose operational directory is unwritable still consolidated;
        # losing the bookkeeping degrades the gate to "always run", which is
        # today's behaviour, and must not fail the pass that just succeeded.
        logger.warning(
            "Could not record consolidation run path=%s error=%r", path, error
        )


def status(memory_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Gate configuration and per-mode state, for ``/status`` and diagnostics."""
    gate = config.consolidation.auto_gate
    return {
        "enabled": gate.enabled,
        "min_hours_elapsed": gate.min_hours_elapsed,
        "min_sessions": gate.min_sessions,
        "max_hours_elapsed": gate.max_hours_elapsed,
        "modes": {
            mode: evaluate(mode, memory_dir).as_dict()
            for mode in ("weekly", "nightly")
        },
    }
