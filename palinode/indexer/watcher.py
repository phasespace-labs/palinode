"""
Palinode Indexer Watcher

Watches the palinode directory with Watchdog and indexes Markdown files
as they are written, keeping the database in sync with disk.
"""
from __future__ import annotations

import atexit
import time
import os
import logging
import weakref

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, FileMovedEvent, DirModifiedEvent, DirCreatedEvent, DirDeletedEvent, DirMovedEvent

import threading
import urllib.request

from palinode.core import store, parser, embedder, cross_refs  # noqa: F401  (embedder re-exported for test patches)
from palinode.core.config import config
from palinode.core.log_files import LazyDirFileHandler
from palinode.core.skip_dirs import is_skipped_path
from palinode.indexer.index_file import index_file
from palinode.indexer.watcher_identity import remove_identity, write_identity
import json
from datetime import UTC, datetime

logger = logging.getLogger("palinode.watcher")
logger.setLevel(logging.INFO)

# Debounce windows for the two background retry paths. Named (rather than magic
# numbers at the call sites) so shutdown tests can shorten them deterministically.
SUMMARY_DEBOUNCE_S = 5.0
DESCRIPTION_DEBOUNCE_S = 10.0

# How long ``shutdown()`` waits for a debounce timer thread to exit. A cancelled
# timer that has not fired yet exits immediately; one already inside its blocking
# urlopen cannot be interrupted, so the wait is bounded rather than indefinite.
SHUTDOWN_JOIN_TIMEOUT_S = 2.0


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class JsonlFormatter(logging.Formatter):
    """Logging formatter that emits one JSON object per record (JSONL)."""
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": _utc_now().isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage()
        })


sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logger.addHandler(sh)

# Directory and file are created on the first record, not at import (see
# palinode.core.log_files).
fh = LazyDirFileHandler(os.path.join(config.palinode_dir, config.logging.operations_log))
fh.setFormatter(JsonlFormatter())
logger.addHandler(fh)


# Every live handler, so its debounce timers can be stopped deterministically at
# process exit. Weak references: registration must never be the thing keeping a
# handler alive. A *pending* timer holds a strong reference to its handler
# through the bound method, so anything worth cancelling is still reachable here.
_live_handlers: weakref.WeakSet[PalinodeHandler] = weakref.WeakSet()
_live_handlers_lock = threading.Lock()


def shutdown_handlers(timeout: float = SHUTDOWN_JOIN_TIMEOUT_S) -> None:
    """Stop every live handler's debounce timers and wait for their threads.

    Registered with ``atexit`` so no watcher timer is still running when the
    interpreter finalizes. That was the abort: a daemon timer holding the
    stderr buffer lock while the main thread flushed it produced
    ``_enter_buffered_busy... at interpreter shutdown`` (SIGABRT, exit 134)
    *after* every test had already passed.

    Idempotent and safe to call from anywhere (tests call it at session end).
    """
    with _live_handlers_lock:
        handlers = list(_live_handlers)
    for handler in handlers:
        handler.shutdown(timeout=timeout)


atexit.register(shutdown_handlers)


class PalinodeHandler(FileSystemEventHandler):
    """Watchdog event handler that re-indexes changed Markdown files and
schedules summary and description generation."""

    def __init__(self) -> None:
        """Initialise debounce timers and per-file tracking state."""
        super().__init__()
        # Per-path trailing-edge debounce. A file event arms (or re-arms)
        # a timer keyed by path; the timer re-reads the file when it fires, so a
        # burst of writes inside ``debounce_seconds`` is coalesced into one index
        # pass over the *final* content instead of the first write winning and
        # the rest being dropped. Entries are evicted when the pass finishes, so
        # the dict is bounded by the number of paths currently in flight.
        self._index_timers: dict[str, threading.Timer] = {}
        self._index_timers_lock = threading.Lock()
        # Timers fire on their own threads; serialize the index passes so the
        # store sees the same one-writer-at-a-time pattern the observer thread
        # used to give it.
        self._index_lock = threading.Lock()
        self._summary_timer: threading.Timer | None = None
        self._description_timer: threading.Timer | None = None
        # Files needing description retry, accumulated between debounce ticks.
        self._description_pending: list[str] = []
        self._stopped = False
        with _live_handlers_lock:
            _live_handlers.add(self)

    def shutdown(self, timeout: float = SHUTDOWN_JOIN_TIMEOUT_S) -> None:
        """Cancel pending debounce timers and wait for their threads to exit.

        Idempotent. After this the handler refuses to arm new timers, so a
        late file event on a shut-down handler is a no-op rather than a
        resurrected background thread.
        """
        self._stopped = True
        for attr in ("_summary_timer", "_description_timer"):
            timer: threading.Timer | None = getattr(self, attr)
            if timer is None:
                continue
            setattr(self, attr, None)
            timer.cancel()
            timer.join(timeout)
        with self._index_timers_lock:
            index_timers = list(self._index_timers.values())
            self._index_timers.clear()
        for timer in index_timers:
            timer.cancel()
            timer.join(timeout)
        self._description_pending.clear()

    def _schedule_index(self, filepath: str, *, retry_empty: bool = True) -> None:
        """Arm (or re-arm) the per-path index timer for *filepath*.

        Cancel-and-reschedule: a new event inside the debounce window pushes the
        fire time out, so only the trailing edge of a write burst is indexed.
        """
        if self._stopped:
            return
        delay = config.services.watcher.debounce_seconds
        with self._index_timers_lock:
            if self._stopped:
                return
            previous = self._index_timers.get(filepath)
            if previous is not None:
                previous.cancel()
            timer = threading.Timer(
                delay, self._fire_index, args=(filepath,),
                kwargs={"retry_empty": retry_empty},
            )
            timer.daemon = True
            self._index_timers[filepath] = timer
            timer.start()

    def _fire_index(self, filepath: str, *, retry_empty: bool = True) -> None:
        """Index only the current timer's work, then retire its entry."""
        with self._index_lock:
            # cancel() cannot stop a callback already waiting for this lock.
            with self._index_timers_lock:
                if self._stopped or self._index_timers.get(filepath) is not threading.current_thread():
                    return
            try:
                self._process_file(filepath, retry_empty=retry_empty)
            except Exception as e:
                logger.error(f"Failed to index {filepath}: {e}")
            finally:
                with self._index_timers_lock:
                    if self._index_timers.get(filepath) is threading.current_thread():
                        del self._index_timers[filepath]

    def _trigger_summaries(self) -> None:
        """Hits the summary generation API to auto-fill missing summaries."""
        if self._stopped:
            return
        logger.info("Triggering POST /generate-summaries from watcher...")
        try:
            # B310 rationale - hardcoded loopback URL to local palinode-api; no user-controlled scheme
            req = urllib.request.Request("http://127.0.0.1:6340/generate-summaries", method="POST")  # nosec B310
            urllib.request.urlopen(req, timeout=300)  # nosec B310
        except Exception as e:
            logger.warning(f"Failed to trigger /generate-summaries process: {e}")

    def _schedule_summary_generation(self) -> None:
        """Debounces the summary generation call."""
        if self._stopped:
            return
        if self._summary_timer is not None:
            self._summary_timer.cancel()
        self._summary_timer = threading.Timer(SUMMARY_DEBOUNCE_S, self._trigger_summaries)
        self._summary_timer.daemon = True
        self._summary_timer.start()

    def _fill_pending_descriptions(self) -> None:
        """Re-call description generation for files that had it deferred.

        the graceful-degrade auto-description work: watcher-retry half of the
        graceful-degrade design. After the description-generation timeout during /save,
        the watcher detects files still missing their description field and calls
        /generate-summaries (which triggers the API to fill any missing descriptions) or
        directly re-saves description for each file.

        Runs from a debounced timer after file events. Logs WARNING if Ollama
        is still unavailable so the operator can correlate.
        """
        if self._stopped:
            return
        pending = list(self._description_pending)
        self._description_pending.clear()
        if not pending:
            return
        logger.info(
            "Watcher: retrying deferred descriptions for %d file(s): %s",
            len(pending), [os.path.basename(f) for f in pending[:5]],
        )
        # Trigger /generate-summaries which has a walk-all-pending-files path.
        # This is the same mechanism used by the summary retry — reuse it so
        # there's one code path for "fill missing LLM metadata".
        try:
            api_port = config.services.api.port
            api_host = config.services.api.host
            # B310 rationale: loopback URL derived from config, not user input
            fill_req = urllib.request.Request(  # nosec B310
                f"http://{api_host}:{api_port}/generate-summaries",
                method="POST",
            )
            urllib.request.urlopen(fill_req, timeout=120)  # nosec B310
            logger.info("Watcher: description retry POST /generate-summaries completed")
        except Exception as e:
            logger.warning(
                "Watcher: description retry failed (will retry on next event): %s", e
            )

    def _schedule_description_fill(self, filepath: str) -> None:
        """Debounce description fill calls across multiple rapid file events.

        Accumulates filepaths; fires _fill_pending_descriptions once after
        DESCRIPTION_DEBOUNCE_S of inactivity so a batch of file events doesn't
        hammer the API.
        """
        if self._stopped:
            return
        self._description_pending.append(filepath)
        if self._description_timer is not None:
            self._description_timer.cancel()
        self._description_timer = threading.Timer(
            DESCRIPTION_DEBOUNCE_S, self._fill_pending_descriptions
        )
        self._description_timer.daemon = True
        self._description_timer.start()
        
    def is_valid_file(self, path: str) -> bool:
        """Return True if *path* is a Markdown file the index holds.

        The shared rule, :func:`palinode.core.skip_dirs.is_skipped_path`, reads the
        path relative to the store root, so a store under ``venv/`` or a dot folder
        still indexes. ``/reindex`` filters through this predicate and its GC pass
        drops every indexed path the walk no longer yields.

        Args:
            path (str): Path to check.

        Returns:
            bool: Whether the file should be indexed.
        """
        if not path.endswith('.md'):
            return False

        try:
            rel = os.path.relpath(path, config.palinode_dir)
        except ValueError:  # different drive on Windows — not under the store
            rel = ""
        if rel and not rel.startswith(os.pardir + os.sep) and is_skipped_path(rel):
            return False

        return True

    def _process_file(self, filepath: str, *, retry_empty: bool = False) -> None:
        """Index a Markdown file, then schedule summary/description generation if needed.

        Synchronous and undebounced — this is the "index it now" primitive that
        ``/reindex`` and the debounce timers call. File events go through
        ``_schedule_index`` instead.

        Args:
            filepath (str): Path of the file to index.
        """
        if not os.path.exists(filepath):
            return

        try:
            with open(filepath, 'r', encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read {filepath}: {e}")
            return

        if not content and retry_empty:
            # A non-atomic writer can pause between truncate and write. Keep
            # the previous index for one more debounce window, then re-open
            # the path. A persistently empty file must still reconcile: disk
            # remains authoritative, and retries must not run forever.
            logger.info("Deferring zero-byte read for one debounce window: %s", filepath)
            self._schedule_index(filepath, retry_empty=False)
            return

        logger.info(f"Indexing: {filepath}")

        # Delegate to the shared indexer helper. Note that the helper now
        # also re-embeds rows whose ``content_hash`` matches but whose FTS
        # / vec0 entries are missing — defense-in-depth against silent
        # index loss.
        outcome = index_file(filepath, content=content)
        logger.info(
            "Indexed %d new, %d re-embedded (missing index), %d unchanged, %d deleted (%s)",
            outcome["chunks_written"],
            outcome["chunks_reembedded"],
            outcome["chunks_unchanged"],
            outcome["chunks_deleted"],
            filepath,
        )
        if outcome.get("error"):
            logger.warning(
                "Index pass for %s reported: %s", filepath, outcome["error"]
            )

        # mechanical untyped cross-linking. Post-index hook — scans the
        # file's body for mentions of other memories and records them in a
        # `cross_refs` frontmatter list. Idempotent: only rewrites/commits when
        # the refs actually change, so the watcher re-processing its own write
        # terminates after one pass. A failure here is non-fatal to indexing.
        if config.capture.cross_refs.enabled:
            try:
                xref = cross_refs.update_file_cross_refs(filepath, content=content)
                if xref.get("changed"):
                    logger.info(
                        "cross_refs updated %s (%d refs)", filepath, len(xref["refs"])
                    )
                elif xref.get("error"):
                    logger.warning(
                        "cross_refs pass for %s reported: %s", filepath, xref["error"]
                    )
            except Exception as e:
                logger.warning("cross_refs pass failed for %s: %r", filepath, e)

        if config.search.retrieval_mode == "lexical":
            return

        # Re-parse for metadata so we can decide whether to schedule summary
        # generation. (Cheap — no embedder call.)
        metadata, _ = parser.parse_markdown(content)

        # Trigger retroactive summary generation if file is core but lacks a summary
        if metadata.get("core") is True and not metadata.get("summary"):
            logger.info(f"File {filepath} has core:true but lacks summary. Scheduling generation...")
            self._schedule_summary_generation()

        # trigger description fill for files missing a description. The
        # auto-description is now always deferred off the /save hot path (
        # extending the timeout path), so the watcher is the normal route
        # for descriptions to land, not just the timeout-retry route. Gated on
        # auto_summary.enabled — the master switch for all LLM enrichment — so
        # disabling it stops description scheduling too.
        if config.auto_summary.enabled and not metadata.get("description"):
            logger.debug(
                "Watcher: %s has no description — scheduling deferred description fill",
                os.path.basename(filepath),
            )
            self._schedule_description_fill(filepath)

    def on_modified(self, event: FileModifiedEvent | DirModifiedEvent) -> None:
        """Re-index the file when it is modified; directories are ignored.

        Args:
            event: Watchdog modification event.
        """
        if not event.is_directory and self.is_valid_file(event.src_path):
            self._schedule_index(event.src_path)

    def on_created(self, event: FileCreatedEvent | DirCreatedEvent) -> None:
        """Index newly created files; directories are ignored.

        Args:
            event: Watchdog creation event.
        """
        if not event.is_directory and self.is_valid_file(event.src_path):
            self._schedule_index(event.src_path)

    def on_deleted(self, event: FileDeletedEvent | DirDeletedEvent) -> None:
        """Delete the file's chunks from the store when it is removed.

        Args:
            event: Watchdog deletion event.
        """
        if not event.is_directory and self.is_valid_file(event.src_path):
            try:
                logger.info(f"Deleting chunks for: {event.src_path}")
                store.delete_file_chunks(event.src_path)
            except Exception as e:
                logger.error(f"Failed to delete chunks for {event.src_path}: {e}")

    def on_moved(self, event: FileMovedEvent | DirMovedEvent) -> None:
        """Reconcile a rename: retire the old path, index the new one.

        Without this, a ``mv`` (which watchdog delivers here, not to
        ``on_modified``) was dropped silently: chunk ids are derived from the
        path, so every chunk of the moved file orphaned under the old path, and
        its entity rows survived too. Deleting the source's derived state
        and indexing the destination makes a rename a first-class edit.
        """
        if event.is_directory:
            return
        if self.is_valid_file(event.src_path):
            try:
                logger.info(f"Renamed away, deleting chunks for: {event.src_path}")
                store.delete_file_chunks(event.src_path)
            except Exception as e:
                logger.error(
                    f"Failed to delete chunks for moved {event.src_path}: {e}"
                )
        if self.is_valid_file(event.dest_path):
            self._schedule_index(event.dest_path)


def main() -> None:
    """Initialise the database, start the observer, and block until interrupted."""
    store.init_db()
    event_handler = PalinodeHandler()
    observer = Observer()
    
    os.makedirs(config.palinode_dir, exist_ok=True)
    
    observer.schedule(event_handler, config.palinode_dir, recursive=True)
    observer.start()
    identity = write_identity()
    logger.info(f"Watching {config.palinode_dir} for changes...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        # Stop the observer *and* the handler's debounce timers before joining,
        # so Ctrl-C exits promptly instead of leaving a 10 s timer to fire into
        # a half-torn-down interpreter.
        observer.stop()
        event_handler.shutdown()
        observer.join()
        remove_identity(identity)


if __name__ == "__main__":
    main()
