from __future__ import annotations

import logging
from pathlib import Path

from palinode.core.log_files import LazyDirFileHandler


def _emit(handler: logging.Handler, message: str) -> None:
    handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None))


def test_handler_is_lazy_and_creates_file_on_first_record(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "operations.jsonl"
    handler = LazyDirFileHandler(str(target), max_bytes=100, backup_count=2)
    try:
        assert not target.parent.exists()
        _emit(handler, "first")
        assert target.read_text(encoding="utf-8") == "first\n"
    finally:
        handler.close()


def test_future_write_rotates_preexisting_oversized_file(tmp_path: Path) -> None:
    target = tmp_path / "operations.jsonl"
    target.write_text("existing history\n", encoding="utf-8")
    handler = LazyDirFileHandler(str(target), max_bytes=8, backup_count=2)
    try:
        assert target.read_text(encoding="utf-8") == "existing history\n"
        assert not (tmp_path / "operations.jsonl.1").exists()
        _emit(handler, "new")
        assert (tmp_path / "operations.jsonl.1").read_text(encoding="utf-8") == "existing history\n"
        assert target.read_text(encoding="utf-8") == "new\n"
    finally:
        handler.close()


def test_rotation_retains_only_configured_generations(tmp_path: Path) -> None:
    target = tmp_path / "operations.jsonl"
    handler = LazyDirFileHandler(str(target), max_bytes=4, backup_count=2)
    try:
        for value in ("one", "two", "three", "four"):
            _emit(handler, value)
        assert (tmp_path / "operations.jsonl.1").exists()
        assert (tmp_path / "operations.jsonl.2").exists()
        assert not (tmp_path / "operations.jsonl.3").exists()
        assert "one" not in "".join(
            path.read_text(encoding="utf-8") for path in tmp_path.iterdir()
        )
    finally:
        handler.close()
