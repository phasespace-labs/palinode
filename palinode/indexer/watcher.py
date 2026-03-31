"""
Palinode Indexer Watcher

Daemon instance observing file modifications natively utilizing 
Watchdog system events. Auto-indexes Markdown memories upon disk write 
operations enforcing real-time DB synchronization boundaries.
"""
from __future__ import annotations

import time
import os
import hashlib
import logging
from typing import Any

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, DirModifiedEvent, DirCreatedEvent, DirDeletedEvent

import threading
import urllib.request

from palinode.core import store, embedder, parser
from palinode.core.config import config
import json
from datetime import datetime

logger = logging.getLogger("palinode.watcher")
logger.setLevel(logging.INFO)


class JsonlFormatter(logging.Formatter):
    """Logging Formatter dictating a JSONL chronological schema format."""
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.utcnow().isoformat() + "Z",
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
        
    def _trigger_summaries(self) -> None:
        """Hits the summary generation API to auto-fill missing summaries."""
        logger.info("Triggering POST /generate-summaries from watcher...")
        try:
            req = urllib.request.Request("http://127.0.0.1:6340/generate-summaries", method="POST")
            urllib.request.urlopen(req, timeout=300)
        except Exception as e:
            logger.warning(f"Failed to trigger /generate-summaries process: {e}")

    def _schedule_summary_generation(self) -> None:
        """Debounces the summary generation call."""
        if self._summary_timer is not None:
            self._summary_timer.cancel()
        self._summary_timer = threading.Timer(5.0, self._trigger_summaries)
        self._summary_timer.daemon = True
        self._summary_timer.start()
        
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
        metadata, sections = parser.parse_markdown(content)
        category = metadata.get('category', os.path.basename(os.path.dirname(filepath)))
        
        chunks = []
        skipped = 0
        valid_chunk_ids = []
        for sec in sections:
            chunk_id = hashlib.md5(f"{filepath}#{sec['section_id']}".encode()).hexdigest()
            valid_chunk_ids.append(chunk_id)
            content_hash = hashlib.sha256(sec["content"].encode()).hexdigest()

            # Check if content has changed before calling embedder
            db = store.get_db()
            existing = db.execute(
                "SELECT content_hash FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            db.close()

            if existing and existing["content_hash"] == content_hash:
                skipped += 1
                continue  # Content unchanged — skip embedding call entirely

            emb = embedder.embed(sec["content"])
            if not emb:
                continue
                
            chunks.append({
                "id": chunk_id,
                "file_path": filepath,
                "section_id": sec["section_id"],
                "category": category,
                "content": sec["content"],
                "metadata": metadata,
                "created_at": metadata.get("created", ""),
                "last_updated": metadata.get("last_updated", ""),
                "embedding": emb
            })
            
        db = store.get_db()
        cursor = db.cursor()
        cursor.execute("SELECT id FROM chunks WHERE file_path = ?", (filepath,))
        existing_ids = [row["id"] for row in cursor.fetchall()]
        
        to_delete = [cid for cid in existing_ids if cid not in valid_chunk_ids]
        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            for cid in to_delete:
                try:
                    cursor.execute("DELETE FROM chunks_fts WHERE rowid = (SELECT rowid FROM chunks WHERE id = ?)", (cid,))
                except Exception:
                    pass
            cursor.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", to_delete)
            cursor.execute(f"DELETE FROM chunks_vec WHERE id IN ({placeholders})", to_delete)
            db.commit()
        db.close()
            
        if chunks:
            store.upsert_chunks(chunks, skip_unchanged=False)
            logger.info(f"Indexed {len(chunks)} chunks, skipped {skipped} unchanged, deleted {len(to_delete)} obsolete ({filepath})")
        elif skipped > 0:
            logger.info(f"All {skipped} chunks unchanged, deleted {len(to_delete)} obsolete ({filepath})")
            
        store.upsert_entities(filepath, metadata)

        # Trigger retroactive summary generation if file is core but lacks a summary
        if metadata.get("core") is True and not metadata.get("summary"):
            logger.info(f"File {filepath} has core:true but lacks summary. Scheduling generation...")
            self._schedule_summary_generation()

    def on_modified(self, event: FileModifiedEvent | DirModifiedEvent) -> None:
        """Hook triggered implicitly by watchdog native listener.

        Args:
            event: Generic OS system notification block.
        """
        if not event.is_directory and self.is_valid_file(event.src_path):
            self._process_file(event.src_path)

    def on_created(self, event: FileCreatedEvent | DirCreatedEvent) -> None:
        """Hook triggered explicitly upon physical file creations natively.

        Args:
            event: Generic OS system notification block.
        """
        if not event.is_directory and self.is_valid_file(event.src_path):
            self._process_file(event.src_path)

    def on_deleted(self, event: FileDeletedEvent | DirDeletedEvent) -> None:
        """Safely remove system traces preventing ghost responses inside API chunks.

        Args:
            event: Generic OS FileEvent.
        """
        if not event.is_directory and self.is_valid_file(event.src_path):
            logger.info(f"Deleting chunks for: {event.src_path}")
            store.delete_file_chunks(event.src_path)


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
