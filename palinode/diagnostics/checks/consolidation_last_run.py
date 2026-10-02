"""Check: consolidation_last_run

``consolidation_schedule_effective`` says what the consolidation cron *should*
run. This check says whether the last pass *worked* — and, more to the point,
how many passes in a row have not.

The count is the number that matters, and what it costs changed when the
nightly's window became a per-project watermark. A failed pass no longer
advances the mark, so its notes are selected again on the next run: the first
failures cost nothing but the delay. What a streak eventually costs is the
catch-up bound — ``B`` days (``nightly.lookback_days``, or the cron's
``--days N``), the furthest back a mark may reach. Once a mark is older than
``B``, the floor clamps it and the notes in the gap leave the nightly's view;
the weekly does not cover them either, since its own window (3 days by
default, Sundays in the shipped crontab) reaches back only a few days from
its own run.

So the thresholds are no longer one arithmetic but two. ``error`` at ``B``
consecutive failures — the streak that actually drops notes — and ``warn`` at
two, because a broken nightly is worth surfacing long before it loses
anything, and two in a row is no longer plausibly a one-off. On the shipped
default (``B = 7``) that is warn at two, error at seven; a host that pins
``--days 1`` gets error on the first failure, which is the truthful answer
for a bound that thin.

Where the history comes from
----------------------------
The activity gate's state file, ``<memory_dir>/.palinode/consolidation-state.json``.
The runner already opens that file to stamp the gate's clock after a success;
it now also appends every real pass's outcome under a ``runs`` key
(``activity_gate.record_outcome``) — ``success``, ``partial`` with the failed
project ids, the idle statuses, and ``error`` when the pass raised. Structured,
written by the thing that knows, and read here without parsing anyone's log.
The alternative — the ``Consolidation complete: {...}`` lines — lives wherever
the crontab redirected stdout, which palinode does not know; it is not read.

The streak is *derived* from the records, newest backwards to the last
success, never stored as a counter — the gate's own rule for its session
count, for the same reason: a hand-edited or truncated file cannot put a
counter and the history it summarises out of step.

What counts as a failure
------------------------
``partial`` and ``error``. The idle statuses (``no_new_notes``,
``no notes found``) count as success: a pass that found nothing in its window
left nothing unconsolidated. A dry run is never recorded — a dry-run
"success" would reset a real streak.

Degradation
-----------
No state file, a file that cannot be read or parsed, a state file from before
outcomes were recorded (the gate's clock alone), or a host that consolidates
some other way: each reports what it could not see and passes with ``info``.
The gate's clock, when that is all there is, is still reported — it is the
last *successful* start the host knows of. Never a failure for a missing
record: a check that fails on every laptop is a check nobody reads on the
host where it matters.

``fast``: one small JSON read.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from palinode.consolidation import activity_gate
from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

logger = logging.getLogger(__name__)

CHECK_NAME = "consolidation_last_run"

#: Weekly thresholds. The weekly is a backstop with its own window; its
#: streak has no day-losing arithmetic of its own, so it warns at two and
#: fails at three regardless of lookback.
WEEKLY_WARN_AT = 2
WEEKLY_FAIL_AT = 3

#: Statuses meaning the pass finished with nothing left unconsolidated. The
#: idle statuses belong here: nothing selected is nothing left behind. Shared
#: with the watermark's cold start, which must agree with this check about
#: which recorded passes count as successful.
_OK_STATUSES = activity_gate.SUCCESS_STATUSES

#: Statuses that count toward a streak. A record with no recognisable status
#: (a hand-edited file, a runner result that carried none) is reported as
#: ``unknown`` and counts for neither side — it must not manufacture a streak
#: on a 1-day lookback, where one failure is already an error.
_FAIL_STATUSES = frozenset({"partial", "error"})

_CADENCE = {"nightly": "daily", "weekly": "weekly"}


#: Where the nightly warn fires, whatever the catch-up bound. A failed nightly
#: costs nothing until the bound is exceeded, so this is not "about to lose
#: notes" — it is "the nightly has stopped working", which on a daily cadence
#: two consecutive failures establish and one does not.
NIGHTLY_WARN_AT = 2


def _why_nightly(nightly_days: int, weekly_days: int) -> str:
    """The sentence that turns a count into a consequence, from the real numbers.

    Kept in one place so the warn and the error say the same thing about the
    same arithmetic — and derived, because the bound is configurable and the
    dogfood host's cron pins its own.
    """
    runs = "one run" if nightly_days == 1 else f"{nightly_days} runs"
    return (
        f"a failed nightly does not advance the watermark, so its notes are "
        f"selected again — but only until the mark is older than the "
        f"{nightly_days}-day catch-up bound, which is {runs} at a daily cadence, "
        f"and the weekly's {weekly_days}-day window reaches back only "
        f"{weekly_days} days from its own run"
    )


def nightly_thresholds(catchup_days: int) -> tuple[int, int]:
    """``(warn_at, fail_at)`` for a nightly bounded by ``catchup_days``.

    The error is the streak that actually drops notes: at a daily cadence a
    mark is older than the bound after ``catchup_days`` consecutive failures,
    and the floor then clamps it. The warn does not scale with the bound — a
    nightly that has failed twice needs looking at whether the bound is 3 days
    or 30 — except where the bound is tighter than the warn, in which case
    there is no room to warn first and both fire together.
    """
    fail_at = max(int(catchup_days), 1)
    return min(NIGHTLY_WARN_AT, fail_at), fail_at


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ModeSummary:
    """One mode's history, reduced to what the message needs."""

    mode: str
    #: The newest record, or None when nothing has been recorded.
    last: dict[str, Any] | None
    #: Consecutive failed passes ending at ``last``; 0 when ``last`` is ok.
    streak: int
    #: Start of the newest ok pass, if any is recorded.
    last_ok_at: datetime | None
    #: The gate's own clock stamp — a success start — for a state file that
    #: predates outcome records or has none for this mode.
    gate_stamp: datetime | None
    #: The ``lookback_days`` the newest record ran with, else the configured
    #: one. For the nightly this is the catch-up bound; for the weekly it is
    #: still the lookback window.
    lookback_days: int


def _is_ok(entry: dict[str, Any]) -> bool:
    return str(entry.get("status")) in _OK_STATUSES


def summarize(
    mode: str,
    history: list[dict[str, Any]],
    gate_entry: dict[str, Any],
    configured_lookback: int,
) -> ModeSummary:
    """Reduce ``mode``'s outcome records (oldest first) to a :class:`ModeSummary`."""
    streak = 0
    last_ok_at: datetime | None = None
    for entry in reversed(history):
        if _is_ok(entry):
            last_ok_at = activity_gate._parse_timestamp(entry.get("started_at"))
            break
        if str(entry.get("status")) in _FAIL_STATUSES:
            streak += 1
    last = history[-1] if history else None
    recorded = last.get("lookback_days") if last else None
    lookback = recorded if isinstance(recorded, int) and recorded >= 1 else configured_lookback
    return ModeSummary(
        mode=mode,
        last=last,
        streak=streak,
        last_ok_at=last_ok_at,
        gate_stamp=activity_gate._parse_timestamp(gate_entry.get("last_run_at")),
        lookback_days=max(int(lookback), 1),
    )


def _level(summary: ModeSummary) -> int:
    """0 ok, 1 warn, 2 error — the streak against the mode's own thresholds."""
    if summary.mode == "nightly":
        warn_at, fail_at = nightly_thresholds(summary.lookback_days)
    else:
        warn_at, fail_at = WEEKLY_WARN_AT, WEEKLY_FAIL_AT
    if summary.streak >= fail_at:
        return 2
    if summary.streak >= warn_at:
        return 1
    return 0


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def _load_state(path: Path) -> tuple[dict[str, Any] | None, str]:
    """The parsed state file, or ``None`` and the reason it could not be used."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"{path} does not exist"
    except OSError as exc:
        return None, f"{path} could not be read ({exc.strerror or exc})"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"{path} is not valid JSON ({exc})"
    if not isinstance(parsed, dict):
        return None, f"{path} is not a JSON object"
    return parsed, ""


def _history(state: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    runs = state.get("runs")
    if not isinstance(runs, dict):
        return []
    entries = runs.get(mode)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _gate_entry(state: dict[str, Any], mode: str) -> dict[str, Any]:
    modes = state.get("modes")
    if not isinstance(modes, dict):
        return {}
    entry = modes.get(mode)
    return entry if isinstance(entry, dict) else {}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _stamp(moment: datetime | None) -> str:
    if moment is None:
        return "an unreadable time"
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(moment: datetime, now: datetime) -> str:
    hours = (now - moment).total_seconds() / 3600
    return f"{hours:.0f} h ago"


def _describe(summary: ModeSummary, now: datetime, why_nightly: str) -> str:
    """One clause per mode: the facts, then the consequence if there is one."""
    mode = summary.mode
    if summary.last is None:
        if summary.gate_stamp is None:
            return f"{mode}: no run recorded"
        return (
            f"{mode}: no outcome recorded yet (outcomes are kept from this release "
            f"on); the activity gate last stamped a successful run at "
            f"{_stamp(summary.gate_stamp)} ({_ago(summary.gate_stamp, now)})"
        )

    last = summary.last
    started = activity_gate._parse_timestamp(last.get("started_at"))
    status = str(last.get("status") or "unknown")
    details: list[str] = []
    if isinstance(last.get("lookback_days"), int):
        details.append(f"--days {last['lookback_days']}")
    if started is not None:
        details.append(_ago(started, now))
    failed = [str(project) for project in last.get("failed_projects") or []]
    if failed:
        details.append(f"failed: {', '.join(failed)}")
    if last.get("error"):
        details.append(f"raised {last['error']}")

    if summary.last_ok_at is not None:
        since_ok = (
            f"last success {_stamp(summary.last_ok_at)} "
            f"({_ago(summary.last_ok_at, now)}, {_CADENCE[mode]} cadence)"
        )
    else:
        since_ok = "no successful run in the recorded history"

    plural = "" if summary.streak == 1 else "s"
    clause = (
        f"{mode}: last run {status} at {_stamp(started)} ({', '.join(details)}); "
        f"{summary.streak} consecutive failure{plural}; {since_ok}"
    )

    level = _level(summary)
    if mode == "nightly" and level == 2:
        clause += (
            f" — {summary.streak} in a row reaches the {summary.lookback_days}-day "
            f"catch-up bound: notes older than it are no longer selected "
            f"({why_nightly})"
        )
    elif mode == "nightly" and level == 1:
        remaining = max(summary.lookback_days - summary.streak, 0)
        clause += (
            f" — the nightly has not completed for {summary.streak} runs; nothing "
            f"is lost yet and roughly {remaining} more day(s) of catch-up remain: "
            f"{why_nightly}"
        )
    elif level >= 1:
        clause += (
            f" — {summary.streak} consecutive {mode} passes have left their notes "
            f"unconsolidated"
        )
    return clause


def _not_applicable(message: str) -> CheckResult:
    return CheckResult(
        name=CHECK_NAME,
        severity="info",
        passed=True,
        message=message,
        remediation=None,
        tags=("fast",),
    )


def _absent(reason: str) -> CheckResult:
    platform_note = ""
    if not sys.platform.startswith("linux"):
        platform_note = (
            f" No palinode cron is shipped for {sys.platform}, so a run here is "
            f"hand-run or API-driven and its absence is expected."
        )
    return _not_applicable(
        f"No consolidation run recorded — {reason}. The file is written by the "
        f"first real pass (`palinode consolidate`, the cron entry point, or "
        f"POST /consolidate); outcomes of passes before this release are not "
        f"visible here.{platform_note}"
    )


@register(tags=("fast",))
def consolidation_last_run(ctx: DoctorContext) -> CheckResult:
    """Report the last consolidation pass per mode and the length of any failure streak."""
    memory_dir = getattr(ctx.config, "memory_dir", None) or ctx.config.palinode_dir
    path = activity_gate.state_path(memory_dir)
    state, problem = _load_state(path)
    if state is None:
        if problem.endswith("does not exist"):
            return _absent(problem)
        return _not_applicable(
            f"Consolidation run history could not be read — {problem}. The next "
            f"real pass rewrites the file; until then the consolidation log is the "
            f"only record of recent outcomes."
        )

    now = _utc_now()
    consolidation = ctx.config.consolidation
    configured = {
        "nightly": int(consolidation.nightly.lookback_days),
        "weekly": int(consolidation.lookback_days),
    }
    summaries = [
        summarize(mode, _history(state, mode), _gate_entry(state, mode), configured[mode])
        for mode in ("nightly", "weekly")
    ]
    if all(s.last is None and s.gate_stamp is None for s in summaries):
        return _absent(f"{path} records no run for either mode")

    nightly, weekly = summaries
    why_nightly = _why_nightly(nightly.lookback_days, weekly.lookback_days)
    message = (
        "Last consolidation runs — "
        + "; ".join(_describe(s, now, why_nightly) for s in summaries)
        + "."
    )
    worst = max(_level(s) for s in summaries)

    if worst == 0:
        return CheckResult(
            name=CHECK_NAME,
            severity="info",
            passed=True,
            message=message,
            remediation=None,
            tags=("fast",),
        )

    return CheckResult(
        name=CHECK_NAME,
        severity="error" if worst == 2 else "warn",
        passed=False,
        message=message,
        remediation=(
            "Find the cause in the consolidation log — the runner names it: "
            "`finish_reason=length` is the token cap (`consolidation.llm_max_tokens`), "
            "an open circuit is an unreachable endpoint, a prose-only reply is the "
            "model ignoring the JSON contract. Then run the pass by hand: "
            "`palinode consolidate --nightly` (or `palinode consolidate` for the "
            "weekly). An on-demand pass records its outcome too, and a success "
            "resets the streak. A hand-run nightly picks up exactly what the "
            "failed ones left behind — their watermarks never advanced — as long "
            "as the marks are still inside the catch-up bound "
            "(`consolidation.nightly.lookback_days`, or `--days N` on the cron "
            "line); past that the pass reports what it skipped."
        ),
        tags=("fast",),
    )
