from __future__ import annotations

from pathlib import Path

from palinode.core.config import Config
from palinode.diagnostics.checks.operations_log import operations_log_size
from palinode.diagnostics.registry import all_checks
from palinode.diagnostics.types import DoctorContext


def _ctx(tmp_path: Path, limit: int = 10) -> DoctorContext:
    config = Config(memory_dir=str(tmp_path))
    config.logging.operations_max_bytes = limit
    return DoctorContext(config=config)


def test_missing_and_below_limit_logs_pass(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert operations_log_size(ctx).passed
    path = tmp_path / "logs" / "operations.jsonl"
    path.parent.mkdir()
    path.write_bytes(b"1234567890")
    assert operations_log_size(ctx).passed


def test_oversized_log_warns_with_sizes_and_future_write_hint(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    path = tmp_path / "logs" / "operations.jsonl"
    path.parent.mkdir()
    path.write_bytes(b"12345678901")
    result = operations_log_size(ctx)
    assert not result.passed
    assert result.severity == "warn"
    assert "11 bytes" in result.message and "10 bytes" in result.message
    assert "future write" in result.message


def test_zero_limit_passes_and_check_is_fast(tmp_path: Path) -> None:
    assert operations_log_size(_ctx(tmp_path, limit=0)).passed
    registrations = {fn.__name__: tags for fn, tags in all_checks()}
    assert registrations["operations_log_size"] == ("fast",)
