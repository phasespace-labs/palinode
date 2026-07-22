"""Regression tests for the project-status one-liner written by session-end (#681).

Before #681 this writer referenced only ``req.summary``: ``decisions[]`` and
``blockers[]`` were dropped with no log line and no trace in the file, and the
summary was cut at a bare 200 chars mid-word with no ellipsis — so a reader
could not tell "the summary ended there" from "the summary was cut".

The contract these tests pin:
  1. every array entry survives to every writer that claims to record it —
     verbatim in the daily note and the indexed file, as a *count plus pointer*
     in the status file (which is a one-line-per-session longitudinal index);
  2. truncation is always marked and lands on a word boundary;
  3. a request with no arrays still produces the old clean single-line shape.
"""
from __future__ import annotations

import os
import re
from unittest import mock

import pytest

from palinode.api.routers.session import (
    STATUS_SUMMARY_MAX_CHARS,
    _status_line,
    _truncate_marked,
)
from palinode.core.config import config


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Per-test SQLite DB so session-end dedup never queries the real store.

    Same rationale as tests/test_session_end.py: ``_check_session_end_dedup``
    reads recent embeddings from ``config.db_path``, and on a machine with live
    Ollama a match in the real DB would suppress the individual file.
    """
    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))


def _run_session_end(tmp_path, monkeypatch, **kwargs):
    """Drive the real handler against a tmp memory dir with a status file present.

    Returns ``(result, status_text)``. Only the network-bound collaborators are
    stubbed — the status write, the daily write and the file layout are real.
    """
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config.git, "auto_commit", False)

    projects_dir = os.path.join(memory_dir, "projects")
    os.makedirs(projects_dir, exist_ok=True)
    status_path = os.path.join(projects_dir, "palinode-status.md")
    with open(status_path, "w") as f:
        f.write("# palinode status\n")

    with mock.patch("palinode.api.routers.session._check_session_end_dedup",
                    return_value=(None, None)):
        from palinode.api.server import SessionEndRequest, session_end_api

        kwargs.setdefault("project", "palinode")
        kwargs.setdefault("source", "test")
        result = session_end_api(SessionEndRequest(**kwargs))

    return result, open(status_path).read()


def _last_status_line(status_text: str) -> str:
    lines = [ln for ln in status_text.splitlines() if ln.startswith("- [")]
    assert lines, f"no dated status line was appended:\n{status_text}"
    return lines[-1]


# ── 1. Arrays survive to every writer ────────────────────────────────────────


def test_arrays_survive_to_every_writer(tmp_path, monkeypatch):
    """Daily note + indexed file carry the entries verbatim; the status file
    records their count and where to read them. Nothing is dropped silently."""
    decisions = ["Use RRF fusion over a linear blend", "Keep the executor deterministic"]
    blockers = ["Smoke the dev rig", "Confirm the embed host"]

    result, status = _run_session_end(
        tmp_path, monkeypatch,
        summary="Landed hybrid search",
        decisions=decisions,
        blockers=blockers,
    )

    daily = open(os.path.join(str(tmp_path), result["daily_file"])).read()
    for entry in decisions + blockers:
        assert entry in daily, f"daily note lost {entry!r}"

    individual = open(result["individual_file"]).read()
    for entry in decisions + blockers:
        assert entry in individual, f"indexed file lost {entry!r}"

    line = _last_status_line(status)
    assert "2 decisions" in line, line
    assert "2 blockers" in line, line


def test_status_line_points_at_the_indexed_file(tmp_path, monkeypatch):
    """The pointer resolves to the durable indexed record, memory-relative.

    Not the daily note: consolidation later moves ``daily/*.md`` into
    ``archive/<year>/``, so a daily pointer goes stale.
    """
    result, status = _run_session_end(
        tmp_path, monkeypatch,
        summary="Pointer check",
        decisions=["only one"],
    )

    expected = os.path.relpath(result["individual_file"], str(tmp_path))
    line = _last_status_line(status)
    assert expected in line, f"{expected!r} not in {line!r}"
    assert expected.startswith("projects/session-end-"), expected


def test_status_line_falls_back_to_daily_when_no_indexed_file(tmp_path, monkeypatch):
    """Dedup suppressed the individual file ⇒ point at the daily note rather
    than emitting a dangling pointer."""
    memory_dir = str(tmp_path)
    monkeypatch.setattr(config, "memory_dir", memory_dir)
    monkeypatch.setattr(config.git, "auto_commit", False)
    os.makedirs(os.path.join(memory_dir, "projects"), exist_ok=True)
    status_path = os.path.join(memory_dir, "projects", "palinode-status.md")
    with open(status_path, "w") as f:
        f.write("# palinode status\n")

    with mock.patch("palinode.api.routers.session._check_session_end_dedup",
                    return_value=("prior-slug", 0.99)):
        from palinode.api.server import SessionEndRequest, session_end_api

        result = session_end_api(SessionEndRequest(
            summary="Deduped session", decisions=["a"], blockers=["b"],
            project="palinode", source="test",
        ))

    assert result["individual_file"] is None
    line = _last_status_line(open(status_path).read())
    assert result["daily_file"] in line, line


def test_singular_counts_read_naturally(tmp_path, monkeypatch):
    _, status = _run_session_end(
        tmp_path, monkeypatch,
        summary="One of each", decisions=["d"], blockers=["b"],
    )
    line = _last_status_line(status)
    assert "1 decision," in line and "1 blocker" in line, line
    assert "1 decisions" not in line and "1 blockers" not in line, line


def test_no_arrays_keeps_the_line_clean(tmp_path, monkeypatch):
    """A caller that passes no arrays gets the pre-#681 shape back: one dated
    line, no annotation. The fix must not add noise where nothing was lost."""
    _, status = _run_session_end(tmp_path, monkeypatch, summary="Plain session")
    line = _last_status_line(status)
    assert line.endswith("Plain session"), line
    assert "→" not in line and "decision" not in line, line


def test_status_entry_is_exactly_one_line(tmp_path, monkeypatch):
    """The status file's contract is one line per session — a multi-line summary
    must be collapsed, not folded into the file as extra bullets."""
    before = "# palinode status\n"
    _, status = _run_session_end(
        tmp_path, monkeypatch,
        summary="line one\n\nline two\twith a tab",
        decisions=["d"],
    )
    appended = [ln for ln in status[len(before):].splitlines() if ln.strip()]
    assert len(appended) == 1, appended
    assert "line one line two with a tab" in appended[0], appended[0]


# ── 2. Truncation is marked and word-aligned ─────────────────────────────────


def test_long_summary_truncation_is_marked_and_word_aligned(tmp_path, monkeypatch):
    summary = " ".join(f"word{i:03d}" for i in range(200))  # ~1600 chars
    assert len(summary) > STATUS_SUMMARY_MAX_CHARS

    _, status = _run_session_end(tmp_path, monkeypatch, summary=summary)
    line = _last_status_line(status)

    assert line.endswith("..."), line
    body = line.split("] ", 1)[1][: -len("...")]
    assert len(body) <= STATUS_SUMMARY_MAX_CHARS, len(body)
    # Word-aligned: the kept text is a whole-word prefix of the original.
    assert summary.startswith(body), body[-40:]
    assert summary[len(body)] == " ", repr(summary[len(body) - 5:len(body) + 5])
    # The reported regression signature — a bare mid-word cut at 200 chars.
    assert body != summary[:200], "the old 200-char hard cut is back"


def test_short_summary_is_never_marked(tmp_path, monkeypatch):
    _, status = _run_session_end(tmp_path, monkeypatch, summary="Short and complete")
    assert _last_status_line(status).endswith("Short and complete")


@pytest.mark.parametrize(
    "text, limit, expected",
    [
        ("abc", 10, "abc"),                       # fits
        ("abcdefghij", 10, "abcdefghij"),         # exactly at the limit
        ("aaa bbb ccc", 8, "aaa bbb..."),         # cut lands mid-word → retreat
        ("aaa bbb ccc", 7, "aaa bbb..."),         # cut lands ON the space → keep
        ("supercalifragilistic", 5, "super..."),  # unbroken token → hard cut
    ],
)
def test_truncate_marked_cases(text, limit, expected):
    assert _truncate_marked(text, limit) == expected


def test_status_line_shape_is_greppable():
    """Pin the rendered shape so downstream readers (and humans skimming the
    file) can rely on it."""
    line = _status_line("2026-07-22", "did a thing", ["d1"], ["b1", "b2"],
                        "projects/session-end-2026-07-22-p-abc123.md")
    assert re.fullmatch(
        r"- \[2026-07-22\] did a thing "
        r"\(1 decision, 2 blockers → projects/session-end-2026-07-22-p-abc123\.md\)",
        line,
    ), line


def test_status_line_omits_absent_array(tmp_path, monkeypatch):
    _, status = _run_session_end(
        tmp_path, monkeypatch, summary="Only blockers", blockers=["b1", "b2", "b3"])
    line = _last_status_line(status)
    assert "3 blockers" in line and "decision" not in line, line
