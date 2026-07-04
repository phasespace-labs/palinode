"""
Palinode Indexer Watcher

Daemon instance observing file modifications natively utilizing 
Watchdog system events. Auto-indexes Markdown memories upon disk write 
operations enforcing real-time DB synchronization boundaries.
"""
from __future__ import annotations

import time
import os
import logging

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, DirModifiedEvent, DirCreatedEvent, DirDeletedEvent

import threading
import urllib.request

from palinode.core import store, parser, embedder, cross_refs  # noqa: F401  (embedder re-exported for test patches)
from palinode.core.config import config
from palinode.indexer.index_file import index_file
import json
from datetime import UTC, datetime

logger = logging.getLogger("palinode.watcher")
logger.setLevel(logging.INFO)


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class JsonlFormatter(logging.Formatter):
    """Logging Formatter dictating a JSONL chronological schema format."""
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

os.makedirs(os.path.join(config.palinode_dir, "logs"), exist_ok=True)
fh = logging.FileHandler(os.path.join(config.palinode_dir, config.logging.operations_log))
fh.setFormatter(JsonlFormatter())
logger.addHandler(fh)


class PalinodeHandler(FileSystemEventHandler):
    """File Event System Handler wrapping Palinode embedding lifecycle hooks."""

    def __init__(self) -> None:
        """Initialize Palinode handler bounding debounce caches safely."""
        super().__init__()
        self.last_processed: dict[str, float] = {}
        self._summary_timer: threading.Timer | None = None
        self._description_timer: threading.Timer | None = None
        # Files needing description retry, accumulated between debounce ticks.
        self._description_pending: list[str] = []

    def _trigger_summaries(self) -> None:
        """Hits the summary generation API to auto-fill missing summaries."""
        logger.info("Triggering POST /generate-summaries from watcher...")
        try:
            # B310 rationale - hardcoded loopback URL to local palinode-api; no user-controlled scheme
            req = urllib.request.Request("http://127.0.0.1:6340/generate-summaries", method="POST")  # nosec B310
            urllib.request.urlopen(req, timeout=300)  # nosec B310
        except Exception as e:
            logger.warning(f"Failed to trigger /generate-summaries process: {e}")

    def _schedule_summary_generation(self) -> None:
        """Debounces the summary generation call."""
        if self._summary_timer is not None:
            self._summary_timer.cancel()
        self._summary_timer = threading.Timer(5.0, self._trigger_summaries)
        self._summary_timer.daemon = True
        self._summary_timer.start()

    def _fill_pending_descriptions(self) -> None:
        """Re-call description generation for files that had it deferred.

        #336: watcher-retry half of the graceful-degrade design. After the
        description-generation timeout during /save, the watcher detects files
        still missing their description field and calls /generate-summaries
        (which triggers the API to fill any missing descriptions) or directly
        re-saves description for each file.

        Runs from a debounced timer after file events. Logs WARNING if Ollama
        is still unavailable so the operator can correlate.
        """
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
        10 s of inactivity so a batch of file events doesn't hammer the API.
        """
        self._description_pending.append(filepath)
        if self._description_timer is not None:
            self._description_timer.cancel()
        self._description_timer = threading.Timer(10.0, self._fill_pending_descriptions)
        self._description_timer.daemon = True
        self._description_timer.start()
        
    def is_valid_file(self, path: str) -> bool:
        """Deduce runtime viability for system memory ingestion boundaries.

        Ignores system/cache directories natively preventing excessive overhead.

        Args:
            path (str): Target disk path.

        Returns:
            bool: Validity criteria.
        """
        if not path.endswith('.md'):
            return False
            
        ignore_patterns = [
            '/.git/', '/logs/', '/.palinode.db', '/venv/', 
            '/node_modules/', '/__pycache__/', '/palinode.egg-info/', 
            '.db-journal', '.db-wal', '.db-shm', '/inbox/processed/'
        ]
        if any(p in path for p in ignore_patterns):
            return False
            
        return True

    def _process_file(self, filepath: str) -> None:
        """Parses generic filesystem hits driving vector embeddings chunks securely.

        Args:
            filepath (str): Evaluated system path triggering event cycles.
        """
        if not os.path.exists(filepath):
            return
            
        current_time = time.time()
        debounce_window = config.services.watcher.debounce_seconds
        
        if filepath in self.last_processed and (current_time - self.last_processed[filepath]) < debounce_window:
            return
        self.last_processed[filepath] = current_time

        try:
            with open(filepath, 'r') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read {filepath}: {e}")
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
        """Hook triggered implicitly by watchdog native listener.

        Args:
            event: Generic OS system notification block.
        """
        if not event.is_directory and self.is_valid_file(event.src_path):
            try:
                self._process_file(event.src_path)
            except Exception as e:
                logger.error(f"Failed to index {event.src_path}: {e}")

    def on_created(self, event: FileCreatedEvent | DirCreatedEvent) -> None:
        """Hook triggered explicitly upon physical file creations natively.

        Args:
            event: Generic OS system notification block.
        """
        if not event.is_directory and self.is_valid_file(event.src_path):
            try:
                self._process_file(event.src_path)
            except Exception as e:
                logger.error(f"Failed to index {event.src_path}: {e}")

    def on_deleted(self, event: FileDeletedEvent | DirDeletedEvent) -> None:
        """Safely remove system traces preventing ghost responses inside API chunks.

        Args:
            event: Generic OS FileEvent.
        """
        if not event.is_directory and self.is_valid_file(event.src_path):
            try:
                logger.info(f"Deleting chunks for: {event.src_path}")
                store.delete_file_chunks(event.src_path)
            except Exception as e:
                logger.error(f"Failed to delete chunks for {event.src_path}: {e}")


def main() -> None:
    """Invokes endless watcher queue loop safely preventing application exit."""
    store.init_db()
    event_handler = PalinodeHandler()
    observer = Observer()
    
    os.makedirs(config.palinode_dir, exist_ok=True)
    
    observer.schedule(event_handler, config.palinode_dir, recursive=True)
    observer.start()
    logger.info(f"Watching {config.palinode_dir} for changes...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
