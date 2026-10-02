"""The watcher debounce must *coalesce* a write burst, not drop its tail.

Before: an event inside ``debounce_seconds`` of the last processed event for
the same path was ignored outright, so ``save`` -> ``cross_refs`` rewrite (well
inside 1 s) left the pre-rewrite body in the index until the file was touched
again. Now each path arms a trailing-edge timer that re-reads the file when it
fires; a new event cancels and re-arms it.

Real SQLite and handler; controlled timers pin coalescing independently of
runner speed. A separate real Observer test covers watchdog delivery.
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import patch

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent
from watchdog.observers import Observer

from palinode.core import store
from palinode.core.config import config
from palinode.indexer import watcher

_VEC = [0.04] * 1024
_DEBOUNCE_S = 0.3


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))
    monkeypatch.setattr(config.git, "auto_commit", False)
    monkeypatch.setattr(config.capture.cross_refs, "enabled", False)
    monkeypatch.setattr(config.auto_summary, "enabled", False)
    monkeypatch.setattr(config.services.watcher, "debounce_seconds", _DEBOUNCE_S)
    store.init_db()
    (tmp_path / "projects").mkdir()
    return tmp_path


def _bodies() -> list[str]:
    db = store.get_db()
    rows = db.execute("SELECT content FROM chunks").fetchall()
    db.close()
    return [r["content"] for r in rows]


def _wait_until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _doc(body: str) -> str:
    return f"---\nid: proj-x\ncategory: projects\n---\n\n# X\n\n{body}\n"


class ControlledTimer:
    """A timer whose deadline is advanced explicitly, including late callbacks."""

    def __init__(self, interval, function, args=(), kwargs=None):
        self.name = "controlled-index-timer"
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def join(self, timeout=None) -> None:
        pass

    def fire(self) -> None:
        assert self.started
        # Deliberately allow cancelled callbacks: Timer.cancel cannot retract
        # a callback that has already started or is waiting on the index lock.
        with patch.object(threading, "current_thread", return_value=self):
            self.function(*self.args, **self.kwargs)


@pytest.fixture()
def controlled_timers(monkeypatch):
    monkeypatch.setattr(watcher.threading, "Timer", ControlledTimer)


def test_second_write_inside_window_is_what_gets_indexed(tmp_store, controlled_timers):
    """Two events before the controlled deadline produce one trailing pass."""
    path = tmp_store / "projects" / "x.md"
    handler = watcher.PalinodeHandler()
    try:
        with patch("palinode.core.embedder.embed", return_value=_VEC) as embed:
            path.write_text(_doc("FIRST body, must not win."))
            handler.on_created(FileCreatedEvent(str(path)))
            first = handler._index_timers[str(path)]
            path.write_text(_doc("SECOND body, the trailing edge."))
            handler.on_modified(FileModifiedEvent(str(path)))
            trailing = handler._index_timers[str(path)]
            assert first.cancelled
            assert trailing is not first
            assert trailing.interval == _DEBOUNCE_S
            assert _bodies() == []
            assert list(handler._index_timers) == [str(path)]

            first.fire()
            assert _bodies() == []
            assert handler._index_timers[str(path)] is trailing
            trailing.fire()

        assert any("SECOND body" in b for b in _bodies()), _bodies()
        assert not any("FIRST body" in b for b in _bodies()), _bodies()
        assert embed.call_count == 1
        assert handler._index_timers == {}
    finally:
        handler.shutdown()


@pytest.mark.parametrize("finish_write", [True, False])
def test_zero_byte_read_gets_one_retry(tmp_store, controlled_timers, finish_write):
    """Pause a real truncate/write; retain good rows until the bounded retry."""
    path = tmp_store / "projects" / "x.md"
    handler = watcher.PalinodeHandler()
    try:
        with patch("palinode.core.embedder.embed", return_value=_VEC) as embed:
            path.write_text(_doc("previous good body"))
            handler._process_file(str(path))
            before = _bodies()
            with path.open("w") as writer:
                handler.on_modified(FileModifiedEvent(str(path)))
                handler._index_timers[str(path)].fire()
                assert _bodies() == before
                assert embed.call_count == 1
                retry = handler._index_timers[str(path)]
                if finish_write:
                    writer.write(_doc("completed body"))
                    writer.flush()
            # No second event is needed for recovery. A genuinely empty file
            # also completes here rather than preserving stale rows forever.
            retry.fire()
            assert handler._index_timers == {}
            assert not any("previous good body" in b for b in _bodies())
            if finish_write:
                assert any("completed body" in b for b in _bodies()), _bodies()
            else:
                # A file with no current text gets no row at all — an empty
                # chunk is not a retrievable hit, it is just a row FTS indexes
                # nothing for. (It used to be written as one empty body.)
                assert _bodies() == []
    finally:
        handler.shutdown()


def test_timer_entry_tracks_inflight_work(tmp_store, controlled_timers):
    handler = watcher.PalinodeHandler()
    path = str(tmp_store / "projects" / "x.md")
    try:
        handler._schedule_index(path)
        timer = handler._index_timers[path]

        def process(filepath, **kwargs):
            assert handler._index_timers[filepath] is timer
            handler._schedule_index(filepath)

        with patch.object(handler, "_process_file", side_effect=process):
            timer.fire()
        assert handler._index_timers[path] is not timer
        assert timer.cancelled
    finally:
        handler.shutdown()


@pytest.mark.parametrize("stop", [False, True])
def test_callback_waiting_for_index_lock_is_rechecked(tmp_store, monkeypatch, stop):
    """A real timer starts, waits for the writer, then loses ownership."""
    entered = threading.Event()
    release = threading.Event()

    class GatedLock:
        def __enter__(self):
            entered.set()
            assert release.wait(5), "test did not release the index lock"

        def __exit__(self, *args):
            pass

    handler = watcher.PalinodeHandler()
    monkeypatch.setattr(handler, "_index_lock", GatedLock())
    monkeypatch.setattr(config.services.watcher, "debounce_seconds", 0)
    path = str(tmp_store / "projects" / "x.md")
    timer = None
    try:
        with patch.object(handler, "_process_file") as process:
            handler._schedule_index(path)
            assert entered.wait(5), "timer never reached the index lock"
            timer = handler._index_timers[path]
            if stop:
                handler.shutdown(timeout=0)
            else:
                monkeypatch.setattr(watcher.threading, "Timer", ControlledTimer)
                handler._schedule_index(path)
                trailing = handler._index_timers[path]
            release.set()
            timer.join(5)
            assert not timer.is_alive()
            process.assert_not_called()
            if not stop:
                assert handler._index_timers[path] is trailing
    finally:
        release.set()
        if timer is not None:
            timer.join(5)
        handler.shutdown()


def test_index_failure_is_logged_and_timer_retired(tmp_store, controlled_timers, caplog):
    handler = watcher.PalinodeHandler()
    path = str(tmp_store / "projects" / "x.md")
    try:
        handler._schedule_index(path)
        with patch.object(handler, "_process_file", side_effect=RuntimeError("broken index")):
            handler._index_timers[path].fire()
        assert "Failed to index" in caplog.text
        assert "broken index" in caplog.text
        assert handler._index_timers == {}
    finally:
        handler.shutdown()


def test_real_observer_indexes_final_content(tmp_store):
    """Real OS delivery converges after a later edit; no coalescing deadline claim."""
    root = os.path.realpath(str(tmp_store))
    path = os.path.join(root, "projects", "obs.md")
    handler = watcher.PalinodeHandler()
    observer = Observer()
    observer.schedule(handler, root, recursive=True)
    try:
        with patch("palinode.core.embedder.embed", return_value=_VEC):
            observer.start()
            with open(path, "w") as fh:
                fh.write(_doc("FIRST via observer."))
            assert _wait_until(
                lambda: any("FIRST via observer" in b for b in _bodies()),
                timeout=10.0,
            ), _bodies()
            with open(path, "w") as fh:
                fh.write(_doc("SECOND via observer."))

            assert _wait_until(
                lambda: any("SECOND via observer" in b for b in _bodies()),
                timeout=10.0,
            ), _bodies()
            observer.stop()
            observer.join(5)
            assert not observer.is_alive()
            assert _wait_until(lambda: not handler._index_timers, timeout=5.0)
        bodies = _bodies()
        assert not any("FIRST via observer" in b for b in bodies), bodies
    finally:
        observer.stop()
        handler.shutdown()
        observer.join(5)


def test_shutdown_cancels_pending_index_timers(tmp_store, controlled_timers):
    """Shutdown cancels armed per-path timers and refuses new ones."""
    handler = watcher.PalinodeHandler()
    a = tmp_store / "projects" / "a.md"
    b = tmp_store / "projects" / "b.md"
    a.write_text(_doc("a"))
    b.write_text(_doc("b"))
    handler.on_created(FileCreatedEvent(str(a)))
    handler.on_created(FileCreatedEvent(str(b)))
    assert set(handler._index_timers) == {str(a), str(b)}
    timers = list(handler._index_timers.values())

    handler.shutdown()

    assert handler._index_timers == {}
    assert all(timer.cancelled for timer in timers)
    for timer in timers:
        timer.fire()
    # A late event on a stopped handler is a no-op.
    handler.on_modified(FileModifiedEvent(str(a)))
    assert handler._index_timers == {}
    assert _bodies() == []
