"""Check: consolidation_schedule_effective

``palinode.config.yaml`` declares the consolidation lookback in two keys —
``consolidation.lookback_days`` for the weekly pass's lookback *window* and
``consolidation.nightly.lookback_days`` for the nightly's **catch-up bound** —
the nightly selects on a per-project watermark, not a window, and the number
only bounds how far back a cold or long-failed mark may reach. The cron lines
that actually run those passes may also pass ``--days N``, and when
they do the argument wins: the entry point reads ``--days`` from ``sys.argv``
and only falls back to the configured value when it is absent. Nothing
announces the override.

Separately from the mismatch below, this check also flags a nightly catch-up
bound that is tighter than ``consolidation.auto_gate.max_hours_elapsed``. The
gate may legitimately let a pass go that long before firing at its ceiling
(``AutoGateConfig.max_hours_elapsed``'s own docstring), and ``NightlyConfig``'s
own comment says a bound shorter than that ceiling drops the notes the gate
itself chose to wait on — a cold or long-failed mark older than the bound is
clamped, and the weekly does not catch the gap either (its own window reaches
back only a few days from its own run).

That gap cost a real diagnosis. A failing nightly was reasoned about from the
configured 1-day lookback; the line in ``/etc/cron.d`` passed ``--days 3``, so
the failing run had seen three days of notes and a hand-run reproduction using
the configured value saw one. Same store, same document, different input set,
opposite conclusion. The weekly had been running ``--days 3`` against a declared
``lookback_days: 7`` for months, unnoticed.

So this check reports the **effective** lookback whether or not it disagrees
with the config. A passing result that says "nightly: ``--days 1``, matching
``consolidation.nightly.lookback_days=1``" is most of the value here; the
mismatch warning is the rest.

What it deliberately does not do
--------------------------------
*Nothing is written.* Not the cron files, not the config. Which of the two
should change is a judgement about what the weekly pass is *for*, and doctor
does not have it.

*The schedule expression is reported, never compared.*
``consolidation.schedule`` exists in the config but drives nothing — the cron
tab is an upper bound and the activity gate decides whether a tick does any
work (see ``docs/OPERATIONS.md``). Flagging ``17 * * * *`` against a declared
``0 3 * * 0`` would fire on every host that follows the documented advice. The
expression is printed because an operator asking "what actually runs" wants it,
not because a difference means anything.

Platform and degradation
------------------------
Linux only. macOS schedules through launchd and ships no palinode cron, so
there the check reports not-applicable rather than failing — a check that fails
on every laptop is a check nobody reads on the one host where it matters. The
same applies to every other blind spot: no cron entry found, a directory that
cannot be listed, root's crontab when doctor is not root. Each reports what it
could not see and why, and passes.

Where it looks, and why those places
------------------------------------
- ``/etc/cron.d/*`` — where a packaged or Ansible-managed install lands, and
  where the dogfood host's entry lives. Filenames outside run-parts'
  ``[A-Za-z0-9_-]`` rule are skipped *because cron skips them*: a file named
  ``palinode.bak-20260531`` sitting in that directory is inert, and reporting
  its stale schedule as live would be worse than not reporting at all.
- ``/etc/crontab`` — the system crontab, same six-field format.
- ``crontab -l`` for the invoking user — the layout ``docs/OPERATIONS.md``
  documents for a non-root install.
- ``crontab -l -u root`` when doctor is not running as root — attempted so that
  "I could not see root's crontab" is *said* rather than silently assumed
  empty. When doctor *is* root, plain ``crontab -l`` already is root's.

``deep``: reads a handful of small files and shells out to ``crontab``.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from palinode.diagnostics.registry import register
from palinode.diagnostics.types import CheckResult, DoctorContext

logger = logging.getLogger(__name__)

CHECK_NAME = "consolidation_schedule_effective"

#: Every scheduled pass invokes this module path; a line without it is not ours.
CRON_MODULE = "palinode.consolidation.cron"

#: Module-level so tests can point the whole check at a tmp_path tree.
CRON_D_DIR = Path("/etc/cron.d")
SYSTEM_CRONTAB = Path("/etc/crontab")

#: cron reads a file in /etc/cron.d only if its name is letters, digits,
#: underscores and hyphens (run-parts' rule). Anything else is inert.
_RUN_PARTS_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: A plausible cron time field: numeric/wildcard/step/range, or a three-letter
#: month or weekday name with optional range and list punctuation. Deliberately
#: permissive about values (``99 * * * *`` passes) and strict about *shape*, so
#: that a line whose first five tokens are not a schedule at all is caught.
_CRON_FIELD_RE = re.compile(
    r"^(?:[0-9*][0-9*,\-/]*|[A-Za-z]{3}[A-Za-z0-9,\-/]*)$"
)

#: ``PALINODE_DIR=/x`` at the start of a line is a crontab environment
#: assignment, not a job, and carries no schedule.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")

_CONFIG_KEY = {
    "nightly": "consolidation.nightly.lookback_days",
    "weekly": "consolidation.lookback_days",
}


@dataclass(frozen=True)
class CronEntry:
    """One cron line that invokes the consolidation entry point.

    *days* is the value the entry point would use, or ``None`` when the line
    passes no usable ``--days`` and the configured value therefore governs.

    The two problem fields are separate because they have different
    consequences. A line whose ``--days`` cannot be read still has a knowable
    effective lookback — the entry point swallows the parse failure and falls
    back to the config — whereas a line whose schedule cannot be read is one
    cron will not run at all. Collapsing them into one "unparseable" would lose
    that, and the whole point of this check is to say which number runs.
    """

    source: str
    mode: str
    schedule: str | None
    days: int | None
    schedule_problem: str | None
    days_problem: str | None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_schedule(tokens: list[str]) -> str | None:
    """The schedule expression at the head of *tokens*, or None if unreadable.

    ``@daily`` and friends are one token; everything else is the leading five
    fields. The user column that ``/etc/cron.d`` and ``/etc/crontab`` carry and
    a user crontab does not sits *after* those five either way, so it needs no
    special handling here.
    """
    if not tokens:
        return None
    if tokens[0].startswith("@"):
        return tokens[0]
    if len(tokens) < 6:
        return None
    fields = tokens[:5]
    if not all(_CRON_FIELD_RE.match(field) for field in fields):
        return None
    return " ".join(fields)


def _parse_days(tokens: list[str]) -> tuple[int | None, str | None]:
    """The ``--days`` value on this line, and any reason it could not be read.

    Mirrors the entry point exactly, including its limits: it looks for a bare
    ``--days`` token and takes the next one, so the first occurrence wins and
    the attached ``--days=3`` form is not seen at all. A line written that way
    runs on the *configured* value, which is worth saying out loud rather than
    reporting as a 3-day lookback nobody is getting.
    """
    if "--days" in tokens:
        index = tokens.index("--days")
        if index + 1 >= len(tokens):
            return None, "`--days` is the last token on the line, with no value after it"
        raw = tokens[index + 1]
        try:
            return int(raw), None
        except ValueError:
            return None, f"`--days {raw}` is not an integer, so the entry point ignores it"
    attached = [token for token in tokens if token.startswith("--days=")]
    if attached:
        return None, (
            f"`{attached[0]}` uses the attached form, which the entry point does "
            f"not read — it looks for `--days N` as two tokens"
        )
    return None, None


def parse_cron_line(line: str, *, source: str) -> CronEntry | None:
    """Parse one crontab line into a CronEntry, or None if it is not ours.

    Comments, blank lines and environment assignments are not jobs. Everything
    else that names the consolidation module is, and is reported even when its
    schedule cannot be read — an unparseable line is a finding, not a skip.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if CRON_MODULE not in stripped:
        return None
    if _ASSIGNMENT_RE.match(stripped):
        return None

    tokens = stripped.split()
    schedule = _parse_schedule(tokens)
    days, days_problem = _parse_days(tokens)

    return CronEntry(
        source=source,
        mode="nightly" if "--nightly" in tokens else "weekly",
        schedule=schedule,
        days=days,
        schedule_problem=(
            None if schedule else "the leading fields are not a readable cron schedule"
        ),
        days_problem=days_problem,
    )


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@dataclass
class _Sources:
    """What could be read, what could not, and what cron itself ignores."""

    texts: list[tuple[str, str]]
    searched: list[str]
    unread: list[str]
    absent: list[str]
    skipped_names: list[str]


def read_crontab(user: str | None = None) -> tuple[str | None, str, bool]:
    """``crontab -l`` output for *user*, plus a reason and an "absent" flag.

    Returns ``(text, reason, absent)``. ``absent`` distinguishes "this user has
    no crontab" — an answer — from "I was not allowed to look", which is not.
    """
    argv = ["crontab", "-l"] + (["-u", user] if user else [])
    printable = " ".join(argv)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except FileNotFoundError:
        return None, "the `crontab` command is not installed", True
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"`{printable}` did not complete ({exc!r})", False

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        first = stderr.splitlines()[0] if stderr else f"`{printable}` exited {proc.returncode}"
        # "no crontab for <user>" is the empty answer, not a refusal.
        return None, first, "no crontab for" in stderr.lower()
    return proc.stdout, "", False


def _collect_sources() -> _Sources:
    """Read every cron location that could hold a consolidation entry."""
    sources = _Sources(texts=[], searched=[], unread=[], absent=[], skipped_names=[])

    if CRON_D_DIR.is_dir():
        try:
            names = sorted(path.name for path in CRON_D_DIR.iterdir())
        except OSError as exc:
            names = []
            sources.unread.append(f"{CRON_D_DIR} could not be listed ({exc.strerror or exc})")
        else:
            sources.searched.append(f"{CRON_D_DIR} ({len(names)} file(s))")
        for name in names:
            path = CRON_D_DIR / name
            if not path.is_file():
                continue
            if not _RUN_PARTS_NAME_RE.match(name):
                sources.skipped_names.append(name)
                continue
            try:
                sources.texts.append(
                    (str(path), path.read_text(encoding="utf-8", errors="replace"))
                )
            except OSError as exc:
                sources.unread.append(f"{path} ({exc.strerror or exc})")
    else:
        sources.absent.append(f"{CRON_D_DIR} does not exist")

    if SYSTEM_CRONTAB.is_file():
        sources.searched.append(str(SYSTEM_CRONTAB))
        try:
            sources.texts.append(
                (str(SYSTEM_CRONTAB), SYSTEM_CRONTAB.read_text(encoding="utf-8", errors="replace"))
            )
        except OSError as exc:
            sources.unread.append(f"{SYSTEM_CRONTAB} ({exc.strerror or exc})")
    else:
        sources.absent.append(f"{SYSTEM_CRONTAB} does not exist")

    text, reason, absent = read_crontab()
    if text is not None:
        sources.searched.append("`crontab -l`")
        sources.texts.append(("`crontab -l`", text))
    elif absent:
        sources.absent.append(f"`crontab -l` ({reason})")
    else:
        sources.unread.append(f"`crontab -l` ({reason})")

    # Root's crontab is a place an entry can hide from a non-root doctor. Only
    # worth a second call when we are not already root.
    if getattr(os, "geteuid", None) is not None and os.geteuid() != 0:
        text, reason, absent = read_crontab("root")
        if text is not None:
            sources.searched.append("`crontab -l -u root`")
            sources.texts.append(("`crontab -l -u root`", text))
        elif absent:
            sources.absent.append(f"root's crontab ({reason})")
        else:
            sources.unread.append(f"root's crontab ({reason})")

    return sources


def _entries(sources: _Sources) -> list[CronEntry]:
    """Every consolidation cron entry across *sources*, in source order."""
    found: list[CronEntry] = []
    for label, text in sources.texts:
        for lineno, line in enumerate(text.splitlines(), start=1):
            entry = parse_cron_line(line, source=f"{label}:{lineno}")
            if entry is not None:
                found.append(entry)
    return found


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


#: What to call the number in prose, per mode. The nightly's is a catch-up
#: bound; the weekly's is still a lookback window.
_NOUN = {"nightly": "catch-up bound", "weekly": "lookback"}


def _describe(mode: str, entries: list[CronEntry], configured: int) -> tuple[str, bool]:
    """One clause describing *mode*'s effective bound/lookback, and whether it is a problem."""
    key = _CONFIG_KEY[mode]
    noun = _NOUN[mode]

    if len(entries) > 1:
        listed = ", ".join(
            f"{entry.source} (--days {entry.days if entry.days is not None else 'unset'})"
            for entry in entries
        )
        return (
            f"{mode}: {len(entries)} entries invoke the pass — {listed} — so there "
            f"is no single effective {noun}; cron runs all of them"
        ), True

    entry = entries[0]
    where = f"{entry.source} [{entry.schedule}]" if entry.schedule else entry.source

    if entry.schedule_problem:
        prefix = (
            f"{mode}: {where} — {entry.schedule_problem}, so cron will not run this "
            f"line at all; read as written it"
        )
    else:
        prefix = f"{mode}: {where}"

    if entry.days_problem:
        detail = (
            f"{entry.days_problem}, so the configured {key}={configured} governs — "
            f"effective {noun} {configured} day(s) from a line that reads as though "
            f"it set one"
        )
    elif entry.days is None:
        detail = (
            f"passes no --days, so the configured {key}={configured} governs — "
            f"effective {noun} {configured} day(s)"
        )
    elif entry.days == configured:
        detail = (
            f"runs --days {entry.days}, matching {key}={configured} — "
            f"effective {noun} {entry.days} day(s)"
        )
    else:
        detail = (
            f"runs --days {entry.days} but {key}={configured} — the cron argument "
            f"wins, so the effective {noun} is {entry.days} day(s), not {configured}"
        )

    is_problem = bool(
        entry.schedule_problem
        or entry.days_problem
        or (entry.days is not None and entry.days != configured)
    )
    return f"{prefix} {detail}", is_problem


def _not_applicable(message: str) -> CheckResult:
    return CheckResult(
        name=CHECK_NAME,
        severity="info",
        passed=True,
        message=message,
        remediation=None,
        tags=("deep",),
    )


#: Hours per day, for comparing a day-denominated catch-up bound against
#: ``auto_gate.max_hours_elapsed``, which is denominated in hours.
_HOURS_PER_DAY = 24


def _ceiling_remediation(ceiling_days: float) -> str:
    return (
        f"Raise {_CONFIG_KEY['nightly']} (or the nightly cron's `--days N`) to at "
        f"least {ceiling_days:.0f}, or lower consolidation.auto_gate."
        f"max_hours_elapsed to match, so the catch-up bound can reach as far back "
        f"as the gate may legitimately wait before firing a pass. Nothing here is "
        f"edited automatically."
    )


def _ceiling_problem(nightly_days: int, max_hours_elapsed: float) -> str | None:
    """A clause naming a nightly catch-up bound tighter than the gate's ceiling.

    ``None`` when the bound reaches at least as far back as the gate may
    legitimately wait (``consolidation.auto_gate.max_hours_elapsed``) — see
    ``NightlyConfig.lookback_days``'s own comment. Below it, a cold or
    long-failed watermark older than the bound is clamped, and the notes in
    the gap are dropped rather than caught up on the next run.
    """
    ceiling_days = max_hours_elapsed / _HOURS_PER_DAY
    if nightly_days >= ceiling_days:
        return None
    return (
        f"nightly: the {nightly_days}-day catch-up bound is tighter than the "
        f"activity gate's {ceiling_days:.0f}-day ({max_hours_elapsed:.0f} h) ceiling "
        f"(consolidation.auto_gate.max_hours_elapsed) — the gate may legitimately "
        f"wait that long before firing a pass, and a shorter bound drops the notes "
        f"it chose to wait on instead of catching them up next run"
    )


@register(tags=("deep",))
def consolidation_schedule_effective(ctx: DoctorContext) -> CheckResult:
    """Report the lookback the consolidation cron actually runs, and any drift."""
    if not sys.platform.startswith("linux"):
        return _not_applicable(
            f"Not checked on {sys.platform}: consolidation is scheduled by cron on "
            f"Linux hosts, and no palinode cron entry is shipped for this platform."
        )

    sources = _collect_sources()
    entries = _entries(sources)

    blind = f" Not read: {'; '.join(sources.unread)}." if sources.unread else ""
    # Named in both outcomes on purpose: "there is a palinode file in
    # /etc/cron.d and doctor says no entry" is exactly the moment an operator
    # needs to be told the file is one cron itself will not read.
    ignored = ""
    if sources.skipped_names:
        ignored = (
            f" Ignored in {CRON_D_DIR}, as cron itself ignores them (names outside "
            f"run-parts' [A-Za-z0-9_-] rule): {', '.join(sources.skipped_names)}."
        )

    if not entries:
        looked = ", ".join(sources.searched) if sources.searched else "nothing readable"
        missing = f" Absent: {'; '.join(sources.absent)}." if sources.absent else ""
        nightly_days = ctx.config.consolidation.nightly.lookback_days
        message = (
            f"No consolidation cron entry found — nothing invoking {CRON_MODULE} in "
            f"{looked}. Consolidation may be scheduled another way (a systemd timer, "
            f"the API, hand-run passes) or not at all; the nightly's configured "
            f"catch-up bound ({_CONFIG_KEY['nightly']}={nightly_days}) and the "
            f"weekly's configured lookback ({_CONFIG_KEY['weekly']}="
            f"{ctx.config.consolidation.lookback_days}) are what any un-argumented "
            f"run would use.{ignored}{missing}{blind}"
        )
        ceiling_problem = _ceiling_problem(
            nightly_days, ctx.config.consolidation.auto_gate.max_hours_elapsed
        )
        if ceiling_problem is None:
            return _not_applicable(message)
        return CheckResult(
            name=CHECK_NAME,
            severity="warn",
            passed=False,
            message=f"{message} {ceiling_problem}",
            remediation=_ceiling_remediation(
                ctx.config.consolidation.auto_gate.max_hours_elapsed / _HOURS_PER_DAY
            ),
            tags=("deep",),
        )

    configured = {
        "nightly": ctx.config.consolidation.nightly.lookback_days,
        "weekly": ctx.config.consolidation.lookback_days,
    }

    clauses: list[str] = []
    problems = False
    #: The nightly's single, unambiguous effective catch-up bound, for the
    #: ceiling check below. ``None`` when ambiguous (two or more entries) —
    #: that is already its own, separately reported problem.
    nightly_effective: int | None = None
    for mode in ("nightly", "weekly"):
        for_mode = [entry for entry in entries if entry.mode == mode]
        if not for_mode:
            # Not a failure: a host may schedule only one of the two passes.
            # Still worth a clause, because "doctor said nothing about weekly"
            # and "weekly is unscheduled" must not look the same.
            clauses.append(
                f"{mode}: no cron entry, so a run of that pass would use "
                f"{_CONFIG_KEY[mode]}={configured[mode]}"
            )
            if mode == "nightly":
                nightly_effective = configured[mode]
            continue
        clause, is_problem = _describe(mode, for_mode, configured[mode])
        clauses.append(clause)
        problems = problems or is_problem
        if mode == "nightly" and len(for_mode) == 1:
            entry = for_mode[0]
            nightly_effective = (
                configured[mode] if (entry.days_problem or entry.days is None) else entry.days
            )

    ceiling_problem = None
    if nightly_effective is not None:
        ceiling_problem = _ceiling_problem(
            nightly_effective, ctx.config.consolidation.auto_gate.max_hours_elapsed
        )
        if ceiling_problem is not None:
            clauses.append(ceiling_problem)
            problems = True

    message = f"Effective consolidation lookback — {'; '.join(clauses)}.{ignored}{blind}"

    if not problems:
        return CheckResult(
            name=CHECK_NAME,
            severity="info",
            passed=True,
            message=message,
            remediation=None,
            tags=("deep",),
        )

    remediation = (
        "The cron argument is what runs; the config value is what gets read by "
        "anyone reasoning about a pass after the fact, which is how a failed run "
        "gets diagnosed against a scope it never had. Converge them — doctor does "
        "not pick which side moves, because that is a decision about what the pass "
        "is for: either delete `--days N` from the cron line so palinode.config.yaml "
        "governs (one source of truth), or set the configured value to the number "
        "that actually runs. For two entries on one pass, delete the duplicate; for "
        "a line that could not be read, fix its syntax — cron will have been "
        "failing on it too. Nothing here is edited automatically."
    )
    if ceiling_problem is not None:
        remediation += " Separately: " + _ceiling_remediation(
            ctx.config.consolidation.auto_gate.max_hours_elapsed / _HOURS_PER_DAY
        )

    return CheckResult(
        name=CHECK_NAME,
        severity="warn",
        passed=False,
        message=message,
        remediation=remediation,
        tags=("deep",),
    )
