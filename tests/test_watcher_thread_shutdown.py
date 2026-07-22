"""Deterministic shutdown of the watcher's debounce timers (#677).

The CI signature was ``unit-tests`` aborting with exit 134 *after* the whole
suite passed:

    2109 passed, 1 skipped, 4 xfailed in 50.46s
    ValueError: I/O operation on closed file.   (x several)
    Fatal Python error: _enter_buffered_busy: could not acquire lock for
      <_io.BufferedWriter name='<stderr>'> at interpreter shutdown,
      possibly due to daemon threads

Root cause: ``PalinodeHandler._schedule_description_fill`` arms a 10 s daemon
``threading.Timer``. ``POST /reindex`` builds a fresh handler per request and
several tests drive it, so the suite ends with timers still armed; when one
fired during interpreter finalization while holding the stderr buffer lock, the
process aborted.

The invariant these tests pin: *no watcher timer thread outlives the process*.
That is the necessary condition for the abort, and unlike the abort itself it
is deterministic to assert.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

from palinode.indexer import watcher


def _watcher_timer_threads(
    owners: list[watcher.PalinodeHandler] | None = None,
) -> list[threading.Thread]:
    """Live ``threading.Timer`` threads whose callback belongs to the watcher.

    ``owners`` narrows the result to timers armed by specific handlers. The
    whole-suite view is shared state — ``POST /reindex`` tests elsewhere leave
    their own timers armed — so per-handler assertions must not depend on test
    ordering.
    """
    out = []
    for thread in threading.enumerate():
        fn = getattr(thread, "function", None)
        owner = getattr(fn, "__self__", None)
        if not isinstance(owner, watcher.PalinodeHandler):
            continue
        if owners is not None and not any(owner is o for o in owners):
            continue
        out.append(thread)
    return out


def test_shutdown_cancels_both_debounce_timers() -> None:
    handler = watcher.PalinodeHandler()
    handler._schedule_description_fill("/nonexistent/a.md")
    handler._schedule_summary_generation()

    assert handler._description_timer is not None
    assert handler._summary_timer is not None
    assert len(_watcher_timer_threads([handler])) == 2

    handler.shutdown()

    assert handler._description_timer is None
    assert handler._summary_timer is None
    assert _watcher_timer_threads([handler]) == []


def test_shutdown_is_idempotent_and_refuses_to_rearm() -> None:
    handler = watcher.PalinodeHandler()
    handler._schedule_description_fill("/nonexistent/a.md")
    handler.shutdown()
    handler.shutdown()  # must not raise

    handler._schedule_description_fill("/nonexistent/b.md")
    handler._schedule_summary_generation()

    assert handler._description_timer is None
    assert handler._summary_timer is None
    assert _watcher_timer_threads([handler]) == []


def test_stopped_handler_callbacks_are_noops() -> None:
    """A timer that fires between ``cancel()`` and thread exit does nothing."""
    handler = watcher.PalinodeHandler()
    handler._description_pending.append("/nonexistent/a.md")
    handler.shutdown()

    # No urlopen, no log write — a stopped handler's callbacks return early.
    handler._fill_pending_descriptions()
    handler._trigger_summaries()


def test_shutdown_handlers_stops_every_live_handler() -> None:
    handlers = [watcher.PalinodeHandler() for _ in range(3)]
    for handler in handlers:
        handler._schedule_description_fill("/nonexistent/a.md")
    assert len(_watcher_timer_threads(handlers)) == 3

    watcher.shutdown_handlers()

    # Global, not per-handler: shutdown_handlers is what the conftest session
    # hook and the atexit hook call, and it must leave *nothing* armed.
    assert _watcher_timer_threads() == []
    assert all(h._stopped for h in handlers)


_ATEXIT_PROBE = textwrap.dedent(
    """
    import atexit, json, sys, threading

    result_path = sys.argv[1]

    def _report():
        # atexit is LIFO, and this is registered *before* palinode.indexer.watcher
        # imports (and registers its own hook), so this runs last — after the
        # watcher has had its chance to stop its timers.
        names = [
            t.name for t in threading.enumerate()
            if getattr(getattr(t, "function", None), "__self__", None).__class__.__name__
            == "PalinodeHandler"
        ]
        with open(result_path, "w") as fh:
            json.dump(names, fh)

    atexit.register(_report)

    from palinode.indexer import watcher

    handler = watcher.PalinodeHandler()
    handler._schedule_description_fill("/nonexistent/a.md")
    handler._schedule_summary_generation()
    """
)


def test_process_exit_leaves_no_watcher_timer_threads(tmp_path: Path) -> None:
    """End-to-end: a process with armed timers exits cleanly and promptly.

    This is the #677 guard. Before the fix the child exits with two daemon
    timers still armed (the abort is a race on top of that); after it, the
    ``atexit`` hook has stopped them by the time the interpreter finalizes.
    """
    script = tmp_path / "probe.py"
    script.write_text(_ATEXIT_PROBE)
    result_path = tmp_path / "threads.json"

    # Pin the child to the same tree this test imported, not whatever copy of
    # palinode happens to be on the child's default sys.path.
    repo_root = Path(watcher.__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo_root)}

    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(script), str(result_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, (
        f"child exited {proc.returncode} (134/-6 == the #677 abort)\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    surviving = json.loads(result_path.read_text())
    assert surviving == [], f"watcher timer threads outlived the process: {surviving}"
    # The debounce windows are 5 s and 10 s; a clean shutdown cancels rather
    # than waits them out.
    assert elapsed < watcher.SUMMARY_DEBOUNCE_S, f"exit took {elapsed:.1f}s"
