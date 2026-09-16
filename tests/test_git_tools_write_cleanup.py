"""Regression coverage for failed atomic-write cleanup."""
import os
import stat

import pytest

from palinode.core import git_tools
from palinode.core.config import config


@pytest.mark.skipif(os.name != "nt", reason="native Windows read-only attributes")
def test_readonly_destination_failure_cleans_temp_and_preserves_destination(tmp_path, monkeypatch):
    """A copied read-only mode must not strand the write's temporary file."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    target = tmp_path / "memory.md"
    sentinel = tmp_path / "unrelated.tmp"
    target.write_text("original: café and 中文\n", encoding="utf-8")
    sentinel.write_text("leave alone\n", encoding="utf-8")
    target.chmod(stat.S_IREAD)
    sentinel.chmod(stat.S_IREAD)
    target_mode = stat.S_IMODE(target.stat().st_mode)
    sentinel_mode = stat.S_IMODE(sentinel.stat().st_mode)
    replace_errors = []
    real_replace = os.replace

    def record_replace(*args):
        try:
            return real_replace(*args)
        except OSError as error:
            replace_errors.append(error)
            raise

    monkeypatch.setattr(git_tools.os, "replace", record_replace)
    try:
        with pytest.raises(PermissionError) as caught:
            git_tools.write_memory_file(str(target), "replacement\n")

        assert target.read_text(encoding="utf-8") == "original: café and 中文\n"
        assert stat.S_IMODE(target.stat().st_mode) == target_mode
        assert sentinel.read_text(encoding="utf-8") == "leave alone\n"
        assert stat.S_IMODE(sentinel.stat().st_mode) == sentinel_mode
        assert list(tmp_path.glob("*.tmp")) == [sentinel]
        assert len(replace_errors) == 1
        assert caught.value is replace_errors[0]
        assert caught.value.filename2 == str(target)
    finally:
        # These are only this test's direct temporary files, including an orphan
        # left by the unfixed implementation; restore attributes for teardown.
        for item in tmp_path.iterdir():
            if item.is_file() and not item.is_symlink():
                item.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_writable_overwrite_preserves_bytes_mode_and_leaves_no_temp(tmp_path, monkeypatch):
    """Successful writes must retain UTF-8 bytes and the existing file mode."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    target = tmp_path / "memory.md"
    target.write_text("original", encoding="utf-8")
    target_mode = stat.S_IMODE(target.stat().st_mode)

    git_tools.write_memory_file(str(target), "café and 中文")

    assert target.read_bytes() == "café and 中文".encode("utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == target_mode
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("cleanup_error", [OSError("cleanup failed"), PermissionError("cleanup denied")])
def test_cleanup_error_preserves_original_write_failure(tmp_path, monkeypatch, caplog, cleanup_error):
    """A failure to clean up must not replace the original write exception."""
    monkeypatch.setattr(config, "memory_dir", str(tmp_path))
    target = tmp_path / "memory.md"
    target.write_text("original\n", encoding="utf-8")
    original_error = OSError("original replace failure")

    def fail_replace(*args):
        raise original_error

    def fail_unlink(*args):
        raise cleanup_error

    with monkeypatch.context() as faults:
        faults.setattr(git_tools.os, "replace", fail_replace)
        faults.setattr(git_tools.os, "unlink", fail_unlink)
        with pytest.raises(OSError) as caught:
            git_tools.write_memory_file(str(target), "replacement\n")

    assert caught.value is original_error
    assert target.read_text(encoding="utf-8") == "original\n"
    assert any("cleanup" in record.message.lower() for record in caplog.records)
